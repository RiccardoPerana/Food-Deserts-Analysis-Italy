"""
routing.py
----------
Handles the two distance operations in the pipeline:

  1. Shortlisting WHICH supermarkets are worth routing to (spatial index).
  2. Asking OSRM for the real routed walking distance and geometry to each
     (the shortest of which is tested against DISTANCE_THRESHOLD_KM).

REQUIRES a self-hosted OSRM server listening on config.OSRM_BASE_URL (see
README.md). A "connection actively refused" error means the OSRM container is
not running -- start it in its own terminal tab and leave it there.

--- THREE DESIGN DECISIONS WORTH KNOWING -------------------------------------

1. ALL DISTANCE WORK HAPPENS IN A METRE-BASED PROJECTION (EPSG:3035).
   Measuring in raw WGS84 degrees is tempting and wrong. At 46 deg N -- the
   Alps -- a degree of latitude spans ~111 km but a degree of longitude only
   ~77 km, so degree-space distances are stretched by ~1.44x along the
   east-west axis. Given one shop 2.8 km due east and another 3.1 km due
   north, that error ranks the northern one as closer. The wrong shop gets
   routed to, and an inflated distance lands in the results.

2. NEAREST-NEIGHBOUR LOOKUPS GO THROUGH THE R-TREE SPATIAL INDEX.
   Comparing every shop against every town is ~20 million distance
   calculations at this scale. The index answers the same question in
   logarithmic time, and is built once up front rather than lazily mid-loop.

3. THERE IS NO DELAY BETWEEN OSRM REQUESTS.
   Courtesy delays belong on third-party services. OSRM runs in a local Docker
   container, so a pause between calls protects nothing and costs roughly half
   an hour across a full run.
"""

import requests
import geopandas as gpd
from shapely.geometry import Point, LineString
import polyline

from . import config
from .geo_utils import METRIC_CRS, WGS84


# A single reused Session keeps the TCP connection to the local OSRM
# container alive across calls instead of re-opening one every request.
_SESSION = requests.Session()

# How far a town centre may sit from OSRM's nearest walkable way before the
# graph is judged not to cover it. Town centres are normally metres from a
# street; hundreds of kilometres means the graph was built from another area.
_COVERAGE_MAX_SNAP_M = 5_000
_COVERAGE_SAMPLE_SIZE = 12


def check_osrm_available():
    """
    Fails fast if nothing is answering on config.OSRM_BASE_URL.

    Without this, a stopped container is discovered only after the town and
    supermarket steps -- up to half an hour into a first national run -- and
    then surfaces as every single town being reported "unroutable".
    """
    probe = f"{config.OSRM_BASE_URL}/nearest/v1/{config.OSRM_PROFILE_WALK}/0,0"
    try:
        _SESSION.get(probe, timeout=config.OSRM_TIMEOUT_SEC)
    except requests.RequestException as e:
        raise RuntimeError(
            f"The OSRM routing server is not answering at {config.OSRM_BASE_URL} "
            f"({type(e).__name__}). Start it in its own terminal and leave it "
            f"running -- see the README, 'Start the routing server'."
        ) from None


