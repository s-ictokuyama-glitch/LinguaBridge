"""Prove manual-code and query-code joins and browser display preferences."""

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import re
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / ".qa-tools"))

from playwright.sync_api import expect, sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--run-dir", type=Path, required=True)
parser.add_argument("--browser", default="C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")
args = parser.parse_args()
run = args.run_dir.resolve()
subprocess.run([sys.executable, str(Path(__file__).with_name("instance.py")), "doctor", "--run-dir", str(run)], check=True)
state = json.loads((run / "instance.json").read_text(encoding="utf-8"))
out = run / "student-preferences"
out.mkdir(exist_ok=False)
result = {"feature": "student-preferences", "status": "fail", "url": state["url"], "actions": [], "scope": "real production CLI, HTTP/WS, Edge, real configured models; no microphone or inference exercised"}
contexts = []
pages = []
errors = []

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.browser, headless=True)
        with ExitStack() as resources:
            resources.callback(browser.close)
            result["browser"] = browser.version
            for role in ["teacher", "student"]:
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                contexts.append(context)
                resources.callback(context.close)
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                resources.callback(context.tracing.stop, path=str(out / f"{len(contexts) - 1}-trace.zip"))
                page = context.new_page()
                pages.append(page)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(state["url"] + ("/teacher" if role == "teacher" else "/"))
            teacher, student = pages
            expect(teacher.locator("#join-code")).to_have_text(re.compile(r"^\d{4}$"))
            expect(teacher.locator("#start-btn")).to_be_enabled()
            expect(teacher.locator("#stat-students")).to_have_text("0")
            code = teacher.locator("#join-code").inner_text()
            result["actions"].append({"action": "read_teacher_code", "code": code, "teacher_state": teacher.locator("#session-state").inner_text()})
            expect(student.locator("#join-btn")).to_be_disabled()
            student.locator("#code-input").fill(code)
            student.locator('#lang-options button[data-code="en"]').click()
            student.screenshot(path=str(out / "01-before-join.png"), full_page=True)
            student.locator("#join-btn").click()
            expect(student.locator("#caption-screen")).to_be_visible()
            expect(student.locator("#join-screen")).to_be_hidden()
            expect(student.locator("#session-banner")).to_have_text("Waiting to start")
            expect(teacher.locator("#stat-students")).to_have_text("1")
            result["actions"].append({"action": "manual_code_join", "language": "en", "teacher_students": 1})
            student.locator('#size-buttons button[data-size="l"]').click()
            student.locator("#ja-toggle").uncheck()
            expect(student.locator("#caption-screen")).to_have_class(re.compile(r"size-l"))
            expect(student.locator("#caption-screen")).to_have_class(re.compile(r"hide-ja"))
            result["actions"].append({"action": "change_preferences", "size": "l", "show_ja": False})
            student.screenshot(path=str(out / "02-preferences.png"), full_page=True)
            student.reload()
            student.locator("#code-input").fill(code)
            student.locator('#lang-options button[data-code="en"]').click()
            student.locator("#join-btn").click()
            expect(student.locator("#caption-screen")).to_be_visible()
            expect(student.locator("#join-screen")).to_be_hidden()
            expect(student.locator("#caption-screen")).to_have_class(re.compile(r"size-l"))
            expect(student.locator("#ja-toggle")).not_to_be_checked()
            expect(student.locator("#caption-screen")).to_have_class(re.compile(r"hide-ja"))
            result["actions"].append({"action": "reload_and_rejoin", "size_retained": "l", "show_ja_retained": False})
            student.screenshot(path=str(out / "03-reloaded.png"), full_page=True)
            student.locator("#lang-select").select_option("zh")
            expect(student.locator("html")).to_have_attribute("lang", "zh")
            expect(student.locator("#session-banner")).to_have_text("等待开始")
            expect(teacher.locator("#stat-langs")).to_have_text("zh: 1")
            result["actions"].append({"action": "change_language", "language": "zh", "teacher_languages": "zh: 1"})
            student.goto(state["url"] + "/?code=" + code)
            expect(student.locator("#code-input")).to_have_value(code)
            student.locator('#lang-options button[data-code="zh"]').click()
            student.locator("#join-btn").click()
            expect(student.locator("#caption-screen")).to_be_visible()
            expect(student.locator("#join-screen")).to_be_hidden()
            expect(student.locator("#session-banner")).to_have_text("等待开始")
            expect(teacher.locator("#stat-students")).to_have_text("1")
            expect(teacher.locator("#stat-langs")).to_have_text("zh: 1")
            result["actions"].append({"action": "query_code_join", "language": "zh", "teacher_students": 1})
            for role, page in zip(["teacher", "student"], pages):
                page.screenshot(path=str(out / f"04-{role}.png"), full_page=True)
                (out / f"{role}.aria.txt").write_text(page.locator("body").aria_snapshot(), encoding="utf-8")
            assert not errors, errors
            assert not list(Path(state["data_dir"]).rglob("transcript*")), "Recording is off; no transcript may be written"
            result.update(status="pass", page_errors=errors, recording_files=[])
except Exception:
    result["status"] = "fail"
    result["error"] = traceback.format_exc()
finally:
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "evidence": str(out)}, ensure_ascii=False))

raise SystemExit(0 if result["status"] == "pass" else 1)
