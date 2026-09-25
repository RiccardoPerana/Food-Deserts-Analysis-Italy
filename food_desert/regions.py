"""
regions.py
----------
The region of a comune, from the first three digits of its ISTAT code (the
province code). Used for the per-region breakdown in the results summary.

ISTAT's codes are stable, but provinces created since 2001 were given new
numbers above 100 rather than slotted into their region's range, so each
region lists its codes explicitly.
"""

REGION_PROVINCES = {
    "Piemonte": [1, 2, 3, 4, 5, 6, 96, 103],
    "Valle d'Aosta": [7],
    "Lombardia": [12, 13, 14, 15, 16, 17, 18, 19, 20, 97, 98, 108],
    "Trentino-Alto Adige": [21, 22],
    "Veneto": [23, 24, 25, 26, 27, 28, 29],
    "Friuli-Venezia Giulia": [30, 31, 32, 93],
    "Liguria": [8, 9, 10, 11],
    "Emilia-Romagna": [33, 34, 35, 36, 37, 38, 39, 40, 99],
    "Toscana": [45, 46, 47, 48, 49, 50, 51, 52, 53, 100],
    "Umbria": [54, 55],
    "Marche": [41, 42, 43, 44, 109],
    "Lazio": [56, 57, 58, 59, 60],
    "Abruzzo": [66, 67, 68, 69],
    "Molise": [70, 94],
    "Campania": [61, 62, 63, 64, 65],
    "Puglia": [71, 72, 73, 74, 75, 110],
    "Basilicata": [76, 77],
    "Calabria": [78, 79, 80, 101, 102],
    "Sicilia": [81, 82, 83, 84, 85, 86, 87, 88, 89],
    "Sardegna": [90, 91, 92, 95, 111],
}

_BY_PROVINCE = {f"{code:03d}": region
                for region, codes in REGION_PROVINCES.items() for code in codes}


def region_of(istat_code):
    """The region of a six-digit ISTAT comune code, or "Unknown"."""
    return _BY_PROVINCE.get(str(istat_code or "")[:3], "Unknown")
