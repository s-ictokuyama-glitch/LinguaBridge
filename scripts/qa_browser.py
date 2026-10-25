"""Exercise the shipped HTML/CSS/JS in headless Edge, with controlled WS/media.

No live microphone or classroom data is accessed. Network requests are fulfilled
from the worktree. This proves browser behavior, not LAN, real audio, or ASR quality.
Run with PYTHONPATH=.qa-tools after installing Playwright there.
"""
from __future__ import annotations

from datetime import datetime
import argparse
import ctypes
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import traceback


class _WindowsFileBasicInfo(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_longlong),
        ("LastAccessTime", ctypes.c_longlong),
        ("LastWriteTime", ctypes.c_longlong),
        ("ChangeTime", ctypes.c_longlong),
        ("FileAttributes", ctypes.c_ulong),
    ]


def _change_time_ns(path: Path) -> int:
    if os.name != "nt":
        return path.lstat().st_ctime_ns
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_ulong,
    ]
    get_information.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    value = os.path.abspath(path)
    if not value.startswith("\\\\?\\"):
        value = (
            "\\\\?\\UNC\\" + value[2:]
            if value.startswith("\\\\")
            else "\\\\?\\" + value
        )
    handle = create_file(
        value,
        0x0080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    if handle in {None, ctypes.c_void_p(-1).value}:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        information = _WindowsFileBasicInfo()
        if not get_information(
            handle,
            0,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(information.ChangeTime) * 100
    finally:
        close_handle(handle)


def _early_self_state(
    path: Path, *, root: Path | None = None,
) -> tuple[str, tuple[int, int, int, int, int, int]]:
    """Bind this runner before importing any repository-local module."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    ancestor_identities: list[tuple[Path, tuple[int, int]]] = []
    if root is not None:
        absolute_root = Path(os.path.abspath(root))
        absolute_path = Path(os.path.abspath(path))
        try:
            relative = absolute_path.relative_to(absolute_root)
        except ValueError as exc:
            raise RuntimeError("browser runner source is outside repository") from exc
        current = absolute_root
        for part in relative.parts[:-1]:
            identity = current.lstat()
            if (
                current.is_symlink()
                or bool(is_junction(current))
                or not stat.S_ISDIR(identity.st_mode)
            ):
                raise RuntimeError(
                    f"browser runner source has unsafe ancestor: {current}"
                )
            ancestor_identities.append(
                (current, (identity.st_dev, identity.st_ino))
            )
            current /= part
        identity = current.lstat()
        if (
            current.is_symlink()
            or bool(is_junction(current))
            or not stat.S_ISDIR(identity.st_mode)
        ):
            raise RuntimeError(
                f"browser runner source has unsafe ancestor: {current}"
            )
        ancestor_identities.append(
            (current, (identity.st_dev, identity.st_ino))
        )
    if path.is_symlink() or bool(is_junction(path)):
        raise RuntimeError(f"browser runner source is a link or junction: {path}")
    before, before_revision = _early_revision_snapshot(path)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"browser runner source is not regular: {path}")
    content = path.read_bytes()
    _after, after_revision = _early_revision_snapshot(path)
    if before_revision != after_revision:
        raise RuntimeError("browser runner source changed during import")
    for ancestor, expected in ancestor_identities:
        identity = ancestor.lstat()
        if (
            ancestor.is_symlink()
            or bool(is_junction(ancestor))
            or not stat.S_ISDIR(identity.st_mode)
            or (identity.st_dev, identity.st_ino) != expected
        ):
            raise RuntimeError("browser runner source ancestor changed during import")
    return hashlib.sha256(content).hexdigest(), after_revision


def _early_revision_snapshot(
    path: Path,
) -> tuple[os.stat_result, tuple[int, int, int, int, int, int]]:
    """Capture stat and ChangeTime together, rejecting an unstable pair."""

    def revision(
        value: os.stat_result, change: int
    ) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            change,
        )

    first = path.lstat()
    first_revision = revision(first, _change_time_ns(path))
    second = path.lstat()
    second_revision = revision(second, _change_time_ns(path))
    if first_revision != second_revision:
        raise RuntimeError("browser runner source revision was unstable")
    return second, second_revision


_SELF_PATH = Path(os.path.abspath(__file__))
ROOT = _SELF_PATH.parent.parent
_SELF_IMPORT_STATE = _early_self_state(_SELF_PATH, root=ROOT)
_EARLY_IMPORT_CODE_FILES = (
    _SELF_PATH,
    ROOT / "scripts/qa_evidence_manifest.py",
)
_EARLY_IMPORT_CODE_STATE = {
    path: (
        _SELF_IMPORT_STATE
        if path == _SELF_PATH
        else _early_self_state(path, root=ROOT)
    )
    for path in _EARLY_IMPORT_CODE_FILES
}
# Never consult a worktree ``.pyc`` whose timestamp/size can be replayed after
# an in-place edit.  The script itself is executed from source; all local
# imports below compile into this new empty cache tree.
_IMPORT_PYCACHE = tempfile.TemporaryDirectory(
    prefix="linguabridge-browser-import-pycache-"
)
sys.pycache_prefix = _IMPORT_PYCACHE.name
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".qa-tools"))
from playwright.sync_api import sync_playwright, expect
from scripts.qa_evidence_manifest import (
    EvidenceManifestError,
    commit_browser_result,
    publish_browser_evidence,
    snapshot_supplied_source_hashes,
    snapshot_source_hashes,
    validate_output_parent,
)

_IMPORT_ROOT = ROOT
_IMPORT_CODE_FILES = _EARLY_IMPORT_CODE_FILES
_IMPORT_CODE_SNAPSHOT = snapshot_source_hashes(
    _IMPORT_ROOT, _IMPORT_CODE_FILES
)
_IMPORT_REVISIONS = dict(_IMPORT_CODE_SNAPSHOT.revisions)
for _path, (_digest, _revision) in _EARLY_IMPORT_CODE_STATE.items():
    _relative = _path.relative_to(_IMPORT_ROOT).as_posix()
    if (
        _IMPORT_CODE_SNAPSHOT.get(_relative) != _digest
        or _IMPORT_REVISIONS.get(_relative) != _revision
    ):
        raise RuntimeError(
            "browser audit source changed while local modules imported"
        )


def _import_code_is_current() -> bool:
    try:
        prefix = sys.pycache_prefix
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        if not isinstance(prefix, str) or not prefix or not sys.dont_write_bytecode:
            return False
        cache_root = Path(prefix)
        if (
            cache_root.is_symlink()
            or bool(is_junction(cache_root))
            or not cache_root.is_dir()
            or any(cache_root.iterdir())
        ):
            return False
        current = snapshot_source_hashes(_IMPORT_ROOT, _IMPORT_CODE_FILES)
    except (EvidenceManifestError, OSError, ValueError):
        return False
    return (
        current == _IMPORT_CODE_SNAPSHOT
        and current.revisions == _IMPORT_CODE_SNAPSHOT.revisions
    )

MOCK = r"""
window.qa = {sockets: [], sent: [], timers: [], code: '4831', current: 'idle',
 recording: false, failConnect: false, failMic: false, failDevice: false,
 micPermissionGranted: false, deferNextMic: false, pendingMic: null,
 streams: [], sources: [], micCalls: [], devices: [{kind:'audioinput', deviceId:'default', label:'Default'},
 {kind:'audioinput',deviceId:'mic-a',label:'Pin microphone'}, {kind:'audioinput',deviceId:'mic-b',label:'USB microphone'}]};
window.setTimeout = (fn, delay) => { qa.timers.push({fn,delay}); return qa.timers.length; };
class FakeSocket extends EventTarget {
 static OPEN = 1;
 constructor(url) { super(); this.url=url; this.readyState=0; qa.sockets.push(this);
   queueMicrotask(() => { if (qa.failConnect) { this.close(); return; }
     this.readyState=1; this.dispatchEvent(new Event('open')); }); }
 send(value) { qa.sent.push(value instanceof ArrayBuffer ? {audioBytes:value.byteLength} : JSON.parse(value));
   if (value instanceof ArrayBuffer) return;
   const msg=JSON.parse(value);
   if (msg.type==='join') this.emit(msg.code===qa.code ? {type:'joined',seq_head:0,history_from:1,
     languages:[{code:'en',label:'English'},{code:'zh',label:'中文（简体）'}],session_state:qa.current,recording:qa.recording,speaking:false}
     : {type:'join_rejected',reason:'bad_code'});
   if(msg.type==='control') { qa.current={start:'live',pause:'paused',end:'ended'}[msg.action]; this.emit({type:'session',state:qa.current}); }
   if(msg.type==='recording') { qa.recording=msg.on; this.emit({type:'recording',on:msg.on}); }
 }
 emit(msg) { this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(msg)})); }
 close(code=1006) { this.readyState=3; this.dispatchEvent(new CloseEvent('close',{code})); }
}
window.WebSocket=FakeSocket;
window.qaEmit=msg=>qa.sockets.at(-1).emit(msg);
const hasMicPermission=()=>{
  try{return qa.micPermissionGranted || sessionStorage.getItem('qa_mic_permission')==='1';}
  catch{return qa.micPermissionGranted;}
};
const rememberMicPermission=()=>{
  qa.micPermissionGranted=true;
  try{sessionStorage.setItem('qa_mic_permission','1');}catch{}
};
const media={
 addEventListener(){},
 enumerateDevices:async()=>qa.devices.map(d=>({...d,label:hasMicPermission() ? d.label : ''})),
 getUserMedia:async constraints=>{ qa.micCalls.push(constraints);
   const requested=constraints.audio.deviceId?.exact || '';
   const makeStream=()=>{
     const track={deviceId:requested,stopped:false,stop(){this.stopped=true;}};
     const stream={deviceId:requested,track,getTracks:()=>[track]};
     rememberMicPermission(); qa.streams.push(stream); return stream;
   };
   if(qa.failMic) throw new DOMException('denied','NotAllowedError');
   if(qa.deferNextMic){
     qa.deferNextMic=false;
     return await new Promise((resolve,reject)=>{
       qa.pendingMic={requested,
         resolve(){qa.pendingMic=null; resolve(makeStream());},
         reject(name='NotReadableError'){qa.pendingMic=null; reject(new DOMException('device unavailable',name));}};
     });
   }
   if(qa.failDevice && requested) throw new DOMException('device unavailable','NotReadableError');
   return makeStream();
 }
};
Object.defineProperty(navigator,'mediaDevices',{value:media,configurable:true});
window.AudioContext=class {
 constructor(){ this.destination={}; this.audioWorklet={addModule:async()=>{}}; }
 async resume(){} createMediaStreamSource(stream){
   const source={connected:false,connectedTo:null,stream,
     connect(target){this.connected=true;this.connectedTo=target;},
     disconnect(){this.connected=false;this.connectedTo=null;}};
   qa.sources.push(source); return source;
 }
 createGain(){return {gain:{value:1},connect(){}};}
};
window.AudioWorkletNode=class { constructor(){this.port={};} connect(){} };
"""

CASES = []
DEFAULT_SCOPE = "実Edge DOM。WS・マイクは決定的代替。LAN/実音声/モデル品質は未検証"


class EvidenceAssertionError(AssertionError):
    """An assertion whose completed observations should survive a failed case."""

    def __init__(self, message: str, observation: dict):
        super().__init__(message)
        self.observation = observation


def case(
    story: str,
    name: str,
    page_type: str = "student",
    *,
    complete: bool = True,
    context_options: dict | None = None,
    scope: str | None = None,
):
    def register(fn):
        CASES.append(
            (story, name, page_type, complete, fn, dict(context_options or {}), scope)
        )
        return fn
    return register


def join(page, lang="en"):
    page.locator(f'#lang-options button[data-code="{lang}"]').click()
    page.locator("#code-input").fill("4831")
    page.locator("#join-btn").click()
    expect(page.locator("#caption-screen")).to_be_visible()


def emit(page, msg):
    page.evaluate("msg => qaEmit(msg)", msg)


def caption(page, seq=1, delay=0, text="The sunlight is necessary."):
    emit(page, {"type":"caption", "seq":seq, "text":text, "ja":"光合成には日光が必要です。", "lang":"en", "delay_ms":delay})


def open_settings(page):
    page.locator("#settings-btn").click()
    expect(page.locator("#settings-sheet")).to_be_visible()


def close_settings(page):
    page.locator("#settings-close").click()
    expect(page.locator("#settings-sheet")).to_be_hidden()


def select_language(page, lang):
    open_settings(page)
    page.locator("#lang-select").select_option(lang)
    close_settings(page)


def decode_qr_with_local_opencv(png: bytes) -> dict[str, object]:
    """既存の別Python環境にあるOpenCVで、実Edgeの画像を独立復号する。"""

    decoder = r"""
