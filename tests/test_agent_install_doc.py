"""AGENT_INSTALL.md must stay in step with README.md and install.sh.

The agent guide repeats the prerequisite commands so that it works when a
user pastes only that one file into a chat. These tests fail when the two
copies drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
README = (REPO / "README.md").read_text(encoding="utf-8")
GUIDE = (REPO / "AGENT_INSTALL.md").read_text(encoding="utf-8")

_BASH_BLOCK = re.compile(r"```bash\n(.*?)```", re.S)


def _section(text: str, heading: str) -> str:
    """Text from ``heading`` up to the next heading of the same or higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    rest = text[start + len(heading) + 1:]
    nxt = re.search(rf"^#{{1,{level}}} ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_readme_points_to_the_agent_guide():
    section = _section(README, "## Install with an AI agent")
    assert "(AGENT_INSTALL.md)" in section
    assert (
        "https://raw.githubusercontent.com/adapt-ai-systems/pulsar/main/AGENT_INSTALL.md"
        in section
    )


def test_prerequisite_commands_match_the_readme():
    blocks = _BASH_BLOCK.findall(_section(README, "### Install prerequisites"))
    assert len(blocks) >= 5, "README prerequisite blocks not found"
    for block in blocks:
        assert block in GUIDE, f"AGENT_INSTALL.md lacks this README block:\n{block}"


def test_clone_url_matches_the_readme():
    url = re.search(r"git clone (\S+)", _section(README, "## Install")).group(1)
    assert f"git clone {url} " in GUIDE


def test_guide_uses_the_default_ports_of_the_installer():
    installer = (REPO / "install.sh").read_text(encoding="utf-8")
    daemon = re.search(r'DAEMON_PORT="\$\{PULSAR_DAEMON_PORT:-(\d+)\}"', installer).group(1)
    web = re.search(r'WEB_PORT="\$\{PULSAR_WEB_PORT:-(\d+)\}"', installer).group(1)
    assert f"http://127.0.0.1:{daemon}/healthz" in GUIDE
    assert f"http://127.0.0.1:{web}/" in GUIDE


def test_guide_has_a_check_for_each_numbered_step():
    steps = re.split(r"^## Step \d+ ", GUIDE, flags=re.M)[1:]
    assert len(steps) == 6
    for step in steps[:5]:
        assert "**Check:**" in step, step.splitlines()[0]
