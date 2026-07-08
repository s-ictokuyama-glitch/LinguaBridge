"""FastAPIエントリ。静的配信・HTTP API・WSエンドポイント。

平文HTTP(生徒用)と自己署名HTTPS(先生用)を同時リッスンする（#16）。
先生ページは HTTPS で開けば別端末でも getUserMedia のセキュアコンテキストを
満たす。証明書が無ければ HTTP 単独で起動し、先生は localhost で開く運用に退避する。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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

# 平文HTTP から参加コードを取れる送信元（先生がこのPCの localhost で開く場合）。
# HTTPS からは常に許可する（先生ページの正規経路）。"testclient" は TestClient の固定値。
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
    async def healthz() -> JSONResponse:
        # モデルの事前ロード完了まで 503（E-13）。start.bat はこれが 200 になってから
        # ブラウザを開く。ロード中でもサーバー自体は起動済みで応答する
        if not pipeline.ready:
            return JSONResponse({"status": "loading"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/config")
    async def api_config() -> dict:
        return {"languages": [lang.model_dump() for lang in config.languages]}

    @app.get("/api/teacher-info")
    async def teacher_info(request: Request) -> JSONResponse:
        # 参加コードを晒す口。先生ページは HTTPS(8443) 側で開く運用なので https は許可、
        # 平文HTTP(生徒用)からはループバックのみ許可し部外者のコード取得を抑止（R-10）
        host = request.client.host if request.client else None
        if request.url.scheme != "https" and host not in _TEACHER_INFO_HOSTS:
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


async def _open_teacher_page_when_ready(pipeline: Pipeline, url: str) -> None:
    """モデルのロード完了（ready）を待ってから既定ブラウザで先生ページを開く。"""
    import webbrowser

    for _ in range(600):  # 最大60秒待つ
        if pipeline.ready:
            break
        await asyncio.sleep(0.1)
    webbrowser.open(url)


def cert_days_remaining(cert_path: Path) -> int | None:
    """証明書の残存有効日数（E-15）。読めなければ None。"""
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return (cert.not_valid_after_utc - datetime.now(timezone.utc)).days
    except Exception:
        return None


async def _serve(app: FastAPI, config: AppConfig, *, open_browser: bool) -> None:
    import signal

    import uvicorn

    server_configs = [
        uvicorn.Config(app, host="0.0.0.0", port=config.server.http_port, log_level="info")
    ]
    teacher_url = f"http://127.0.0.1:{config.server.http_port}/teacher"
    if config.server.tls_ready():
        server_configs.append(
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=config.server.https_port,
                log_level="warning",
                lifespan="off",  # lifespan(モデル起動)はHTTP側で1度だけ走らせる
                ssl_certfile=str(config.server.cert_path()),
                ssl_keyfile=str(config.server.key_path()),
            )
        )
        teacher_url = f"https://127.0.0.1:{config.server.https_port}/teacher"

    servers = [uvicorn.Server(c) for c in server_configs]
    # 2サーバーが個別にSIGINTを奪い合うと、片方（lifespanを持つHTTP側）が終了せず
    # pipeline.stop() が走らない。各サーバーの個別ハンドラを抑止し、共有ハンドラで
    # 全サーバーに終了を伝える（Windowsは Ctrl+C=SIGINT）
    for server in servers:
        server.install_signal_handlers = lambda: None  # type: ignore[attr-defined]

    def _request_shutdown(*_: object) -> None:
        for server in servers:
            server.should_exit = True

    with contextlib.suppress(ValueError):  # signal はメインスレッドでのみ設定可
        signal.signal(signal.SIGINT, _request_shutdown)
        signal.signal(signal.SIGTERM, _request_shutdown)

    browser_task = (
        asyncio.create_task(_open_teacher_page_when_ready(app.state.pipeline, teacher_url))
        if open_browser
        else None
    )
    try:
        await asyncio.gather(*(server.serve() for server in servers))
    finally:
        if browser_task is not None:
            browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="LinguaBridge サーバー")
    parser.add_argument("--config", default="config.yaml", help="設定ファイルのパス")
    parser.add_argument(
        "--open-browser", action="store_true", help="起動後に先生ページを既定ブラウザで開く"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config)
    try:
        app = create_app(config)
    except (FileNotFoundError, ValueError) as exc:  # モデル欠損/設定不整合（E-13）。生tbを見せない
        print(f"起動できません: {exc}")
        raise SystemExit(1) from exc
    session: Session = app.state.session
    ip = get_lan_ip()
    https = config.server.tls_ready()
    teacher_line = (
        f"https://{ip}:{config.server.https_port}/teacher（別端末可・初回のみ証明書警告を承認）"
        if https
        else f"http://127.0.0.1:{config.server.http_port}/teacher（このPCで開く。別端末HTTPSは要 setup.ps1）"
    )
    print("=" * 66)
    print("LinguaBridge サーバー起動")
    print(f"  参加コード : {session.join_code}")
    print(f"  生徒用URL  : http://{ip}:{config.server.http_port}/?code={session.join_code}")
    print(f"  先生ページ : {teacher_line}")
    if not https:
        print("    （マイクにはセキュアコンテキストが必要。証明書が無いため localhost 運用）")
    else:
        days = cert_days_remaining(config.server.cert_path())
        if days is not None and days < 30:
            state = "期限切れ" if days < 0 else f"残り{days}日"
            print(f"  ⚠ 証明書の有効期限が近い/切れています（{state}）。")
            print("    python scripts\\make_cert.py --force で再生成してください。")
    print("=" * 66)
    try:
        asyncio.run(_serve(app, config, open_browser=args.open_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
