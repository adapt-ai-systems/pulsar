"""Question routes: ask, list, answer and dismiss pulse questions."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Request

from pulsar.daemon import questions as questions_mod
from pulsar.daemon.http import ApiError, ok as _ok, store as _store
from pulsar.daemon.schemas import AnswerQuestionBody, AskQuestionBody


def build_router() -> APIRouter:
    router = APIRouter()

    # -- questions --------------------------------------------------------- #
    def _qerr(exc: questions_mod.QuestionError) -> ApiError:
        return ApiError(exc.code, str(exc), exc.status)

    @router.post("/questions")
    async def ask_question(request: Request, body: AskQuestionBody):
        """A pulse asks the user. Shown in the pane's question strip until
        answered; the answer arrives in the pulse's inbox."""
        try:
            q = questions_mod.ask(_store(request), body.pulse_id, body.text,
                                  choices=body.choices,
                                  default_choice=body.default_choice,
                                  context=body.context)
        except questions_mod.QuestionError as exc:
            raise _qerr(exc)
        return _ok(q, status=201)

    @router.get("/questions")
    async def list_questions(request: Request, pulse_id: Optional[str] = None,
                             state: Optional[str] = "open"):
        """Open questions by default; ``?state=`` (empty) for all.

        Archived pulses are excluded — their questions close at archive time,
        and the join keeps a stale row from surfacing."""
        qs = _store(request).list_questions(pulse_id=pulse_id,
                                            state=state or None,
                                            include_archived=False)
        return _ok([questions_mod.serialize(q) for q in qs])

    @router.post("/questions/{question_id}/answer")
    async def answer_question(request: Request, question_id: str,
                              body: AnswerQuestionBody):
        st = _store(request)
        try:
            q = await asyncio.to_thread(
                questions_mod.answer, st, question_id, body.text)
        except questions_mod.QuestionError as exc:
            raise _qerr(exc)
        return _ok(q)

    @router.post("/questions/{question_id}/dismiss")
    async def dismiss_question(request: Request, question_id: str):
        st = _store(request)
        try:
            q = await asyncio.to_thread(
                questions_mod.dismiss, st, question_id)
        except questions_mod.QuestionError as exc:
            raise _qerr(exc)
        return _ok(q)

    return router
