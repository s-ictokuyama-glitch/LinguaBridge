from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]

MUTATIONS = (
    ("source_guard_removed", "source_access.py", "if ip is not None and not ip.is_global:",
     "if True:", "source", "source_gate"),
    ("source_global_reversed", "source_access.py", "not ip.is_global", "ip.is_global",
     "source", "source_gate"),
    ("missing_source_allowed", "source_access.py", "if ip is not None and not ip.is_global:",
     "if ip is None or not ip.is_global:", "source", "source_gate"),
    ("forwarded_source_trusted", "source_access.py", 'client = scope.get("client")',
     'client = scope.get("client")\n        forwarded = dict(scope["headers"]).get(b"x-forwarded-for")\n'
     '        if forwarded:\n            client = (forwarded.decode().split(",")[0], 1)',
     "source", "source_gate"),
    ("websocket_gate_removed", "source_access.py", '("http", "websocket")', '("http",)',
     "source", "source_gate"),
    ("app_middleware_removed", "main.py", "app.add_middleware(SourceAccess)", "pass",
     "source", "source_wiring"),
    ("static_gate_bypassed", "source_access.py", 'client = scope.get("client")',
     'if scope.get("path", "").startswith("/static"):\n'
     '            await self.app(scope, receive, send)\n            return\n'
     '        client = scope.get("client")', "source", "source_wiring"),
    ("proxy_headers_enabled", "serving.py", "proxy_headers=False", "proxy_headers=True",
     "listeners", "listener_proxy"),
    ("disclosure_guard_removed", "main.py",
     'if peer is not None and (peer.is_loopback or peer == socket_ip(request.scope.get("server"))):',
     "if True:", "disclosure", "teacher_disclosure"),
    ("disclosure_condition_reversed", "main.py",
     'peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     'not (peer.is_loopback or peer == socket_ip(request.scope.get("server")))',
     "disclosure", "teacher_disclosure"),
    ("remote_private_disclosed", "main.py",
     'peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     'not peer.is_global', "disclosure", "teacher_disclosure"),
    ("https_disclosed", "main.py", 'peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     'request.url.scheme == "https" or peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     "disclosure", "teacher_disclosure"),
    ("host_disclosed", "main.py", 'peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     'request.headers.get("host") == "localhost" or peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     "disclosure", "teacher_disclosure"),
    ("forwarded_disclosed", "main.py", 'peer = socket_ip(request.scope.get("client"))',
     'peer = socket_ip((request.headers.get("x-forwarded-for", "invalid"), 1))',
     "disclosure", "teacher_disclosure"),
    ("loopback_removed", "main.py", 'peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     'peer == socket_ip(request.scope.get("server"))', "disclosure", "teacher_disclosure"),
    ("equal_socket_removed", "main.py", 'peer.is_loopback or peer == socket_ip(request.scope.get("server"))',
     'peer.is_loopback', "disclosure", "teacher_disclosure"),
    ("public_equal_disclosed", "main.py", "app.add_middleware(SourceAccess)", "pass",
     "disclosure", "teacher_disclosure"),
    ("failure_not_counted", "rate_limit.py", "failures[role] += 1", "failures[role] += 0",
     "limits", "limit_accounting"),
    ("roles_not_summed", "rate_limit.py", "failures.total() >= self._max_failures",
     "failures[role] >= self._max_failures", "limits", "limit_accounting"),
    ("threshold_late", "rate_limit.py", ">= self._max_failures", "> self._max_failures",
     "limits", "limit_accounting"),
    ("success_clears_both_roles", "rate_limit.py", "failures.pop(role, None)", "failures.clear()",
     "limits", "limit_accounting"),
    ("success_clears_neither", "rate_limit.py", "failures.pop(role, None)", "pass",
     "limits", "limit_accounting"),
    ("expiry_late", "rate_limit.py", "self._clock() >= until", "self._clock() > until",
     "limits", "join_outcome"),
    ("expiry_early", "rate_limit.py", "self._clock() >= until", "self._clock() >= until - 0.2",
     "limits", "join_outcome"),
    ("deadline_shortened", "rate_limit.py", "self._clock() + self._block_seconds",
     "self._clock() + self._block_seconds - 1", "limits", "limit_accounting"),
    ("blocked_guard_removed", "lesson.py", "if self._limiter.is_blocked(client_ip):", "if False:",
     "limits", "join_outcome"),
    ("success_wrong_role", "lesson.py", "record_success(client_ip, role=msg.role)",
     "record_success(client_ip)", "limits", "limit_accounting"),
    ("failure_wrong_role", "lesson.py", "record_failure(client_ip, role=msg.role)",
     "record_failure(client_ip)", "limits", "limit_accounting"),
    ("shared_ip_key", "lesson.py", 'reason: proto.JoinRejectReason | None = None',
     'client_ip = "shared"\n        reason: proto.JoinRejectReason | None = None',
     "limits", "limit_accounting"),
    ("ws_ip_shared", "main.py", 'ws.client.host if ws.client else "unknown"',
     '"shared"', "join-wiring", "join_wiring"),
)


def run_mutations(selected_group="all"):
    results = {}
    with tempfile.TemporaryDirectory(prefix="school-access-mutants-") as directory:
        product = Path(directory)
        shutil.copytree(ROOT / "server", product / "server", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "web", product / "web")
        for name, file, before, after, group, property in MUTATIONS:
            if selected_group not in ("all", group):
                continue
            path = product / "server" / file
            original = path.read_text(encoding="utf-8")
            if original.count(before) != 1:
                raise ValueError(f"Mutation must match exactly once: {name}")
            path.write_text(original.replace(before, after), encoding="utf-8")
            try:
                child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(ROOT / "scripts/formal/school_access.py"),
                    "--product-root", str(product), "--group", group], cwd=ROOT,
                    capture_output=True, text=True, encoding="utf-8", timeout=150)
                if child.returncode not in (0, 1):
                    raise RuntimeError(f"Mutant crashed: {name}\n{child.stderr}")
                report = json.loads(child.stdout)
                witness = report.get("violation", {})
                detected = child.returncode == 1 and witness.get("property") == property
                results[name] = dict(detected=detected, expected_property=property,
                                     status=report["status"], violation=witness)
            finally:
                path.write_text(original, encoding="utf-8")
    return results


if __name__ == "__main__":
    result = run_mutations()
    print(json.dumps(result, indent=2))
    sys.exit(not all(item["detected"] for item in result.values()))
