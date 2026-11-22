"""Chat feed regression test against a local Vite server (HTTP path only).

Covers the pane's transcript feed: tail window, scrollback (older events load
with the view anchored), events held while reading back plus the "new" chip,
catching up, a streamed partial turning into its final text without a
duplicate, and stream off.

The page's WebSocket is disabled (the `mobile_restore.py` pattern), so this
exercises the HTTP poll fallback only. The /live push path is covered by
`web/src/lib/chatfeed.test.mjs`.

Run: PULSAR_WEB_URL=http://127.0.0.1:5294 python3 tests/browser/chat_feed.py
Requires Playwright + Chromium. All /api calls are stubbed (in-memory event
log, never a daemon). Set PULSAR_WEB_URL to the Vite server.
Set CHAT_FEED_SHOTS=<dir> to save screenshots of the pane (desktop + 402px).
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

URL = os.environ.get("PULSAR_WEB_URL", "http://127.0.0.1:5292")
CHROME = os.environ.get("CHROMIUM_BIN")  # default: Playwright's own chromium
SHOTS = os.environ.get("CHAT_FEED_SHOTS")
N_EVENTS = 150


def ts(i):
    return f"2026-10-01T10:{i // 60:02d}:{i % 60:02d}Z"


def make_events():
    evs = []
    for k in range(N_EVENTS // 3):
        evs.append(dict(type="user_message", ts=ts(len(evs)), text=f"question {k}"))
        evs.append(dict(type="assistant_text", ts=ts(len(evs)), text=f"answer {k}"))
        evs.append(dict(type="done", ts=ts(len(evs))))
    return evs


class Stub:
    def __init__(self, stream=True):
        self.events = make_events()
        self.partial = None
        self.requests = []  # (method, path, query)
        self.pulse = dict(id="alpha", title="Alpha pulse", model="gpt-6-luna",
                          vessel="codex", state="running", grid_id=None, position=0,
                          settings={"stream_partials": stream},
                          created_at="2026-10-01T09:00:00Z",
                          updated_at="2026-10-01T12:00:00Z")

    def calls(self, name):
        return [q for (_, p, q) in self.requests if p.endswith(name)]

    async def route(self, r):
        req = r.request
        path, _, query = req.url.split("/api")[-1].partition("?")
        self.requests.append((req.method, path, query))
        if req.method != "GET":  # never a pulse write
            assert not path.startswith("/pulses/alpha/"), f"unexpected write {req.method} {path}"
            await r.fulfill(json={"ok": True, "value": {}})
            return
        value = []
        if path == "/pulses":
            value = [self.pulse]
        elif path == "/pulses/alpha/events":
            q = dict(kv.split("=") for kv in query.split("&") if kv)
            tail = int(q["tail"])
            size = len(self.events)
            if q.get("after") == str(size):
                value = dict(events=[], count=0, offset=size, unchanged=True)
            else:
                win = self.events[-tail:]
                value = dict(events=win, count=len(win), offset=size)
        elif path == "/pulses/alpha/partial":
            value = dict(partial=self.partial)
        elif path == "/pulses/alpha/reasoning":
            value = dict(reasoning=None)
        elif path == "/pulses/alpha/task-events":
            value = dict(events=[])
        elif path == "/inbox/unread-count":
            value = {"count": 0}
        elif path == "/grids":
            value = []
        await r.fulfill(json={"ok": True, "value": value})


async def open_pane(browser, stub, width=1280, height=800):
    page = await browser.new_page(viewport={"width": width, "height": height})
    await page.add_init_script("window.WebSocket = class {close(){} send(){}};")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.route("**/api/**", stub.route)
    await page.goto(URL + "/")
    await page.wait_for_selector(".pane[data-pulse-id=alpha] .chat .msg")
    return page, errors


async def texts(page):
    return await page.eval_on_selector_all(
        ".pane[data-pulse-id=alpha] .chat .msg .body",
        "els => els.map(e => e.textContent.trim())")


async def scroll_state(page):
    return await page.evaluate("""() => {
        const c = document.querySelector('.pane[data-pulse-id=alpha] .chat');
        return { top: c.scrollTop, height: c.scrollHeight, client: c.clientHeight,
                 fromBottom: c.scrollHeight - c.scrollTop };
    }""")


async def visible_anchor(page):
    """The first message row fully inside the scroll container: (text, offset)."""
    return await page.evaluate("""() => {
        const c = document.querySelector('.pane[data-pulse-id=alpha] .chat');
        const cr = c.getBoundingClientRect();
        for (const el of c.querySelectorAll('.msg')) {
            const r = el.getBoundingClientRect();
            if (r.top >= cr.top + 4)
                return { text: el.textContent.trim().slice(0, 60), offset: r.top - cr.top };
        }
        return null;
    }""")


async def scenario_main(browser):
    stub = Stub(stream=True)
    page, errors = await open_pane(browser, stub)
    await page.wait_for_timeout(600)

    # 1. First load: tail window of 100 events, parked at the newest message.
    assert any("tail=100" in q for q in stub.calls("/events")), stub.requests
    rows = await texts(page)
    assert rows[-1] == "answer 49", rows[-3:]
    assert rows[0] != "question 0", "window must be a tail, not the whole log"
    assert await page.locator(".older-hint").count() == 1
    st = await scroll_state(page)
    assert st["fromBottom"] - st["client"] <= 2, st  # pinned to the bottom
    if SHOTS:
        await page.locator(".pane[data-pulse-id=alpha]").screenshot(path=f"{SHOTS}/desktop-bottom.png")

    # 2. Scroll to the top: older events load, the view stays anchored.
    await page.hover(".pane[data-pulse-id=alpha] .chat")
    await page.mouse.wheel(0, -100000)  # releases the tail, scrolls to the top
    await page.evaluate("document.querySelector('.pane[data-pulse-id=alpha] .chat').scrollTop = 0")
    await page.wait_for_function("""() => document.querySelectorAll(
        '.pane[data-pulse-id=alpha] .chat .msg.you').length > 40""")
    await page.wait_for_timeout(400)
    assert any("tail=500" in q for q in stub.calls("/events")), stub.calls("/events")
    rows = await texts(page)
    assert "question 0" in rows, rows[:3]  # the whole 150-event log is now loaded
    assert await page.locator(".older-hint").count() == 0
    # Anchored: the view did not jump to the top of the (now longer) log. The
    # anchor row before the load was the tail's first rows; after it they sit
    # where scrollTop=0 left them, offset by the prepended rows.
    st = await scroll_state(page)
    assert st["top"] > 0, st

    # 3. Scroll up in the middle, then append an event: held, "new" chip shown.
    await page.evaluate("""() => { const c = document.querySelector(
        '.pane[data-pulse-id=alpha] .chat'); c.scrollTop = c.scrollHeight / 2; }""")
    await page.mouse.wheel(0, -50)
    await page.wait_for_function("""() => !!document.querySelector(
        '.pane[data-pulse-id=alpha] .to-bottom')""")
    # The wheel nudge above starts a smooth scroll; let it settle before reading.
    await page.wait_for_timeout(800)
    before_rows = await texts(page)
    before_anchor = await visible_anchor(page)
    stub.events.append(dict(type="user_message", ts=ts(N_EVENTS), text="late question"))
    stub.events.append(dict(type="assistant_text", ts=ts(N_EVENTS + 1), text="late answer"))
    stub.events.append(dict(type="done", ts=ts(N_EVENTS + 2)))
    await page.wait_for_selector(".pane[data-pulse-id=alpha] .to-bottom.has-new", timeout=8000)
    assert await texts(page) == before_rows, "held events must not touch the DOM"
    _a = await visible_anchor(page)
    assert _a == before_anchor, f"reading position moved {before_anchor} -> {_a}"

    # 4. Catch up: the held events render and the view follows the tail.
    await page.click(".pane[data-pulse-id=alpha] .to-bottom")
    await page.wait_for_function("""() => [...document.querySelectorAll(
        '.pane[data-pulse-id=alpha] .chat .msg .body')].some(e => e.textContent.trim() === 'late answer')""")
    await page.wait_for_timeout(500)
    st = await scroll_state(page)
    assert st["fromBottom"] - st["client"] <= 2, st
    assert await page.locator(".pane[data-pulse-id=alpha] .to-bottom").count() == 0

    # 5. Streamed partial -> final text, no duplicate.
    stub.partial = dict(ts=ts(N_EVENTS + 3), text="streaming hello")
    await page.wait_for_selector(".pane[data-pulse-id=alpha] .msg.ghost", timeout=5000)
    assert "streaming hello" in await page.inner_text(".pane[data-pulse-id=alpha] .msg.ghost")
    stub.partial = dict(ts=ts(N_EVENTS + 3), text="streaming hello world")
    await page.wait_for_function("""() => document.querySelector(
        '.pane[data-pulse-id=alpha] .msg.ghost')?.textContent.includes('hello world')""")
    stub.partial = None
    stub.events.append(dict(type="assistant_text", ts=ts(N_EVENTS + 3), text="streaming hello world"))
    await page.wait_for_function("""() => !document.querySelector('.pane[data-pulse-id=alpha] .msg.ghost')""",
                                 timeout=5000)
    await page.wait_for_timeout(700)
    final = [t for t in await texts(page) if "streaming hello world" in t]
    assert len(final) == 1, f"reply shown {len(final)} times: {final}"
    assert await page.locator(".pane[data-pulse-id=alpha] .msg.ghost").count() == 0
    assert not errors, errors
    await page.close()


async def scenario_stream_off(browser):
    stub = Stub(stream=False)
    page, errors = await open_pane(browser, stub)
    stub.partial = dict(ts=ts(N_EVENTS + 3), text="should never show")
    await page.wait_for_timeout(1500)
    assert await page.locator(".pane[data-pulse-id=alpha] .msg.ghost").count() == 0
    assert not stub.calls("/partial"), "stream off must not poll /partial"
    assert not errors, errors
    await page.close()


async def scenario_mobile(browser):
    if not SHOTS:
        return
    stub = Stub(stream=True)
    page = await browser.new_page(viewport={"width": 402, "height": 844}, is_mobile=True, has_touch=True)
    await page.add_init_script("window.WebSocket = class {close(){} send(){}};")
    await page.route("**/api/**", stub.route)
    await page.goto(URL + "/")
    await page.wait_for_selector(".pane[data-pulse-id=alpha] .chat .msg")
    await page.wait_for_timeout(800)
    await page.screenshot(path=f"{SHOTS}/mobile-bottom.png")
    await page.close()


async def main():
    async with async_playwright() as playwright:
        kw = dict(args=["--no-sandbox"])
        if CHROME:
            kw["executable_path"] = CHROME
        browser = await playwright.chromium.launch(**kw)
        try:
            for fn in (scenario_main, scenario_stream_off, scenario_mobile):
                await fn(browser)
                print("PASS", fn.__name__, flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
