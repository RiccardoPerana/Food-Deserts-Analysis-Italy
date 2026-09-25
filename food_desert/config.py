"""
config.py
---------
Central configuration for the Food Desert Analysis pipeline.

Change the AREA OF INTEREST block to move between a single province, several
regions, and the whole country. Switching scope needs three things to agree:
TARGET_LEVEL (below), the OSM extract (OSM_EXTRACT_NAME), and the OSRM routing
graph built from that same extract. `python run.py paths` checks all three.

--- A NOTE ON "SCALE HOOK" MARKERS -------------------------------------------
Settings tagged

    # SCALE HOOK (...): ...

are DELIBERATE extension points. Some are not read by any code path yet, which
makes them look identical to accidental dead code in a diff or a static
analysis pass. They are not dead code. Do not remove them during cleanup. Each
tag states what the setting is for and what work is required to activate it.

Anything NOT carrying that tag has no such protection.
"""

import re
import socket
import urllib.request

from . import paths

# ---------------------------------------------------------------------------
# GLOBAL NETWORK DEFAULTS
# ---------------------------------------------------------------------------
# Prevents indefinite hangs on an unresponsive Overpass mirror. Applied at
# import time because overpy uses urllib internally and offers no per-request
# timeout hook of its own.
socket.setdefaulttimeout(60)

# Public Overpass servers reject requests carrying a generic or missing
# User-Agent header with HTTP 406. Installing a global opener with a proper
# identifying agent fixes every overpy query in the project at once.
_opener = urllib.request.build_opener()
_opener.addheaders = [
    ("User-Agent", "FoodDesertAnalysisTool/1.0 (personal research project)")
]
urllib.request.install_opener(_opener)

# ---------------------------------------------------------------------------
# AREA OF INTEREST
# ---------------------------------------------------------------------------
# "country" | "multi_region" | "region" | "province"
#
# "country" analyses every comune in COUNTRY_NAME. It needs the national
# extract (OSM_EXTRACT_NAME = "italy-latest"), an OSRM graph built from it,
# and one ISTAT workbook per region in data/istat/ -- 21 files, since
# Trentino-Alto Adige is published as two.
#
# To go back to the original three-region study, set:
#     TARGET_LEVEL = "multi_region"
#     OSM_EXTRACT_NAME = "nord-est-latest"
TARGET_LEVEL = "country"

TARGET_NAME = "Padova"          # used when TARGET_LEVEL is "region" or "province"
TARGET_REGIONS = ["Veneto", "Friuli-Venezia Giulia", "Trentino-Alto Adige"]

# The country the study area lies in. The name is what Nominatim geocodes for
# the study-area outline (and qualifies region names with); the ISO code names
# the caches.
COUNTRY_NAME = "Italy"
COUNTRY_ISO = "IT"


def _scope_slug():
    """
    A short, filesystem-safe name for the configured study area, e.g.
    "country-it" or "multi-region-veneto-friuli-venezia-giulia-...".

    Every cache file is named after it. Before this, the caches had fixed
    names, so switching TARGET_LEVEL from three regions to the whole country
    silently reloaded the three-region towns cache and analysed the wrong
    area without a single warning.
    """
    if TARGET_LEVEL == "country":
        parts = [COUNTRY_ISO]
    elif TARGET_LEVEL == "multi_region":
        parts = TARGET_REGIONS
    else:
        parts = [TARGET_NAME]
    text = " ".join([TARGET_LEVEL, *parts]).lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


SCOPE_SLUG = _scope_slug()

# Human-readable name of the study area, shown in the web map's title.
STUDY_AREA_LABEL = (
    COUNTRY_NAME if TARGET_LEVEL == "country"
    else ", ".join(TARGET_REGIONS) if TARGET_LEVEL == "multi_region"
    else TARGET_NAME
)

