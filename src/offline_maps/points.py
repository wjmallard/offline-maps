import geopandas

from offline_maps import config

# The GeoParquet on disk is the source of truth; this in-memory GeoDataFrame
# (plus its spatial index) is a rebuildable view over it, loaded once at startup.
_gdf = geopandas.read_parquet(config.POINTS_PARQUET)

if _gdf.crs is None:
    _gdf = _gdf.set_crs("EPSG:4326")
elif _gdf.crs.to_epsg() != 4326:
    _gdf = _gdf.to_crs("EPSG:4326")

_ = _gdf.sindex  # build the spatial index eagerly

POINT_COUNT = len(_gdf)
_GEOM = _gdf.geometry.name


def query_bbox(minx, miny, maxx, maxy, limit):
    """Return (geojson_str, n_returned, capped) for points within the bbox."""
    subset = _gdf.cx[minx:maxx, miny:maxy]
    capped = len(subset) > limit
    if capped:
        subset = subset.head(limit)
    # Coerce dtypes GeoJSON can't hold (e.g. datetimes in an unknown schema).
    subset = subset.copy()
    for col in subset.columns:
        if col == _GEOM:
            continue
        if str(subset[col].dtype).startswith("datetime"):
            subset[col] = subset[col].astype(str)
    return subset.to_json(), len(subset), capped
