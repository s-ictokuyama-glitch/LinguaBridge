"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const OPERATIONS = ["pause", "start", "suspend", "end_track"];
const CASES = {
  initial: [],
  paused: ["pause"],
  resume_suspended: ["pause", "suspend", "start"],
  resume_ended: ["pause", "end_track", "start"],
};
const RATE = 48000;
const FRAME_BYTES = 3200;

class Events {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) || [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  async emit(type, event = {}) {
    for (const callback of this.listeners.get(type) || []) await callback(event);
  }
}

function element() {
  return Object.assign(new Events(), {
    textContent: "", value: "", disabled: false, hidden: false, style: {},
    children: [], classList: { remove() {}, toggle() {} },
    appendChild(child) { this.children.push(child); },
  });
}

async function page(sourceRoot, writeFrame = () => {}) {
  const elements = new Map();
  const sockets = [];
  const contexts = [];
  const storage = new Map();
  const observed = { packets: 0, bytes: 0, nonzero: 0, matching: 0, wanted: 0, sizes: new Set() };
  let Processor;
  vm.runInNewContext(fs.readFileSync(path.join(sourceRoot, "web/audio-worklet.js"), "utf8"), {
    sampleRate: RATE,
    AudioWorkletProcessor: class {
      constructor() { this.port = { postMessage() {} }; }
    },
    registerProcessor(name, implementation) {
      if (name !== "pcm16-downsampler") throw new Error(`unknown processor: ${name}`);
      Processor = implementation;
    },
  }, { filename: "web/audio-worklet.js" });
  if (!Processor) throw new Error("worklet did not register");

  class Track {
    constructor() { this.readyState = "live"; }
    stop() { this.readyState = "ended"; }
  }
  class Stream {
    constructor() { this.track = new Track(); }
    getTracks() { return [this.track]; }
    getAudioTracks() { return [this.track]; }
  }
  class Context {
    constructor() {
      this.state = "suspended";
      this.sampleRate = RATE;
      this.destination = {};
      this.sources = [];
      this.nodes = [];
      this.audioWorklet = { addModule: async () => {} };
      contexts.push(this);
    }
    async resume() { this.state = "running"; }
    async suspend() { this.state = "suspended"; }
    createGain() {
      return { gain: { value: 1 }, destination: null,
        connect(destination) { this.destination = destination; } };
    }
    createMediaStreamSource(stream) {
      const source = { stream, target: null,
        connect(target) { this.target = target; },
        disconnect() { this.target = null; } };
      this.sources.push(source);
      return source;
    }
  }
  class Worklet {
    constructor(context, name) {
      if (name !== "pcm16-downsampler") throw new Error(`unknown node: ${name}`);
      this.context = context;
      this.port = { onmessage: null };
      this.processor = new Processor();
      this.processor.port.postMessage = (data) => this.port.onmessage?.({ data });
      this.output = null;
      this.active = true;
      context.nodes.push(this);
    }
    connect(output) { this.output = output; }
  }
  class Socket extends Events {
    static OPEN = 1;
    constructor() {
      super();
      this.readyState = Socket.OPEN;
      sockets.push(this);
    }
    send(data) {
      if (typeof data === "string") {
        const message = JSON.parse(data);
        if (message.type === "join") {
          this.message({ type: "joined", session_state: "idle", recording: false });
        } else if (message.type === "control") {
          const state = { start: "live", pause: "paused", end: "ended" }[message.action];
          this.message({ type: "session", state });
        }
        return;
      }
      const bytes = Buffer.from(new Uint8Array(data));
      observed.packets++;
      observed.bytes += bytes.length;
      observed.sizes.add(bytes.length);
      let nonzero = false;
      let matching = false;
      for (let offset = 0; offset + 1 < bytes.length; offset += 2) {
        const sample = bytes.readInt16LE(offset);
        nonzero ||= sample !== 0;
        matching ||= sample === observed.wanted;
      }
      observed.nonzero += Number(nonzero);
      observed.matching += Number(matching && observed.wanted !== 0);
      writeFrame(bytes);
    }
    message(payload) {
      for (const callback of this.listeners.get("message") || []) {
        callback({ data: JSON.stringify(payload) });
      }
    }
  }
  const sandbox = {
    document: {
      body: { dataset: {} },
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      },
      createElement: element,
    },
    window: { AudioContext: Context }, AudioWorkletNode: Worklet, WebSocket: Socket,
    navigator: { mediaDevices: {
      getUserMedia: async () => new Stream(),
      enumerateDevices: async () => [{ kind: "audioinput", deviceId: "default", label: "Fake microphone" }],
      addEventListener() {},
    } },
    localStorage: { getItem: (key) => storage.get(key) || null,
      setItem: (key, value) => storage.set(key, value) },
    location: { protocol: "http:", host: "127.0.0.1" },
    fetch: async () => ({ ok: true, json: async () => ({
      teacher_code: "654321", code: "123456", join_url: "http://127.0.0.1/" }) }),
    AbortSignal: { timeout: () => ({}) },
    setTimeout() {},
    Option: function(text, value) { return { text, value }; },
    QRCode: function() {},
  };
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(sourceRoot, "web/teacher.js"), "utf8");
  if (!/init\(\);\s*$/.test(source)) throw new Error("teacher init entry changed");
  vm.runInContext(source.replace(/init\(\);\s*$/, ""), sandbox, { filename: "web/teacher.js" });
  await vm.runInContext("init()", sandbox);
  if (sockets.length !== 1) throw new Error("teacher init did not connect");
  await sockets[0].emit("open");
  const state = () => vm.runInContext("state", sandbox);
  if (!state().joined) throw new Error("teacher did not join");

  function pump(samples, signal) {
    const channel = new Float32Array(128);
    const zero = new Float32Array(128);
    for (let position = 0; position < samples; position += 128) {
      const length = Math.min(128, samples - position);
      for (let i = 0; i < length; i++) channel[i] = signal(position + i);
      for (const context of contexts) {
        if (context.state !== "running") continue;
        for (const node of context.nodes) {
          if (!node.active || node.output?.destination !== context.destination) continue;
          const live = context.sources.some((source) => source.target === node &&
            source.stream.getAudioTracks().some((track) => track.readyState === "live"));
          const input = live ? channel : zero;
          node.active = node.processor.process([[length === 128 ? input : input.subarray(0, length)]]);
        }
      }
    }
  }

  return {
    observed, state, pump,
    async click(action) {
      const button = elements.get(`${action}-btn`);
      if (button.disabled) throw new Error(`disabled button: ${action}`);
      await button.emit("click");
    },
    async fault(operation) {
      if (operation === "suspend") await state().audioCtx.suspend();
      else state().micStream.getAudioTracks().forEach((track) => track.stop());
    },
    snapshot() {
      const current = state();
      return { audio_state: current.audioCtx?.state || null,
        tracks: current.micStream?.getAudioTracks().map((track) => track.readyState) || [],
        mic_ready: current.micReady, session_state: current.sessionState,
        packets: observed.packets, nonzero_packets: observed.nonzero,
        matching_packets: observed.matching, frame_sizes: [...observed.sizes] };
    },
  };
}

