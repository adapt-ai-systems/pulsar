"""Pulse list + long-press drag regression test against the local Vite server.

Covers the drawer list, the list in full-screen mode and the phone Wall:
search, sort, fold, open a pulse, drag a pulse onto a workspace, reorder
a workspace; on the Wall, long-press drag of a card and of a workspace chip.

Run: python3 tests/browser/pulse_list.py
Requires Playwright + Chromium. All /api calls are stubbed (writes go to an
in-memory model, never to a daemon). Set PULSAR_WEB_URL to the Vite server.
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = os.environ.get("PULSAR_WEB_URL", "http://127.0.0.1:5292")
CHROME = os.environ.get("CHROMIUM_BIN")  # default: Playwright's own chromium


def seed():
    def pulse(id, title, grid, pos, **kw):
        return dict(id=id, title=title, model="gpt-6-luna", vessel="codex",
                    state="running", settings={}, grid_id=grid, position=pos,
                    created_at=f"2026-10-0{pos + 1}T10:00:00Z",
                    updated_at=f"2026-10-0{pos + 1}T12:00:00Z", **kw)
    pulses = [
        pulse("alpha", "Alpha pulse", "work", 0),
        pulse("bravo", "Bravo pulse", "work", 1),
        pulse("charlie", "Charlie pulse", "ops", 2),
        pulse("delta", "Delta pulse", None, 3),
    ]
    grids = [dict(id="work", name="WORK", position=0),
             dict(id="ops", name="OPS", position=1),
             dict(id="side", name="SIDE", position=2, parent_id="work")]
    return pulses, grids


class Stub:
    def __init__(self):
        self.pulses, self.grids = seed()
        self.writes = []  # (method, path, body)

    async def route(self, r):
        req = r.request
        path = req.url.split("/api")[-1].split("?")[0]
        body = None
        if req.method != "GET":
            body = req.post_data_json if req.post_data else None
            self.writes.append((req.method, path, body))
        value = []
        if req.method == "GET":
            if path == "/pulses":
                value = list(self.pulses)
            elif path == "/grids":
                value = list(self.grids)
            elif path == "/inbox/unread-count":
                value = {"count": 0}
        elif path.startswith("/pulses/") and path.endswith("/grid"):
            pid = path.split("/")[2]
            for p in self.pulses:
                if p["id"] == pid:
                    p["grid_id"] = body["grid_id"]
        elif path == "/grids/reorder":
            by = {g["id"]: g for g in self.grids}
            self.grids = [by[i] for i in body["ids"]]
        elif path == "/pulses/reorder":
            by = {p["id"]: p for p in self.pulses}
            self.pulses = [by[i] for i in body["ids"]]
        await r.fulfill(json={"ok": True, "value": value})


async def new_page(browser, stub, *, mobile=False):
    kw = dict(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True) \
        if mobile else dict(viewport={"width": 1280, "height": 800})
    page = await browser.new_page(**kw)
    await page.add_init_script("window.WebSocket = class {close(){} send(){}};")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.errors = errors
    await page.route("**/api/**", stub.route)
    await page.goto(URL + "/")
    return page


async def hold_drag(page, src, dst, *, dst_frac=(0.5, 0.5), steps=8):
    """Mouse hold (past LONG_PRESS_MS), carry, release over dst."""
    sb = await src.bounding_box()
    db = await dst.bounding_box()
    sx, sy = sb["x"] + sb["width"] * 0.3, sb["y"] + sb["height"] / 2
    dx, dy = db["x"] + db["width"] * dst_frac[0], db["y"] + db["height"] * dst_frac[1]
    await page.mouse.move(sx, sy)
    await page.mouse.down()
    await page.wait_for_timeout(450)
    db = await dst.bounding_box()
    dx, dy = db["x"] + db["width"] * dst_frac[0], db["y"] + db["height"] * dst_frac[1]
    await page.mouse.move(dx, dy, steps=steps)
    await page.wait_for_timeout(60)
    await page.mouse.up()
    await page.wait_for_timeout(200)


async def titles(page, sel):
    return await page.locator(sel).evaluate_all(
        "els => els.map(e => e.querySelector('.st')?.firstChild?.textContent.trim())")


# --- list scenarios (the same steps run on the drawer and in full screen) --- #

async def list_scenario(page, stub, *, root, row, head, search, sort_btn,
                        fold_btn, label):
    """root: container locator; row/head: selectors inside it."""
    rows = f"{root} {row}"
    heads = f"{root} {head}"
    # all four pulses listed, three workspace heads + no-workspace head
    await page.wait_for_selector(rows)
    assert len(await page.locator(rows).all()) == 4, label

    # search filters rows, clearing restores them
    await page.locator(search).fill("charlie")
    await page.wait_for_timeout(150)
    assert await titles(page, rows) == ["Charlie pulse"], (label, await titles(page, rows))
    await page.locator(search).fill("")
    await page.wait_for_timeout(150)
    assert len(await page.locator(rows).all()) == 4, label

    # sort by name: within the WORK section Alpha stays before Bravo, and
    # switching to "created" puts the newest first. The no-grid pulse is last.
    await page.locator(sort_btn("name")).click()
    await page.wait_for_timeout(150)
    assert await titles(page, rows) == ["Alpha pulse", "Bravo pulse", "Charlie pulse", "Delta pulse"], \
        (label, await titles(page, rows))
    await page.locator(sort_btn("created")).click()
    await page.wait_for_timeout(150)
    got = await titles(page, rows)
    assert got.index("Bravo pulse") < got.index("Alpha pulse"), (label, got)

    # fold: folding WORK hides its direct pulses and persists the key
    work_fold = page.locator(f'{heads}[data-ws-id="work"] {fold_btn}')
    await work_fold.click()
    await page.wait_for_timeout(150)
    got = await titles(page, rows)
    assert "Alpha pulse" not in got and "Charlie pulse" in got, (label, got)
    folds = json.loads(await page.evaluate("localStorage.getItem('pulsar.drawerFolds')"))
    assert folds.get("work") is True, (label, folds)
    await work_fold.click()
    await page.wait_for_timeout(150)
    assert "Alpha pulse" in await titles(page, rows), label

    # drag onto "no workspace" (Alpha -> null)
    stub.writes.clear()
    await hold_drag(page, page.locator(rows, has_text="Alpha pulse"),
                    page.locator(f'{heads}[data-ws-id=""]'))
    moved = [w for w in stub.writes if w[1] == "/pulses/alpha/grid"]
    assert moved and moved[0][2] == {"grid_id": None}, (label, stub.writes)

    # drag a pulse onto a workspace header (Delta -> OPS)
    stub.writes.clear()
    await hold_drag(page, page.locator(rows, has_text="Delta pulse"),
                    page.locator(f'{heads}[data-ws-id="ops"]'))
    moved = [w for w in stub.writes if w[1] == "/pulses/delta/grid"]
    assert moved and moved[0][2] == {"grid_id": "ops"}, (label, stub.writes)
    # the click that follows the release was swallowed (no pane opened/closed)
    await page.wait_for_timeout(300)

    # reorder a workspace: hold OPS, drop on the top half of WORK
    stub.writes.clear()
    await hold_drag(page, page.locator(f'{heads}[data-ws-id="ops"]'),
                    page.locator(f'{heads}[data-ws-id="work"]'), dst_frac=(0.5, 0.15))
    order = [w for w in stub.writes if w[1] == "/grids/reorder"]
    assert order and order[0][2]["ids"] == ["ops", "work", "side"], (label, stub.writes)

    # a hold that moves early is a scroll, not a drag
    stub.writes.clear()
    sb = await page.locator(rows, has_text="Bravo pulse").bounding_box()
    await page.mouse.move(sb["x"] + 40, sb["y"] + sb["height"] / 2)
    await page.mouse.down()
    await page.mouse.move(sb["x"] + 40, sb["y"] + sb["height"] / 2 + 40, steps=4)
    await page.wait_for_timeout(450)
    await page.mouse.up()
    await page.wait_for_timeout(150)
    assert not [w for w in stub.writes if w[1].endswith("/grid")], (label, stub.writes)


async def drawer(browser):
    stub = Stub()
    page = await new_page(browser, stub)
    await page.wait_for_selector('[data-pulse-id="alpha"]')  # hydrated
    await page.locator('nav.rail button.tab[aria-label="all pulses"]').click()
    await page.wait_for_selector("aside.drawer")
    await list_scenario(
        page, stub, root="aside.drawer", row=".srow", head=".sec-head",
        search='aside.drawer input[aria-label="search pulses"]',
        sort_btn=lambda k: f'aside.drawer .sort-opt:text-is("{k}")',
        fold_btn=".fold", label="drawer")
    # open a pulse by clicking its row: the row is a normal click target
    await page.locator("aside.drawer .srow", has_text="Charlie pulse").click()
    await page.wait_for_function(
        "() => document.querySelector('[data-pulse-id=\"charlie\"]')")
    assert not page.errors, page.errors
    await page.close()


async def window(browser):
    stub = Stub()
    page = await new_page(browser, stub)
    # open a pane full screen from its head button (alt-click = full screen)
    await page.wait_for_selector('[data-pulse-id="charlie"]')
    pane = page.locator('[data-pulse-id="charlie"]').first
    await pane.locator('button[aria-label="expand pane"], button[aria-label="open pane full screen"]').first.click(
        modifiers=["Alt"])
    await page.wait_for_selector('.fs-stage [data-pulse-id="charlie"]')
    # full screen lives inside the pane area: no overlay window, and the
    # list is the app's own docked list
    assert await page.locator(".float-pane").count() == 0
    await page.locator('nav.rail button.tab[aria-label="all pulses"]').click()
    await page.wait_for_selector("aside.drawer")
    await list_scenario(
        page, stub, root="aside.drawer", row=".srow", head=".sec-head",
        search='aside.drawer input[aria-label="search pulses"]',
        sort_btn=lambda k: f'aside.drawer .sort-opt:text-is("{k}")',
        fold_btn=".fold", label="full screen")
    # a row click switches the full-screen pulse
    await page.locator("aside.drawer .srow", has_text="Bravo pulse").click()
    await page.wait_for_selector('.fs-stage [data-pulse-id="bravo"]')
    # the pane head button leaves full screen
    await page.locator('.fs-stage button[aria-label="leave full screen"]').click()
    await page.wait_for_function("() => !document.querySelector('.fs-stage')")
    assert not page.errors, page.errors
    await page.close()


async def wall(browser):
    stub = Stub()
    page = await new_page(browser, stub, mobile=True)
    await page.wait_for_selector(".deck .slide .pane")
    # open the overview the way the phone does: double-tap the pane head
    head = page.locator(".deck .slide .pane .pane-head .title-row").first
    await head.tap()
    await page.wait_for_timeout(120)
    await head.tap()
    await page.wait_for_selector(".wall")
    cards = page.locator(".wall-card")
    assert await cards.count() == 4

    async def drag(src, dst, frac=(0.5, 0.5)):
        # Touch long-press drag over CDP (real touch events, pointerType=touch)
        cdp = await page.context.new_cdp_session(page)
        sb = await src.bounding_box()
        db = await dst.bounding_box()
        sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
        dx, dy = db["x"] + db["width"] * frac[0], db["y"] + db["height"] * frac[1]
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": sx, "y": sy}]})
        await page.wait_for_timeout(600)
        # The hold swaps the header text, so the layout shifts: aim at where
        # the target is now, like a finger would.
        db = await dst.bounding_box()
        dx, dy = db["x"] + db["width"] * frac[0], db["y"] + db["height"] * frac[1]
        for i in range(1, 9):
            await cdp.send("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [{"x": sx + (dx - sx) * i / 8, "y": sy + (dy - sy) * i / 8}]})
            await page.wait_for_timeout(15)
        # settle: re-aim at the target's current spot until it stops moving
        for _ in range(10):
            db = await dst.bounding_box()
            nx, ny = db["x"] + db["width"] * frac[0], db["y"] + db["height"] * frac[1]
            if (nx, ny) == (dx, dy):
                break
            dx, dy = nx, ny
            await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": dx, "y": dy}]})
            await page.wait_for_timeout(60)
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        await page.wait_for_timeout(250)
        await cdp.detach()

    # card -> workspace chip
    stub.writes.clear()
    await drag(page.locator('.wall-card[data-card-drop="delta"]'),
               page.locator('.ws-chip[data-ws-drop="ops"]'))
    moved = [w for w in stub.writes if w[1] == "/pulses/delta/grid"]
    assert moved and moved[0][2] == {"grid_id": "ops"}, ('card->ops', stub.writes)
    assert await page.locator(".wall").count() == 1, "wall must stay open after a drag"

    # card -> ALL chip (out of every workspace)
    stub.writes.clear()
    await drag(page.locator('.wall-card[data-card-drop="alpha"]'),
               page.locator('.ws-chip[data-ws-drop=""]'))
    moved = [w for w in stub.writes if w[1] == "/pulses/alpha/grid"]
    assert moved and moved[0][2] == {"grid_id": None}, ('card->ALL', stub.writes)

    # card -> another card (reorder)
    stub.writes.clear()
    await page.locator('.ws-chip[data-ws-drop=""]').tap()  # show every pulse
    await page.wait_for_timeout(200)
    await drag(page.locator('.wall-card[data-card-drop="delta"]'),
               page.locator('.wall-card[data-card-drop="alpha"]'))
    assert [w for w in stub.writes if w[1] == "/pulses/reorder"], ('card->card', stub.writes)

    # chip -> chip (workspace reorder)
    stub.writes.clear()
    await drag(page.locator('.ws-chip[data-ws-drop="ops"]'),
               page.locator('.ws-chip[data-ws-drop="work"]'))
    order = [w for w in stub.writes if w[1] == "/grids/reorder"]
    assert order and order[0][2]["ids"][0] == "ops", ('chip->chip', stub.writes)

    # a plain tap on a card still picks it (closes the wall). Wait out the
    # swallow-next-click window (400 ms) left armed by the last drag.
    await page.wait_for_timeout(600)
    await page.locator('.wall-card[data-card-drop="bravo"]').tap()
    await page.wait_for_function("() => !document.querySelector('.wall')")
    assert not page.errors, page.errors
    await page.close()


async def main():
    async with async_playwright() as playwright:
        kw = {"args": ["--no-sandbox"]}
        if CHROME:
            kw["executable_path"] = CHROME
        browser = await playwright.chromium.launch(**kw)
        failed = 0
        try:
            for name, fn in (("drawer", drawer), ("window", window), ("wall", wall)):
                try:
                    await fn(browser)
                    print("PASS", name, flush=True)
                except Exception as exc:  # report every scenario, then fail
                    failed += 1
                    print("FAIL", name, repr(exc), flush=True)
        finally:
            await browser.close()
        raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
