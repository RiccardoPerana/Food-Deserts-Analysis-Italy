"""
population_istat.py
-------------------
Reads official ISTAT demographic data and matches it to towns by ISTAT code,
falling back to the name only where OSM carries no usable code.

--- HOW IT READS THE SOURCE DATA ---------------------------------------------
ISTAT's published .xlsx workbooks are read unmodified, exactly as downloaded.
No manual conversion step sits between the published data and the analysis, so
anyone can download the same files and regenerate the same numbers -- which is
what makes the demographic half of this project verifiable.

Three sheets are used:

  Tavola A1  header row 3, data from row 4
             A = province, B = ISTAT code, C = town name,
             J = total resident population at 31 December

  Tavola A3  header rows 3-4, data from row 5
             Five-year age brackets in columns E..Y, total in Z.
             Columns R..Y are 65-69, 70-74, 75-79, 80-84, 85-89, 90-94,
             95-99 and 100+ -- summed to give the 65+ population.

  Tavola A4  header rows 3-4, data from row 5
             F = "Indice di vecchiaia" (aging index) for 2024. This is
             ISTAT's own ratio of over-65s to under-15s, per 100. A value of
             272 means 272 residents aged 65+ for every 100 aged under 15.

The three sheets are joined on CODICE COMUNE (column B), a unique and stable
ISTAT identifier, rather than on town names. Names are not unique: Italy has
several distinct municipalities sharing one name across different provinces,
and more still that collide once accents and punctuation are normalised away
(there are two separate "San Gregorio nelle Alpi"). A name-keyed join has to
silently discard one of each pair. Codes do not collide.

--- MATCHING AGAINST OPENSTREETMAP -------------------------------------------
Italian comuni in OSM carry the same code as a `ref:ISTAT` tag, so towns are
matched on the code first. Names are only a fallback, for relations whose tag
is missing or stale, and the fallback refuses to guess: a name shared by two
or more still-unmatched ISTAT rows is left unmatched rather than assigned to
whichever row happened to be read first -- otherwise one town could be handed
another town's population.

NOTE ON TRENTINO-ALTO ADIGE: ISTAT publishes this region as two separate
workbooks, one per autonomous province (Trento and Bolzano/Bozen). Both are
listed in config.ISTAT_POPULATION_XLSX and are combined automatically.

NOTE ON LIGURIA: ISTAT's published Liguria workbook has no per-comune tables.
Its comuni are filled from POSAS, the same census count published by
demo.istat.it -- see _load_posas_fallback().
"""

import re
import unicodedata
from collections import Counter

import pandas as pd

from . import config

# --- Sheet layout (rows numbered as they appear in Excel, 1-indexed) -------
SHEET_POPULATION = "Tavola A1"
SHEET_AGE = "Tavola A3"
SHEET_INDICATORS = "Tavola A4"

POPULATION_FIRST_DATA_ROW = 4
AGE_FIRST_DATA_ROW = 5
INDICATORS_FIRST_DATA_ROW = 5

# --- Column positions (0-indexed, as pandas sees them) --------------------
COL_PROVINCE = 0            # A
COL_ISTAT_CODE = 1          # B
COL_NAME = 2                # C
COL_TOTAL_POPULATION = 9    # J, in Tavola A1

# Tavola A3: R..Y inclusive are the 65+ five-year brackets; Z is the total.
COL_AGE_65_START = 17       # R  (65-69)
COL_AGE_65_END = 24         # Y  (100 e piu), inclusive
COL_AGE_TOTAL = 25          # Z

COL_AGING_INDEX = 5         # F, in Tavola A4 (the 2024 column)


def _normalize_name(name):
    """
    Normalises town names for reliable joining: strips accents, casing and
    punctuation. "Arsie" and "Arsie'" both reduce to the same key.
    """
    if not isinstance(name, str):
        # Blank or NaN cells (stray footer rows) arrive as float.
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("utf-8")
    return re.sub(r"[^a-z0-9]", "", name.lower().strip())


