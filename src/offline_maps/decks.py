"""Waypoint deck registry: swappable point sets the viewer lists and queries.

A deck is any entry in the decks directory (config decks.dir):

    <name>.parquet / .geojson / .json / .gpx / .gpkg    bare file of points
    <name>/                                             deck directory
    <name>.deck                                         zipped deck directory

A deck directory holds points.parquet plus optional pieces, and a .deck file is
an ordinary zip of the same layout (stored, not compressed, so members can be
served straight out of it — see pack.py):

    points.parquet      the point set (required)
    meta.parquet        per-point detail records, keyed by id (optional)
    deck.yaml           name / description / attribution / license (optional)
    photos/<id>.<ext>   full-resolution images for the lightbox (optional)
    thumbs/<id>.jpg     popup-sized thumbnails (optional)

Drop a deck in and it is selectable on the next page load: the registry rescans
the directory on every listing (cheap — no point data is read) and lazy-loads
each deck's GeoDataFrame on first query, cached against the points file's mtime
so a re-synced or replaced deck is picked up without a restart.

Every deck is normalized the same way at load: reprojected to WGS84, filtered to
Point geometries, and given a stable string `id` column (the key that joins
points to photos and meta records) — taken from an existing `id` column, else
`osm_id`, else the row number.

Traded decks are untrusted input: zip member names are validated before any use
(a deck that fails validation is skipped), and lookups match scanned entries
rather than building filesystem paths from request input.
"""

import datetime
import glob
import io
import json
import math
import zipfile
from pathlib import Path

import geopandas
import pyarrow.parquet
import yaml

from offline_maps import config

_BARE_SUFFIXES = (
    ".geojson",
    ".gpkg",
    ".gpx",
    ".json",
    ".parquet",
)

_INFO_KEYS = (
    "attribution",
    "description",
    "license",
    "name",
)

_gdf_cache = {}  # deck id -> (points signature, normalized GeoDataFrame)
_zip_cache = {}  # zip path -> (file signature, member index, or None if invalid)


class DeckError(Exception):
    """A deck that cannot be used (bad zip, unsafe member names, missing points)."""


class Deck:
    """A bare file of points — the simplest deck: no metadata, no photos."""

    def __init__(self, deck_id, points_path):
        self.id = deck_id
        self.points_path = points_path

    @property
    def info(self):
        return {}

    @property
    def name(self):
        return self.info.get("name") or self.id

    @property
    def has_meta(self):
        return False

    @property
    def has_photos(self):
        return False

    def meta_record(self, point_id):
        """One meta.parquet record as a JSON-safe dict, or None."""
        source = self._meta_source()
        if source is None:
            return None
        return _read_meta_record(source, point_id)

    def _meta_source(self):
        return None

    def photo(self, point_id):
        return None

    def thumb(self, point_id):
        return None

    def _read_points(self):
        return _read_points_file(self.points_path)

    def _signature(self):
        stat = self.points_path.stat()
        return (str(self.points_path), stat.st_mtime_ns, stat.st_size)

    @property
    def gdf(self):
        signature = self._signature()
        cached = _gdf_cache.get(self.id)
        if cached is None or cached[0] != signature:
            _gdf_cache[self.id] = (signature, _normalize(self._read_points()))
        return _gdf_cache[self.id][1]

    @property
    def count(self):
        """Point count when knowable without parsing the whole file, else None."""
        cached = _gdf_cache.get(self.id)
        if cached is not None and cached[0] == self._signature():
            return len(cached[1])
        if self.points_path.suffix == ".parquet":
            return pyarrow.parquet.read_metadata(self.points_path).num_rows
        return None

    def query_bbox(self, minx, miny, maxx, maxy, limit):
        """Return (geojson_str, n_returned, capped) for points within the bbox."""
        subset = self.gdf.cx[minx:maxx, miny:maxy]
        capped = len(subset) > limit
        if capped:
            subset = subset.head(limit)
        # Coerce dtypes GeoJSON can't hold (e.g. datetimes in an unknown schema).
        subset = subset.copy()
        geom = subset.geometry.name
        for col in subset.columns:
            if col == geom:
                continue
            if str(subset[col].dtype).startswith("datetime"):
                subset[col] = subset[col].astype(str)
        return subset.to_json(), len(subset), capped


