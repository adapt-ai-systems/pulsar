"""pulsar.fsutil — the atomic-write helper behaves exactly like the
hand-rolled ``tmp.write_text(...); tmp.replace(path)`` it replaced."""
import json
import subprocess
import sys

import pytest

from pulsar import fsutil


def test_text_replaces_target_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("old")
    fsutil.atomic_write_text(path, "new")
    assert path.read_text() == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json"]


def test_default_tmp_is_name_plus_dot_tmp(tmp_path, monkeypatch):
    path = tmp_path / "diet.json"
    seen = []
    real = type(path).replace

    def spy(self, target):
        seen.append((self.name, self.read_text(), target.name))
        return real(self, target)

    monkeypatch.setattr(type(path), "replace", spy)
    fsutil.atomic_write_text(path, "x")
    assert seen == [("diet.json.tmp", "x", "diet.json")]


def test_explicit_tmp_name_is_used(tmp_path, monkeypatch):
    path = tmp_path / "c1.json"
    seen = []
    real = type(path).replace
    monkeypatch.setattr(type(path), "replace",
                        lambda self, t: (seen.append(self.name), real(self, t))[1])
    fsutil.atomic_write_text(path, "x", tmp=tmp_path / "c1.tmp")
    assert seen == ["c1.tmp"] and path.read_text() == "x"


def test_text_encoding_is_passed_through(tmp_path):
    path = tmp_path / "u.txt"
    fsutil.atomic_write_text(path, "café", encoding="utf-8")
    assert path.read_bytes() == "café".encode("utf-8")


@pytest.mark.parametrize("kwargs,expected", [
    ({}, lambda v: json.dumps(v)),
    ({"indent": 2}, lambda v: json.dumps(v, indent=2)),
    ({"indent": 1}, lambda v: json.dumps(v, indent=1)),
    ({"indent": 2, "newline": True}, lambda v: json.dumps(v, indent=2) + "\n"),
    ({"indent": 2, "ensure_ascii": False, "newline": True, "encoding": "utf-8"},
     lambda v: json.dumps(v, indent=2, ensure_ascii=False) + "\n"),
])
def test_json_bytes_match_json_dumps(tmp_path, kwargs, expected):
    value = {"b": [1, 2], "a": "café ☃", "n": None}
    path = tmp_path / "v.json"
    fsutil.atomic_write_json(path, value, **kwargs)
    want = expected(value)
    enc = kwargs.get("encoding") or "utf-8"
    assert path.read_bytes() == want.encode(enc)


def test_missing_parent_raises_oserror_and_does_not_mkdir(tmp_path):
    with pytest.raises(OSError):
        fsutil.atomic_write_text(tmp_path / "nope" / "a.json", "x")
    assert not (tmp_path / "nope").exists()


def test_failed_write_leaves_target_untouched(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("keep")
    with pytest.raises(TypeError):
        fsutil.atomic_write_json(path, {"bad": object()})
    assert path.read_text() == "keep"


def test_import_is_light():
    code = ("import sys, pulsar.fsutil; "
            "bad = [m for m in ('fastapi', 'starlette', 'pulsar.daemon') "
            "if m in sys.modules]; assert not bad, bad")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_atomic_write_sets_owner_only_mode(tmp_path):
    import os
    import stat

    old_umask = os.umask(0o022)
    try:
        path = tmp_path / "secret.json"
        pre_tmp = tmp_path / "secret.json.tmp"
        pre_tmp.write_text("stale")
        pre_tmp.chmod(0o644)

        fsutil.atomic_write_json(path, {"apiKey": "dummy"})
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)
