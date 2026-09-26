"""
pipeline.py
-----------
Main orchestrator. Runs the full food-desert analysis:

  1. Fetch comune boundaries (used to place settlements and count shops)
  2. Attach official ISTAT population figures to each comune
  3. Build the settlements -- ISTAT's towns and villages -- with population
     figures scaled from their comune (see settlements.py)
  4. Fetch supermarkets/minimarkets from the local OSM extract, and add the
     stores ISTAT's register lists but OSM has not mapped, placed in the
     settlements most likely to hold them (see shops_istat.py)
  5. For each settlement:
       a. Exclude it if a shop -- mapped or inferred -- lies inside its
          outline or within config.SETTLEMENT_SHOP_BUFFER_M of it
       b. Otherwise route on foot to the nearest candidate shops
       c. Exclude it if the shortest walk is within
          config.DISTANCE_THRESHOLD_KM
  6. Export results to spreadsheet + GeoJSON for the web map

There is deliberately no automated cycling-lane/sidewalk check. Instead the
web map renders the cycling-lane layer on top of each selected settlement's
route, so infrastructure can be assessed by eye against the actual path.

Settlements are identified by ISTAT's locality code (settlement_id) wherever
one is needed as a key. Names repeat across Italy -- there are dozens of
"San Giorgio" -- codes do not.

Run with:  python run.py analyze
"""

import json
from datetime import date

import pandas as pd
import geopandas as gpd
from tqdm import tqdm

from . import config
from .fetch_towns import build_towns_dataset
from .fetch_supermarkets import fetch_supermarkets
from .population_istat import attach_population
from .routing import (
    check_osrm_available,
    check_osrm_coverage,
    project_points_to_metric,
    find_supermarket_candidates,
    get_walking_route,
)
from .regions import region_of
from .settlements import build_settlements
from .shops_istat import (
    INFERRED,
    infer_register_stores,
    load_shop_register,
    shops_by_settlement,
)
from .export_spreadsheet import export_to_spreadsheet
from .geo_utils import METRIC_CRS
from . import paths

# Route lines are simplified to this tolerance, in metres, before they are
# written for the map: a walk drawn at street scale needs nothing finer, and
# thousands of full-resolution routes would make the page slow to open.
ROUTE_SIMPLIFY_M = 3


def _safe_value(value, fallback=None):
    """
    Returns `value` unless it is None or NaN, in which case returns `fallback`.

    A plain `value or fallback` is NOT safe here: float('nan') is truthy in
    Python, so an `or` chain would happily keep a stray NaN instead of
    falling through.

    This matters more than it looks. json.dump() writes NaN as a bare `NaN`
    token, which is not valid JSON, and JavaScript's JSON.parse() rejects the
    entire file when it hits one. A single NaN anywhere in towns.geojson
    would make EVERY town vanish from the map -- not just the bad row.
    """
    if value is None:
        return fallback
    try:
        if pd.isna(value):
            return fallback
    except (TypeError, ValueError):
        pass  # not NaN-checkable (e.g. it's a string) -- that's fine
    return value


def _int_or_none(value):
    """int() for pandas values that may be NA; json rejects numpy integers."""
    return int(value) if pd.notna(value) else None


