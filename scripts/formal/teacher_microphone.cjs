"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { spawnSync } = require("node:child_process");

const SYNC_COMMIT = "df2dd6e93f83fd85a5186898071392599b7e2875";
const DEVICES = ["mic-a", "mic-b", ""];
const tick = () => new Promise((resolve) => setImmediate(resolve));

class Violation extends Error {
  constructor(property, expected, actual) {
    super(property);
    this.detail = { property, expected, actual };
  }
}

function requireProperty(condition, property, expected, actual) {
  if (!condition) throw new Violation(property, expected, actual);
}

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(name, callback) {
    this.listeners.set(name, [...this.listeners.get(name) || [], callback]);
  }
  emit(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) {
      const result = callback(event);
      if (result?.then) result.catch((error) => { this.error = error; });
    }
  }
}

function element() {
  return Object.assign(new Events(), {
    textContent: "", value: "", disabled: false, hidden: false,
    style: {}, classList: { remove() {}, toggle() {} },
    appendChild() {},
  });
}

function stream(identity, device, live = true) {
  const track = { readyState: live ? "live" : "ended",
    stop() { this.readyState = "ended"; } };
  return { identity, device, getTracks: () => [track], getAudioTracks: () => [track] };
}

class Contract {
  constructor(initial) {
    this.context = initial.context;
    this.active = { identity: "old", device: "mic-a", live: initial.live };
    this.latest = null;
    this.requests = [];
    this.calls = [];
    this.start = null;
    this.starts = 0;
    this.selections = 0;
    this.sent = [];
    this.notice = "";
  }
  acquire(intent, device) {
    const request = { id: this.requests.length, intent, device, status: "pending" };
    this.requests.push(request);
    this.latest = request;
    this.calls.push({ kind: "gum", device, owner: request.id, pending: true });
  }
  afterResume() {
    const changed = this.start.initial !== this.latest?.id;
    if (this.start.wasPending || changed) this.start.phase = "request";
    else if (!this.active.live) {
      this.acquire("recover", this.active.device);
      this.start.phase = "request";
    } else this.finishStart();
    this.progress();
  }
  finishStart() {
    this.start.phase = this.context === "running" && this.active.live ? "sent" : "failed";
    if (this.start.phase === "sent") {
      this.sent.push({ identity: this.active.identity, device: this.active.device });
    }
  }
  progress() {
    if (this.start?.phase === "request" && this.latest?.status !== "pending") this.finishStart();
  }
  apply(event) {
    if (event.kind === "start") {
      this.starts++;
      this.start = { phase: "resume", initial: this.latest?.id,
        wasPending: this.latest?.status === "pending" };
      if (this.context === "running") this.afterResume();
      else this.calls.push({ kind: "resume", pending: true });
    } else if (event.kind === "select") {
      this.selections++;
      this.acquire("select", event.device);
    } else {
      const call = this.calls[event.call];
      call.pending = false;
      call.ok = event.ok;
      if (call.kind === "resume") {
        if (event.ok) {
          this.context = "running";
          this.afterResume();
        } else this.start.phase = "failed";
      } else if (call.owner === this.latest.id) {
        if (event.ok) {
          this.active = { identity: `call-${event.call}`, device: call.device, live: true };
          this.latest.status = "ready";
          this.notice = this.latest.intent === "recover" && call.device === "" ? "fallback" : "";
        } else if (this.latest.intent === "recover" && call.device !== "") {
          this.calls.push({ kind: "gum", device: "", owner: call.owner, pending: true });
        } else {
          this.latest.status = "failed";
          if (this.latest.intent === "select") this.notice = "selection_failure";
        }
        this.progress();
      }
    }
  }
  candidates() {
    const events = [];
    if (this.starts === 0) events.push({ kind: "start" });
    if (this.selections < 2) {
      for (const device of DEVICES) events.push({ kind: "select", device });
    }
    this.calls.forEach((call, index) => {
      if (call.pending) for (const ok of [true, false]) events.push({ kind: "complete", call: index, ok });
    });
    return events;
  }
  legal(event) {
    return event.kind !== "select" || event.device !== this.selection();
  }
  selection() {
    return this.latest?.status === "pending" && this.latest.intent === "select"
      ? this.latest.device : this.active.device;
  }
}

