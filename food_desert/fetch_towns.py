"""
fetch_towns.py
---------------
Builds the towns dataset: one row per municipality, with its boundary polygon,
its ISTAT code as tagged in OSM, and the point where its name label sits on
the map.

Everything is read from the local .osm.pbf via `osmium`, in two passes and
with no network involved:

  Pass 1  relations only   every admin_level=8 boundary: name, ref:ISTAT, and
                           the node ID of its admin_centre (or label) member
  Pass 2  full, located    libosmium assembles those same relations into
                           polygons; the node callback picks up the centre
                           nodes and every place node for the fallback

--- WHY NOT NOMINATIM ANY MORE -----------------------------------------------
Boundary polygons used to come from osmnx/Nominatim, one request per town name
at a mandatory 1 request/second. That was 30-60 minutes for three regions and
over two hours for Italy -- and it returned whatever Nominatim ranked first
for "<name>, Italy". Italy has dozens of comuni sharing a name across
provinces (Calliano, Livo, Samone, Castro, Peglio, San Teodoro...), so at
national scale a wrong-entity match stops being an occasional quirk and
becomes a steady source of wrong boundaries.

Assembling each boundary from its own relation removes both problems. Rows are
keyed by OSM relation ID throughout, never by name, so two towns with the same
name can no longer overwrite each other's polygon.
"""

import geopandas as gpd
import osmium
import osmnx as ox
import shapely
import shapely.wkb
from shapely.geometry import Point

from . import config
from .geo_utils import get_combined_target_polygon, METRIC_CRS, WGS84
from .osm_reader import apply_with_locations, require_pbf
from .population_istat import _preview

PLACE_TYPES = ("city", "town", "village")

# Relation member roles that mark a town's label point, in order of preference.
CENTRE_ROLES = ("admin_centre", "label")


def _clean_istat_ref(value):
    """
    Normalises an OSM ref:ISTAT tag to ISTAT's six-digit form ("025001").
    Returns None for anything that is not a plain number.
    """
    if not value:
        return None
    text = value.strip()
    return text.zfill(6) if text.isdigit() else None


class _AdminRelationCollector(osmium.SimpleHandler):
    """
    Pass 1: collects every admin_level=8 boundary relation's name, ISTAT code
    and centre node ID.

    Only the node ID is captured here, not its location. Relations are stored
    after nodes in a normal .osm.pbf, so which node IDs matter cannot be
    known until this pass has completed.
    """

    def __init__(self):
        super().__init__()
        self.records = {}
        self.wanted_node_ids = set()

    def relation(self, r):
        if r.tags.get("boundary") != "administrative" or r.tags.get("admin_level") != "8":
            return
        istat_ref = _clean_istat_ref(r.tags.get("ref:ISTAT"))
        # A comune's relation can lose its name tag in an edit (Pietramelara,
        # 2026) while keeping its ISTAT code. Dropping it for that would drop a
        # real comune without a trace, so it is kept with an empty name, which
        # population_istat.py fills in from ISTAT. Without either, there is
        # nothing to identify the relation by.
        name = r.tags.get("name") or r.tags.get("name:it") or ""
        if not name and not istat_ref:
            return

        refs = {m.role: m.ref for m in r.members
                if m.type == "n" and m.role in CENTRE_ROLES}
        centre_ref = next((refs[role] for role in CENTRE_ROLES if role in refs), None)
        if centre_ref is not None:
            self.wanted_node_ids.add(centre_ref)

        self.records[r.id] = {
            "name": name,
            "osm_id": r.id,
            "istat_ref": istat_ref,
            "province_name": config.TARGET_NAME if config.TARGET_LEVEL == "province" else None,
            "centre_ref": centre_ref,
        }


class _BoundaryCollector(osmium.SimpleHandler):
    """
    Pass 2: assembles the pass-1 relations into boundary polygons, and
    resolves node locations for the label points.

    Defining area() makes osmium run its multipolygon assembler over the file.
    It assembles every area it finds (buildings, parks...), so the callback
    discards anything that is not one of the wanted relations as its first
    step.
    """

    def __init__(self, relation_ids, wanted_node_ids):
        super().__init__()
        self.relation_ids = relation_ids
        self.wanted_node_ids = wanted_node_ids
        self.boundaries = {}
        self.node_locations = {}
        self.place_nodes = {}       # lower-cased name -> [Point, ...]
        self._wkb = osmium.geom.WKBFactory()

    def node(self, n):
        if n.id in self.wanted_node_ids:
            self.node_locations[n.id] = (n.location.lon, n.location.lat)
        if n.tags.get("place") in PLACE_TYPES:
            name = n.tags.get("name")
            if name:
                self.place_nodes.setdefault(name.strip().lower(), []).append(
                    Point(n.location.lon, n.location.lat)
                )

    def area(self, a):
        if a.from_way() or a.orig_id() not in self.relation_ids:
            return
        try:
            wkb = self._wkb.create_multipolygon(a)
        except RuntimeError:
            return  # broken geometry -- reported as missing below
        self.boundaries[a.orig_id()] = shapely.wkb.loads(wkb, hex=True)