def _read_sheet(path, sheet_name, first_data_row):
    """
    Reads one sheet with no header interpretation and trims it to the real data.

    ISTAT workbooks carry explanatory footers below the data (notes on
    statistical adjustment), and Excel reports a max_row far past the last
    populated cell because of trailing formatting. Both are handled by keeping
    only rows whose column B holds a valid ISTAT code.

    keep_default_na=False is not optional. By default pandas reads the TEXT
    "None" as a missing value -- and None is a real comune, in the province of
    Turin, which would otherwise read as a blank row.
    """
    df = pd.read_excel(path, sheet_name=sheet_name, header=None,
                       skiprows=first_data_row - 1, engine="openpyxl",
                       keep_default_na=False, na_values=[""])

    is_town = df.iloc[:, COL_ISTAT_CODE].map(_clean_code).notna()
    return df[is_town].reset_index(drop=True)


def _clean_code(value):
    """
    ISTAT codes are zero-padded six-character strings ("025001"). Excel
    sometimes returns them as integers, dropping the leading zero, so they are
    normalised to one consistent form.
    """
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6) if text.isdigit() else None


def _load_single_workbook(path):
    """
    Loads one ISTAT regional workbook, returning one row per town.

    Columns: istat_code, name, province, population, population_65plus,
             pct_65plus, aging_index
    """
    pop_df = _read_sheet(path, SHEET_POPULATION, POPULATION_FIRST_DATA_ROW)
    age_df = _read_sheet(path, SHEET_AGE, AGE_FIRST_DATA_ROW)
    ind_df = _read_sheet(path, SHEET_INDICATORS, INDICATORS_FIRST_DATA_ROW)

    base = pd.DataFrame({
        "istat_code": pop_df.iloc[:, COL_ISTAT_CODE].map(_clean_code),
        "name": pop_df.iloc[:, COL_NAME].astype(str).str.strip(),
        "province": pop_df.iloc[:, COL_PROVINCE].astype(str).str.strip(),
        "population": pd.to_numeric(
            pop_df.iloc[:, COL_TOTAL_POPULATION], errors="coerce"
        ).astype("Int64"),
    })

    # --- 65+ population, summed across the eight senior brackets ----------
    senior = age_df.iloc[:, COL_AGE_65_START:COL_AGE_65_END + 1]
    senior = senior.apply(pd.to_numeric, errors="coerce")
    age = pd.DataFrame({
        "istat_code": age_df.iloc[:, COL_ISTAT_CODE].map(_clean_code),
        "population_65plus": senior.sum(axis=1, min_count=1).astype("Int64"),
        "_age_sheet_total": pd.to_numeric(
            age_df.iloc[:, COL_AGE_TOTAL], errors="coerce"
        ).astype("Int64"),
    })

    indicators = pd.DataFrame({
        "istat_code": ind_df.iloc[:, COL_ISTAT_CODE].map(_clean_code),
        "aging_index": pd.to_numeric(ind_df.iloc[:, COL_AGING_INDEX], errors="coerce"),
    })

    # Joined on the ISTAT code, not the name -- see the module docstring.
    df = base.merge(age, on="istat_code", how="left")
    df = df.merge(indicators, on="istat_code", how="left")

    # --- Cross-check the two sheets against each other -------------------
    # Tavola A1's total and Tavola A3's total describe the same quantity from
    # different tables. A mismatch means the column positions assumed above no
    # longer hold -- exactly the kind of breakage that would otherwise produce
    # plausible-looking but wrong numbers.
    both = df["population"].notna() & df["_age_sheet_total"].notna()
    mismatched = int((df.loc[both, "population"] != df.loc[both, "_age_sheet_total"]).sum())
    if mismatched:
        print(f"[WARN] {mismatched} rows in {path.name}: the total in "
              f"'{SHEET_POPULATION}' disagrees with the age-table total in "
              f"'{SHEET_AGE}'. The sheet layout may have changed -- verify the "
              f"column positions at the top of population_istat.py.")
    df = df.drop(columns=["_age_sheet_total"])

    df["pct_65plus"] = (
        df["population_65plus"].astype("Float64")
        / df["population"].astype("Float64") * 100
    ).round(1)

    print(f"[INFO] Loaded {len(df)} towns from {path.name} "
          f"(median {df['pct_65plus'].median():.1f}% aged 65+).")
    return df


