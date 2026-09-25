#!/usr/bin/env python3
"""
run.py
------
Single entry point for the Food Desert Analysis project.

    python run.py analyze              Run the full analysis pipeline
    python run.py layers               Build the map overlay layers
    python run.py publish              Copy results into docs/ for the live demo
    python run.py serve                Preview the web map locally
    python run.py diagnose "Town Name" Spot-check one or more towns
    python run.py paths                Show where the project expects its files
    python run.py istat                Load and summarise the ISTAT workbooks
    python run.py all                  analyze -> layers -> publish

--- WHY A SINGLE ENTRY POINT -------------------------------------------------
  * The pipeline's shape is visible in one place. `python run.py --help` is a
    truthful description of what this project does, rather than an ordering
    that exists only in the README.
  * The working directory stops mattering. Combined with paths.py, this can be
    invoked from anywhere and still resolve everything from the repository root.
  * Publishing to the live demo is an explicit, named action rather than a side
    effect of running the analysis.
"""

import argparse
import shutil
import subprocess
import sys
import webbrowser

from food_desert import config, paths


def cmd_analyze(_args):
    """Runs the full analysis and writes results to output/."""
    from food_desert.pipeline import run_pipeline
    run_pipeline()


def cmd_layers(_args):
    """Builds the cycling-lane and public-transport overlay layers."""
    from food_desert.fetch_map_layers import build_map_layers
    paths.ensure_directories()
    build_map_layers()


def _is_generated(output):
    """A file exists, or a grid directory has its manifest."""
    return (output / "manifest.json").exists() if output.suffix == "" else output.exists()


