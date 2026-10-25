"""Real loopback HTTP/WS + Edge. Only media hardware and ASR/MT are substituted.

Each scenario owns an ephemeral loopback listener, separate from the soak test.
No production configuration, certificates, microphone, or classroom data change.
"""

from contextlib import contextmanager, ExitStack
import argparse
import ctypes
from datetime import datetime
import hashlib
from ipaddress import ip_address
import json
import os
from pathlib import Path
import socket
import stat
import sys
from tempfile import TemporaryDirectory
import threading
import time
import traceback
from urllib.parse import parse_qs, urlsplit


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
            raise RuntimeError("native browser source is outside repository") from exc
        current = absolute_root
        for part in relative.parts[:-1]:
            identity = current.lstat()
            if (
                current.is_symlink()
                or bool(is_junction(current))
                or not stat.S_ISDIR(identity.st_mode)
            ):
                raise RuntimeError(
                    f"native browser source has unsafe ancestor: {current}"
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
                f"native browser source has unsafe ancestor: {current}"
            )
        ancestor_identities.append(
            (current, (identity.st_dev, identity.st_ino))
        )
    if path.is_symlink() or bool(is_junction(path)):
        raise RuntimeError(f"native browser runner source is a link: {path}")
    before, before_revision = _early_revision_snapshot(path)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"native browser runner source is not regular: {path}")
    content = path.read_bytes()
    _after, after_revision = _early_revision_snapshot(path)
    if before_revision != after_revision:
        raise RuntimeError("native browser runner source changed during import")
    for ancestor, expected in ancestor_identities:
        identity = ancestor.lstat()
        if (
            ancestor.is_symlink()
            or bool(is_junction(ancestor))
            or not stat.S_ISDIR(identity.st_mode)
            or (identity.st_dev, identity.st_ino) != expected
        ):
            raise RuntimeError(
                "native browser source ancestor changed during import"
            )
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
        raise RuntimeError("native browser runner source revision was unstable")
    return second, second_revision


_SELF_PATH = Path(os.path.abspath(__file__))
ROOT = _SELF_PATH.parent.parent
_SELF_IMPORT_STATE = _early_self_state(_SELF_PATH, root=ROOT)


def _early_native_import_files(root: Path) -> tuple[Path, ...]:
    is_junction = getattr(os.path, "isjunction", lambda _path: False)
    server_root = root / "server"
    if (
        server_root.is_symlink()
        or bool(is_junction(server_root))
        or not server_root.is_dir()
    ):
        raise RuntimeError("native browser server source tree is unsafe")
    paths = {
        _SELF_PATH,
        root / "scripts/qa_browser.py",
        root / "scripts/qa_evidence_manifest.py",
        root / "tests/conftest.py",
    }

    def walk(directory: Path) -> None:
        for entry in os.scandir(directory):
            path = Path(entry.path)
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            if entry.is_symlink() or bool(is_junction(path)):
                raise RuntimeError(
                    f"native browser server source tree contains a link: {path}"
                )
            if entry.is_dir(follow_symlinks=False):
                walk(path)
            elif entry.name.endswith(".py"):
                if not entry.is_file(follow_symlinks=False):
                    raise RuntimeError(
                        f"native browser server source is not regular: {path}"
                    )
                paths.add(path)

    walk(server_root)
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


_EARLY_IMPORT_CODE_FILES = _early_native_import_files(ROOT)
_EARLY_IMPORT_CODE_STATE = {
    path: (
        _SELF_IMPORT_STATE
        if path == _SELF_PATH
        else _early_self_state(path, root=ROOT)
    )
    for path in _EARLY_IMPORT_CODE_FILES
}
# Keep repository imports off potentially stale worktree bytecode.  Native is
# launched as a source script; every local import below uses this empty,
# process-private cache tree.
_IMPORT_PYCACHE = TemporaryDirectory(
    prefix="linguabridge-native-import-pycache-"
)
sys.pycache_prefix = _IMPORT_PYCACHE.name
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from scripts.qa_browser import MOCK, decode_qr_with_local_opencv, sync_playwright, expect
from scripts.qa_evidence_manifest import (
    EvidenceManifestError,
    commit_browser_result,
    publish_browser_evidence,
    snapshot_supplied_source_hashes,
    snapshot_source_hashes,
    validate_output_parent,
)
from server.asr.fake_engine import FakeASREngine
from server.asr.base import ASRResult
from server.main import create_app
from server.network import resolve_ip
from server.publication import Publication
from server.mt.fake_engine import FakeTranslationEngine
from tests.conftest import JOIN_CODE, make_ws_test_config
import uvicorn

# Importing the controlled runner installs its own private prefix.  All local
# imports above were therefore protected, but the native entry point must own
# the process-wide setting for the remainder of its run and for its final
# integrity checks.
sys.pycache_prefix = _IMPORT_PYCACHE.name
sys.dont_write_bytecode = True


def _loaded_native_repo_modules() -> tuple[Path, ...]:
    files = {
        Path(os.path.abspath(__file__)),
        ROOT / "scripts/qa_browser.py",
        ROOT / "scripts/qa_evidence_manifest.py",
        ROOT / "tests/conftest.py",
    }
    server_root = Path(os.path.abspath(ROOT / "server"))
    for module in tuple(sys.modules.values()):
        value = getattr(module, "__file__", None)
        if not isinstance(value, str) or not value:
            continue
        path = Path(os.path.abspath(value))
        if path.suffix != ".py" or not path.is_relative_to(server_root):
            continue
        files.add(path)
    return tuple(sorted(files, key=lambda path: path.as_posix()))


_IMPORT_ROOT = ROOT
_IMPORT_CODE_FILES = _EARLY_IMPORT_CODE_FILES
if not set(_loaded_native_repo_modules()) <= set(_IMPORT_CODE_FILES):
    raise RuntimeError("native browser imported an unbound repository module")
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
            "native browser audit source changed while local modules imported"
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

MEDIA_ONLY = MOCK.replace("window.WebSocket=FakeSocket;", "").replace(
    "window.setTimeout = (fn, delay) => { qa.timers.push({fn,delay}); return qa.timers.length; };", ""
)
NATIVE_TRACE = """
window.qaNativeFrames = [];
window.qaNativeSent = [];
window.qaNativeClosed = [];
window.qaNativeSocketUrls = [];
const NativeWebSocket = window.WebSocket;
window.WebSocket = class extends NativeWebSocket {
  constructor(...args) { super(...args); qaNativeSocketUrls.push(String(args[0])); this.addEventListener('message', e => {
    qaNativeFrames.push({message:JSON.parse(e.data), at:performance.now()});
  }); this.addEventListener('close', e => qaNativeClosed.push(e.code)); }
  send(data) {
    qaNativeSent.push({binary:typeof data !== 'string', bytes:data.byteLength || 0});
    return super.send(data);
  }
};
"""


