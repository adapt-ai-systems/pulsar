"""B6 — log watches track the file inode so delete/recreate is detected
without rewinding pre-existing (inode-less) watch details."""
from __future__ import annotations

import os

from pulsar.daemon.condwatch import _eval_log


def _spec(path, pattern="ERROR"):
    return {"path": str(path), "pattern": pattern}


def test_initial_baseline_records_offset_and_inode(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("ERROR old\n")
    bad, detail = _eval_log(_spec(f), None)
    assert bad is False
    assert detail["offset"] == f.stat().st_size
    assert detail["ino"] == f.stat().st_ino
    assert detail["note"] == "baselined at current end"


def test_old_detail_without_inode_is_not_rewound(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("ERROR old\n")
    prev = {"offset": f.stat().st_size}  # pre-deploy shape: no "ino"
    bad, detail = _eval_log(_spec(f), prev)
    assert bad is False  # the pre-existing match must not fire
    assert detail["offset"] == f.stat().st_size
    assert detail["ino"] == f.stat().st_ino
    with f.open("a") as fh:
        fh.write("ERROR new\n")
    bad, detail2 = _eval_log(_spec(f), detail)
    assert bad is True and "ERROR new" in detail2["match"]


def test_truncate_same_inode_rereads_from_top(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("hello world, quite a long line\n")
    _, detail = _eval_log(_spec(f), None)
    ino = f.stat().st_ino
    with f.open("w") as fh:  # truncate in place, same inode
        fh.write("ERROR x\n")
    assert f.stat().st_ino == ino
    bad, detail2 = _eval_log(_spec(f), detail)
    assert bad is True and "ERROR x" in detail2["match"]
    assert detail2["ino"] == ino


def test_replace_with_larger_inode_containing_pattern_fires(tmp_path):
    f = tmp_path / "a.log"
    f.write_text("short\n")
    _, detail = _eval_log(_spec(f), None)
    old_ino = detail["ino"]
    g = tmp_path / "b.log"
    g.write_text("a much longer replacement file with ERROR inside it\n")
    os.replace(g, f)  # new inode, larger than the old offset
    assert f.stat().st_ino != old_ino
    assert f.stat().st_size > detail["offset"]
    bad, detail2 = _eval_log(_spec(f), detail)
    assert bad is True and "ERROR inside" in detail2["match"]
    assert detail2["ino"] == f.stat().st_ino
    assert detail2["offset"] == f.stat().st_size
