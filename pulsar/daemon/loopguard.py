"""Degenerate-loop guard — detect a pulse stuck repeating itself, heal it.

Born 2026-08-15: a DS4 Flash pulse spent ~40 minutes and ~23k context tokens
emitting "Go." / "Now." / "Run it." thousands of times while promising — but
never making — its next tool call. The garbage lands in events.jsonl as
abnormally large assistant_text events (19-29k chars vs a normal 0.2-3.5k),
so the daemon can see it with the same bounded tail the handoff engine uses.
The runner stays dumb: all detection and every strike
lives here.

Escalation ladder (prefer self-healing before paging):

  strike 1  steer — drop a loop-guard message into the inbox; the runner
            injects it into the live turn (same mid-turn path as handoff
            requests). Tonight's pulse broke its own loop when it finally
            noticed — a shove works.
  strike 2  rescue — fire run_handoff(reason="loop_rescue"): stop the stuck
            session, respawn fresh, seed it with the pulse's own handoff
            prose (or a mechanical fallback built here if the model is too
            far gone to write one).
  strike 3  alert — the rescue failed or is blocked; file a system Inbox
            alert and leave the pulse alone (throttled).

Strike memory is in-process (a daemon restart forgets it); the event
freshness gate below keeps a restart from re-striking stale history, and a
still-looping pulse re-earns its strikes within a tick or two anyway.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import datetime
from typing import Optional

from pulsar.daemon import events_io, handoff, mailbox, paths
from pulsar.store import Store, Pulse

logger = logging.getLogger("pulsar.daemon.loopguard")

TICK_SECONDS = 30.0

# Detection thresholds, validated against all four known DS4 incidents
# (2026-08-09 55k, 2026-08-15 103k, 2026-08-15 117k, 2026-08-16 ~100k)
# and swept against
# healthy traffic. The 2026-08-15/16 incidents defeated the original
# line-based detector two ways: barks run together on one line with no
# newlines ("Let me run it.Let me do it."), and varied phrasing that never
# repeats one exact line ("Applying." / "Now applying." / "Let me apply.").
# Hence: sentence-boundary normalization, a fuzzy first-words family key,
# and the "Let me" density check (every incident is dominated by "Let me X"
# intent barks — 50-108 per block).
MIN_EVENT_CHARS = 3_000     # output cap means real loops can be short
MAX_LINE_CHARS = 100        # loop sentences are short barks ("Go.", "Run it.")
MIN_TOP_REPEATS = 10        # the same sentence family at least this many times
FAMILY_MIN = 5              # a sentence "family" = same key 5+ times
FAMILY_FRACTION = 0.4       # families must own this share of all sentences
LETME_MIN = 10              # "Let me ..." sentences to call it barking
LETME_FRACTION = 0.25       # ...and they must own this share of sentences

# "lderGo.Let me run it." — split where a terminator meets a capital, so
# run-together barks become sentences even with no whitespace between them.
_SENT_BOUNDARY = re.compile(r"(?<=[.!?])\s*(?=[A-Z])")
_WORDS = re.compile(r"[a-z0-9']+")
# Markdown structure — table rows, bullets, numbered items, headings,
# quotes. Excluded from the sentence pool: a 51-row checklist repeating
# "- ✅ Matches proposal" or a table whose rows share a path prefix is
# healthy output, while every observed loop barks in plain prose.
_STRUCTURED_LINE = re.compile(r"^\s*(?:[|\-*#>+]|\d+[.)]\s)")

FRESH_EVENT_S = 300.0       # only strike on recent events (restart-safe)
STRIKE_WINDOW_S = 1800.0    # strikes decay after 30 min of clean output
STEER_GRACE_S = 120.0       # give the steer time to land before escalating
ALERT_COOLDOWN_S = 1800.0   # at most one alert per pulse per 30 min

STEER_TEXT = (
    "[pulsar loop-guard] You are stuck repeating the same short lines "
    "instead of acting. Stop narrating intent. Take the single next action "
    "NOW: emit exactly one tool call, with no preamble. If the task is "
    "already complete, state the final result plainly and end the turn."
)

# In-process state, keyed by pulse id.
_strikes: dict[str, list[float]] = {}
_handled_ts: dict[str, str] = {}     # newest event ts already struck on
_last_alert: dict[str, float] = {}


def _sentences(text: str) -> list[str]:
    """Short bark-sized sentences, split on newlines AND terminator+capital."""
    out: list[str] = []
    for raw in text.splitlines():
        if _STRUCTURED_LINE.match(raw):
            continue
        for s in _SENT_BOUNDARY.split(raw):
            s = s.strip()
            if s and len(s) < MAX_LINE_CHARS and any(c.isalpha() for c in s):
                out.append(s)
    return out


def _family_key(sentence: str) -> str:
    """First three words, lowercased — "Let me apply." and "Let me apply the
    patch now." land in one family; digits kept so table rows stay unique."""
    return " ".join(_WORDS.findall(sentence.lower())[:3])