class ControlledASR(FakeASREngine):
    def __init__(self):
        super().__init__()
        self.gate = threading.Event()
        self.gate.set()
        self.replies = []

    def transcribe(self, pcm16, sample_rate):
        if not self.gate.wait(timeout=15):
            raise TimeoutError("QA ASR gate was not released")
        if self.replies:
            result = ASRResult(text=self.replies.pop(0))
            self.calls.append(result)
            return result
        return super().transcribe(pcm16, sample_rate)


class Screens:
    def __init__(self, browser, port, *, lan_ip=None):
        self.browser, self.port = browser, port
        self.contexts, self.pages, self.errors = [], [], []
        self.lan_ip = lan_ip
        self.allowed_hosts = {"127.0.0.1"}
        if lan_ip is not None:
            self.allowed_hosts.add(lan_ip)
        self.navigations = []

    def open(self, role="student", lang="en", *, native_media=False, host="127.0.0.1"):
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.contexts.append(context)
        context.add_init_script(("" if native_media else MEDIA_ONLY) + NATIVE_TRACE)
        page = context.new_page()
        self.pages.append(page)
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        def guard(route):
            url = urlsplit(route.request.url)
            if url.hostname in self.allowed_hosts and url.port == self.port:
                route.continue_()
            else:
                self.errors.append("Unexpected request: " + route.request.url)
                route.abort()
        page.route("**/*", guard)
        response = page.goto(
            f"http://{host}:{self.port}/"
            + ("teacher" if role == "teacher" else f"?code={JOIN_CODE}")
        )
        self.navigations.append(
            {
                "role": role,
                "url": page.url,
                "status": response.status if response is not None else None,
            }
        )
        if role == "teacher":
            page.wait_for_function("state.joined")
        else:
            page.locator(f'#lang-options button[data-code="{lang}"]').click()
            page.locator("#join-btn").click()
            expect(page.locator("#caption-screen")).to_be_visible()
        return page

    def close(self):
        for context in self.contexts:
            context.close()


@contextmanager
def live_screens(browser, *, scenario="", lan_ip=None):
    config = make_ws_test_config()
    temporary_cert_dir = None
    temporary_recording_dir = None
    if scenario == "native_http_audio_worklet":
        temporary_cert_dir = TemporaryDirectory(prefix="linguabridge-qa-empty-certs-")
        config.server.cert_dir = temporary_cert_dir.name
        assert not config.server.cert_path().exists()
        assert not config.server.key_path().exists()
        assert not config.server.tls_ready()
    if scenario == "native_local_only_lesson_path":
        temporary_recording_dir = TemporaryDirectory(
            prefix="linguabridge-qa-recording-", dir=ROOT / "docs" / "qa"
        )
        config.recording.out_dir = temporary_recording_dir.name
    assert config.monitoring.stats_interval_s == 2
    gate = threading.Event()
    gate.set()
    if scenario == "native_replay_backpressure_recovers":
        config.limits.mt_replay_watermark = 2
        config.limits.mt_queue_max = 8
        config.mt.cache_size = 0
    if scenario in {"native_silence_warning_lifecycle_and_swap", "native_speaking_indicator_lifecycle", "native_speaking_after_capture_gap"}:
        assert config.monitoring.silence_warning_s == 30
        config.monitoring.silence_warning_s = 3
    if scenario == "native_overload_audio_and_mt_then_clear":
        config.mt.cache_size = 0
    if scenario == "native_partial_on_replaces_final_pause":
        config.partial.enabled = True
        config.partial.interim_silence_ms = 96
        config.partial.min_interim_audio_s = 0.3
    mt = FakeTranslationEngine(gate=gate)
    asr = ControlledASR()
    if scenario == "native_partial_on_replaces_final_pause":
        asr.replies = ["光合成には", "光合成には日光が", "光合成には日光が必要です。", "次の説明の途中", "次の説明です。"]
    lan_scenario = scenario == "native_lan_teacher_url_qr_and_caption"
    if lan_scenario:
        assert lan_ip is not None and not ip_address(lan_ip).is_loopback
    listener = socket.socket()
    bind_host = "0.0.0.0" if lan_scenario else "127.0.0.1"
    listener.bind((bind_host, 0))
    listener.listen(32)
    port = listener.getsockname()[1]
    config.server.http_port = port
    publication = Publication(config.server, requested_ip=lan_ip)
    app = create_app(config, publication, asr_engine=asr, mt_engine=mt, join_code=JOIN_CODE)
    server = uvicorn.Server(uvicorn.Config(app, host=bind_host, port=port, log_level="error", access_log=False))
    thread_errors = []
    def run_server():
        try:
            server.run(sockets=[listener])
        except BaseException:
            thread_errors.append(traceback.format_exc())
    thread = threading.Thread(target=run_server, daemon=True)
    screens = Screens(browser, port, lan_ip=lan_ip)
    screens.mt, screens.gate = mt, gate
    screens.asr = asr
    screens.pipeline = app.state.pipeline
    screens.lesson = app.state.lesson
    screens.settings = {"monitoring": config.monitoring.model_dump(), "limits": config.limits.model_dump(),
                        "history_resend": config.history_resend, "mt_cache_size": config.mt.cache_size, "partial": config.partial.model_dump()}
    if scenario == "native_http_audio_worklet":
        screens.settings["server"] = {
            "certificate_fixture": "temporary-empty-directory",
            "tls_ready": config.server.tls_ready(),
        }
    elif lan_scenario:
        screens.settings["server"] = {
            "bind_host": bind_host,
            "http_port": port,
            "advertised_lan_ip": lan_ip,
        }
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Native test server did not start")
            time.sleep(0.01)
        yield screens
        assert not screens.errors, screens.errors
    finally:
        gate.set()
        asr.gate.set()
        server.should_exit = True
        def stop_server_thread():
            thread.join(timeout=5)
            assert not thread.is_alive(), "Native test server failed to stop"
            assert not thread_errors, "Native test server raised:\n" + "\n".join(thread_errors)
        # ExitStack invokes every cleanup even when an earlier callback fails.
        with ExitStack() as cleanup:
            if temporary_cert_dir is not None:
                cleanup.callback(temporary_cert_dir.cleanup)
            if temporary_recording_dir is not None:
                cleanup.callback(temporary_recording_dir.cleanup)
            cleanup.callback(listener.close)
            cleanup.callback(screens.close)
            cleanup.callback(stop_server_thread)


def select_language(student, lang):
    student.locator("#settings-btn").click()
    expect(student.locator("#settings-sheet")).to_be_visible()
    student.locator("#lang-select").select_option(lang)
    student.locator("#settings-close").click()
    expect(student.locator("#settings-sheet")).to_be_hidden()


