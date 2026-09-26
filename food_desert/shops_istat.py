"""
shops_istat.py
--------------
Reads ISTAT's register of business locations (ASIA unita' locali) and places
the stores OpenStreetMap has not mapped, so the analysis can route to them
like any other shop (see infer_register_stores).

--- WHY ---------------------------------------------------------------------
The "has its own shop" test trusts OSM to be complete, and nationally it is
not: Turi (13,000 residents), Poggiomarino (22,600) and Naro have no
supermarket in OSM, while ISTAT's register lists 34 non-specialised stores in
Poggiomarino alone.

The register only counts stores per comune. Simply dropping every comune with
a registered store would make the results two analyses in one: OSM locations
where OSM is complete, comune-level counts where it is not. Placing the
missing stores in settlements puts both on the same footing.

--- THE FILE ----------------------------------------------------------------
An SDMX CSV exactly as served by esploradati.istat.it (dataflow
183_285_DF_DICA_ASIAULP_3, "Settori economici (Ateco 3 cifre) - com."),
fetched by tools/download_istat.py. Columns are used by NAME -- SDMX CSV
headers are part of the standard, unlike the workbooks' positional layout:

    REF_AREA                  six-digit ISTAT comune code (also regions and
                              provinces, which are skipped)
    ECON_ACTIVITY_NACE_2007   ATECO sector: 471 or 472
    OBS_VALUE                 number of local units

A comune with no row has no local unit in either sector: ISTAT omits zero
rows rather than printing them.
"""

import geopandas as gpd
import pandas as pd

from . import config
from .geo_utils import METRIC_CRS, WGS84

# The shop_type given to stores placed from the register, so the outputs can
# always tell an inferred location from a mapped one.
INFERRED = "istat_inferred"

SECTOR_GENERAL_STORE = "471"   # supermarkets, discounters, minimarkets, grocers
SECTOR_FOOD_SPECIALIST = "472"  # bakers, butchers, greengrocers...


def load_shop_register():
    """
    Returns {istat_code: (general_stores, food_specialists)}, or None if the
    register file is absent (the check is then skipped, loudly).

    Comuni merged since the register's reference year are folded together
    with config.COMUNE_MERGERS, as the population table is -- otherwise a
    merged comune would read as having no shops at all.
    """
    path = config.ISTAT_SHOP_REGISTER
    if not path.exists():
        print(f"[WARN] {path.name} not found -- towns will NOT be checked against "
              f"ISTAT's shop register, so OSM's gaps go uncorrected. Run "
              f"`python tools/download_istat.py`.")
        return None

    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    raw = raw[raw["REF_AREA"].str.fullmatch(r"\d{6}")]
    raw["OBS_VALUE"] = pd.to_numeric(raw["OBS_VALUE"], errors="coerce").fillna(0)

    counts = (raw.pivot_table(index="REF_AREA", columns="ECON_ACTIVITY_NACE_2007",
                              values="OBS_VALUE", aggfunc="sum")
              .reindex(columns=[SECTOR_GENERAL_STORE, SECTOR_FOOD_SPECIALIST])
              .fillna(0).astype(int))

    for successor, (_, predecessors) in config.COMUNE_MERGERS.items():
        present = [code for code in predecessors if code in counts.index]
        if present and present != [successor]:
            counts.loc[successor] = counts.loc[present].sum()

    years = sorted(raw["TIME_PERIOD"].unique()) if "TIME_PERIOD" in raw else []
    print(f"[INFO] ISTAT shop register ({', '.join(years) or 'year unknown'}): "
          f"{len(counts)} comuni, {int(counts[SECTOR_GENERAL_STORE].sum()):,} "
          f"general food stores, {int(counts[SECTOR_FOOD_SPECIALIST].sum()):,} "
          f"specialist food shops.")
    return {code: (int(row[SECTOR_GENERAL_STORE]), int(row[SECTOR_FOOD_SPECIALIST]))
            for code, row in counts.iterrows()}