function legal(operations) {
  let session = "live";
  let context = "running";
  let track = "live";
  for (const operation of operations) {
    if (operation === "pause") {
      if (session !== "live") return false;
      session = "paused";
    } else if (operation === "start") {
      if (session !== "paused") return false;
      session = "live";
      context = "running";
      track = "live";
    } else if (operation === "suspend") {
      if (context !== "running") return false;
      context = "suspended";
    } else {
      if (track !== "live") return false;
      track = "ended";
    }
  }
  return true;
}

async function runCase(sourceRoot, operations) {
  const instance = await page(sourceRoot);
  const violations = [];
  const trace = [];
  let identity = 0;
  for (const operation of ["start", ...operations]) {
    trace.push(operation);
    if (operation === "suspend" || operation === "end_track") {
      await instance.fault(operation);
      continue;
    }
    await instance.click(operation);
    const before = instance.snapshot();
    const expectedSample = 1000 + ++identity;
    instance.observed.wanted = expectedSample;
    instance.pump(RATE / 5, () => (expectedSample + 0.25) / 32767);
    const after = instance.snapshot();
    const packets = after.packets - before.packets;
    const matching = after.matching_packets - before.matching_packets;
    const require = (condition, property, expected, actual) => {
      if (!condition) violations.push({ property, trace: [...trace], expected, actual });
    };
    if (operation === "start") {
      require(matching > 0, "capture_liveness", { new_pcm_sample: expectedSample, minimum_packets: 1 },
        { ...after, packets_since_start: packets, matching_since_start: matching });
    } else {
      require(packets === 0, "inactive_quiet", { packets: 0 }, { packets });
    }
    require(after.frame_sizes.every((size) => size === FRAME_BYTES), "pcm_framing",
      { frame_bytes: FRAME_BYTES }, { frame_sizes: after.frame_sizes });
  }
  return { status: violations.length ? "FAIL" : "PASS", trace: ["start", ...operations],
    violations, observation: instance.snapshot() };
}

