"""Numbered schema migrations, applied in order at :class:`Store` open.

Each migration is ``(version, sql)``. ``version`` is a monotonically
increasing integer; the store applies every migration whose version is
greater than the current ``schema_meta.version`` inside one transaction and
records the new version. Reopening an already-migrated db applies nothing.

Keep migrations append-only: never edit an already-shipped migration's SQL,
add a new numbered one instead.
"""

from __future__ import annotations

# --- v1: base schema -------------------------------------------------------
_V1 = """
CREATE TABLE grids (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    position  INTEGER NOT NULL
);

CREATE TABLE pulses (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    model          TEXT NOT NULL,
    vessel         TEXT NOT NULL,
    grid_id        TEXT,
    state          TEXT NOT NULL,
    handoff_tokens INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    archived_at    TEXT,
    settings_json  TEXT NOT NULL,
    FOREIGN KEY (grid_id) REFERENCES grids(id)
);

CREATE INDEX idx_pulses_grid  ON pulses(grid_id);
CREATE INDEX idx_pulses_state ON pulses(state);
"""

# --- v2: schedules (loops + wake schedules) ---------------------------------
# kind ∈ loop|once|daily. Exactly one timing field is meaningful per kind:
#   loop  -> interval_minutes   (run the prompt every N minutes)
#   once  -> at_iso             (fire once at/after this ISO instant, then off)
#   daily -> cron_hhmm [+ days] (fire when today's HH:MM passes, optional DOW)
# last_fired_at is the ONLY firing memory — the scheduler recomputes due-ness
# from it + the row each tick, so a daemon restart never double-fires or loses
# a schedule (all state on disk).
_V2 = """
CREATE TABLE schedules (
    id               TEXT PRIMARY KEY,
    pulse_id         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    interval_minutes INTEGER,
    at_iso           TEXT,
    cron_hhmm        TEXT,
    days             TEXT,
    enabled          INTEGER NOT NULL DEFAULT 1,
    last_fired_at    TEXT,
    created_at       TEXT NOT NULL,
    FOREIGN KEY (pulse_id) REFERENCES pulses(id)
);

CREATE INDEX idx_schedules_pulse   ON schedules(pulse_id);
CREATE INDEX idx_schedules_enabled ON schedules(enabled);
"""

# --- v3: scheduled cross-pulse messages -------------------------------------
# A schedule may carry a target: at fire time the prompt is delivered to the
# TARGET pulse as a message from the schedule's own pulse.
_V3 = """
ALTER TABLE schedules ADD COLUMN target_pulse_id TEXT;
"""

# --- v4: app settings KV (themes, global settings) --------------------------
# One row per key, JSON value. Theme profiles, active theme, per-model
# handoff defaults, and future app-level settings all live here — boring on
# purpose.
_V4 = """
CREATE TABLE app_settings (
    key        TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# --- v5: pulse ordering (wall drag-reorder) -------------------------------
# Pulses get an explicit position like grids already have. Backfill matches
# the pre-v5 display order (created_at DESC, rowid DESC) so nothing visibly
# moves on upgrade; new pulses append after MAX(position) → far right.
_V5 = """
ALTER TABLE pulses ADD COLUMN position INTEGER NOT NULL DEFAULT 0;

UPDATE pulses SET position = (
    SELECT COUNT(*) FROM pulses AS p2
    WHERE p2.created_at > pulses.created_at
       OR (p2.created_at = pulses.created_at AND p2.rowid > pulses.rowid)
);
"""

# --- v6: delegation watches --------------------------------------------------
# When a pulse delegates to another (spawn-with-sender or pulse-to-pulse
# message), the daemon records a watch. The scheduler checks due watches and
# messages the spawner: work is happening, or NOTHING is happening — no
# delegation may fail silently. All state on disk; a daemon
# restart never loses or double-fires a watch.
#   kind    ∈ spawn|message   (how the delegation was made)
#   confirm 1 = also report "working" positively; 0 = alert only on stall
#   state   ∈ watching|closed; outcome ∈ working|stalled|target_gone
_V6 = """
CREATE TABLE delegation_watches (
    id            TEXT PRIMARY KEY,
    spawner_id    TEXT NOT NULL,
    target_id     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    confirm       INTEGER NOT NULL DEFAULT 1,
    state         TEXT NOT NULL DEFAULT 'watching',
    baseline_ctx  INTEGER,
    checks_done   INTEGER NOT NULL DEFAULT 0,
    next_check_at TEXT NOT NULL,
    outcome       TEXT,
    created_at    TEXT NOT NULL,
    closed_at     TEXT,
    FOREIGN KEY (spawner_id) REFERENCES pulses(id),
    FOREIGN KEY (target_id)  REFERENCES pulses(id)
);

