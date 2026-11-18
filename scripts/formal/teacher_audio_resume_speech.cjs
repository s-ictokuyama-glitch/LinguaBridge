"use strict";

const fs = require("node:fs");
const { page } = require("./teacher_audio.cjs");

async function capture(action, muted, fixtures) {
  const packets = [];
  const teacher = await page(process.cwd(), (bytes) => {
    if (bytes.length !== 3200) throw new Error("expected 100 ms PCM16 packets");
    packets.push(bytes);
  });

  function play(pcm) {
    // Repeat each 16 kHz fixture sample on three 48 kHz microphone ticks.
    // A quarter LSB keeps float32 rounding inside the original PCM16 bin.
    teacher.pump(pcm.length / 2 * 3, (i) => {
      const sample = pcm.readInt16LE(2 * Math.floor(i / 3));
      if (!sample) return 0;
      return sample < 0 ? (sample - 0.25) / 32768 : (sample + 0.25) / 32767;
    });
  }

  async function click(button, expectedState) {
    await teacher.click(button);
    if (teacher.snapshot().session_state !== expectedState) {
      throw new Error(`clicking ${button} did not enter ${expectedState}`);
    }
  }

  await click("start", "live");
  await click(action, action === "end" ? "ended" : "paused");
  play(muted ? Buffer.alloc(fixtures.inactive.length) : fixtures.inactive);
  if (packets.length) throw new Error("fixture sent a complete packet before restart");
  await click("start", "live");

  const result = {};
  for (const phase of ["first", "following"]) {
    play(fixtures[phase]);
    // Flush the last packet of speech through the real Worklet before switching fixtures.
    teacher.pump(48000 / 5, () => 0);
    result[phase] = Buffer.concat(packets.splice(0)).toString("base64");
  }
  return result;
}

async function main() {
  const { action, fixtures } = JSON.parse(fs.readFileSync(0, "utf8"));
  if (!["pause", "end"].includes(action)) throw new Error("invalid boundary");
  for (const name of Object.keys(fixtures)) fixtures[name] = Buffer.from(fixtures[name], "base64");
  const control = await capture(action, true, fixtures);
  const spoken = await capture(action, false, fixtures);
  process.stdout.write(JSON.stringify({ control, spoken }));
}

main().catch((error) => {
  process.stderr.write(String(error.stack) + "\n");
  process.exitCode = 2;
});