def _label(record):
    """A town's name for log lines, or its ISTAT code / relation ID if unnamed."""
    return record["name"] or f"ISTAT {record['istat_ref'] or '?'} (relation {record['osm_id']})"


def _fetch_boundaries_from_nominatim(relation_ids):
    """
    Returns {relation_id: polygon} for boundary relations osmium could not
    assemble -- a gap in a ring, or a member way missing from the extract.

    Looked up by relation ID, never by name, so a comune cannot be handed a
    same-named comune's outline. Nominatim repairs small ring defects when it
    builds its polygons, so it often has an outline where libosmium, which is
    strict by design, gives up. Anything it can only return as a point or a
    line is treated as not found.
    """
    if not relation_ids:
        return {}
    print(f"[INFO] Fetching {len(relation_ids)} broken comune boundaries from "
          f"Nominatim by relation ID...")
    recovered = {}
    for rel_id in relation_ids:
        try:
            gdf = ox.geocode_to_gdf(f"R{rel_id}", by_osmid=True)
        except Exception as e:
            print(f"[WARN] Nominatim lookup failed for relation {rel_id}: {e}")
            continue
        geom = gdf.geometry.iloc[0]
        if geom.geom_type in ("Polygon", "MultiPolygon") and not geom.is_empty:
            recovered[rel_id] = geom
    return recovered


def _choose_centre(record, boundary, collector):
    """
    Picks the town's label point:

      1. its tagged admin_centre / label node;
      2. otherwise a place node of the SAME NAME lying INSIDE the boundary;
      3. otherwise a point guaranteed to lie inside the boundary.

    Step 2 used to be a bare name lookup across the whole extract. Place names
    repeat constantly ("San Giorgio", "Villanova"), and with a national extract
    that lookup would happily put a town's centre in a different region.
    """
    ref = record["centre_ref"]
    if ref is not None and ref in collector.node_locations:
        return Point(*collector.node_locations[ref]), False

    for candidate in collector.place_nodes.get(record["name"].strip().lower(), []):
        if boundary.contains(candidate):
            return candidate, False

    # representative_point(), not centroid: a crescent-shaped comune's
    # centroid can fall outside it, in the next town over or the sea.
    return boundary.representative_point(), True


def fetch_towns_from_pbf():
    """Reads every admin_level=8 boundary in the extract, with its label point."""
    pbf = require_pbf(config.OSM_PBF_PATH)

    print(f"[INFO] Pass 1/2: reading town boundary relations from {pbf.name}...")
    relations = _AdminRelationCollector()
    relations.apply_file(str(pbf))
    print(f"[INFO] Found {len(relations.records)} admin_level=8 boundary relations "
          f"({sum(1 for r in relations.records.values() if r['istat_ref'])} "
          f"carry an ISTAT code).")

    print(f"[INFO] Pass 2/2: assembling boundary polygons (index: "
          f"{config.OSMIUM_NODE_INDEX.split(',')[0]}). This is the slowest step of "
          f"a first run -- roughly 10-25 minutes for all of Italy -- and is "
          f"cached afterwards.")
    collector = _BoundaryCollector(set(relations.records), relations.wanted_node_ids)
    apply_with_locations(collector, pbf)

    broken = [rel_id for rel_id in relations.records
              if collector.boundaries.get(rel_id) is None
              or collector.boundaries[rel_id].is_empty]
    italian_broken = [rel_id for rel_id in broken if relations.records[rel_id]["istat_ref"]]
    recovered = _fetch_boundaries_from_nominatim(italian_broken)

    rows, geoms, missing, fallbacks = [], [], [], []
    for rel_id, record in relations.records.items():
        boundary = collector.boundaries.get(rel_id) or recovered.get(rel_id)
        if boundary is None or boundary.is_empty:
            missing.append(record)
            continue
        if not boundary.is_valid:
            boundary = shapely.make_valid(boundary)

        centre, used_fallback = _choose_centre(record, boundary, collector)
        if used_fallback:
            fallbacks.append(_label(record))

        row = {k: v for k, v in record.items() if k != "centre_ref"}
        row["center_point"] = centre
        rows.append(row)
        geoms.append(boundary)

    print(f"[INFO] Assembled {len(rows)} boundary polygons.")
    # Split by ref:ISTAT, which only Italian comuni carry. Most broken
    # relations are foreign border towns the extract clips mid-boundary, which
    # the target-area filter would drop anyway; an Italian comune among them
    # is a real gap in the results and must not hide in that list.
    if recovered:
        print(f"[FALLBACK] {len(recovered)} Italian comuni have a broken boundary "
              f"relation in the extract; their outline was fetched from Nominatim "
              f"instead: {_preview([_label(relations.records[i]) for i in recovered])}")
    missing_italian = [_label(r) for r in missing if r["istat_ref"]]
    missing_other = [_label(r) for r in missing if not r["istat_ref"]]
    if missing_italian:
        print(f"[WARN] {len(missing_italian)} Italian comuni are MISSING from the "
              f"analysis: their boundary is broken in the extract and Nominatim "
              f"could not supply one. {_preview(missing_italian)}")
    if missing_other:
        print(f"[INFO] {len(missing_other)} relations without an ISTAT code could "
              f"not be assembled (normally foreign border towns the extract cuts "
              f"through) and are skipped: {_preview(missing_other)}")
    if fallbacks:
        print(f"[FALLBACK] {len(fallbacks)} towns have no tagged centre node or "
              f"same-named place node inside their boundary; using an interior "
              f"point instead -- verify manually: {_preview(fallbacks)}")

    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs=WGS84)
    return gdf.rename_geometry("boundary")