import json
import sys

import cv2
import numpy as np

image = cv2.imdecode(np.frombuffer(sys.stdin.buffer.read(), dtype=np.uint8), cv2.IMREAD_COLOR)
if image is None:
    raise SystemExit("PNGを読み取れませんでした")
# 標準のQRCodeDetectorは、正しいQRでも模様によって位置検出に失敗する（#96: 参加URLの約1.5%）。
# 方式の違うAruco版でも試し、どちらかが読めれば復号できたとみなす
for name, detector in (("QRCodeDetector", cv2.QRCodeDetector()), ("QRCodeDetectorAruco", cv2.QRCodeDetectorAruco())):
    value, points, _ = detector.detectAndDecode(image)
    if value:
        break
print(json.dumps({
    "decoder": f"OpenCV {cv2.__version__} {name}",
    "detected": points is not None,
    "value": value,
}))
"""
    failures = []
    for command in ([sys.executable], ["py", "-3"]):
        try:
            result = subprocess.run(
                [*command, "-c", decoder],
                input=png,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{command}: {exc}")
            continue
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            failures.append(f"{command}: exit {result.returncode}: {detail}")
            continue
        return json.loads(result.stdout.decode("utf-8"))
    raise AssertionError("利用可能なローカルOpenCV QR decoderがありません: " + " | ".join(failures))


@case("JOIN-01", "join_hides_entry_screen")
def test_join(page):
    expect(page.locator("#join-btn")).to_be_disabled()
    page.locator("#code-input").fill("4831")
    expect(page.locator("#join-btn")).to_be_disabled()
    join(page)
    expect(page.locator("#join-screen")).to_be_hidden()


@case("JOIN-02", "qr_query_prefills_code")
def test_query(page):
    expect(page.locator("#code-input")).to_have_value("4831")
    page.locator('#lang-options button[data-code="zh"]').click()
    expect(page.locator("#join-btn")).to_be_enabled()
    page.locator("#join-btn").click()
    expect(page.locator("#caption-screen")).to_be_visible()
    assert page.evaluate("state.joined")
    assert page.evaluate("state.lang") == "zh"


@case("JOIN-03", "wrong_code_then_retry")
def test_bad_code(page):
    page.locator('#lang-options button[data-code="en"]').click()
    page.locator("#code-input").fill("9999")
    page.locator("#join-btn").click()
    expect(page.locator("#join-error")).to_have_text("Wrong join code")
    page.locator("#code-input").fill("4831")
    page.locator("#join-btn").click()
    expect(page.locator("#caption-screen")).to_be_visible()
    expect(page.locator("#join-error")).to_be_hidden()


@case("STU-01", "cards_order_dedup_plain_text")
def test_cards(page):
    join(page)
    for seq in [3, 1, 2, 2]:
        caption(page, seq, text="<img src=x onerror=alert(1)>")
    assert page.locator("#cards .card").evaluate_all("xs=>xs.map(x=>+x.dataset.seq)") == [1,2,3]
    assert page.locator("#cards img").count() == 0
    assert page.locator("#cards .ja").count() == 3
    assert page.locator("#cards .ja").all_text_contents() == ["光合成には日光が必要です。"] * 3
    assert page.locator("#cards .text").all_text_contents() == ["<img src=x onerror=alert(1)>"] * 3
    assert all(page.locator("#cards .time").all_text_contents())


@case("STU-02", "follow_pause_and_latest")
def test_follow(page):
    join(page)
    for seq in range(1,31):
        caption(page, seq, text="Long caption " * 25)

    initial = page.locator("#cards").evaluate(
        "e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})"
    )
    assert initial["height"] > initial["viewport"], "caption list did not overflow"
    assert initial["height"] - initial["top"] - initial["viewport"] <= 1
    assert page.evaluate("state.following") is True
    expect(page.locator("#latest-btn")).to_be_hidden()

    caption(page, 31)
    followed = page.locator("#cards").evaluate(
        "e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})"
    )
    assert followed["height"] > initial["height"]
    assert followed["top"] > initial["top"]
    assert followed["height"] - followed["top"] - followed["viewport"] <= 1

    page.locator("#cards").evaluate(
        "e=>{e.scrollTop=Math.max(0,e.scrollHeight-e.clientHeight-100);"
        "e.dispatchEvent(new Event('scroll'))}"
    )
    assert page.evaluate("state.following") is False
    expect(page.locator("#latest-btn")).to_be_visible()
    before = page.locator("#cards").evaluate(
        "e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})"
    )
    assert before["height"] - before["top"] - before["viewport"] >= 40

    caption(page, 32)
    paused = page.locator("#cards").evaluate(
        "e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})"
    )
    assert paused["height"] > before["height"]
    assert paused["top"] == before["top"]
    assert page.evaluate("state.following") is False
    expect(page.locator("#latest-btn")).to_be_visible()

    page.locator("#latest-btn").click()
    assert page.evaluate("state.following") is True
    expect(page.locator("#latest-btn")).to_be_hidden()
    restored = page.locator("#cards").evaluate(
        "e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})"
    )
    assert restored["top"] > paused["top"]
    assert restored["height"] - restored["top"] - restored["viewport"] <= 1

    caption(page, 33)
    resumed = page.locator("#cards").evaluate(
        "e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})"
    )
    assert resumed["height"] > restored["height"]
    assert resumed["top"] > restored["top"]
    assert resumed["height"] - resumed["top"] - resumed["viewport"] <= 1
    assert page.evaluate("state.following") is True
    expect(page.locator("#latest-btn")).to_be_hidden()


@case("STU-03", "original_toggle_and_persistence")
def test_original(page):
    join(page); caption(page)
    expect(page.locator("#cards .ja")).to_be_visible()
    open_settings(page)
    page.locator("#ja-toggle").uncheck()
    expect(page.locator("#cards .ja")).to_be_hidden()
    expect(page.locator("#cards .text")).to_be_visible()
    page.locator("#ja-toggle").check()
    expect(page.locator("#cards .ja")).to_be_visible()
    page.locator("#ja-toggle").uncheck()
    close_settings(page)
    caption(page,2)
    expect(page.locator("#cards .ja").last).to_be_hidden()
    page.reload(); join(page); caption(page)
    expect(page.locator("#ja-toggle")).not_to_be_checked()
    expect(page.locator("#cards .ja")).to_be_hidden()


@case("STU-04", "font_sizes_and_persistence")
def test_font(page):
    join(page); caption(page)
    sizes=[]
    originals=[]
    open_settings(page)
    for size in ["s","m","l"]:
        page.locator(f'#size-buttons button[data-size="{size}"]').click()
        sizes.append(page.locator("#cards .text").evaluate("e=>parseFloat(getComputedStyle(e).fontSize)"))
        originals.append(page.locator("#cards .ja").evaluate("e=>parseFloat(getComputedStyle(e).fontSize)"))
        expect(page.locator(f'#size-buttons button[data-size="{size}"]')).to_have_class("selected")
        assert page.locator("#size-buttons button.selected").count() == 1
    close_settings(page)
    assert sizes[0] < sizes[1] < sizes[2]
    assert originals[0] < originals[1] < originals[2]
    page.reload(); join(page)
    expect(page.locator("#caption-screen")).to_have_class("size-l")
    expect(page.locator('#size-buttons button[data-size="l"]')).to_have_class("selected")


@case("STU-05", "storage_denied_student")
def test_storage(page):
    page.add_init_script("Storage.prototype.getItem=()=>{throw Error('blocked')};Storage.prototype.setItem=()=>{throw Error('blocked')}")
    page.reload(); join(page); caption(page)
    open_settings(page)
    page.locator('#size-buttons button[data-size="l"]').click()
    page.locator("#ja-toggle").uncheck()
    close_settings(page)
    expect(page.locator("#cards .ja")).to_be_hidden()


@case("STU-05", "storage_denied_teacher", "teacher")
def test_storage_teacher(page):
    page.add_init_script("Storage.prototype.getItem=()=>{throw Error('blocked')};Storage.prototype.setItem=()=>{throw Error('blocked')}")
    page.reload()
    expect(page.locator("#start-btn")).to_be_enabled()
    page.locator("#start-btn").click()
    page.locator("#mic-select").select_option("mic-a")
    expect(page.locator("#mic-status")).to_have_text("マイクを切り替えました。")
    expect(page.locator("#session-state")).to_have_text("配信中")


@case("STU-06", "switch_language_and_rejected_rollback", complete=False)
def test_language(page):
    join(page); caption(page)
    select_language(page, "zh")
    assert page.evaluate("qa.sent.at(-1)") == {"type":"set_lang","lang":"zh"}
    expect(page.locator("html")).to_have_attribute("lang","zh")
    assert page.locator("#cards .card").count()==1
    emit(page,{"type":"error","code":"bad_lang"})
    expect(page.locator("#lang-select")).to_have_value("en")


@case("STU-07", "recording_indicator_localized")
def test_i18n(page):
    join(page)
    expect(page.locator("#session-banner")).to_have_text("Waiting to start")
    emit(page,{"type":"recording","on":True})
    text=page.locator("#record-indicator").inner_text()
    assert "記録中" not in text, f"English UI still shows Japanese recording state: {text}"


@case("STU-08", "session_banners")
def test_banners(page):
    join(page)
    for state,label in [("idle","Waiting to start"),("live","Live"),("paused","Paused"),("ended","Class has ended")]:
        emit(page,{"type":"session","state":state})
        expect(page.locator("#session-banner")).to_have_text(label)
    page.reload(); join(page)
    emit(page,{"type":"session","state":"live"})
    page.evaluate("qa.sockets.at(-1).close()")
    expect(page.locator("#session-banner")).to_have_text("Reconnecting…")


@case("STU-09", "delay_boundary_and_replay")
def test_delay(page):
    join(page)
    for seq,delay in enumerate([4999,5000,5001,0],1):
        caption(page,seq,delay)
    assert page.locator(".delay-tag").count()==2
    assert page.locator('[data-seq="1"] .delay-tag').count()==0
    expect(page.locator('[data-seq="2"] .delay-tag')).to_be_visible()
    expect(page.locator('[data-seq="3"] .delay-tag')).to_be_visible()
    assert page.locator('[data-seq="4"] .delay-tag').count()==0
    select_language(page, "zh")
    expect(page.locator(".delay-tag").first).to_have_text("延迟中")


@case("STU-10", "speaking_indicator_clears", complete=False)
def test_speaking(page):
    join(page)
    emit(page,{"type":"speaking","on":True})
    expect(page.locator("#speaking-indicator")).to_be_visible()
    emit(page,{"type":"session","state":"paused"})
    expect(page.locator("#speaking-indicator")).to_be_hidden()
    emit(page,{"type":"speaking","on":True})
    page.evaluate("qa.sockets.at(-1).close()")
    expect(page.locator("#speaking-indicator")).to_be_hidden()


@case("CONN-01", "student_retries_multiple_failures")
def test_student_retry(page):
    join(page)
    page.evaluate("qa.sockets.at(-1).close(); qa.failConnect=true")
    expect(page.locator("#session-banner")).to_have_text("Reconnecting…")
    delays=[]
    for base in [1000,2000,4000,8000,15000,15000]:
        delays.append(page.evaluate("qa.timers[0].delay"))
        assert base*0.7 <= delays[-1] <= base*1.3
        page.evaluate("qa.timers.shift().fn()")
    page.evaluate("qa.failConnect=false; qa.timers.shift().fn()")
    assert page.evaluate("state.retryDelayMs")==1000


@case("CONN-03", "continuous_watermark_and_history_floor")
def test_watermark(page):
    join(page)
    caption(page,1); caption(page,3)
    assert page.evaluate("state.lastSeq")==1
    page.evaluate("qa.sockets.at(-1).close(); qa.timers.shift().fn()")
    assert page.evaluate("qa.sent.at(-1).last_seq")==1
    caption(page,2)
    assert page.evaluate("state.lastSeq")==3
    emit(page,{"type":"joined","history_from":9,"session_state":"live","recording":False,"speaking":False})
    assert page.evaluate("state.lastSeq")==8


@case("CONN-05", "new_server_does_not_reuse_old_cards")
def test_new_session(page):
    join(page); caption(page,1,text="Old lesson")
    emit(page,{"type":"join_rejected","reason":"bad_code"})
    page.evaluate("qa.code='1234'")
    page.locator("#code-input").fill("1234")
    page.locator("#join-btn").click()
    caption(page,1,text="New lesson")
    expect(page.locator("#cards")).to_contain_text("New lesson")


@case("TEACH-05", "restarted_lesson_can_reconnect")
def test_restart(page):
    join(page)
    emit(page,{"type":"session","state":"ended"})
    emit(page,{"type":"session","state":"live"})
    page.evaluate("qa.sockets.at(-1).close()")
    assert page.evaluate("qa.timers.length")>0, "No reconnect scheduled after ended -> live"


@case("TEACH-01", "start_with_media_permission", "teacher", complete=False)
def test_start(page):
    page.locator("#start-btn").click()
    expect(page.locator("#session-state")).to_have_text("配信中")
    expect(page.locator("#start-btn")).to_be_disabled()
    assert page.evaluate("qa.micCalls.length")==1


@case("TEACH-02", "mic_denied_then_retry", "teacher")
def test_denied(page):
    page.evaluate("qa.failMic=true")
    page.locator("#start-btn").click()
    expect(page.locator("#mic-status")).to_contain_text("マイクを取得できませんでした")
    assert page.evaluate("state.sessionState")=="idle"
    page.evaluate("qa.failMic=false")
    page.locator("#start-btn").click()
    expect(page.locator("#session-state")).to_have_text("配信中")


@case("TEACH-03", "pause_resume_buttons", "teacher", complete=False)
def test_pause(page):
    page.locator("#start-btn").click()
    page.locator("#pause-btn").click()
    expect(page.locator("#start-btn")).to_have_text("再開")
    expect(page.locator("#pause-btn")).to_be_disabled()
    page.locator("#start-btn").click()
    expect(page.locator("#session-state")).to_have_text("配信中")


@case("TEACH-04", "end_buttons", "teacher", complete=False)
def test_end(page):
    page.locator("#start-btn").click()
    page.locator("#end-btn").click()
    expect(page.locator("#session-state")).to_have_text("終了")
    expect(page.locator("#pause-btn")).to_be_disabled()
    expect(page.locator("#end-btn")).to_be_disabled()


@case("MIC-01", "microphone_list_and_selection", "teacher")
def test_mic_list(page):
    expect(page.locator("#mic-select")).to_be_disabled()
    expect(page.locator("#mic-select option")).to_have_count(3)
    before = page.locator("#mic-select option").evaluate_all(
        "xs=>xs.map(x=>({value:x.value,label:x.textContent}))"
    )
    assert [x["value"] for x in before] == ["", "mic-a", "mic-b"]
    assert before[1]["label"] == before[2]["label"]
    assert [x["label"] for x in before[1:]] != ["Pin microphone", "USB microphone"]

    page.locator("#start-btn").click()
    expect(page.locator("#mic-select")).to_be_enabled()
    after = page.locator("#mic-select option").evaluate_all(
        "xs=>xs.map(x=>({value:x.value,label:x.textContent}))"
    )
    assert [x["value"] for x in after] == ["", "mic-a", "mic-b"]
    assert [x["label"] for x in after[1:]] == ["Pin microphone", "USB microphone"]

    page.locator("#mic-select").select_option("mic-a")
    expect(page.locator("#mic-status")).to_have_text("マイクを切り替えました。")
    selected = page.evaluate("""()=>({
      requested:qa.micCalls.at(-1).audio.deviceId.exact,
      streamDevice:state.micStream.deviceId,
      sourceDevice:state.micSource.stream.deviceId,
      sourceConnected:state.micSource.connected,
      sourceTarget:state.micSource.connectedTo === state.workletNode,
      stateDevice:state.micDeviceId,
      selected:el.micSelect.value,
    })""")
    assert selected == {
        "requested": "mic-a",
        "streamDevice": "mic-a",
        "sourceDevice": "mic-a",
        "sourceConnected": True,
        "sourceTarget": True,
        "stateDevice": "mic-a",
        "selected": "mic-a",
    }


@case("MIC-02", "microphone_persistence_and_missing_fallback", "teacher")
def test_mic_persist(page):
    page.locator("#start-btn").click()
    page.locator("#mic-select").select_option("mic-a")
    expect(page.locator("#mic-status")).to_have_text("マイクを切り替えました。")
    page.reload()
    expect(page.locator("#mic-select")).to_have_value("mic-a")
    page.evaluate("qa.devices=qa.devices.filter(d=>d.deviceId!=='mic-a'); refreshMicList()")
    expect(page.locator("#mic-notice")).to_contain_text("見つかりません")
    expect(page.locator("#mic-select")).to_have_value("")


@case("MIC-03", "microphone_switch_failure_preserves_old_track", "teacher")
def test_mic_failure(page):
    page.locator("#start-btn").click()
    expect(page.locator("#mic-select")).to_be_enabled()
    assert page.evaluate("qa.streams.length") == 1
    assert page.evaluate("qa.streams[0].track.stopped") is False
    assert page.evaluate("qa.sources[0].connected") is True
    assert page.evaluate("qa.sources[0].connectedTo === state.workletNode") is True

    page.evaluate("qa.deferNextMic=true")
    page.locator("#mic-select").select_option("mic-a")
    page.wait_for_function("qa.pendingMic?.requested === 'mic-a'")
    assert page.evaluate("qa.streams[0].track.stopped") is False
    assert page.evaluate("state.micStream === qa.streams[0]") is True
    assert page.evaluate("qa.sources[0].connected") is True
    assert page.evaluate("state.micDeviceId") == ""
    page.evaluate("qa.pendingMic.resolve()")
    expect(page.locator("#mic-status")).to_have_text("マイクを切り替えました。")
    page.wait_for_function("state.micDeviceId === 'mic-a'")
    assert page.evaluate("qa.streams.length") == 2
    assert page.evaluate("qa.streams[0].track.stopped") is True
    assert page.evaluate("qa.streams[1].track.stopped") is False
    assert page.evaluate("state.micStream === qa.streams[1]") is True
    assert page.evaluate("qa.sources[0].connected") is False
    assert page.evaluate("qa.sources[1].connected") is True
    assert page.evaluate("qa.sources[1].connectedTo === state.workletNode") is True

    page.evaluate("qa.deferNextMic=true")
    page.locator("#mic-select").select_option("mic-b")
    page.wait_for_function("qa.pendingMic?.requested === 'mic-b'")
    assert page.evaluate("qa.streams[1].track.stopped") is False
    assert page.evaluate("state.micStream === qa.streams[1]") is True
    assert page.evaluate("qa.sources[1].connected") is True
    assert page.evaluate("state.micDeviceId") == "mic-a"
    page.evaluate("qa.pendingMic.reject('NotReadableError')")
    expect(page.locator("#mic-notice")).to_contain_text("NotReadableError")
    expect(page.locator("#mic-notice")).to_contain_text("前のマイクのまま")
    expect(page.locator("#mic-select")).to_have_value("mic-a")
    assert page.evaluate("qa.streams.length") == 2
    assert page.evaluate("qa.streams[1].track.stopped") is False
    assert page.evaluate("state.micStream === qa.streams[1]") is True
    assert page.evaluate("state.micDeviceId") == "mic-a"
    assert page.evaluate("qa.sources.length") == 2
    assert page.evaluate("qa.sources[1].connected") is True
    assert page.evaluate("qa.sources[1].connectedTo === state.workletNode") is True


@case("MIC-04", "microphone_meter", "teacher")
def test_meter(page):
    page.evaluate("updateMicMeter(new Int16Array(1600).fill(1500).buffer)")
    assert page.locator("#mic-meter-bar").evaluate("e=>e.style.width")=="50%"
    page.evaluate("updateMicMeter(new Int16Array(1600).buffer)")
    expect(page.locator("#mic-meter-bar")).to_have_class("silent")


@case("MIC-05", "audio_worklet_rate_and_send_gate", "teacher")
def test_worklet(page):
    source=(ROOT/"web/audio-worklet.js").read_text(encoding="utf-8")
    for rate in [16000,44100,48000]:
        result=page.evaluate("""({source,rate})=>{
          const chunks=[]; let Processor;
          const Base=class {constructor(){this.port={postMessage:b=>chunks.push(Array.from(new Int16Array(b)))}}};
          new Function('sampleRate','AudioWorkletProcessor','registerProcessor',source)(rate,Base,(_,p)=>Processor=p);
          const p=new Processor(); for(let n=0;n<rate*2;n+=128) p.process([[new Float32Array(Math.min(128,rate*2-n)).fill(0.5)]]);
          return {sizes:chunks.map(x=>x.length),values:chunks.flat().every(x=>x===16383)};
        }""",{"source":source,"rate":rate})
        assert 19<=len(result["sizes"])<=20 and set(result["sizes"])=={1600}
        assert result["values"]
    page.locator("#start-btn").click()
    page.evaluate("state.workletNode.port.onmessage({data:new Int16Array(1600).buffer})")
    assert page.evaluate("qa.sent.at(-1).audioBytes")==3200
    page.locator("#pause-btn").click()
    before=page.evaluate("qa.sent.length")
    page.evaluate("state.workletNode.port.onmessage({data:new Int16Array(1600).buffer})")
    assert page.evaluate("qa.sent.length")==before


@case("MON-01", "teacher_transcript_last_50", "teacher")
def test_transcript(page):
    for seq in range(1,53):
        emit(page,{"type":"asr_final","seq":seq,"ja":f"日本語{seq}","turn_id":seq})
    assert page.locator("#transcript li").count()==50
    expect(page.locator("#transcript li").first).to_have_text("#3 日本語3")
    expect(page.locator("#transcript li").last).to_have_text("#52 日本語52")


def assert_worklet_count(page, rate):
    source = (ROOT / "web/audio-worklet.js").read_text(encoding="utf-8")
    result = page.evaluate("""({source, rate}) => {
      let Processor, emitted = 0, wrongChunk = false;
      const Base = class { constructor() { this.port = {postMessage: b => {
        wrongChunk ||= b.byteLength !== 3200; emitted += b.byteLength / 2;
      }}; }};
      new Function('sampleRate', 'AudioWorkletProcessor', 'registerProcessor', source)(rate, Base, (_, p) => Processor = p);
      const p = new Processor(), samples = rate * 60;
      const block = new Float32Array(128).fill(0.25);
      for (let offset = 0; offset < samples; offset += 128)
        p.process([[samples - offset >= 128 ? block : block.slice(0, samples - offset)]]);
      return {actual: emitted + p.outLen, expected: Math.ceil((samples - 1) / (rate / 16000)), wrongChunk};
    }""", {"source": source, "rate": rate})
    assert not result["wrongChunk"]
    assert abs(result["actual"] - result["expected"]) <= 1, f"{rate}Hz 60秒入力の出力サンプル数がずれた: {result}"


@case("MIC-05", "worklet_16000hz_60s_sample_count", "teacher")
def test_worklet_16k_count(page):
    assert_worklet_count(page, 16000)


@case("MIC-05", "worklet_44100hz_60s_sample_count", "teacher")
def test_worklet_44k_count(page):
    assert_worklet_count(page, 44100)


@case("MIC-05", "worklet_48000hz_60s_sample_count", "teacher")
def test_worklet_48k_count(page):
    assert_worklet_count(page, 48000)


@case("MIC-05", "worklet_silence_and_clipping", "teacher")
def test_worklet_clipping(page):
    source = (ROOT / "web/audio-worklet.js").read_text(encoding="utf-8")
    result = page.evaluate("""source => {
      let Processor; const chunks = [];
      const Base = class { constructor() { this.port = {postMessage: b => chunks.push(...new Int16Array(b))}; }};
      new Function('sampleRate', 'AudioWorkletProcessor', 'registerProcessor', source)(16000, Base, (_, p) => Processor = p);
      const p = new Processor(), signal = new Float32Array(1601);
      for (let i=0; i<signal.length; i++) signal[i] = [0, 2, -2][i % 3];
      p.process([[signal]]);
      return {length:chunks.length, correct:chunks.every((v, i) => v === [0,32767,-32768][i % 3])};
    }""", source)
    assert result == {"length": 1600, "correct": True}


@case("MON-02", "partial_replacement_and_clear", "teacher", complete=False)
def test_partial(page):
    emit(page,{"type":"turn.partial","turn_id":3,"revision":1,"ja":"途中"})
    emit(page,{"type":"turn.partial","turn_id":3,"revision":2,"ja":"途中の説明"})
    expect(page.locator("#partial")).to_have_text("途中の説明")
    emit(page,{"type":"asr_final","seq":1,"turn_id":3,"ja":"確定した説明"})
    expect(page.locator("#partial")).to_be_hidden()


@case("MON-04", "student_count_and_language_display", "teacher", complete=False)
@case("MON-05", "latency_breakdown_display", "teacher", complete=False)
@case("MON-06", "cache_display", "teacher", complete=False)
@case("MON-08", "overload_warning_display", "teacher", complete=False)
def test_stats(page):
    msg={"type":"stats","students":3,"langs":{"en":2,"zh":1},"median_delay_ms":750,"asr_wait_seconds":1.5,"asr_active_seconds":2.5,"median_asr_ms":123,"mt_queue_depth":4,"median_mt_ms":456,"mt_cache_hits":2,"mt_cache_hit_rate":0.5,"overloaded":True}
    emit(page,msg)
    for id_,expected in [("students","3"),("langs","en: 2 / zh: 1"),("delay","750"),("asr-wait","1.5"),("asr-active","2.5"),("asr-ms","123"),("mt-queue","4"),("mt-ms","456"),("cache","50% (2件節約)")]:
        expect(page.locator("#stat-"+id_)).to_have_text(expected)
    expect(page.locator("#overload-warning")).to_be_visible()
    emit(page,{**msg,"overloaded":False})
    expect(page.locator("#overload-warning")).to_be_hidden()


@case("MON-07", "silence_warning_display_and_clear", "teacher", complete=False)
def test_silence(page):
    emit(page,{"type":"error","code":"mic_silent","message":"30秒以上音声がありません"})
    expect(page.locator("#silence-warning")).to_be_visible()
    emit(page,{"type":"session","state":"paused"})
    expect(page.locator("#silence-warning")).to_be_hidden()


@case("REC-02", "teacher_recording_toggle", "teacher", complete=False)
def test_record(page):
    expect(page.locator("#record-toggle")).not_to_be_checked()
    page.locator("#record-toggle").check()
    expect(page.locator("#record-indicator")).to_be_visible()
    page.locator("#record-toggle").uncheck()
    expect(page.locator("#record-indicator")).to_be_hidden()


@case("CONN-08", "teacher_retries_after_failed_retry", "teacher")
def test_teacher_retry(page):
    page.evaluate("qa.sockets.at(-1).close(); qa.failConnect=true")
    expect(page.locator("#start-btn")).to_be_disabled()
    assert page.evaluate("qa.timers.length")==1
    page.evaluate("qa.timers.shift().fn()")
    assert page.evaluate("qa.timers.length")==1, "First retry failed; no further retry was scheduled"


@case("CONN-09", "replaced_teacher_does_not_reconnect", "teacher", complete=False)
def test_replaced(page):
    page.evaluate("qa.sockets.at(-1).close(4000)")
    expect(page.locator("#page-error")).to_contain_text("別のタブ")
    assert page.evaluate("qa.timers.length")==0


@case(
    "STU-11",
    "narrow_screen_large_captions",
    complete=False,
    context_options={
        "viewport": {"width": 375, "height": 667},
        "has_touch": True,
        "is_mobile": True,
        "device_scale_factor": 2,
    },
    scope=(
        "実Edge Chromiumのmobile/touch模擬。375x667、DPR 2、縦横・半分のCSS viewportによる"
        "200% reflow相当、touch入力、模擬WS切断後の再接続を確認。実iOS Safari/ChromeOS、"
        "OSの30秒ロック、実機browser zoom、実LANは未検証であり代替しない"
    ),
)
def test_narrow_screen(page):
    def assert_reflow(label: str, width: int, height: int) -> dict:
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_function(
            "size => innerWidth === size.width && innerHeight === size.height",
            arg={"width": width, "height": height},
        )
        metrics = page.evaluate(
            """label => {
              const root = document.documentElement;
              const body = document.body;
              const selectors = [
                '#caption-screen header', '#session-banner', '#settings-btn',
                '#ja-toggle', '#size-buttons', '#lang-select', '#cards'
              ];
              const outside = selectors.flatMap(selector =>
                [...document.querySelectorAll(selector)].flatMap(node => {
                  const rect = node.getBoundingClientRect();
                  return rect.left < -1 || rect.right > innerWidth + 1
                    ? [{selector, left: rect.left, right: rect.right}]
                    : [];
                })
              );
              const cardOverflow = [...document.querySelectorAll('#cards .card')].flatMap((card, index) => {
                const text = card.querySelector('.text');
                const bad = card.scrollWidth > card.clientWidth + 1
                  || card.scrollHeight > card.clientHeight + 1
                  || text.scrollWidth > text.clientWidth + 1
                  || text.scrollHeight > text.clientHeight + 1;
                return bad ? [{
                  index,
                  cardWidth: [card.clientWidth, card.scrollWidth],
                  cardHeight: [card.clientHeight, card.scrollHeight],
                  textWidth: [text.clientWidth, text.scrollWidth],
                  textHeight: [text.clientHeight, text.scrollHeight],
                }] : [];
              });
              return {
                label,
                innerWidth,
                innerHeight,
                devicePixelRatio,
                documentOverflow: Math.max(root.scrollWidth, body.scrollWidth) - innerWidth,
                cardsClientHeight: document.querySelector('#cards').clientHeight,
                outside,
                cardOverflow,
              };
            }""",
            label,
        )
        assert metrics["documentOverflow"] <= 1, f"{label}: ページ全体が横にはみ出す: {metrics}"
        assert metrics["cardsClientHeight"] > 0, f"{label}: 字幕領域が表示領域を失った: {metrics}"
        assert not metrics["outside"], f"{label}: 操作部が横方向に欠ける: {metrics}"
        return metrics

    page.evaluate(
        """() => {
          qa.pointerUps = [];
          qa.cardTouchStarts = [];
          document.addEventListener('pointerup', event => {
            const button = event.target.closest && event.target.closest('button');
            qa.pointerUps.push({
              pointerType: event.pointerType,
              target: button ? (button.id || button.dataset.size || button.dataset.code) : event.target.id,
              trusted: event.isTrusted,
            });
          }, true);
          document.querySelector('#cards').addEventListener('touchstart', event => {
            qa.cardTouchStarts.push({trusted: event.isTrusted});
          }, {passive: true});
          qa.current = 'live';
        }"""
    )

    page.locator('#lang-options button[data-code="en"]').tap()
    page.locator("#code-input").tap()
    page.locator("#code-input").fill("4831")
    page.locator("#join-btn").tap()
    expect(page.locator("#caption-screen")).to_be_visible()

    page.locator("#settings-btn").tap()
    large_button = page.locator('#size-buttons button[data-size="l"]')
    large_button.tap()
    assert large_button.evaluate("button => button.classList.contains('selected')")
    page.locator("#settings-close").tap()
    expect(page.locator("#settings-sheet")).to_be_hidden()

    long_text = "Photosynthesis needs sunlight and carbon dioxide. " * 12
    for seq in range(1, 7):
        caption(page, seq=seq, text=f"Caption {seq}. {long_text}")
    expect(page.locator("#cards .card")).to_have_count(6)
    expect(page.locator("#cards .text").last).to_contain_text("carbon dioxide")
    # 字幕は古いほど小さくする設計なので、文字サイズ「大」の大きさは最新の字幕で測る
    computed_caption_font_px = page.locator("#cards .text").last.evaluate(
        "node => parseFloat(getComputedStyle(node).fontSize)"
    )
    assert computed_caption_font_px >= 25.5, computed_caption_font_px

    portrait = assert_reflow("portrait", 375, 667)
    zoom_200_equivalent = assert_reflow("200%-reflow-equivalent", 188, 334)
    page.locator("#settings-btn").tap()
    expect(page.locator("#settings-sheet")).to_be_visible()
    assert_reflow("200%-reflow-settings-open", 188, 334)
    page.locator("#settings-close").tap()
    expect(page.locator("#settings-sheet")).to_be_hidden()
    landscape = assert_reflow("landscape", 667, 375)
    assert_reflow("portrait-restored", 375, 667)

    cards = page.locator("#cards")
    page.wait_for_function(
        """() => {
          const cards = document.querySelector('#cards');
          return cards.scrollHeight > cards.clientHeight
            && cards.scrollHeight - cards.scrollTop - cards.clientHeight <= 1;
        }"""
    )
    before_touch_scroll = cards.evaluate("node => node.scrollTop")
    assert before_touch_scroll > 20, before_touch_scroll
    page.locator("#caption-screen").evaluate(
        "node => node.scrollIntoView({block: 'start', inline: 'nearest'})"
    )
    box = cards.bounding_box()
    assert box and box["height"] > 80, box
    viewport = page.evaluate("({width: innerWidth, height: innerHeight})")
    visible_left = max(0, box["x"])
    visible_right = min(viewport["width"], box["x"] + box["width"])
    visible_top = max(0, box["y"])
    visible_bottom = min(viewport["height"], box["y"] + box["height"])
    assert visible_right - visible_left > 40 and visible_bottom - visible_top > 80, {
        "box": box,
        "viewport": viewport,
    }
    x = (visible_left + visible_right) / 2
    y = (visible_top + visible_bottom) / 2
    touch_hit = page.evaluate(
        """point => {
          const node = document.elementFromPoint(point.x, point.y);
          return {
            tag: node && node.tagName,
            id: node && node.id,
            className: node && node.className,
            insideCards: Boolean(node && node.closest('#cards')),
          };
        }""",
        {"x": x, "y": y},
    )
    assert touch_hit["insideCards"], {
        "box": box,
        "viewport": viewport,
        "touch_hit": touch_hit,
    }
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart",
                "touchPoints": [{"id": 1, "x": x, "y": y - min(160, y - visible_top - 10)}],
            },
        )
        start_y = y - min(160, y - visible_top - 10)
        end_y = y + min(160, visible_bottom - y - 10)
        for step in range(1, 9):
            move_y = start_y + (end_y - start_y) * step / 8
            cdp.send(
                "Input.dispatchTouchEvent",
                {
                    "type": "touchMove",
                    "touchPoints": [{"id": 1, "x": x, "y": move_y}],
                },
            )
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(resolve))"
            )
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    finally:
        cdp.detach()
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    after_touch_scroll = page.evaluate(
        """() => ({
          cardsTop: document.querySelector('#cards').scrollTop,
          documentTop: document.scrollingElement.scrollTop,
          following: state.following,
          touchStarts: qa.cardTouchStarts,
        })"""
    )
    assert after_touch_scroll["cardsTop"] < before_touch_scroll - 20, {
        "before": before_touch_scroll,
        "after": after_touch_scroll,
        "box": box,
        "viewport": viewport,
        "touch_hit": touch_hit,
    }
    page.wait_for_function("state.following === false")
    expect(page.locator("#latest-btn")).to_be_visible()
    assert page.evaluate("qa.cardTouchStarts.some(event => event.trusted)")

    page.locator("#latest-btn").tap()
    page.wait_for_function(
        """() => {
          const cards = document.querySelector('#cards');
          return state.following
            && cards.scrollHeight - cards.scrollTop - cards.clientHeight <= 1;
        }"""
    )
    touch_targets = page.evaluate(
        "qa.pointerUps.filter(event => event.pointerType === 'touch' && event.trusted).map(event => event.target)"
    )
    assert "l" in touch_targets, touch_targets
    assert "latest-btn" in touch_targets, touch_targets

    last_seq_before_background = page.evaluate("state.lastSeq")
    socket_count = page.evaluate("qa.sockets.length")
    visibility_simulation = page.evaluate(
        """() => {
          qa.visibilityState = 'visible';
          qa.visibilityChanges = [];
          Object.defineProperty(document, 'visibilityState', {
            configurable: true,
            get: () => qa.visibilityState,
          });
          Object.defineProperty(document, 'hidden', {
            configurable: true,
            get: () => qa.visibilityState === 'hidden',
          });
          document.addEventListener('visibilitychange', () => {
            qa.visibilityChanges.push(document.visibilityState);
          });
          return {
            visibilityState: document.visibilityState,
            hidden: document.hidden,
            mechanism: 'document property override + visibilitychange',
          };
        }"""
    )
    page.evaluate(
        """() => {
          qa.visibilityState = 'hidden';
          document.dispatchEvent(new Event('visibilitychange'));
          qa.sockets.at(-1).close();
        }"""
    )
    page.wait_for_function(
        "document.visibilityState === 'hidden' && state.bannerKey === 'disconnected' && qa.timers.length === 1"
    )
    page.evaluate(
        """() => {
          qa.visibilityState = 'visible';
          document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )
    page.wait_for_function("document.visibilityState === 'visible'")
    visibility_changes = page.evaluate("qa.visibilityChanges")
    assert visibility_changes == ["hidden", "visible"], visibility_changes

    page.evaluate("qa.timers.shift().fn()")
    page.wait_for_function(
        "count => qa.sockets.length === count + 1 && qa.sockets.at(-1).readyState === 1",
        arg=socket_count,
    )
    rejoin = page.evaluate("qa.sent.filter(message => message.type === 'join').at(-1)")
    assert rejoin["last_seq"] == last_seq_before_background, rejoin
    page.wait_for_function("state.joined && state.bannerKey === 'live'")

    caption(page, seq=last_seq_before_background + 2, text="Recovered caption 8")
    caption(page, seq=last_seq_before_background + 1, text="Recovered caption 7")
    expected_seqs = list(range(1, last_seq_before_background + 3))
    page.wait_for_function(
        "expected => state.lastSeq === expected",
        arg=expected_seqs[-1],
    )
    actual_seqs = page.locator("#cards .card").evaluate_all(
        "cards => cards.map(card => Number(card.dataset.seq))"
    )
    assert actual_seqs == expected_seqs, actual_seqs

    card_overflow = {
        metrics["label"]: metrics["cardOverflow"]
        for metrics in (portrait, zoom_200_equivalent, landscape)
        if metrics["cardOverflow"]
    }
    violations = []
    if card_overflow:
        violations.append({
            "kind": "card_internal_overflow",
            "layouts": card_overflow,
        })
    observation = {
        "context": {
            "viewport": "375x667",
            "has_touch": True,
            "is_mobile": True,
            "device_scale_factor": portrait["devicePixelRatio"],
        },
        "font": {"selected": "l", "computed_px": computed_caption_font_px},
        "reflow": {
            "portrait": portrait,
            "landscape": landscape,
            "zoom_200_equivalent": zoom_200_equivalent,
        },
        "touch": {
            "trusted_touch_scroll": True,
            "trusted_tap_targets": touch_targets,
            "hit_target": touch_hit,
            "cards_scroll_top_before": before_touch_scroll,
            "cards_scroll_top_after": after_touch_scroll["cardsTop"],
        },
        "background_foreground": {
            "simulation": "synthetic document visibilityState/hidden + visibilitychange, explicit fake WebSocket close, controlled retry timer",
            "visibility_setup": visibility_simulation,
            "visibility_changes": visibility_changes,
            "rejoin_last_seq": rejoin["last_seq"],
            "restored_seqs": actual_seqs,
        },
        "violations": violations,
        "boundaries": [
            "device_scale_factor=2 is high-DPI emulation, not real browser 200% zoom",
            "188x334 is a half-CSS-viewport reflow stress surrogate",
            "synthetic visibility properties/events and controlled timer do not reproduce a 30-second OS lock, real tab lifecycle, or Safari timer freezing",
            "headless Edge does not substitute for physical iOS Safari or ChromeOS Chrome",
        ],
    }
    try:
        assert not violations, (
            "STU-11 collected violations: "
            + json.dumps(violations, ensure_ascii=False, sort_keys=True)
        )
    except AssertionError as error:
        raise EvidenceAssertionError(str(error), observation) from error
    return observation


