# Offline Maps

A minimalist, fully offline map viewer. Renders a vendored [Protomaps](https://protomaps.com/)
PMTiles basemap with [MapLibre GL JS](https://maplibre.org/) — no tile server, no CDNs, no API keys.

## Stack

- **Tiles:** Protomaps PMTiles (a single-file vector-tile archive, served as a static file)
- **Renderer:** MapLibre GL JS (vendored, WebGL vector rendering)
- **Backend:** Flask (serves the page and the `.pmtiles` file with HTTP range support)
- **Tooling:** `uv`, `src/` layout, Python 3.12+

Everything the browser loads is vendored into this repo — so the app makes **no
network requests at runtime**; every asset is served from your machine. ("Offline" means
no *non-local* calls — local requests, including dynamically querying local data, are fine.)

## Setup

```bash
cp config.yaml.example config.yaml
uv sync
```

Then vendor the frontend libraries and a basemap (see `scripts/`, coming next), and run:

```bash
uv run offline-maps
```

Open http://127.0.0.1:5000.

## ALPR data

The point layer mirrors [DeFlock](https://deflock.org)'s worldwide dataset of
Automated License Plate Readers (ALPRs) — which DeFlock regenerates hourly from
OpenStreetMap and publishes as static JSON region tiles. To (re)build the local
GeoParquet the viewer reads:

```bash
uv run offline-maps-sync
```

This fetches every region tile (~50 files, ~15 MB) and writes `data/points.parquet`
(~120k points, ~3 MB). It is idempotent — re-run any time to refresh; each run takes
a full snapshot and atomically overwrites the file, so additions and deletions both
just fall out. Point out the source under `sync:` in `config.yaml`.

ALPR locations and tags come from OpenStreetMap, © OpenStreetMap contributors,
licensed under the [ODbL](https://www.openstreetmap.org/copyright); DeFlock is the
upstream cache we mirror.

### Optional: full OSM metadata

DeFlock's tiles carry only a 9-tag whitelist and no edit history. To pull each node's
complete OSM record — every tag, plus version/timestamp/changeset/user — into a sidecar
GeoParquet (`data/points_meta.parquet`), run:

```bash
uv run offline-maps-sync-osm
```

It fetches the nodes already in `points.parquet` *by id* straight from Overpass (a
direct lookup — no bbox harvest), keyed by `osm_id` for joining. The viewer does not
read this file; it's a standalone artifact for analysis. Tune it under `sync.osm:` in
`config.yaml`, or pass `--limit N` for a quick partial run.

### Optional: local photo cache

~1,400 nodes reference a photo (`wikimedia_commons`, `image`/`photo` URLs, `mapillary`,
`panoramax`) — all *external* links. To keep the map fully offline, cache the images
locally so the viewer can serve them from its own `/photos` route:

```bash
uv run offline-maps-sync-photos
```

This downloads one size-capped image per node into `data/photos/<osm_id>.<ext>` (a few
hundred MB). It is resumable — an already-cached file is its own checkpoint, and dead
links are recorded and skipped (`--retry-failed` to retry them). Mapillary's share needs
a free token: copy `.env.example` to `.env`, set `MAPILLARY_TOKEN`, and re-run — it
resumes and fills in the rest. Point clicks in the viewer then show the cached photo.

## Layout

```
src/offline_maps/
  config.py            module-level settings loaded from config.yaml
  web/
    app.py             Flask app + routes (entry point: `offline-maps`)
    templates/         Jinja page
    static/
      vendor/          maplibre-gl.js/.css, pmtiles.js  (vendored)
      styles/          basemap.json                     (vendored)
      glyphs/          font PBF ranges                  (vendored)
      js/, css/        app code
data/tiles/            basemap.pmtiles  (git-ignored, fetched locally)
```