# How far beyond the study area's edge to search for supermarkets, in km.
#
# This is NOT a future feature -- it fixes a limitation that exists right
# now: a town on the outer boundary whose nearest shop sits just across that
# boundary would otherwise never see it, and would be wrongly reported as
# underserved. Now applied in fetch_supermarkets.py.
#
# NOTE: changing this value invalidates the supermarket cache. Delete
# data/cache/supermarkets_<scope>.gpkg after adjusting it, or the cached clip
# radius silently persists and the new value has no effect.
BORDER_BUFFER_KM = 12

# ---------------------------------------------------------------------------
# LOCAL OSM EXTRACT
# ---------------------------------------------------------------------------
# One file serves four purposes: town boundaries, supermarket POIs, the
# cycling-lane / public-transport map overlays, and -- built separately in
# Docker -- the OSRM routing graph. The name is the Geofabrik file name
# without ".osm.pbf": https://download.geofabrik.de/europe/italy.html
OSM_EXTRACT_NAME = "italy-latest"
OSM_PBF_PATH = paths.OSM_DIR / f"{OSM_EXTRACT_NAME}.osm.pbf"

# Where `osrm-extract` writes the routing graph built from OSM_PBF_PATH. Only
# used by `run.py paths` to confirm the graph exists; the analysis itself talks
# to the running OSRM server, and checks at startup that it covers the study
# area (see routing.check_osrm_coverage).
OSRM_DATASET_PATH = paths.OSM_DIR / f"{OSM_EXTRACT_NAME}.osrm"

# SCALE HOOK (cross-border): the list of extracts to read supermarkets from.
# Kept as a LIST so that neighbouring-country extracts (Switzerland, Austria,
# Slovenia, France) can be appended to close the cross-border gap in Known
# Limitations without changing any code. The routing graph would need the same
# extracts merged in to route to those shops.
#
# Deduplication by (osm_id, osm_type) already handles the overlap where two
# adjacent extracts cover the same ground.
OSM_PBF_PATHS = [OSM_PBF_PATH]

# Where osmium keeps node coordinates while it resolves way and area geometry.
#
# This is the single hardest constraint on running nationally. The default
# in-memory index ("flex_mem") holds every node in the file at ~16 bytes each:
# ~1.5-3GB of RAM for the nord-est extract, but ~4.5-5GB for all of Italy, on
# top of GeoPandas and -- during `analyze` -- the OSRM server's own several GB.
# On a 16GB machine that tips into swapping.
#
# "sparse_file_array" keeps the same index in a temporary file on disk instead,
# with a near-flat memory profile. It is somewhat slower, and makes no
# difference to the results. The file lives in the system temp folder (%TEMP%
# on Windows) -- ~4.5GB per pass for Italy, up to two at once during `analyze`
# -- and is removed automatically when the command finishes.
#
# Give NO filename: osm_reader.py explains why a named index file breaks on
# Windows, and refuses one.
#
# For small extracts on a machine with RAM to spare, "flex_mem" is faster.
OSMIUM_NODE_INDEX = "sparse_file_array"

# ---------------------------------------------------------------------------
# CRITERIA THRESHOLDS
# ---------------------------------------------------------------------------
# Routed walking distance from the town centre to the nearest supermarket.
# Anything above this counts as underserved.
DISTANCE_THRESHOLD_KM = 3.0

# A shop inside a settlement's ISTAT outline, or within this many METRES of
# it, serves that settlement -- no routing needed.
#
# The comune-level analysis this replaced used 500 m around the boundary plus
# 1.5 km around the centre. Both are far too generous for settlements:
# villages can lie a kilometre apart, and a shop in the next one is exactly
# what the routed distance is there to measure.
SETTLEMENT_SHOP_BUFFER_M = 200

# Where the register's unmapped stores go. A comune's missing stores are given
# to its settlements without an OSM shop, main town first, then largest first,
# one each -- but a settlement other than the main town only qualifies if a
# store would be expected there: register count x its share of the comune's
# residents >= this value. Without that test, Poggiomarino's 34 unmapped
# stores would be spread one per settlement until every village in the
# comune counted as served, when most of them are in the town itself.
INFERRED_STORE_MIN_SHARE = 0.5