def looks_degenerate(text: str) -> Optional[tuple[str, int]]:
    """(representative sentence, count) when ``text`` is a repetition loop,
    else None.

    Cheap and deliberately strict: big block, short sentences, and either a
    "Let me ..." bark storm or fuzzy sentence families owning a large share
    of the block — so tables, logs, and long healthy prose pass.
    """
    if len(text) < MIN_EVENT_CHARS:
        return None
    sents = _sentences(text)
    if len(sents) < MIN_TOP_REPEATS * 2:
        return None

    # DS4's signature failure: dozens of "Let me X" intent barks in one block.
    letme = [s for s in sents if s.lower().startswith("let me")]
    if len(letme) >= LETME_MIN and len(letme) / len(sents) >= LETME_FRACTION:
        top = Counter(letme).most_common(1)[0][0]
        return top, len(letme)

    families = Counter(_family_key(s) for s in sents)
    top_key, top_n = families.most_common(1)[0]
    if top_n < MIN_TOP_REPEATS:
        return None
    family_mass = sum(n for n in families.values() if n >= FAMILY_MIN)
    if family_mass / len(sents) < FAMILY_FRACTION:
        return None
    top = Counter(s for s in sents
                  if _family_key(s) == top_key).most_common(1)[0][0]
    return top, top_n


