"""授業の参加、進行、記録のルール。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from server import ws_protocol as proto
from server.caption_history import Utterance
from server.config import AppConfig
from server.pipeline import Pipeline
from server.rate_limit import JoinRateLimiter
from server.recorder import SessionRecorder
from server.roster import Client, ClientSocket


logger = logging.getLogger(__name__)


def generate_join_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


class RecordingSaveError(Exception):
    pass


class Lesson:
    def __init__(self, pipeline: Pipeline, config: AppConfig, *, join_code: str,
                 teacher_code: str | None = None,
                 limiter: JoinRateLimiter | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 recording_wait_s: float = 30) -> None:
        self.pipeline = pipeline
        self.roster = pipeline.roster
        self.join_code = join_code
        self.teacher_code = (
            teacher_code if teacher_code is not None else f"{secrets.randbelow(1_000_000):06d}"
        )
        self._config = config
        self._limiter = limiter or JoinRateLimiter(clock=clock)
        self.state: proto.SessionState = "idle"
        self.auto_paused = False
        self.recording = config.recording.default_on
        self._recording_wait_s = recording_wait_s
        self._recorder = self._new_recorder()
        self._recording_task: asyncio.Task[Path | None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._control_version = 0
        pipeline.on_utterance = self._record_utterance

    def _new_recorder(self) -> SessionRecorder:
        return SessionRecorder(self._config.recording.resolved_out_dir,
                               self._config.language_codes)

    def _record_utterance(self, utterance: Utterance) -> None:
        if self.recording:
            self._recorder.add(utterance)

    def _needs_recording_save(self) -> bool:
        return self.recording or self._recorder.has_entries or self._recording_task is not None

    async def _transition(self, state: proto.SessionState) -> None:
        await self.pipeline.set_accepting_audio(state == "live")
        if self.state == "live" and state != "live":
            await self.pipeline.flush_audio()
        self.state = state
        self.roster.broadcast(proto.SessionStateMsg(state=state).model_dump())

    async def control(self, action: Literal["start", "pause", "end"]) -> Path | None:
        if self._shutdown_task is not None:
            return None
        self._control_version += 1
        version = self._control_version
        self.auto_paused = False
        states: dict[str, proto.SessionState] = {
            "start": "live", "pause": "paused", "end": "ended"
        }
        try:
            if self.state == "ended" and action != "end":
                await self._finalize_lesson()
                if self._shutdown_task is not None or version != self._control_version:
                    return None
                self._recorder = self._new_recorder()
                self._recording_task = None
            await self._transition(states[action])
            if action == "end" and self._needs_recording_save():
                return await self._finalize_lesson()
        except TimeoutError:
            self.roster.send_teacher(proto.ErrorMsg(
                code="recording_pending",
                message="前の授業の音声・翻訳を処理中です。保存完了後に再開できます。しばらく待ってから再度操作してください。",
            ).model_dump())
        except RecordingSaveError:
            pass
        return None

    def _write_recording(self, *, final: bool) -> Path | None:
        try:
            return self._recorder.write(final=final)
        except OSError as error:
            logger.exception("授業記録の保存に失敗しました")
            self.roster.send_teacher(proto.ErrorMsg(
                code="recording_save_failed",
                message="授業記録を保存できませんでした。保存先を確認し、もう一度「終了」を押してください。",
            ).model_dump())
            raise RecordingSaveError("授業記録の保存に失敗しました") from error

    async def _finish_recording(self) -> Path | None:
        await self.pipeline.drain(timeout=None)
        saved = self._write_recording(final=True)
        if saved is not None:
            logger.info("授業記録を保存しました: %s", saved)
        return saved

    def _recording_finished(self, task: asyncio.Task[Path | None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not isinstance(error, RecordingSaveError):
            logger.error("授業記録の確定処理に失敗しました", exc_info=error)

    async def _finalize_lesson(self) -> Path | None:
        task = self._recording_task
        if task is not None and task.done() and not task.cancelled() and task.exception() is None:
            return None
        if task is None or task.done():
            task = asyncio.create_task(self._finish_recording(), name="recording-finalizer")
            task.add_done_callback(self._recording_finished)
            self._recording_task = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._recording_wait_s)
        except TimeoutError:
            if task.done():
                return task.result()
            self._write_recording(final=False)
            raise

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown(), name="lesson-shutdown")
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self) -> None:
        try:
            if self.state != "ended":
                await self._transition("ended")
            if self._needs_recording_save():
                with contextlib.suppress(TimeoutError, RecordingSaveError):
                    await self._finalize_lesson()
        finally:
            if self._recording_task is not None:
                self._recording_task.cancel()
                await asyncio.gather(self._recording_task, return_exceptions=True)
            await self.roster.aclose()

    async def leave(self, client: Client) -> None:
        self.pipeline.captions.cancel_history_refresh(client)
        await self.roster.remove(client)
        if (self._shutdown_task is None and client.role == "teacher"
                and self.roster.teacher() is None and self.state == "live"):
            self.auto_paused = True
            await self._transition("paused")

    def set_recording(self, on: bool) -> None:
        if self._shutdown_task is not None:
            return
        self.recording = on
        self.roster.broadcast(proto.RecordingState(on=on).model_dump())

    async def send_now(self, socket: ClientSocket, payload: dict) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(socket.send_json(payload),
                                   timeout=self._config.limits.send_timeout_s)

    async def join(self, socket: ClientSocket, current: Client | None,
                   msg: proto.JoinMessage, client_ip: str) -> Client | None:
        if self._shutdown_task is not None:
            return current
        reason: proto.JoinRejectReason | None = None
        expected_code = self.teacher_code if msg.role == "teacher" else self.join_code
        if self._limiter.is_blocked(client_ip):
            reason = "rate_limited"
        elif not msg.code.isascii() or not secrets.compare_digest(msg.code, expected_code):
            self._limiter.record_failure(client_ip, role=msg.role)
            reason = "bad_code"
        else:
            self._limiter.record_success(client_ip, role=msg.role)
            if msg.role == "student" and msg.lang not in self._config.language_codes:
                reason = "bad_lang"
            elif (msg.role == "student"
                  and not (current is not None and self.roster.contains(current)
                           and current.role == "student")
                  and len(self.roster.students()) >= self._config.limits.max_students):
                reason = "full"
        if reason is not None:
            payload = proto.JoinRejected(reason=reason).model_dump()
            if current is not None and self.roster.contains(current):
                self.roster.send(current, payload)
            else:
                await self.send_now(socket, payload)
            return current
        if current is not None:
            self.pipeline.captions.cancel_history_refresh(current)
        client = await self.roster.add(
            socket, msg.role, msg.lang, current=current,
            initial=proto.Joined(
                seq_head=self.pipeline.captions.history.seq_head,
                history_from=self.pipeline.captions.history.history_from,
                languages=self._config.languages, session_state=self.state,
                recording=self.recording, speaking=self.pipeline.speaking,
            ).model_dump(),
        )
        if msg.role == "student" and msg.last_seq is not None:
            await self.pipeline.captions.replay_history(client, msg.last_seq)
        if (self._shutdown_task is None and msg.role == "teacher"
                and self.roster.contains(client)):
            self.pipeline.on_teacher_joined()
            if self.auto_paused:
                self.auto_paused = False
                await self._transition("live")
        return client

    async def set_lang(self, client: Client, lang: str) -> None:
        if (self._shutdown_task is not None or not self.roster.contains(client)
                or client.role != "student"):
            return
        if lang in self._config.language_codes:
            self.pipeline.captions.set_language(client, lang)
        else:
            self.roster.send(client, proto.ErrorMsg(
                code="bad_lang", message=f"未対応の言語: {lang}"
            ).model_dump())