def audio(teacher, key=2000, *, close=True, direct=False):
    teacher.evaluate("""({key,frames,direct}) => {
      for (let frame=0; frame<frames; frame++) {
        const pcm = new Int16Array(1600).fill(frame<6 ? key : 0);
        if (direct) state.ws.send(pcm.buffer);
        else state.workletNode.port.onmessage({data:pcm.buffer});
      }
    }""", {"key": key, "frames": 18 if close else 6, "direct": direct})


def audio_frames(teacher, frames):
    teacher.evaluate("frames=>{for(const key of frames){const pcm=new Int16Array(1600).fill(key);state.workletNode.port.onmessage({data:pcm.buffer})}}", frames)


def partial_on(screens):
    teacher, student = started_pair(screens)
    for expected_text in ["光合成には", "光合成には日光が"]:
        audio_frames(teacher, [2000] * 6 + [0] * 2)
        expect(teacher.locator("#partial")).to_have_text(expected_text)
        expect(teacher.locator("#partial")).to_be_visible()
        expect(teacher.locator("#transcript li")).to_have_count(0)
        expect(student.locator("#cards .card")).to_have_count(0)
    audio_frames(teacher, [0] * 12)
    expect(student.locator("#cards .card")).to_have_count(1)
    expect(teacher.locator("#partial")).to_be_hidden()
    expect(teacher.locator("#transcript li")).to_have_text("#1 光合成には日光が必要です。")
    audio_frames(teacher, [2000] * 6 + [0] * 2)
    expect(teacher.locator("#partial")).to_have_text("次の説明の途中")
    teacher.locator("#pause-btn").click()
    expect(teacher.locator("#partial")).to_be_hidden()
    expect(student.locator("#cards .card")).to_have_count(2)
    assert student.evaluate("qaNativeFrames.every(x=>x.message.type!=='turn.partial')")
    partials = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='turn.partial').map(x=>x.message)")
    finals = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='asr_final').map(x=>x.message)")
    assert [p["revision"] for p in partials[:2]] == [1, 2]
    assert partials[0]["turn_id"] == partials[1]["turn_id"] == finals[0]["turn_id"]
    assert partials[2]["turn_id"] == finals[1]["turn_id"] != finals[0]["turn_id"]
    return {"partials": partials, "finals": finals, "student_partials": 0}


def partial_off(screens):
    assert not screens.settings["partial"]["enabled"]
    teacher, student = started_pair(screens)
    audio_frames(teacher, [2000] * 6 + [0] * 2)
    next_stats(teacher)
    expect(teacher.locator("#partial")).to_be_hidden()
    audio_frames(teacher, [0] * 12)
    expect(student.locator("#cards .card")).to_have_count(1)
    assert teacher.evaluate("qaNativeFrames.every(x=>x.message.type!=='turn.partial')")
    return {"default_partial_enabled": False, "student_captions": 1}


def teacher_disconnect_resume(screens):
    teacher, student = started_pair(screens)
    audio(teacher, close=False)
    expect(student.locator("#speaking-indicator")).to_be_visible()
    teacher.evaluate("window.qaHeldTimers=[]; window.setTimeout=(fn,ms)=>{qaHeldTimers.push({fn,ms});return qaHeldTimers.length}; state.ws.close()")
    expect(student.locator("#session-banner")).to_have_text("Paused")
    expect(student.locator("#cards .card")).to_have_count(1)
    expect(student.locator("#speaking-indicator")).to_be_hidden()
    assert teacher.evaluate("state.micReady")
    teacher.evaluate("qaHeldTimers.shift().fn()")
    expect(student.locator("#session-banner")).to_have_text("Live")
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    audio(teacher, 1000)
    expect(student.locator("#cards .card")).to_have_count(2)
    return {"flushed_seq": 1, "resumed_seq": 2, "same_tab_mic_preserved": True}


def speaking_lifecycle(screens):
    assert screens.settings["partial"]["speaking_indicator"]
    assert not screens.settings["partial"]["enabled"]
    teacher, student = started_pair(screens)
    audio(teacher, close=False)
    expect(student.locator("#speaking-indicator")).to_be_visible()
    late = screens.open()
    expect(late.locator("#speaking-indicator")).to_be_visible()
    teacher.locator("#pause-btn").click()
    expect(student.locator("#speaking-indicator")).to_be_hidden()
    expect(late.locator("#speaking-indicator")).to_be_hidden()
    expect(student.locator("#cards .card")).to_have_count(1)
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    audio(teacher, close=False)
    expect(student.locator("#speaking-indicator")).to_be_visible()
    student.evaluate("window.setTimeout=()=>0;state.ws.close()")
    expect(student.locator("#speaking-indicator")).to_be_hidden()
    teacher.locator("#end-btn").click()
    expect(late.locator("#session-banner")).to_have_text("Class has ended")
    expect(late.locator("#speaking-indicator")).to_be_hidden()
    assert late.evaluate("qaNativeFrames.every(x=>x.message.type!=='turn.partial')")
    return {"default_on": True, "late_join_speaking": True, "pause_end_disconnect_clear": True}


def speaking_after_gap(screens):
    teacher, student = started_pair(screens)
    audio(teacher, close=False)
    expect(student.locator("#speaking-indicator")).to_be_visible()
    expect(teacher.locator("#silence-warning")).to_be_visible(timeout=7000)
    expect(student.locator("#speaking-indicator")).to_be_hidden()
    after_warning = {
        "pipeline_speaking": screens.pipeline.speaking,
        "segmenter_open": screens.pipeline._segmenter.is_open,
        "session_state": screens.lesson.state,
        "audio_frames_rejected": screens.pipeline.audio_frames_rejected,
    }
    # A capture gap means no PCM frames, not zero-valued PCM. Restarting
    # capture must make renewed speech visible even before its final caption.
    before = teacher.evaluate("qaNativeSent.filter(x=>x.binary).length")
    audio(teacher, close=False)
    assert teacher.evaluate("qaNativeSent.filter(x=>x.binary).length") == before + 6
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    teacher.wait_for_timeout(300)
    after_reinput = {
        "pipeline_speaking": screens.pipeline.speaking,
        "segmenter_open": screens.pipeline._segmenter.is_open,
        "session_state": screens.lesson.state,
        "audio_frames_rejected": screens.pipeline.audio_frames_rejected,
        "speaking_frames": student.evaluate("qaNativeFrames.filter(x=>x.message.type==='speaking').map(x=>x.message.on)"),
    }
    assert student.locator("#speaking-indicator").is_visible(), (
        f"入力途絶後の発話再開が表示されない: after_warning={after_warning}, "
        f"after_reinput={after_reinput}"
    )
    return {"clears_on_long_gap": True, "renewed_speech_visible": True}


def started_pair(screens):
    teacher = screens.open("teacher")
    expect(teacher.locator("#pause-btn")).to_be_disabled()
    student = screens.open()
    teacher.locator("#start-btn").click()
    expect(student.locator("#session-banner")).to_have_text("Live")
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    return teacher, student


