"""Read-only phone navigation regression test against the local Vite server.

Run: python3 tests/browser/mobile_restore.py
Requires Playwright + Chromium. All /api calls are stubbed; no live data changes.
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = os.environ.get("PULSAR_WEB_URL", "http://127.0.0.1:5292")
CHROME = os.environ.get("CHROMIUM_BIN") or None


async def scenario(browser, *, child=False, hidden=False, missing=False,
                   pulse_delay=0, grid_delay=0, deep_link=False, reload=False):
    page = await browser.new_page(viewport={"width": 390, "height": 844},
                                  is_mobile=True, has_touch=True)
    await page.add_init_script("""
        if (!localStorage.getItem('seeded')) {
            localStorage.setItem('pulsar.selectedGrid', 'work');
            localStorage.setItem('pulsar.deckPulse.work', 'second');
            localStorage.setItem('seeded', '1');
        }
        window.WebSocket = class {close(){} send(){}};
    """)
    pulses = [dict(id=id, title=id, model="gpt-6-luna", vessel="codex",
                   state="stopped", settings={}, grid_id="work", position=i)
              for i, id in enumerate(["first", "second", "third"])]
    grids = [dict(id="work", name="WORK", position=0)]
    if child:
        pulses[1]["grid_id"] = "child"
        grids.append(dict(id="child", name="CHILD", position=1, parent_id="work"))
    if hidden:
        pulses[1]["settings"] = {"mode": "watching"}
    if missing:
        pulses.pop(1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def route(r):
        assert r.request.method == "GET", "Navigation must not mutate live data"
        path = r.request.url.split("/api")[-1].split("?")[0]
        if path == "/pulses":
            await asyncio.sleep(pulse_delay)
            value = pulses
        elif path == "/grids":
            await asyncio.sleep(grid_delay)
            value = grids
        elif path == "/inbox/unread-count":
            value = {"count": 0}
        else:
            value = []
        await r.fulfill(json={"ok": True, "value": value})

    await page.route("**/api/**", route)
    await page.goto(URL + ("/?pulse-focus=first" if deep_link else "/"))
    expected = "first" if deep_link or missing else "second"
    await page.wait_for_function("""(id) => [...document.querySelectorAll('.deck .slide')]
        .some(e => e.style.transform === 'none' && e.querySelector('.pane')?.dataset.pulseId === id)
    """, arg=expected)
    await page.wait_for_function("(id) => localStorage.getItem('pulsar.deckPulse.work') === id",
                                 arg=expected)
    assert await page.evaluate("localStorage.getItem('pulsar.selectedGrid')") == "work"
    if reload:
        # Swipe to another pane, then reload without reseeding storage.
        await page.locator(".deck").dispatch_event("wheel", {"deltaX": 100, "deltaY": 0})
        await page.wait_for_function("localStorage.getItem('pulsar.deckPulse.work') === 'third'")
        await page.reload()
        await page.wait_for_function("""() => [...document.querySelectorAll('.deck .slide')]
            .some(e => e.style.transform === 'none' && e.querySelector('.pane')?.dataset.pulseId === 'third')
        """)
    assert not errors, errors
    await page.close()


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME,
                                                   args=["--no-sandbox"])
        cases = [
            {"reload": True},
            {"child": True, "grid_delay": .6},
            {"child": True, "pulse_delay": .6},
            {"hidden": True},
            {"missing": True},
            {"deep_link": True, "grid_delay": .4},
        ]
        try:
            for case in cases:
                await scenario(browser, **case)
                print("PASS", json.dumps(case), flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