def shops_by_settlement(settlements_metric, shops_metric):
    """
    The number of shops inside, or within config.SETTLEMENT_SHOP_BUFFER_M of,
    each settlement's outline, as a Series aligned to settlements_metric.
    """
    zones = settlements_metric.geometry.buffer(config.SETTLEMENT_SHOP_BUFFER_M)
    hits = gpd.sjoin(gpd.GeoDataFrame(geometry=shops_metric.geometry.values, crs=METRIC_CRS),
                     gpd.GeoDataFrame(geometry=zones.values, crs=METRIC_CRS),
                     predicate="within")
    counts = hits.groupby("index_right").size()
    return pd.Series(counts.reindex(range(len(settlements_metric)), fill_value=0).to_numpy(),
                     index=settlements_metric.index)


def infer_register_stores(settlements, towns, shops, register):
    """
    Places the register's stores that OSM has not mapped, and returns them as
    shop rows (shop_type INFERRED, at the chosen settlement's centre point)
    plus an audit list.

    For each comune the register lists more 471 stores than OSM maps inside
    its boundary. The difference goes to its settlements that have no OSM
    shop: main town first, then by population, one store each. A settlement
    other than the main town must also expect a store -- the register count
    times its share of the comune's residents at least
    config.INFERRED_STORE_MIN_SHARE -- or the unmapped stores of a town
    would be spread across every hamlet around it.
    """
    empty = gpd.GeoDataFrame(columns=list(shops.columns.drop("geometry")),
                             geometry=[], crs=WGS84)
    if register is None:
        return empty, []

    shops_metric = shops.to_crs(METRIC_CRS)
    comuni_metric = towns[["istat_code", "population"]].set_geometry(
        towns.geometry.to_crs(METRIC_CRS))
    osm_per_comune = (gpd.sjoin(shops_metric[["geometry"]], comuni_metric, predicate="within")
                      .groupby("istat_code").size())

    settlements = settlements.copy()
    settlements["osm_shops"] = shops_by_settlement(settlements.to_crs(METRIC_CRS), shops_metric)
    comune_pop = towns.set_index("istat_code")["population"].astype("Float64")

    rows, audit = [], []
    for code, group in settlements.groupby("istat_code"):
        registered = register.get(code, (0, 0))[0]
        mapped = int(osm_per_comune.get(code, 0))
        missing = registered - mapped
        if missing <= 0:
            continue

        candidates = group[group["osm_shops"] == 0].sort_values(
            ["is_main_town", "population"], ascending=False)
        for _, s in candidates.iterrows():
            if missing <= 0:
                break
            population = float(s["population"]) if pd.notna(s["population"]) else 0.0
            total = comune_pop.get(code)
            share = population / float(total) if pd.notna(total) and total > 0 else 0.0
            expected = registered * share
            if not s["is_main_town"] and expected < config.INFERRED_STORE_MIN_SHARE:
                break   # sorted by population: nobody further down qualifies either
            rows.append({
                "name": f"Store in ISTAT's register, {s['name']} (location inferred)",
                "shop_type": INFERRED,
                "osm_id": None,
                "osm_type": None,
                "geometry": s["center_point"],
            })
            audit.append({
                "settlement": s["name"], "settlement_id": s["settlement_id"],
                "comune": s["comune"], "istat_code": code,
                "is_main_town": bool(s["is_main_town"]),
                "population": int(s["population"]) if pd.notna(s["population"]) else None,
                "register_stores": registered, "osm_shops_in_comune": mapped,
            })
            missing -= 1

    inferred = gpd.GeoDataFrame(rows, geometry="geometry", crs=WGS84) if rows else empty
    comuni = len({a["istat_code"] for a in audit})
    main = sum(a["is_main_town"] for a in audit)
    print(f"[INFO] Placed {len(inferred):,} stores from ISTAT's register that OSM has "
          f"not mapped, in {comuni:,} comuni: {main:,} in a main town, "
          f"{len(inferred) - main:,} in another settlement.")
    return inferred, audit
