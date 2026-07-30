"""Enrich the ALPR points with full OSM tags + edit metadata (optional sidecar).

`offline-maps-sync` mirrors DeFlock's CDN, which publishes only a 9-tag whitelist
and no edit metadata. This tool takes the osm_ids already in the points GeoParquet
and fetches each node's complete record straight from OpenStreetMap *by id* — a
direct lookup with no spatial search, so it stays fast and reliable where a global
tag query times out. It writes a sidecar GeoParquet keyed by osm_id: every OSM tag
(as a JSON dict) plus version / timestamp / changeset / user.

The fetch is **resumable**: each batch is appended to a JSONL checkpoint next to the
output as it completes, and a re-run skips ids already in that checkpoint. So an
interrupted or rate-limited run picks up where it left off instead of starting over.

The viewer ignores this file unless you wire it in; it is a standalone artifact you
join onto the points by osm_id. OSM data is ODbL (attribution + share-alike).
"""

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas
import pandas
from tqdm import tqdm

from offline_maps import config
from offline_maps.sync import write_parquet

_USER_AGENT = "offline-maps-alpr-sync/0.1 (personal use)"
_REQUEST_TIMEOUT_S = 90
_MAX_RETRIES = 6
_BACKOFF_BASE_S = 5.0
_BACKOFF_MAX_S = 120.0
_POLITENESS_DELAY_S = 1.0
_CHECKPOINT_SUFFIX = ".partial.jsonl"


def _chunked(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _retry_after_seconds(error):
    """Seconds to wait per an HTTP Retry-After header, if the server sent one."""
    if isinstance(error, urllib.error.HTTPError):
        value = error.headers.get("Retry-After")
        if value and value.strip().isdigit():
            return int(value.strip())
    return None


def _post_overpass(query):
    """POST an Overpass query, retrying with backoff on transient failures.

    Covers client timeouts, 429 rate limits, and 5xx (all surface as URLError /
    HTTPError / TimeoutError); honors a Retry-After header when the server sends one.
    """
    data = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(_MAX_RETRIES):
        try:
            request = urllib.request.Request(
                config.SYNC_OSM_OVERPASS_URL,
                data=data,
                headers={"User-Agent": _USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
                result = json.loads(response.read())
            elements = result.get("elements")
            if elements is None:
                raise urllib.error.URLError("Overpass returned no elements")
            return elements
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt == _MAX_RETRIES - 1:
                raise
            delay = _retry_after_seconds(error)
            if delay is None:
                delay = min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * 2 ** attempt)
            time.sleep(delay)


def load_osm_ids(points_path, limit):
    """Read the osm_ids to enrich from the points GeoParquet (the viewer's file)."""
    ids = geopandas.read_parquet(points_path)["osm_id"].tolist()
    return ids[:limit] if limit else ids


def _read_checkpoint(path):
    """Yield elements previously saved to the JSONL checkpoint, skipping any
    truncated trailing line left behind by an interrupted run."""
    with path.open() as checkpoint:
        for line in checkpoint:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def fetch_osm_records(osm_ids, batch_size, checkpoint_path):
    """Fetch full OSM records (meta + all tags) for the given ids, batched by id.

    Appends each completed batch to `checkpoint_path` (flushed) and skips ids already
    present there, so an interrupted run resumes rather than restarting.
    """
    wanted = list(dict.fromkeys(osm_ids))
    fetched = {}
    if checkpoint_path.exists():
        for element in _read_checkpoint(checkpoint_path):
            fetched[element["id"]] = element
    remaining = [osm_id for osm_id in wanted if osm_id not in fetched]
    if fetched:
        print(f"Resuming: {len(wanted) - len(remaining):,} already fetched, {len(remaining):,} to go")

    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("a") as checkpoint:
        for batch in tqdm(list(_chunked(remaining, batch_size)), desc="batches", unit="batch"):
            id_list = ",".join(str(osm_id) for osm_id in batch)
            query = f"[out:json][timeout:{_REQUEST_TIMEOUT_S}];node(id:{id_list});out meta;"
            for element in _post_overpass(query):
                fetched[element["id"]] = element
                checkpoint.write(json.dumps(element) + "\n")
            checkpoint.flush()
            time.sleep(_POLITENESS_DELAY_S)
    return [fetched[osm_id] for osm_id in wanted if osm_id in fetched]


def build_meta_gdf(elements):
    """Build a WGS84 point GeoDataFrame of OSM metadata + the full tag dict as JSON."""
    records = []
    lons = []
    lats = []
    for element in elements:
        lons.append(element["lon"])
        lats.append(element["lat"])
        records.append(
            {
                "osm_id": element["id"],
                "version": element.get("version"),
                "changeset": element.get("changeset"),
                "osm_timestamp": element.get("timestamp"),
                "user": element.get("user"),
                "uid": element.get("uid"),
                "tags": json.dumps(element.get("tags", {}), sort_keys=True),
            }
        )
    gdf = geopandas.GeoDataFrame(
        records,
        geometry=geopandas.points_from_xy(lons, lats),
        crs="EPSG:4326",
    )
    gdf["osm_timestamp"] = pandas.to_datetime(gdf["osm_timestamp"], utc=True)
    return gdf


def main():
    parser = argparse.ArgumentParser(
        description="Enrich the ALPR points with full OSM tags + metadata (optional sidecar).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=config.SYNC_OSM_OUTPUT,
        help="destination sidecar GeoParquet (default: the alpr deck's meta.parquet)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only enrich the first N points (for a quick test run)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="ignore any resume checkpoint and re-fetch everything",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and report, but do not write the parquet",
    )
    args = parser.parse_args()

    if not config.SYNC_POINTS_PARQUET.exists():
        parser.error(
            f"{config.SYNC_POINTS_PARQUET} not found — run offline-maps-sync first.",
        )

    checkpoint_path = Path(f"{args.output}{_CHECKPOINT_SUFFIX}")
    if args.full:
        checkpoint_path.unlink(missing_ok=True)

    osm_ids = load_osm_ids(config.SYNC_POINTS_PARQUET, args.limit)
    print(f"Enriching {len(osm_ids):,} points from {config.SYNC_OSM_OVERPASS_URL} ...")
    try:
        elements = fetch_osm_records(osm_ids, config.SYNC_OSM_BATCH_SIZE, checkpoint_path)
    except (KeyboardInterrupt, urllib.error.URLError, TimeoutError) as error:
        print(
            f"\nStopped ({type(error).__name__}). Progress saved to {checkpoint_path} — "
            f"re-run the same command to resume.",
        )
        raise SystemExit(1)

    gdf = build_meta_gdf(elements)
    print(f"Fetched full OSM records for {len(gdf):,} of {len(osm_ids):,} points")

    if args.dry_run:
        print("Dry run — nothing written.")
        return

    write_parquet(gdf, args.output)
    checkpoint_path.unlink(missing_ok=True)
    print(f"Wrote {len(gdf):,} enriched records to {args.output}")


if __name__ == "__main__":
    main()