CREATE INDEX idx_watches_state  ON delegation_watches(state);
CREATE INDEX idx_watches_target ON delegation_watches(target_id);
"""

# --- v7: Inbox ---------------------------------------------------------------
# The async comms rail between the user and the agent org — a third top-level
# surface. Inspect semantics: an item is Inbox (unread) until marked read → it
# auto-archives (browsable forever, never deleted). Channels are dynamic (any
# unknown name auto-creates with default mode `notify`). Web Push subscriptions
# for the iPhone PWA live here too. Single-user, no account column.
#   inbox_items.push_status ∈ NULL|pending|sent|failed:<reason>|skipped:<why>
#   inbox_channels.mode ∈ notify|quiet|muted; mute_until gates push passively.
_V7 = """
CREATE TABLE inbox_items (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    channel         TEXT NOT NULL,
    thread_key      TEXT,
    source_kind     TEXT NOT NULL,
    source_pulse_id TEXT,
    source_label    TEXT NOT NULL,
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'notify',
    needs_response  INTEGER NOT NULL DEFAULT 0,
    starred         INTEGER NOT NULL DEFAULT 0,
    read_at         TEXT,
    replied_at      TEXT,
    push_status     TEXT
);

CREATE INDEX idx_inbox_read_created ON inbox_items(read_at, created_at);
CREATE INDEX idx_inbox_channel_read ON inbox_items(channel, read_at);
CREATE INDEX idx_inbox_thread_created ON inbox_items(thread_key, created_at);