def _read_posas(path):
    """
    Reads a POSAS file exactly as downloaded from demo.istat.it -- a .zip
    holding one semicolon-separated CSV, whose first line is a title rather
    than the header. Columns are used by POSITION, as with the workbooks:

        0 = code (comune or province), 1 = name, 2 = age, last = "Totale"

    Age runs 0..100 (100 meaning "100 and over"), plus a 999 row per area
    holding its total.
    """
    raw = pd.read_csv(path, sep=";", skiprows=1, encoding="utf-8-sig",
                      dtype=str, keep_default_na=False)
    return pd.DataFrame({
        "code": raw.iloc[:, 0].str.strip(),
        "name": raw.iloc[:, 1].str.strip(),
        "age": pd.to_numeric(raw.iloc[:, 2], errors="coerce"),
        "n": pd.to_numeric(raw.iloc[:, -1], errors="coerce"),
    })


def _load_posas_fallback(exclude_codes):
    """
    Returns rows, in the workbook table's shape, for every comune in
    config.ISTAT_POSAS_COMUNI that NO workbook covered -- or None if the file
    is absent or covers nothing new.

    WHY: ISTAT's published Liguria "Allegato statistico" contains no
    per-comune tables at all (its sheets are province-level summaries), so the
    workbooks alone leave 234 comuni without data. POSAS is the same census
    count in another format -- population at 1 January 2025, i.e. the census
    figure at 31 December 2024 -- and matched the workbooks exactly, comune for
    comune, in population and 65+ when checked across all 7,358 they share.

    The aging index is computed as ISTAT defines it (65+ per 100 aged 0-14).
    Where a comune has no children it is left MISSING: the workbooks print 0.0
    there, which reads as "very young", and the arithmetic gives infinity,
    which is not valid JSON and would blank the web map.
    """
    path = config.ISTAT_POSAS_COMUNI
    if not path.exists():
        return None

    raw = _read_posas(path)
    raw["istat_code"] = raw["code"].map(_clean_code)
    raw = raw[raw["istat_code"].notna() & ~raw["istat_code"].isin(exclude_codes)]
    if raw.empty:
        return None

    by_code = raw.groupby("istat_code")
    total = raw[raw["age"] == 999].set_index("istat_code")["n"]
    summed = raw[raw["age"] <= 100].groupby("istat_code")["n"].sum()
    senior = raw[raw["age"].between(65, 100)].groupby("istat_code")["n"].sum()
    children = raw[raw["age"] <= 14].groupby("istat_code")["n"].sum()

    df = pd.DataFrame({
        "name": by_code["name"].first(),
        "population": total.astype("Int64"),
        "population_65plus": senior.astype("Int64"),
        "aging_index": (senior / children.where(children > 0) * 100),
    }).rename_axis("istat_code").reset_index()

    # Same cross-check as the workbooks: the per-age rows must add up to the
    # published total, or the column positions above no longer hold.
    mismatched = int((summed.reindex(total.index) != total).sum())
    if mismatched:
        print(f"[WARN] {mismatched} comuni in {path.name}: the ages do not sum to "
              f"the published total. The file layout may have changed -- verify "
              f"the column positions in _read_posas().")

    provinces = {}
    if config.ISTAT_POSAS_PROVINCE.exists():
        prov = _read_posas(config.ISTAT_POSAS_PROVINCE)
        provinces = dict(zip(prov["code"].str.zfill(3), prov["name"]))
    else:
        print(f"[WARN] {config.ISTAT_POSAS_PROVINCE.name} not found -- the "
              f"{len(df)} comuni filled from {path.name} will have no province "
              f"name. Run `python tools/download_istat.py`.")
    df["province"] = df["istat_code"].str[:3].map(provinces)

    df["pct_65plus"] = (
        df["population_65plus"].astype("Float64")
        / df["population"].astype("Float64") * 100
    ).round(1)

    filled = df["province"].fillna("unknown province").value_counts()
    print(f"[INFO] Filled {len(df)} comuni that no workbook covered from {path.name}: "
          + ", ".join(f"{name} ({n})" for name, n in filled.items()))
    return df[["istat_code", "name", "province", "population",
               "population_65plus", "aging_index", "pct_65plus"]]


def _is_comune_annex(path):
    """True if the workbook has the per-comune sheets this module reads."""
    return SHEET_POPULATION in pd.ExcelFile(path, engine="openpyxl").sheet_names