def _event_age_s(ev: dict, now: Optional[float] = None) -> Optional[float]:
    try:
        ts = datetime.fromisoformat(str(ev.get("ts")).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (now or time.time()) - ts.timestamp()


def find_fresh_loop(pulse_id: str,
                    now: Optional[float] = None) -> Optional[dict]:
    """The newest fresh, not-yet-handled degenerate assistant_text event.

    Returns {"ts", "line", "repeats"} or None. Reuses handoff's bounded
    events.jsonl tail — never a whole-file read.
    """
    newest = None
    for ev in events_io.tail_events(pulse_id):
        if ev.get("type") != "assistant_text":
            continue
        age = _event_age_s(ev, now)
        if age is None or age > FRESH_EVENT_S:
            continue
        ts = str(ev.get("ts"))
        if ts <= _handled_ts.get(pulse_id, ""):
            continue
        hit = looks_degenerate(ev.get("text") or "")
        if hit:
            newest = {"ts": ts, "line": hit[0], "repeats": hit[1]}
    return newest


def _mechanical_handoff_text(pulse_id: str) -> str:
    """A rescue seed built from the event tail, for a model too far gone to
    write its own handoff prose. Degenerate blocks are skipped so the loop
    text cannot re-seed the successor."""
    user_tail = ""
    snippets: list[str] = []
    for ev in events_io.tail_events(pulse_id):
        if ev.get("type") == "user_message":
            user_tail = ev.get("text") or user_tail
        elif ev.get("type") == "assistant_text":
            text = ev.get("text") or ""
            if text and not looks_degenerate(text):
                snippets.append(text[:500])
    parts = [
        "Your predecessor session got stuck in a degenerate output loop "
        "(repeating the same short lines instead of acting) and was replaced. "
        "It could not write a proper handoff. Reconstruct state from the "
        "working notes below, re-read any working documents it mentions, "
        "verify what is actually done before redoing anything, and continue "
        "the task.",
    ]
    if user_tail:
        parts.append("Most recent user/system message it was working on:\n"
                     + user_tail[:1000])
    if snippets:
        parts.append("Its last healthy progress notes (oldest first):\n"
                     + "\n---\n".join(snippets[-3:]))
    return "\n\n".join(parts)


async def _strike(store: Store, pulse: Pulse, hit: dict) -> None:
    pid = pulse.id
    now = time.time()
    strikes = [t for t in _strikes.get(pid, ()) if now - t < STRIKE_WINDOW_S]
    strikes.append(now)
    _strikes[pid] = strikes
    _handled_ts[pid] = hit["ts"]
    n = len(strikes)
    logger.error("loopguard.strike pulse=%s n=%d line=%r x%d event_ts=%s",
                 pid, n, hit["line"][:80], hit["repeats"], hit["ts"])

    if n == 1:
        # Steer the live turn. Same inbox path as the handoff request — the
        # runner injects within seconds; the loopguard prefix dedupes a
        # re-steer instead of stacking.
        mailbox.write(pid, STEER_TEXT, prefix=mailbox.LOOPGUARD,
                      supersede=True, source="loopguard",
                      extra=mailbox.origin_keys("daemon", "loopguard"))
        return

    # Strike 2+: rescue, unless the steer hasn't had time to land yet.
    if now - strikes[0] < STEER_GRACE_S:
        logger.info("loopguard.strike pulse=%s n=%d within steer grace "
                    "(%.0fs) — holding", pid, n, now - strikes[0])
        return

    if handoff.state.in_flight(pid) is None and not handoff.state.in_cooldown(pulse):
        logger.error("loopguard.rescue pulse=%s firing loop_rescue handoff",
                     pid)
        ok = await handoff.engine.run_handoff(
            store, pulse, reason="loop_rescue",
            fallback_reply=_mechanical_handoff_text(pid))
        if ok:
            # Fresh session, fresh slate.
            _strikes.pop(pid, None)
            return
        logger.error("loopguard.rescue pulse=%s handoff FAILED", pid)
    else:
        logger.error("loopguard.rescue pulse=%s blocked (handoff in flight "
                     "or cooldown) — escalating", pid)

    # Strike 3: the rescue failed or is blocked — file an Inbox alert (throttled).
    if now - _last_alert.get(pid, 0.0) < ALERT_COOLDOWN_S:
        return
    _last_alert[pid] = now
    msg = (f"Pulsar loop-guard — pulse "
           f"*{pulse.title or pid}* (`{pid[:8]}`, {pulse.model}) is stuck "
           f"repeating {hit['line'][:60]!r} x{hit['repeats']} and could not "
           f"be auto-rescued (steer + loop_rescue handoff both failed). "
           f"It needs a look.")
    try:
        from pulsar.daemon import inbox as inbox_mod
        inbox_mod.produce(
            store, title=f"Loop guard stuck: {pulse.title or pid}",
            body=msg, channel="system", source_kind="loopguard",
            source_label="loop-guard",
            # Tap-to-open: the subject pulse, so the phone tap lands on the
            # stuck pulse itself (loopguard is not pulse-sourced).
            source_pulse_id=pid, validate=False)
        logger.error("loopguard.alert pulse=%s inbox ok", pid)
    except Exception as exc:
        logger.error("loopguard.alert pulse=%s inbox FAILED err=%s", pid, exc)


async def tick(store: Store) -> int:
    """One guard pass over running pulses. Returns strikes issued."""
    struck = 0
    for pulse in store.list_pulses(state="running"):
        beat = paths.read_heartbeat(pulse.id) or {}
        if not paths.heartbeat_fresh(beat):
            continue
        try:
            hit = find_fresh_loop(pulse.id)
            if hit is None:
                continue
            await _strike(store, pulse, hit)
            struck += 1
        except Exception:
            logger.exception("loopguard.tick pulse=%s RAISED", pulse.id)
    return struck
