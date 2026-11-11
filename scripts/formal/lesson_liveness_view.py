"""Replay observed pipeline frames through the real student page in a browser."""
from __future__ import annotations

import argparse
import json
import mimetypes
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


def check_screens(screens: list[dict], source_root: Path, channel: str) -> dict:
    observations = []
    violations = []
    with sync_playwright() as playwright:
        options = {"channel": channel} if channel != "chromium" else {}
        browser = playwright.chromium.launch(headless=True, **options)
        try:
            for screen in screens:
                page = browser.new_page(viewport={"width": 1100, "height": 760})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def serve(route):
                    path = urlparse(route.request.url).path
                    if path == "/api/config":
                        route.fulfill(json={"languages": [
                            {"code": "en", "label": "English"},
                            {"code": "zh", "label": "中文"},
                        ]})
                        return
                    relative = path.removeprefix("/static/").lstrip("/") or "index.html"
                    asset = (source_root / "web" / relative).resolve()
                    if not asset.is_relative_to((source_root / "web").resolve()):
                        raise ValueError(f"unexpected asset path: {path}")
                    route.fulfill(
                        body=asset.read_bytes(),
                        content_type=mimetypes.guess_type(asset.name)[0] or "application/octet-stream",
                    )

                def connect(socket):
                    def receive(message):
                        join = json.loads(message)
                        if join["type"] != "join":
                            raise ValueError(f"unexpected browser message: {join}")
                        socket.send(json.dumps({
                            "type": "joined", "session_state": "live", "history_from": 1,
                            "recording": False, "speaking": False,
                        }))
                        for frame in screen["frames"]:
                            socket.send(json.dumps(frame, ensure_ascii=False))
                        socket.send(json.dumps({"type": "session", "state": "ended"}))
                    socket.on_message(receive)

                page.route("**/*", serve)
                page.route_web_socket("**/ws", connect)
                page.goto("http://lesson.test/?code=1234")
                page.locator(f'#lang-options button[data-code="{screen["language"]}"]').click()
                page.locator("#join-btn").click()
                page.wait_for_function("state.ended === true")
                actual = page.locator("#cards .card").evaluate_all("""cards => cards.map(card => ({
                    seq: Number(card.dataset.seq),
                    ja: card.querySelector('.ja').textContent,
                    text: card.querySelector('.text').textContent,
                    lang: card.querySelector('.text').lang,
                }))""")
                expected = screen["expected"]
                if errors:
                    raise RuntimeError(f"student page errors: {errors}")
                visible = (page.locator("#caption-screen").is_visible()
                           and page.locator("#cards .card .text").last.is_visible())
                if expected and not visible:
                    violations.append({"property": "caption_visibility", "screen": screen["name"],
                                       "language": screen["language"], "expected": True, "actual": visible})
                if actual != expected:
                    first = next((i for i, pair in enumerate(zip(actual, expected))
                                  if pair[0] != pair[1]), min(len(actual), len(expected)))
                    violations.append({
                        "property": "caption_display", "screen": screen["name"],
                        "language": screen["language"], "first_difference": first,
                        "expected_count": len(expected), "actual_count": len(actual),
                        "expected": expected[first:first + 1], "actual": actual[first:first + 1],
                    })
                observations.append({
                    "name": screen["name"], "language": screen["language"],
                    "cards": len(actual), "last_seq": actual[-1]["seq"] if actual else None,
                    "visible": visible,
                })
                page.close()
        finally:
            browser.close()
    return {"status": "FAIL" if violations else "PASS", "screens": observations,
            "violations": violations}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("screens", type=Path)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--mutation-check", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    screens = json.loads(args.screens.read_text(encoding="utf-8"))
    if not screens or not any(len(screen["expected"]) > 16 for screen in screens):
        parser.error("screens must include a pipeline observation with more than 16 captions")
    result = check_screens(screens, args.source_root, args.channel)
    if args.mutation_check:
        result["mutations"] = []
        for name, change, property_name in (
            ("display_stops_after_16", "if (msg.seq > 16) return;", "caption_display"),
            ("hidden_after_16", "if (msg.seq > 16) el.captionScreen.hidden = true;", "caption_visibility"),
        ):
            with tempfile.TemporaryDirectory(prefix="lesson-view-") as directory:
                scratch = Path(directory)
                shutil.copytree(args.source_root / "web", scratch / "web")
                source = scratch / "web/student.js"
                original = source.read_text(encoding="utf-8")
                needle = '    case "caption": {\n'
                if original.count(needle) != 1:
                    raise RuntimeError("student caption mutation no longer matches the source")
                source.write_text(original.replace(needle, needle + f"      {change}\n"), encoding="utf-8")
                broken = check_screens(screens, scratch, args.channel)
                detected = not result["violations"] and any(
                    violation["property"] == property_name for violation in broken["violations"])
                result["mutations"].append({"name": name, "detected": detected,
                                            "violations": broken["violations"]})
                if not detected:
                    result["status"] = "FAIL"
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
