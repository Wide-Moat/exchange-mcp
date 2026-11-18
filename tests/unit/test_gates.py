"""Gates on the gates: every refusal in docs/requirements.md names a test that exists, the docs match the
code, the tool surface matches its lock, and the catalogue fits its budget."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROW = re.compile(r"(tests/unit/test_\w+\.py)::(\w+)")


def _tests_on_disk() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in (ROOT / "tests" / "unit").glob("test_*.py"):
        tree = ast.parse(path.read_text())
        out[f"tests/unit/{path.name}"] = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name.startswith("test_")
        }
    return out


def test_every_must_refuse_row_names_a_test_that_exists():
    text = (ROOT / "docs" / "requirements.md").read_text()
    rows = ROW.findall(text)
    assert len(rows) >= 40, f"only {len(rows)} refusal rows found: the parser or the document is broken"
    have = _tests_on_disk()
    missing = [f"{f}::{t}" for f, t in rows if t not in have.get(f, set())]
    assert not missing, "requirements name tests that do not exist:\n" + "\n".join(missing)


def test_the_gate_fails_on_a_missing_test(tmp_path):
    """The check above, pointed at a row that names nothing, must report it."""
    have = _tests_on_disk()
    rows = ROW.findall("| x | tests/unit/test_mail.py::test_that_does_not_exist |")
    assert [t for f, t in rows if t not in have.get(f, set())] == ["test_that_does_not_exist"]


async def test_docs_tools_md_matches_the_code():
    from exchange_mcp.docs import render

    path = ROOT / "docs" / "tools.md"
    assert path.exists(), "docs/tools.md is missing: python -m exchange_mcp.docs > docs/tools.md"
    assert path.read_text() == await render(), "regenerate: python -m exchange_mcp.docs > docs/tools.md"


async def test_the_tool_surface_matches_the_lock():
    import json

    from exchange_mcp.surface import LOCK, drift, surface

    assert LOCK.exists(), "tool-surface.lock.json is missing: python -m exchange_mcp.surface > tool-surface.lock.json"
    assert drift(json.loads(LOCK.read_text()), await surface()) == []


async def test_the_catalogue_fits_its_budget():
    from exchange_mcp.budget import measure, problems
    from exchange_mcp.registry import AREAS, parse_areas

    default = await measure(parse_areas(None))
    assert problems(default, check_total=True) == []
    assert problems(await measure(frozenset(AREAS)), check_total=False) == []
    assert default["total"] > 1000, "the measurement saw almost nothing: the catalogue did not load"


def test_product_code_names_no_installation():
    """A local echo of the organisation's purity gate: example domains only, and no known installation name."""
    files = [
        *(ROOT / "src").rglob("*.py"),
        *(ROOT / "tests").rglob("*.py"),
        *ROOT.glob("*.md"),
        *(ROOT / "docs").glob("*.md"),
    ]
    assert len(files) > 20, "the scan saw almost nothing"
    allowed = ("example.com", "example.org", "corp.example", "microsoft.com", "xmlsoap.org", "python.org", "claude.com")
    found = []
    for path in files:
        # the product's own public repositories may be linked; any other GitHub path is a finding
        text = re.sub(r"github\.com/wide-moat/[a-z0-9-]+", "", path.read_text().lower())
        for host in re.findall(r"\b(?:[a-z0-9-]+\.)+(?:ru|com|net|org|io|ai)\b", text):
            if not host.endswith(allowed):
                found.append(f"{path.relative_to(ROOT)}: {host}")
    assert not found, "\n".join(found)
