#!/usr/bin/env python3
"""
download_istat.py
-----------------
Downloads the ISTAT data into data/istat/, exactly as published -- the
pipeline reads it unmodified.

    python tools/download_istat.py

Four sources:

  * The 21 regional census workbooks, "Censimento della popolazione: dati
    regionali, anno 2024", released 30 April 2026:
    https://www.istat.it/comunicato-territoriale/censimento-della-popolazione-dati-regionali-anno-2024/

  * POSAS 2025 from demo.istat.it -- the same census count by comune and age,
    which fills the comuni the workbooks miss. ISTAT's Liguria workbook has no
    per-comune tables, so without it Liguria's 234 comuni have no data.
    https://demo.istat.it/app/?i=POS&l=it

  * ASIA local units 2023, per comune, ATECO sectors 471 (supermarkets,
    minimarkets, grocers) and 472 (specialist food shops) -- ISTAT's register
    of shops, used to catch towns whose shops OpenStreetMap has not mapped.
    https://esploradati.istat.it/databrowser/ (dataflow 183_285_DF_DICA_ASIAULP_3)

  * Localities 2021 ("Basi territoriali"): the outline and 2021 population of
    every town, village and hamlet -- the settlements the analysis runs on.
    255 MB, read straight from the .zip.
    https://www.istat.it/notizia/basi-territoriali-e-variabili-censuarie/

Files already present are skipped, so it is safe to re-run after a partial
download. If ISTAT moves the files, download them by hand from the page above:
filenames do not matter, every .xlsx in data/istat/ is loaded.

Then check what was loaded:   python run.py istat
"""

import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ISTAT_DIR = PROJECT_ROOT / "data" / "istat"

WORKBOOK_BASE = "https://www.istat.it/wp-content/uploads/2026/04/"
POSAS_BASE = "https://demo.istat.it/data/posas/"

# As linked from the release page. The names are ISTAT's own, typos and all
# ("Lombradia"); Sicily's breaks the pattern.
WORKBOOKS = [
    "01_Piemonte_2024_Allegato-statistico.xlsx",
    "02_Valle_d_Aosta_2024_Allegato-statistico.xlsx",
    "03_Lombradia_2024_Allegato-statistico.xlsx",
    "04.1_Trentino_2024_Allegato-statistico.xlsx",
    "04.2_Alto_Adige_2024_Allegato-statistico.xlsx",
    "05_Veneto_2024_Allegato-statistico-1.xlsx",
    "06_Friuli_Venezia_Giulia_2024_Allegato-statistico.xlsx",
    "07_Liguria_2024_Allegato-statistico.xlsx",
    "08_Emilia_Romagna_2024_Allegato-statistico.xlsx",
    "09_Toscana_2024_Allegato-statistico.xlsx",
    "10_Umbria_2024_Allegato-statistico.xlsx",
    "11_Marche_2024_Allegato-statistico.xlsx",
    "12_Lazio_2024_Allegato-statistico.xlsx",
    "13_Abruzzo_2024_Allegato-statistico.xlsx",
    "14_Molise_2024_Allegato-statistico.xlsx",
    "15_Campania_2024_Allegato-statistico.xlsx",
    "16_Puglia_2024_Allegato-statistico.xlsx",
    "17_Basilicata_2024_Allegato-statistico.xlsx",
    "18_Calabria_2024_Allegato-statistico.xlsx",
    "Allegato-statistico_Sicilia-1.xlsx",
    "20_Sardegna_2024_Allegato-statistico.xlsx",
]

POSAS = [
    "POSAS_2025_it_Comuni.zip",     # ~8.5 MB: every comune, by single year of age
    "POSAS_2025_it_Province.zip",   # province names, which the comuni file lacks
]

LOCALITIES_BASE = "https://www.istat.it/storage/cartografia/basi_territoriali/2021/"
LOCALITIES = ["Localita_21.zip"]

# The shop register, straight from ISTAT's SDMX API as CSV. The year is pinned
# (and in the filename) so a re-download reproduces the same numbers.
SHOP_REGISTER = "ASIA_UL_2023_ateco_471_472.csv"
SHOP_REGISTER_URL = ("https://esploradati.istat.it/SDMXWS/rest/data/"
                     "IT1,183_285_DF_DICA_ASIAULP_3,1.0/A..LU.471+472.TOTAL"
                     "?startPeriod=2023&endPeriod=2023")
SDMX_CSV = "application/vnd.sdmx.data+csv;version=1.0.0"

# ISTAT's servers reject requests without a browser-like or identifying agent.
HEADERS = {"User-Agent": "FoodDesertAnalysisTool/1.0 (personal research project)"}

# .xlsx and .zip are both zip archives; anything else (an HTML error page
# served with a 200) must not be saved under their names, or the loader fails
# later with a far less obvious error.
ZIP_MAGIC = b"PK\x03\x04"


def download_shop_register():
    """Fetches the ASIA shop register; returns False on failure."""
    target = ISTAT_DIR / SHOP_REGISTER
    if target.exists():
        print(f"  [skip] {SHOP_REGISTER} (already present)")
        return True
    try:
        # The API is slow to answer at times, hence the long timeout.
        resp = requests.get(SHOP_REGISTER_URL, headers={**HEADERS, "Accept": SDMX_CSV},
                            timeout=300)
        resp.raise_for_status()
        # Same idea as ZIP_MAGIC: an error page must not be saved as data.
        if not resp.content.startswith(b"DATAFLOW,"):
            raise ValueError("response is not an SDMX CSV file")
    except (requests.RequestException, ValueError) as e:
        print(f"  [FAIL] {SHOP_REGISTER}: {e}")
        return False
    target.write_bytes(resp.content)
    print(f"  [ OK ] {SHOP_REGISTER} ({len(resp.content) / 2**20:.1f} MB)")
    return True


def main():
    ISTAT_DIR.mkdir(parents=True, exist_ok=True)
    failed = []

    downloads = ([(WORKBOOK_BASE, n) for n in WORKBOOKS]
                 + [(POSAS_BASE, n) for n in POSAS]
                 + [(LOCALITIES_BASE, n) for n in LOCALITIES])
    for base, name in downloads:
        target = ISTAT_DIR / name
        if target.exists():
            print(f"  [skip] {name} (already present)")
            continue
        if name in LOCALITIES:
            print(f"  [....] {name} (255 MB -- a few minutes)")
        try:
            resp = requests.get(base + name, headers=HEADERS, timeout=900)
            resp.raise_for_status()
            if not resp.content.startswith(ZIP_MAGIC):
                raise ValueError("response is not a zip/xlsx file")
        except (requests.RequestException, ValueError) as e:
            print(f"  [FAIL] {name}: {e}")
            failed.append(name)
            continue
        target.write_bytes(resp.content)
        print(f"  [ OK ] {name} ({len(resp.content) / 2**20:.1f} MB)")

    if not download_shop_register():
        failed.append(SHOP_REGISTER)

    present = len(list(ISTAT_DIR.glob("*.xlsx")))
    print(f"\n{present} workbook(s) in {ISTAT_DIR}.")
    if failed:
        print(f"{len(failed)} download(s) failed -- fetch them by hand from the "
              f"pages linked at the top of this script.")
        return 1
    print("Next:  python run.py istat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
