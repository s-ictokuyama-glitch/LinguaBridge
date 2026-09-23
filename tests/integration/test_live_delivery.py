"""#41: 本番の二重待受で、ページ取得・WS handshake・参加・配信を段階ごとに確かめる。

推論だけフェイクで、Uvicorn・TLS・実TCP・本番のイベントループは実物（#42 の Launcher を共用）。
ループバックでの現地外の証拠であり、実マイク・対象端末・社内Wi-Fiの確認は #43 F5 で行う。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from tests.conftest import JOIN_CODE
from tests.helpers import utterance_bytes
from tests.integration.test_connection_reset import (  # noqa: F401  (launcher は fixture)
    launcher,
    receive,
    run_in_worker,
    wait_until,
)
from tests.integration.test_ws_boundary import PHRASE_1000, PHRASE_2000, PHRASE_3000

UTTERANCES = [(1000, PHRASE_1000), (2000, PHRASE_2000), (3000, PHRASE_3000)]


def urls(scheme: str, port: int) -> tuple[str, str]:
    """ページのOrigin（＝ページURLの起点）と、ページが開く同一ホスト・ポートのWS URL。"""
    page = f"{scheme}://127.0.0.1:{port}"
    return page, page.replace("https:", "wss:").replace("http:", "ws:") + "/ws"


@pytest.mark.timeout(60)
def test_pages_handshake_join_and_three_captions_are_separately_confirmed(
    launcher, record_property, caplog
):
    stages: dict[str, str] = {}

    async def scenario():
        opened = []
        try:
            async with launcher.serving() as (http_port, https_port):
                tls = launcher.tls()
                student_page, student_ws = urls("http", http_port)
                teacher_page, teacher_ws = urls("https", https_port)

                async def open_ws(uri, origin):
                    options = {"ssl": tls} if uri.startswith("wss:") else {}
                    ws = await connect(uri, origin=origin, proxy=None, open_timeout=3,
                                       close_timeout=1, **options)
                    opened.append(ws)
                    return ws

                # 1. ページ取得（生徒はHTTP、先生はHTTPS。証明書は検証する）。
                # 先生情報API（参加コード・案内URL）はループバックを公開IPにしないので、
                # 実Wi-Fi相当の接続先で test_network_advertising.py が確かめる
                async with httpx.AsyncClient(verify=tls, trust_env=False, timeout=3) as http:
                    student_html = await http.get(f"{student_page}/")
                    teacher_html = await http.get(f"{teacher_page}/teacher")
                assert student_html.status_code == 200 and "student.js" in student_html.text
                assert teacher_html.status_code == 200 and "teacher.js" in teacher_html.text
                stages["page"] = "ok"

                # 2. WS handshake（ページと同じホスト・ポート、ページ自身のOrigin）
                teacher = await open_ws(teacher_ws, teacher_page)
                student_en = await open_ws(student_ws, student_page)
                student_zh = await open_ws(student_ws, student_page)
                stages["handshake"] = "ok"

                # 3. 参加
                await teacher.send(json.dumps({"type": "join", "role": "teacher",
                                               "code": JOIN_CODE}))
                await receive(teacher, "joined")
                for ws, lang in ((student_en, "en"), (student_zh, "zh")):
                    await ws.send(json.dumps({"type": "join", "role": "student",
                                              "code": JOIN_CODE, "lang": lang}))
                    await receive(ws, "joined", session_state="idle")
                await wait_until(lambda: len(launcher.app.state.session.students()) == 2,
                                 "two students joined")
                stages["join"] = "ok"

                # 4. 配信: 3発話それぞれが、各生徒の選択言語の字幕として届く
                await teacher.send(json.dumps({"type": "control", "action": "start"}))
                for ws in (student_en, student_zh):
                    await receive(ws, "session", state="live")
                for seq, (key, ja) in enumerate(UTTERANCES, start=1):
                    for chunk in utterance_bytes(key):
                        await teacher.send(chunk)
                    final = await receive(teacher, "asr_final", seq=seq)
                    assert final["ja"] == ja
                    for ws, lang in ((student_en, "en"), (student_zh, "zh")):
                        caption = await receive(ws, "caption", seq=seq)
                        assert caption["lang"] == lang, caption  # 他言語の字幕は届かない
                        assert caption["ja"] == ja
                        assert caption["text"] == f"[{lang}] {ja}"
                stages["delivery"] = "ok: 3 captions per selected language"

                await teacher.send(json.dumps({"type": "control", "action": "end"}))
                for ws in (student_en, student_zh):
                    # 終了通知までに余分な字幕（他言語・重複）が届いていない
                    async with asyncio.timeout(5):
                        while (message := json.loads(await ws.recv()))["type"] != "session":
                            assert message["type"] != "caption", message
                    assert message["state"] == "ended"
                for ws in opened:
                    await ws.close()
                await wait_until(lambda: not launcher.app.state.session.clients,
                                 "all clients released")
        finally:
            for ws in opened:
                await ws.close()

    run_in_worker(scenario, timeout=50)
    record_property("stages", json.dumps(stages, ensure_ascii=False))
    assert list(stages) == ["page", "handshake", "join", "delivery"]
    assert not launcher.exceptions
    assert not [r for r in caplog.records if r.levelname in ("ERROR", "CRITICAL")]


# 許可: ページ自身のOrigin、Originなしの非ブラウザ（replay_client 等）、
# 同じホストの別ポートのページ。Originはポートではなくホストで照合する既存方針（#25 A-4）。
# 接続先を同一ポートにするのはページ側（tests/unit/test_page_ws_url.py）。
ALLOWED = ["{page}", None, "{other_page}"]
# 拒否: 外部サイト、LAN内の別ホスト、file:// やサンドボックス（"null"）、
# ホスト名の前方一致を狙ったもの。
REJECTED = ["https://unrelated.invalid", "http://192.168.5.99:{port}", "null",
            "http://127.0.0.1.unrelated.invalid:{port}"]


@pytest.mark.timeout(60)
def test_origin_rule_allows_and_rejects_on_both_production_listeners(
    launcher, record_property, caplog
):
    results = []

    def origin_warnings():
        return [r for r in caplog.records if "Origin が不許可" in r.getMessage()]

    async def scenario():
        async with launcher.serving() as ports:
            tls = launcher.tls()
            for scheme, port, other in (("http", ports[0], ports[1]),
                                        ("https", ports[1], ports[0])):
                page, uri = urls(scheme, port)
                options = {"ssl": tls} if scheme == "https" else {}
                other_page = urls("https" if scheme == "http" else "http", other)[0]
                for template in ALLOWED:
                    origin = template and template.format(page=page, other_page=other_page)
                    async with connect(uri, origin=origin, proxy=None, open_timeout=3,
                                       close_timeout=1, **options) as ws:
                        await ws.send(json.dumps({"type": "join", "role": "student",
                                                  "code": JOIN_CODE, "lang": "en"}))
                        await receive(ws, "joined")
                    results.append((uri, origin, "accepted"))
                    await wait_until(lambda: not launcher.app.state.session.clients,
                                     "allowed client released")
                for template in REJECTED:
                    origin = template.format(port=port)
                    warned = len(origin_warnings())
                    with pytest.raises(InvalidStatus) as rejected:
                        async with connect(uri, origin=origin, proxy=None, open_timeout=3,
                                           **options):
                            pass
                    assert rejected.value.response.status_code == 403
                    results.append((uri, origin, "rejected 403"))
                    await wait_until(lambda: len(origin_warnings()) == warned + 1,
                                     "one warning per rejected origin")

    run_in_worker(scenario, timeout=50)
    record_property("origin_results", json.dumps(results, ensure_ascii=False))
    assert len(results) == 2 * (len(ALLOWED) + len(REJECTED))
    assert len(origin_warnings()) == 2 * len(REJECTED)
    assert not launcher.exceptions
