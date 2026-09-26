"""
fetch_supermarkets.py
---------------------
Reads supermarket and minimarket POIs from the local OSM extract(s) listed in
config.OSM_PBF_PATHS, using the `osmium` library.

WHY A LOCAL FILE RATHER THAN THE OVERPASS API:
Free public Overpass mirrors return technically-successful but SILENTLY
INCOMPLETE responses under load -- real supermarkets simply absent from the
result, with no error to indicate anything went wrong. Silent incompleteness is
the worst possible failure for this analysis, because a missing shop turns a
well-served town into a false positive with nothing to warn you. Reading from a
local extract removes the network from this step entirely.

WHY SUPERMARKETS ARE CLIPPED TO THE STUDY AREA (config.BORDER_BUFFER_KM):
A town on the outer edge whose nearest shop lies just across the boundary must
still be able to see it, or it is wrongly reported as underserved -- so the clip
is the study area EXPANDED by the buffer, not the study area itself. (This does
not help when the shop lies outside the .osm.pbf extract altogether; for that,
add the neighbouring extract to config.OSM_PBF_PATHS.)

Clipping also keeps the spatial index scoped to the relevant area rather than
every POI in every loaded extract.
"""

from pathlib import Path

import numpy as np
import osmium
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from . import config
from .geo_utils import METRIC_CRS, WGS84
from .osm_reader import apply_with_locations

SHOP_TAGS = set(config.SUPERMARKET_SHOP_TYPES)


class _SupermarketHandler(osmium.SimpleHandler):
    """
    Collects every node or way tagged shop=supermarket / shop=convenience.

    Ways -- shops mapped as building outlines rather than single points -- get
    an approximate centre computed from their resolved node coordinates.
    `locations=True` in apply_file() is what makes those coordinates
    available.
    """

    def __init__(self):
        super().__init__()
        self.records = []
        self.geoms = []

    def node(self, n):
        shop = n.tags.get("shop")
        if shop in SHOP_TAGS:
            self.records.append({
                "name": n.tags.get("name", "Unnamed supermarket"),
                "shop_type": shop,
                "osm_id": n.id,
                "osm_type": "node",
            })
            self.geoms.append(Point(n.location.lon, n.location.lat))

    def way(self, w):
        shop = w.tags.get("shop")
        if shop not in SHOP_TAGS:
            return
        coords = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if not coords:
            return
        # Simple average of the outline's points -- well inside the precision
        # this analysis needs.
        avg_lon = sum(c[0] for c in coords) / len(coords)
        avg_lat = sum(c[1] for c in coords) / len(coords)
        self.records.append({
            "name": w.tags.get("name", "Unnamed supermarket"),
            "shop_type": shop,
            "osm_id": w.id,
            "osm_type": "way",
        })
        self.geoms.append(Point(avg_lon, avg_lat))


def _extract_from_file(pbf_path):
    """Extracts supermarket/minimarket POIs from a single local .osm.pbf file."""
    pbf_path = Path(pbf_path)
    print(f"[INFO] Reading {pbf_path} (no internet/Overpass involved)...")
    handler = _SupermarketHandler()

    # Node locations are needed for shops mapped as building outlines. Where
    # the index lives (RAM or disk) is config.OSMIUM_NODE_INDEX.
    apply_with_locations(handler, pbf_path)

    print(f"[INFO] Found {len(handler.records)} supermarket/minimarket entries "
          f"in {pbf_path.name}.")
    return gpd.GeoDataFrame(handler.records, geometry=handler.geoms, crs=WGS84)


