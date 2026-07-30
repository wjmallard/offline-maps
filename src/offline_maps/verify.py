"""Inspect a received deck before slotting it in: structure, data, contents.

The viewer's own guarantees do the heavy lifting — property values are escaped,
ids are validated, and the CSP blocks any off-origin request no matter what a
deck contains. This tool is the pre-flight report for the human: structural
problems (a corrupt or path-traversing zip, unreadable points, images that are
not images) come back as errors and a non-zero exit; everything else — URLs
sitting in values, HTML-looking text, missing attribution — is reported for
eyeballing, because values like wikimedia_commons=https://... are legitimate
provenance data that render as inert text.

Works on any deck form: a bare points file, a deck directory, or a .deck zip.
"""

import argparse
import io
import re
import zipfile
from pathlib import Path

import pandas.api.types
import pyarrow.parquet
from tqdm import tqdm

from offline_maps import decks

_URL_RE = re.compile(r"(?:https?:)?//[^\s\"'<>]{3,}", re.IGNORECASE)
_HTML_RE = re.compile(r"<[a-zA-Z!/]|javascript:|\bon\w+\s*=", re.IGNORECASE)

_BOMB_RATIO = 50
_BOMB_BYTES = 500_000_000


class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.lines = []

    def error(self, text):
        self.errors.append(text)

    def warn(self, text):
        self.warnings.append(text)

    def note(self, text):
        self.lines.append(text)


def _open_deck(path, report):
    """Build the right Deck for a standalone path; None when unusable."""
    if path.is_dir():
        if not (path / "points.parquet").is_file():
            report.error("directory has no points.parquet — not a deck")
            return None
        return decks.DirDeck(path.name, path)
    if not path.is_file():
        report.error("no such file")
        return None
    suffix = path.suffix.lower()
    if suffix == ".deck":
        try:
            return decks.ZipDeck(path.stem, path)
        except (zipfile.BadZipFile, decks.DeckError) as error:
            report.error(f"unusable zip: {error}")
            return None
    if suffix in decks._BARE_SUFFIXES:
        return decks.Deck(path.stem, path)
    report.error(f"unrecognized deck form ({path.suffix or 'no suffix'})")
    return None


def _check_zip(path, report):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
    names = [info.filename for info in infos]
    dupes = len(names) - len(set(names))
    if dupes:
        report.error(f"zip: {dupes} duplicate member names")
    stored = sum(1 for info in infos if info.compress_type == zipfile.ZIP_STORED)
    uncompressed = sum(info.file_size for info in infos)
    compressed = sum(info.compress_size for info in infos)
    ratio = uncompressed / compressed if compressed else 1.0
    if uncompressed > _BOMB_BYTES and ratio > _BOMB_RATIO:
        report.error(
            f"zip: expands {ratio:,.0f}x to {uncompressed / 1e9:,.1f} GB — looks like a zip bomb",
        )
    if stored < len(infos):
        report.warn(f"zip: {len(infos) - stored} compressed members (decks are normally stored)")
    report.note(
        f"zip: {len(infos):,} members, names clean, "
        f"{uncompressed / 1e6:,.1f} MB (ratio {ratio:,.1f}x)",
    )


def _check_points(deck, report):
    try:
        raw = deck._read_points()
    except Exception as error:  # any parse failure in the geo stack
        report.error(f"points: unreadable ({type(error).__name__}: {error})")
        return None
    dropped = int((raw.geometry.geom_type != "Point").sum())
    gdf = decks._normalize(raw)
    if dropped:
        report.warn(f"points: {dropped:,} non-Point geometries dropped by the viewer")
    if not len(gdf):
        report.warn("points: deck is empty")
        return gdf
    bounds = gdf.total_bounds
    if not (
        -180.001 <= bounds[0] <= bounds[2] <= 180.001
        and -90.001 <= bounds[1] <= bounds[3] <= 90.001
    ):
        report.error(f"points: coordinates outside WGS84 range (bounds {bounds.tolist()})")
    dupe_ids = int(gdf["id"].duplicated().sum())
    if dupe_ids:
        report.warn(f"points: {dupe_ids:,} duplicate ids (photo/meta joins will collide)")
    report.note(f"points: {len(gdf):,} points, ids {'unique' if not dupe_ids else 'NOT unique'}")
    return gdf


def _check_meta(deck, gdf, report):
    source = deck._meta_source()
    if source is None:
        return
    try:
        schema = pyarrow.parquet.read_schema(source)
        if hasattr(source, "seek"):
            source.seek(0)
        key = next((k for k in ("id", "osm_id") if k in schema.names), None)
        if key is None:
            report.warn("meta: no id/osm_id column — records can never match a point")
            return
        table = pyarrow.parquet.read_table(source, columns=[key])
    except Exception as error:
        report.error(f"meta: unreadable ({type(error).__name__}: {error})")
        return
    meta_ids = {str(v) for v in table.column(key).to_pylist()}
    point_ids = set(gdf["id"]) if gdf is not None else set()
    orphans = len(meta_ids - point_ids)
    report.note(
        f"meta: {table.num_rows:,} records keyed by {key}"
        + (f"; {orphans:,} match no point" if orphans else "; all match points"),
    )


