"""
diagnostics.py
--------------
DIAGNOSTIC MODULE -- spot-checks a specific town's supermarket data.

For each town (comune) supplied this reports:
  1. Its centre point and boundary size (a sanity check on the location data)
  2. Which of its settlements are in the final spreadsheet, if the analysis
     has run
  3. What the CACHED supermarket dataset contains nearby
  4. What a LIVE Overpass query finds over the same area, as a second opinion

Pass one or more town names -- or six-digit ISTAT codes, to pick one of
several same-named comuni -- as command-line arguments.
Run with:  python run.py diagnose "Town Name" 025001

Overpass is used HERE and nowhere else in the project. The main pipeline reads
everything from the local extract; this script deliberately queries the live API
so its answer is independent of the cache it is checking. Disagreement between
the two is the signal worth having.
"""

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from . import config
from .overpass_utils import query_with_retry
from .geo_utils import km_to_degrees

CHECK_RADIUS_KM = 10        # radius for the cached-vs-live comparison
TIGHT_RADIUS_KM = 3         # radius for the close-in raw element listing

# Spreadsheet columns the cross-check reads (see export_spreadsheet.HEADERS).
COL_COMUNE_CODE = "ISTAT Code (comune)"
COL_COMUNE = "Comune"
COL_SETTLEMENT = "Settlement"
COL_DISTANCE = "Distance to Nearest Supermarket (km)"
COL_SHOP = "Nearest Supermarket"
COL_POPULATION = "Population (est.)"


def _bbox_around(center, radius_km):
    """Returns (south, west, north, east) for an Overpass bounding box."""
    deg_lat, deg_lon = km_to_degrees(radius_km, center.y)
    return (
        center.y - deg_lat,
        center.x - deg_lon,
        center.y + deg_lat,
        center.x + deg_lon,
    )


def _live_supermarket_query(center, radius_km):
    """
    Runs one live Overpass query for supermarkets within radius_km of a point.

    Returns a list of (kind, name, has_coords) tuples, where kind is "NODE" or
    "WAY". Ways without a computed centre are reported rather than dropped:
    a silently missing shop is exactly the kind of gap this check looks for.
    """
    south, west, north, east = _bbox_around(center, radius_km)
    query = f"""
    [out:json][timeout:{config.OVERPASS_TIMEOUT}];
    (
      node["shop"~"supermarket|convenience"]({south},{west},{north},{east});
      way["shop"~"supermarket|convenience"]({south},{west},{north},{east});
    );
    out center;
    """

    result = query_with_retry(query)
    found = []
    for node in result.nodes:
        found.append(("NODE", node.tags.get("name", "Unnamed"), True))
    for way in result.ways:
        has_coords = way.center_lon is not None and way.center_lat is not None
        found.append(("WAY", way.tags.get("name", "Unnamed"), has_coords))
    return found


def _find_towns(query, towns_gdf):
    """Rows matching a six-digit ISTAT code, or else an exact town name."""
    if query.isdigit() and "istat_ref" in towns_gdf:
        return towns_gdf[towns_gdf["istat_ref"] == query.zfill(6)]
    return towns_gdf[towns_gdf["name"] == query]