def _simplify_boundaries(gdf):
    """Simplifies boundaries to config.TOWN_BOUNDARY_SIMPLIFY_M, in metres."""
    tolerance = config.TOWN_BOUNDARY_SIMPLIFY_M
    if not tolerance:
        return gdf
    before = int(shapely.get_num_coordinates(gdf.geometry.values).sum())
    gdf = gdf.copy()
    gdf["boundary"] = (
        gdf.geometry.to_crs(METRIC_CRS)
        .simplify(tolerance, preserve_topology=True)
        .to_crs(WGS84)
    )
    after = int(shapely.get_num_coordinates(gdf.geometry.values).sum())
    print(f"[INFO] Simplified boundaries at {tolerance}m: "
          f"{before:,} -> {after:,} vertices.")
    return gdf


def _filter_to_target_regions(gdf):
    """
    The extract includes some territory beyond the target area -- border
    strips of neighbouring countries, San Marino and the Vatican for Italy;
    bordering regions for a regional extract. This trims the towns down to
    those whose centre point actually falls inside the target area.

    Called ONCE, before the result is cached. Running it again on cache load
    would re-geocode the target area on every run, to re-filter data that was
    already filtered before it was saved.
    """
    if config.TARGET_LEVEL == "province":
        return gdf  # single-province mode does not need this filter

    print("[INFO] Filtering towns to the target area "
          "(the local extract includes some bordering territory)...")
    target = get_combined_target_polygon()   # prepared -- see geo_utils

    xs = gdf["center_point"].map(lambda p: p.x).to_numpy()
    ys = gdf["center_point"].map(lambda p: p.y).to_numpy()
    mask = shapely.contains_xy(target, xs, ys)

    filtered = gdf[mask].copy()
    removed = len(gdf) - len(filtered)
    if removed:
        print(f"[INFO] Removed {removed} towns outside the target area "
              f"(e.g. neighbouring regions or countries picked up by the extract).")
    return filtered


def build_towns_dataset():
    """
    Full assembly: boundaries plus label points.
    Columns: name, osm_id, istat_ref, province_name, boundary (polygon),
             center_point (Point)

    The result is cached to config.TOWNS_CACHE_PATH so that a crash in a
    LATER pipeline step does not cost another full pass over the extract. Set
    config.FORCE_REFRESH_CACHE = True to rebuild.

    WARNING: FORCE_REFRESH_CACHE also invalidates the supermarket cache. If
    you only need supermarkets rebuilt, delete that cache file directly rather
    than setting this flag.
    """
    if not config.FORCE_REFRESH_CACHE and config.TOWNS_CACHE_PATH.exists():
        print(f"[INFO] Loading towns from cache: {config.TOWNS_CACHE_PATH}")
        gdf = gpd.read_file(config.TOWNS_CACHE_PATH)

        if gdf.geometry.name != "boundary":
            print(f"[INFO] Renaming geometry column '{gdf.geometry.name}' -> 'boundary'.")
            gdf = gdf.rename_geometry("boundary")

        gdf["center_point"] = gdf.apply(
            lambda r: Point(r["center_lon"], r["center_lat"]), axis=1
        )
        gdf = gdf.drop(columns=["center_lon", "center_lat"])

        # NOTE: no re-filter here. The cache was written post-filter.
        print(f"[INFO] Loaded {len(gdf)} towns from cache (already filtered to target area).")
        return gdf

    gdf = fetch_towns_from_pbf()
    gdf = _simplify_boundaries(gdf)
    gdf = _filter_to_target_regions(gdf)

    config.TOWNS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache_gdf = gdf.copy()
    cache_gdf["center_lon"] = cache_gdf["center_point"].apply(lambda p: p.x)
    cache_gdf["center_lat"] = cache_gdf["center_point"].apply(lambda p: p.y)
    cache_gdf = cache_gdf.drop(columns=["center_point"])
    cache_gdf.to_file(config.TOWNS_CACHE_PATH, driver="GPKG")
    print(f"[INFO] Cached towns dataset to {config.TOWNS_CACHE_PATH}")

    return gdf
