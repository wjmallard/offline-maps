"""Cache referenced photos locally so the offline viewer needs no network.

`points_meta.parquet` (from offline-maps-sync-osm) carries external photo references:
`wikimedia_commons`, `image`/`photo` URLs, `mapillary` ids, and `panoramax` ids.
Rendering those in the map would mean runtime network calls, breaking the offline
guarantee. This tool resolves each reference to a real image and downloads a
full-resolution copy to `data/photos/<osm_id>.<ext>`, then builds a downscaled JPEG
thumbnail in `data/photos/thumbs/` for the map popups; the viewer serves both from its
own `/photos` and `/thumbs` routes, so the browser only ever talks to the local server.

One photo per node (best available, by source priority). Resumable: an already-cached
file is its own checkpoint, and permanent failures (dead links, 404s) are recorded so
re-runs skip them — pass --retry-failed to try them again. Mapillary needs a free token
in `.env` (MAPILLARY_TOKEN); without it, Mapillary-only nodes are left for a later run.

Imagery is variously licensed (Commons / Mapillary / Panoramax are CC; `image=` URLs are
unknown) — fine for a local cache; attribute if you ever redistribute it.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas
from tqdm import tqdm

from offline_maps import config

# OSM tags that reference an image, in the order we prefer to fetch them.
_IMAGE_KEYS = (
    "wikimedia_commons",
    "mapillary",
    "panoramax",
    "image",
    "photo",
)
_CONTENT_TYPE_EXT = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_FAILURES_FILE = ".failures.json"

_USER_AGENT = "offline-maps-photo-cache/0.1 (personal use)"
_REQUEST_TIMEOUT_S = 30
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 2.0
_MAX_BYTES = 12_000_000
_POLITENESS_DELAY_S = 0.3


def _is_permanent(error):
    """A 4xx other than 429 (rate limiting) will not succeed on retry."""
    return (
        isinstance(error, urllib.error.HTTPError)
        and error.code != 429
        and 400 <= error.code < 500
    )


def _http_get(url):
    """GET a URL with retry/backoff; return (content_type, body).

    Body is read up to _MAX_BYTES + 1 so the caller can detect and skip an oversized
    response. Permanent 4xx failures raise immediately (no wasted retries).
    """
    for attempt in range(_MAX_RETRIES):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as response:
                return response.headers.get("Content-Type", ""), response.read(_MAX_BYTES + 1)
        except (urllib.error.URLError, TimeoutError) as error:
            if _is_permanent(error) or attempt == _MAX_RETRIES - 1:
                raise
            time.sleep(_BACKOFF_BASE_S * 2 ** attempt)


def _resolve_mapillary(image_id, max_width, token):
    """Mapillary id -> a thumbnail URL via the Graph API (needs an access token)."""
    size = 256 if max_width <= 256 else 1024 if max_width <= 1024 else 2048
    field = f"thumb_{size}_url"
    api = (
        f"https://graph.mapillary.com/{urllib.parse.quote(image_id)}"
        f"?access_token={urllib.parse.quote(token)}&fields={field}"
    )
    try:
        _, body = _http_get(api)
        return json.loads(body).get(field)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _resolve_panoramax(picture_id):
    """Panoramax id -> an image URL via the open search API (no token needed)."""
    api = f"https://api.panoramax.xyz/api/search?ids={urllib.parse.quote(picture_id)}&limit=1"
    try:
        _, body = _http_get(api)
        features = json.loads(body).get("features", [])
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    if not features:
        return None
    assets = features[0].get("assets", {})
    for name in ("sd", "thumb", "hd"):
        href = assets.get(name, {}).get("href")
        if href:
            return href
    return None


def _image_urls(refs, max_width, token):
    """Yield candidate direct image URLs for a node's refs, best source first.

    Lazy on purpose: the Mapillary/Panoramax resolvers (which each hit an API) only run
    if the higher-priority sources ahead of them did not already yield a working image.
    """
    if "wikimedia_commons" in refs:
        name = refs["wikimedia_commons"].split("File:", 1)[-1]
        yield f"https://commons.wikimedia.org/wiki/Special:FilePath/{urllib.parse.quote(name)}?width={max_width}"
    if "mapillary" in refs and token:
        yield _resolve_mapillary(refs["mapillary"], max_width, token)
    if "panoramax" in refs:
        yield _resolve_panoramax(refs["panoramax"])
    for key in ("image", "photo"):
        if refs.get(key, "").startswith(("http://", "https://")):
            yield refs[key]


def _download_one(osm_id, refs, photos_dir, max_width, token):
    """Try each candidate image until one downloads; return 'ok', 'no_token', or 'failed'."""
    for url in _image_urls(refs, max_width, token):
        if not url:
            continue
        try:
            content_type, body = _http_get(url)
        except (urllib.error.URLError, TimeoutError):
            continue
        ext = _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip())
        if ext is None or len(body) > _MAX_BYTES:
            continue
        tmp = photos_dir / f"{osm_id}{ext}.tmp"
        tmp.write_bytes(body)
        os.replace(tmp, photos_dir / f"{osm_id}{ext}")
        return "ok"
    if "mapillary" in refs and not token:
        return "no_token"
    return "failed"


def _cached_ids(photos_dir):
    return {int(p.stem) for p in photos_dir.glob("*.*") if p.stem.isdigit()}


def _load_failed(photos_dir):
    path = photos_dir / _FAILURES_FILE
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, ValueError):
        return set()


def _save_failed(photos_dir, failed_ids):
    (photos_dir / _FAILURES_FILE).write_text(json.dumps(sorted(failed_ids)))


def cache_photos(meta_path, photos_dir, max_width, token, retry_failed, limit=None):
    """Download one photo per referenced node into photos_dir; return a result tally."""
    photos_dir.mkdir(parents=True, exist_ok=True)
    gdf = geopandas.read_parquet(meta_path)
    cached = _cached_ids(photos_dir)
    failed = set() if retry_failed else _load_failed(photos_dir)

    todo = []
    for osm_id, tags_json in zip(gdf["osm_id"].tolist(), gdf["tags"].tolist()):
        osm_id = int(osm_id)
        if osm_id in cached or osm_id in failed:
            continue
        refs = {k: v for k, v in json.loads(tags_json).items() if k in _IMAGE_KEYS}
        if refs:
            todo.append((osm_id, refs))
    if limit:
        todo = todo[:limit]

    counts = {"ok": 0, "failed": 0, "no_token": 0}
    for osm_id, refs in tqdm(todo, desc="photos", unit="img"):
        result = _download_one(osm_id, refs, photos_dir, max_width, token)
        counts[result] += 1
        if result == "failed":
            failed.add(osm_id)
        time.sleep(_POLITENESS_DELAY_S)

    _save_failed(photos_dir, failed)
    return counts


def build_thumbnails(photos_dir, thumbs_dir, width):
    """Generate a JPEG thumbnail in thumbs_dir for every full-size image missing one.

    Full-resolution originals are left untouched. Resumable: existing thumbnails are
    skipped, so this can be re-run (or --thumbs-only'd) to backfill at any time.
    """
    from PIL import Image, ImageOps  # heavy optional dependency, imported lazily

    thumbs_dir.mkdir(parents=True, exist_ok=True)
    sources = [
        path
        for path in photos_dir.glob("*.*")
        if path.stem.isdigit() and path.suffix.lower() in (".gif", ".jpeg", ".jpg", ".png", ".webp")
    ]
    built = 0
    for source in tqdm(sources, desc="thumbs", unit="img"):
        target = thumbs_dir / f"{source.stem}.jpg"
        if target.exists():
            continue
        try:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                if image.width > width:
                    height = round(image.height * width / image.width)
                    image = image.resize((width, height))
                tmp = thumbs_dir / f"{source.stem}.jpg.tmp"
                image.save(tmp, format="JPEG", quality=82)
            os.replace(tmp, target)
            built += 1
        except OSError:
            continue
    return built


def main():
    parser = argparse.ArgumentParser(
        description="Cache referenced photos locally for the offline viewer.",
    )
    parser.add_argument(
        "--meta",
        type=Path,
        default=config.SYNC_OSM_OUTPUT,
        help="enriched GeoParquet with the photo tags (default: config sync.osm.output_path)",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=config.SYNC_PHOTOS_DIR,
        help="destination photo directory (default: config sync.photos.dir)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="re-attempt references previously recorded as permanently failed",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only process the first N referenced nodes (for a quick test run)",
    )
    parser.add_argument(
        "--thumbs-only",
        action="store_true",
        help="skip downloading; only (re)build thumbnails from cached full-size images",
    )
    args = parser.parse_args()

    thumbs_dir = args.dir / "thumbs"

    if not args.thumbs_only:
        if not args.meta.exists():
            parser.error(f"{args.meta} not found — run offline-maps-sync-osm first.")
        token = config.MAPILLARY_TOKEN
        note = "" if token else "  (no MAPILLARY_TOKEN — Mapillary refs will be skipped)"
        print(f"Caching photos into {args.dir}{note}")
        counts = cache_photos(
            args.meta, args.dir, config.SYNC_PHOTOS_MAX_WIDTH, token, args.retry_failed, args.limit,
        )
        tail = f", {counts['no_token']:,} awaiting a Mapillary token" if counts["no_token"] else ""
        print(f"Done: {counts['ok']:,} cached, {counts['failed']:,} failed{tail}")

    built = build_thumbnails(args.dir, thumbs_dir, config.SYNC_PHOTOS_THUMB_WIDTH)
    total = sum(1 for _ in thumbs_dir.glob("*.jpg"))
    print(f"Thumbnails: {built:,} new, {total:,} total in {thumbs_dir}")


if __name__ == "__main__":
    main()
