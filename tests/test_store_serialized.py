"""B2+B8 regression tests — the live SQLite connection is serialized.

Covers: concurrent ``list_pulses`` readers, ``_Txn.__enter__`` releasing the lock when BEGIN raises,
``close`` serialized against a held transaction, and the four hand-written
transaction paths (reorder_grids, delete_grid, reorder_pulses, delete_pulse).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from pulsar.store import Store
from pulsar.store.db import GridNotEmpty, UnknownGrid, UnknownPulse, _LockedConn, _Txn


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "pulsar.db"))
    try:
        yield s
    finally:
        s.close()


def _mk_pulses(store, n, grid_id=None):
    return [store.create_pulse(f"p{i}", "m", "claude", grid_id=grid_id)
            for i in range(n)]


def test_connection_is_locked_proxy_after_startup(store):
    assert isinstance(store._conn, _LockedConn)
    assert isinstance(store._lock, threading.RLock().__class__)


def test_five_concurrent_list_pulses_readers(store):
    _mk_pulses(store, 20)

    def read(_):
        out = []
        for _ in range(50):
            out.append(len(store.list_pulses()))
        return out

    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(read, range(5)))
    assert all(set(r) == {20} for r in results)



def test_txn_enter_releases_lock_when_begin_raises():
    class Boom:
        def execute(self, sql, parameters=()):
            raise sqlite3.OperationalError("cannot start a transaction")

    lock = threading.RLock()
    txn = _Txn(Boom(), lock)
    with pytest.raises(sqlite3.OperationalError):
        txn.__enter__()
    # If __enter__ leaked the lock a second thread could never take it.
    acquired = []
    t = threading.Thread(target=lambda: acquired.append(lock.acquire(timeout=2)))
    t.start()
    t.join()
    assert acquired == [True]


def test_txn_rolls_back_on_exception(store):
    g = store.create_grid("g")
    with pytest.raises(RuntimeError):
        with store._conn.transaction() as conn:
            conn.execute("UPDATE grids SET name = 'changed' WHERE id = ?;", (g.id,))
            raise RuntimeError("abort")
    assert store.get_grid(g.id).name == "g"
    assert not store._conn._conn.in_transaction


def test_close_serialized_against_held_transaction(tmp_path):
    s = Store(str(tmp_path / "pulsar.db"))
    entered = threading.Event()
    release = threading.Event()
    closed_at = {}

    def holder():
        with s._conn.transaction() as conn:
            conn.execute("INSERT INTO grids (id, name, position)"
                         " VALUES ('x', 'x', 1);")
            entered.set()
            release.wait(5)

    def closer():
        entered.wait(5)
        s.close()
        closed_at["t"] = time.monotonic()

    th, tc = threading.Thread(target=holder), threading.Thread(target=closer)
    th.start(); tc.start()
    entered.wait(5)
    time.sleep(0.2)
    assert "t" not in closed_at  # close is blocked behind the open txn
    released_at = time.monotonic()
    release.set()
    th.join(5); tc.join(5)
    assert closed_at["t"] >= released_at
    # The transaction committed before close ran.
    raw = sqlite3.connect(str(tmp_path / "pulsar.db"))
    assert raw.execute("SELECT COUNT(*) FROM grids WHERE id='x'").fetchone()[0] == 1
    raw.close()


def test_reorder_grids_path(store):
    a, b, c = (store.create_grid(n) for n in "abc")
    store.reorder_grids([c.id, a.id, b.id])
    assert [g.id for g in store.list_grids()] == [c.id, a.id, b.id]
    with pytest.raises(UnknownGrid):
        store.reorder_grids(["nope"])
    assert not store._conn._conn.in_transaction


def test_delete_grid_path_preserves_error_and_detaches(store):
    g = store.create_grid("g")
    p = _mk_pulses(store, 2, grid_id=g.id)
    with pytest.raises(GridNotEmpty):
        store.delete_grid(g.id)
    assert store.delete_grid(g.id, detach=True) == 2
    with pytest.raises(UnknownGrid):
        store.get_grid(g.id)
    assert all(store.get_pulse(x.id).grid_id is None for x in p)
    assert not store._conn._conn.in_transaction


def test_reorder_pulses_path(store):
    ps = _mk_pulses(store, 3)
    ids = [x.id for x in ps]
    store.reorder_pulses(list(reversed(ids)))
    assert [x.id for x in store.list_pulses()] == list(reversed(ids))
    with pytest.raises(UnknownPulse):
        store.reorder_pulses(["nope"])
    assert not store._conn._conn.in_transaction


def test_delete_pulse_path(store):
    p = _mk_pulses(store, 1)[0]
    store.delete_pulse(p.id)
    with pytest.raises(UnknownPulse):
        store.get_pulse(p.id)
    assert not store._conn._conn.in_transaction