# Town boundaries are simplified to this tolerance (in metres) before caching.
# OSM comune boundaries carry a vertex every few metres; at national scale that
# is tens of millions of vertices, a several-hundred-MB cache, and slow spatial
# joins -- all for precision no step here needs.
TOWN_BOUNDARY_SIMPLIFY_M = 10

# Results at or beyond this distance are highlighted in the spreadsheet as
# worth a manual look rather than being silently trusted. Genuinely remote
# mountain towns really can be this far from a shop by road, so this is a
# review flag, not a rejection.
DISTANCE_REVIEW_THRESHOLD_KM = 10.0

# OSM shop tags that count as "supermarket or minimarket".
SUPERMARKET_TAGS = {
    "shop": ["supermarket", "convenience"]
}

# ---------------------------------------------------------------------------
# CACHING
# ---------------------------------------------------------------------------
# Named after the study area (SCOPE_SLUG), so each scope keeps its own caches
# and switching between them never loads the wrong one.
TOWNS_CACHE_PATH = paths.CACHE_DIR / f"towns_{SCOPE_SLUG}.gpkg"
SUPERMARKETS_CACHE_PATH = paths.CACHE_DIR / f"supermarkets_{SCOPE_SLUG}.gpkg"
# Settlements joined to their comune and its ISTAT figures. Delete it after
# changing SETTLEMENT_TYPES or the ISTAT files.
SETTLEMENTS_CACHE_PATH = paths.CACHE_DIR / f"settlements_{SCOPE_SLUG}.gpkg"
FORCE_REFRESH_CACHE = False   # True = ignore caches and rebuild everything

# ---------------------------------------------------------------------------
# ISTAT DEMOGRAPHIC DATA
# ---------------------------------------------------------------------------
# Official ISTAT workbooks, used EXACTLY AS DOWNLOADED -- no manual editing.
# Source: "Censimento della popolazione: dati regionali, anno 2024"
# https://www.istat.it/comunicato-territoriale/censimento-della-popolazione-dati-regionali-anno-2024/
#
# Reading the published .xlsx directly (rather than hand-converted CSVs) is
# what makes the demographic half of this analysis reproducible: anyone who
# clones the repository can download the same files and regenerate the same
# numbers. `python tools/download_istat.py` fetches all of them.
#
# NOTE: Trentino-Alto Adige ships as TWO workbooks, one per autonomous
# province (Trento, and Bolzano/Alto Adige) -- 21 files for the whole country.
def _discover_istat_workbooks():
    """
    Every .xlsx in data/istat/ is treated as an ISTAT regional workbook.

    Filenames are NOT hardcoded, deliberately. ISTAT's published names vary in
    punctuation between downloads ("Allegato-statistico" vs
    "Allegato_statistico", "04_1" vs "04.1"), browsers append " (1)" to
    duplicates, and Windows hides extensions -- so a hardcoded list produces a
    MISSING file for something that is visibly sitting in the folder.

    Discovery removes that entire class of problem: drop a workbook in, and it
    is picked up. Adding a region needs no code change at all, which is also
    what the national-scale path requires.
    """
    if not paths.ISTAT_DIR.exists():
        return []
    return sorted(paths.ISTAT_DIR.glob("*.xlsx"))


ISTAT_POPULATION_XLSX = _discover_istat_workbooks()

# Fallback for comuni no workbook covers: POSAS, from demo.istat.it -- resident
# population by comune and single year of age at 1 January 2025, i.e. the same
# census count as the workbooks' 31 December 2024 figure. Needed because ISTAT's
# published Liguria workbook contains no per-comune tables. Read as downloaded,
# straight from the .zip. The province file supplies province names, which the
# comuni file does not carry.
#
# The year is in the filename on purpose: pairing the 2024 workbooks with a
# later POSAS year would mix two reference dates in one table.
ISTAT_POSAS_COMUNI = paths.ISTAT_DIR / "POSAS_2025_it_Comuni.zip"
ISTAT_POSAS_PROVINCE = paths.ISTAT_DIR / "POSAS_2025_it_Province.zip"

