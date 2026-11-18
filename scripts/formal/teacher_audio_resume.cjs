"use strict";

const { deflateSync } = require("node:zlib");
const { page } = require("./teacher_audio.cjs");

// One worklet render quantum is 128 samples at 48 kHz. 75 quanta fill two packets.
async function capture(root, [action, period, quanta, delivery]) {
  const phases = { before: [], stopped: [], first: [], following: [] };
  let phase = "before";
  const pending = [];
  let hold = delivery === "queued";
  const instance = await page(root, (frame) => {
    if (frame.length !== 3200) throw new Error(`unexpected packet size: ${frame.length}`);
    phases[phase].push(frame);
  }, (message) => {
    if (hold) pending.push(message);
    else message();
  });
  const pump = (count, marker) => instance.pump(count * 128, () => marker ? (marker + 0.25) / 32767 : 0);
  async function click(button, expected) {
    await instance.click(button);
    if (instance.state().sessionState !== expected) throw new Error(`teacher did not enter ${expected}`);
  }
  await click("start", "live");
  if (period === "before") pump(quanta, 1000);
  await click(action, action === "pause" ? "paused" : "ended");
  phase = "stopped";
  if (period === "stopped") pump(quanta, 2000);
  await click("start", "live");
  phase = "first";
  hold = false;
  for (const message of pending) message();
  for (const [name, marker] of [["first", 3000], ["following", 4000]]) {
    phase = name;
    pump(150, marker);
    pump(450, 0);
  }
  // Preserve the bytes from Socket.send. All packet boundaries are checked above.
  return Object.fromEntries(Object.entries(phases).map(([name, frames]) =>
    [name, deflateSync(Buffer.concat(frames)).toString("base64")]));
}

async function main() {
  const root = process.argv[2];
  let input = "";
  for await (const chunk of process.stdin) input += chunk;
  for (const values of JSON.parse(input)) {
    process.stdout.write(JSON.stringify(await capture(root, values)) + "\n");
  }
}

main().catch((error) => { console.error(error); process.exitCode = 2; });