def check_osrm_coverage(points):
    """
    Fails fast if the running OSRM graph does not cover the study area.

    OSRM always snaps a coordinate to its nearest walkable way, however far
    away that is. A graph built from the nord-est extract will therefore
    happily "route" a Sicilian town -- from a road in Veneto -- or fail it as
    unroutable, and neither looks like a configuration error. Switching scope
    without rebuilding the graph is the easiest mistake to make here, so a
    sample of town centres spread north to south is snapped first, and the run
    stops if a meaningful share of them are nowhere near a road.
    """
    points = sorted(points, key=lambda p: (p.y, p.x))
    if not points:
        return
    step = max(1, len(points) // _COVERAGE_SAMPLE_SIZE)
    sample = points[::step][:_COVERAGE_SAMPLE_SIZE]

    far = []
    for p in sample:
        url = (f"{config.OSRM_BASE_URL}/nearest/v1/{config.OSRM_PROFILE_WALK}/"
               f"{p.x},{p.y}?number=1")
        try:
            data = _SESSION.get(url, timeout=config.OSRM_TIMEOUT_SEC).json()
            snap_m = data["waypoints"][0]["distance"]
        except (requests.RequestException, ValueError, KeyError, IndexError):
            snap_m = None
        if snap_m is None or snap_m > _COVERAGE_MAX_SNAP_M:
            far.append((p, snap_m))

    if len(far) > len(sample) // 4:
        examples = "; ".join(
            f"({p.y:.3f}, {p.x:.3f}) -> "
            + ("no answer" if d is None else f"{d / 1000:,.0f} km from the nearest way")
            for p, d in far[:4]
        )
        raise RuntimeError(
            f"The running OSRM graph does not cover the study area: "
            f"{len(far)}/{len(sample)} sampled town centres are more than "
            f"{_COVERAGE_MAX_SNAP_M / 1000:.0f} km from any walkable way ({examples}). "
            f"It was most likely built from a different extract. Rebuild it "
            f"from {config.OSM_PBF_PATH.name} and restart osrm-routed."
        )
    print(f"[INFO] OSRM covers the study area ({len(sample) - len(far)}/{len(sample)} "
          f"sampled town centres snap to a walkable way).")


def project_points_to_metric(points, source_crs=WGS84):
    """
    Projects an iterable of shapely Points into METRIC_CRS in one vectorised
    operation, returning a GeoSeries.

    Doing this once up front is far cheaper than projecting point-by-point
    inside the main loop.
    """
    return gpd.GeoSeries(list(points), crs=source_crs).to_crs(METRIC_CRS)


def find_supermarket_candidates(town_point_metric, supermarkets_metric,
                                count=None, search_radius_m=None):
    """
    Returns the positional indices of the nearest `count` supermarkets by
    straight-line distance, closest first.

    Both arguments must already be in METRIC_CRS. The returned indices are
    POSITIONAL, so they work with .iloc[] against the original (unprojected)
    supermarkets GeoDataFrame to recover names and coordinates.

    WHY SEVERAL CANDIDATES RATHER THAN ONE. Straight-line distance ignores
    whether anything can actually be walked. In Papozze, on the Po delta, the
    closest shop as the crow flies sits 2.8 km away on the far bank with no
    bridge nearby -- a 28.7 km walk. A different shop 7.1 km away in a straight
    line is reachable in about 8 km. Routing to only the straight-line nearest
    would report Papozze as a 28.7 km food desert when it is an 8 km one.

    Callers route to each candidate in turn and keep the shortest genuine walk.

    If nothing falls inside the search radius, this falls back to the single
    absolute nearest so the caller always has something to route to.
    """
    count = count or config.ROUTING_CANDIDATE_COUNT
    radius = search_radius_m or config.ROUTING_CANDIDATE_SEARCH_M

    zone = town_point_metric.buffer(radius)
    positions = supermarkets_metric.sindex.query(zone)

    if len(positions) == 0:
        # Nothing within the radius at all -- extremely remote. Fall back to
        # the single nearest shop anywhere in the dataset.
        match = supermarkets_metric.sindex.nearest(town_point_metric, return_all=False)
        return [int(match[1][0])]

    geoms = supermarkets_metric.geometry.iloc[positions]
    distances = geoms.distance(town_point_metric).to_numpy()
    order = distances.argsort()[:count]
    return [int(positions[i]) for i in order]


def get_routed_distance_and_geometry(origin_point: Point, dest_point: Point, profile: str):
    """
    Queries the local OSRM instance for a route between two WGS84 points.

    Returns (distance_km, route_linestring), or (None, None) if OSRM could
    not find a route or the request failed. Callers MUST handle the None
    case -- an unroutable town is a real signal worth recording, not
    something to swallow.
    """
    url = (
        f"{config.OSRM_BASE_URL}/route/v1/{profile}/"
        f"{origin_point.x},{origin_point.y};{dest_point.x},{dest_point.y}"
        f"?overview=full&geometries=polyline"
    )

    try:
        resp = _SESSION.get(url, timeout=config.OSRM_TIMEOUT_SEC)
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            return None, None

        route = data["routes"][0]
        distance_km = route["distance"] / 1000.0
        coords = polyline.decode(route["geometry"])       # list of (lat, lon)
        line = LineString([(lon, lat) for lat, lon in coords])
        return distance_km, line

    except requests.RequestException as e:
        print(f"[ERROR] OSRM request failed: {e}")
        return None, None


def get_walking_route(town_point, supermarket_point):
    """Convenience wrapper for the pedestrian profile used by the distance rule."""
    return get_routed_distance_and_geometry(
        town_point, supermarket_point, config.OSRM_PROFILE_WALK
    )