# ISTAT's register of business locations (ASIA unita' locali), per comune, by
# 3-digit ATECO sector -- an independent check on OpenStreetMap, which is
# missing the shops of whole towns in parts of Italy (Poggiomarino, 22,600
# residents: no supermarket in OSM, 34 in the register). Two sectors are read:
#
#   471  non-specialised stores: supermarkets, discounters, minimarkets,
#        grocery stores (and some non-food general stores)
#   472  specialist food shops: bakers, butchers, greengrocers...
#
# The register gives counts per comune, not locations. Where it lists more
# 471 stores than OSM maps in a comune, the missing ones are placed in its
# settlements (see INFERRED_STORE_MIN_SHARE) and then treated like any other
# shop: they serve that settlement and can be routed to. 472 counts are only
# reported. Downloaded as published by tools/download_istat.py; if the file is
# absent, no stores are inferred and OSM is used alone, with a warning.
ISTAT_SHOP_REGISTER = paths.ISTAT_DIR / "ASIA_UL_2023_ateco_471_472.csv"

# ISTAT's inhabited localities, census 2021 ("Basi territoriali"): the polygon
# and 2021 population of every town, village and hamlet. The analysis runs on
# SETTLEMENTS rather than whole comuni, because comuni differ wildly in size:
# a Tuscan comune averages 66 km2 and several villages, a Piedmontese one
# 14 km2 and one. Asking "does the comune have a shop?" hid every village far
# from its main town in large-comune regions, and counted every hamlet in
# small-comune ones. Read straight from the downloaded .zip.
ISTAT_LOCALITIES = paths.ISTAT_DIR / "Localita_21.zip"

# Which localities are analysed, by ISTAT's TIPO_LOC: 1 = centro abitato
# (towns and villages, 91% of residents). 2 = nucleo abitato (hamlets, median
# 26 residents) is left out by choice; 3 = industrial areas and 4 = scattered
# houses (no location of their own) cannot be analysed.
SETTLEMENT_TYPES = (1,)

# Drop towns with no ISTAT match from the analysis entirely.
#
# ISTAT covers every Italian municipality, so a town that fails to match is
# almost always one the OSM extract picked up from OUTSIDE the study area --
# Swiss, Austrian, Slovenian, French, San Marino, or a neighbouring Italian
# region. Leaving them in means analysing towns that are not in the stated
# study area, and reporting them without population figures.
#
# The other cause is a comune created by a merger after the ISTAT reference
# date: OSM already carries the new comune, the workbooks still list its
# predecessors. population_istat.py reports those separately.
#
# Set to False to keep them and inspect the list first. Turning this on is
# the recommended default once you have reviewed the unmatched names once.
EXCLUDE_UNMATCHED_TOWNS = True

# Comuni merged after the ISTAT reference date (31 December 2024), which OSM
# already maps as one boundary:  successor code -> (name, predecessor codes).
# Their ISTAT rows are summed under the successor's code. For a merger by
# incorporation the successor keeps its own code, so it is also listed among
# the predecessors.
#
# A new entry is needed when the log reports ISTAT comuni with no matching
# town. Entries are ignored once the ISTAT files list the successor itself.
COMUNE_MERGERS = {
    # New comune formed from Castegnero and Nanto (Vicenza).
    "024129": ("Castegnero Nanto", ["024027", "024071"]),
    # Lirio incorporated into Montalto Pavese, 31 January 2026.
    "018094": ("Montalto Pavese", ["018094", "018082"]),
}