def delivery(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .text")).to_have_text("[en] 光合成には日光が必要です。")
    expect(teacher.locator("#transcript li")).to_have_text("#1 光合成には日光が必要です。")


def mic_denied_retry_delivery(screens):
    teacher = screens.open("teacher")
    student = screens.open()

    # MEDIA_ONLY keeps real Edge + loopback HTTP/WS while making the browser's
    # permission outcome deterministic. The first click must not start the
    # server session when getUserMedia rejects.
    teacher.evaluate("qa.failMic=true")
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#mic-status")).to_contain_text(
        "マイクを取得できませんでした: denied"
    )
    expect(teacher.locator("#session-state")).to_have_text("未開始")
    expect(teacher.locator("#start-btn")).to_be_enabled()
    assert teacher.evaluate("state.sessionState") == "idle"
    assert not teacher.evaluate("state.micReady")
    assert teacher.evaluate("qa.micCalls.length") == 1
    assert screens.lesson.state == "idle"

    # Switch the simulated permission in the same tab, retry, then prove the
    # resulting PCM traverses the real WebSocket/server path to a student.
    teacher.evaluate("qa.failMic=false")
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    expect(student.locator("#session-banner")).to_have_text("Live")
    assert teacher.evaluate("state.micReady")
    assert teacher.evaluate("qa.micCalls.length") == 2
    audio(teacher)
    expect(student.locator("#cards .text")).to_have_text(
        "[en] 光合成には日光が必要です。"
    )
    expect(teacher.locator("#transcript li")).to_have_text(
        "#1 光合成には日光が必要です。"
    )
    return {
        "first_attempt_state": "idle",
        "same_tab_retry_state": "live",
        "get_user_media_calls": 2,
        "student_caption_seq": 1,
    }