def _apply_mergers(df):
    """
    Folds the predecessors of each merged comune in config.COMUNE_MERGERS into
    one row under the successor's code, so OSM's already-merged boundary
    finds its population.

    Counts are summed. The aging index is a ratio, so it is rebuilt from the
    under-15 counts it implies (65+ x 100 / index) rather than averaged; it is
    left missing if any predecessor lacks one.

    A merger whose predecessors are not all in the table is skipped: either
    data/istat/ does not cover that region, or the files are from after the
    merger and already list the successor.
    """
    for successor, (name, predecessors) in config.COMUNE_MERGERS.items():
        parts = df[df["istat_code"].isin(predecessors)]
        if len(parts) != len(predecessors):
            continue

        # The workbooks print 0.0 for a comune with no children -- no ratio to
        # invert, so it counts as missing (see _load_posas_fallback).
        index = parts["aging_index"].astype("Float64")
        under_15 = parts["population_65plus"].astype("Float64") * 100 / index.where(index > 0)
        merged = {
            "istat_code": successor,
            "name": name,
            "province": parts["province"].iloc[0],
            "population": parts["population"].sum(),
            "population_65plus": parts["population_65plus"].sum(),
            "aging_index": (parts["population_65plus"].sum() / under_15.sum() * 100
                            if under_15.notna().all() else pd.NA),
        }
        merged["pct_65plus"] = round(merged["population_65plus"] / merged["population"] * 100, 1)

        df = pd.concat([df[~df["istat_code"].isin(predecessors)], pd.DataFrame([merged])],
                       ignore_index=True)
        print(f"[INFO] Merged {' + '.join(parts['name'])} into {name} ({successor}): "
              f"{merged['population']:,} residents.")
    return df


def load_population_table():
    """
    Loads and combines every workbook in config.ISTAT_POPULATION_XLSX, then
    fills any comune they do not cover from the POSAS file, if present.
    """
    all_dfs = []
    for path in config.ISTAT_POPULATION_XLSX:
        try:
            if not _is_comune_annex(path):
                print(f"[INFO] Skipping {path.name}: it has no '{SHEET_POPULATION}' "
                      f"sheet, so it is not a per-comune annex. (ISTAT's published "
                      f"Liguria file is like this; its comuni come from POSAS.)")
                continue
            all_dfs.append(_load_single_workbook(path))
        except FileNotFoundError:
            print(f"[WARN] ISTAT workbook not found, skipping: {path}")
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {type(e).__name__}: {e}")

    workbook_codes = (set(pd.concat(all_dfs)["istat_code"].dropna())
                      if all_dfs else set())
    posas_df = _load_posas_fallback(workbook_codes)
    sources = len(all_dfs)
    if posas_df is not None:
        all_dfs.append(posas_df)

    if not all_dfs:
        raise RuntimeError(
            "No ISTAT data could be loaded. Check that data/istat/ holds the "
            "regional workbooks and/or the POSAS file -- "
            "`python tools/download_istat.py` fetches both. "
            "Run `python run.py paths` to see where they are expected."
        )

    df = pd.concat(all_dfs, ignore_index=True)

    # Duplicate ISTAT codes across workbooks would mean one town appearing
    # twice, which should never happen -- the regional files partition the
    # country. Surfaced loudly if it ever does.
    dup_codes = df["istat_code"].duplicated(keep=False)
    if dup_codes.any():
        print(f"[WARN] {int(dup_codes.sum())} rows share an ISTAT code across "
              f"workbooks: {sorted(df.loc[dup_codes, 'name'].tolist())}. "
              f"Keeping the first of each.")
        df = df.drop_duplicates(subset="istat_code", keep="first")

    df = _apply_mergers(df)
    df["_join_key"] = df["name"].apply(_normalize_name)

    total_pop = int(df["population"].sum())
    total_65 = int(df["population_65plus"].sum())
    posas_note = f" + {len(posas_df)} from POSAS" if posas_df is not None else ""
    print(f"[INFO] Combined ISTAT table: {len(df)} towns ({sources} workbook(s)"
          f"{posas_note}). Total population {total_pop:,}, of whom "
          f"{total_65:,} ({100 * total_65 / total_pop:.1f}%) are 65 or over.")
    return df


