"""Questions — a pulse asks the user, who answers from the pane.

A question posted in chat can scroll away. This rail makes it a first-class
object:

  - ``ask``    — a pulse records an open question (text, optional choices,
                 default, one line of context). The pane shows it in a strip
                 above the watch bar until it is answered.
  - ``answer`` — the user's answer is stamped on the question AND written into
                 the asking pulse's ``inbox/new`` as a wrapped message (same
                 atomic tmp+rename contract as ``messages._write_inbox``),
                 waking a stopped asker. A wake/write failure leaves the
                 question OPEN so nothing is lost silently.
  - ``dismiss``— close without an answer; the pulse is told so it can move on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from typing import Any, Optional

from pulsar.daemon import lifecycle, mailbox
from pulsar.store import (InvalidStateTransition, Question, Store,
                          UnknownPulse, UnknownQuestion)

logger = logging.getLogger("pulsar.daemon.questions")


class QuestionError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def serialize(q: Question) -> dict[str, Any]:
    return asdict(q)


def ask(store: Store, pulse_id: str, text: str,
        choices: Optional[list[str]] = None,
        default_choice: Optional[str] = None,
        context: Optional[str] = None) -> dict[str, Any]:
    try:
        pulse = store.get_pulse(pulse_id)
    except UnknownPulse as exc:
        raise QuestionError("not_found", str(exc), 404)
    if pulse.state == "archived":
        raise QuestionError("bad_state", "archived pulses cannot ask", 409)
    try:
        q = store.create_question(pulse_id, text, choices=choices,
                                  default_choice=default_choice,
                                  context=context)
    except ValueError as exc:
        raise QuestionError("bad_request", str(exc), 400)
    return serialize(q)


def _deliver(store: Store, q: Question, text: str) -> None:
    """Wake the asker if stopped and drop the wrapped message in its inbox.
    Raises QuestionError (503) on failure — the caller leaves the question
    open."""
    with lifecycle.pulse_lock(q.pulse_id):
        pulse = store.get_pulse(q.pulse_id)
        try:
            mailbox.deliver(
                store, pulse, text,
                on_archived="raise", locked=True, source="pulse",
                extra=mailbox.origin_keys("user", "question"),
                wake_error=RuntimeError,
            )
        except mailbox.Archived as exc:
            raise QuestionError("bad_state", "asking pulse is archived", 409) from exc
        except RuntimeError as exc:
            cause = exc.__cause__ or exc
            logger.error("question.deliver id=%s pulse=%s WAKE FAILED err=%s",
                         q.id, q.pulse_id, cause)
            raise QuestionError("wake_failed",
                                f"could not wake pulse: {cause}", 503) from cause
        except OSError as exc:
            logger.error("question.deliver id=%s pulse=%s WRITE FAILED err=%s",
                         q.id, q.pulse_id, exc)
            raise QuestionError("write_failed",
                                f"could not write inbox: {exc}", 503) from exc


def answer(store: Store, question_id: str, text: str,
           by: str = "The user") -> dict[str, Any]:
    started = time.monotonic()
    text = (text or "").strip()
    if not text:
        raise QuestionError("bad_request", "answer is empty", 400)
    try:
        q = store.get_question(question_id)
    except UnknownQuestion as exc:
        raise QuestionError("not_found", str(exc), 404)
    if q.state != "open":
        raise QuestionError("bad_state", f"question is already {q.state}", 409)
    short = q.text if len(q.text) <= 160 else q.text[:157] + "…"
    _deliver(store, q, f"[{by} answered your question {q.id}] "
                       f"Q: {short}\nA: {text}")
    try:
        q = store.resolve_question(question_id, "answered", answer=text)
    except InvalidStateTransition as exc:
        raise QuestionError("bad_state", str(exc), 409)
    logger.info("question.answered id=%s pulse=%s in %.3fs", q.id, q.pulse_id,
                time.monotonic() - started)
    return serialize(q)


def dismiss(store: Store, question_id: str,
            by: str = "The user") -> dict[str, Any]:
    try:
        q = store.get_question(question_id)
    except UnknownQuestion as exc:
        raise QuestionError("not_found", str(exc), 404)
    if q.state != "open":
        raise QuestionError("bad_state", f"question is already {q.state}", 409)
    short = q.text if len(q.text) <= 160 else q.text[:157] + "…"
    _deliver(store, q, f"[{by} dismissed your question {q.id} without an "
                       f"answer — decide yourself or move on] Q: {short}")
    try:
        q = store.resolve_question(question_id, "dismissed")
    except InvalidStateTransition as exc:
        raise QuestionError("bad_state", str(exc), 409)
    return serialize(q)