@case("OPS-10", "teacher_url_and_qr_pixels_match", "teacher", complete=False)
def test_teacher_join_qr(page):
    """画面URLと、出荷QRライブラリが描いた画素を同じpayloadへ固定する。"""

    expected = "http://127.0.0.1:18765/?code=4831"
    expect(page.locator("#join-code")).to_have_text("4831")
    expect(page.locator("#join-url")).to_have_text(expected)
    expect(page.locator("#qr")).to_have_attribute("title", expected)
    expect(page.locator("#qr canvas")).to_have_count(1)

    decoded = decode_qr_with_local_opencv(page.screenshot(full_page=True))
    assert decoded["detected"], decoded
    assert decoded["value"] == expected, decoded
    print(f'OPS-10 decoder={decoded["decoder"]}; payload={decoded["value"]}', flush=True)

    comparison = page.evaluate(
        """expected => {
          const actual = document.querySelector('#qr canvas');
          const render = text => {
            const host = document.createElement('div');
            host.style.position = 'fixed';
            host.style.left = '-10000px';
            document.body.appendChild(host);
            new QRCode(host, {text, width: 200, height: 200});
            const canvas = host.querySelector('canvas');
            const pixels = Array.from(
              canvas.getContext('2d').getImageData(0, 0, 200, 200).data
            );
            host.remove();
            return pixels;
          };
          const actualPixels = Array.from(
            actual.getContext('2d').getImageData(0, 0, 200, 200).data
          );
          const expectedPixels = render(expected);
          const alternatePixels = render(expected.replace('4831', '4832'));
          const digest = pixels => {
            let hash = 2166136261;
            for (const value of pixels) {
              hash ^= value;
              hash = Math.imul(hash, 16777619);
            }
            return hash >>> 0;
          };
          return {
            width: actual.width,
            height: actual.height,
            actualDigest: digest(actualPixels),
            expectedDigest: digest(expectedPixels),
            alternateDigest: digest(alternatePixels),
            distinctColors: new Set(actualPixels).size,
          };
        }""",
        expected,
    )
    assert comparison["width"] == comparison["height"] == 200
    assert comparison["distinctColors"] >= 2
    assert comparison["actualDigest"] == comparison["expectedDigest"]
    assert comparison["actualDigest"] != comparison["alternateDigest"]
    return {
        "decoder": decoded["decoder"],
        "image_source": "full_page_edge_screenshot",
        "teacher_info_join_url": expected,
        "decoded_join_url": decoded["value"],
    }


