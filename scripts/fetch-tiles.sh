#!/usr/bin/env bash
# Build an offline basemap by extracting a bounding box from a pinned Protomaps
# planet build. Requires the `pmtiles` CLI (`brew install pmtiles`). Only the
# tiles inside the bbox are transferred (HTTP range requests) — the 127 GB
# planet is never downloaded whole.
#
# Usage:
#   bash scripts/fetch-tiles.sh                          # contiguous US, z13, into repo
#   bash scripts/fetch-tiles.sh --maxzoom 14             # + buildings (larger)
#   OUT=/Volumes/Scratch/Protomaps/basemap.pmtiles \
#     bash scripts/fetch-tiles.sh --maxzoom 14           # write elsewhere
#   bash scripts/fetch-tiles.sh --bbox=-122.52,37.70,-122.35,37.83 --maxzoom 15
set -euo pipefail

# Pinned, dated planet build for reproducibility. Newer dates: https://build.protomaps.com/
BUILD="${BUILD:-20260703}"
SRC="https://build.protomaps.com/${BUILD}.pmtiles"

BBOX="-125,24,-66,50"   # contiguous US (W,S,E,N)
MAXZOOM=13

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$ROOT/data/tiles/basemap.pmtiles}"

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
echo "Heads up: a US extract is large — roughly z12 ~2-3GB, z13 ~4-6GB, z14 ~8-12GB,"
echo "z15 ~15-20GB+, and can take a while over range requests. Ctrl-C to abort."
echo

# Extract to a temp name and rename on success, so the served file is never partial.
pmtiles extract "$SRC" "$OUT.partial" --bbox="$BBOX" --maxzoom="$MAXZOOM"
mv -f "$OUT.partial" "$OUT"
echo "Done: $(du -h "$OUT" | cut -f1) at $OUT"
