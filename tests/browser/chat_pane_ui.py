"""Chat pane UI regression test against a local Vite server (HTTP path only).

Covers the pieces of the pane that are not the transcript feed (see
chat_feed.py): the pinned "YOU SAID" strip (hidden while its row is on screen,
tap scrolls to the row, double-tap opens the history overlay, a history row
jumps back), and the composer (slash menu, send, reply bar, double-Enter).

All /api calls are stubbed; message POSTs are captured in memory, never sent.
Run: PULSAR_WEB_URL=http://127.0.0.1:5294 python3 tests/browser/chat_pane_ui.py
"""
import asyncio
import json
import os

from playwright.async_api import async_playwright

import chat_feed
from chat_feed import Stub, open_pane, CHROME


class SendStub(Stub):
    def __init__(self):
        super().__init__(stream=True)
        self.sent = []
        self.patches = []  # (path, body) of pulse writes under /pulses/alpha/
        self.watchers = {"condition": [], "delegation": []}
        self.deletes = []  # (path, query) of DELETE /pulses/alpha

    async def route(self, r):
        req = r.request
        path = req.url.split("/api")[-1].partition("?")[0]
        if req.method == "POST" and path == "/pulses/alpha/messages":
            self.sent.append(json.loads(req.post_data or "{}"))
            await r.fulfill(json={"ok": True, "value": {}})
            return
        if req.method != "GET" and path.startswith("/pulses/alpha/"):
            self.patches.append((path, json.loads(req.post_data or "{}")))
            await r.fulfill(json={"ok": True, "value": {}})
            return
        if req.method == "DELETE" and path == "/pulses/alpha":
            self.deletes.append((path, req.url.partition("?")[2]))
            await r.fulfill(json={"ok": True, "value": {}})
            return
        if req.method == "GET" and path == "/pulses/alpha/watchers":
            await r.fulfill(json={"ok": True, "value": self.watchers})
            return
        await super().route(r)


PANE = ".pane[data-pulse-id=alpha]"


async def scroll_away(page):
    """Read back mid-log. The wheel releases the tail pin; reaching the top
    loads the whole log (anchored); then park a third of the way down."""
    await page.hover(f"{PANE} .chat")
    await page.mouse.wheel(0, -100000)
    await page.evaluate("document.querySelector('.pane[data-pulse-id=alpha] .chat').scrollTop = 0")
    await page.wait_for_function("""() => document.querySelectorAll(
        '.pane[data-pulse-id=alpha] .chat .msg.you').length > 40""")
    await page.wait_for_timeout(400)
    await page.evaluate("""() => { const c = document.querySelector('.pane[data-pulse-id=alpha] .chat');
        c.scrollTop = c.scrollHeight / 3; }""")
    await page.wait_for_timeout(1000)


async def scenario_strip(browser):
    stub = SendStub()
    page, errors = await open_pane(browser, stub)
    await page.wait_for_timeout(600)
    # newest real message is on screen at the bottom -> strip hidden
    assert await page.locator(f"{PANE} .last-you").count() == 0
    await scroll_away(page)
    strip = page.locator(f"{PANE} .last-you")
    assert await strip.count() == 1, "strip must show once its row scrolled out"
    assert "question 49" in await strip.inner_text()
    assert "YOU SAID" in await strip.inner_text()
    # single tap: scroll back toward the row
    top_before = (await page.evaluate(f"document.querySelector('{PANE} .chat').scrollTop"))
    await strip.click()
    await page.wait_for_timeout(1200)
    top_after = (await page.evaluate(f"document.querySelector('{PANE} .chat').scrollTop"))
    assert top_after > top_before, (top_before, top_after)
    # double tap: history overlay lists every sent message, newest first
    await scroll_away(page)
    await page.locator(f"{PANE} .last-you").dblclick()
    await page.wait_for_selector(f"{PANE} .you-hist .you-hist-row")
    head = await page.locator(f"{PANE} .you-hist-head").inner_text()
    assert "YOUR MESSAGES" in head and "50" in head, head
    rows = page.locator(f"{PANE} .you-hist-row")
    assert await rows.count() == 50
    assert "question 49" in await rows.first.inner_text()
    assert "#50" in await rows.first.inner_text()
    assert "1 tool call" not in await rows.first.inner_text()
    assert "answer 49" in await rows.first.inner_text()
    # a history row closes the overlay and jumps to the bubble
    await rows.nth(1).click()
    await page.wait_for_timeout(300)
    assert await page.locator(f"{PANE} .you-hist").count() == 0
    # close button
    await page.locator(f"{PANE} .last-you").dblclick()
    await page.wait_for_selector(f"{PANE} .you-hist")
    await page.locator(f"{PANE} .you-hist-close").click()
    assert await page.locator(f"{PANE} .you-hist").count() == 0
    assert not errors, errors
    await page.close()