class DirDeck(Deck):
    """A deck directory: points.parquet beside optional meta, photos, thumbs."""

    def __init__(self, deck_id, deck_dir):
        super().__init__(deck_id, deck_dir / "points.parquet")
        self.dir = deck_dir

    @property
    def info(self):
        return _read_info_file(self.dir / "deck.yaml")

    @property
    def has_meta(self):
        return (self.dir / "meta.parquet").is_file()

    @property
    def has_photos(self):
        return (self.dir / "thumbs").is_dir() or (self.dir / "photos").is_dir()

    def _meta_source(self):
        path = self.dir / "meta.parquet"
        return path if path.is_file() else None

    def photo(self, point_id):
        photos = self.dir / "photos"
        if not photos.is_dir():
            return None
        return next(
            (p for p in photos.glob(f"{glob.escape(point_id)}.*") if p.suffix != ".tmp"),
            None,
        )

    def thumb(self, point_id):
        path = self.dir / "thumbs" / f"{point_id}.jpg"
        return path if path.is_file() else None


class ZipDeck(Deck):
    """A .deck file: the deck directory layout inside an ordinary (stored) zip."""

    def __init__(self, deck_id, zip_path):
        super().__init__(deck_id, zip_path)
        self.zip_path = zip_path
        self._index = _zip_index(zip_path)
        if "points.parquet" not in self._index["names"]:
            raise DeckError("no points.parquet member")

    @property
    def info(self):
        return self._index["info"]

    @property
    def has_meta(self):
        return "meta.parquet" in self._index["names"]

    @property
    def has_photos(self):
        return bool(self._index["photos"] or self._index["thumbs"])

    def _read_points(self):
        with zipfile.ZipFile(self.zip_path) as archive:
            return geopandas.read_parquet(io.BytesIO(archive.read("points.parquet")))

    @property
    def count(self):
        cached = _gdf_cache.get(self.id)
        if cached is not None and cached[0] == self._signature():
            return len(cached[1])
        # Stored members are seekable, so this reads only the parquet footer.
        with zipfile.ZipFile(self.zip_path) as archive:
            with archive.open("points.parquet") as member:
                return pyarrow.parquet.read_metadata(member).num_rows

    def _meta_source(self):
        if not self.has_meta:
            return None
        with zipfile.ZipFile(self.zip_path) as archive:
            return io.BytesIO(archive.read("meta.parquet"))

    def photo(self, point_id):
        return self._open_member(self._index["photos"].get(point_id))

    def thumb(self, point_id):
        return self._open_member(self._index["thumbs"].get(point_id))

    def _open_member(self, name):
        """Return (fileobj, filename) streaming one member, or None.

        zipfile refcounts the underlying handle, so closing the ZipFile here
        keeps the member readable until the response finishes with it.
        """
        if name is None:
            return None
        archive = zipfile.ZipFile(self.zip_path)
        try:
            return archive.open(name), Path(name).name
        finally:
            archive.close()


def list_decks():
    """Scan the decks directory; return Decks sorted by id. Reads no point data."""
    if not config.DECKS_DIR.is_dir():
        return []
    found = {}
    for entry in sorted(config.DECKS_DIR.iterdir()):
        if entry.name.startswith("."):
            continue
        suffix = entry.suffix.lower()
        if entry.is_dir():
            if (entry / "points.parquet").is_file():
                found.setdefault(entry.name, DirDeck(entry.name, entry))
        elif suffix == ".deck":
            try:
                found.setdefault(entry.stem, ZipDeck(entry.stem, entry))
            except (zipfile.BadZipFile, DeckError):
                continue  # already reported when first seen (see _zip_index)
        elif suffix in _BARE_SUFFIXES:
            found.setdefault(entry.stem, Deck(entry.stem, entry))
    return [found[deck_id] for deck_id in sorted(found)]


