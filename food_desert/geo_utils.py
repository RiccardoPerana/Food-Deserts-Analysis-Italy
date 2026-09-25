"""
geo_utils.py
------------
Shared geographic helpers.

Three things live here because they must be identical everywhere they are
used, and are easy to get subtly wrong in isolation.

1. THE METRIC PROJECTION (METRIC_CRS).
   Every distance and buffer in this project is computed in metres, in one
   shared CRS. Two modules using different projections would produce distances
   that are not comparable to each other -- a discrepancy that produces
   plausible numbers rather than an error.

2. DEGREES-TO-KILOMETRES CONVERSION (km_to_degrees).
   The familiar "divide by 111" is correct for LATITUDE only. At 46 degrees
   north -- the Alps -- one degree of LONGITUDE spans about 77 km, and at 37
   degrees -- Sicily -- about 89 km, so a flat 111 stretches east-west
   distances by 1.25-1.44x across Italy. That is enough to pick the wrong
   nearest supermarket. This helper accounts for latitude, and is used only
   for rough bounding boxes; anything affecting results projects to
   METRIC_CRS instead.

3. TARGET-AREA POLYGON LOOKUP (get_target_polygons).
   Resolved from Nominatim once and memoised, so the several modules needing
   the study-area boundary share one set of network calls per run. osmnx also
   caches the responses on disk, so later runs make none at all.
"""

import math

import osmnx as ox
import shapely

from . import config, paths

ox.settings.cache_folder = str(paths.OSMNX_CACHE_DIR)


# ---------------------------------------------------------------------------
# PROJECTION
# ---------------------------------------------------------------------------
# EPSG:3035 is ETRS89 / LAEA Europe: an equal-area projection expressed in
# METRES, designed by the EU for exactly this kind of pan-European analysis.
#
# Working in this CRS means .distance(), .buffer() and spatial-index
# nearest-neighbour queries all speak real metres directly. No hand-rolled
# conversion factors, no latitude-dependent error, no anisotropy.
METRIC_CRS = "EPSG:3035"

WGS84 = "EPSG:4326"

# Kilometres per degree of latitude. Near-constant everywhere on Earth.
KM_PER_DEG_LAT = 111.32


def km_to_degrees(km, latitude):
    """
    Converts a distance in km into (degrees_latitude, degrees_longitude) at
    the given latitude.

    The two return values differ because meridians converge towards the
    poles: a degree of longitude is a full 111 km at the equator but shrinks
    by cos(latitude) as you move away from it.

    NOTE: this is only appropriate for drawing rough bounding boxes, which
    is why the only remaining caller is the diagnostic script. Anything that
    affects the actual results should project to METRIC_CRS instead.
    """
    deg_lat = km / KM_PER_DEG_LAT
    deg_lon = km / (KM_PER_DEG_LAT * math.cos(math.radians(latitude)))
    return deg_lat, deg_lon


# ---------------------------------------------------------------------------
# TARGET AREA RESOLUTION
# ---------------------------------------------------------------------------
_TARGET_POLYGON_CACHE = None
_COMBINED_POLYGON_CACHE = None


def _target_queries():
    """[(label, Nominatim query), ...] for the configured TARGET_LEVEL."""
    if config.TARGET_LEVEL == "country":
        # A structured query, so "Italy" cannot resolve to a street or a
        # restaurant of that name. The polygon returned is the national
        # boundary relation, territorial waters included -- which keeps every
        # coastal town's centre point comfortably inside it.
        return [(config.COUNTRY_NAME, {"country": config.COUNTRY_NAME})]
    if config.TARGET_LEVEL == "multi_region":
        names = config.TARGET_REGIONS
    elif config.TARGET_LEVEL in ("region", "province"):
        names = [config.TARGET_NAME]
    else:
        raise ValueError(
            f"Unsupported config.TARGET_LEVEL: {config.TARGET_LEVEL!r}. "
            f"Expected one of: 'country', 'multi_region', 'region', 'province'."
        )
    return [(name, f"{name}, {config.COUNTRY_NAME}") for name in names]


def get_target_polygons():
    """
    Returns [(label, polygon), ...] for the configured target area, geocoded
    via Nominatim.

    The result is memoised, so the several modules that need it share a
    single set of network calls per run rather than each paying for its own.

    Raises rather than returning an empty list, which would let
    fetch_map_layers.py write empty overlays and exit successfully -- a
    failure you would only discover by noticing a blank map.
    """
    global _TARGET_POLYGON_CACHE
    if _TARGET_POLYGON_CACHE is not None:
        return _TARGET_POLYGON_CACHE

    queries = _target_queries()
    print(f"[INFO] Resolving target area polygons via Nominatim: "
          f"{[label for label, _ in queries]}")
    polygons = []
    for label, query in queries:
        try:
            gdf = ox.geocode_to_gdf(query)
            polygons.append((label, gdf.geometry.iloc[0]))
        except Exception as e:
            print(f"[WARN] Could not geocode '{label}': {e}")

    if not polygons:
        raise RuntimeError(
            "Could not resolve ANY target area polygon. Check your internet "
            "connection and the names in config.TARGET_REGIONS / COUNTRY_NAME."
        )

    _TARGET_POLYGON_CACHE = polygons
    return polygons


def get_combined_target_polygon():
    """
    Returns a single polygon covering the whole target area, PREPARED for
    fast repeated predicates.

    Preparation matters at national scale: Italy's outline has hundreds of
    thousands of vertices, and an unprepared point-in-polygon test walks all
    of them -- for every one of ~8,000 towns and every one of several hundred
    thousand overlay lines.
    """
    global _COMBINED_POLYGON_CACHE
    if _COMBINED_POLYGON_CACHE is None:
        polygons = [poly for _, poly in get_target_polygons()]
        combined = shapely.union_all(polygons) if len(polygons) > 1 else polygons[0]
        shapely.prepare(combined)
        _COMBINED_POLYGON_CACHE = combined
    return _COMBINED_POLYGON_CACHE