def _generate_name_candidates(name):
    """
    Generates candidate names to try when matching OSM names against ISTAT.

    Bilingual names are common in Trentino-Alto Adige (German - Italian, e.g.
    "Meran - Merano"; the language order is NOT consistent -- "Bolzano - Bozen"
    is Italian-first) and in Friuli-Venezia Giulia (Italian / Friulian or
    Slovenian, e.g. "Udine / Udin"). ISTAT records only the plain Italian name,
    while OSM's `name` tag often combines both languages into one string, so
    every segment is tried rather than just one.
    """
    candidates = [name]

    for sep in [" - ", "-", " / ", "/"]:
        if sep in name:
            candidates.extend(part.strip() for part in name.split(sep))

    # ISTAT and OSM disagree about Italian connective particles in place names.
    # OSM records "Vodo di Cadore"; ISTAT records "Vodo Cadore". Since
    # _normalize_name() strips punctuation but not words, those reduce to
    # "vododicadore" and "vodocadore" -- close, but not equal, so the town was
    # dropped as unmatched despite being a real municipality.
    #
    # Removing these particles is safe because they are never a whole name and
    # never the distinguishing part of one: no two Italian municipalities
    # differ only by a "di" or a "sul".
    particles = {"di", "in", "sul", "sulla", "sui", "del", "della", "dei",
                 "delle", "dello", "da", "al", "alla", "a", "d"}
    for base in list(candidates):
        words = [w for w in base.split() if w.lower().strip("'") not in particles]
        if words and len(words) != len(base.split()):
            candidates.append(" ".join(words))

    # De-duplicated, order preserved, so the exact name is always tried first.
    return list(dict.fromkeys(candidates))


def _preview(names, limit=30):
    """A bounded list for log lines -- a national run can have hundreds."""
    names = sorted(names)
    return f"{names[:limit]}{f' ... (+{len(names) - limit} more)' if len(names) > limit else ''}"


def _match_towns(towns_gdf, pop_df):
    """
    Returns (codes, by_code, by_name, ambiguous): the ISTAT code matched to
    each town (None where unmatched), the match counts, and the names that
    were left unmatched because they were ambiguous.

    Each ISTAT row is claimed by at most one town.
    """
    known_codes = set(pop_df["istat_code"].dropna())
    osm_codes = (towns_gdf["istat_ref"] if "istat_ref" in towns_gdf
                 else pd.Series([None] * len(towns_gdf)))

    codes = [None] * len(towns_gdf)
    claimed = set()

    # --- 1. By the ISTAT code carried in OSM (ref:ISTAT) --------------------
    for pos, raw in enumerate(osm_codes):
        code = _clean_code(raw)
        if code in known_codes and code not in claimed:
            codes[pos] = code
            claimed.add(code)
    by_code = len(claimed)

    # --- 2. By name, among ISTAT rows nobody has claimed yet ---------------
    # Restricting to unclaimed rows both prevents a second town from taking a
    # row already matched by code, and resolves many collisions outright: if
    # one of two same-named comuni was matched by code, the other is no
    # longer ambiguous.
    remaining = pop_df[~pop_df["istat_code"].isin(claimed)]
    key_counts = remaining["_join_key"].value_counts()
    unique = remaining[remaining["_join_key"].map(key_counts) == 1]
    code_by_name = dict(zip(unique["_join_key"], unique["istat_code"]))
    ambiguous_keys = set(key_counts.index[key_counts > 1])

    ambiguous = []
    for pos, name in enumerate(towns_gdf["name"]):
        if codes[pos] is not None:
            continue
        hit_ambiguous = False
        for candidate in _generate_name_candidates(name):
            key = _normalize_name(candidate)
            code = code_by_name.get(key)
            if code is not None and code not in claimed:
                codes[pos] = code
                claimed.add(code)
                break
            hit_ambiguous = hit_ambiguous or key in ambiguous_keys
        if codes[pos] is None and hit_ambiguous:
            ambiguous.append(name)

    return codes, by_code, len(claimed) - by_code, ambiguous


