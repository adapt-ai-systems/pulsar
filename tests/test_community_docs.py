"""The contribution files must stay in step with the tree.

AGENTS.md, CONTRIBUTING.md, CODEOWNERS and the CI workflow all name files
and commands. These tests fail when one of them points at something that
is not there.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _codeowner_paths() -> list[str]:
    paths = []
    for line in _read(".github/CODEOWNERS").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line.split()[0])
    return paths


def test_codeowners_paths_exist():
    paths = _codeowner_paths()
    assert paths, "CODEOWNERS has no entries"
    for path in paths:
        assert path.startswith("/"), f"{path}: anchor the path with a leading /"
        assert (REPO / path.lstrip("/")).exists(), f"CODEOWNERS names a missing path: {path}"


def test_agents_md_lists_every_codeowners_path():
    agents = _read("AGENTS.md")
    for path in _codeowner_paths():
        assert f"`{path.lstrip('/')}`" in agents, f"AGENTS.md does not list {path}"


def test_docs_name_the_setup_and_check_commands():
    for rel in ("AGENTS.md", "CONTRIBUTING.md", "README.md"):
        text = _read(rel)
        assert "./dev-setup.sh" in text, f"{rel} does not name ./dev-setup.sh"
        assert "./ci.sh" in text, f"{rel} does not name ./ci.sh"


def test_ci_workflow_runs_the_same_two_commands():
    if not (REPO / ".github/workflows/ci.yml").exists():
        pytest.skip("the CI workflow file is not in this tree")
    workflow = _read(".github/workflows/ci.yml")
    assert "run: ./dev-setup.sh" in workflow
    assert "run: ./ci.sh" in workflow
    assert re.search(r"^permissions:\n  contents: read$", workflow, re.M)


def test_agents_md_map_paths_exist():
    agents = _read("AGENTS.md")
    table = agents.split("## Map", 1)[1].split("## Tests", 1)[0]
    found = 0
    for token in re.findall(r"`([^`]+)`", table):
        m = re.fullmatch(r"(.*)\{([^}]+)\}(.*)", token)
        names = [m.group(1) + part + m.group(3) for part in m.group(2).split(",")] if m else [token]
        for name in names:
            found += 1
            assert (REPO / name).exists(), f"AGENTS.md map names a missing path: {name}"
    assert found >= 10


def test_ci_tools_are_declared():
    """A fresh clone must get every tool that ci.sh runs."""
    assert re.search(r"^pytest==", _read("requirements-dev.txt"), re.M)
    assert "-r requirements.txt" in _read("requirements-dev.txt")
    dev = json.loads(_read("web/package.json"))["devDependencies"]
    assert "svelte-check" in dev and "typescript" in dev
    assert (REPO / "dev-setup.sh").stat().st_mode & 0o111, "dev-setup.sh is not executable"
