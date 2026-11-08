"""Bounded checks of production source access, teacher disclosure and join limits."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from dataclasses import dataclass
import importlib.metadata
from ipaddress import ip_address
import itertools
import json
import logging
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SYNC_COMMIT = "5381edb92062d1a35972b3a7dee8d387c5b0aa1b"
JOIN, TEACHER = "4831", "604831"
DEPTH = 8
TIMES = (0.0, 59.9, 60.0, 60.1, 120.1)
IPS = ("192.168.5.10", "192.168.5.11")


class Violation(Exception):
    def __init__(self, property, trace, expected, actual):
        self.witness = dict(property=property, trace=trace, expected=expected, actual=actual)


def require(condition, property, trace, expected, actual):
    if not condition:
        raise Violation(property, trace, expected, actual)


def addresses():
    values = {None, "", "invalid", "localhost", "127.1", "010.0.0.1", "1.2.3.999",
              "[::1]", "fe80::1%eth0", "::1", "::", "ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"}
    ranges = ("0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
              "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
              "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
              "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "64:ff9b::/96",
              "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32",
              "2002::/16", "fc00::/7", "fe80::/10", "ff00::/8")
    from ipaddress import ip_network
    for value in ranges:
        net = ip_network(value)
        for number in (int(net.network_address) - 1, int(net.network_address),
                       int(net.broadcast_address), int(net.broadcast_address) + 1):
            if 0 <= number < 2 ** net.max_prefixlen:
                values.add(str(type(net.network_address)(number)))
    values.update(("8.8.8.8", "2606:4700::1111", "192.0.0.9", "192.0.0.10",
                   "2001:1::1", "2001:1::2", "2001:3::1", "2001:4:112::1"))
    for value in tuple(values):
        if value and ":" not in value:
            try:
                if ip_address(value).version == 4:
                    values.add("::ffff:" + value)
            except ValueError:
                pass
    return sorted(values, key=lambda x: (x is not None, x or ""))


def parsed(value):
    if value is None:
        return None
    try:
        return ip_address(value)
    except ValueError:
        return None


def permitted(value):
    ip = parsed(value)
    return ip is not None and not ip.is_global


HEADERS = (
    [],
    [(b"host", b"localhost"), (b"forwarded", b"for=127.0.0.1;host=localhost;proto=https"),
     (b"x-forwarded-for", b"127.0.0.1"), (b"x-real-ip", b"127.0.0.1"),
     (b"x-forwarded-host", b"localhost"), (b"x-forwarded-proto", b"https")],
    [(b"host", b"8.8.8.8"), (b"forwarded", b"for=8.8.8.8;proto=http"),
     (b"x-forwarded-for", b"8.8.8.8, 127.0.0.1"), (b"x-real-ip", b"8.8.8.8"),
     (b"x-forwarded-host", b"8.8.8.8"), (b"x-forwarded-proto", b"http")],
)


async def exchange(app, peer, *, kind="http", path="/healthz", scheme="http",
                   server="192.168.5.25", headers=(), frames=()):
    scope = dict(type=kind, asgi={"version": "3.0", "spec_version": "2.4"},
                 http_version="1.1", method="GET", scheme=scheme, path=path,
                 raw_path=path.encode(), root_path="", query_string=b"", headers=list(headers),
                 client=None if peer is None else (peer, 50000),
                 server=None if server is None else (server, 8000), subprotocols=[])
    incoming = deque(([{"type": "http.request", "body": b"", "more_body": False}]
                      if kind == "http" else
                      [{"type": "websocket.connect"}, *frames,
                       {"type": "websocket.disconnect", "code": 1000}]))
    sent = []

    async def receive():
        await asyncio.sleep(0)
        return incoming.popleft()

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def http_result(messages):
    return (next(m["status"] for m in messages if m["type"] == "http.response.start"),
            b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body"))


def classroom():
    from server.asr.fake_engine import FakeASREngine
    from server.config import AppConfig, TurnConfig, VadConfig
    from server.main import create_app
    from server.mt.fake_engine import FakeTranslationEngine
    from server.network import InterfaceAddress
    from server.publication import Publication
    config = AppConfig(vad=VadConfig(engine="energy", threshold=300), turn=TurnConfig(strategy="simple"))
    with patch("server.network.list_addresses", return_value=[
        InterfaceAddress("formal", "192.168.5.25", True, "physical")
    ]):
        publication = Publication(config.server)
    with patch.object(Publication, "current", return_value=publication):
        app = create_app(config, publication, asr_engine=FakeASREngine(),
                         mt_engine=FakeTranslationEngine(), join_code=JOIN, teacher_code=TEACHER)
    return app, publication


async def check_source():
    from server.source_access import SourceAccess, socket_ip
    app, publication = classroom()
    count = 0
    reached = []

    async def downstream(scope, receive, send):
        reached.append(scope)

    with patch.object(type(publication), "current", return_value=publication):
        for peer, headers in itertools.product(addresses(), HEADERS):
            ip = parsed(peer)
            require(socket_ip(None if peer is None else (peer, 1)) == ip,
                    "source_parse", [peer], str(ip), "different parsed address")
            for kind in ("http", "websocket"):
                reached.clear()
                messages = await exchange(SourceAccess(downstream), peer, kind=kind, headers=headers)
                require(bool(reached) == permitted(peer), "source_gate", [peer, kind, headers],
                        permitted(peer), bool(reached))
                if not permitted(peer):
                    denied = (http_result(messages)[0] == 403 if kind == "http" else
                              messages == [{"type": "websocket.close", "code": 1008}])
                    require(denied, "source_denial", [peer, kind], "403/1008", messages)
                count += 1
            for path in ("/healthz", "/", "/static/teacher.js", "/api/teacher-info", "/ws"):
                kind = "websocket" if path == "/ws" else "http"
                messages = await exchange(app, peer, kind=kind, path=path, headers=headers)
                actual = (http_result(messages)[0] if kind == "http" else
                          next(m["type"] for m in messages))
                expected = ((200 if permitted(peer) else 403) if kind == "http" else
                            ("websocket.accept" if permitted(peer) else "websocket.close"))
                require(actual == expected, "source_wiring", [peer, path, headers], expected, actual)
                count += 1
    with patch.object(app.router, "middleware_stack", downstream):
        for peer in addresses():
            for kind in ("http", "websocket"):
                reached.clear()
                await exchange(app, peer, kind=kind)
                require(bool(reached) == permitted(peer), "source_routing", [peer, kind],
                        permitted(peer), bool(reached))
                count += 1
    return dict(cases=count, peer_values=len(addresses()))


async def check_disclosure():
    app, publication = classroom()
    servers = (None, "invalid", "0.0.0.0", "192.168.5.25", "127.0.0.1", "::1",
               "fe80::1", "::ffff:192.168.5.25", "2001:db8::1", "8.8.8.8")
    count = 0
    with patch.object(type(publication), "current", return_value=publication):
        for peer in addresses():
            for server, scheme, headers in itertools.product((*servers, peer), ("http", "https"), HEADERS):
                messages = await exchange(app, peer, path="/api/teacher-info", server=server,
                                          scheme=scheme, headers=headers)
                status, body = http_result(messages)
                data = json.loads(body)
                ip = parsed(peer)
                expected = permitted(peer) and (ip.is_loopback or ip == parsed(server))
                actual = data.get("teacher_code")
                require(("teacher_code" in data) == bool(expected) and
                        (not expected or actual == TEACHER) and
                        status == (200 if permitted(peer) else 403),
                        "teacher_disclosure", [peer, server, scheme, headers],
                        dict(status=200 if permitted(peer) else 403, disclose=bool(expected)),
                        dict(status=status, disclose="teacher_code" in data))
                require(expected or TEACHER.encode() not in body, "teacher_body",
                        [peer, server, scheme], "no teacher code anywhere in body", body.decode())
                count += 1
    return dict(cases=count)


@dataclass(frozen=True)
class LimitState:
    failures: tuple = ((0, 0), (0, 0))
    deadlines: tuple = (None, None)
    roles: tuple = (None, None)
    tick: int = 0


async def check_limits():
    from server.config import AppConfig
    from server.lesson import Lesson
    from server.rate_limit import JoinRateLimiter
    from server.ws_protocol import JoinMessage
    now = [0.0]
    limiter = JoinRateLimiter(clock=lambda: now[0])
    require((limiter._max_failures, limiter._block_seconds) == (5, 60.0),
            "limit_defaults", [], [5, 60], [limiter._max_failures, limiter._block_seconds])
    outcome = []
    roster = SimpleNamespace(contains=lambda c: True, students=lambda: [],
                             send=lambda c, p: outcome.append(p))

    async def add(socket, role, lang, **kwargs):
        outcome.append(kwargs["initial"])
        return SimpleNamespace(role=role, ws=socket)

    async def send_now(socket, payload):
        outcome.append(payload)

    roster.add = add
    pipeline = SimpleNamespace(roster=roster, speaking=False, on_teacher_joined=lambda: None,
        captions=SimpleNamespace(history=SimpleNamespace(seq_head=0, history_from=1),
                                 cancel_history_refresh=lambda c: None))
    lesson = Lesson(pipeline, AppConfig(), join_code=JOIN, teacher_code=TEACHER, limiter=limiter)
    lesson.send_now = send_now
    sockets = [object(), object()]
    events = tuple(itertools.product(range(2), range(2), (False, True)))
    queue = deque([(LimitState(), ())])
    seen = {LimitState()}
    edges = 0
    depths = Counter()
    while queue:
        state, trace = queue.popleft()
        depths[len(trace)] += 1
        if len(trace) == DEPTH:
            continue
        for event in (*events, "advance"):
            now[0] = TIMES[state.tick]
            limiter._failures = {ip: Counter(dict(zip(("student", "teacher"), counts)))
                                 for ip, counts in zip(IPS, state.failures) if any(counts)}
            limiter._blocked_until = {ip: deadline for ip, deadline in zip(IPS, state.deadlines)
                                      if deadline is not None}
            failures = [list(c) for c in state.failures]
            deadlines, roles, tick = list(state.deadlines), list(state.roles), state.tick
            if event == "advance":
                if tick == len(TIMES) - 1:
                    continue
                tick += 1
                label = f"time={TIMES[tick]}"
            else:
                ip_index, role_index, correct = event
                role, ip = ("student", "teacher")[role_index], IPS[ip_index]
                label = f"{ip} join {role} {'correct' if correct else 'wrong'}"
                deadline = deadlines[ip_index]
                blocked = deadline is not None and now[0] < deadline
                if deadline is not None and not blocked:
                    deadlines[ip_index] = None
                expected = "rate_limited" if blocked else ("joined" if correct else "bad_code")
                if not blocked:
                    if correct:
                        failures[ip_index][role_index] = 0
                        roles[ip_index] = role
                    else:
                        failures[ip_index][role_index] += 1
                        if sum(failures[ip_index]) == 5:
                            failures[ip_index] = [0, 0]
                            deadlines[ip_index] = now[0] + 60.0
                current_role = state.roles[ip_index]
                current = SimpleNamespace(role=current_role) if current_role else None
                outcome.clear()
                client = await lesson.join(sockets[ip_index], current,
                    JoinMessage(type="join", role=role, lang="en",
                                code=(JOIN if role == "student" else TEACHER) if correct else "wrong"), ip)
                actual = outcome[-1].get("reason", outcome[-1]["type"]) if outcome else None
                require(actual == expected, "join_outcome", [*trace, label], expected, actual)
                require((client.role if client else None) == roles[ip_index], "join_role",
                        [*trace, label], roles[ip_index], client.role if client else None)
            successor = LimitState(tuple(map(tuple, failures)), tuple(deadlines), tuple(roles), tick)
            actual_counts = tuple(tuple(limiter._failures.get(ip, {}).get(r, 0)
                                        for r in ("student", "teacher")) for ip in IPS)
            actual_deadlines = tuple(limiter._blocked_until.get(ip) for ip in IPS)
            require((actual_counts, actual_deadlines) == (successor.failures, successor.deadlines),
                    "limit_accounting", [*trace, label],
                    [successor.failures, successor.deadlines], [actual_counts, actual_deadlines])
            edges += 1
            if successor not in seen:
                seen.add(successor)
                queue.append((successor, (*trace, label)))
    boundary_cases = 0
    for start, failure_roles, offset, role in itertools.product(
            TIMES, itertools.product(("student", "teacher"), repeat=5),
            (-0.1, 0.0, 0.1), ("student", "teacher")):
        limiter._failures.clear()
        limiter._blocked_until.clear()
        now[0] = start
        boundary_trace = [f"time={start}"]
        for failed_role in failure_roles:
            outcome.clear()
            await lesson.join(sockets[0], None,
                JoinMessage(type="join", role=failed_role, lang="en", code="wrong"), IPS[0])
            boundary_trace.append(f"{IPS[0]} join {failed_role} wrong")
            require(outcome == [{"type": "join_rejected", "reason": "bad_code"}],
                    "join_outcome", boundary_trace, "bad_code", outcome)
        now[0] = start + 60.0 + offset
        outcome.clear()
        await lesson.join(sockets[0], None, JoinMessage(type="join", role=role, lang="en",
            code=JOIN if role == "student" else TEACHER), IPS[0])
        expected = "rate_limited" if offset < 0 else "joined"
        actual = outcome[-1].get("reason", outcome[-1]["type"]) if outcome else None
        require(actual == expected, "join_outcome",
                [*boundary_trace, f"time={now[0]}", f"join {role} correct"], expected, actual)
        boundary_cases += 1
    return dict(depth=DEPTH, states=len(seen), transitions=edges, states_by_shortest_depth=dict(depths),
                times=TIMES, deadline_boundary_cases=boundary_cases)


async def check_join_wiring():
    from server.rate_limit import JoinRateLimiter
    app, publication = classroom()
    now = [0.0]
    app.state.lesson._limiter = JoinRateLimiter(clock=lambda: now[0])
    cases = 0
    with patch.object(type(publication), "current", return_value=publication):
        for attempt in range(5):
            for role, code, expected in (("teacher", JOIN, "bad_code"),
                                          ("student", JOIN, "joined" if attempt < 4 else "rate_limited")):
                frames = [{"type": "websocket.receive", "text": json.dumps(
                    dict(type="join", role=role, code=code, lang="en"))}]
                messages = await exchange(app, IPS[0], kind="websocket", path="/ws", frames=frames)
                payloads = [json.loads(m["text"]) for m in messages if m["type"] == "websocket.send"]
                actual = [p.get("reason", p["type"]) for p in payloads]
                require(expected in actual, "join_wiring", [attempt, role], expected, actual)
                cases += 1
        for peer, time, expected in ((IPS[0], 59.9, "rate_limited"), (IPS[1], 59.9, "joined"),
                                      (IPS[0], 60.0, "joined")):
            now[0] = time
            messages = await exchange(app, peer, kind="websocket", path="/ws", headers=HEADERS[1],
                frames=[{"type": "websocket.receive", "text": json.dumps(
                    dict(type="join", role="teacher", code=TEACHER))}])
            actual = [p.get("reason", p["type"]) for m in messages if m["type"] == "websocket.send"
                      for p in [json.loads(m["text"])]]
            require(expected in actual, "join_wiring", [peer, time], expected, actual)
            cases += 1
    return dict(cases=cases)


async def check_listeners():
    import httpx
    from scripts.make_cert import generate
    from server.serving import ListenerSpec, TLSFiles, start_listeners
    reached = []

    async def app(scope, receive, send):
        reached.append((scope["client"][0], scope["scheme"]))
        await send(dict(type="http.response.start", status=200, headers=[]))
        await send(dict(type="http.response.body", body=b"ok"))

    with tempfile.TemporaryDirectory(prefix="school-access-tls-") as directory:
        cert, key = Path(directory) / "cert.pem", Path(directory) / "key.pem"
        generate(cert, key, ip="127.0.0.1")
        specs = [ListenerSpec("127.0.0.1", 0),
                 ListenerSpec("127.0.0.1", 0, tls=TLSFiles(cert, key))]
        async with start_listeners(app, specs, install_signals=False) as running:
            for server in running._servers:
                require(server.config.proxy_headers is False, "listener_proxy", [], False,
                        server.config.proxy_headers)
            async with httpx.AsyncClient(verify=str(cert), trust_env=False, timeout=5) as client:
                for scheme, port in zip(("http", "https"), running.ports):
                    for headers in HEADERS:
                        response = await client.get(f"{scheme}://127.0.0.1:{port}/",
                                                    headers=dict(headers))
                        require(response.status_code == 200 and reached[-1] == ("127.0.0.1", scheme),
                                "listener_source", [scheme, headers], ["127.0.0.1", scheme], reached[-1])
    return dict(listeners=2, requests=6)


CHECKS = {"source": check_source, "disclosure": check_disclosure,
          "limits": check_limits, "join-wiring": check_join_wiring, "listeners": check_listeners}


async def check(group):
    results = {}
    try:
        for name, run in CHECKS.items():
            if group in ("all", name):
                async with asyncio.timeout(120):
                    results[name] = await run()
        return dict(status="PASS", checks=results)
    except Violation as error:
        return dict(status="FAIL", checks=results, violation=error.witness)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("all", *CHECKS), default="all")
    parser.add_argument("--product-root", type=Path, default=ROOT)
    parser.add_argument("--mutation-check", action="store_true")
    args = parser.parse_args()
    sys.path[:0] = [str(args.product_root), str(ROOT)] if args.product_root != ROOT else [str(ROOT)]
    logging.disable(logging.CRITICAL)
    from server import event_loop
    report = event_loop.run(check(args.group))
    if args.mutation_check and report["status"] == "PASS":
        from scripts.formal.school_access_mutations import run_mutations
        report["mutations"] = run_mutations(args.group)
        report["after_mutations"] = event_loop.run(check(args.group))
        if (not all(item["detected"] for item in report["mutations"].values())
                or report["after_mutations"]["status"] != "PASS"):
            report["status"] = "FAIL"
    report.update(sync_commit=SYNC_COMMIT, product_root=str(args.product_root), python=sys.version,
                  dependencies={name: importlib.metadata.version(name) for name in
                    ("fastapi", "starlette", "uvicorn", "httpx", "numpy", "pydantic", "PyYAML",
                     "cryptography", "psutil")})
    print(json.dumps(report, indent=2, default=lambda x: x.decode() if isinstance(x, bytes) else str(x)))
    return report["status"] != "PASS"


if __name__ == "__main__":
    sys.exit(main())
