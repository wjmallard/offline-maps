"""Mirror DeFlock's published ALPR points into the viewer's GeoParquet.

DeFlock regenerates a worldwide set of "Automated License Plate Reader" locations
hourly from OpenStreetMap and serves them as static JSON "region" tiles from its
CDN (S3/R2). Each tile is a list of bare nodes:

    {"id": <int>, "lat": <float>, "lon": <float>, "tags": {<whitelisted subset>}}

This tool fetches the tile index, fetches every listed tile, and writes the
points.parquet of a viewer deck (data/decks/alpr by default, config sync.deck).
It is idempotent: each run refetches the
whole (small) set and atomically overwrites the output, so both new nodes and
deletions simply fall out of a fresh snapshot. If the file is ever wrong, delete
it and re-run.

OSM data is ODbL (attribution + share-alike). DeFlock is the upstream cache; the
GeoParquet is a rebuildable index over it.
"""

import argparse
import json
import os
import urllib.request
from pathlib import Path

import geopandas
from tqdm import tqdm

from offline_maps import config

# OSM tags DeFlock keeps on each node, broken out as their own columns (and shown
# in the viewer's point popups). Alphabetized.
TAG_COLUMNS = [
    "brand",
    "camera:direction",
    "direction",
    "manufacturer",
    "operator",
    "surveillance:brand",
    "surveillance:manufacturer",
    "surveillance:operator",
    "wikimedia_commons",
]

_USER_AGENT = "offline-maps-alpr-sync/0.1 (personal use)"

# Default deck.yaml, written once beside a deck-shaped output so the ODbL
# attribution travels with the deck when it is packed and traded.
_DECK_YAML = """\
name: ALPR cameras
description: Automated license-plate readers worldwide, as mapped in OpenStreetMap;
  mirrored from DeFlock's hourly regenerated dataset.
attribution: © OpenStreetMap contributors, via DeFlock (deflock.me)
license: ODbL 1.0 — opendatacommons.org/licenses/odbl/
"""


def _get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def fetch_nodes(regions_url):
    """Fetch every region tile listed in the index; return one flat list of nodes."""
    index = _get_json(f"{regions_url}/index.json")
    tile_url = index["tile_url"]  # e.g. ".../regions/{lat}/{lon}.json?v=<epoch>"
    nodes = []
    for region in tqdm(index["regions"], desc="tiles", unit="tile"):
        nodes.extend(_get_json(tile_url.replace("{lat}/{lon}", region)))
    return nodes


def build_gdf(nodes):
    """Build a WGS84 point GeoDataFrame with the whitelisted tags as columns."""
    records = []
    lons = []
    lats = []
    for node in nodes:
        tags = node.get("tags", {})
        lons.append(node["lon"])
        lats.append(node["lat"])
        record = {"osm_id": node["id"]}
        for tag in TAG_COLUMNS:
            record[tag] = tags.get(tag)
        records.append(record)
    return geopandas.GeoDataFrame(
        records,
        geometry=geopandas.points_from_xy(lons, lats),
        crs="EPSG:4326",
    )


def write_parquet(gdf, output_path):
    """Write atomically so the viewer never reads a half-written file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.parent / (output_path.name + ".tmp")
    gdf.to_parquet(tmp_path)
    os.replace(tmp_path, output_path)


def write_deck_yaml(points_path):
    """Seed the deck's deck.yaml when the output is deck-shaped and has none yet."""
    if points_path.name != "points.parquet":
        return
    path = points_path.parent / "deck.yaml"
    if not path.exists():
        path.write_text(_DECK_YAML)


def main():
    parser = argparse.ArgumentParser(
        description="Mirror DeFlock's ALPR region tiles into the points GeoParquet.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=config.SYNC_POINTS_PARQUET,
        help="destination GeoParquet (default: the alpr deck's points.parquet)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report the point count, but do not write the parquet",
    )
    args = parser.parse_args()

    nodes = fetch_nodes(config.SYNC_REGIONS_URL)
    gdf = build_gdf(nodes)
    print(f"Fetched {len(gdf):,} ALPR points from {config.SYNC_REGIONS_URL}")

    if args.dry_run:
        print("Dry run — nothing written.")
        return

    write_parquet(gdf, args.output)
    write_deck_yaml(Path(args.output))
    print(f"Wrote {len(gdf):,} points to {args.output}")


if __name__ == "__main__":
    main()
