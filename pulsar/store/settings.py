"""App settings key/value table and the default-skills merge.
"""

from __future__ import annotations

import json
import sqlite3

from typing import Any, Optional

from pulsar.store.common import _now_iso, logger


class SettingsStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def get_setting(self, key: str) -> Optional[Any]:
        """Read one app-settings value (parsed JSON) or None if absent."""
        row = self._conn.execute(
            "SELECT value_json FROM app_settings WHERE key = ?;", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError as exc:
            logger.error("settings.get key=%s CORRUPT json err=%s", key, exc)
            return None

    DEFAULT_SKILLS_KEY = "pulse.default_skills"

    def diet_with_defaults(self, diet: Optional[dict]) -> dict:
        """The diet a spawn should actually load: the pulse's stored diet plus
        every skill marked "on by default" (settings KV ``pulse.default_skills``).
        Default skills load on every pulse, whether a human or an agent spawned
        it — the UI preselect alone would miss helpers spawned through
        POST /pulses. Full-fat (clean=False) loads everything anyway, so only a
        clean diet is merged. The stored diet is untouched; the merge is
        recomputed at each spawn so a newly marked default reaches existing
        pulses on their next spawn."""
        base = dict(diet) if diet else {"clean": True}
        if not base.get("clean", True):
            return base
        # ONE effective prompt-parts allowlist:
        # the harness-overrides list wins over the diet's, so an experiment knob
        # that excludes "default_skills" (or re-includes it on a lean
        # diet) reaches the default-skills merge too — the same resolution
        # the context renderer and the pi todo extension use.
        from pulsar.harness_overrides import effective_prompt_parts
        parts = effective_prompt_parts(base.get("harness_overrides"), base)
        if parts is not None and "default_skills" not in parts:
            return base  # a lean spawn opted out of the default skills
        raw = self.get_setting(self.DEFAULT_SKILLS_KEY)
        defaults = [d for d in raw if isinstance(d, str)] if isinstance(raw, list) else []
        if not defaults:
            return base
        skills = list(base.get("skills") or [])
        for name in defaults:
            if name not in skills:
                skills.append(name)
        base["skills"] = skills
        return base

    def put_setting(self, key: str, value: Any) -> None:
        """Upsert one app-settings value (stored as JSON)."""
        if not key:
            logger.error("settings.put empty key")
            raise ValueError("key must be non-empty")
        now = _now_iso()
        value_json = json.dumps(value)
        try:
            self._conn.execute(
                "INSERT INTO app_settings (key, value_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value_json = excluded.value_json, "
                "updated_at = excluded.updated_at;",
                (key, value_json, now),
            )
        except sqlite3.Error as exc:
            logger.error("settings.put key=%s FAILED err=%s", key, exc)
            raise
        logger.info("settings.put key=%s bytes=%d ok", key,
                    len(value_json))

    def delete_setting(self, key: str) -> None:
        """Delete one app-settings key (no-op if absent, logged)."""
        try:
            cur = self._conn.execute(
                "DELETE FROM app_settings WHERE key = ?;", (key,))
        except sqlite3.Error as exc:
            logger.error("settings.delete key=%s FAILED err=%s", key, exc)
            raise
        logger.info("settings.delete key=%s existed=%s ok", key,
                    cur.rowcount > 0)
