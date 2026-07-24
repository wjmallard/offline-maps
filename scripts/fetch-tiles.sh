#!/usr/bin/env bash
# Build an offline basemap by extracting a bounding box from a Protomaps
# planet build. Requires the `pmtiles` CLI (`brew install pmtiles`). Only the
# tiles inside the bbox are transferred (HTTP range requests) — the 127 GB
# planet is never downloaded whole. Extracts keep the build's full detail
# (z15); use --maxzoom only to shrink the file (the viewer over-zooms past
# whatever the archive holds either way).
#
# Usage:
#   bash scripts/fetch-tiles.sh                                     # contiguous US, ~19 GB
#   bash scripts/fetch-tiles.sh --maxzoom 13                        # smaller US (~4 GB, no buildings)
#   OUT=/Volumes/Scratch/Protomaps/basemap.pmtiles \
#     bash scripts/fetch-tiles.sh                                   # write elsewhere
#   bash scripts/fetch-tiles.sh --bbox=-122.52,37.70,-122.35,37.83  # San Francisco proper (~14 MB)
set -euo pipefail

# Upstream keeps only about a week of daily planet builds (an old pin 404s), so
# default to the newest live build; pin one explicitly with BUILD=YYYYMMDD. The
# resolved date is echoed below for the record.
if [ -z "${BUILD:-}" ]; then
  for days_ago in $(seq 0 7); do
    d=$(date -v-"${days_ago}"d +%Y%m%d 2>/dev/null || date -d "${days_ago} days ago" +%Y%m%d)
    if curl -sf --head "https://build.protomaps.com/${d}.pmtiles" >/dev/null; then
      BUILD="$d"
      break
    fi
  done
fi
[ -n "${BUILD:-}" ] || { echo "No live planet build found at build.protomaps.com" >&2; exit 1; }
SRC="https://build.protomaps.com/${BUILD}.pmtiles"

BBOX="-125,24,-66,50"   # contiguous US (W,S,E,N)
MAXZOOM=15

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/data/basemap.pmtiles}"

while [ $# -gt 0 ]; do
  case "$1" in
    --bbox=*)    BBOX="${1#*=}" ;;
    --bbox)      BBOX="$2"; shift ;;
    --maxzoom=*) MAXZOOM="${1#*=}" ;;
    --maxzoom)   MAXZOOM="$2"; shift ;;
    --out=*)     OUT="${1#*=}" ;;
    --out)       OUT="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$(dirname "$OUT")"

echo "Source : $SRC"
echo "BBox   : $BBOX"
echo "Maxzoom: $MAXZOOM"
echo "Output : $OUT"
echo
echo "Heads up: a full-detail contiguous-US extract is ~19 GB (--maxzoom 13 cuts"
echo "it to ~4 GB) and can take a while over range requests. Ctrl-C to abort."
echo

# Extract to a temp name and rename on success, so the served file is never partial.
pmtiles extract "$SRC" "$OUT.partial" --bbox="$BBOX" --maxzoom="$MAXZOOM"
mv -f "$OUT.partial" "$OUT"
echo "Done: $(du -h "$OUT" | cut -f1) at $OUT"
