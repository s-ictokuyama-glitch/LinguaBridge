"""FastAPIエントリ。静的配信・HTTP API・WSエンドポイント。

HTTPS二重リッスンと証明書まわりは #16（運用パッケージ）。
#10 では先生ページを Windows 機の localhost で開く運用
（getUserMedia のセキュアコンテキストを localhost で満たす）。
"""

from __future__ import annotations

import contextlib
import logging
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from server import ws_protocol as proto
from server.asr.base import ASREngine
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, load_config
from server.mt.base import TranslationEngine
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.session import Client, Session, generate_join_code

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 参加コードを返すエンドポイントの許可元。コードは教室内掲示が前提の
# ソフトな防御（R-10）だが、LAN内の他端末へ無条件に晒さないよう
# ループバックに限定する。"testclient" は starlette TestClient の固定値。
# 先生ページを別端末のHTTPSで開く構成は #16 でトークン方式にする。
_TEACHER_INFO_HOSTS = {"127.0.0.1", "::1", "testclient"}


def build_asr_engine(config: AppConfig) -> ASREngine:
    if config.asr.engine == "fake":
        return FakeASREngine()
    raise NotImplementedError(f"ASRエンジン '{config.asr.engine}' は未実装（#11 で追加）")


def build_mt_engine(config: AppConfig) -> TranslationEngine:
    if config.mt.engine == "fake":
        return FakeTranslationEngine(config.language_codes)
    raise NotImplementedError(f"翻訳エンジン '{config.mt.engine}' は未実装（#12 で追加）")


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # UDPなので実送信はしない。経路のあるIF判定のみ
            return str(s.getsockname()[0])
    except OSError:
        with contextlib.suppress(OSError):
            return socket.gethostbyname(socket.gethostname())
    return "127.0.0.1"


def create_app(
    config: AppConfig,
    *,
    asr_engine: ASREngine | None = None,
    mt_engine: TranslationEngine | None = None,
    join_code: str | None = None,
) -> FastAPI:
    session = Session(
        join_code=join_code or generate_join_code(), history_len=config.history_resend
    )
    pipeline = Pipeline(
        session,
        config,
        asr_engine or build_asr_engine(config),
        mt_engine or build_mt_engine(config),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await pipeline.start()
        yield
        await pipeline.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.session = session
    app.state.pipeline = pipeline
    app.state.config = config

    @app.get("/")
    async def student_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/teacher")
    async def teacher_page() -> FileResponse:
        return FileResponse(WEB_DIR / "teacher.html")

    @app.get("/healthz")
    async def healthz() -> dict:
        # フェイクエンジンは即ロード完了。実モデルのロード完了ゲート（503）は #11/#16
        return {"status": "ok"}

    @app.get("/api/config")
    async def api_config() -> dict:
        return {"languages": [lang.model_dump() for lang in config.languages]}

    @app.get("/api/teacher-info")
    async def teacher_info(request: Request) -> JSONResponse:
        host = request.client.host if request.client else None
        if host not in _TEACHER_INFO_HOSTS:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        join_url = f"http://{get_lan_ip()}:{config.server.http_port}/?code={session.join_code}"
        return JSONResponse(
            {
                "code": session.join_code,
                "join_url": join_url,
                "languages": [lang.model_dump() for lang in config.languages],
            }
        )

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    async def handle_join(
        ws: WebSocket, current: Client | None, msg: proto.JoinMessage
    ) -> Client | None:
        if not session.check_code(msg.code):
            await ws.send_json(proto.JoinRejected(reason="bad_code").model_dump())
            return current
        if msg.role == "student" and msg.lang not in config.language_codes:
            await ws.send_json(proto.JoinRejected(reason="bad_lang").model_dump())
            return current
        if current is not None:
            session.remove_client(current.id)
        if msg.role == "teacher":
            old = session.teacher()
            if old is not None:
                # 後勝ち（E-08）。旧接続への通知UIは #13
                session.remove_client(old.id)
                if old.ws is not None:
                    with contextlib.suppress(Exception):
                        await old.ws.close(code=4000)
        client = Client(
            id=uuid.uuid4().hex,
            role=msg.role,
            lang=msg.lang if msg.role == "student" else None,
            ws=ws,
        )
        session.add_client(client)
        await ws.send_json(
            proto.Joined(
                seq_head=session.seq_head,
                languages=config.languages,
                session_state=session.state,
            ).model_dump()
        )
        if msg.role == "student" and msg.last_seq is not None:
            for utt, translation in session.history_since(msg.last_seq, client.lang):
                await ws.send_json(
                    proto.Caption(
                        seq=utt.seq,
                        ja=utt.text_ja,
                        text=translation.text,
                        lang=translation.lang,
                        delay_ms=0,
                    ).model_dump()
                )
        return client

    async def handle_control(action: str) -> None:
        if action == "start":
            session.state = "live"
        elif action == "pause":
            session.state = "paused"
            await pipeline.flush_audio()  # 停止直前の発話を確定して処理
        elif action == "end":
            session.state = "ended"
            await pipeline.flush_audio()
        await pipeline.broadcast_session_state()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        client: Client | None = None
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    if client is not None and client.role == "teacher":
                        await pipeline.feed_audio(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    msg = proto.parse_client_message(text)
                except proto.ProtocolError as exc:
                    await ws.send_json(
                        proto.ErrorMsg(code="bad_message", message=str(exc)).model_dump()
                    )
                    continue
                if isinstance(msg, proto.JoinMessage):
                    client = await handle_join(ws, client, msg)
                elif client is None:
                    await ws.send_json(
                        proto.ErrorMsg(code="not_joined", message="先に join してください").model_dump()
                    )
                elif isinstance(msg, proto.SetLangMessage):
                    if client.role == "student":
                        if msg.lang in config.language_codes:
                            client.lang = msg.lang
                        else:
                            await ws.send_json(
                                proto.ErrorMsg(
                                    code="bad_lang", message=f"未対応の言語: {msg.lang}"
                                ).model_dump()
                            )
                elif isinstance(msg, proto.ControlMessage):
                    if client.role == "teacher":
                        await handle_control(msg.action)
                elif isinstance(msg, proto.RecordingMessage):
                    if client.role == "teacher":
                        session.recording = msg.on  # 書き出しとインジケーターは #18
        except WebSocketDisconnect:
            pass
        finally:
            if client is not None:
                session.remove_client(client.id)

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="LinguaBridge サーバー")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    app = create_app(config)
    session: Session = app.state.session
    ip = get_lan_ip()
    print("=" * 60)
    print("LinguaBridge サーバー起動")
    print(f"  参加コード : {session.join_code}")
    print(f"  生徒用URL  : http://{ip}:{config.server.http_port}/?code={session.join_code}")
    print(f"  先生ページ : http://127.0.0.1:{config.server.http_port}/teacher")
    print("    （マイクを使うため、先生ページはこのPCの localhost で開くこと）")
    print("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=config.server.http_port, log_level="info")


if __name__ == "__main__":
    main()
