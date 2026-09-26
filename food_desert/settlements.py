"""
settlements.py
--------------
Builds the unit of analysis: one row per inhabited settlement (ISTAT
"localita' abitata"), with its outline, a point to route from, and population
figures.

--- WHY SETTLEMENTS, NOT COMUNI ----------------------------------------------
Comuni are administrative units, and their size varies by region more than by
anything to do with shops. A Tuscan comune averages 66 km2 and several
villages; a Piedmontese one 14 km2 and usually one. Asking whether a COMUNE
has a shop made Emilia-Romagna, Toscana and Puglia look almost fully served --
any village far from its main town was hidden inside a comune whose main town
has one -- while Piemonte, counting every village as a comune, looked like the
worst region in Italy by far.

--- THE SOURCE -----------------------------------------------------------------
ISTAT "Basi territoriali 2021", Localita_21.zip, read as downloaded: every
locality's polygon (EPSG:32632) with, among others,

    PRO_COM    comune code as an integer, 2021 boundaries
    LOC_ID     locality code, unique nationally
    TIPO_LOC   1 centro abitato, 2 nucleo abitato, 3 localita' produttiva,
               4 case sparse
    NOME       name
    CENTRO_CL  1 for the comune's main town (capoluogo)
    POP21      residents at the 2021 census

--- POPULATION -----------------------------------------------------------------
POP21 is scaled to the 2024 figures the rest of the analysis uses, comune by
comune: each settlement keeps its 2021 share of its comune's residents. Age is
not published per locality, so residents 65+ are estimated from the comune's
2024 share -- an estimate, and labelled as one in the outputs.

--- COMUNI MERGED SINCE 2021 ---------------------------------------------------
PRO_COM uses 2021 boundaries. Rather than maintain a table of every merger
since, each settlement is placed in today's comune by where it lies: its
point is joined to the comune outlines already built from OpenStreetMap.
"""

import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pyogrio
from shapely.geometry import Point

from . import config
from .geo_utils import METRIC_CRS, WGS84

# What each column of the cache holds, in order.
COLUMNS = ["settlement_id", "name", "comune", "istat_code", "province",
           "is_main_town", "population", "population_65plus", "pct_65plus",
           "aging_index"]

# Outlines are simplified to this tolerance in metres: ample for a test at
# SETTLEMENT_SHOP_BUFFER_M, and it keeps the cache a manageable size.
SIMPLIFY_M = 5


def _read_localities(zip_path):
    """
    Returns (settlements, totals): the analysed settlement polygons, and every
    locality's 2021 population by PRO_COM -- all types, since scaling needs
    each comune's full 2021 total, scattered houses included.

    The shapefile is extracted to a temporary folder first: the folder inside
    the .zip is named "Località_21", and how a non-ASCII member name decodes
    differs between zip tools, so it is found by extension, not by name.
    """
    with zipfile.ZipFile(zip_path) as zf, tempfile.TemporaryDirectory() as tmp:
        members = [m for m in zf.namelist()
                   if Path(m).suffix.lower() in (".shp", ".shx", ".dbf", ".prj", ".cpg")]
        if not any(m.lower().endswith(".shp") for m in members):
            raise RuntimeError(f"No shapefile found inside {zip_path.name}.")
        for member in members:
            (Path(tmp) / Path(member).name).write_bytes(zf.read(member))
        shp = next(Path(tmp).glob("*.shp"))

        totals = pyogrio.read_dataframe(shp, columns=["PRO_COM", "POP21"],
                                        read_geometry=False)
        types = ", ".join(str(t) for t in config.SETTLEMENT_TYPES)
        settlements = pyogrio.read_dataframe(
            shp, columns=["PRO_COM", "LOC_ID", "TIPO_LOC", "NOME", "CENTRO_CL", "POP21"],
            where=f"TIPO_LOC IN ({types})",
        )
    return settlements, totals


def _scale_factors(settlements, totals, towns):
    """
    Returns (factors, overall): {istat_code: 2024 population / 2021
    population} per current comune, and the same ratio for the study area as
    a whole.

    The 2021 total of a current comune is the sum over every 2021 PRO_COM
    whose settlements mostly fall inside it -- which folds merged comuni
    together without a table of mergers.

    `overall` is taken over the comuni that have both figures, never over
    the whole file: the localities file covers all of Italy, so dividing the
    study area's 2024 population by the national 2021 total would shrink
    every fallback population in a regional run.
    """
    majority = (settlements.groupby("PRO_COM")["istat_code"]
                .agg(lambda codes: codes.mode().iloc[0]))
    pop21 = (totals.assign(istat_code=totals["PRO_COM"].map(majority))
             .groupby("istat_code")["POP21"].sum())
    pop24 = towns.set_index("istat_code")["population"].astype("Float64")
    factors = (pop24 / pop21.where(pop21 > 0)).to_dict()

    paired = pop24[pop24.index.isin(pop21.index)].dropna()
    overall = float(paired.sum()) / float(pop21[paired.index].sum())
    return factors, overall