CREATE TABLE inbox_channels (
    channel    TEXT PRIMARY KEY,
    mode       TEXT NOT NULL DEFAULT 'notify',
    mute_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE push_subscriptions (
    endpoint        TEXT PRIMARY KEY,
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    device_label    TEXT,
    created_at      TEXT NOT NULL,
    last_seen_at    TEXT,
    last_success_at TEXT,
    last_error      TEXT
);
"""

# --- v8: stable Inbox cursor sequence --------------------------------------
# ``created_at`` was originally stored at second precision, so more than one
# item can share it.  A durable monotonic tie-breaker makes pagination and the
# read-all cutoff a true total order, including for rows created before this
# migration.  Keep the public ``before=<timestamp>`` form as a legacy cursor;
# new clients use ``<created_at>|<cursor_seq>``.
_V8 = """
ALTER TABLE inbox_items ADD COLUMN cursor_seq INTEGER;

UPDATE inbox_items SET cursor_seq = rowid WHERE cursor_seq IS NULL;

CREATE UNIQUE INDEX idx_inbox_cursor_seq ON inbox_items(cursor_seq);
CREATE INDEX idx_inbox_created_cursor
    ON inbox_items(created_at, cursor_seq);
"""

# --- v9: condition watches --------------------------------------------------
# A pulse registers a daemon-evaluated condition; when it trips the owner gets
# a message. kind ∈ pulse_state|file|log|http|cmd. Unlike delegation_watches
# (spawner→target pairs), a condition watch has ONE pulse: the owner that
# registered it and receives the notification. `bad` + `detail_json` carry the
# previous check's result for edge detection — recomputed state on disk, so a
# daemon restart never loses or double-fires a watch.
_V9 = """
CREATE TABLE condition_watches (
    id            TEXT PRIMARY KEY,
    pulse_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    spec_json     TEXT NOT NULL,
    mode          TEXT NOT NULL DEFAULT 'edge',
    interval_s    INTEGER NOT NULL DEFAULT 60,
    note          TEXT,
    state         TEXT NOT NULL DEFAULT 'watching',
    checks_done   INTEGER NOT NULL DEFAULT 0,
    bad           INTEGER,
    detail_json   TEXT,
    next_check_at TEXT NOT NULL,
    outcome       TEXT,
    created_at    TEXT NOT NULL,
    closed_at     TEXT,
    FOREIGN KEY (pulse_id) REFERENCES pulses(id)
);

CREATE INDEX idx_condwatches_pulse  ON condition_watches(pulse_id);
CREATE INDEX idx_condwatches_state  ON condition_watches(state);
"""

# --- v10: kanban (REMOVED 2026-09-11 — tables dropped again by v15) --------
# Project-tracking board for the user + pulses. Cards are pointers to the real
# work (docs/repos), never containers. Every mutation appends a kanban_events
# row (who/when/what) so card history is append-only. Cards are owned by
# PROJECT, not pulse id, so they outlive archived/deleted pulses — hence no FK.
# Removal recipe: drop the two tables and delete kanban.py + its wiring.
_V10 = """
CREATE TABLE kanban_cards (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT '',
    next_action   TEXT NOT NULL DEFAULT '',
    col           TEXT NOT NULL DEFAULT 'waiting',
    project       TEXT NOT NULL DEFAULT '',
    owner_pulse_id TEXT,
    owner_label   TEXT NOT NULL DEFAULT '',
    verified      TEXT NOT NULL DEFAULT 'none',
    links_json    TEXT NOT NULL DEFAULT '[]',
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX idx_kanban_cards_col ON kanban_cards(col, position);

CREATE TABLE kanban_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_kanban_events_card ON kanban_events(card_id, seq);
"""

# --- v11: Inbox scoring -----------------------------------------------------
# The user scores an item 1|2|3 and the daemon learns a rule for that KIND of
# message (channel + sender + normalized title prefix, see
# ``inbox_kind_key``):  1 = do not want (auto-archived, no push),
# 2 = ok (badge only, no push), 3 = notify (push + badge).  ``inbox_items.score``
# is the score carried by the item (set by the user, or stamped from the rule at
# creation).  Rules are keyed by kind; scoring the same kind again overwrites.
_V11 = """
ALTER TABLE inbox_items ADD COLUMN score INTEGER;

CREATE TABLE inbox_rules (
    kind_key      TEXT PRIMARY KEY,
    score         INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    source_label  TEXT NOT NULL,
    example_title TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
"""

# --- v12: delegation-watch follow phase (the stall watcher)
# A delegation watch no longer closes the moment the delegate shows one sign
# of work — that left a helper which works for a while and THEN hangs totally
# unwatched. After the first working check the watch re-arms into a follow
# phase that rechecks every few minutes for the life of the delegation:
#   phase         initial | follow
#   last_ctx      target ctx at the last follow check (progress detection)
#   follow_checks consecutive follow checks with NO ctx growth (zombie idle:
#                 the helper finished its turn and never replied)
# Hung-while-busy detection rides the heartbeat's busy_since + a frozen ctx;
# stopped/expired close the watch. All state on disk.
_V12 = """
ALTER TABLE delegation_watches ADD COLUMN phase TEXT NOT NULL DEFAULT 'initial';
ALTER TABLE delegation_watches ADD COLUMN last_ctx INTEGER;
ALTER TABLE delegation_watches ADD COLUMN follow_checks INTEGER NOT NULL DEFAULT 0;
"""

# v13 (2026-09-07, Watchers): a condition watch records its last FIRE
# separately from its last check — ``last_fired_at`` is stamped when the
# condition tripped and a wake was attempted; ``last_delivery`` is
# "delivered" or "failed: <reason>" so the Watchers view can tell "met" from
# "the owner actually got woken" (found in review, 2026-09-07).
_V13 = """
ALTER TABLE condition_watches ADD COLUMN last_fired_at TEXT;
ALTER TABLE condition_watches ADD COLUMN last_delivery TEXT;
"""

# v14 (2026-09-08, Questions): a pulse that needs an answer from the user posts
# a QUESTION instead of burying it in chat where questions are easy to miss. The pane
# shows open questions in a strip above the watch bar; answering writes the
# answer into the pulse's inbox and wakes it. choices_json is a JSON list of
# strings (may be empty = free text only).
_V14 = """
CREATE TABLE IF NOT EXISTS questions (
    id             TEXT PRIMARY KEY,
    pulse_id       TEXT NOT NULL REFERENCES pulses(id),
    text           TEXT NOT NULL,
    choices_json   TEXT NOT NULL DEFAULT '[]',
    default_choice TEXT,
    context        TEXT,
    state          TEXT NOT NULL DEFAULT 'open',
    answer         TEXT,
    created_at     TEXT NOT NULL,
    answered_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_questions_state ON questions(state, pulse_id);
"""

# v15 (2026-09-11): Kanban removed (unused). Drops the two v10
# tables; kanban.py, Kanban.svelte and their wiring are gone from the tree.
_V15 = """
DROP TABLE IF EXISTS kanban_events;
DROP TABLE IF EXISTS kanban_cards;
"""

# v16 (2026-09-11, sub-workspaces): a workspace may sit inside ONE parent
# workspace (nested workspaces). One layer only: a child
# never has children of its own. NULL = top level. Enforced in Store, not by
# the schema, so the column stays a plain nullable text.
_V16 = """
ALTER TABLE grids ADD COLUMN parent_id TEXT;
"""

# v17 (2026-09-11, user input recency): when the user last sent a pulse a
# message directly (typed in the pane, or an Inbox reply) — NOT pulse-to-pulse
# mail, schedules, or watches. The drawer's recent-inputs view sorts on it.
_V17 = """
ALTER TABLE pulses ADD COLUMN last_input_at TEXT;
"""

# v18: durable project records, explicit none vs undecided, append-only work log.
_V18 = """
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
 description TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('own','client')),
 created_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', hidden INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL
);
CREATE TABLE pulse_projects (
 pulse_id TEXT PRIMARY KEY REFERENCES pulses(id) ON DELETE CASCADE,
 state TEXT NOT NULL CHECK(state IN ('attached','none')),
 project_id TEXT REFERENCES projects(id), updated_at TEXT NOT NULL,
 CHECK((state='attached' AND project_id IS NOT NULL) OR (state='none' AND project_id IS NULL))
);
CREATE TABLE project_sources (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
 pulse_id TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
 end_offset INTEGER, file_key TEXT, error TEXT
);
CREATE UNIQUE INDEX project_source_active ON project_sources(pulse_id) WHERE end_offset IS NULL;
CREATE TABLE project_entries (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES projects(id), pulse_id TEXT,
 kind TEXT NOT NULL, text TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL, source_id TEXT REFERENCES project_sources(id), source_offset INTEGER,
 UNIQUE(source_id,source_offset)
);
CREATE INDEX project_entries_project ON project_entries(project_id,seq DESC);
CREATE INDEX projects_recent ON projects(updated_at DESC);
"""

# v19: sent mail replies belong to conversations, not the inbound mail list.
_V19 = """
CREATE TABLE inbox_replies (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    item_id TEXT NOT NULL REFERENCES inbox_items(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_inbox_replies_item ON inbox_replies(item_id, seq);
"""

# v20 (2026-09-17, outcome consolidation): semantic durable grouping —
# one plain-language dated outcome headline per accomplishment, with the
# ORIGINAL project entries linked as read-only members. Original records,
# timestamps, payloads and ingestion stay untouched; an entry belongs to at
# most one outcome (UNIQUE member entry_id), and conflicts are rejected by
# the Store rather than silently re-attributed. No status field, no backfill.
_V20 = """
CREATE TABLE project_outcomes (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id),
    title       TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    pulse_id    TEXT
);
CREATE INDEX project_outcomes_order ON project_outcomes(project_id, occurred_at DESC, id DESC);
CREATE TABLE project_outcome_members (
    project_id TEXT NOT NULL REFERENCES projects(id),
    entry_id   TEXT NOT NULL UNIQUE REFERENCES project_entries(id),
    outcome_id TEXT NOT NULL REFERENCES project_outcomes(id) ON DELETE CASCADE,
    PRIMARY KEY (outcome_id, entry_id)
);
CREATE INDEX project_outcome_members_project ON project_outcome_members(project_id);
"""

# v21 (2026-09-18, project proposals): durable project-level proposals. A
# proposal is deliberately separate from the append-only work log because its
# body, evidence, status, filing pulse, and lifecycle timestamps are edited as
# one item.
_V21 = """
CREATE TABLE project_proposals (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    status      TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected','deferred')),
    filed_by    TEXT NOT NULL,
    pulse_id    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX project_proposals_order ON project_proposals(project_id, updated_at DESC, id DESC);
"""

# v22 (2026-09-23, project documents): generated reference documents kept on a
# project (e.g. a project brief), one row per (slug, audience). Separate from
# notes (hand-edited, versioned into Activity) and from the append-only log
# because a writer replaces the whole body on every refresh. audience 'team'
# is the redacted/shared copy.
_V22 = """
CREATE TABLE project_documents (
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug         TEXT NOT NULL,
    audience     TEXT NOT NULL CHECK(audience IN ('full','team')),
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    source       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    checked_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, slug, audience)
);
"""

# v23 (2026-10-16, legions): a tree of pulses as a
# daemon object. ``legions`` is the outcome + quotas; ``legion_members`` is
# the rank tree (one active legion per pulse; ``host`` is the placement seam
# for runners on other boxes, only 'local' today); ``legion_digest`` queues
# informational delegation-watch lines per manager until the flush.
_V23 = """
CREATE TABLE legions (
    id               TEXT PRIMARY KEY,
    slug             TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    goal             TEXT NOT NULL DEFAULT '',
    legate_id        TEXT NOT NULL,
    quartermaster_id TEXT,
    home             TEXT NOT NULL,
    quotas_json      TEXT NOT NULL,
    digest_minutes   INTEGER NOT NULL DEFAULT 15,
    state            TEXT NOT NULL CHECK(state IN ('active','stopped')),
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE legion_members (
    legion_id   TEXT NOT NULL REFERENCES legions(id) ON DELETE CASCADE,
    pulse_id    TEXT NOT NULL,
    rank        TEXT NOT NULL CHECK(rank IN ('legate','quartermaster','tribune','centurion','legionary','auxiliary')),
    manager_id  TEXT,
    host        TEXT NOT NULL DEFAULT 'local',
    status      TEXT NOT NULL CHECK(status IN ('active','released')),
    joined_at   TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (legion_id, pulse_id)
);
CREATE UNIQUE INDEX legion_members_active_pulse ON legion_members(pulse_id) WHERE status = 'active';
CREATE INDEX legion_members_manager ON legion_members(legion_id, manager_id, status);
CREATE TABLE legion_digest (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    legion_id  TEXT NOT NULL,
    manager_id TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX legion_digest_manager ON legion_digest(legion_id, manager_id, id);
"""

# v24: a legion's base skills list (JSON array of skill names for member spawns).
_V24 = """
ALTER TABLE legions ADD COLUMN skills_json TEXT NOT NULL DEFAULT '[]';
"""

# v25: loop triggers. A loop fires on its timer, and/or on
# every handoff (prompt pasted into the successor seed), and/or on every
# message the user sends (prompt appended as a "Standing loop" block). CSV of
# timer|handoff|message|replies:N|tools:N; NULL/empty = timer only (every
# pre-v25 loop). trigger_state is JSON: the heartbeat counter values at the
# last fire ({"replies": 12, "tools": 340}). trigger_mode: "any" (default,
# any armed timer/counter condition fires and the fire resets ALL of them, so
# a tool-call fire pushes the timer back out) or "all" (fire only when every
# armed condition is met).
_V25 = """
ALTER TABLE schedules ADD COLUMN triggers TEXT;
ALTER TABLE schedules ADD COLUMN trigger_state TEXT;
ALTER TABLE schedules ADD COLUMN trigger_mode TEXT;
"""

# v26: opt-in shared, disposable Legion working ledger.
_V26 = """
ALTER TABLE legions ADD COLUMN ledger_enabled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE legions ADD COLUMN ledger_last_wake_at TEXT;
CREATE TABLE legion_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    legion_id TEXT NOT NULL,
    author_id TEXT,
    author_title TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX legion_ledger_pending ON legion_ledger(legion_id, id);
"""

# v27 (2026-11-07, org switchboard): an ORG is a legion with
# switches. Existing legions keep today's behavior (strict ranks, digests on,
# no enforcement); auto orgs (created by the daemon for a lead with 2+
# helpers) are non-strict, digest-off. Members get the switchboard fields:
# a per-pulse comms override (NULL = role template), extra message lines,
# and a per-pulse enforcement override (NULL = the org's switch).
# ``org_templates`` holds the user's edited role templates (no row = built-in).
_V27 = """
ALTER TABLE legions ADD COLUMN strict_ranks INTEGER NOT NULL DEFAULT 1;
ALTER TABLE legions ADD COLUMN digest_enabled INTEGER NOT NULL DEFAULT 1;
ALTER TABLE legions ADD COLUMN enforce_lines INTEGER NOT NULL DEFAULT 0;
ALTER TABLE legions ADD COLUMN auto INTEGER NOT NULL DEFAULT 0;
ALTER TABLE legion_members ADD COLUMN comms_override TEXT;
ALTER TABLE legion_members ADD COLUMN may_message_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE legion_members ADD COLUMN enforce INTEGER;
CREATE TABLE org_templates (
    role       TEXT PRIMARY KEY CHECK(role IN ('lead','manager','helper')),
    text       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Ordered list. Append new tuples; never mutate existing ones.
MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1),
    (2, _V2),
    (3, _V3),
    (4, _V4),
    (5, _V5),
    (6, _V6),
    (7, _V7),
    (8, _V8),
    (9, _V9),
    (10, _V10),
    (11, _V11),
    (12, _V12),
    (13, _V13),
    (14, _V14),
    (15, _V15),
    (16, _V16),
    (17, _V17),
    (18, _V18),
    (19, _V19),
    (20, _V20),
    (21, _V21),
    (22, _V22),
    (23, _V23),
    (24, _V24),
    (25, _V25),
    (26, _V26),
    (27, _V27),
]

LATEST_VERSION: int = MIGRATIONS[-1][0]
