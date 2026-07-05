"""Lazy per-node lookup into the enriched OSM sidecar (points_meta.parquet).

The viewer serves the primary points from points.py; this reads a single enriched
record on demand (a popup click) so the full tag set + edit metadata never has to be
held in memory or shipped in the bbox response. Returns None when the sidecar is
absent, so the viewer works with or without an enrichment run.
"""

import json

import pyarrow.parquet as pq

from offline_maps import config

_COLUMNS = [
    "changeset",
    "osm_id",
    "osm_timestamp",
    "tags",
    "uid",
    "user",
    "version",
]


def point_meta(osm_id):
    """Return the enriched OSM record for one node, or None if unavailable."""
    if not config.SYNC_OSM_OUTPUT.exists():
        return None
    table = pq.read_table(
        config.SYNC_OSM_OUTPUT,
        columns=_COLUMNS,
        filters=[("osm_id", "=", osm_id)],
    )
    if table.num_rows == 0:
        return None
    record = {name: table.column(name)[0].as_py() for name in table.column_names}
    record["tags"] = json.loads(record["tags"]) if record["tags"] else {}
    if record["osm_timestamp"] is not None:
        record["osm_timestamp"] = record["osm_timestamp"].isoformat()
    return record
