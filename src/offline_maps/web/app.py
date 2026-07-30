from flask import (
    Flask,
    Response,
    abort,
    render_template,
    request,
    send_file,
)

from offline_maps import (
    config,
    decks,
    meta,
)

app = Flask(__name__)

_EMPTY_COLLECTION = '{"type": "FeatureCollection", "features": []}'


@app.route("/")
def index():
    return render_template(
        "index.html",
        center=config.DEFAULT_CENTER,
        zoom=config.DEFAULT_ZOOM,
    )


@app.route("/tiles/<path:filename>")
def tiles(filename):
    # data/ also holds the deck data, so serve only the configured basemap.
    # conditional=True enables the HTTP range requests the pmtiles protocol
    # relies on to read slices of the archive.
    if filename != config.TILES_FILE:
        abort(404)
    return send_file(config.TILES_PATH, conditional=True)


@app.route("/photos/<int:osm_id>")
def photos(osm_id):
    # Locally cached ALPR photos (offline-maps-sync-photos) — keeps the map offline.
    match = next(
        (p for p in config.SYNC_PHOTOS_DIR.glob(f"{osm_id}.*") if p.suffix != ".tmp"),
        None,
    )
    if match is None:
        abort(404)
    return send_file(match, conditional=True)


@app.route("/thumbs/<int:osm_id>")
def thumb(osm_id):
    # Downscaled thumbnail for map popups (offline-maps-sync-photos builds these).
    path = config.SYNC_PHOTOS_THUMBS_DIR / f"{osm_id}.jpg"
    if not path.exists():
        abort(404)
    return send_file(path, conditional=True)


@app.route("/api/decks")
def api_decks():
    return {
        "decks": [
            {
                "id": deck.id,
                "name": deck.name,
                "count": deck.count,
            }
            for deck in decks.list_decks()
        ],
    }


@app.route("/api/decks/<deck_id>/points")
def api_deck_points(deck_id):
    deck = decks.get_deck(deck_id)
    if deck is None:
        abort(404)
    return _points_response(deck)


def _points_response(deck):
    bbox = request.args.get("bbox")
    if not bbox:
        return Response(_EMPTY_COLLECTION, mimetype="application/json")
    minx, miny, maxx, maxy = (float(v) for v in bbox.split(","))
    limit = request.args.get("limit", default=config.DECKS_MAX_FEATURES, type=int)
    geojson, n, capped = deck.query_bbox(minx, miny, maxx, maxy, limit)
    resp = Response(geojson, mimetype="application/json")
    resp.headers["X-Point-Count"] = str(n)
    resp.headers["X-Capped"] = "1" if capped else "0"
    return resp


@app.route("/api/point/<int:osm_id>")
def api_point(osm_id):
    # Enriched OSM record (full tags + edit metadata) for one node, read lazily
    # from the sidecar parquet on popup click. 404 when the sidecar is absent.
    record = meta.point_meta(osm_id)
    if record is None:
        abort(404)
    return record


def main():
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
    )


if __name__ == "__main__":
    main()
