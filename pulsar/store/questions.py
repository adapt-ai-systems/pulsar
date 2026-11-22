"""Questions a pulse asks the user: row type, limits, error and Store methods."""

from __future__ import annotations

import json
import sqlite3

from dataclasses import dataclass
from typing import Optional

from pulsar.store.common import StoreError, _new_id, _now_iso, logger
from pulsar.store.pulses import InvalidStateTransition


QUESTION_MAX_OPEN = 10       # open questions per pulse


QUESTION_TEXT_MAX = 2000


QUESTION_CHOICES_MAX = 8


class UnknownQuestion(StoreError):
    """No question with that id."""


@dataclass(frozen=True)
class Question:
    id: str
    pulse_id: str                 # asker — gets the answer as an inbox message
    text: str
    choices: list[str]            # tap-to-answer options (may be empty)
    default_choice: Optional[str]
    context: Optional[str]        # one line of why / what happens next
    state: str                    # open|answered|dismissed
    answer: Optional[str]
    created_at: str
    answered_at: Optional[str]


def _question_from_row(row: sqlite3.Row) -> Question:
    try:
        choices = json.loads(row["choices_json"] or "[]")
    except (TypeError, ValueError):
        choices = []
    return Question(
        id=row["id"], pulse_id=row["pulse_id"], text=row["text"],
        choices=[str(c) for c in choices] if isinstance(choices, list) else [],
        default_choice=row["default_choice"], context=row["context"],
        state=row["state"], answer=row["answer"],
        created_at=row["created_at"], answered_at=row["answered_at"])


class QuestionStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def create_question(self, pulse_id: str, text: str,
                        choices: Optional[list[str]] = None,
                        default_choice: Optional[str] = None,
                        context: Optional[str] = None) -> Question:
        """Record an open question from a pulse. Raises UnknownPulse /
        ValueError loudly — a lost question is the whole problem."""
        text = (text or "").strip()
        if not text:
            raise ValueError("question text is empty")
        if len(text) > QUESTION_TEXT_MAX:
            raise ValueError(f"question text over {QUESTION_TEXT_MAX} chars")
        choices = [str(c).strip() for c in (choices or []) if str(c).strip()]
        if len(choices) > QUESTION_CHOICES_MAX:
            raise ValueError(f"at most {QUESTION_CHOICES_MAX} choices")
        if default_choice and default_choice not in choices:
            raise ValueError("default_choice must be one of choices")
        self.get_pulse(pulse_id)
        open_n = len(self.list_questions(pulse_id=pulse_id, state="open"))
        if open_n >= QUESTION_MAX_OPEN:
            logger.error("question.create pulse=%s open=%d CAP", pulse_id, open_n)
            raise ValueError(
                f"pulse already has {open_n} open questions (cap "
                f"{QUESTION_MAX_OPEN}) — wait for answers first")
        qid = _new_id()
        now = _now_iso()
        try:
            self._conn.execute(
                "INSERT INTO questions (id, pulse_id, text, choices_json, "
                "default_choice, context, state, answer, created_at, "
                "answered_at) VALUES (?, ?, ?, ?, ?, ?, 'open', NULL, ?, NULL);",
                (qid, pulse_id, text, json.dumps(choices), default_choice or None,
                 (context or "").strip() or None, now))
        except sqlite3.Error as exc:
            logger.error("question.create pulse=%s FAILED err=%s", pulse_id, exc)
            raise
        logger.info("question.create id=%s pulse=%s choices=%d ok", qid,
                    pulse_id, len(choices))
        return self.get_question(qid)

    def get_question(self, question_id: str) -> Question:
        row = self._conn.execute(
            "SELECT * FROM questions WHERE id = ?;", (question_id,)).fetchone()
        if row is None:
            raise UnknownQuestion(question_id)
        return _question_from_row(row)

    def list_questions(self, pulse_id: Optional[str] = None,
                       state: Optional[str] = None,
                       limit: int = 500,
                       include_archived: bool = False) -> list[Question]:
        where, args = [], []
        if pulse_id:
            where.append("pulses.state != 'archived'")
            where.append("questions.pulse_id = ?"); args.append(pulse_id)
        elif not include_archived:
            # Archived pulses' questions are closed at archive time; this is a
            # belt-and-braces filter so a stale row can never surface.
            where.append("pulses.state != 'archived'")
        if state:
            where.append("questions.state = ?"); args.append(state)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._conn.execute(
            f"SELECT questions.* FROM questions JOIN pulses ON "
            f"pulses.id = questions.pulse_id{clause} "
            f"ORDER BY questions.created_at LIMIT ?;",
            (*args, int(limit))).fetchall()
        return [_question_from_row(r) for r in rows]

    def close_open_questions(self, pulse_id: str,
                             outcome: str = "dismissed") -> int:
        """Close every open question of a pulse without delivering an inbox
        message (the pulse is going away — e.g. archived). Returns the count."""
        now = _now_iso()
        try:
            cur = self._conn.execute(
                "UPDATE questions SET state = ?, answered_at = ? "
                "WHERE pulse_id = ? AND state = 'open';",
                (outcome, now, pulse_id))
        except sqlite3.Error as exc:
            logger.error("question.close-open pulse=%s FAILED err=%s",
                         pulse_id, exc)
            raise
        n = cur.rowcount or 0
        if n:
            logger.info("question.close-open pulse=%s closed=%d outcome=%s",
                        pulse_id, n, outcome)
        return n

    def resolve_question(self, question_id: str, state: str,
                         answer: Optional[str] = None) -> Question:
        """Mark a question answered (with the answer text) or dismissed.
        Only an OPEN question can be resolved — a second answer is a 409
        upstream, not a silent overwrite."""
        if state not in ("answered", "dismissed"):
            raise ValueError(f"invalid question state: {state!r}")
        q = self.get_question(question_id)
        if q.state != "open":
            raise InvalidStateTransition(
                f"question {question_id} is already {q.state}")
        try:
            cur = self._conn.execute(
                "UPDATE questions SET state = ?, answer = ?, answered_at = ? "
                "WHERE id = ? AND state = 'open';",
                (state, answer, _now_iso(), question_id))
        except sqlite3.Error as exc:
            logger.error("question.resolve id=%s FAILED err=%s", question_id, exc)
            raise
        if cur.rowcount == 0:
            # Lost a race with another resolver between the read and the UPDATE.
            q = self.get_question(question_id)
            raise InvalidStateTransition(
                f"question {question_id} is already {q.state}")
        logger.info("question.resolve id=%s pulse=%s state=%s ok", question_id,
                    q.pulse_id, state)
        return self.get_question(question_id)
