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
from server.config import AppConfig
from server.pipeline import Pipeline
from server.rate_limit import JoinRateLimiter
from server.roster import Client, ClientSocket


logger = logging.getLogger(__name__)


def generate_join_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


class Lesson:
    def __init__(self, pipeline: Pipeline, config: AppConfig, *, join_code: str,
                 limiter: JoinRateLimiter | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.pipeline = pipeline
        self.roster = pipeline.roster
        self.join_code = join_code
        self._config = config
        self._limiter = limiter or JoinRateLimiter(clock=clock)
        self.state: proto.SessionState = "idle"
        self.auto_paused = False
        self.recording = config.recording.default_on

    async def _transition(self, state: proto.SessionState) -> None:
        self.state = state
        await self.pipeline.on_lesson_state(state)
        self.roster.broadcast(proto.SessionStateMsg(state=state).model_dump())

    async def control(self, action: Literal["start", "pause", "end"]) -> Path | None:
        self.auto_paused = False
        states: dict[str, proto.SessionState] = {
            "start": "live", "pause": "paused", "end": "ended"
        }
        await self._transition(states[action])
        if action == "end":
            saved = await self.pipeline.finalize_recording()
            if saved is not None:
                logger.info("授業記録を保存しました: %s", saved)
            return saved
        return None

    async def leave(self, client: Client) -> None:
        await self.roster.remove(client)
        if client.role == "teacher" and self.roster.teacher() is None and self.state == "live":
            self.auto_paused = True
            await self._transition("paused")

    def set_recording(self, on: bool) -> None:
        self.recording = on
        self.pipeline.set_recording(on)
        self.roster.broadcast(proto.RecordingState(on=on).model_dump())

    async def send_now(self, socket: ClientSocket, payload: dict) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(socket.send_json(payload),
                                   timeout=self._config.limits.send_timeout_s)

    async def join(self, socket: ClientSocket, current: Client | None,
                   msg: proto.JoinMessage, client_ip: str) -> Client | None:
        reason: proto.JoinRejectReason | None = None
        if self._limiter.is_blocked(client_ip):
            reason = "rate_limited"
        elif msg.code != self.join_code:
            self._limiter.record_failure(client_ip)
            reason = "bad_code"
        else:
            self._limiter.record_success(client_ip)
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
        client = await self.roster.add(
            socket, msg.role, msg.lang, current=current,
            initial=proto.Joined(
                seq_head=self.pipeline.history.seq_head,
                history_from=self.pipeline.history.history_from,
                languages=self._config.languages, session_state=self.state,
                recording=self.recording, speaking=self.pipeline.speaking,
            ).model_dump(),
        )
        if msg.role == "student" and msg.last_seq is not None:
            await self.pipeline.replay_history(client, msg.last_seq)
        if msg.role == "teacher":
            self.pipeline.on_teacher_joined()
            if self.auto_paused:
                self.auto_paused = False
                await self._transition("live")
        return client

    async def set_lang(self, client: Client, lang: str) -> None:
        if not self.roster.contains(client) or client.role != "student":
            return
        if lang in self._config.language_codes:
            client.lang = lang
        else:
            self.roster.send(client, proto.ErrorMsg(
                code="bad_lang", message=f"未対応の言語: {lang}"
            ).model_dump())
