"""
fetch_map_layers.py
-------------------
Builds the two toggleable overlay layers for the web map -- cycling lanes and
public transport routes -- both read from the local .osm.pbf via osmium.

Target-area polygons come from geo_utils.get_combined_target_polygon(),
shared with fetch_towns.py and memoised. That helper raises on an unsupported
TARGET_LEVEL rather than returning nothing -- otherwise an unrecognised target
area produces valid-but-empty GeoJSON and exits successfully, a failure you
would only notice as a blank map.

--- TWO PASSES, NOT FOUR -----------------------------------------------------
Transit geometry needs a relations-only pass first (to learn which ways the
bus routes use), then a pass with node locations. Cycling needs only the
located pass. Both layers share that second pass rather than each paying for
its own: on the Italian extract, every located pass is several minutes and a
multi-GB node index.

--- WHY THE OUTPUT IS A GRID OF FILES ----------------------------------------
See WEB MAP PAYLOAD SIZE in config.py. Each layer is written as one small
GeoJSON per non-empty grid cell plus a manifest.json, and the web map fetches
only the cells in view.
"""

import json
import math

import numpy as np
import osmium
import shapely
from shapely.geometry import LineString
from shapely.ops import unary_union

from . import config
from .geo_utils import get_combined_target_polygon
from .osm_reader import apply_with_locations, require_pbf

ROUTE_TYPES = {"bus", "tram", "trolleybus"}


def _round_coords(coords):
    """Trims coordinate precision to config.MAP_COORD_PRECISION decimals."""
    n = config.MAP_COORD_PRECISION
    return [[round(x, n), round(y, n)] for x, y in coords]


def _simplify(line):
    """
    Douglas-Peucker simplification at config.MAP_SIMPLIFY_TOLERANCE_DEG.

    preserve_topology=False is deliberate and safe here: these are independent
    display-only line segments, not a topological network, so there are no
    shared boundaries to keep valid. The faster non-topological algorithm is
    the right choice.
    """
    tolerance = config.MAP_SIMPLIFY_TOLERANCE_DEG
    if not tolerance:
        return line
    return line.simplify(tolerance, preserve_topology=False)


def _explode_lines(geoms):
    """
    Flattens clipping results into plain LineStrings. Clipping a line can
    return a MultiLineString where it re-enters the area, or stray points
    where it only touches the edge -- the latter are dropped.
    """
    lines = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            lines.append(geom)
        elif hasattr(geom, "geoms"):
            lines.extend(_explode_lines(geom.geoms))
    return lines


def _as_array(lines):
    """A 1-D object array of geometries, for shapely's vectorised functions."""
    arr = np.empty(len(lines), dtype=object)
    arr[:] = lines
    return arr


def _clip_to_area(lines, area, label):
    """
    Clips lines to the target area.

    Lines entirely inside are kept whole and only those crossing the edge are
    actually intersected. With a prepared national outline, the containment
    test is fast; an intersection against hundreds of thousands of vertices is
    not, so it is reserved for the few thousand lines that need it.
    """
    geoms = _as_array(lines)
    if len(geoms) == 0:
        return []
    inside = shapely.contains(area, geoms)
    crossing = ~inside & shapely.intersects(area, geoms)
    clipped = _explode_lines(shapely.intersection(geoms[crossing], area))
    kept = list(geoms[inside]) + clipped
    print(f"[INFO] {label}: {len(kept)} segments inside the target area "
          f"({int(crossing.sum())} clipped at its edge).")
    return kept