# ---------------------------------------------------------------------------
# VULNERABILITY SCORING
# ---------------------------------------------------------------------------
# Distance alone treats a commuter town of 4,000 with a median age of 38 and a
# mountain village of 400 where a third of residents are over 75 as equivalent
# findings. They are not. The score below weights the distance burden by the
# number of people most affected by it.
#
#     vulnerability = residents_65plus x (routed_km - DISTANCE_THRESHOLD_KM)
#
# The unit is "elderly-kilometres": how many people are affected, multiplied by
# how far past the acceptable threshold they are. It has no meaning on its own,
# only as a ranking -- which is exactly what it is for. Sorting by it surfaces
# where the problem is LARGEST, whereas sorting by distance alone surfaces only
# where it is most extreme, which tends to be tiny hamlets.
#
# 65 is the standard pensionable-age cutoff and matches ISTAT's own "indice di
# vecchiaia" numerator, so the figure is directly comparable to published
# statistics rather than being a threshold invented here.
VULNERABILITY_AGE_FIELD = "population_65plus"

# ---------------------------------------------------------------------------
# WEB MAP PAYLOAD SIZE
# ---------------------------------------------------------------------------
# The cycling and public-transport overlays are by far the largest published
# data: ~15MB together for three regions, on the order of 100MB for Italy.
#
# They are therefore written as a GRID of small GeoJSON files -- one per
# MAP_TILE_SIZE_DEG square that contains anything -- plus a manifest.json
# listing them. The web map fetches only the cells under the current view,
# and only once a layer is switched on and zoomed in far enough to be drawn
# (cycling at zoom 14, a few cells; transit at zoom 10, a few dozen). Nothing
# overlay-related is downloaded when the page opens.
#
# Smaller cells mean smaller downloads per pan but more requests; 0.25 degrees
# is roughly 25 x 20 km in Italy.
MAP_TILE_SIZE_DEG = {
    "cycling": 0.25,
    "transit": 0.5,
}

# Within each cell, two cheap reductions, neither visible at map zoom levels:
#
# 1. COORDINATE PRECISION. Raw OSM coordinates carry 13+ decimal places
#    (11.876843210987...). Five decimals is about 1.1 metres -- far finer than
#    the underlying survey accuracy. Each number shrinks from ~17 characters to
#    ~8, which roughly halves the file on its own.
#
# 2. GEOMETRY SIMPLIFICATION. Douglas-Peucker removes points that sit
#    essentially on a straight line between their neighbours. At a ~2 metre
#    tolerance a cycle path keeps its shape but sheds redundant vertices.
#
# Set SIMPLIFY_TOLERANCE_DEG to 0 to disable simplification entirely.
MAP_COORD_PRECISION = 5           # decimal places; 5 ~= 1.1m
MAP_SIMPLIFY_TOLERANCE_DEG = 0.00002   # ~2m in Italy

# ---------------------------------------------------------------------------
# OUTPUT PATHS
# ---------------------------------------------------------------------------
# All resolved from paths.py, which anchors them to the repository root --
# so the pipeline behaves identically regardless of your working directory.
OUTPUT_DIR = paths.OUTPUT_DIR
SPREADSHEET_PATH = OUTPUT_DIR / "food_desert_towns.xlsx"
GEOJSON_TOWNS_PATH = OUTPUT_DIR / "towns.geojson"
GEOJSON_ROUTES_PATH = OUTPUT_DIR / "routes.geojson"

# Directories of grid cells, not single files -- see WEB MAP PAYLOAD SIZE.
GEOJSON_CYCLING_DIR = OUTPUT_DIR / "cycling_lanes"
GEOJSON_TRANSIT_DIR = OUTPUT_DIR / "public_transport"

# The study area's name and bounding box. The web map reads it to decide where
# to open and how far it can be panned, so the page itself hardcodes no region.
STUDY_AREA_META_PATH = OUTPUT_DIR / "meta.json"

# Everything the published web map loads. `run.py publish` copies these into
# docs/data/ from OUTPUT_DIR as an explicit, deliberate step -- so an
# experimental run can never silently become your live demo.
PUBLISHED_DATA_DIR = paths.DOCS_DATA_DIR
# Data for the site's summary and explorer pages: every underserved settlement
# (flagged ones included, as in the spreadsheet), and the run's headline
# figures with a per-region breakdown.
RESULTS_JSON_PATH = OUTPUT_DIR / "results.json"
SUMMARY_JSON_PATH = OUTPUT_DIR / "summary.json"

