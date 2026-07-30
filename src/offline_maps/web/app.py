import re
from pathlib import Path

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
)

app = Flask(__name__)

_EMPTY_COLLECTION = '{"type": "FeatureCollection", "features": []}'

# Same-origin-only CSP: the browser itself refuses any remote fetch, so "no
# network requests at runtime" holds structurally — for every deck, even a
# malicious one, and even if an escaping bug slips through. blob: covers
# MapLibre's workers; data:/blob: cover the favicon and decoded images.
# Violations POST to /csp-report so an attempted outlink leaves a log line.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "worker-src blob:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "report-uri /csp-report"
)


@app.after_request
def add_csp(resp):
    resp.headers["Content-Security-Policy"] = _CSP
    # Deck images are served with a mimetype from their extension; nosniff
    # stops the browser second-guessing a masquerading member into HTML.
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# Point ids land in filesystem globs and zip member lookups, so only a
# conservative charset is ever accepted from the URL.
_POINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@app.route("/")
def index():
    return render_template(
        "index.html",
        map_config={
            "center": config.DEFAULT_CENTER,
            "zoom": config.DEFAULT_ZOOM,
            "styleUrl": "/static/styles/basemap.json",
        },
    )


@app.route("/tiles/<path:filename>")
def tiles(filename):
    # data/ also holds the deck data, so serve only the configured basemap.
    # conditional=True enables the HTTP range requests the pmtiles protocol
    # relies on to read slices of the archive.
    if filename != config.TILES_FILE:
        abort(404)
    return send_file(config.TILES_PATH, conditional=True)


@app.route("/api/decks")
def api_decks():
    entries = []
    for deck in decks.list_decks():
        entry = {
            "id": deck.id,
            "name": deck.name,
            "count": deck.count,
            "has_meta": deck.has_meta,
            "has_photos": deck.has_photos,
        }
        entry.update({k: v for k, v in deck.info.items() if k != "name"})
        entries.append(entry)
    return {"decks": entries}


@app.route("/api/decks/<deck_id>/points")
def api_deck_points(deck_id):
    deck = _deck_or_404(deck_id)
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


@app.route("/api/decks/<deck_id>/point/<point_id>")
def api_deck_point(deck_id, point_id):
    # One point's detail record from the deck's meta.parquet, read lazily on
    # popup click. 404 when the deck carries no meta or the id is unknown.
    deck = _deck_or_404(deck_id)
    record = deck.meta_record(_point_id_or_404(point_id))
    if record is None:
        abort(404)
    return record


@app.route("/decks/<deck_id>/photos/<point_id>")
def deck_photo(deck_id, point_id):
    # Full-resolution image for the lightbox, from the deck's photos/.
    deck = _deck_or_404(deck_id)
    return _send_deck_image(deck.photo(_point_id_or_404(point_id)))


@app.route("/decks/<deck_id>/thumbs/<point_id>")
def deck_thumb(deck_id, point_id):
    # Popup-sized thumbnail from the deck's thumbs/.
    deck = _deck_or_404(deck_id)
    return _send_deck_image(deck.thumb(_point_id_or_404(point_id)))


def _deck_or_404(deck_id):
    deck = decks.get_deck(deck_id)
    if deck is None:
        abort(404)
    return deck


def _point_id_or_404(point_id):
    if not _POINT_ID.match(point_id):
        abort(404)
    return point_id


def _send_deck_image(result):
    # DirDecks hand back a real file path; ZipDecks a lazily-streamed zip member.
    if result is None:
        abort(404)
    if isinstance(result, Path):
        return send_file(result, conditional=True)
    fileobj, filename = result
    return send_file(fileobj, download_name=filename)


@app.route("/csp-report", methods=["POST"])
def csp_report():
    # The CSP's report-uri: any attempted off-origin request lands here, so a
    # deck that tries to phone home leaves a visible log line.
    app.logger.warning("CSP violation: %s", request.get_data(as_text=True))
    return ("", 204)


def main():
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
    )


if __name__ == "__main__":
    main()