def _build(towns):
    zip_path = config.ISTAT_LOCALITIES
    if not zip_path.exists():
        raise RuntimeError(
            f"{zip_path.name} not found in {zip_path.parent}. It holds ISTAT's "
            f"settlement outlines -- run `python tools/download_istat.py`."
        )

    print(f"[INFO] Reading settlements (TIPO_LOC {config.SETTLEMENT_TYPES}) from "
          f"{zip_path.name}...")
    raw, totals = _read_localities(zip_path)
    raw = raw.to_crs(METRIC_CRS)
    raw = raw[raw["POP21"] > 0].copy()   # uninhabited in 2021: no one to serve

    # A point to route from, guaranteed inside the outline.
    raw["center"] = raw.geometry.representative_point()

    # Place each settlement in today's comune by where its point lies.
    comuni = towns[["istat_code", "name", "province", "population",
                    "population_65plus", "pct_65plus", "aging_index"]].copy()
    comuni = comuni.rename(columns={"name": "comune"})
    comuni = gpd.GeoDataFrame(comuni, geometry=towns.geometry.values,
                              crs=towns.crs).to_crs(METRIC_CRS)
    points = gpd.GeoDataFrame(raw.drop(columns="geometry"),
                              geometry=raw["center"], crs=METRIC_CRS)
    joined = gpd.sjoin(points, comuni, how="inner", predicate="within")
    outside = len(points) - joined.index.nunique()
    joined = joined[~joined.index.duplicated(keep="first")]
    raw = raw.loc[joined.index]
    for column in ("istat_code", "comune", "province", "pct_65plus", "aging_index"):
        raw[column] = joined[column]

    # A comune can end up with no 2021 total: when its only settlement is
    # filed by ISTAT under a neighbouring comune's PRO_COM, but lies inside
    # this one's boundary today. Those settlements take the study area's
    # 2021-to-2024 change instead of being left without a population.
    factors, overall = _scale_factors(raw, totals, towns)
    factor = raw["istat_code"].map(factors).astype("Float64").fillna(overall)
    raw["population"] = (raw["POP21"] * factor).round().astype("Int64")
    raw["population_65plus"] = (
        raw["population"].astype("Float64") * raw["pct_65plus"].astype("Float64") / 100
    ).round().astype("Int64")

    raw["settlement_id"] = raw["LOC_ID"].astype("int64").astype(str)
    raw["name"] = raw["NOME"].astype(str).str.strip()
    raw["is_main_town"] = raw["CENTRO_CL"] == 1

    geometry = raw.geometry.simplify(SIMPLIFY_M, preserve_topology=True)
    gdf = gpd.GeoDataFrame(raw[COLUMNS], geometry=geometry.to_crs(WGS84).values, crs=WGS84)
    gdf["center_point"] = gpd.GeoSeries(raw["center"], crs=METRIC_CRS).to_crs(WGS84).values

    print(f"[INFO] {len(gdf):,} settlements in the study area, home to "
          f"{int(gdf['population'].sum()):,} residents (2021 shares scaled to 2024)."
          + (f" {outside:,} lie outside it and were skipped." if outside else ""))
    return gdf


def build_settlements(towns):
    """
    The settlements of every comune in `towns`, with population figures.
    Columns: COLUMNS, geometry (outline, WGS84), center_point (Point, WGS84).

    Cached to config.SETTLEMENTS_CACHE_PATH; config.FORCE_REFRESH_CACHE or
    deleting the file rebuilds it.
    """
    path = config.SETTLEMENTS_CACHE_PATH
    if not config.FORCE_REFRESH_CACHE and path.exists():
        print(f"[INFO] Loading settlements from cache: {path}")
        gdf = gpd.read_file(path)
        gdf["center_point"] = [Point(xy) for xy in zip(gdf["center_lon"], gdf["center_lat"])]
        return gdf.drop(columns=["center_lon", "center_lat"])

    gdf = _build(towns)
    cache = gdf.drop(columns="center_point")
    cache["center_lon"] = gdf["center_point"].map(lambda p: p.x)
    cache["center_lat"] = gdf["center_point"].map(lambda p: p.y)
    path.parent.mkdir(parents=True, exist_ok=True)
    cache.to_file(path, driver="GPKG")
    print(f"[INFO] Cached settlements to {path}")
    return gdf