def check_town(row, supermarkets_cached):
    name = row["name"]
    istat_ref = row.get("istat_ref")
    if not isinstance(istat_ref, str):
        istat_ref = None

    print(f"\n{'=' * 62}")
    print(f"TOWN: {name}" + (f"  (ISTAT {istat_ref})" if istat_ref else ""))
    print(f"{'=' * 62}")

    center = row["center_point"]
    boundary = row["boundary"]
    print(f"Centre point : lon={center.x:.5f}, lat={center.y:.5f}")
    print(f"Boundary area: {boundary.area:.6f} deg^2 (rough size indicator)")
    print(f"Bounding box : {boundary.bounds}")

    # --- Cross-check against the final spreadsheet, if it exists ------------
    # The spreadsheet lists settlements, so a comune can appear several
    # times (its main town and any underserved frazioni) or not at all.
    try:
        df = pd.read_excel(config.SPREADSHEET_PATH, dtype={COL_COMUNE_CODE: str})
    except FileNotFoundError:
        print(f"\n[FINAL RESULTS] {config.SPREADSHEET_PATH} not found -- "
              f"run `python run.py analyze` first if you want this cross-check.")
        df = None
    if df is not None:
        missing = [c for c in (COL_COMUNE_CODE, COL_COMUNE, COL_SETTLEMENT,
                               COL_DISTANCE, COL_SHOP, COL_POPULATION)
                   if c not in df]
        if missing:
            print(f"\n[FINAL RESULTS] {config.SPREADSHEET_PATH.name} has no "
                  f"{missing} column(s) -- it was written by a different version "
                  f"of the pipeline. Re-run `python run.py analyze`.")
        else:
            match = (df[df[COL_COMUNE_CODE] == istat_ref] if istat_ref
                     else df[df[COL_COMUNE] == name])
            if match.empty:
                print(f"\n[FINAL RESULTS] No settlement of '{name}' is in the "
                      f"spreadsheet -- each has a shop of its own or one within "
                      f"{config.DISTANCE_THRESHOLD_KM}km on foot.")
            else:
                print(f"\n[FINAL RESULTS] {len(match)} settlement(s) of '{name}' "
                      f"are underserved:")
                for _, r in match.iterrows():
                    print(f"   {str(r[COL_SETTLEMENT])[:30]:32} "
                          f"{r[COL_DISTANCE]:>6} km to {r[COL_SHOP]}  "
                          f"(pop. {r[COL_POPULATION]})")

    # --- Close-in raw element listing --------------------------------------
    print(f"\n[TIGHT LIVE CHECK] Raw OSM elements within {TIGHT_RADIUS_KM}km...")
    try:
        for kind, shop_name, has_coords in _live_supermarket_query(center, TIGHT_RADIUS_KM):
            status = "coords OK" if has_coords else "*** NO CENTRE COMPUTED ***"
            print(f"   [{kind:4}] {shop_name} -- {status}")
    except Exception as e:
        print(f"[ERROR] Tight live check failed: {e}")

    # --- What the cached dataset holds nearby ------------------------------
    deg_lat, deg_lon = km_to_degrees(CHECK_RADIUS_KM, center.y)
    check_zone = Point(center.x, center.y).buffer(max(deg_lat, deg_lon))
    nearby_cached = supermarkets_cached[supermarkets_cached.geometry.intersects(check_zone)]

    print(f"\n[CACHED DATA] Supermarkets within ~{CHECK_RADIUS_KM}km: {len(nearby_cached)}")
    for _, sm in nearby_cached.iterrows():
        dist_km_approx = center.distance(sm.geometry) * 111.0
        print(f"   - {sm['name']} (~{dist_km_approx:.1f}km straight-line, approx)")

    within_boundary = supermarkets_cached[supermarkets_cached.geometry.intersects(boundary)]
    print(f"[CACHED DATA] Supermarkets INSIDE this town's boundary: {len(within_boundary)}")
    for _, sm in within_boundary.iterrows():
        print(f"   - {sm['name']}")

    # --- Same area, live, for comparison -----------------------------------
    print(f"\n[LIVE QUERY] Fetching fresh data for the same {CHECK_RADIUS_KM}km area...")
    try:
        live = _live_supermarket_query(center, CHECK_RADIUS_KM)
        live_names = {shop_name for _, shop_name, _ in live}
        print(f"[LIVE QUERY] Supermarkets found: {len(live)}")
        for _, shop_name, _ in live:
            print(f"   - {shop_name}")

        cached_names = set(nearby_cached["name"].tolist())
        missing_from_cache = live_names - cached_names
        if missing_from_cache:
            print(f"\n[GAP] Present in the LIVE query but MISSING from the cached "
                  f"dataset: {missing_from_cache}")
            print("      Note: the cache is clipped to the study area plus "
                  "BORDER_BUFFER_KM, so shops well outside it are expected to be absent.")
        else:
            print("\n[NO GAP] Live query and cached data agree for this area.")
    except Exception as e:
        print(f"[ERROR] Live query failed: {e}")


def run_diagnostics(town_names):
    print("Loading cached towns and supermarkets data...")
    towns_gdf = gpd.read_file(config.TOWNS_CACHE_PATH)
    if towns_gdf.geometry.name != "boundary":
        towns_gdf = towns_gdf.rename(columns={towns_gdf.geometry.name: "boundary"})
        towns_gdf = towns_gdf.set_geometry("boundary")
    towns_gdf["center_point"] = towns_gdf.apply(
        lambda r: Point(r["center_lon"], r["center_lat"]), axis=1
    )

    supermarkets_gdf = gpd.read_file(config.SUPERMARKETS_CACHE_PATH)

    for query in town_names:
        matches = _find_towns(query, towns_gdf)
        if matches.empty:
            print(f"\n[ERROR] '{query}' not found in the towns cache -- check exact "
                  f"spelling and accents, or pass the six-digit ISTAT code.")
            continue
        if len(matches) > 1:
            print(f"\n[INFO] '{query}' matches {len(matches)} towns -- checking each. "
                  f"Pass an ISTAT code to check just one.")
        for _, row in matches.iterrows():
            check_town(row, supermarkets_gdf)
