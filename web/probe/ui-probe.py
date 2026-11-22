#!/usr/bin/env python3
"""ui-probe — measure the running Pulsar web app, in one command, no helper pulse.

Measures DOM geometry and computed styles directly with Playwright/Chromium in
seconds instead of spinning up a browser-driving agent for layout checks.

It attaches to the ALREADY RUNNING app (default http://127.0.0.1:5292/, override
via PULSAR_UI_URL or --url) — it does not build, does not boot a server, and
does not touch your logged-in Chrome profile. Anything needing an authenticated
browser session is outside this tool's scope; a headless probe cannot see your
cookies.

    ./ui-probe.py "document.querySelector('.clock').getBoundingClientRect().top"
    ./ui-probe.py --width 390 --json "getComputedStyle(document.querySelector('.chat')).overflowX"
    ./ui-probe.py --localStorage pulsar-fold-noise=0 --expr-file /tmp/probe.js
    ./ui-probe.py --tick '.clock'                 # prove a live element updates
    ./ui-probe.py --contained .clock .nav        # a box inside another box
    ./ui-probe.py --no-cross .clock              # is any rule crossing this box?
    ./ui-probe.py --screenshot /tmp/shot.png --clip 700,0,580,120

Exit: 0 pass, 1 assertion failed, 2 the app is unreachable (the service is down
or mid-restart — that is NOT a UI bug, do not chase it).

Named checks are deliberately NOT a library here. --contained / --no-cross /
--tick are shape questions that hold for any selector pair; app-specific rules
(".msg.dreamer must have an accent") rot the moment markup changes and return a
silent green, so they live in the caller's one-liner until they've earned a home.
"""
import argparse
import json
import os
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sys.exit("ui-probe needs python playwright: pip install playwright")

DEFAULT_URL = os.environ.get("PULSAR_UI_URL", "http://127.0.0.1:5292/")


def rect_js():
    return """(sel) => {
      const el = document.querySelector(sel);
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {top:r.top, left:r.left, bottom:r.bottom, right:r.right,
              width:r.width, height:r.height};
    }"""


