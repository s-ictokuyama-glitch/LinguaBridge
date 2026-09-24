"""FastAPIエントリ。静的配信・HTTP API・WSエンドポイント。

平文HTTP(生徒用)と自己署名HTTPS(先生用)を同時リッスンする（#16）。
先生ページは HTTPS で開けば別端末でも getUserMedia のセキュアコンテキストを
満たす。証明書が無ければ HTTP 単独で起動し、先生は localhost で開く運用に退避する。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from server import event_loop
from server import ws_protocol as proto
from server.asr.base import ASREngine
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, add_layer_arguments, config_cli_args, load_config
from server.diagnostics import record_template
from server.certificates import certificate_ready, inspect_certificate, print_certificate_report
from server.model_files import require_model_files
from server.network import PublishedAddress, choose_ip, resolve_ip
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
            cpu_threads=config.asr.cpu_threads,
        )
    if config.asr.engine == "sherpa":
        # 遅延import: 他構成では sherpa-onnx を要求しない
        from server.asr.sherpa_engine import SherpaOnnxEngine

        model_dir = config.models.resolve(config.asr.sherpa.model)
        require_model_files(model_dir, 10_000_000, "ASRモデル")
        hotwords = config.asr.sherpa.resolved_hotwords_file()
        return SherpaOnnxEngine(
            model_dir,
            num_threads=config.asr.cpu_threads,
            decoding_method=config.asr.sherpa.decoding_method,
            lead_in_ms=config.asr.sherpa.lead_in_ms,
            hotwords_file=hotwords,
            hotwords_score=config.asr.sherpa.hotwords_score,
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
            max_tokens_cap=config.mt.hy_mt2.max_tokens_cap,
        )
    raise NotImplementedError(f"未知の翻訳エンジン: '{config.mt.engine}'")


def get_lan_ip() -> str:
    """ローカルNICの状態・役割から公開IPを選ぶ。曖昧なら明示選択を要求する。"""
    return resolve_ip()


def origin_allowed(origin: str | None, host: str | None, allowed: list[str]) -> bool:
    """WSの Origin を検証する（#25 A-4）。

    - Origin ヘッダが無い接続は許可する。ブラウザは必ず付けるので、無いのは
      `scripts/replay_client.py` のような非ブラウザのツール（LAN内・信頼済み）。
    - 明示的な許可リストに完全一致すれば許可する。
    - それ以外は「Origin のホストが接続先ホストと同一」であることを求める。
      学校LANでは生徒も先生も同じホスト名（IP）で開くので通常は素通りし、
      外部サイトに置かれたページからのWS接続だけが落ちる。
    """
    if origin is None:
        return True
    if origin in allowed:
        return True
    origin_host = urlsplit(origin).hostname
    if origin_host is None:  # "null"（file:// やサンドボックス）
        return False
    # Host ヘッダはポート付き。ホスト名だけ取り出して比べる
    host_only = urlsplit(f"//{host}").hostname if host else None
    return host_only is not None and origin_host == host_only


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
    session.recording = config.recording.default_on  # 既定OFF（F-10）
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
        # 終了操作(end)なしで停止した場合の保険（既に書き出し済みなら no-op）
        with contextlib.suppress(Exception):
            await pipeline.finalize_recording()
        await pipeline.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.session = session
    app.state.pipeline = pipeline
    app.state.config = config
    public_address = PublishedAddress(config.server.advertise_ip)
    app.state.public_address = public_address

    @app.get("/")
    async def student_page() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/teacher")
    async def teacher_page() -> FileResponse:
        return FileResponse(WEB_DIR / "teacher.html")

    @app.get("/connection-help", response_class=HTMLResponse)
    def connection_help() -> HTMLResponse:
        page = (WEB_DIR / "connection-help.html").read_text(encoding="utf-8")
        values: dict[str, object] = {
            "http_port": config.server.http_port, "https_port": config.server.https_port,
            "record_template": record_template(),
        }
        # テンプレートは <!-- remote-steps --> と <!-- unavailable-steps --> の2区間を持ち、片方だけ残す。
        try:
            values["ip"] = public_address.current_ip()
            hidden, status_code = "unavailable-steps", 200
        except ValueError as exc:
            # 別端末用URLは出さず、利用者向け説明とサーバーPC内の確認だけを残す。
            values["error"] = str(exc)
            hidden, status_code = "remote-steps", 503
        page = re.sub(rf"<!-- {hidden} -->.*?<!-- /{hidden} -->\s*", "", page, flags=re.S)
        for name, value in values.items():
            page = page.replace("{{" + name + "}}", escape(str(value)))
        return HTMLResponse(page, status_code=status_code, headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # liveness: プロセスが生きて応答できるか（#25 W-2）。モデルのロード状況とは
        # 独立で、常に 200 を返す。「生きているが未準備」を区別するために
        # readiness は /ready に分けてある
        return JSONResponse({"status": "ok", "ready": pipeline.ready})

    @app.get("/ready")
    async def ready() -> JSONResponse:
        # readiness: モデルの事前ロード完了まで 503（E-13）。start.bat はこれが
        # 200 になってからブラウザを開く
        if not pipeline.ready:
            return JSONResponse({"status": "loading"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/api/config")
    async def api_config() -> dict:
        return {"languages": [lang.model_dump() for lang in config.languages]}

    @app.get("/api/teacher-info")
    def teacher_info(request: Request) -> JSONResponse:
        # 参加コードを晒す口。先生ページは HTTPS(8443) 側で開く運用なので https は許可、
        # 平文HTTP(生徒用)からはループバックのみ許可し部外者のコード取得を抑止（R-10）
        host = request.client.host if request.client else None
        if request.url.scheme != "https" and host not in _TEACHER_INFO_HOSTS:
            return JSONResponse({"detail": "forbidden"}, status_code=403)
        try:
            ip = public_address.current_ip()
        except ValueError as exc:
            logger.warning("公開接続先を利用できません: %s", exc)
            return JSONResponse({"detail": str(exc)}, status_code=503,
                                headers={"Cache-Control": "no-store"})
        join_url = f"http://{ip}:{config.server.http_port}/?code={session.join_code}"
        tls = inspect_certificate(config.server, ip)
        return JSONResponse(
            {
                "code": session.join_code,
                "join_url": join_url,
                "teacher_url": (f"https://{ip}:{config.server.https_port}/teacher"
                                if certificate_ready(tls) else
                                f"http://127.0.0.1:{config.server.http_port}/teacher"),
                "tls": tls,
                "languages": [lang.model_dump() for lang in config.languages],
            }, headers={"Cache-Control": "no-store"}
        )

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    async def handle_join(
        ws: WebSocket, current: Client | None, msg: proto.JoinMessage, client_ip: str
    ) -> Client | None:
        if limiter.is_blocked(client_ip):
            # 総当たり対策（E-09）: コードの正誤にかかわらず一定時間拒否
            await send_now(ws, proto.JoinRejected(reason="rate_limited").model_dump())
            return current
        if not session.check_code(msg.code):
            limiter.record_failure(client_ip)
            await send_now(ws, proto.JoinRejected(reason="bad_code").model_dump())
            return current
        limiter.record_success(client_ip)
        if msg.role == "student" and msg.lang not in config.language_codes:
            await send_now(ws, proto.JoinRejected(reason="bad_lang").model_dump())
            return current
        if (
            msg.role == "student"
            and (current is None or current.role != "student")
            and len(session.students()) >= config.limits.max_students
        ):
            # 生徒数の明示上限（#25 A-4 / whisper-flow W-4）。既に入っている生徒の
            # 入り直しは席を増やさないので数えない
            await send_now(ws, proto.JoinRejected(reason="full").model_dump())
            return current
        if current is not None:
            session.remove_client(current.id)
            await pipeline.drop_client(current.id)
        if msg.role == "teacher":
            old = session.teacher()
            if old is not None:
                # 後勝ち（E-08）: 旧接続を code 4000 で切断（クライアントは再接続しない）
                session.remove_client(old.id)
                await pipeline.drop_client(old.id)
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
        await send_now(
            ws,
            proto.Joined(
                seq_head=session.seq_head,
                history_from=session.history_from,
                languages=config.languages,
                session_state=session.state,
                recording=session.recording,
                speaking=pipeline.speaking,  # 発話の途中で参加した生徒にも出す（#29）
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
        if action == "end":
            # 記録ON中なら排出完了を待って書き出す（F-10。既定OFFなら no-op）
            saved = await pipeline.finalize_recording()
            if saved is not None:
                logger.info("授業記録を保存しました: %s", saved)

    async def handle_teacher_disconnect() -> None:
        """先生切断（E-07）: 配信中なら自動一時停止し、生徒にバナーを出す。
        後勝ち切断（新しい先生が既に接続済み）の場合は何もしない。"""
        if session.teacher() is None and session.state == "live":
            session.state = "paused"
            session.auto_paused = True
            await pipeline.flush_audio()
            await pipeline.broadcast_session_state()

    async def send_now(ws: WebSocket, payload: dict) -> None:
        """join 前後の直接送信（拒否・エラー通知）。送信タイムアウト付き（#25 A-4）。

        字幕の配信は pipeline のクライアント別送信キューを通るが、ここは
        まだクライアントが登録されていない・登録できない経路なので直接送る。
        タイムアウトが無いと、応答しない接続がこの受信ループを永久に止める。
        """
        with contextlib.suppress(Exception):  # TimeoutError もここに含まれる
            await asyncio.wait_for(
                ws.send_json(payload), timeout=config.limits.send_timeout_s
            )

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if not origin_allowed(
            ws.headers.get("origin"), ws.headers.get("host"), config.limits.allowed_origins
        ):
            # 他サイトに置かれたページからのWS接続を断る（#25 A-4）。
            # accept 前に閉じるのでハンドシェイク自体が成立しない
            logger.warning("Origin が不許可のWS接続を拒否: %s", ws.headers.get("origin"))
            await ws.close(code=1008)
            return
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
                    if len(data) > config.limits.max_audio_bytes:
                        # 100msフレーム(3200B)を大きく超えるバイナリは捨てる。
                        # 接続は維持する（先生の操作は効いたままにする）
                        logger.warning("上限超過の音声フレームを破棄: %d bytes", len(data))
                        pipeline.audio_frames_rejected += 1  # 長時間試験の破棄内訳（#32）
                        continue
                    if client is not None and client.role == "teacher":
                        await pipeline.feed_audio(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                # 文字数ではなくバイト数で測る（日本語は1文字3バイト）
                if len(text.encode("utf-8", "ignore")) > config.limits.max_text_bytes:
                    await send_now(
                        ws,
                        proto.ErrorMsg(
                            code="too_large", message="メッセージが大きすぎます"
                        ).model_dump(),
                    )
                    continue
                try:
                    msg = proto.parse_client_message(text)
                except proto.ProtocolError as exc:
                    await send_now(
                        ws, proto.ErrorMsg(code="bad_message", message=str(exc)).model_dump()
                    )
                    continue
                if isinstance(msg, proto.JoinMessage):
                    client = await handle_join(ws, client, msg, client_ip)
                elif client is None:
                    await send_now(
                        ws,
                        proto.ErrorMsg(
                            code="not_joined", message="先に join してください"
                        ).model_dump(),
                    )
                elif isinstance(msg, proto.SetLangMessage):
                    if client.role == "student":
                        if msg.lang in config.language_codes:
                            client.lang = msg.lang
                        else:
                            await send_now(
                                ws,
                                proto.ErrorMsg(
                                    code="bad_lang", message=f"未対応の言語: {msg.lang}"
                                ).model_dump(),
                            )
                elif isinstance(msg, proto.ControlMessage):
                    if client.role == "teacher":
                        await handle_control(msg.action)
                elif isinstance(msg, proto.RecordingMessage):
                    if client.role == "teacher":
                        session.recording = msg.on
                        await pipeline.broadcast_recording()  # 双方の記録中インジケーター（F-10）
        except WebSocketDisconnect:
            pass
        finally:
            if client is not None:
                session.remove_client(client.id)
                await pipeline.drop_client(client.id)  # 送信キューを畳む（#25 A-3）
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
    parser.add_argument("--advertise-ip", help="案内に使う、このPCのIPv4（今回のみ）")
    parser.add_argument("--select-network", action="store_true", help="曖昧・無効な接続先を対話選択")
    add_layer_arguments(parser)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = load_config(args.config, override=args.config_override, data_root=args.data_root)
    config_args = config_cli_args(args)
    try:
        requested_ip = args.advertise_ip or config.server.advertise_ip
        if args.select_network:
            ip = choose_ip(requested_ip, interactive=True)
        else:
            ip = resolve_ip(requested_ip) if requested_ip else get_lan_ip()
        config.server.advertise_ip = ip
        app = create_app(config)
    except (FileNotFoundError, ValueError) as exc:  # モデル欠損/設定不整合（E-13）。生tbを見せない
        print(f"起動できません: {exc}")
        raise SystemExit(1) from exc
    session: Session = app.state.session
    tls = inspect_certificate(config.server, ip)
    https = certificate_ready(tls)
    teacher_line = (
        f"https://{ip}:{config.server.https_port}/teacher（証明書整合性確認済み・別端末での利用は未確認）"
        if https
        else f"http://127.0.0.1:{config.server.http_port}/teacher（このPCで開く。別端末HTTPSは証明書の修復後に再確認）"
    )
    print("=" * 66)
    print("LinguaBridge サーバー起動")
    print(f"  公開IP     : {ip}（変更時は再起動して再選択）")
    print(f"  一時選択時 : 診断・証明書生成にも --advertise-ip {ip} を渡してください。")
    print(f"  参加コード : {session.join_code}")
    print(f"  生徒用URL  : http://{ip}:{config.server.http_port}/?code={session.join_code}")
    print(f"  先生ページ : {teacher_line}")
    print("  接続診断   : 別のターミナルで start.bat --diagnose（読み取り専用）")
    print_certificate_report(tls)
    days = tls["certificate"].get("days_remaining")
    if days is not None and days < 30:
        state = "期限切れ" if days < 0 else f"残り{days}日"
        print(f"  ⚠ 証明書の有効期限が近い/切れています（{state}）。")
    if not https or (days is not None and days < 30):
        print("  サーバーを停止して次を実行（旧証明書・鍵は一組で自動退避）:")
        print(f'    .venv\\Scripts\\python scripts\\make_cert.py {config_args} --advertise-ip {ip} --force')
    print(f'  HTTPS再確認: 再起動後、.venv\\Scripts\\python -m server.diagnostics {config_args} --advertise-ip {ip} --json')
    print("  警告承認・復元手順: docs/certificate-recovery.md")
    print("=" * 66)
    try:
        event_loop.run(_serve(app, config, open_browser=args.open_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