def run_pipeline():
    paths.ensure_directories()
    print(f"[INFO] Study area: {config.STUDY_AREA_LABEL} "
          f"(extract: {config.OSM_PBF_PATH.name})")
    check_osrm_available()

    print("[STEP 1] Fetching comune boundaries and label points...")
    towns = build_towns_dataset()

    print("[STEP 2] Attaching ISTAT population data...")
    towns = attach_population(towns)
    check_osrm_coverage(towns["center_point"])

    print("[STEP 3] Building settlements...")
    settlements = build_settlements(towns)

    print("[STEP 4] Fetching supermarkets (study area + border buffer)...")
    osm_shops = fetch_supermarkets(towns)
    register = load_shop_register()
    inferred, inferred_audit = infer_register_stores(settlements, towns, osm_shops, register)
    shops = pd.concat([osm_shops, inferred], ignore_index=True)

    # --- Project everything once, up front ---------------------------------
    # Every distance and buffer operation below happens in metres. Doing the
    # projection here, vectorised, is dramatically cheaper than converting
    # geometry inside the per-settlement loop.
    print("[STEP 4b] Projecting geometry to a metric CRS and building spatial index...")
    shops_metric = shops.to_crs(METRIC_CRS)
    shops_metric.sindex  # force the R-tree to build now, not lazily mid-loop
    centers_metric = project_points_to_metric(settlements["center_point"])

    # Criterion 1, for every settlement at once: a shop of its own.
    settlements_metric = settlements.to_crs(METRIC_CRS)
    own_osm = shops_by_settlement(settlements_metric, shops_metric[shops["shop_type"] != INFERRED])
    own_any = shops_by_settlement(settlements_metric, shops_metric)
    served_by_osm = int((own_osm > 0).sum())
    served_by_inferred = int(((own_any > 0) & (own_osm == 0)).sum())

    results = []
    route_geoms_for_map = []
    unroutable = []          # settlements OSRM could not find a path for
    excluded_close_enough = 0

    print("[STEP 5] Evaluating each settlement against the criteria...")
    for pos in tqdm(range(len(settlements))):
        if own_any.iloc[pos] > 0:
            continue
        s = settlements.iloc[pos]
        center_metric = centers_metric.iloc[pos]
        origin = s["center_point"]

        # --- Criterion 2: shortest ACTUAL walk to any nearby shop ----------
        # Several candidates are routed rather than just the straight-line
        # nearest. A shop that looks closest on a map may be across a river,
        # lake or motorway with no crossing -- see ROUTING_CANDIDATE_COUNT in
        # config.py for the Papozze case that motivated this.
        candidate_positions = find_supermarket_candidates(center_metric, shops_metric)

        walk_km, walk_route, nearest = None, None, None
        for position in candidate_positions:
            candidate = shops.iloc[position]
            km, route = get_walking_route(origin, candidate.geometry)
            if km is None:
                continue
            if walk_km is None or km < walk_km:
                walk_km, walk_route, nearest = km, route, candidate
            if walk_km <= config.DISTANCE_THRESHOLD_KM:
                # Already inside the threshold -- no closer candidate can
                # change the outcome, so stop routing.
                break

        if walk_km is None:
            # Not one candidate could be reached on foot. Recorded rather than
            # dropped: no walkable route to ANY nearby shop is arguably the most
            # severe finding available, and silently discarding it would remove
            # the worst cases from a report about access.
            unroutable.append({
                "name": s["name"],
                "comune": s["comune"],
                "settlement_id": s["settlement_id"],
                "istat_code": s["istat_code"],
                "province": _safe_value(s["province"], "Unknown"),
                "lat": origin.y,
                "lon": origin.x,
                "candidates_tried": [str(shops.iloc[p]["name"]) for p in candidate_positions],
            })
            continue

        if walk_km <= config.DISTANCE_THRESHOLD_KM:
            excluded_close_enough += 1
            continue

        # --- Passed both criteria: record it -------------------------------
        population = _int_or_none(s["population"])
        population_65plus = _int_or_none(s["population_65plus"])
        pct_65plus = _safe_value(s["pct_65plus"])
        aging_index = _safe_value(s["aging_index"])

        # How far past the acceptable threshold this settlement sits, and how
        # many people that burdens. See VULNERABILITY SCORING in config.py.
        excess_km = round(walk_km - config.DISTANCE_THRESHOLD_KM, 2)
        vulnerability = (
            round(population_65plus * excess_km) if population_65plus is not None else None
        )
        specialists = register.get(s["istat_code"], (0, 0))[1] if register is not None else None

        results.append({
            "name": s["name"],
            "comune": s["comune"],
            "settlement_id": s["settlement_id"],
            "istat_code": s["istat_code"],
            "province": _safe_value(s["province"], "Unknown"),
            "region": region_of(s["istat_code"]),
            "is_main_town": bool(s["is_main_town"]),
            "population": population,
            "distance_km": round(walk_km, 2),
            "flagged_for_review": walk_km >= config.DISTANCE_REVIEW_THRESHOLD_KM,
            "population_65plus": population_65plus,
            "pct_65plus": round(float(pct_65plus), 1) if pct_65plus is not None else None,
            "aging_index": round(float(aging_index), 1) if aging_index is not None else None,
            "excess_km": excess_km,
            "vulnerability": vulnerability,
            "nearest_supermarket": nearest["name"],
            "nearest_is_inferred": nearest["shop_type"] == INFERRED,
            "town_lat": origin.y,
            "town_lon": origin.x,
            "supermarket_lat": nearest.geometry.y,
            "supermarket_lon": nearest.geometry.x,
            "istat_food_specialists": specialists,
        })

        route_geoms_for_map.append({
            "settlement_id": s["settlement_id"],
            "geometry": walk_route,
        })

    # --- Run summary: a full accounting of every settlement ----------------
    print("\n" + "=" * 62)
    print("RUN SUMMARY")
    print("=" * 62)
    print(f"  Settlements evaluated             : {len(settlements):,}")
    print(f"  Excluded -- shop mapped in OSM     : {served_by_osm:,}")
    print(f"  Excluded -- store inferred (ISTAT) : {served_by_inferred:,}")
    print(f"  Excluded -- within {config.DISTANCE_THRESHOLD_KM}km           : {excluded_close_enough:,}")
    print(f"  Unroutable (no walking path)       : {len(unroutable):,}")
    print(f"  MATCHED as underserved             : {len(results):,}")

    # Headline figures are computed from CONFIRMED results only -- rows flagged
    # for review are excluded here.
    #
    # This matters more than it looks. Flagged rows are, by definition, the
    # ones with implausibly large distances, which makes them exactly the rows
    # most likely to top a distance-weighted ranking. Reporting an unverified
    # outlier as the project's headline finding is how a single routing quirk
    # ends up quoted as a result. Flagged rows remain in the spreadsheet, which
    # is the full audit trail; they just do not drive the summary.
    confirmed = [r for r in results if not r.get("flagged_for_review", False)]
    flagged = len(results) - len(confirmed)

    affected_total = sum(r["population"] or 0 for r in confirmed)
    affected_65 = sum(r["population_65plus"] or 0 for r in confirmed)

    if affected_total:
        print("  " + "-" * 58)
        print(f"  Confirmed (unflagged) settlements  : {len(confirmed):,}")
        print(f"    of which main towns of a comune  : "
              f"{sum(r['is_main_town'] for r in confirmed):,}")
        print(f"  Residents affected                 : {affected_total:,}")
        print(f"    of whom aged 65+ (estimated)     : {affected_65:,} "
              f"({100 * affected_65 / affected_total:.1f}%)")

        ranked = sorted(confirmed, key=lambda r: r.get("vulnerability") or 0, reverse=True)
        print("\n  Most affected settlements (confirmed):")
        for r in ranked[:5]:
            label = f"{r['name']} ({r['comune']})" if r["name"] != r["comune"] else r["name"]
            print(f"    {label[:34]:36} {r['population_65plus'] or 0:>6,} aged 65+ "
                  f"@ {r['distance_km']:>5.1f}km  (score {r['vulnerability'] or 0:,})")

        if flagged:
            worst_flagged = max(results, key=lambda r: r["distance_km"])
            print(f"\n  {flagged:,} settlement(s) flagged for review and EXCLUDED from "
                  f"the figures above.")
            print(f"    Furthest: {worst_flagged['name']} ({worst_flagged['comune']}) at "
                  f"{worst_flagged['distance_km']}km -- verify this route by hand "
                  f"before quoting it.")
    print("=" * 62 + "\n")

    if unroutable:
        print(f"[WARN] {len(unroutable):,} settlements had no routable walking path to "
              f"their nearest shop. These are NOT in the results and are worth a "
              f"manual look -- see {config.UNROUTABLE_REPORT_PATH}")
    with open(config.UNROUTABLE_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(unroutable, f, ensure_ascii=False, indent=2)

    with open(config.INFERRED_STORES_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(inferred_audit, f, ensure_ascii=False, indent=2)
    if inferred_audit:
        print(f"[INFO] Stores placed from ISTAT's register: {config.INFERRED_STORES_REPORT_PATH}")

    counts = {
        "settlements_evaluated": len(settlements),
        "served_by_osm_shop": served_by_osm,
        "served_by_inferred_store": served_by_inferred,
        "within_threshold": excluded_close_enough,
        "unroutable": len(unroutable),
        "underserved": len(results),
        "confirmed": len(confirmed),
        "flagged": flagged,
        "confirmed_main_towns": sum(r["is_main_town"] for r in confirmed),
        "residents_affected": affected_total,
        "residents_65plus_affected": affected_65,
        "inferred_stores_placed": len(inferred_audit),
        # For comparison on the summary page: how old Italy is as a whole.
        "national_pct_65plus": round(100 * float(towns["population_65plus"].sum())
                                     / float(towns["population"].sum()), 1),
    }
    _write_outputs(results, route_geoms_for_map, towns, len(settlements))
    _write_summary(results, settlements, inferred_audit, counts)
    return results


def _write_outputs(results, route_geoms_for_map, towns, settlements_evaluated):
    export_to_spreadsheet(results, config.SPREADSHEET_PATH)

    # The spreadsheet intentionally keeps EVERY result, flagged or not -- it
    # is the full audit trail. The web map, by contrast, should only show
    # settlements whose figures have been accepted, so flagged ones (and
    # their routes) are excluded here.
    unflagged_results = [r for r in results if not r.get("flagged_for_review", False)]
    unflagged_ids = {r["settlement_id"] for r in unflagged_results}
    print(f"[INFO] Map will show {len(unflagged_results):,}/{len(results):,} settlements "
          f"({len(results) - len(unflagged_results):,} flagged ones excluded from the map).")

    towns_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point",
                             "coordinates": [round(r["town_lon"], 6), round(r["town_lat"], 6)]},
                "properties": {
                    "name": r["name"],
                    "comune": r["comune"],
                    "settlement_id": r["settlement_id"],
                    "istat_code": r["istat_code"],
                    "province": r["province"],
                    "region": r["region"],
                    "is_main_town": r["is_main_town"],
                    "population": r["population"],
                    "population_65plus": r["population_65plus"],
                    "pct_65plus": r["pct_65plus"],
                    "aging_index": r["aging_index"],
                    "vulnerability": r["vulnerability"],
                    "distance_km": r["distance_km"],
                    "flagged_for_review": r["flagged_for_review"],
                    "nearest_supermarket": r["nearest_supermarket"],
                    "nearest_is_inferred": r["nearest_is_inferred"],
                    "supermarket_lat": r["supermarket_lat"],
                    "supermarket_lon": r["supermarket_lon"],
                },
            }
            for r in unflagged_results
        ],
    }
    _write_compact_json(towns_geojson, config.GEOJSON_TOWNS_PATH)

    routes = gpd.GeoSeries([rg["geometry"] for rg in route_geoms_for_map], crs="EPSG:4326")
    simplified = routes.to_crs(METRIC_CRS).simplify(ROUTE_SIMPLIFY_M).to_crs("EPSG:4326")
    routes_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[round(x, 5), round(y, 5)] for x, y in line.coords],
                },
                "properties": {"settlement_id": rg["settlement_id"]},
            }
            for rg, line in zip(route_geoms_for_map, simplified)
            if rg["settlement_id"] in unflagged_ids
        ],
    }
    _write_compact_json(routes_geojson, config.GEOJSON_ROUTES_PATH)
    _write_study_area_meta(towns, settlements_evaluated)

    print(f"[OUTPUT] Spreadsheet   -> {config.SPREADSHEET_PATH}")
    print(f"[OUTPUT] Towns GeoJSON -> {config.GEOJSON_TOWNS_PATH}")
    print(f"[OUTPUT] Routes GeoJSON-> {config.GEOJSON_ROUTES_PATH}")
    print(f"[OUTPUT] Study area    -> {config.STUDY_AREA_META_PATH}")