def http_native_media(screens):
    # Chromium's synthetic capture device replaces hardware only. Production
    # getUserMedia, MediaStream, AudioContext and AudioWorklet all run natively.
    teacher = screens.open("teacher", native_media=True)
    assert teacher.evaluate("window.isSecureContext")
    assert teacher.url.startswith("http://127.0.0.1:")
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    teacher.wait_for_function("qaNativeSent.filter(x=>x.binary).length>=10", timeout=10000)
    observed = teacher.evaluate("""({
      secure:window.isSecureContext, micReady:state.micReady,
      audioState:state.audioCtx.state, sampleRate:state.audioCtx.sampleRate,
      tracks:state.micStream.getAudioTracks().map(t=>({state:t.readyState,label:t.label})),
      frameSizes:[...new Set(qaNativeSent.filter(x=>x.binary).map(x=>x.bytes))],
      binary_frame_count:qaNativeSent.filter(x=>x.binary).length
    })""")
    url = urlsplit(teacher.url)
    observed.update({
        "url_scheme": url.scheme,
        "url_host": url.hostname,
        "url_path": url.path,
        "tls_ready": screens.settings["server"]["tls_ready"],
        "certificate_fixture": screens.settings["server"]["certificate_fixture"],
    })
    assert observed["micReady"] and observed["audioState"] == "running"
    assert observed["tracks"] and all(t["state"] == "live" for t in observed["tracks"])
    assert all(t["label"].startswith("Fake ") for t in observed["tracks"]), observed["tracks"]
    assert observed["frameSizes"] == [3200]
    assert observed["binary_frame_count"] >= 10
    assert observed["url_scheme"] == "http"
    assert observed["url_host"] == "127.0.0.1"
    assert observed["url_path"] == "/teacher"
    assert observed["tls_ready"] is False
    assert observed["certificate_fixture"] == "temporary-empty-directory"
    teacher.locator("#end-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("終了")
    return observed


def teacher_replacement(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    replacement = screens.open("teacher")
    expect(teacher.locator("#page-error")).to_contain_text("別のタブ")
    assert teacher.evaluate("qaNativeClosed") == [4000]
    expect(replacement.locator("#session-state")).to_have_text("配信中")
    expect(student.locator("#session-banner")).to_have_text("Live")
    # Two real stats periods also establish that the displaced tab does not
    # enter a reconnect fight. The new tab must have a usable capture path.
    next_stats(replacement)
    next_stats(replacement)
    assert teacher.evaluate("state.joined") is False
    observed = replacement.evaluate("({micReady:state.micReady,worklet:!!state.workletNode,startDisabled:el.startBtn.disabled})")
    assert observed["micReady"] and observed["worklet"], f"配信中表示だが新先生の音声経路が未準備: {observed}"
    audio(replacement, 1000)
    expect(student.locator("#cards .card")).to_have_count(2)
    return observed


def language_switch(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    first = student.locator("#cards .text").first.inner_text()
    select_language(student, "zh")
    # Receiving the updated stats is a server-side barrier for set_lang.
    expect(teacher.locator("#stat-langs")).to_have_text("zh: 1")
    audio(teacher, 1000)
    expect(student.locator("#cards .card")).to_have_count(2)
    expect(student.locator("#cards .text").first).to_have_text(first)
    expect(student.locator("#cards .text").last).to_contain_text("[zh]")
    student.evaluate("el.langSelect.add(new Option('Unsupported','fr'))")
    select_language(student, "fr")
    expect(student.locator("#lang-select")).to_have_value("zh")
    expect(student.locator("html")).to_have_attribute("lang", "zh")


def language_switch_during_replay(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    first = student.locator("#cards .text").first.inner_text()
    student.evaluate("window.qaHeldTimers=[]; window.setTimeout=(fn,ms)=>{qaHeldTimers.push({fn,ms});return qaHeldTimers.length}; state.ws.close()")
    expect(student.locator("#session-banner")).to_have_text("Reconnecting…")
    audio(teacher, 1000)
    expect(teacher.locator("#transcript li")).to_have_count(2)
    screens.gate.clear()
    student.evaluate("qaHeldTimers.shift().fn()")
    expect(student.locator("#session-banner")).to_have_text("Live")
    # Real server has joined this student in English; hold inference so that
    # changes are guaranteed to precede the pending replay's completion.
    for lang in ["zh", "en", "zh"]:
        select_language(student, lang)
    expect(teacher.locator("#stat-langs")).to_have_text("zh: 1")
    screens.gate.set()
    expect(student.locator("#cards .card")).to_have_count(2)
    expect(student.locator("#cards .text").first).to_have_text(first)
    expect(student.locator("#cards .text").last).to_have_text("[zh] おはようございます。")
    audio(teacher, 3000)
    expect(student.locator("#cards .card")).to_have_count(3)
    expect(student.locator("#cards .text").last).to_contain_text("[zh]")
    assert student.evaluate("state.lastSeq") == 3
    assert student.evaluate("qaNativeFrames.filter(x=>x.message.type==='caption' && x.message.seq>1).every(x=>x.message.lang==='zh')")
    return {"last_seq": 3, "restored_and_live_language": "zh", "existing_card_unchanged": True}


def transcript_follow(screens):
    teacher, student = started_pair(screens)
    for seq in range(1, 53):
        audio(teacher, 1000)
        expect(student.locator("#cards .card")).to_have_count(seq)
    expect(teacher.locator("#transcript li")).to_have_count(50)
    expect(teacher.locator("#transcript li").first).to_have_text("#3 おはようございます。")
    expect(teacher.locator("#transcript li").last).to_have_text("#52 おはようございます。")
    scroll = teacher.locator("#transcript").evaluate("e=>({top:e.scrollTop,height:e.scrollHeight,viewport:e.clientHeight})")
    assert scroll["height"] > scroll["viewport"]
    assert abs(scroll["height"] - scroll["top"] - scroll["viewport"]) <= 1
    return {"retained_seq": [3, 52], "scroll": scroll}


def silence_lifecycle(screens):
    teacher = screens.open("teacher")
    student = screens.open()
    def two_periods():
        next_stats(teacher)
        next_stats(teacher)
    two_periods()
    assert teacher.evaluate("qaNativeFrames.every(x=>x.message.code!=='mic_silent')")
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    expect(teacher.locator("#silence-warning")).to_be_visible(timeout=7000)
    expect(teacher.locator("#silence-warning")).to_contain_text("3秒以上")
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    expect(teacher.locator("#silence-warning")).to_be_hidden()
    expect(teacher.locator("#silence-warning")).to_be_visible(timeout=7000)
    teacher.locator("#pause-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("一時停止中")
    expect(teacher.locator("#silence-warning")).to_be_hidden()
    two_periods()
    expect(teacher.locator("#silence-warning")).to_be_hidden()
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#silence-warning")).to_be_visible(timeout=7000)
    count = teacher.evaluate("qaNativeFrames.filter(x=>x.message.code==='mic_silent').length")
    assert count == 3
    replacement = screens.open("teacher")
    expect(teacher.locator("#page-error")).to_contain_text("別のタブ")
    expect(replacement.locator("#silence-warning")).to_be_visible(timeout=7000)
    return {"warning_count": count, "shipped_threshold_s": 30, "test_threshold_s": 3, "replacement_warned": True}


def assert_stats_rendered(teacher, snapshot):
    fields = {"delay": "median_delay_ms", "asr-ms": "median_asr_ms", "mt-queue": "mt_queue_depth", "mt-ms": "median_mt_ms"}
    for element, field in fields.items():
        expect(teacher.locator("#stat-" + element)).to_have_text(str(snapshot[field]))
    for element, field in {"asr-wait": "asr_wait_seconds", "asr-active": "asr_active_seconds"}.items():
        expect(teacher.locator("#stat-" + element)).to_have_text(f"{snapshot[field]:.1f}")


def latency_breakdown(screens):
    teacher, student = started_pair(screens)
    screens.asr.gate.clear()
    screens.gate.clear()
    for key in [1000, 2000, 3000]:
        audio(teacher, key)
    teacher.wait_for_function("qaNativeFrames.some(x=>x.message.type==='stats' && x.message.asr_wait_seconds>0 && x.message.asr_active_seconds>0)", timeout=5000)
    during_asr = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='stats').at(-1).message")
    assert_stats_rendered(teacher, during_asr)
    screens.asr.gate.set()
    expect(teacher.locator("#transcript li")).to_have_count(3)
    teacher.wait_for_function("qaNativeFrames.some(x=>x.message.type==='stats' && x.message.mt_queue_depth===2 && x.message.asr_active_seconds===0)", timeout=5000)
    during_mt = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='stats').at(-1).message")
    assert during_mt["asr_wait_seconds"] == 0
    assert_stats_rendered(teacher, during_mt)
    screens.gate.set()
    expect(student.locator("#cards .card")).to_have_count(3)
    drained = next_stats(teacher)
    assert drained["mt_queue_depth"] == 0 and drained["asr_active_seconds"] == 0 and drained["asr_wait_seconds"] == 0
    assert drained["median_delay_ms"] > 0
    assert_stats_rendered(teacher, drained)
    return {"asr_waiting_and_active": during_asr, "mt_waiting": during_mt, "drained": drained}


def cache_monitor(screens):
    teacher, student = started_pair(screens)
    screens.gate.clear()
    audio(teacher, 1000)
    expect(teacher.locator("#transcript li")).to_have_count(1)
    next_stats(teacher)  # Deliberately nonzero first inference duration.
    screens.gate.set()
    expect(student.locator("#cards .card")).to_have_count(1)
    first = next_stats(teacher)
    assert first["median_mt_ms"] > 0
    for seq in range(2, 5):
        audio(teacher, 1000)
        expect(student.locator("#cards .card")).to_have_count(seq)
    cached = next_stats(teacher)
    assert cached["mt_cache_hits"] == 3
    assert cached["mt_cache_hit_rate"] == 0.75
    assert cached["median_mt_ms"] == first["median_mt_ms"]
    assert len(screens.mt.calls) == 1
    expect(teacher.locator("#stat-cache")).to_have_text("75% (3件節約)")
    assert_stats_rendered(teacher, cached)
    return {"first": first, "after_three_hits": cached, "actual_mt_calls": 1}


def overload_monitor(screens):
    teacher, student = started_pair(screens)
    assert screens.settings["monitoring"]["overload_audio_seconds"] == 10
    assert screens.settings["monitoring"]["overload_queue_depth"] == 3
    screens.asr.gate.clear()
    for _ in range(12):
        audio(teacher, 1000)
    teacher.wait_for_function("qaNativeFrames.some(x=>x.message.type==='stats' && x.message.audio_queue_seconds>=10)", timeout=5000)
    asr_backlog = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='stats').at(-1).message")
    assert asr_backlog["mt_queue_depth"] == 0 and asr_backlog["overloaded"]
    expect(teacher.locator("#overload-warning")).to_be_visible()
    screens.asr.gate.set()
    expect(student.locator("#cards .card")).to_have_count(12)
    assert not next_stats(teacher)["overloaded"]
    expect(teacher.locator("#overload-warning")).to_be_hidden()
    screens.gate.clear()
    for _ in range(3):
        audio(teacher, 2000)
    expect(teacher.locator("#transcript li")).to_have_count(15)
    below = next_stats(teacher)
    assert below["mt_queue_depth"] == 2 and not below["overloaded"]
    expect(teacher.locator("#overload-warning")).to_be_hidden()
    audio(teacher, 2000)
    expect(teacher.locator("#transcript li")).to_have_count(16)
    boundary = next_stats(teacher)
    assert boundary["mt_queue_depth"] == 3 and boundary["overloaded"]
    assert boundary["audio_queue_seconds"] == 0
    expect(teacher.locator("#overload-warning")).to_be_visible()
    screens.gate.set()
    expect(student.locator("#cards .card")).to_have_count(16)
    assert not next_stats(teacher)["overloaded"]
    expect(teacher.locator("#overload-warning")).to_be_hidden()
    return {"audio_backlog": asr_backlog, "mt_below_boundary": below, "mt_at_boundary": boundary}


def stats(screens):
    teacher = screens.open("teacher")
    screens.open(lang="en")
    second = screens.open(lang="en")
    third = screens.open(lang="zh")
    expect(teacher.locator("#stat-students")).to_have_text("3")
    expect(teacher.locator("#stat-langs")).to_have_text("en: 2 / zh: 1")
    select_language(second, "zh")
    expect(teacher.locator("#stat-langs")).to_have_text("en: 1 / zh: 2")
    third.close()
    expect(teacher.locator("#stat-students")).to_have_text("2")
    expect(teacher.locator("#stat-langs")).to_have_text("en: 1 / zh: 1")
    intervals = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='stats').map(x=>x.at)")
    assert len(intervals) >= 3
    assert all(1700 <= b-a <= 2500 for a,b in zip(intervals, intervals[1:])), intervals


def recording(screens):
    teacher, student = started_pair(screens)
    expect(student.locator("#record-indicator")).to_be_hidden()
    teacher.locator("#record-toggle").check()
    expect(teacher.locator("#record-indicator")).to_be_visible()
    expect(student.locator("#record-indicator")).to_be_visible()
    late = screens.open(lang="zh")
    expect(late.locator("#record-indicator")).to_be_visible()
    teacher.locator("#record-toggle").uncheck()
    for page in [teacher, student, late]:
        expect(page.locator("#record-indicator")).to_be_hidden()


def next_stats(teacher):
    count = teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='stats').length")
    teacher.wait_for_function("n => qaNativeFrames.filter(x=>x.message.type==='stats').length > n", arg=count, timeout=5000)
    return teacher.evaluate("qaNativeFrames.filter(x=>x.message.type==='stats').at(-1).message")