def _clear_layer_dir(out_dir):
    """
    Removes the previous run's cells. Only files this module writes are
    touched, so a misconfigured path cannot take anything else with it.
    Stale cells matter: a cell that is empty in this run but left over from
    the last one would never be listed in the manifest, yet still be
    published.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in [*out_dir.glob("*.geojson"), out_dir / "manifest.json"]:
        old.unlink(missing_ok=True)


def _write_chunked_layer(lines, out_dir, cell_deg, label, merge_overlaps=False):
    """
    Splits lines into a grid of cell_deg x cell_deg cells and writes one
    minified GeoJSON per non-empty cell, named "<ix>_<iy>.geojson" where
    ix = floor(lon / cell_deg) and iy = floor(lat / cell_deg), plus a
    manifest.json listing them. The web map computes the same indices from its
    viewport, so a cell's name is also its address.

    Lines are clipped at cell edges rather than assigned whole to one cell;
    otherwise a long line anchored in an unloaded cell would be missing from a
    view it crosses.

    merge_overlaps=True unions and re-merges the lines of each cell, so a road
    served by five bus routes is drawn once instead of five times.
    """
    _clear_layer_dir(out_dir)
    geoms = _as_array(lines)
    cells, total_features, total_bytes = [], 0, 0

    if len(geoms):
        tree = shapely.STRtree(geoms)
        min_x, min_y, max_x, max_y = shapely.total_bounds(geoms)
        for ix in range(math.floor(min_x / cell_deg), math.floor(max_x / cell_deg) + 1):
            for iy in range(math.floor(min_y / cell_deg), math.floor(max_y / cell_deg) + 1):
                cell = shapely.box(ix * cell_deg, iy * cell_deg,
                                   (ix + 1) * cell_deg, (iy + 1) * cell_deg)
                hits = tree.query(cell, predicate="intersects")
                if len(hits) == 0:
                    continue
                pieces = _explode_lines(shapely.intersection(geoms[hits], cell))
                if merge_overlaps and pieces:
                    # shapely.line_merge, not shapely.ops.linemerge: a cell
                    # holding a single line unions to a plain LineString, which
                    # linemerge rejects outright.
                    pieces = _explode_lines([shapely.line_merge(unary_union(pieces))])

                features = []
                for piece in pieces:
                    simplified = _simplify(piece)
                    if simplified.is_empty or len(simplified.coords) < 2:
                        continue
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "LineString",
                            "coordinates": _round_coords(simplified.coords),
                        },
                        # No properties: the map draws these as anonymous
                        # lines, and street names across hundreds of thousands
                        # of segments would be a meaningful share of the data.
                        "properties": {},
                    })
                if not features:
                    continue

                key = f"{ix}_{iy}"
                path = out_dir / f"{key}.geojson"
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"type": "FeatureCollection", "features": features},
                              f, ensure_ascii=False, separators=(",", ":"))
                cells.append(key)
                total_features += len(features)
                total_bytes += path.stat().st_size

    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump({"cell_deg": cell_deg, "cells": cells}, f, separators=(",", ":"))

    largest = max((p.stat().st_size for p in out_dir.glob("*.geojson")), default=0)
    print(f"[INFO] {label} -> {out_dir} ({total_features} segments in {len(cells)} "
          f"cells, {total_bytes / 2**20:.1f} MB total, largest cell "
          f"{largest / 2**20:.2f} MB)")


def _is_cycleway(tags):
    return (
        tags.get("highway") == "cycleway"
        or "cycleway" in tags
        or "cycleway:left" in tags
        or "cycleway:right" in tags
    )


class _RouteRelationCollector(osmium.SimpleHandler):
    """
    Pass 1: collects every relation tagged route=bus|tram|trolleybus and the
    IDs of every WAY member it references. Only IDs are needed here --
    resolving coordinates requires the located second pass.
    """

    def __init__(self):
        super().__init__()
        self.wanted_way_ids = set()
        self.route_count = 0

    def relation(self, r):
        if r.tags.get("route") not in ROUTE_TYPES:
            return
        self.route_count += 1
        for m in r.members:
            if m.type == "w":
                self.wanted_way_ids.add(m.ref)


class _OverlayWayCollector(osmium.SimpleHandler):
    """
    Pass 2: resolves the geometry of every cycleway, and of every way used by
    a transit route from pass 1. A way can belong to both layers.

    Lines are stored as shapely LineStrings straight away: at national scale
    that is several hundred thousand ways, and a LineString holds its
    coordinates as packed doubles, several times smaller than the equivalent
    list of Python tuples.
    """

    def __init__(self, transit_way_ids):
        super().__init__()
        self.transit_way_ids = transit_way_ids
        self.cycleways = []
        self.transit = []

    def way(self, w):
        is_transit = w.id in self.transit_way_ids
        is_cycle = _is_cycleway(w.tags)
        if not (is_transit or is_cycle):
            return
        coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(coords) < 2:
            return
        line = LineString(coords)
        if is_cycle:
            self.cycleways.append(line)
        if is_transit:
            self.transit.append(line)


def build_map_layers():
    """Builds both overlay layers from the local .osm.pbf, in two passes."""
    pbf = require_pbf(config.OSM_PBF_PATH)
    area = get_combined_target_polygon()

    print(f"[INFO] Pass 1/2: reading bus/tram/trolleybus route relations from "
          f"{pbf.name}...")
    relations = _RouteRelationCollector()
    relations.apply_file(str(pbf))
    print(f"[INFO] Found {relations.route_count} route relations, referencing "
          f"{len(relations.wanted_way_ids)} unique road segments.")

    print("[INFO] Pass 2/2: resolving cycleway and route geometry "
          "(no internet/Overpass involved)...")
    ways = _OverlayWayCollector(relations.wanted_way_ids)
    apply_with_locations(ways, pbf)
    print(f"[INFO] Resolved {len(ways.cycleways)} cycleway-tagged ways and "
          f"{len(ways.transit)} route segments.")

    cycling = _clip_to_area(ways.cycleways, area, "Cycling lanes")
    transit = _clip_to_area(ways.transit, area, "Public transport")
    del ways   # release the unclipped copies before the per-cell work

    _write_chunked_layer(cycling, config.GEOJSON_CYCLING_DIR,
                         config.MAP_TILE_SIZE_DEG["cycling"], "Cycling lanes")
    _write_chunked_layer(transit, config.GEOJSON_TRANSIT_DIR,
                         config.MAP_TILE_SIZE_DEG["transit"], "Public transport",
                         merge_overlaps=True)