function* histories(depth, prefix = []) {
  yield prefix;
  if (prefix.length === depth) return;
  for (const operation of OPERATIONS) yield* histories(depth, [...prefix, operation]);
}

async function bounded(sourceRoot, onlyCase) {
  const result = { attempted: 0, legal: 0, passed: 0, failed: 0, violations: [] };
  const traces = onlyCase ? [CASES[onlyCase]] : [...histories(3)].sort((a, b) => a.length - b.length);
  for (const operations of traces) {
    result.attempted++;
    if (!legal(operations)) continue;
    result.legal++;
    const outcome = await runCase(sourceRoot, operations);
    result[outcome.status === "PASS" ? "passed" : "failed"]++;
    result.violations.push(...outcome.violations);
  }
  return result;
}

async function sustained(sourceRoot, pcm, seconds) {
  fs.mkdirSync(path.dirname(path.resolve(pcm)), { recursive: true });
  const descriptor = fs.openSync(pcm, "w");
  try {
    const instance = await page(sourceRoot, (bytes) => fs.writeSync(descriptor, bytes));
    await instance.click("start");
    instance.pump(seconds * RATE, (sample) => {
      const second = sample / RATE;
      const identity = Math.floor(second / 5) + 1;
      return second % 5 < 4 ? (1000 + identity + 0.25) / 32767 : 0;
    });
    const observation = instance.snapshot();
    const expected = { packets: seconds * 10, bytes: seconds * 32000 };
    const actual = { packets: instance.observed.packets, bytes: instance.observed.bytes };
    const passed = actual.packets === expected.packets && actual.bytes === expected.bytes &&
      observation.frame_sizes.every((size) => size === FRAME_BYTES);
    return { status: passed ? "PASS" : "FAIL", seconds, rate: RATE, block_samples: 128,
      planned_utterances: seconds / 5, ...actual, expected,
      frame_sizes: observation.frame_sizes,
      violations: passed ? [] : [{ property: "pcm_framing", trace: [`capture_${seconds}_seconds`], expected, actual }] };
  } finally {
    fs.closeSync(descriptor);
  }
}

async function main() {
  const options = { "source-root": process.cwd(), seconds: "3000" };
  for (let index = 2; index < process.argv.length; index += 2) {
    const argument = process.argv[index];
    if (!["--source-root", "--json", "--pcm", "--seconds", "--case"].includes(argument) ||
        !process.argv[index + 1]) throw new Error(`invalid argument: ${argument}`);
    options[argument.slice(2)] = process.argv[index + 1];
  }
  if (options.case && !Object.hasOwn(CASES, options.case)) throw new Error(`unknown case: ${options.case}`);
  const seconds = Number(options.seconds);
  if (!Number.isInteger(seconds) || seconds <= 0 || seconds % 5 !== 0) {
    throw new Error("seconds must be a positive multiple of 5");
  }
  const sourceRoot = path.resolve(options["source-root"]);
  const result = { status: "PASS", bounded: await bounded(sourceRoot, options.case) };
  if (options.pcm) result.sustained = await sustained(sourceRoot, options.pcm, seconds);
  if (result.bounded.failed || result.sustained?.status === "FAIL") result.status = "FAIL";
  const output = JSON.stringify(result, null, 2) + "\n";
  if (options.json) {
    fs.mkdirSync(path.dirname(path.resolve(options.json)), { recursive: true });
    fs.writeFileSync(options.json, output);
  }
  process.stdout.write(output);
  process.exitCode = result.status === "PASS" ? 0 : 1;
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ status: "ERROR", error: error.stack }, null, 2) + "\n");
  process.exitCode = 2;
});