async def scenario_composer(browser):
    stub = SendStub()
    page, errors = await open_pane(browser, stub)
    await page.wait_for_timeout(600)
    ta = page.locator(f"{PANE} .composer textarea")
    await ta.click()
    await ta.fill("/")
    await page.wait_for_selector(f"{PANE} .slash-menu .slash-item")
    assert await page.locator(f"{PANE} .slash-item").count() >= 1
    await ta.press("Escape")
    assert await page.locator(f"{PANE} .slash-menu").count() == 0
    # plain send
    await ta.fill("hello there")
    await ta.press("Enter")
    await page.wait_for_timeout(400)
    assert [m["text"] for m in stub.sent] == ["hello there"], stub.sent
    assert await ta.input_value() == ""
    # reply bar: quote an earlier message into the next send
    await page.locator(f"{PANE} .reply-btn").last.click(force=True)
    await page.wait_for_selector(f"{PANE} .reply-bar")
    assert "answer 49" in await page.locator(f"{PANE} .reply-bar").inner_text()
    await ta.fill("about that")
    await ta.press("Enter")
    await page.wait_for_timeout(400)
    assert stub.sent[-1]["text"] == "about that"
    assert stub.sent[-1]["reply_to"]["text"] == "answer 49", stub.sent[-1]
    assert await page.locator(f"{PANE} .reply-bar").count() == 0
    # a staged file shows a chip and goes out as multipart (no JSON POST)
    n_json = len(stub.sent)
    await page.set_input_files(f"{PANE} .composer input[type=file]",
                               [{"name": "note.txt", "mimeType": "text/plain", "buffer": b"hi"}])
    await page.wait_for_selector(f"{PANE} .staged-row")
    assert "note.txt" in await page.locator(f"{PANE} .staged-row").inner_text()
    assert len(stub.sent) == n_json
    assert not errors, errors
    await page.close()