def _clip_to_study_area(supermarkets_gdf, towns_gdf):
    """
    Keeps supermarkets within config.BORDER_BUFFER_KM of any town in the
    study area.

    Distances are measured in METRIC_CRS so the buffer is a true distance in
    metres, not a degree approximation that would be 25-44% too generous
    east-west across Italy.

    This is a nearest-town query per shop rather than "union every town,
    buffer the result, test each shop against it". The two are equivalent,
    but the union-and-buffer of ~8,000 comuni -- every island and coastline
    of Italy in one polygon -- is minutes of work and gigabytes of memory,
    whereas the spatial index answers each shop's question locally.

    Returns the input unchanged if no towns are supplied, which keeps
    fetch_supermarkets() usable standalone for a quick check.
    """
    if towns_gdf is None or towns_gdf.empty:
        print("[INFO] No towns supplied -- skipping border-buffer clip "
              "(keeping every supermarket found in the extract).")
        return supermarkets_gdf

    buffer_m = config.BORDER_BUFFER_KM * 1000
    print(f"[INFO] Clipping supermarkets to the study area + "
          f"{config.BORDER_BUFFER_KM}km border buffer...")

    towns_metric = towns_gdf.to_crs(METRIC_CRS)
    supermarkets_metric = supermarkets_gdf.to_crs(METRIC_CRS)

    # Pairs of (supermarket position, nearest town position); supermarkets
    # with no town within buffer_m are simply absent from the result.
    pairs = towns_metric.sindex.nearest(
        supermarkets_metric.geometry, return_all=False, max_distance=buffer_m
    )
    keep_positions = np.unique(pairs[0])
    if len(keep_positions) == 0:
        print("[WARN] The border-buffer clip matched ZERO supermarkets. That "
              "almost certainly means the towns and the extract cover "
              "different areas -- check OSM_EXTRACT_NAME against TARGET_LEVEL.")
        return supermarkets_gdf.iloc[0:0]

    clipped = supermarkets_gdf.iloc[keep_positions].copy()
    removed = len(supermarkets_gdf) - len(clipped)
    print(f"[INFO] Kept {len(clipped)} supermarkets within the buffered study "
          f"area ({removed} outside it were dropped).")
    return clipped.reset_index(drop=True)


def fetch_supermarkets(towns_gdf=None):
    """
    Returns a GeoDataFrame of every supermarket/minimarket relevant to the
    study area.

    towns_gdf : GeoDataFrame or None
        The towns under analysis. When supplied, supermarkets are clipped to
        their combined extent plus config.BORDER_BUFFER_KM. Pass None to skip
        clipping entirely.

    NOTE ON CACHING: the cache stores the CLIPPED result. Changing
    BORDER_BUFFER_KM therefore requires FORCE_REFRESH_CACHE = True, or
    deleting config.SUPERMARKETS_CACHE_PATH by hand -- otherwise the previous
    buffer silently persists.
    """
    if not config.FORCE_REFRESH_CACHE and config.SUPERMARKETS_CACHE_PATH.exists():
        print(f"[INFO] Loading supermarkets from cache: {config.SUPERMARKETS_CACHE_PATH}")
        print(f"[INFO] (Cached at BORDER_BUFFER_KM={config.BORDER_BUFFER_KM}. If you "
              f"have changed that value, set FORCE_REFRESH_CACHE = True.)")
        return gpd.read_file(config.SUPERMARKETS_CACHE_PATH)

    all_gdfs = [_extract_from_file(p) for p in config.OSM_PBF_PATHS]
    combined = gpd.GeoDataFrame(
        pd.concat(all_gdfs, ignore_index=True),
        geometry="geometry",
        crs=WGS84,
    )

    # Adjacent extracts overlap along their shared edge, so the same shop can
    # appear in two files. OSM IDs are globally unique, which makes this exact
    # rather than heuristic.
    before_dedup = len(combined)
    combined = combined.drop_duplicates(subset=["osm_id", "osm_type"]).reset_index(drop=True)
    if before_dedup != len(combined):
        print(f"[INFO] Removed {before_dedup - len(combined)} duplicate entries "
              f"appearing in more than one extract.")

    print(f"[INFO] Extracted {len(combined)} unique supermarkets/minimarkets "
          f"from {len(config.OSM_PBF_PATHS)} local file(s).")

    combined = _clip_to_study_area(combined, towns_gdf)

    config.SUPERMARKETS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    combined.to_file(config.SUPERMARKETS_CACHE_PATH, driver="GPKG")
    print(f"[INFO] Cached supermarkets to {config.SUPERMARKETS_CACHE_PATH}")

    return combined