def get_deck(deck_id):
    """Look up one deck by id against a fresh scan.

    Matching against scanned entries (never building a path from the id) is what
    makes traversal via a crafted deck id structurally impossible.
    """
    for deck in list_decks():
        if deck.id == deck_id:
            return deck
    return None


def _zip_index(zip_path):
    """Validated member index for a .deck zip, cached against the file signature."""
    stat = zip_path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _zip_cache.get(str(zip_path))
    if cached is not None and cached[0] == signature:
        if cached[1] is None:
            raise DeckError("failed validation")
        return cached[1]
    try:
        index = _build_zip_index(zip_path)
    except (zipfile.BadZipFile, DeckError) as error:
        _zip_cache[str(zip_path)] = (signature, None)
        print(f"decks: skipping {zip_path.name}: {error}")
        raise
    _zip_cache[str(zip_path)] = (signature, index)
    return index


def _build_zip_index(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        for name in names:
            if name.startswith("/") or "\\" in name or ".." in name.split("/"):
                raise DeckError(f"unsafe member name {name!r}")
        info = _parse_info(archive.read("deck.yaml")) if "deck.yaml" in names else {}
    photos = {}
    thumbs = {}
    for name in names:
        head, _, tail = name.partition("/")
        if not tail or "/" in tail or tail.startswith(".") or tail.endswith(".tmp"):
            continue
        stem = tail.rsplit(".", 1)[0]
        if head == "photos":
            photos.setdefault(stem, name)
        elif head == "thumbs":
            thumbs.setdefault(stem, name)
    return {
        "names": set(names),
        "photos": photos,
        "thumbs": thumbs,
        "info": info,
    }


def _read_info_file(path):
    try:
        return _parse_info(path.read_text()) if path.is_file() else {}
    except (OSError, UnicodeDecodeError):
        return {}


def _parse_info(raw):
    """deck.yaml -> the whitelisted string fields; anything malformed becomes {}."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in _INFO_KEYS if isinstance(data.get(key), str)}


def _read_meta_record(source, point_id):
    """Read one record from a meta.parquet path or buffer, as a JSON-safe dict.

    The read is filtered (predicate pushdown), so the full meta table never sits
    in memory or ships in a bbox response. Records key on a string `id` column,
    falling back to the alpr sidecar's integer `osm_id`.
    """
    schema = pyarrow.parquet.read_schema(source)
    if hasattr(source, "seek"):
        source.seek(0)
    if "id" in schema.names:
        record_filter = ("id", "==", str(point_id))
    elif "osm_id" in schema.names and str(point_id).lstrip("-").isdigit():
        record_filter = ("osm_id", "==", int(point_id))
    else:
        return None
    columns = [name for name in schema.names if name != "geometry"]
    table = pyarrow.parquet.read_table(
        source,
        columns=columns,
        filters=[record_filter],
    )
    if table.num_rows == 0:
        return None
    record = {name: table.column(name)[0].as_py() for name in table.column_names}
    if "tags" in record and isinstance(record["tags"], str):
        try:
            record["tags"] = json.loads(record["tags"]) if record["tags"] else {}
        except json.JSONDecodeError:
            record["tags"] = {}
    for key, value in record.items():
        if isinstance(value, datetime.datetime):
            record[key] = value.isoformat()
        elif isinstance(value, float) and math.isnan(value):
            record[key] = None
    return record


def _read_points_file(path):
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return geopandas.read_parquet(path)
    if suffix == ".gpx":
        return geopandas.read_file(path, layer="waypoints")
    return geopandas.read_file(path)


def _normalize(gdf):
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs("EPSG:4326")
    gdf = gdf[gdf.geometry.geom_type == "Point"].reset_index(drop=True)
    if "id" in gdf.columns:
        gdf["id"] = gdf["id"].astype(str)
    elif "osm_id" in gdf.columns:
        gdf["id"] = gdf["osm_id"].astype(str)
    else:
        gdf["id"] = [str(i) for i in range(len(gdf))]
    _ = gdf.sindex  # build the spatial index eagerly
    return gdf