PUBLISHABLE_OUTPUTS = [
    GEOJSON_TOWNS_PATH,
    GEOJSON_ROUTES_PATH,
    STUDY_AREA_META_PATH,
    RESULTS_JSON_PATH,
    SUMMARY_JSON_PATH,
    SPREADSHEET_PATH,
    GEOJSON_CYCLING_DIR,
    GEOJSON_TRANSIT_DIR,
]

# The files the MAP page downloads when it opens -- what `publish` reports as
# the first load. The summary and explorer pages fetch their own data.
MAP_FIRST_LOAD = [GEOJSON_TOWNS_PATH, GEOJSON_ROUTES_PATH, STUDY_AREA_META_PATH]

# Towns for which OSRM could find no walking route to any candidate shop.
# Written out for manual inspection rather than dropped.
UNROUTABLE_REPORT_PATH = OUTPUT_DIR / "unroutable_towns.json"

# Every store placed from ISTAT's register (see INFERRED_STORE_MIN_SHARE):
# which settlement it was put in, and the comune's register and OSM counts,
# so each inference can be audited.
INFERRED_STORES_REPORT_PATH = OUTPUT_DIR / "inferred_stores.json"

# ---------------------------------------------------------------------------
# ROUTING (OSRM, self-hosted -- see README)
# ---------------------------------------------------------------------------
# How many nearby supermarkets to route to before choosing the closest.
#
# WHY THIS IS NOT 1. The obvious approach -- find the straight-line nearest
# shop, route to it, report that distance -- is wrong wherever geography gets
# in the way. Papozze, on the Po delta, has an unnamed shop 2.8km away in a
# straight line, but it sits on the far bank with no bridge nearby: the real
# walking route is 28.7km. A Coop 7.1km away straight-line is reachable in
# roughly 8km on foot. Routing only to the straight-line nearest reported
# Papozze as a 28.7km food desert when it is an 8km one.
#
# The same failure occurs anywhere a river, lake, motorway or ridge separates
# a town from a shop that looks close on a map. Routing to several candidates
# and keeping the shortest ACTUAL walk removes it.
#
# Cost is roughly linear in this number, but OSRM is local and answers in
# milliseconds, so 5 candidates is cheap insurance.
ROUTING_CANDIDATE_COUNT = 5

# Radius searched for those candidates. Generous on purpose: a town whose
# nearest shops are all across a river needs candidates well beyond the
# straight-line nearest to find a reachable one.
ROUTING_CANDIDATE_SEARCH_M = 25_000

OSRM_BASE_URL = "http://localhost:5000"
OSRM_PROFILE_WALK = "foot"
OSRM_TIMEOUT_SEC = 15

# NOTE: there is deliberately NO pause between OSRM requests. OSRM runs in a
# local Docker container -- there is no third-party server to be polite to,
# and a 2-second courtesy delay across ~1,000 towns was costing over half an
# hour per run for no benefit whatsoever.

# ---------------------------------------------------------------------------
# NOMINATIM / OVERPASS
# ---------------------------------------------------------------------------
# Nominatim is now used for ONE thing: the outline of the study area itself
# (a single request per region, or one for the whole country), cached on disk
# by osmnx under data/cache/osmnx/. Town boundaries used to be geocoded one
# request per town at Nominatim's mandatory 1 request/second -- over two hours
# for Italy, and prone to matching a different town of the same name. They are
# now assembled from the local .osm.pbf instead; see fetch_towns.py.

# Overpass is used only by diagnostics.py, never by the analysis itself.
# Public mirrors go through real periods of instability, so query_with_retry()
# rotates through this list on each retry rather than hammering one broken
# mirror.
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]
OVERPASS_TIMEOUT = 180