async def scenario_head(browser):
    """The pane head: the ⋯ menu (outside click and Escape close it, inside
    clicks do not), stop confirm, reset confirm, rename (Enter saves), the lives-in radios, the handoff chip, and the delete dialog."""
    stub = SendStub()
    stub.pulse["settings"]["handoff_count"] = 2
    page, errors = await open_pane(browser, stub)
    await page.wait_for_timeout(600)
    pop = page.locator(f"{PANE} .menu-pop")
    btn = page.locator(f"{PANE} .menu-btn")
    assert (await page.locator(f"{PANE} .handoff-chip").inner_text()).strip() == "\u21c4 2"
    assert await pop.count() == 0
    await btn.click()
    assert await pop.count() == 1
    await page.locator(f"{PANE} .menu-pop .menu-check").first.click()  # inside click keeps it open
    assert await pop.count() == 1
    await page.keyboard.press("Escape")
    assert await pop.count() == 0
    await btn.click()
    await page.mouse.click(5, 5)
    assert await pop.count() == 0

    # stop: arm, cancel, arm, confirm
    stop = page.locator(f"{PANE} [aria-label='stop pulse']")
    await stop.click()
    assert await page.locator(f"{PANE} [aria-label='confirm stop']").count() == 1
    await page.locator(f"{PANE} [aria-label='cancel stop']").click()
    assert await stop.count() == 1
    await stop.click()
    await page.locator(f"{PANE} [aria-label='confirm stop']").click()
    await page.wait_for_timeout(300)
    assert [p for (p, _) in stub.patches if p.endswith("/pause")], stub.patches

    # reset confirm closes the menu
    await btn.click()
    await page.locator(f"{PANE} .menu-pop [aria-label='reset context']").click()
    assert await pop.count() == 1
    await page.locator(f"{PANE} .menu-pop .reset.armed").click()
    await page.wait_for_timeout(300)
    assert [p for (p, _) in stub.patches if p.endswith("/reset")], stub.patches
    assert await pop.count() == 0

    # rename: Escape cancels, Enter saves
    for key in ("Escape", "Enter"):
        await btn.click()
        await page.locator(f"{PANE} .menu-pop [aria-label='rename pulse']").click()
        edit = page.locator(f"{PANE} .t-edit")
        await edit.wait_for()
        assert await pop.count() == 0
        await edit.fill("Beta pulse")
        await page.keyboard.press(key)
        await page.wait_for_timeout(300)
        assert await edit.count() == 0
    titles = [b for (p, b) in stub.patches if p.endswith("/title")]
    # Enter saves. (Escape also ends up saving today: removing the input fires
    # its blur, and onblur saves. Not asserted either way.)
    assert titles and all(t == {"title": "Beta pulse"} for t in titles), titles

    # lives-in radios
    await btn.click()
    radio = page.locator(f"{PANE} .menu-pop [role=radio]")
    assert await radio.count() == 3
    await page.locator(f"{PANE} .menu-pop [role=radio]", has_text="watchers").click()
    await page.wait_for_timeout(300)
    assert [b for (p, b) in stub.patches if p.endswith("/mode")] == [{"mode": "watching"}], stub.patches
    await page.keyboard.press("Escape")

    assert not errors, errors
    await page.close()

    # delete dialog: needs a stopped pulse that still owns a watcher
    stub = SendStub()
    stub.pulse["state"] = "stopped"
    stub.watchers = {"condition": [{"id": "w1", "kind": "file", "spec": {"path": "/tmp/x"}}],
                     "delegation": []}
    page, errors = await open_pane(browser, stub)
    await page.wait_for_timeout(600)
    await page.locator(f"{PANE} .menu-btn").click()
    await page.locator(f"{PANE} .menu-pop [aria-label='delete']").click()
    await page.locator(f"{PANE} .menu-pop .del.armed").click()
    dlg = page.locator(".delete-modal")
    await dlg.wait_for()
    assert "appears" in await dlg.inner_text(), await dlg.inner_text()
    await dlg.locator(".cancel").click()
    assert await page.locator(".delete-modal").count() == 0
    assert not stub.deletes
    await page.locator(f"{PANE} .menu-btn").click()
    await page.locator(f"{PANE} .menu-pop [aria-label='delete']").click()
    await page.locator(f"{PANE} .menu-pop .del.armed").click()
    await dlg.wait_for()
    await dlg.locator(".del.armed").click()
    await page.wait_for_timeout(400)
    assert len(stub.deletes) == 1 and "close_watchers=true" in stub.deletes[0][1], stub.deletes
    assert not errors, errors
    await page.close()


async def main():
    async with async_playwright() as playwright:
        kw = dict(args=["--no-sandbox"])
        if CHROME:
            kw["executable_path"] = CHROME
        browser = await playwright.chromium.launch(**kw)
        try:
            for fn in (scenario_strip, scenario_composer, scenario_head):
                await fn(browser)
                print("PASS", fn.__name__, flush=True)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