def _publish_grid(src_dir, dst_dir):
    """
    Replaces a published grid directory with a freshly generated one.

    The old cells are removed first, not merely overwritten: a cell that is
    empty in the new run would otherwise survive from the previous one and be
    committed forever, unreferenced. Returns (file count, bytes).
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    for old in [*dst_dir.glob("*.geojson"), dst_dir / "manifest.json"]:
        old.unlink(missing_ok=True)
    count, size = 0, 0
    for src in [*src_dir.glob("*.geojson"), src_dir / "manifest.json"]:
        shutil.copy2(src, dst_dir / src.name)
        count += 1
        size += src.stat().st_size
    return count, size


def cmd_publish(_args):
    """
    Copies the outputs into docs/data/, where GitHub Pages serves them.

    This is deliberately a separate command rather than something `analyze`
    does automatically. The published files ARE the live demo -- making the
    copy explicit means an experimental run, a partial run, or a run with a
    changed threshold cannot silently replace what the world sees.
    """
    paths.ensure_directories()

    missing = [p for p in config.PUBLISHABLE_OUTPUTS if not _is_generated(p)]
    if missing:
        print("[ERROR] Cannot publish -- these have not been generated yet:")
        for p in missing:
            print(f"          {p.name}")
        print("\n        Run:  python run.py analyze     (towns, routes, meta)")
        print("              python run.py layers      (cycling + transport)")
        return 1

    total_mb = 0.0
    first_load_mb = 0.0
    print(f"[INFO] Publishing to {paths.DOCS_DATA_DIR}")
    for src in config.PUBLISHABLE_OUTPUTS:
        dst = paths.DOCS_DATA_DIR / src.name
        if src.is_dir():
            count, size = _publish_grid(src, dst)
            size_mb = size / 2**20
            print(f"       {src.name + '/':28} {size_mb:6.2f} MB  ({count} files, "
                  f"fetched a cell at a time)")
        else:
            shutil.copy2(src, dst)
            size_mb = dst.stat().st_size / 2**20
            if src in config.MAP_FIRST_LOAD:
                first_load_mb += size_mb
            print(f"       {src.name:28} {size_mb:6.2f} MB")
        total_mb += size_mb

    print(f"[INFO] Published {total_mb:.2f} MB total; {first_load_mb:.2f} MB of it is "
          f"downloaded when the map opens.")

    # The grid directories are fetched a cell at a time, so only the single
    # files load up front -- that is the number a visitor on a phone feels.
    if first_load_mb > 10:
        print(f"[WARN] {first_load_mb:.0f} MB is a heavy first load for a web map. "
              f"routes.geojson is the likely cause; consider loading each route "
              f"only when its town is clicked.")

    print("\n       Next:  git add docs/ && git commit -m 'Update published data'")
    return 0


def cmd_serve(args):
    """Serves docs/ locally so the map can be previewed exactly as published."""
    url = f"http://localhost:{args.port}/"
    print(f"[INFO] Serving {paths.DOCS_DIR} at {url}")
    print("[INFO] This mirrors how GitHub Pages will serve it. Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)

    # Ctrl+C is the normal, documented way to stop a dev server -- it is not an
    # error, and it should not print a stack trace. subprocess.run() propagates
    # KeyboardInterrupt from the child, so it is caught and reported plainly.
    try:
        subprocess.run(
            [sys.executable, "-m", "http.server", str(args.port)],
            cwd=paths.DOCS_DIR,
        )
    except KeyboardInterrupt:
        print("\n[INFO] Server stopped.")
    return 0


def cmd_diagnose(args):
    """Spot-checks specific towns against cached and live data."""
    from food_desert.diagnostics import run_diagnostics
    run_diagnostics(args.towns)


def cmd_paths(_args):
    """Prints the resolved project layout and flags anything missing."""
    print("Resolved project layout:")
    paths.describe()

    print(f"\nStudy area: {config.STUDY_AREA_LABEL} (TARGET_LEVEL = "
          f"{config.TARGET_LEVEL!r}, caches named '{config.SCOPE_SLUG}')")
    print(f"Node index: {config.OSMIUM_NODE_INDEX.split(',')[0]}")

    # OSRM writes several files sharing the dataset name (.osrm.cells,
    # .osrm.mldgr, ...); any of them means a graph has been built.
    osrm_built = any(config.OSRM_DATASET_PATH.parent.glob(
        config.OSRM_DATASET_PATH.name + ".*"))

    print("\nKey inputs:")
    checks = [
        ("OSM extract", config.OSM_PBF_PATH, config.OSM_PBF_PATH.exists()),
        ("OSRM routing graph", config.OSRM_DATASET_PATH, osrm_built),
        ("Towns cache", config.TOWNS_CACHE_PATH, config.TOWNS_CACHE_PATH.exists()),
        ("Supermarket cache", config.SUPERMARKETS_CACHE_PATH,
         config.SUPERMARKETS_CACHE_PATH.exists()),
        *[(f"ISTAT {p.stem[:24]}", p, p.exists()) for p in config.ISTAT_POPULATION_XLSX],
        ("ISTAT POSAS comuni", config.ISTAT_POSAS_COMUNI, config.ISTAT_POSAS_COMUNI.exists()),
        ("ISTAT POSAS provinces", config.ISTAT_POSAS_PROVINCE,
         config.ISTAT_POSAS_PROVINCE.exists()),
        ("ISTAT shop register", config.ISTAT_SHOP_REGISTER,
         config.ISTAT_SHOP_REGISTER.exists()),
        ("ISTAT localities", config.ISTAT_LOCALITIES, config.ISTAT_LOCALITIES.exists()),
        ("Settlements cache", config.SETTLEMENTS_CACHE_PATH,
         config.SETTLEMENTS_CACHE_PATH.exists()),
    ]
    for label, path, ok in checks:
        # The caches are built by the first run, so their absence is expected.
        mark = "OK     " if ok else "not yet" if "cache" in label else "MISSING"
        print(f"  [{mark}] {label:22} {path}")

    n = len(config.ISTAT_POPULATION_XLSX)
    print(f"\n  {n} ISTAT workbook(s) found.", end="")
    if config.TARGET_LEVEL == "country":
        print(" Italy is published as 21 (20 regions, Trentino-Alto Adige as two), "
              "plus the two POSAS files for Liguria"
              + (" -- OK." if n == 21 and config.ISTAT_POSAS_COMUNI.exists()
                 else " -- run `python tools/download_istat.py`."))
    else:
        print()
    print("  `python run.py istat` loads them all in seconds and reports what it found.")


def cmd_istat(_args):
    """
    Loads every ISTAT workbook and prints what was found, in seconds.

    Worth running after downloading the workbooks and before the first full
    analysis: a workbook whose layout differs, or a region that is missing, is
    far cheaper to discover here than half an hour into a national run.
    """
    from food_desert.population_istat import load_population_table
    df = load_population_table()
    by_province = df.groupby("province").size()
    print(f"\n{len(by_province)} provinces across {len(config.ISTAT_POPULATION_XLSX)} "
          f"workbook(s). For all of Italy expect about 7,900 comuni, 107 provinces "
          f"and a population of about 59 million.")


def cmd_all(args):
    """analyze -> layers -> publish, in the correct order."""
    cmd_analyze(args)
    cmd_layers(args)
    return cmd_publish(args)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py",
        description=f"Food Desert Analysis -- {config.STUDY_AREA_LABEL}.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("analyze", help="Run the full analysis pipeline").set_defaults(func=cmd_analyze)
    sub.add_parser("layers", help="Build map overlay layers").set_defaults(func=cmd_layers)
    sub.add_parser("publish", help="Copy results into docs/ for the live demo").set_defaults(func=cmd_publish)
    sub.add_parser("paths", help="Show resolved paths and check inputs exist").set_defaults(func=cmd_paths)
    sub.add_parser("istat", help="Load and summarise the ISTAT workbooks").set_defaults(func=cmd_istat)
    sub.add_parser("all", help="analyze, then layers, then publish").set_defaults(func=cmd_all)

    p_serve = sub.add_parser("serve", help="Preview the web map locally")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--no-browser", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    p_diag = sub.add_parser("diagnose", help="Spot-check specific towns")
    p_diag.add_argument("towns", nargs="+", metavar="TOWN",
                        help='One or more town names or ISTAT codes, '
                             'e.g. "Torri di Quartesolo" 025001')
    p_diag.set_defaults(func=cmd_diagnose)

    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    sys.exit(args.func(args) or 0)
