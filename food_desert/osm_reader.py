"""
osm_reader.py
-------------
Shared plumbing for reading the local .osm.pbf with osmium.

Every pass that needs geometry -- way shapes, area polygons -- has to resolve
node IDs to coordinates, and osmium keeps an index of every node in the file
to do it. Where that index lives (RAM or disk) is the single biggest factor in
whether a national run fits on the machine. It is decided once, in
config.OSMIUM_NODE_INDEX, and applied here, so no module can quietly fall back
to the in-memory default.
"""

from pathlib import Path

from . import config


def require_pbf(pbf_path):
    """Raises a FileNotFoundError that says where the extract should be."""
    pbf_path = Path(pbf_path)
    if not pbf_path.exists():
        raise FileNotFoundError(
            f"Could not find {pbf_path}. Download the Geofabrik extract named in "
            f"config.OSM_EXTRACT_NAME into {pbf_path.parent} -- it must be the "
            f"same file the OSRM routing graph is built from. "
            f"Run `python run.py paths` to check every input."
        )
    return pbf_path


def _check_index_setting(idx):
    """
    Rejects a file-backed index given an explicit filename.

    libosmium opens that file and never closes the handle. On Windows an open
    file cannot be deleted or reopened, so the SECOND pass of a run fails; and
    since the file is opened without truncation, one left over from an earlier
    run would be read back as part of the new index. The unnamed form uses a
    temporary file instead, which the operating system removes when the
    process exits.
    """
    kind, _, filename = idx.partition(",")
    if kind.endswith("_file_array") and filename:
        raise ValueError(
            f"config.OSMIUM_NODE_INDEX = {idx!r}: give the index type without a "
            f"filename (just {kind!r}). A named index file cannot be reused "
            f"within a run on Windows; the unnamed form uses a temporary file "
            f"that is removed automatically."
        )


def apply_with_locations(handler, pbf_path):
    """
    Runs `handler` over the extract with node locations resolved, using the
    index configured in config.OSMIUM_NODE_INDEX.
    """
    pbf_path = require_pbf(pbf_path)
    _check_index_setting(config.OSMIUM_NODE_INDEX)
    handler.apply_file(str(pbf_path), locations=True, idx=config.OSMIUM_NODE_INDEX)
