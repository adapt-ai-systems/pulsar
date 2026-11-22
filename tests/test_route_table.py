"""Route-table invariants of ``create_app`` (kept after splitting ``daemon/app.py``).

Starlette dispatches on the first FULL match (path and method) and answers 405
with the ``Allow`` header of the first PARTIAL match (path only). So the
registration order of routes whose paths can match the same URL is behaviour,
whatever their methods. The route groups in ``daemon/routes`` are included in
an order that keeps it. These tests pin the pairs found while splitting the
file:

* ``test_registered_before``: explicit "A is registered before B" pairs;
* ``test_405_allow_header``: the 405 status and ``Allow`` header of probes
  that no route accepts, including ``DELETE /pulses/x/handoff`` -> ``PATCH``.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from pulsar.daemon.app import create_app
from pulsar.store import Store


def _flat_routes(router) -> list:
    """The routes in dispatch order. This FastAPI keeps ``include_router`` as a
    nested ``_IncludedRouter`` instead of copying the routes in; matching is
    first-full-match / first-partial-match across the nested order, which is
    the same as the flattened order (the prefix is empty everywhere)."""
    out = []
    for r in router.routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            assert not r.include_context.prefix, "prefixed include_router"
            out.extend(_flat_routes(inner))
        else:
            out.append(r)
    return out


def _index(app, method: str, path: str) -> int:
    for i, r in enumerate(_flat_routes(app.router)):
        if getattr(r, "path", None) == path and method in (
                getattr(r, "methods", None) or ()):
            return i
    raise AssertionError(f"no route {method} {path}")


@pytest.fixture()
def app(tmp_path):
    store = Store(str(tmp_path / "pulsar.db"))
    try:
        yield create_app(store=store)
    finally:
        store.close()


# (earlier, later): every path-overlapping pair with different path strings,
# plus the same-path pair that decides the handoff 405.
ORDER = [
    (("PATCH", "/pulses/{pulse_id}/handoff"),
     ("POST", "/pulses/{pulse_id}/handoff")),
    (("POST", "/grids/reorder"), ("PATCH", "/grids/{grid_id}")),
    (("POST", "/grids/reorder"), ("DELETE", "/grids/{grid_id}")),
    (("POST", "/pulses/reorder"), ("GET", "/pulses/{pulse_id}")),
    (("POST", "/pulses/reorder"), ("DELETE", "/pulses/{pulse_id}")),
    (("GET", "/pulses/{pulse_id}"), ("POST", "/pulses/archive-idle")),
    (("POST", "/pulses/archive-idle"), ("DELETE", "/pulses/{pulse_id}")),
    (("DELETE", "/watches/{watch_id}"), ("POST", "/watches/conditions")),
    (("DELETE", "/watches/{watch_id}"), ("GET", "/watches/conditions")),
]


@pytest.mark.parametrize("first,second", ORDER,
                         ids=[f"{a[1]}<{b[1]}:{a[0]}/{b[0]}" for a, b in ORDER])
def test_registered_before(app, first, second):
    assert _index(app, *first) < _index(app, *second)


# method, URL -> (status, Allow). No route accepts the method; Allow comes
# from the first partial match in registration order.
PROBES_405 = {
    ("DELETE", "/pulses/x/handoff"): "PATCH",
    ("PUT", "/pulses/x"): "GET",
    ("PUT", "/pulses/archive-idle"): "GET",
    ("PUT", "/pulses/reorder"): "POST",
    ("PUT", "/grids/x"): "PATCH",
    ("PUT", "/grids/reorder"): "POST",
    ("PUT", "/watches/conditions"): "DELETE",
    ("PUT", "/watches/x"): "DELETE",
}


@pytest.mark.parametrize("method,url", list(PROBES_405),
                         ids=[f"{m} {u}" for m, u in PROBES_405])
def test_405_allow_header(app, method, url):
    resp = TestClient(app, raise_server_exceptions=False).request(method, url)
    assert resp.status_code == 405
    assert resp.headers["allow"] == PROBES_405[(method, url)]
