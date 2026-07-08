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
from server.model_files import require_model_files
from server.mt.base import TranslationEngine
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.rate_limit import JoinRateLimiter
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
    if config.asr.engine == "faster-whisper":
        # 遅延import: fake構成やテストでは faster-whisper を要求しない
        from server.asr.fw_engine import FasterWhisperEngine

        model_dir = config.models.resolve(config.asr.model)
        require_model_files(model_dir, 10_000_000, "ASRモデル")
        return FasterWhisperEngine(
            model_dir,
            compute_type=config.asr.compute_type,
            language=config.asr.language,
        )
    raise NotImplementedError(f"未知のASRエンジン: '{config.asr.engine}'")


def _require_language_coverage(config: AppConfig, supported: set[str]) -> None:
    missing = set(config.language_codes) - supported
    if missing:
        # 対応外言語を設定したまま起動しない（E-14 は join 時にも検証される）
        raise ValueError(
            f"翻訳エンジン '{config.mt.engine}' が未対応の言語が languages にある: {sorted(missing)}"
        )


def build_mt_engine(config: AppConfig) -> TranslationEngine:
    # 言語カバレッジ検証 → モデルファイル検証 → 構築、の順（設定ミスを先に報告する）
    if config.mt.engine == "fake":
        return FakeTranslationEngine(config.language_codes)
    if config.mt.engine == "nllb":
        # 遅延import: 使わないエンジンの依存を要求しない
        from server.mt.nllb_engine import NLLB_LANG_CODES, NllbEngine

        _require_language_coverage(config, set(NLLB_LANG_CODES))
        model_dir = config.models.resolve(config.mt.nllb.model_dir)
        tokenizer_dir = config.models.resolve(config.mt.nllb.tokenizer_dir)
        require_model_files(model_dir, 100_000_000, "NLLBモデル")
        require_model_files(tokenizer_dir, 100_000, "NLLBトークナイザ")
        return NllbEngine(model_dir, tokenizer_dir, beam_size=config.mt.nllb.beam_size)
    if config.mt.engine == "hy-mt2":
        from server.mt.hymt_engine import HYMT_LANG_LABELS, HyMt2Engine

        _require_language_coverage(config, set(HYMT_LANG_LABELS))
        gguf_path = config.models.resolve(config.mt.hy_mt2.gguf_path)
        require_model_files(gguf_path, 500_000_000, "Hy-MT2のGGUF")
        return HyMt2Engine(
            gguf_path,
            threads=config.mt.hy_mt2.threads,
            temperature=config.mt.hy_mt2.temperature,
        )
    raise NotImplementedError(f"未知の翻訳エンジン: '{config.mt.engine}'")


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
    join_limiter: JoinRateLimiter | None = None,
) -> FastAPI:
    session = Session(
        join_code=join_code or generate_join_code(), history_len=config.history_resend
    )
    limiter = join_limiter or JoinRateLimiter()
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
        # uvicorn は lifespan（モデルwarmup）完了後にしか応答しないため、
        # 応答が返る時点でロード済み。明示的な503ゲートは #16（運用パッケージ）
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
        ws: WebSocket, current: Client | None, msg: proto.JoinMessage, client_ip: str
    ) -> Client | None:
        if limiter.is_blocked(client_ip):
            # 総当たり対策（E-09）: コードの正誤にかかわらず一定時間拒否
            await ws.send_json(proto.JoinRejected(reason="rate_limited").model_dump())
            return current
        if not session.check_code(msg.code):
            limiter.record_failure(client_ip)
            await ws.send_json(proto.JoinRejected(reason="bad_code").model_dump())
            return current
        limiter.record_success(client_ip)
        if msg.role == "student" and msg.lang not in config.language_codes:
            await ws.send_json(proto.JoinRejected(reason="bad_lang").model_dump())
            return current
        if current is not None:
            session.remove_client(current.id)
        if msg.role == "teacher":
            old = session.teacher()
            if old is not None:
                # 後勝ち（E-08）: 旧接続を code 4000 で切断（クライアントは再接続しない）
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
                history_from=session.history_from,
                languages=config.languages,
                session_state=session.state,
            ).model_dump()
        )
        if msg.role == "student" and msg.last_seq is not None:
            await pipeline.replay_history(client, msg.last_seq)
        if msg.role == "teacher":
            pipeline.on_teacher_joined()  # 継続中の無音警告を新しい先生にも出せるよう再武装
            if session.auto_paused:
                # 先生切断による自動一時停止（E-07）は、先生の再接続で自動再開する
                session.auto_paused = False
                session.state = "live"
                await pipeline.broadcast_session_state()
        return client

    async def handle_control(action: str) -> None:
        session.auto_paused = False  # 明示操作は自動再開の対象にしない
        if action == "start":
            session.state = "live"
        elif action == "pause":
            session.state = "paused"
            await pipeline.flush_audio()  # 停止直前の発話を確定して処理
        elif action == "end":
            session.state = "ended"
            await pipeline.flush_audio()
        await pipeline.broadcast_session_state()

    async def handle_teacher_disconnect() -> None:
        """先生切断（E-07）: 配信中なら自動一時停止し、生徒にバナーを出す。
        後勝ち切断（新しい先生が既に接続済み）の場合は何もしない。"""
        if session.teacher() is None and session.state == "live":
            session.state = "paused"
            session.auto_paused = True
            await pipeline.flush_audio()
            await pipeline.broadcast_session_state()

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        client_ip = ws.client.host if ws.client else "unknown"
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
                    client = await handle_join(ws, client, msg, client_ip)
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
                if client.role == "teacher":
                    await handle_teacher_disconnect()

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="LinguaBridge サーバー")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    try:
        app = create_app(config)
    except FileNotFoundError as exc:  # モデル未取得（E-13）。トレースバックを見せない
        print(f"起動できません: {exc}")
        raise SystemExit(1) from exc
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
