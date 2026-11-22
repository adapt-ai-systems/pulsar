"""Pulse reveal regression test against the local Vite server.

Covers opening a stopped pulse (while "running only" is on) and a shelved
pulse (mode=watching) from each reveal entry point: drawer,
metrics bar, activity log, notification deep link /?pulse-focus=<id>,
and new-pulse create.

Run: PULSAR_WEB_URL=http://127.0.0.1:5293 python3 tests/browser/reveal.py
Requires Playwright + Chromium. All /api calls are stubbed; no live data changes.
"""
import asyncio
import os

from playwright.async_api import async_playwright

URL = os.environ.get("PULSAR_WEB_URL", "http://127.0.0.1:5292")


def seed():
    def pulse(id, title, grid="work", pos=0, state="running", mode=None, **kw):
        settings = {"mode": mode} if mode else {}
        return dict(id=id, title=title, model="gpt-6-luna", vessel="codex",
                    state=state, settings=settings, grid_id=grid, position=pos,
                    created_at="2026-10-01T10:00:00Z",
                    updated_at="2026-10-01T12:00:00Z", **kw)

    pulses = [
        pulse("host", "Host pulse", "work", 0, state="running"),
        pulse("stop-p", "Stopped pulse", "ops", 1, state="stopped"),
        pulse("shelf-p", "Shelved pulse", "ops", 2, state="running", mode="watching"),
        pulse("busy-shelf", "Busy shelved", "ops", 3, state="running", mode="watching",
              live={"socket_state": "busy", "busy_since": 1700000000}),
    ]
    grids = [
        dict(id="work", name="WORK", position=0),
        dict(id="ops", name="OPS", position=1),
    ]
    return pulses, grids


class Stub:
    def __init__(self, *, created=None):
        self.pulses, self.grids = seed()
        self.created = created or dict(
            id="created-p", title="Created pulse", model="gpt-6-luna",
            vessel="codex", state="stopped", settings={}, grid_id="ops", position=4,
            created_at="2026-10-01T12:00:00Z", updated_at="2026-10-01T12:00:00Z"
        )

    async def route(self, r):
        req = r.request
        path = req.url.split("/api")[-1].split("?")[0]
        if req.method == "POST" and path == "/pulses":
            self.pulses.append(self.created)
            await r.fulfill(json={"ok": True, "value": self.created})
            return
        if path == "/pulses":
            value = list(self.pulses)
        elif path == "/grids":
            value = list(self.grids)
        elif path == "/inbox/unread-count":
            value = {"total": 0, "channels": {}, "threads": {}}
        else:
            value = []
        await r.fulfill(json={"ok": True, "value": value})


async def new_page(browser, stub, *, url_suffix="/", hide_stopped=True):
    page = await browser.new_page(viewport={"width": 1280, "height": 800})
    hs = "1" if hide_stopped else ""
    await page.add_init_script(f"""
        if (!sessionStorage.getItem('seeded')) {{
            localStorage.setItem('pulsar.selectedGrid', 'work');
            if ({'true' if hide_stopped else 'false'}) localStorage.setItem('pulsar.hideStopped', '1');
            else localStorage.removeItem('pulsar.hideStopped');
            sessionStorage.setItem('seeded', '1');
        }}
        window.WebSocket = class {{close(){{}} send(){{}}}};
    """)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors
    await page.route("**/api/**", stub.route)
    await page.route("**/metrics-proxy/**", lambda r: r.fulfill(json={"hosts": []}))
    await page.goto(URL + url_suffix)
    return page


async def pane_visible(page, pulse_id, timeout=1500):
    try:
        await page.wait_for_selector(f'[data-pulse-id="{pulse_id}"]', timeout=timeout)
        return True
    except Exception:
        return False


async def from_drawer(browser, target_id, target_title):
    stub = Stub()
    page = await new_page(browser, stub)
    await page.wait_for_selector('[data-pulse-id="host"]')
    await page.locator('nav.rail button.tab[aria-label="all pulses"]').click()
    await page.wait_for_selector("aside.drawer .srow")
    await page.locator("aside.drawer .srow", has_text=target_title).click()
    vis = await pane_visible(page, target_id)
    assert not page.errors, page.errors
    await page.close()
    return vis


async def from_metrics(browser, target_id, target_title, *, make_busy=False):
    stub = Stub()
    if make_busy:
        for p in stub.pulses:
            if p["id"] == target_id:
                p["state"] = "running"
                p["live"] = {"socket_state": "busy", "busy_since": 1700000000}
    page = await new_page(browser, stub)
    await page.wait_for_selector('[data-pulse-id="host"]')
    await page.locator('.metrics-bar').dblclick()
    await page.wait_for_selector('.metrics-overlay .pulse-link')
    await page.locator('.metrics-overlay .pulse-link', has_text=target_title).click()
    vis = await pane_visible(page, target_id)
    assert not page.errors, page.errors
    await page.close()
    return vis


async def from_deeplink(browser, target_id):
    stub = Stub()
    page = await new_page(browser, stub, url_suffix=f"/?pulse-focus={target_id}")
    vis = await pane_visible(page, target_id)
    assert not page.errors, page.errors
    await page.close()
    return vis


async def from_new_pulse(browser, *, mode=None):
    created = dict(
        id="created-p", title="Created pulse", model="gpt-6-luna",
        vessel="codex", state="stopped",
        settings={"mode": mode} if mode else {},
        grid_id="ops", position=4,
        created_at="2026-10-01T12:00:00Z", updated_at="2026-10-01T12:00:00Z"
    )
    stub = Stub(created=created)
    page = await new_page(browser, stub)
    await page.wait_for_selector('[data-pulse-id="host"]')
    await page.locator('nav.rail button[aria-label="new pulse"]').click()
    await page.wait_for_selector('#np-title')
    await page.locator('#np-title').fill("Created pulse")
    await page.locator('.modal-actions button.create').click()
    vis = await pane_visible(page, "created-p")
    assert not page.errors, page.errors
    await page.close()
    return vis


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(args=["--no-sandbox"])
        try:
            # Stopped pulse with "running only" on:
            # Today: drawer, metrics, deeplink, new_pulse untick hideStopped;
            # activity log calls focusPulse({tag:'activity'}) which only unticks for drawer.
            assert await from_drawer(browser, "stop-p", "Stopped pulse") is True, "drawer stopped"
            print("PASS drawer stopped", flush=True)

            assert await from_metrics(browser, "stop-p", "Stopped pulse", make_busy=True) is True, "metrics busy"
            print("PASS metrics busy", flush=True)

            assert await from_deeplink(browser, "stop-p") is True, "deeplink stopped"
            print("PASS deeplink stopped", flush=True)

            assert await from_new_pulse(browser, mode=None) is True, "new_pulse stopped"
            print("PASS new_pulse stopped", flush=True)

            # Shelved pulse (mode=watching):
            # Every entry point reveals shelved pulses.
            assert await from_drawer(browser, "shelf-p", "Shelved pulse") is True, "drawer shelved"
            print("PASS drawer shelved", flush=True)

            assert await from_metrics(browser, "busy-shelf", "Busy shelved") is True, "metrics shelved"
            print("PASS metrics shelved", flush=True)

            assert await from_deeplink(browser, "shelf-p") is True, "deeplink shelved"
            print("PASS deeplink shelved", flush=True)

            assert await from_new_pulse(browser, mode="watching") is True, "new_pulse shelved"
            print("PASS new_pulse shelved", flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
