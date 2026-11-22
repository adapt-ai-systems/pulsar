"""Message routes: post a message to a pulse and fetch attachments."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

from pulsar.daemon import (
    messages as messages_mod,
    paths,
    reconcile as reconcile_mod,
    watcher as watcher_mod,
)
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    invalidate_roster as _invalidate_roster,
    ok as _ok,
    store as _store,
)
from pulsar.daemon import loop_triggers
from pulsar.daemon.routes.common import _write_message
from pulsar.daemon.schemas import MessageBody, ReplyRef
from pulsar.store import UnknownPulse

logger = logging.getLogger("pulsar.daemon")


# --------------------------------------------------------------------------- #
# Message ingest (atomic)
# --------------------------------------------------------------------------- #
# A replied-to message longer than this is clipped in the quote the pulse gets.
REPLY_QUOTE_MAX_CHARS = 4000

def _reply_ref(reply_to: Any) -> Optional[dict[str, Any]]:
    """Normalize a reply_to (ReplyRef, dict, or multipart JSON string) to
    ``{"text", "ts"?}`` with the text clipped; None when absent/empty."""
    if reply_to is None or reply_to == "":
        return None
    try:
        if isinstance(reply_to, str):
            reply_to = json.loads(reply_to)
        ref = ReplyRef.model_validate(reply_to)
    except (json.JSONDecodeError, ValidationError):
        raise ApiError("bad_request", "reply_to must be {text, ts?}", 422)
    text = ref.text.strip()
    if not text:
        return None
    if len(text) > REPLY_QUOTE_MAX_CHARS:
        text = text[:REPLY_QUOTE_MAX_CHARS].rstrip() + " …[clipped]"
    out: dict[str, Any] = {"text": text}
    if ref.ts:
        out["ts"] = ref.ts
    return out

def _with_reply_quote(reply: Optional[dict[str, Any]], text: str) -> str:
    """Prefix what the pulse receives with the message the user replied to."""
    if not reply:
        return text
    when = f" (sent {reply['ts']})" if reply.get("ts") else ""
    quoted = "\n".join(f"> {line}" for line in reply["text"].splitlines())
    head = f"[The user is replying to this earlier message of yours{when}:]\n{quoted}"
    return f"{head}\n\n{text}" if text else head

# Attachment limits (chat uploads — paste/attach in the pane);
# env-overridable for tests.
ATTACHMENT_MAX_COUNT = int(os.environ.get("PULSAR_MAX_ATTACHMENT_COUNT", "30"))
ATTACHMENT_MAX_BYTES = int(
    os.environ.get("PULSAR_MAX_ATTACHMENT_SIZE_BYTES", str(500 * 1024 * 1024)))
ATTACHMENT_ALLOWED_MIME = frozenset(
    t.strip() for t in os.environ.get(
        "PULSAR_ATTACHMENT_ALLOWED_MIME_TYPES",
        "image/png,image/jpeg,image/gif,image/webp,text/plain,application/pdf,"
        "text/markdown,text/csv,application/json,video/mp4,video/quicktime,"
        "video/webm,video/x-m4v,video/mpeg,video/x-matroska,video/x-msvideo,"
        "audio/mpeg,audio/mp4,audio/x-m4a,audio/wav,audio/x-wav,audio/ogg,"
        "audio/webm,audio/flac,audio/x-flac,audio/aac,audio/x-aac,audio/opus,"
        "audio/x-opus+ogg,"
        "application/zip,application/x-zip-compressed,"
        "model/obj,model/mtl,model/stl,model/gltf-binary,model/gltf+json,"
        "application/x-ply,model/vnd.usdz+zip,application/x-bittorrent",
    ).split(",") if t.strip())
ATTACHMENT_COPY_CHUNK = 1024 * 1024

# Some browsers leave the MIME blank for formats such as MKV. Keep the daemon
# allowlist strict while recognizing known video extensions in that one case.
VIDEO_MIME_BY_SUFFIX = {
    ".avi": "video/x-msvideo",
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".webm": "video/webm",
}

# Same browser-MIME quirk exists for audio — an attached audio clip often
# uploads as application/octet-stream. Only recognized extensions infer a
# MIME; everything else stays blocked. Extension inference is video-first,
# then audio, in the handler.
AUDIO_MIME_BY_SUFFIX = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}

# Archives and 3D scan/mesh exports (phone LiDAR scans → zip/obj/ply/stl)
# nearly always arrive as application/octet-stream or a vendor-specific zip
# MIME. Infer from the extension the same way as video/audio above.
ARCHIVE_MODEL_MIME_BY_SUFFIX = {
    ".zip": "application/zip",
    ".obj": "model/obj",
    ".mtl": "model/mtl",
    ".stl": "model/stl",
    ".ply": "application/x-ply",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".usdz": "model/vnd.usdz+zip",
    ".torrent": "application/x-bittorrent",
}

def _validate_attachment_filename(filename: str) -> None:
    """400 on path-traversal / control-char filenames."""
    if (filename in ("", ".", "..")
            or "/" in filename or "\\" in filename or "\0" in filename
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in filename)):
        raise ApiError("invalid_filename", f"bad filename {filename!r}", 400)


# Normal stop reasons (deliberate pause, watch sleep) still accept queued user
# messages with HTTP 202 and deliver them when the pulse is restored.
_NON_FAILURE_STOP_REASONS = frozenset({"", "watch work complete", "user"})


def _is_runner_failure_reason(reason: Optional[str]) -> bool:
    """True when ``stop_reason`` came from a runner crash or startup failure."""
    clean = (reason or "").strip()
    return bool(clean) and clean.lower() not in _NON_FAILURE_STOP_REASONS


def _stopped_message_text(reason: Optional[str]) -> str:
    """Plain-English refusal text when a user sends to a pulse whose runner failed."""
    clean = (reason or "").strip()
    low = clean.lower()
    if "vessel failed to start" in low or "not found" in low:
        hint = "Install the tool or pick another model."
    else:
        hint = "Restore the pulse before sending a message."
    if clean:
        return f"Pulse is stopped: {clean}. {hint}"
    return f"Pulse is stopped. {hint}"


def _ensure_user_message_target_alive(request: Request, pulse) -> None:
    """Refuse a direct user message ONLY when the runner died or failed to start,
    so messages never look accepted and vanish. A pulse paused by the user or
    stopped normally still queues messages with 202 for delivery on restore."""
    if pulse.state == "stopped":
        reason = (pulse.settings or {}).get("stop_reason")
        if _is_runner_failure_reason(reason):
            raise ApiError("pulse_stopped", _stopped_message_text(reason), 409)
        return
    if pulse.state != "running":
        return
    beat = paths.read_heartbeat(pulse.id)
    if reconcile_mod.is_fresh_dead_beat(pulse, beat):
        reason = (beat or {}).get("reason") or "runner stopped"
        st = _store(request)
        try:
            st.update_settings(pulse.id, {"stop_reason": reason})
            st.set_state(pulse.id, "stopped")
            _invalidate_roster(request)
        except Exception:
            logger.exception("message pulse=%s FAILED to mark dead runner stopped",
                             pulse.id)
        logger.warning("message pulse=%s REFUSED dead runner reason=%r",
                       pulse.id, reason)
        raise ApiError("pulse_stopped", _stopped_message_text(reason), 409)


def build_router() -> APIRouter:
    router = APIRouter()

    # -- messages ---------------------------------------------------------- #
    @router.post("/pulses/{pulse_id}/messages")
    async def post_message(request: Request, pulse_id: str):
        pulse = _get_pulse(request, pulse_id)
        content_type = request.headers.get("content-type", "")

        # Multipart = message with attachments (paste/attach in the pane).
        # Files land under <home>/attachments/<msg-id>/; the vessel gets the
        # typed text plus an "Attached file: <path>" block so the agent can
        # Read them.
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            text = str(form.get("text") or "").strip()
            reply = _reply_ref(form.get("reply_to"))
            parts = [p for p in form.getlist("file") if hasattr(p, "filename")]
            if not text and not parts:
                raise ApiError("bad_request", "need text or at least one file", 422)
            if len(parts) > ATTACHMENT_MAX_COUNT:
                raise ApiError("too_many_files",
                               f"{len(parts)} files, max {ATTACHMENT_MAX_COUNT}", 413)
            uploads: list[tuple[str, str, Any]] = []  # (filename, mime, UploadFile)
            for part in parts:
                filename = Path(part.filename or "").name
                _validate_attachment_filename(filename)
                mime = part.content_type or "application/octet-stream"
                if mime == "application/octet-stream":
                    mime = (VIDEO_MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
                            or AUDIO_MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
                            or ARCHIVE_MODEL_MIME_BY_SUFFIX.get(Path(filename).suffix.lower())
                            or mime)
                # Markdown arrives as octet-stream, text/plain or text/x-markdown
                # depending on the OS (iOS sends no type at all for .md).
                if Path(filename).suffix.lower() in (".md", ".markdown"):
                    mime = "text/markdown"
                # Browsers/OSes disagree on zip MIME; normalize the vendor form.
                if mime == "application/x-zip-compressed":
                    mime = "application/zip"
                if mime not in ATTACHMENT_ALLOWED_MIME:
                    raise ApiError("disallowed_mime_type",
                                   f"{mime} not allowed ({filename})", 415)
                if part.size is not None and part.size > ATTACHMENT_MAX_BYTES:
                    raise ApiError(
                        "file_too_large",
                        f"{filename} is {part.size} bytes, max {ATTACHMENT_MAX_BYTES}",
                        413)
                uploads.append((filename, mime, part))

            _ensure_user_message_target_alive(request, pulse)
            msg_id = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                      + "-" + uuid.uuid4().hex[:8])
            dest_dir = paths.attachments_dir(pulse.id) / msg_id
            saved: list[dict[str, Any]] = []
            try:
                if uploads:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                for filename, mime, part in uploads:
                    dest = dest_dir / filename
                    # Two pastes named "image.png" in one message must not
                    # clobber each other — suffix the later ones.
                    n = 2
                    while dest.exists():
                        dest = dest_dir / f"{dest_dir.name}-{n}-{filename}"
                        n += 1
                    size = 0
                    with dest.open("wb") as out:
                        while chunk := await part.read(ATTACHMENT_COPY_CHUNK):
                            size += len(chunk)
                            if size > ATTACHMENT_MAX_BYTES:
                                raise ApiError(
                                    "file_too_large",
                                    f"{filename} exceeds {ATTACHMENT_MAX_BYTES} bytes",
                                    413)
                            out.write(chunk)
                    saved.append({
                        "filename": dest.name,
                        "path": str(dest),
                        "url": (f"/pulses/{quote(pulse.id, safe='')}/attachments/"
                                f"{quote(msg_id, safe='')}/{quote(dest.name, safe='')}"),
                        "size": size,
                        "mime_type": mime,
                    })
                delivered = text
                if saved:
                    block = "\n".join(
                        f"Attached file: {a['path']} ({a['mime_type']}, "
                        f"{a['size']} bytes) — open with the Read tool to view."
                        for a in saved)
                    if any(a["mime_type"].startswith("image/") for a in saved):
                        from pulsar.vessel.vision_sidecar import IMAGE_INSTRUCTION
                        block = f"{block}\n\n{IMAGE_INSTRUCTION}"
                    delivered = f"{text}\n\n{block}".strip() if text else block
                delivered = loop_triggers.append_to_message(
                    _store(request), pulse.id, delivered)
                name = _write_message(pulse.id,
                                      _with_reply_quote(reply, delivered),
                                      display_text=text, attachments=saved,
                                      reply_to=reply)
            except (ApiError, OSError) as exc:
                logger.error("message pulse=%s attachment ingest FAILED err=%s",
                             pulse.id, exc)
                # Don't leave orphan files for a message that never queued.
                shutil.rmtree(dest_dir, ignore_errors=True)
                if isinstance(exc, ApiError):
                    raise
                raise ApiError("ingest_failed", str(exc), 500)
            _store(request).touch_input(pulse.id)  # the user's own input (v17)
            return _ok({"queued": name, "attachments": saved}, status=202)

        # JSON — the original text-only contract.
        try:
            body = MessageBody.model_validate(await request.json())
        except json.JSONDecodeError:
            raise ApiError("bad_request", "body must be JSON", 400)
        except ValidationError as exc:
            raise ApiError("bad_request",
                           "; ".join(e["msg"] for e in exc.errors()), 422)
        # Pulse-to-pulse: from_pulse_id names the sender. The
        # messages engine validates (self/archived), wakes a stopped target,
        # and prefixes the text with the sender's title + reply address.
        if body.from_pulse_id:
            st = _store(request)
            try:
                name = await asyncio.to_thread(
                    messages_mod.send,
                    st, body.from_pulse_id, pulse.id, body.text)
            except UnknownPulse as exc:
                raise ApiError("not_found", str(exc), 404)
            except messages_mod.MessageError as exc:
                raise ApiError("bad_request", str(exc), 400)
            # Delegation watch, alert-only: if the target never
            # consumes this and goes to work, the sender hears about it. One
            # active watch per sender→target pair — a burst of messages must
            # not spawn a watch per message. A NEW message on a pair already
            # in the follow phase (v12) re-baselines that watch instead: its
            # zombie/stall counters measure progress against the LATEST
            # message, not the first one.
            try:
                existing = st.active_watch(body.from_pulse_id, pulse.id)
                if existing is None:
                    beat = paths.read_heartbeat(pulse.id) or {}
                    st.create_watch(
                        body.from_pulse_id, pulse.id, "message",
                        next_check_at=watcher_mod.next_check_iso(),
                        confirm=False,
                        baseline_ctx=beat.get("last_ctx_tokens"))
                elif existing.phase == "follow":
                    beat = paths.read_heartbeat(pulse.id) or {}
                    st.arm_follow(
                        existing.id,
                        watcher_mod.next_check_iso(),
                        last_ctx=beat.get("last_ctx_tokens"),
                        baseline_ctx=beat.get("last_ctx_tokens"))
            except Exception as exc:
                logger.error("message sender=%s target=%s watch FAILED err=%s",
                             body.from_pulse_id, pulse.id, exc)
            return _ok({"queued": name}, status=202)
        _ensure_user_message_target_alive(request, pulse)
        try:
            # Reply button: the pulse gets the quoted message ahead of their text,
            # the pane shows their text with the quote chip (reply_to on the event).
            reply = _reply_ref(body.reply_to.model_dump() if body.reply_to else None)
            # Standing loops (v25): every message the user sends carries the
            # prompt of each enabled loop with the "message" trigger, appended
            # AFTER their words; the pane still shows only what they typed.
            delivered = loop_triggers.append_to_message(
                _store(request), pulse.id, body.text)
            looped = delivered != body.text
            name = _write_message(pulse.id,
                                  _with_reply_quote(reply, delivered),
                                  display_text=(body.text if reply or looped
                                                else None),
                                  reply_to=reply)
        except OSError as exc:
            logger.error("message pulse=%s FAILED err=%s", pulse.id, exc)
            raise ApiError("ingest_failed", str(exc), 500)
        _store(request).touch_input(pulse.id)  # no from_pulse_id: user typed it (v17)
        return _ok({"queued": name}, status=202)

    # -- attachments (chat thumbnails / open-in-tab) ------------------------ #
    @router.get("/pulses/{pulse_id}/attachments/{msg_id}/{filename}")
    async def get_attachment(request: Request, pulse_id: str, msg_id: str,
                             filename: str):
        pulse = _get_pulse(request, pulse_id)
        base = paths.attachments_dir(pulse.id).resolve()
        target = (base / msg_id / filename).resolve()
        # Traversal guard: the resolved target must stay under attachments/.
        if not target.is_relative_to(base) or not target.is_file():
            raise ApiError("not_found", "no such attachment", 404)
        return FileResponse(target)

    return router