async function page(source, initial) {
  const elements = new Map();
  const calls = [];
  const streams = [stream("old", "mic-a", initial.live)];
  const connected = new Set([streams[0]]);
  const saved = new Map([["lb_mic_device_id", "mic-a"]]);
  const sent = [];
  const defer = (kind, device) => new Promise((resolve, reject) => {
    calls.push({ kind, device, resolve, reject, pending: true });
  });
  const context = { state: initial.context,
    resume() { return defer("resume").then(() => { context.state = "running"; }); },
    createMediaStreamSource(input) {
      return { connect() { connected.add(input); }, disconnect() { connected.delete(input); } };
    },
  };
  class Socket extends Events {
    static OPEN = 1;
    constructor() { super(); this.readyState = Socket.OPEN; }
    send(data) {
      const message = JSON.parse(data);
      if (message.type !== "control" || message.action !== "start") {
        throw new Error(`unexpected send: ${data}`);
      }
      sent.push({ context: context.state, inputs: [...connected]
        .filter((input) => input.getAudioTracks()[0].readyState === "live")
        .map(({ identity, device }) => ({ identity, device })) });
    }
  }
  const sandbox = {
    document: { body: { dataset: {} }, getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    }, createElement: element },
    location: { protocol: "http:", host: "teacher.test" },
    navigator: { mediaDevices: {
      enumerateDevices: async () => ["mic-a", "mic-b"].map((deviceId) => ({
        kind: "audioinput", deviceId, label: deviceId })),
      getUserMedia: ({ audio }) => defer("gum", audio.deviceId?.exact || ""),
      addEventListener() {},
    } },
    localStorage: { getItem: (key) => saved.get(key) || null,
      setItem: (key, value) => saved.set(key, value) },
    WebSocket: Socket, Option: function() {}, QRCode: function() {},
    AbortSignal: { timeout: () => ({}) }, setTimeout() {},
    fetch: async () => ({ ok: true, json: async () => ({
      teacher_code: "654321", code: "123456", join_url: "http://teacher.test/" }) }),
    context, oldStream: streams[0],
  };
  vm.createContext(sandbox);
  if (!/init\(\);\s*$/.test(source)) throw new Error("teacher init entry changed");
  vm.runInContext(source.replace(/init\(\);\s*$/, ""), sandbox, { filename: "web/teacher.js" });
  await vm.runInContext("init()", sandbox);
  await tick();
  vm.runInContext(`
    state.micReady = true;
    state.audioCtx = context;
    state.micStream = oldStream;
    state.micSource = context.createMediaStreamSource(oldStream);
    state.workletNode = {};
    state.micDeviceId = "mic-a";
    state.joined = true;
    applySessionState("paused");
  `, sandbox);
  const state = vm.runInContext("state", sandbox);
  return { calls, streams, connected, saved, sent, state, context, elements,
    async apply(event) {
      if (event.kind === "start" || event.kind === "select") {
        const target = elements.get(event.kind === "start" ? "start-btn" : "mic-select");
        if (target.disabled) throw new Violation("controls_available", "enabled control", event);
        if (event.kind === "select") target.value = event.device;
        target.emit(event.kind === "start" ? "click" : "change");
      } else {
        const call = calls[event.call];
        if (!call?.pending) throw new Error(`unknown pending completion ${event.call}`);
        call.pending = false;
        if (event.ok) {
          if (call.kind === "gum") {
            call.stream = stream(`call-${event.call}`, call.device);
            streams.push(call.stream);
          }
          call.resolve(call.stream);
        } else call.reject(new Error("microphone or context denied"));
      }
      await tick();
      for (const target of elements.values()) if (target.error) throw target.error;
    },
  };
}

