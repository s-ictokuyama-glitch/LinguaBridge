"""Observe teacher capture recovery with native Edge media and loopback captions."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

def snapshot(teacher, students):
    observed = teacher.evaluate("""({
      binary:qaNativeSent.filter(x=>x.binary).length,
      audio:state.audioCtx.state,
      tracks:state.micStream.getAudioTracks().map(t=>t.readyState),
      micReady:state.micReady, lesson:state.sessionState,
      pcm:window.qaCaptureProbe
    })""")
    observed["captions"] = {lang: student.locator("#cards .text").count()
                            for lang, student in students.items()}
    return observed


def probe(browser, fault):
    from playwright.sync_api import expect
    from scripts.qa_native_browser import live_screens

    with live_screens(browser) as screens:
        teacher = screens.open("teacher", native_media=True)
        students = {lang: screens.open("student", lang=lang) for lang in ("en", "zh")}
        teacher.locator("#start-btn").click()
        expect(teacher.locator("#session-state")).to_have_text("配信中")
        teacher.wait_for_function("qaNativeSent.filter(x=>x.binary).length >= 15", timeout=10000)
        teacher.locator("#pause-btn").click()
        expect(teacher.locator("#session-state")).to_have_text("一時停止中")
        for student in students.values():
            expect(student.locator("#cards .text")).to_have_count(1)
        teacher.evaluate("""() => {
          window.qaCaptureProbe = {frames:0, nonzero_frames:0, peak:0};
          const original = WebSocket.prototype.send;
          WebSocket.prototype.send = function(data) {
            original.call(this, data);
            if (typeof data !== "string" && state.sessionState === "live") {
              const pcm = new Int16Array(data);
              const peak = pcm.reduce((max, value) => Math.max(max, Math.abs(value)), 0);
              qaCaptureProbe.frames += 1;
              if (peak > 0) qaCaptureProbe.nonzero_frames += 1;
              qaCaptureProbe.peak = Math.max(qaCaptureProbe.peak, peak);
            }
          };
        }""")
        if fault == "suspend":
            teacher.evaluate("state.audioCtx.suspend()")
        elif fault == "ended":
            teacher.evaluate("state.micStream.getAudioTracks().forEach(t=>t.stop())")
        teacher.wait_for_timeout(300)
        before = snapshot(teacher, students)
        teacher.locator("#start-btn").click()
        expect(teacher.locator("#session-state")).to_have_text("配信中")
        teacher.wait_for_timeout(1500)
        resumed = snapshot(teacher, students)
        teacher.locator("#pause-btn").click()
        expect(teacher.locator("#session-state")).to_have_text("一時停止中")
        teacher.wait_for_timeout(1000)
        after = snapshot(teacher, students)
        new_captions = {lang: after["captions"][lang] - before["captions"][lang] for lang in students}
        healthy = (resumed["audio"] == "running" and resumed["tracks"]
                   and all(track == "live" for track in resumed["tracks"])
                   and resumed["pcm"]["nonzero_frames"] > 0
                   and all(count >= 1 for count in new_captions.values()))
        return dict(status="PASS" if healthy else "FAIL", fault=fault,
                    property="capture_liveness", trace=["start", "pause", fault, "start", "audio", "pause"],
                    resume_observation_ms=1500, drain_observation_ms=1000,
                    before=before, resumed=resumed, after_final_pause=after, new_captions=new_captions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    report = dict(cases=[])
    try:
        from playwright.sync_api import sync_playwright
        report["playwright"] = importlib.metadata.version("playwright")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel="msedge", headless=True,
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
            )
            report["browser"] = browser.version
            try:
                for fault in ("normal", "suspend", "ended"):
                    report["cases"].append(probe(browser, fault))
            finally:
                browser.close()
        if report["cases"][0]["status"] != "PASS":
            raise RuntimeError("normal pause/start control failed; recovery findings are inconclusive")
        report["status"] = "PASS" if all(case["status"] == "PASS" for case in report["cases"]) else "FAIL"
        report["source_sha256"] = {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
            for path in ("web/teacher.js", "web/audio-worklet.js", "server/connection.py",
                         "server/pipeline.py", "scripts/qa_native_browser.py")
        }
        code = int(report["status"] != "PASS")
    except Exception as error:
        report.update(status="ERROR", error=f"{type(error).__name__}: {error}")
        code = 2
    args.json.parent.mkdir(parents=True, exist_ok=True)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    args.json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