def _report_coverage(towns_gdf, codes, pop_df):
    """
    Explains the unmatched towns in terms that point at a fix.

    A town whose OSM ISTAT code appears in NO loaded workbook is the signature
    of a missing workbook when it happens province-wide: every comune of that
    region fails the same way. The first three digits of an ISTAT code are its
    province code, so grouping by them makes a missing region obvious rather
    than burying it in a list of hundreds of names.
    """
    known_codes = set(pop_df["istat_code"].dropna())
    orphan_prefixes = Counter()
    if "istat_ref" in towns_gdf:
        for raw, matched in zip(towns_gdf["istat_ref"], codes):
            code = _clean_code(raw)
            if matched is None and code and code not in known_codes:
                orphan_prefixes[code[:3]] += 1

    if orphan_prefixes:
        worst = ", ".join(f"{prefix} ({n})" for prefix, n in orphan_prefixes.most_common(12))
        print(f"[WARN] {sum(orphan_prefixes.values())} towns carry an ISTAT code "
              f"that appears in no loaded workbook. By province code: {worst}.")
        print("       Dozens under one province code almost always mean that "
              "region's workbook is missing from data/istat/ -- run "
              "`python run.py istat`. One or two usually mean a comune created "
              "by a merger after the ISTAT reference date.")

    claimed = {c for c in codes if c is not None}
    unclaimed = pop_df[pop_df["istat_code"].notna() & ~pop_df["istat_code"].isin(claimed)]
    if len(unclaimed):
        # Named when few: at national scale each one is a comune missing from
        # the results -- usually a merger to add to config.COMUNE_MERGERS.
        # Hundreds just mean data/istat/ holds regions outside the study area.
        names = (f": {_preview(unclaimed['name'] + ' (' + unclaimed['istat_code'] + ')')}"
                 if len(unclaimed) <= 30 else "")
        print(f"[INFO] {len(unclaimed)} ISTAT comuni have no matching town in this "
              f"study area (expected if data/istat/ holds regions outside it; "
              f"otherwise a merger to add to config.COMUNE_MERGERS){names}")


def attach_population(towns_gdf):
    """
    Adds demographic columns to the town GeoDataFrame, matching on the ISTAT
    code where OSM carries one and on the name otherwise.

    Adds: istat_code, population, province, population_65plus, pct_65plus,
          aging_index

    An unmatched town is usually NOT a data problem -- it is a town from
    outside the target area that the OSM extract happened to include
    (neighbouring countries, San Marino, or a neighbouring region), because
    ISTAT covers every real Italian municipality. The coverage report below
    distinguishes that from a missing workbook.
    """
    pop_df = load_population_table()

    codes, by_code, by_name, ambiguous = _match_towns(towns_gdf, pop_df)

    fields = ["population", "province", "population_65plus", "pct_65plus", "aging_index"]
    lookup = pop_df.set_index("istat_code")[fields].to_dict("index")

    towns_gdf = towns_gdf.copy()
    towns_gdf["istat_code"] = codes
    for field in fields:
        missing = None if field == "province" else pd.NA
        towns_gdf[field] = [lookup[c][field] if c is not None else missing for c in codes]

    # A relation that lost its name tag in OSM arrives with an empty name
    # (see fetch_towns.py); ISTAT's name is the authoritative one anyway.
    istat_names = dict(zip(pop_df["istat_code"], pop_df["name"]))
    unnamed = towns_gdf["name"].fillna("").eq("") & towns_gdf["istat_code"].notna()
    if unnamed.any():
        towns_gdf.loc[unnamed, "name"] = towns_gdf.loc[unnamed, "istat_code"].map(istat_names)
        print(f"[INFO] Named {int(unnamed.sum())} towns from ISTAT because their OSM "
              f"relation has no name tag: {_preview(towns_gdf.loc[unnamed, 'name'])}")

    unmatched = [name for name, code in zip(towns_gdf["name"], codes) if code is None]
    print(f"[INFO] Matched {by_code + by_name}/{len(towns_gdf)} towns to ISTAT data "
          f"({by_code} by ISTAT code, {by_name} by name).")

    if ambiguous:
        print(f"[WARN] {len(ambiguous)} towns were left unmatched because their "
              f"name is shared by several ISTAT comuni and OSM carries no usable "
              f"ref:ISTAT to tell them apart: {_preview(ambiguous)}")
    if unmatched:
        print(f"[WARN] {len(unmatched)} towns had no ISTAT match: {_preview(unmatched)}")
        print("       Most are likely towns from OUTSIDE the study area that the "
              "OSM extract included. See config.EXCLUDE_UNMATCHED_TOWNS.")
    _report_coverage(towns_gdf, codes, pop_df)

    if config.EXCLUDE_UNMATCHED_TOWNS and unmatched:
        before = len(towns_gdf)
        towns_gdf = towns_gdf[towns_gdf["istat_code"].notna()].copy()
        print(f"[INFO] EXCLUDE_UNMATCHED_TOWNS is on -- dropped "
              f"{before - len(towns_gdf)} unmatched towns from the analysis.")

    return towns_gdf
