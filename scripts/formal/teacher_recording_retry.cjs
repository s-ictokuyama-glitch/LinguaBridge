"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const readline = require("node:readline");

class Events {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, callback) {
    this.listeners.set(type, [...(this.listeners.get(type) || []), callback]);
  }
  async emit(type, data = {}) {
    for (const callback of this.listeners.get(type) || []) await callback(data);
  }
}

function element() {
  return Object.assign(new Events(), {
    textContent: "", disabled: false, hidden: false, value: "", children: [], style: {},
    classList: { remove() {}, toggle() {} },
    appendChild(child) { this.children.push(child); },
  });
}

async function page(sourceRoot) {
  const elements = new Map(), sockets = [], timers = [], outgoing = [];
  class Socket extends Events {
    static OPEN = 1;
    constructor() { super(); this.readyState = 1; sockets.push(this); }
    send(data) { outgoing.push(JSON.parse(data)); }
    async close() { this.readyState = 3; await this.emit("close", { code: 1006 }); }
  }
  class Context {
    constructor() { this.state = "suspended"; this.audioWorklet = { addModule: async () => {} }; }
    async resume() { this.state = "running"; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  }
  const sandbox = {
    document: { body: { dataset: {} }, createElement: element,
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
      } },
    WebSocket: Socket, window: { AudioContext: Context },
    AudioWorkletNode: class { constructor() { this.port = {}; } connect() {} },
    navigator: { mediaDevices: {
      async getUserMedia() {
        const track = { readyState: "live", stop() { this.readyState = "ended"; } };
        return { getTracks: () => [track], getAudioTracks: () => [track] };
      },
      enumerateDevices: async () => [], addEventListener() {},
    } },
    localStorage: { getItem: () => null, setItem() {} },
    location: { protocol: "http:", host: "127.0.0.1" },
    fetch: async () => ({ ok: true, json: async () => ({
      teacher_code: "604831", code: "4831", join_url: "http://127.0.0.1/" }) }),
    AbortSignal: { timeout: () => ({}) },
    setTimeout(fn, delay) { timers.push({ fn, delay }); },
    Option: function(text, value) { return { text, value }; }, QRCode: function() {},
  };
  vm.createContext(sandbox);
  const source = fs.readFileSync(path.join(sourceRoot, "web/teacher.js"), "utf8");
  if (!/init\(\);\s*$/.test(source)) throw new Error("teacher init entry changed");
  vm.runInContext(source.replace(/init\(\);\s*$/, ""), sandbox, { filename: "web/teacher.js" });
  await vm.runInContext("init()", sandbox);
  await sockets.at(-1).emit("open");
  return {
    async run(command) {
      if (command.op === "message") {
        await sockets.at(-1).emit("message", { data: JSON.stringify(command.payload) });
      } else if (command.op === "click") {
        const button = elements.get(command.id);
        if (!button.disabled) await button.emit("click");
      } else if (command.op === "disconnect") {
        await sockets.at(-1).close();
      } else if (command.op === "reconnect") {
        const index = timers.findIndex(timer => timer.delay === 2000);
        if (index < 0) throw new Error("reconnect timer not scheduled");
        timers.splice(index, 1)[0].fn();
        await sockets.at(-1).emit("open");
      } else if (command.op !== "snapshot") {
        throw new Error(`unknown page operation: ${command.op}`);
      }
      return this.snapshot();
    },
    snapshot() {
      return {
        end_disabled: elements.get("end-btn").disabled,
        start_disabled: elements.get("start-btn").disabled,
        warning: elements.get("mic-status").textContent,
        page_state: sandbox.document.body.dataset.state,
        outgoing: outgoing.splice(0),
      };
    },
  };
}

let current;
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
(async () => {
  for await (const line of input) {
    try {
      const command = JSON.parse(line);
      if (command.op === "reset") {
        current = await page(process.argv[2]);
        process.stdout.write(JSON.stringify(current.snapshot()) + "\n");
      } else {
        process.stdout.write(JSON.stringify(await current.run(command)) + "\n");
      }
    } catch (error) {
      process.stdout.write(JSON.stringify({ harness_error: error.stack }) + "\n");
    }
  }
})().catch(error => { console.error(error); process.exitCode = 2; });