def preview_tracker():
    directory = ROOT / "docs/qa"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless=True)
        page = browser.new_page(viewport={"width": 1800, "height": 950})
        page.route("**/*", lambda route: route.abort())
        page.set_content((directory / "feature-status.html").read_text(encoding="utf-8"))
        page.screenshot(path=str(directory / "feature-status-preview.png"))
        overflow = page.locator("td").evaluate_all("cells => cells.filter(c => c.scrollHeight > c.clientHeight + 2).map(c => ({text:c.textContent, height:c.clientHeight, scroll:c.scrollHeight}))")
        (directory / "preview-check.json").write_text(json.dumps({"vertical_overflow": overflow}, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
        assert not overflow, overflow[:3]
    print("Spreadsheet HTML preview: no vertically clipped cells")
    return 0


def _evidence_tree_files(root: Path, *, suffix: str | None = None) -> tuple[Path, ...]:
    """Select reviewed files while surfacing link-like entries for rejection."""

    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    if root.is_symlink() or bool(is_junction(root)) or not root.is_dir():
        return (root,)
    result = []
    for path in root.rglob("*"):
        if any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(root).parts
        ):
            continue
        link_like = path.is_symlink() or bool(is_junction(path))
        if link_like or (path.is_file() and (suffix is None or path.suffix == suffix)):
            result.append(path)
    return tuple(result)


def _evidence_sources() -> tuple[Path, ...]:
    return (
        *_evidence_tree_files(ROOT / "web"),
        Path(__file__),
        ROOT / "scripts/qa_evidence_manifest.py",
        ROOT / "scripts/story_tracker.py",
        ROOT / "docs/qa/stories.tsv",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story", action="append")
    parser.add_argument("--preview-tracker", action="store_true")
    args = parser.parse_args()
    if args.preview_tracker:
        return preview_tracker()
    if not _import_code_is_current():
        print(
            "Imported browser runner code differs from current disk state; rerun.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    out=ROOT/"docs/qa/runs"/datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-browser")
    try:
        validate_output_parent(ROOT, out)
    except EvidenceManifestError as exc:
        print(f"Evidence output rejected: {exc}", file=sys.stderr, flush=True)
        return 2
    out.mkdir(parents=True,exist_ok=False)
    results=[]
    try:
        source_hashes_at_start = snapshot_supplied_source_hashes(
            ROOT, _evidence_sources
        )
    except (OSError, EvidenceManifestError):
        payload = {
            "finished_at": datetime.now().astimezone().isoformat(),
            "browser": None,
            "results": results,
            "source_snapshot_error": traceback.format_exc(),
        }
        commit_browser_result(
            out, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )
        print(f"Evidence: {out}; source snapshot failed", flush=True)
        return 2
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",headless=True)
        version=browser.version
        browser_close_error = None
        try:
            for story,name,kind,complete,fn,context_options,scope in CASES:
                if args.story and story not in args.story:
                    continue
                context = None
                page = None
                errors=[]
                item={"story":story,"scenario":name,"complete":complete,"scope":scope or DEFAULT_SCOPE}
                try:
                    options={"viewport":{"width":1280,"height":900}}
                    options.update(context_options)
                    context=browser.new_context(**options)
                    page=context.new_page()
                    page.set_default_timeout(3000)
                    page.add_init_script(MOCK)
                    def route(request):
                        from urllib.parse import urlsplit
                        path=urlsplit(request.request.url).path
                        if path=="/api/config":
                            request.fulfill(json={"languages":[{"code":"en","label":"English"},{"code":"zh","label":"中文（简体）"}]})
                        elif path=="/api/teacher-info":
                            request.fulfill(json={"code":"4831","join_url":"http://127.0.0.1:18765/?code=4831","languages":[]})
                        else:
                            relative="teacher.html" if path=="/teacher" else "index.html" if path=="/" else path.removeprefix("/static/")
                            file=(ROOT/"web"/relative).resolve()
                            if not file.is_relative_to(ROOT/"web") or not file.is_file():
                                request.fulfill(status=404,body="Not found")
                            else:
                                request.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(file)[0] or "application/octet-stream")
                    page.route("**/*",route)
                    page.on("pageerror",lambda error:errors.append(str(error)))
                    page.goto("http://127.0.0.1:18765/teacher" if kind=="teacher" else "http://127.0.0.1:18765/?code=4831")
                    if kind=="teacher":
                        expect(page.locator("#start-btn")).to_be_enabled()
                    else:
                        expect(page.locator("#lang-options button")).to_have_count(2)
                    observation = fn(page)
                    if observation is not None:
                        item["observation"] = observation
                    assert not errors, errors
                    item["status"]="pass"
                except Exception as error:
                    item.update(status="fail",detail=traceback.format_exc(),page_errors=errors)
                    preserved_observation = getattr(error, "observation", None)
                    if preserved_observation is not None:
                        item["observation"] = preserved_observation
                    if page is not None:
                        try:
                            page.screenshot(path=str(out/f"{story}-{name}.png"),full_page=True)
                        except Exception:
                            item["screenshot_error"] = traceback.format_exc()
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            item.update(status="fail", context_close_error=traceback.format_exc())
                    results.append(item)
                    print(story,name,item["status"],flush=True)
        finally:
            try:
                browser.close()
            except Exception:
                browser_close_error = traceback.format_exc()
    payload={"finished_at":datetime.now().astimezone().isoformat(),"browser":version,"results":results}
    if browser_close_error:
        payload["browser_close_error"] = browser_close_error
    browser_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    import_code_current = _import_code_is_current()
    if browser_close_error or not import_code_current:
        commit_browser_result(out, browser_bytes)
        sealed = False
    else:
        sealed = publish_browser_evidence(
            out,
            root=ROOT,
            kind="browser",
            source_files=_evidence_sources,
            source_hashes_at_start=source_hashes_at_start,
            browser_bytes=browser_bytes,
        )
    print(f"Evidence: {out}; passed={sum(r['status']=='pass' for r in results)}, failed={sum(r['status']=='fail' for r in results)}")
    if browser_close_error or not import_code_current or not sealed:
        return 2
    return int(any(r["status"]=="fail" for r in results))


if __name__=="__main__":
    raise SystemExit(main())