def rejected_audio(teacher, student, captions):
    before = teacher.evaluate("qaNativeSent.filter(x=>x.binary).length")
    audio(teacher, 1000)
    assert teacher.evaluate("qaNativeSent.filter(x=>x.binary).length") == before, "非liveでもブラウザが音声を送った"
    # Also bypass the client gate: the server must independently discard it.
    audio(teacher, 1000, direct=True)
    stats = next_stats(teacher)
    assert stats["asr_calls"] == captions, stats
    expect(student.locator("#cards .card")).to_have_count(captions)
    return stats["asr_calls"]


def pause_resume(screens):
    teacher, student = started_pair(screens)
    audio(teacher, close=False)
    teacher.locator("#pause-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("一時停止中")
    expect(student.locator("#session-banner")).to_have_text("Paused")
    expect(student.locator("#cards .text")).to_have_text("[en] 光合成には日光が必要です。")
    expect(teacher.locator("#pause-btn")).to_be_disabled()
    expect(teacher.locator("#start-btn")).to_have_text("再開")
    rejected_audio(teacher, student, 1)
    teacher.locator("#start-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    expect(student.locator("#session-banner")).to_have_text("Live")
    audio(teacher, 1000)
    expect(student.locator("#cards .card")).to_have_count(2)
    expect(student.locator("#cards .text").last).to_have_text("[en] おはようございます。")
    return {"captions_after_resume": 2, "asr_calls_while_paused": 1}


def end_session(screens, *, from_paused=False):
    teacher, student = started_pair(screens)
    audio(teacher, close=False)
    if from_paused:
        teacher.locator("#pause-btn").click()
        expect(teacher.locator("#session-state")).to_have_text("一時停止中")
        expect(student.locator("#cards .card")).to_have_count(1)
    teacher.locator("#end-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("終了")
    expect(student.locator("#session-banner")).to_have_text("Class has ended")
    expect(student.locator("#cards .text")).to_have_text("[en] 光合成には日光が必要です。")
    expect(teacher.locator("#pause-btn")).to_be_disabled()
    expect(teacher.locator("#end-btn")).to_be_disabled()
    rejected_audio(teacher, student, 1)
    return {"ended_from": "paused" if from_paused else "live", "asr_calls_after_end": 1}


def reconnect_history(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    # Keep the browser's real retry loop; only hold its next timeout so the
    # outage deterministically contains an utterance, then release that retry.
    student.evaluate("window.qaHeldTimers=[]; window.setTimeout=(fn,ms)=>{qaHeldTimers.push({fn,ms});return qaHeldTimers.length}; state.ws.close()")
    expect(student.locator("#session-banner")).to_have_text("Reconnecting…")
    audio(teacher, 1000)
    expect(teacher.locator("#transcript li")).to_have_count(2)
    student.evaluate("qaHeldTimers.shift().fn()")
    expect(student.locator("#session-banner")).to_have_text("Live")
    expect(student.locator("#cards .card")).to_have_count(2)
    assert student.locator("#cards .card").evaluate_all("xs=>xs.map(x=>+x.dataset.seq)") == [1,2]
    assert student.evaluate("state.lastSeq") == 2
    expect(student.locator("#cards .text").last).to_have_text("[en] おはようございます。")
    return {"restored_seq": 2, "last_seq": 2}


def local_only_lesson_path(screens):
    """参加・配信・記録・再接続を通し、Edge の全通信先をloopbackへ限定する。"""

    teacher, student = started_pair(screens)
    teacher.locator("#record-toggle").check()
    expect(student.locator("#record-indicator")).to_be_visible()
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)

    student.evaluate(
        "window.qaHeldTimers=[]; "
        "window.setTimeout=(fn,ms)=>{qaHeldTimers.push({fn,ms});return qaHeldTimers.length}; "
        "state.ws.close()"
    )
    expect(student.locator("#session-banner")).to_have_text("Reconnecting…")
    audio(teacher, 1000)
    expect(teacher.locator("#transcript li")).to_have_count(2)
    student.evaluate("qaHeldTimers.shift().fn()")
    expect(student.locator("#session-banner")).to_have_text("Live")
    expect(student.locator("#cards .card")).to_have_count(2)
    assert student.evaluate("state.lastSeq") == 2

    teacher.locator("#record-toggle").uncheck()
    expect(student.locator("#record-indicator")).to_be_hidden()
    teacher.locator("#end-btn").click()
    expect(student.locator("#session-banner")).to_have_text("Class has ended")

    socket_urls = [
        url
        for page in (teacher, student)
        for url in page.evaluate("qaNativeSocketUrls")
    ]
    assert socket_urls
    for raw_url in socket_urls:
        url = urlsplit(raw_url)
        assert url.scheme == "ws"
        assert url.hostname == "127.0.0.1"
        assert url.port == screens.port
        assert url.path == "/ws"
    return {
        "journey": ["join", "caption", "record", "disconnect", "replay", "end"],
        "restored_seq": 2,
        "websocket_urls": socket_urls,
        "http_resource_guard": "127.0.0.1 and scenario port only",
    }


def lan_teacher_url_qr_and_caption(screens):
    """実NICを広告する参加URLを、実HTTP・Edge・QR復号・参加まで一続きで確認する。"""

    assert screens.lan_ip is not None
    assert not ip_address(screens.lan_ip).is_loopback

    teacher = screens.open("teacher")
    teacher_navigation = screens.navigations[-1]
    assert teacher_navigation["status"] == 200
    assert urlsplit(teacher_navigation["url"]).hostname == "127.0.0.1"

    api = teacher.evaluate(
        """async () => {
          const response = await fetch('/api/teacher-info', {cache: 'no-store'});
          return {status: response.status, body: await response.json()};
        }"""
    )
    assert api["status"] == 200, api
    info = api["body"]
    assert info["code"] == JOIN_CODE
    assert len(info["code"]) == 4 and info["code"].isdigit()

    join_url = info["join_url"]
    parsed_join = urlsplit(join_url)
    assert parsed_join.scheme == "http"
    assert parsed_join.hostname == screens.lan_ip == resolve_ip(screens.lan_ip)
    assert not ip_address(parsed_join.hostname).is_loopback
    assert parsed_join.port == screens.port
    assert parsed_join.path == "/"
    assert parse_qs(parsed_join.query) == {"code": [JOIN_CODE]}

    expect(teacher.locator("#join-code")).to_have_text(info["code"])
    expect(teacher.locator("#join-url")).to_have_text(join_url)
    expect(teacher.locator("#qr")).to_have_attribute("title", join_url)
    expect(teacher.locator("#qr canvas")).to_have_count(1)
    decoded = decode_qr_with_local_opencv(teacher.screenshot(full_page=True))
    assert decoded["detected"], decoded
    assert decoded["value"] == join_url, decoded

    student = screens.open(host=parsed_join.hostname)
    student_navigation = screens.navigations[-1]
    assert student_navigation["status"] == 200
    assert student.url == join_url
    expect(student.locator("#code-input")).to_have_value(JOIN_CODE)

    student_ws_urls = student.evaluate("qaNativeSocketUrls")
    assert len(student_ws_urls) == 1, student_ws_urls
    student_ws = urlsplit(student_ws_urls[0])
    assert student_ws.scheme == "ws"
    assert student_ws.hostname == parsed_join.hostname
    assert student_ws.port == parsed_join.port
    assert student_ws.path == "/ws"

    teacher.locator("#start-btn").click()
    expect(teacher.locator("#session-state")).to_have_text("配信中")
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    assert student.locator("#cards .card").get_attribute("data-seq") == "1"
    expect(teacher.locator("#transcript li")).to_have_count(1)

    return {
        "advertised_lan_ip": screens.lan_ip,
        "listener_port": screens.port,
        "teacher_page_url": teacher.url,
        "teacher_info_join_url": join_url,
        "displayed_join_url": teacher.locator("#join-url").inner_text(),
        "decoded_join_url": decoded["value"],
        "decoder": decoded["decoder"],
        "student_page_url": student.url,
        "student_navigation_status": student_navigation["status"],
        "student_websocket_url": student_ws_urls[0],
        "student_caption_seq": 1,
    }


def replay_backpressure(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    student.evaluate("window.qaHeldTimers=[]; window.setTimeout=(fn,ms)=>{qaHeldTimers.push({fn,ms});return qaHeldTimers.length}; state.ws.close()")
    expect(student.locator("#session-banner")).to_have_text("Reconnecting…")
    for index, key in enumerate([1000, 3000, 5000], 2):
        audio(teacher, key)
        expect(teacher.locator("#transcript li")).to_have_count(index)
    screens.gate.clear()
    student.evaluate("qaHeldTimers.shift().fn()")
    student.wait_for_function("qaNativeClosed.includes(1013)", timeout=5000)
    student.wait_for_function("qaHeldTimers.length > 0", timeout=5000)
    # The first replay is now executing (gated), with another waiting behind it.
    teacher.wait_for_function("qaNativeFrames.some(x=>x.message.type==='stats' && x.message.mt_queue_depth===1)", timeout=5000)
    observer = screens.open(lang="zh")
    audio(teacher, 6000)
    expect(teacher.locator("#transcript li")).to_have_count(5)
    screens.gate.set()
    expect(observer.locator("#cards .card")).to_have_count(1)
    assert observer.locator("#cards .card").get_attribute("data-seq") == "5"
    # The already running replay cannot be preempted. The queued live job must
    # run before the queued second replay, which targets the disconnected user.
    assert [lang for _, lang in screens.mt.calls[:3]] == ["en", "en", "zh"], screens.mt.calls
    for _ in range(5):
        # Wait for a new empty-queue snapshot between retries. This excludes
        # test-induced starvation from releasing held timers too quickly.
        assert next_stats(teacher)["mt_queue_depth"] == 0
        student.evaluate("qaHeldTimers.shift().fn()")
        student.wait_for_function("state.lastSeq===5 || qaHeldTimers.length>0", timeout=5000)
        if student.evaluate("state.lastSeq") == 5:
            break
    expect(student.locator("#session-banner")).to_have_text("Live")
    assert student.locator("#cards .card").evaluate_all("xs=>xs.map(x=>+x.dataset.seq)") == [1,2,3,4,5]
    assert student.evaluate("state.lastSeq") == 5
    return {"close_codes": student.evaluate("qaNativeClosed"), "last_seq": 5,
            "translation_order": screens.mt.calls}


def cached_large_gap(screens):
    teacher, student = started_pair(screens)
    audio(teacher)
    expect(student.locator("#cards .card")).to_have_count(1)
    observer = screens.open(lang="en")
    student.evaluate("window.qaHeldTimers=[]; window.setTimeout=(fn,ms)=>{qaHeldTimers.push({fn,ms});return qaHeldTimers.length}; state.ws.close()")
    expect(student.locator("#session-banner")).to_have_text("Reconnecting…")
    for seq in range(2, 37):
        audio(teacher, 1000)
        # The observer receiving each one proves every missing caption is
        # already translated. No MT inference load remains to explain failure.
        expect(observer.locator("#cards .card")).to_have_count(seq - 1)
    for _ in range(3):
        assert next_stats(teacher)["mt_queue_depth"] == 0
        student.evaluate("qaHeldTimers.shift().fn()")
        student.wait_for_function("state.lastSeq===36 || qaHeldTimers.length>0", timeout=5000)
        if student.evaluate("state.lastSeq") == 36:
            break
    actual = student.evaluate("({last_seq:state.lastSeq,codes:qaNativeClosed,cards:el.cards.children.length})")
    assert actual["last_seq"] == 36, f"翻訳済み35件・MT待ち0でも復元が進まない: {actual}"
    assert actual["cards"] == 36
    return actual


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
        *_evidence_tree_files(ROOT / "server", suffix=".py"),
        *_evidence_tree_files(ROOT / "web"),
        Path(__file__),
        ROOT / "scripts/qa_browser.py",
        ROOT / "scripts/qa_evidence_manifest.py",
        ROOT / "scripts/story_tracker.py",
        ROOT / "tests/conftest.py",
        ROOT / "docs/qa/stories.tsv",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story", action="append")
    parser.add_argument("--scenario", action="append")
    args = parser.parse_args()
    if not _import_code_is_current():
        print(
            "Imported native browser runner code differs from current disk state; rerun.",
            file=sys.stderr,
            flush=True,
        )
        return 2
    out = ROOT / "docs/qa/runs" / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-native-browser")
    try:
        validate_output_parent(ROOT, out)
    except EvidenceManifestError as exc:
        print(f"Evidence output rejected: {exc}", file=sys.stderr, flush=True)
        return 2
    out.mkdir(parents=True, exist_ok=False)
    results = []
    environment_skips = []
    try:
        source_hashes_at_start = snapshot_supplied_source_hashes(
            ROOT, _evidence_sources
        )
    except (OSError, EvidenceManifestError):
        payload = {
            "finished_at": datetime.now().astimezone().isoformat(),
            "browser": None,
            "results": results,
            "environment_skips": environment_skips,
            "source_snapshot_error": traceback.format_exc(),
        }
        commit_browser_result(
            out, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )
        print(f"Evidence: {out}; source snapshot failed", flush=True)
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", headless=True,
                                     args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
        version = browser.version
        cases = [("TEACH-01", "native_audio_to_caption", delivery),
                 ("TEACH-02", "native_mic_denied_same_tab_retry_to_caption", mic_denied_retry_delivery),
                 ("STU-06", "native_language_switch_and_rejection", language_switch), ("MON-04", "native_join_change_leave_stats", stats), ("REC-02", "native_recording_all_screens", recording),
                 ("TEACH-03", "native_pause_flush_gate_resume", pause_resume),
                 ("TEACH-04", "native_end_live_flush_and_gate", end_session),
                 ("TEACH-04", "native_end_paused_and_gate", lambda screens: end_session(screens, from_paused=True)),
                 ("CONN-02", "native_reconnect_missing_translation", reconnect_history),
                 ("CONN-04", "native_replay_backpressure_recovers", replay_backpressure),
                 ("CONN-04", "native_cached_large_gap_recovers", cached_large_gap),
                 ("CONN-09", "native_teacher_replacement_audio_continues", teacher_replacement),
                 ("STU-06", "native_language_switch_during_replay", language_switch_during_replay),
                 ("MON-01", "native_transcript_last_50_and_follow", transcript_follow),
                 ("MON-07", "native_silence_warning_lifecycle_and_swap", silence_lifecycle),
                 ("MON-05", "native_latency_breakdown_wait_active_drain", latency_breakdown),
                 ("MON-06", "native_cache_stats_keep_real_inference_median", cache_monitor),
                 ("MON-08", "native_overload_audio_and_mt_then_clear", overload_monitor),
                 ("MON-02", "native_partial_on_replaces_final_pause", partial_on),
                 ("MON-02", "native_partial_off_is_default", partial_off),
                 ("CONN-06", "native_teacher_disconnect_flush_and_resume", teacher_disconnect_resume),
                 ("STU-10", "native_speaking_indicator_lifecycle", speaking_lifecycle),
                 ("STU-10", "native_speaking_after_capture_gap", speaking_after_gap),
                  ("SAFE-08", "native_local_only_lesson_path", local_only_lesson_path),
                  ("OPS-09", "native_http_audio_worklet", http_native_media),
                  ("OPS-10", "native_lan_teacher_url_qr_and_caption", lan_teacher_url_qr_and_caption)]
        for story, name, fn in cases:
            if args.story and story not in args.story:
                continue
            if args.scenario and name not in args.scenario:
                continue
            lan_ip = None
            if name == "native_lan_teacher_url_qr_and_caption":
                lan_ip = resolve_ip()
                if ip_address(lan_ip).is_loopback:
                    environment_skips.append(
                        {
                            "story": story,
                            "scenario": name,
                            "reason": "non-loopback LAN IPv4 address is unavailable",
                            "observed_lan_ip": lan_ip,
                        }
                    )
                    print(story, name, "environment-skip", flush=True)
                    continue
            result = {"story": story, "scenario": name, "complete": False, "scope": "実HTTP/WS・実Edge。energy VAD・fake ASR/MT・模擬マイク。学校LAN/実機音声は未検証"}
            if name == "native_http_audio_worklet":
                result["scope"] = "証明書なしループバックHTTP・実Edge音声API/Worklet/WS。Chromium合成入力・fake ASR/MT。物理マイクと起動CLIの案内は未検証"
            elif name == "native_local_only_lesson_path":
                result["scope"] = "実Edge・実loopback HTTP/WSで参加→配信→記録→切断復元。HTTP資源とWS接続先を127.0.0.1の動的ポートへ制限。実モデル通信ガードはpytest証跡と合成"
            if name == "native_lan_teacher_url_qr_and_caption":
                result["scope"] = (
                    "現在の実NIC自己アドレス・実uvicorn HTTP/WS・実Edge・独立OpenCV QR復号。"
                    "fake ASR/MT・模擬マイク。別端末到達、実DHCP変更、スマホカメラ読取は未検証"
                )
            try:
                with live_screens(browser, scenario=name, lan_ip=lan_ip) as screens:
                    result["test_configuration"] = screens.settings
                    try:
                        result["observations"] = fn(screens)
                    except Exception:
                        result["frames"] = [page.evaluate("qaNativeFrames") for page in screens.pages if not page.is_closed()]
                        for index, page in enumerate(screens.pages):
                            if not page.is_closed():
                                page.screenshot(path=str(out / f"{story}-{index}.png"), full_page=True)
                        raise
                result["status"] = "pass"
            except Exception:
                result.update(status="fail", detail=traceback.format_exc())
            results.append(result)
            print(story, name, result["status"], flush=True)
        browser_close_error = None
        try:
            browser.close()
        except Exception:
            browser_close_error = traceback.format_exc()
    payload = {
        "finished_at": datetime.now().astimezone().isoformat(),
        "browser": version,
        "results": results,
        "environment_skips": environment_skips,
    }
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
            kind="native-browser",
            source_files=_evidence_sources,
            source_hashes_at_start=source_hashes_at_start,
            browser_bytes=browser_bytes,
        )
    print(f"Evidence: {out}")
    if browser_close_error or not import_code_current or not sealed:
        return 2
    if any(r["status"] != "pass" for r in results):
        return 1
    if environment_skips or not results:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