def _iter_images(deck):
    """Yield (label, open_bytes) for every photo/thumb the deck carries."""
    if isinstance(deck, decks.DirDeck):
        for sub in ("photos", "thumbs"):
            directory = deck.dir / sub
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.is_file() and not path.name.startswith(".") and path.suffix != ".tmp":
                    yield f"{sub}/{path.name}", path.read_bytes
    elif isinstance(deck, decks.ZipDeck):
        index = deck._index
        names = sorted(list(index["photos"].values()) + list(index["thumbs"].values()))
        for name in names:
            def read(name=name):
                with zipfile.ZipFile(deck.zip_path) as archive:
                    return archive.read(name)
            yield name, read


def _check_images(deck, gdf, report):
    from PIL import Image  # heavy optional dependency, imported lazily

    entries = list(_iter_images(deck))
    if not entries:
        return
    bad = []
    orphans = 0
    point_ids = set(gdf["id"]) if gdf is not None else set()
    for label, read in tqdm(entries, desc="images", unit="img", disable=len(entries) < 200):
        stem = Path(label).name.rsplit(".", 1)[0]
        if point_ids and stem not in point_ids:
            orphans += 1
        try:
            with Image.open(io.BytesIO(read())) as image:
                image.verify()
        except Exception:
            bad.append(label)
    if bad:
        report.error(
            f"images: {len(bad):,} of {len(entries):,} do not parse as images: "
            + ", ".join(bad[:5]),
        )
    else:
        report.note(f"images: {len(entries):,} — all parse as images")
    if orphans:
        report.warn(f"images: {orphans:,} match no point id")


def _audit_strings(deck, gdf, report):
    """Count URLs and HTML-looking text in everything the deck could render."""
    url_hits = {}
    html_hits = {}
    html_samples = []

    def scan(label, values):
        urls = html = 0
        for value in values:
            if not isinstance(value, str) or not value:
                continue
            if _URL_RE.search(value):
                urls += 1
            match = _HTML_RE.search(value)
            if match:
                html += 1
                if len(html_samples) < 5:
                    window = value[max(0, match.start() - 30):match.start() + 50]
                    html_samples.append(f"{label}: …{window!r}…")
        if urls:
            url_hits[label] = urls
        if html:
            html_hits[label] = html

    if gdf is not None:
        for col in gdf.columns:
            if col == gdf.geometry.name:
                continue
            if pandas.api.types.is_string_dtype(gdf[col]):
                scan(col, gdf[col])
    source = deck._meta_source()
    if source is not None:
        try:
            schema = pyarrow.parquet.read_schema(source)
            if hasattr(source, "seek"):
                source.seek(0)
            if "tags" in schema.names:
                table = pyarrow.parquet.read_table(source, columns=["tags"])
                scan("meta.tags", table.column("tags").to_pylist())
        except Exception:
            pass  # already reported by _check_meta
    scan("deck.yaml", deck.info.values())

    if url_hits:
        counts = ", ".join(f"{k}: {v:,}" for k, v in sorted(url_hits.items()))
        report.note(f"URLs in values (inert text in the viewer): {counts}")
    else:
        report.note("URLs in values: none")
    if html_hits:
        counts = ", ".join(f"{k}: {v:,}" for k, v in sorted(html_hits.items()))
        report.warn(f"HTML-looking values (escaped by the viewer, but eyeball them): {counts}")
        for sample in html_samples:
            report.warn(f"  {sample}")


def verify(path):
    report = Report()
    deck = _open_deck(path, report)
    if deck is not None:
        if isinstance(deck, decks.ZipDeck):
            _check_zip(path, report)
        gdf = _check_points(deck, report)
        _check_meta(deck, gdf, report)
        _check_images(deck, gdf, report)
        _audit_strings(deck, gdf, report)
        info = deck.info
        if info.get("name"):
            report.note(f"deck.yaml: name {info['name']!r}"
                        + (", attribution present" if info.get("attribution") else ""))
        if not info.get("attribution"):
            report.warn("no attribution (deck.yaml) — add one before trading derived data")
    return report


def main():
    parser = argparse.ArgumentParser(
        description="Inspect a received deck: structure errors, data shape, content audit.",
    )
    parser.add_argument(
        "deck",
        type=Path,
        help="deck to verify: a bare points file, a deck directory, or a .deck zip",
    )
    args = parser.parse_args()

    report = verify(args.deck)
    print(f"Verifying {args.deck}")
    for line in report.lines:
        print(f"  ok    {line}")
    for line in report.warnings:
        print(f"  note  {line}")
    for line in report.errors:
        print(f"  ERROR {line}")
    if report.errors:
        print(f"{len(report.errors)} error(s) — do not use this deck.")
        raise SystemExit(1)
    print("No errors." + (f" {len(report.warnings)} note(s) above." if report.warnings else ""))


if __name__ == "__main__":
    main()