# Walking-distance bands for the summary page's distribution chart, in km --
# all the same width, so the columns compare fairly.
DISTANCE_BANDS = [3, 4, 5, 6, 7, 8, 9, 10]

# Fields of results.json, in the order the explorer page shows them. Short
# keys would save little once gzipped, so the names match the spreadsheet's.
RESULT_FIELDS = ["name", "comune", "province", "region", "istat_code", "settlement_id",
                 "is_main_town", "population", "population_65plus", "pct_65plus",
                 "aging_index", "distance_km", "excess_km", "vulnerability",
                 "nearest_supermarket", "nearest_is_inferred", "flagged_for_review",
                 "istat_food_specialists"]


def _write_summary(results, settlements, inferred_audit, counts):
    """
    Writes the data behind the site's summary and explorer pages.

    summary.json holds the run's headline figures, a per-region breakdown, the
    spread of walking distances and the most affected settlements. Like the
    run summary, every figure except the flagged count is from CONFIRMED
    results only. results.json holds every result, flagged included, as the
    spreadsheet does.
    """
    confirmed = [r for r in results if not r["flagged_for_review"]]

    regions = {}
    for code, population in zip(settlements["istat_code"], settlements["population"]):
        region = regions.setdefault(region_of(code), {
            "settlements": 0, "residents": 0, "underserved": 0,
            "residents_affected": 0, "inferred_stores": 0})
        region["settlements"] += 1
        region["residents"] += _int_or_none(population) or 0
    for r in confirmed:
        regions[r["region"]]["underserved"] += 1
        regions[r["region"]]["residents_affected"] += r["population"] or 0
    for a in inferred_audit:
        regions[region_of(a["istat_code"])]["inferred_stores"] += 1
    by_region = [
        {"region": name, **v,
         "pct_affected": round(100 * v["residents_affected"] / v["residents"], 2)
         if v["residents"] else 0.0}
        for name, v in regions.items()
    ]
    by_region.sort(key=lambda v: v["pct_affected"], reverse=True)

    # The first band has no lower bound: every result is past the threshold,
    # but distance_km is rounded, so a 3.004 km walk is stored as 3.0 and
    # would fall into no band at all under a strict "> 3".
    bands = []
    edges = DISTANCE_BANDS + [None]
    for low, high in zip(edges, edges[1:]):
        inside = [r for r in confirmed
                  if (low == edges[0] or r["distance_km"] > low)
                  and (high is None or r["distance_km"] <= high)]
        if high is not None or inside:
            bands.append({"label": f"{low}–{high} km" if high else f"over {low} km",
                          "settlements": len(inside),
                          "residents": sum(r["population"] or 0 for r in inside)})

    top = sorted(confirmed, key=lambda r: r["vulnerability"] or 0, reverse=True)[:10]
    summary = {
        "label": config.STUDY_AREA_LABEL,
        "generated": date.today().isoformat(),
        "distance_threshold_km": config.DISTANCE_THRESHOLD_KM,
        "review_threshold_km": config.DISTANCE_REVIEW_THRESHOLD_KM,
        **counts,
        "by_region": by_region,
        "distance_bands": bands,
        "most_affected": [{k: r[k] for k in RESULT_FIELDS} for r in top],
    }
    with open(config.SUMMARY_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    _write_compact_json([{k: r[k] for k in RESULT_FIELDS} for r in results],
                   config.RESULTS_JSON_PATH)
    print(f"[OUTPUT] Summary       -> {config.SUMMARY_JSON_PATH}")
    print(f"[OUTPUT] Results table -> {config.RESULTS_JSON_PATH}")


def _write_study_area_meta(towns, settlements_evaluated):
    """
    Writes the study area's name and bounding box for the web map, which uses
    it to decide where to open and how far it may be panned. The box covers
    every comune EVALUATED, not only those with results, so it describes the
    area studied rather than the pattern of results.
    """
    min_lon, min_lat, max_lon, max_lat = towns.total_bounds
    meta = {
        "label": config.STUDY_AREA_LABEL,
        "bbox": [round(float(v), 4) for v in (min_lon, min_lat, max_lon, max_lat)],
        "towns_evaluated": int(len(towns)),
        "settlements_evaluated": int(settlements_evaluated),
        "distance_threshold_km": config.DISTANCE_THRESHOLD_KM,
        "generated": date.today().isoformat(),
    }
    with open(config.STUDY_AREA_META_PATH, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _write_compact_json(payload, path):
    """
    Writes JSON (GeoJSON or plain) without pretty-print indentation.

    These files are served to a browser, not read by hand. routes.geojson in
    particular holds a road polyline for every underserved settlement;
    indenting it roughly triples the file size, which directly slows down the
    hosted web map's initial load.
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
