"""FastAPIエントリ。静的配信・HTTP API・WSエンドポイント。

平文HTTP(生徒用)と自己署名HTTPS(先生用)を同時リッスンする（#16）。
先生ページは HTTPS で開けば別端末でも getUserMedia のセキュアコンテキストを
満たす。証明書が無ければ HTTP 単独で起動し、先生は localhost で開く運用に退避する。
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from functools import partial
from html import escape
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from server import event_loop
from server.connection import Connection
from server.asr.base import ASREngine
from server.asr.fake_engine import FakeASREngine
from server.config import AppConfig, add_layer_arguments, config_cli_args, load_config
from server.launch_commands import launch_commands
from server.inference_process import ProcessInferenceRunner
from server.diagnostics import record_template
from server.certificates import print_certificate_report
from server.model_files import require_model_files
from server.publication import Publication
from server.mt.base import TranslationEngine
from server.mt.fake_engine import FakeTranslationEngine
from server.pipeline import Pipeline
from server.rate_limit import JoinRateLimiter
from server.lesson import Lesson, generate_join_code
from server.roster import Roster
from server.source_access import SourceAccess, socket_ip

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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
            blank_penalty=config.asr.sherpa.blank_penalty,
            refill_gap_s=config.asr.sherpa.refill_gap_s,
        )
    if config.asr.engine == "haas-sherpa":
        from server.asr.haas_sherpa_engine import HaasSherpaEngine

        haas = config.asr.haas_sherpa
        return HaasSherpaEngine(
            config.models.resolve(haas.model),
            num_threads=config.asr.cpu_threads,
            decoding_method=haas.decoding_method,
            hotwords_file=haas.resolved_hotwords_file(),
            hotwords_score=haas.hotwords_score,
        )
    if config.asr.engine == "haas-onnx":
        from server.asr.haas_onnx_engine import HaasOnnxEngine

        onnx = config.asr.haas_onnx
        return HaasOnnxEngine(
            config.models.resolve(onnx.model),
            quantization=onnx.quantization,
            num_threads=onnx.cpu_threads if onnx.cpu_threads is not None else config.asr.cpu_threads,
        )
    raise NotImplementedError(f"未知のASRエンジン: '{config.asr.engine}'")


def _require_language_coverage(config: AppConfig, supported: set[str]) -> None:
    missing = set(config.language_codes) - supported
    if missing:
        # 対応外言語を設定したまま起動しない（E-14 は join 時にも検証される）
        raise ValueError(
            f"翻訳モデルが未対応の言語が languages にある: {sorted(missing)}"
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
        require_model_files(model_dir, 100_000_000, "翻訳モデル",
                            translation_models_dir=config.models.resolved_dir)
        require_model_files(tokenizer_dir, 100_000, "翻訳モデル",
                            translation_models_dir=config.models.resolved_dir)
        return NllbEngine(model_dir, tokenizer_dir, beam_size=config.mt.nllb.beam_size)
    if config.mt.engine == "hy-mt2":
        from server.mt.hymt_engine import HYMT_LANG_LABELS, HyMt2Engine

        _require_language_coverage(config, set(HYMT_LANG_LABELS))
        gguf_path = config.models.resolve(config.mt.hy_mt2.gguf_path)
        require_model_files(gguf_path, 500_000_000, "翻訳モデル",
                            translation_models_dir=config.models.resolved_dir)
        return HyMt2Engine(
            gguf_path,
            threads=config.mt.hy_mt2.threads,
            temperature=config.mt.hy_mt2.temperature,
            max_tokens_cap=config.mt.hy_mt2.max_tokens_cap,
        )
    raise ValueError("未知の翻訳エンジンです。設定を確認してください")


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
    publication: Publication,
    *,
    asr_engine: ASREngine | None = None,
    mt_engine: TranslationEngine | None = None,
    join_code: str | None = None,
    teacher_code: str | None = None,
    join_limiter: JoinRateLimiter | None = None,
    diagnostics_command: str | None = None,
    recording_wait_s: float = 30,
) -> FastAPI:
    roster = Roster(config.limits)
    asr_runner = mt_runner = None
    if asr_engine is None:
        asr_factory = partial(build_asr_engine, config.model_copy(deep=True))
        asr_engine = asr_factory()
        asr_runner = ProcessInferenceRunner(
            asr_engine, asr_factory, startup_timeout_s=config.limits.inference_startup_timeout_s, name="asr")
    if mt_engine is None:
        mt_factory = partial(build_mt_engine, config.model_copy(deep=True))
        mt_engine = mt_factory()
        mt_runner = ProcessInferenceRunner(
            mt_engine, mt_factory, startup_timeout_s=config.limits.inference_startup_timeout_s, name="mt")
    pipeline = Pipeline(
        roster,
        config,
        asr_engine,
        mt_engine,
        asr_runner=asr_runner,
        mt_runner=mt_runner,
    )

    lesson = Lesson(pipeline, config, join_code=join_code or generate_join_code(),
                    teacher_code=teacher_code,
                    limiter=join_limiter, recording_wait_s=recording_wait_s)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await pipeline.start()
        try:
            yield
        finally:
            try:
                await lesson.shutdown()
            finally:
                await pipeline.stop()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(SourceAccess)
    app.state.lesson = lesson
    app.state.pipeline = pipeline
    app.state.config = config
    app.state.publication = publication

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
            "http_port": publication.http_port, "https_port": publication.https_port,
            "record_template": record_template(),
        }
        if diagnostics_command is None:
            page = re.sub(
                r"<!-- diagnostics-command -->.*?<!-- /diagnostics-command -->\s*",
                "", page, flags=re.S,
            )
        else:
            values["diagnostics_command"] = diagnostics_command
        # テンプレートは <!-- remote-steps --> と <!-- unavailable-steps --> の2区間を持ち、片方だけ残す。
        try:
            values["ip"] = publication.current().ip
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
        try:
            publication.current()
        except ValueError as exc:
            logger.warning("公開接続先を利用できません: %s", exc)
            return JSONResponse({"detail": str(exc)}, status_code=503,
                                headers={"Cache-Control": "no-store"})
        info = {
            "code": lesson.join_code,
            "join_url": publication.join_url(lesson.join_code),
            "teacher_url": publication.teacher_url,
            "tls": publication.tls,
            "languages": [lang.model_dump() for lang in config.languages],
        }
        peer = socket_ip(request.scope.get("client"))
        if peer is not None and (peer.is_loopback or peer == socket_ip(request.scope.get("server"))):
            info["teacher_code"] = lesson.teacher_code
        return JSONResponse(info, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

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
        connection = Connection(
            ws, ws.client.host if ws.client else "unknown", lesson, config.limits
        )
        async with connection.handling():
            try:
                while True:
                    message = await ws.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    frame = message.get("bytes")
                    if frame is None:
                        frame = message.get("text")
                    if frame is not None:
                        await connection.receive(frame)
            except WebSocketDisconnect:
                pass

    return app


async def _open_teacher_page_when_ready(pipeline: Pipeline, url: str) -> None:
    """モデルのロード完了（ready）を待ってから既定ブラウザで先生ページを開く。"""
    import webbrowser

    for _ in range(600):  # 最大60秒待つ
        if pipeline.ready:
            break
        await asyncio.sleep(0.1)
    webbrowser.open(url)


async def _serve(app: FastAPI, *, open_browser: bool) -> None:
    from server.serving import ListenerSpec, TLSFiles, start_listeners

    publication: Publication = app.state.publication
    specs = [ListenerSpec("0.0.0.0", publication.http_port, lifespan=True)]
    if publication.https_available:
        specs.append(ListenerSpec("0.0.0.0", publication.https_port,
                                  tls=TLSFiles(publication.cert_path, publication.key_path)))
    async with start_listeners(
        app, specs,
        before_shutdown=app.state.lesson.shutdown,
        on_ready=(lambda: _open_teacher_page_when_ready(app.state.pipeline, publication.teacher_url))
        if open_browser else None,
    ) as running:
        await running.wait()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Live Translation サーバー")
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
    commands = launch_commands()  # 配布モードでは同梱の python.exe を案内する（#51）
    try:
        publication = Publication(config.server, requested_ip=args.advertise_ip,
                                  interactive=args.select_network)
        ip = publication.ip
        diagnostics_command = f"{commands.diagnostics} {config_args} --advertise-ip {ip} --json"
        app = create_app(config, publication, diagnostics_command=diagnostics_command)
    except (FileNotFoundError, ValueError) as exc:  # モデル欠損/設定不整合（E-13）。生tbを見せない
        print(f"起動できません: {exc}")
        raise SystemExit(1) from exc
    lesson: Lesson = app.state.lesson
    tls = publication.tls
    https = publication.https_available
    teacher_line = (
        f"{publication.teacher_url}（証明書整合性確認済み・別端末での利用は未確認）"
        if https
        else f"{publication.teacher_url}（このPCで開く。別端末HTTPSは証明書の修復後に再確認）"
    )
    print("=" * 66)
    print("Live Translation サーバー起動")
    print(f"  公開IP     : {ip}（変更時は再起動して再選択）")
    print(f"  一時選択時 : 診断・証明書生成にも --advertise-ip {ip} を渡してください。")
    print(f"  参加コード : {lesson.join_code}")
    print(f"  先生コード : {lesson.teacher_code}")
    print(f"  生徒用URL  : {publication.join_url(lesson.join_code)}")
    print(f"  先生ページ : {teacher_line}")
    print("  接続診断   : 別のターミナルで start.bat --diagnose（読み取り専用）")
    print_certificate_report(tls)
    days = tls["certificate"].get("days_remaining")
    if days is not None and days < 30:
        state = "期限切れ" if days < 0 else f"残り{days}日"
        print(f"  ⚠ 証明書の有効期限が近い/切れています（{state}）。")
    if not https or (days is not None and days < 30):
        print("  サーバーを停止して次を実行（旧証明書・鍵は一組で自動退避）:")
        print(f"    {commands.make_cert} {config_args} --advertise-ip {ip} --force")
    print(f"  HTTPS再確認: 再起動後、{diagnostics_command}")
    print("  警告承認・復元手順: docs/certificate-recovery.md")
    print("=" * 66)
    try:
        event_loop.run(_serve(app, open_browser=args.open_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