function compare(actual, expected) {
  const calls = actual.calls.map(({ kind, device, pending }) => ({ kind, device, pending }));
  const wanted = expected.calls.map(({ kind, device, pending }) => ({ kind, device, pending }));
  requireProperty(JSON.stringify(calls) === JSON.stringify(wanted), "request_contract", wanted, calls);
  const active = expected.active;
  const connected = [...actual.connected].map((input) => input.identity);
  const observed = { connected, identity: actual.state.micStream.identity,
    device: actual.state.micDeviceId, saved: actual.saved.get("lb_mic_device_id"),
    live: actual.state.micStream.getAudioTracks()[0].readyState === "live" };
  requireProperty(connected.length === 1 && connected[0] === active.identity &&
    observed.identity === active.identity && observed.device === active.device &&
    observed.saved === active.device && observed.live === active.live,
  "input_ownership", active, observed);
  const leaked = actual.streams.filter((input) => input.identity !== active.identity &&
    input.getTracks().some((track) => track.readyState !== "ended")).map((input) => input.identity);
  requireProperty(leaked.length === 0, "superseded_cleanup", [], leaked);
  requireProperty(actual.sent.length === expected.sent.length && actual.sent.every((entry, index) =>
    entry.context === "running" && entry.inputs.length === 1 &&
    JSON.stringify(entry.inputs[0]) === JSON.stringify(expected.sent[index])),
  "start_contract", expected.sent, actual.sent);
  const selected = expected.selection();
  requireProperty(actual.elements.get("mic-select").value === selected, "selection_display",
    selected, actual.elements.get("mic-select").value);
  const notice = actual.elements.get("mic-notice");
  if (expected.notice === "fallback") {
    requireProperty(!notice.hidden && /既定のマイク/.test(notice.textContent), "fallback_notice",
      "visible default microphone notice", { hidden: notice.hidden, text: notice.textContent });
  } else if (expected.notice === "selection_failure") {
    requireProperty(!notice.hidden && /前のマイク/.test(notice.textContent), "selection_failure_notice",
      "visible retained microphone notice", { hidden: notice.hidden, text: notice.textContent });
  }
}

function eventText(event) {
  if (event.kind === "start") return "click_start";
  if (event.kind === "select") return `select:${event.device || "default"}`;
  return `complete:${event.call}:${event.ok ? "resolve" : "reject"}`;
}

async function replay(source, initial, trace) {
  const expected = new Contract(initial);
  const actual = await page(source, initial);
  const steps = [];
  try {
    compare(actual, expected);
    for (const event of trace) {
      steps.push(eventText(event));
      expected.apply(event);
      await actual.apply(event);
      compare(actual, expected);
    }
    return { expected };
  } catch (error) {
    if (!(error instanceof Violation)) throw error;
    return { violation: { ...error.detail, initial, trace: steps } };
  }
}

async function check(source, stopProperty = null) {
  const result = { status: "PASS", candidates: 0, illegal: 0, legal: 0,
    passed: 0, failed: 0, settled_prefixes: 0, violations: [] };
  const queue = [];
  for (const live of [true, false]) for (const context of ["running", "suspended"]) {
    queue.push({ initial: { live, context }, trace: [] });
  }
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const { initial, trace } = queue[cursor];
    queue[cursor] = null;
    result.candidates++;
    result.legal++;
    const outcome = await replay(source, initial, trace);
    if (outcome.violation) {
      result.failed++;
      result.status = "FAIL";
      if (!result.violations.some((entry) => entry.property === outcome.violation.property)) {
        result.violations.push(outcome.violation);
      }
      if (outcome.violation.property === stopProperty) return result;
      continue;
    }
    result.passed++;
    const contract = outcome.expected;
    if (!contract.calls.some((call) => call.pending)) result.settled_prefixes++;
    for (const event of contract.candidates()) {
      if (!contract.legal(event)) { result.candidates++; result.illegal++; continue; }
      queue.push({ initial, trace: [...trace, event] });
    }
  }
  return result;
}

const MUTATIONS = [
  { name: "stale_result_attaches", property: "input_ownership",
    from: "if (state.micRequest !== request) {\n        stream.getTracks().forEach((track) => track.stop());",
    to: "if (false) {\n        stream.getTracks().forEach((track) => track.stop());" },
  { name: "stale_track_leaks", property: "superseded_cleanup",
    from: "if (state.micRequest !== request) {\n        stream.getTracks().forEach((track) => track.stop());",
    to: "if (state.micRequest !== request) {\n        void stream;" },
  { name: "start_does_not_follow_latest", property: "start_contract",
    from: "request = state.micRequest;\n        continue;", to: "break;" },
  { name: "recover_does_not_fallback", property: "request_contract",
    from: 'if (request.intent !== "recover" || !acquiredId) throw err;', to: "throw err;" },
  { name: "selection_display_not_updated", property: "selection_display",
    from: 'el.micSelect.value = found ? wanted : "";',
    to: 'el.micSelect.value = state.micReady ? "mic-b" : (found ? wanted : "");' },
  { name: "fallback_notice_missing", property: "fallback_notice",
    from: 'notice = "前回選んだマイクを使えませんでした。既定のマイクを使います。";', to: 'notice = "";' },
  { name: "failed_selection_notice_missing", property: "selection_failure_notice",
    from: 'showMicNotice(`このマイクを使えませんでした（${error.name}）。前のマイクのままです。`);',
    to: 'showMicNotice("");' },
  { name: "resume_control_disabled", property: "controls_available",
    from: 'el.startBtn.disabled = !connected || s === "live";', to: "el.startBtn.disabled = true;" },
];

