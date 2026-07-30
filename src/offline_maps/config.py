import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(_ROOT / ".env")

with open(_ROOT / "config.yaml") as _f:
    _config = yaml.safe_load(_f)

# Map display
DEFAULT_CENTER = _config["map"]["center"]        # [lon, lat]
DEFAULT_ZOOM = _config["map"]["default_zoom"]

# Vendored tile data
TILES_PATH = (_ROOT / _config["tiles"]["file"]).expanduser()
TILES_FILE = TILES_PATH.name

# Waypoint decks — the directory the viewer scans for point sets (see decks.py)
DECKS_DIR = (_ROOT / _config["decks"]["dir"]).expanduser()
DECKS_MAX_FEATURES = _config["decks"].get("max_features", 5000)

# ALPR sync — mirrors DeFlock's CDN region tiles into a deck directory (see sync.py)
_sync = _config.get("sync", {})
SYNC_REGIONS_URL = _sync.get("regions_url", "https://cdn.deflock.me/regions")
SYNC_DECK_DIR = (_ROOT / _sync.get("deck", "data/decks/alpr")).expanduser()
SYNC_POINTS_PARQUET = SYNC_DECK_DIR / "points.parquet"

# Optional OSM enrichment sidecar (see sync_osm.py)
_sync_osm = _sync.get("osm", {})
SYNC_OSM_OVERPASS_URL = _sync_osm.get("overpass_url", "https://overpass-api.de/api/interpreter")
SYNC_OSM_BATCH_SIZE = _sync_osm.get("batch_size", 1000)
SYNC_OSM_OUTPUT = SYNC_DECK_DIR / "meta.parquet"

# Optional local photo cache (see sync_photos.py); MAPILLARY_TOKEN comes from .env
_sync_photos = _sync.get("photos", {})
SYNC_PHOTOS_DIR = SYNC_DECK_DIR / "photos"
SYNC_PHOTOS_THUMBS_DIR = SYNC_DECK_DIR / "thumbs"
SYNC_PHOTOS_MAX_WIDTH = _sync_photos.get("max_width", 1024)
SYNC_PHOTOS_THUMB_WIDTH = _sync_photos.get("thumb_width", 512)
MAPILLARY_TOKEN = os.getenv("MAPILLARY_TOKEN")

# Web server
_server = _config.get("server", {})
HOST = _server.get("host", "127.0.0.1")
PORT = _server.get("port", 5000)
DEBUG = _server.get("debug", True)