def main() -> int:
    ap = argparse.ArgumentParser(description="measure the running Pulsar UI")
    ap.add_argument("expr", nargs="?", help="JS to evaluate in the page")
    ap.add_argument("--expr-file", help="read the JS from a file (multi-line probes)")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=800)
    ap.add_argument("--wait", type=int, default=1500, help="ms to settle after load")
    ap.add_argument("--localStorage", action="append", default=[], metavar="K=V",
                    help="set before load (repeatable) — for prefs like pulsar-fold-noise=0")
    ap.add_argument("--contained", nargs=2, metavar=("INNER", "OUTER"),
                    help="assert inner rect lies fully inside outer rect")
    ap.add_argument("--no-cross", metavar="SEL",
                    help="assert no wide element's bottom border cuts through SEL's box")
    ap.add_argument("--tick", metavar="SEL",
                    help="assert SEL's text changes across --tick-gap ms (a live clock)")
    ap.add_argument("--tick-gap", type=int, default=3000)
    ap.add_argument("--screenshot", metavar="PATH")
    ap.add_argument("--clip", metavar="x,y,w,h", help="clip region for the screenshot")
    args = ap.parse_args()

    expr = args.expr
    if args.expr_file:
        expr = open(args.expr_file).read()
    if args.expr and args.expr_file:
        sys.exit("give an expression OR --expr-file, not both")

    checks = [a for a in ("contained", "no_cross", "tick") if getattr(args, a)]
    if not expr and not checks:
        sys.exit("nothing to do — pass an expression or a check")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(args=["--ignore-certificate-errors"])
        except Exception as exc:
            print(f"UI UNREACHABLE (probe could not start a browser): {exc}")
            return 2
        page = browser.new_page(
            viewport={"width": args.width, "height": args.height}, ignore_https_errors=True
        )
        try:
            if args.localStorage:
                page.goto(args.url, timeout=45000)  # need an origin to write storage on
                for kv in args.localStorage:
                    if "=" not in kv:
                        sys.exit(f"--localStorage wants K=V, got {kv!r}")
                    k, v = kv.split("=", 1)
                    page.evaluate("([k,v]) => localStorage.setItem(k,v)", [k, v])
            page.goto(args.url, wait_until="networkidle", timeout=45000)
        except Exception as exc:
            print(f"UI UNREACHABLE at {args.url} — is pulsar-web.service up? ({exc})")
            browser.close()
            return 2
        page.wait_for_timeout(args.wait)

        rc, lines = 0, []
        if expr:
            try:
                out = page.evaluate(expr)
            except Exception as exc:
                print(f"probe error: {exc}")
                browser.close()
                return 1
            # A probe that prints nothing meaningful is a FAILED probe, never a
            # pass. The null case is exactly the false green this whole tool is
            # meant to avoid: a rotted selector must not read as "verified".
            empty = out is None or out == {}
            lines.append(json.dumps(out, default=str))
            if empty:
                lines.append("# note: expression evaluated to null/undefined/empty")
                rc = 1

        if args.contained:
            inner, outer = args.contained
            js = f"(() => {{const f={rect_js()};return [f({json.dumps(inner)}), f({json.dumps(outer)})];}})()"
            a, b = page.evaluate(js)
            if a is None or b is None:
                which = inner if a is None else outer
                print(f"FAIL: selector {which} matched nothing — the markup it assumed is gone")
                rc = 1
            else:
                ok = a["top"] >= b["top"] - 1 and a["bottom"] <= b["bottom"] + 1 \
                    and a["left"] >= b["left"] - 1 and a["right"] <= b["right"] + 1
                lines.append(f"contained {inner} in {outer}: {ok} | inner {a['top']}..{a['bottom']} outer {b['top']}..{b['bottom']}")
                rc = rc or (0 if ok else 1)

        if args.no_cross:
            sel = args.no_cross
            js = """(sel) => {
              const f = %s;
              const el = document.querySelector(sel);
              if (!el) return {missing:true};
              const r = f(sel);
              const hits = [];
              for (const e of document.querySelectorAll('*')) {
                const s = getComputedStyle(e);
                const bw = parseFloat(s.borderBottomWidth) || 0;
                if (bw < 1) continue;
                const q = e.getBoundingClientRect();
                if (q.width < 200) continue;
                const y = q.bottom - bw / 2;
                if (y > r.top + 3 && y < r.bottom - 3 && !el.contains(e) && !e.contains(el)) {
                  hits.push((e.className || e.tagName) + '@' + Math.round(q.bottom));
                }
              }
              return {box: r, crossers: hits.slice(0, 8)};
            }""" % rect_js()
            res = page.evaluate(js, sel)
            if res.get("missing"):
                print(f"FAIL: selector {sel} matched nothing — the markup it assumed is gone")
                rc = 1
            else:
                ok = not res["crossers"]
                lines.append(f"no rule crossing {sel}: {ok} | box {res['box']['top']}..{res['box']['bottom']} crossers {res['crossers']}")
                rc = rc or (0 if ok else 1)

        if args.tick:
            sel = args.tick
            js = f"(() => {{const e=document.querySelector({json.dumps(sel)});return e?e.textContent.trim():null;}})()"
            first = page.evaluate(js)
            if first is None:
                print(f"FAIL: selector {sel} matched nothing — the markup it assumed is gone")
                rc = 1
            else:
                page.wait_for_timeout(args.tick_gap)
                second = page.evaluate(js)
                ok = first != second
                lines.append(f"ticks {sel}: {ok} | {first!r} -> {second!r} over {args.tick_gap}ms")
                rc = rc or (0 if ok else 1)

        if args.screenshot:
            clip = None
            if args.clip:
                x, y, w, h = [float(v) for v in args.clip.split(",")]
                clip = {"x": x, "y": y, "width": w, "height": h}
            page.screenshot(path=args.screenshot, clip=clip)
            lines.append(f"# screenshot -> {args.screenshot}")

        browser.close()

    print("\n".join(lines))
    print("# PASS" if rc == 0 else "# FAIL (exit 1: real failure · exit 2 would mean the app was unreachable)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