async function mutations(source, baseline) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "teacher-microphone-"));
  const results = [];
  try {
    fs.mkdirSync(path.join(directory, "web"));
    const target = path.join(directory, "web/teacher.js");
    for (const mutation of MUTATIONS) {
      if (source.split(mutation.from).length !== 2) throw new Error(`mutation anchor changed: ${mutation.name}`);
      fs.writeFileSync(target, source.replace(mutation.from, mutation.to));
      const child = spawnSync(process.execPath, [__filename, "--source-root", directory,
        "--stop-on", mutation.property], { encoding: "utf8", timeout: 120000 });
      if (child.error) throw child.error;
      if (![0, 1].includes(child.status)) throw new Error(`mutation process failed: ${child.stdout}\n${child.stderr}`);
      const broken = JSON.parse(child.stdout);
      const witness = broken.bounded.violations.find((entry) => entry.property === mutation.property);
      results.push({ name: mutation.name, property: mutation.property,
        exit_code: child.status, detected: child.status === 1 && broken.status === "FAIL" && Boolean(witness), witness });
    }
    fs.writeFileSync(target, source);
    const restored = await check(fs.readFileSync(target, "utf8"));
    const restoredMatches = JSON.stringify(restored) === JSON.stringify(baseline);
    return { status: results.every((entry) => entry.detected) && restoredMatches ? "PASS" : "ERROR",
      results, restored: restored.status, restored_matches: restoredMatches };
  } finally {
    const resolved = path.resolve(directory);
    if (path.dirname(resolved) !== path.resolve(os.tmpdir()) ||
        !path.basename(resolved).startsWith("teacher-microphone-")) throw new Error("unsafe scratch directory");
    fs.rmSync(resolved, { recursive: true, force: true });
  }
}

async function main() {
  const began = process.hrtime.bigint();
  const options = { root: process.cwd(), mutation: false };
  for (let i = 2; i < process.argv.length; i++) {
    const option = process.argv[i];
    if (option === "--mutation-check") options.mutation = true;
    else if (["--source-root", "--json", "--stop-on"].includes(option) && process.argv[i + 1]) {
      options[option === "--source-root" ? "root" : option.slice(2)] = process.argv[++i];
    } else throw new Error(`invalid argument: ${option}`);
  }
  const source = fs.readFileSync(path.join(path.resolve(options.root), "web/teacher.js"), "utf8").replace(/\r\n/g, "\n");
  if (options["stop-on"] && !MUTATIONS.some((entry) => entry.property === options["stop-on"])) {
    throw new Error(`unknown property: ${options["stop-on"]}`);
  }
  const bounded = await check(source, options["stop-on"]);
  const report = { status: bounded.status, sync_commit: SYNC_COMMIT, node: process.version,
    source_sha256: crypto.createHash("sha256").update(source).digest("hex"),
    bounds: { initial_states: 4, selections: 2, starts: 1,
      selection_devices: DEVICES,
      completions: "every success/failure order of pending getUserMedia/resume promises" }, bounded };
  if (options.mutation) {
    report.mutations = await mutations(source, bounded);
    if (report.mutations.status !== "PASS") report.status = "ERROR";
  }
  report.elapsed_seconds = Number(process.hrtime.bigint() - began) / 1e9;
  outputReport(report);
}

function outputReport(report) {
  const index = process.argv.indexOf("--json");
  const destination = index >= 0 ? process.argv[index + 1] : null;
  if (destination) {
    try {
      fs.mkdirSync(path.dirname(path.resolve(destination)), { recursive: true });
      fs.writeFileSync(destination, JSON.stringify(report, null, 2) + "\n");
    } catch (error) {
      report.status = "ERROR";
      report.output_error = error.message;
    }
  }
  const output = JSON.stringify(report, null, 2) + "\n";
  process.stdout.write(output);
  process.exitCode = report.status === "PASS" ? 0 : report.status === "FAIL" ? 1 : 2;
}

main().catch((error) => {
  outputReport({ status: "ERROR", error: error.stack });
});
