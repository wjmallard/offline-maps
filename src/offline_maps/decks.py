"""Waypoint deck registry: swappable point sets the viewer lists and queries.

A deck is any entry in the decks directory (config decks.dir):

    <name>.parquet / .geojson / .json / .gpx / .gpkg    bare file of points
    <name>/                                             directory with points.parquet
                                                        (+ optional meta.parquet,
                                                        photos/, thumbs/)

Drop a deck in and it is selectable on the next page load: the registry rescans
the directory on every listing (cheap — no data is read) and lazy-loads each
deck's GeoDataFrame on first query, cached against the points file's mtime so a
re-synced or replaced deck is picked up without a restart.

Every deck is normalized the same way at load: reprojected to WGS84, filtered to
Point geometries, and given a stable string `id` column (the key that joins
points to photos and enrichment metadata) — taken from an existing `id` column,
else `osm_id`, else the row number.
"""

import geopandas
import pyarrow.parquet

from offline_maps import config

_BARE_SUFFIXES = (
    ".geojson",
    ".gpkg",
    ".gpx",
    ".json",
    ".parquet",
)

_cache = {}  # deck id -> (points-file signature, normalized GeoDataFrame)


class Deck:
    def __init__(self, deck_id, points_path):
        self.id = deck_id
        self.name = deck_id
        self.points_path = points_path

    def _signature(self):
        stat = self.points_path.stat()
        return (str(self.points_path), stat.st_mtime_ns, stat.st_size)

    @property
    def gdf(self):
        signature = self._signature()
        cached = _cache.get(self.id)
        if cached is None or cached[0] != signature:
            _cache[self.id] = (signature, _normalize(_read_points(self.points_path)))
        return _cache[self.id][1]

    @property
    def count(self):
        """Point count when knowable without parsing the whole file, else None."""
        cached = _cache.get(self.id)
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


def list_decks():
    """Scan the decks directory; return Decks sorted by id. Reads no point data."""
    if not config.DECKS_DIR.is_dir():
        return []
    found = {}
    for entry in sorted(config.DECKS_DIR.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            points = entry / "points.parquet"
            if points.is_file():
                found.setdefault(entry.name, Deck(entry.name, points))
        elif entry.suffix.lower() in _BARE_SUFFIXES:
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


def _read_points(path):
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
