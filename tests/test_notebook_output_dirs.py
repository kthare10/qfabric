"""Every notebook that writes a result file must create its directory first.

`results/` and `qne-sequence/results/` are gitignored scratch — a fresh checkout
has neither. When run outputs stopped being tracked, `qne-sequence/results/` was
deleted and nothing recreated it, so `notebooks/sequence/02_scenarios` raised

    FileNotFoundError: qne-sequence/results/sequence_scenarios_fabric.json

*after* a completed slice sweep, discarding the run. The write is the last line of
the cell, so the sweep is lost with it.

This scans the notebooks rather than any one call site, so a new notebook that
writes results without creating the directory fails here instead of on a slice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NOTEBOOKS = sorted(REPO.glob("notebooks/**/*.ipynb"))

# a write whose path is built from a 'results' directory
WRITE = re.compile(r"(\w+)\.write_text\(|open\(\s*([^,)]*results[^,)]*)\s*,\s*['\"]w")


def code_cells(path: Path):
    nb = json.loads(path.read_text())
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


@pytest.mark.parametrize("nb_path", NOTEBOOKS, ids=lambda p: str(p.relative_to(REPO)))
def test_result_writes_create_their_directory(nb_path):
    for src in code_cells(nb_path):
        if "results" not in src:
            continue
        for m in WRITE.finditer(src):
            var = m.group(1)
            if var and f"{var} = " not in src and f"{var}=" not in src:
                continue    # not bound in this cell; can't tell what it points at
            if var and "results" not in _binding(src, var):
                continue    # writes somewhere else entirely
            assert "mkdir(" in src, (
                f"{nb_path.relative_to(REPO)} writes a result file without creating "
                f"its directory; a fresh checkout has no results/ and the cell will "
                f"raise FileNotFoundError after doing all the work"
            )


def _binding(src: str, var: str) -> str:
    for line in src.split("\n"):
        if re.match(rf"\s*{re.escape(var)}\s*=", line):
            return line
    return ""


def test_the_scan_actually_matches_the_known_writers():
    """Guard the guard: if the regex stops matching, the test above goes silently
    green. These two writes are the ones the incident was about."""
    src = "\n".join(code_cells(REPO / "notebooks/sequence/02_scenarios.ipynb"))
    assert "sequence_scenarios_fabric.json" in src
    assert len(WRITE.findall(src)) >= 2, "expected to find the two result writes"
