"""Import-time paths in pulsar.launch are depth-sensitive: pin them to
the files they must find, so a future move cannot silently re-point them."""

from pathlib import Path

import pulsar
import pulsar.launch.context as context_mod

_PKG = Path(pulsar.__file__).resolve().parent


def test_context_templates_point_at_runner_context_dir():
    assert context_mod.CONTEXT_TEMPLATES == _PKG / "runner" / "context"
    assert (context_mod.CONTEXT_TEMPLATES / "PULSAR.md").is_file()
    assert (context_mod.CONTEXT_TEMPLATES / "HELPER.md").is_file()
