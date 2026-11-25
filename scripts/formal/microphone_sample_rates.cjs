"use strict";

const fs = require("node:fs");
const { page } = require("./teacher_audio.cjs");

const TARGET = 16000;
const PACKET = 1600;
const RATES = [8000, 16000, 22050, 32000, 44100, 48000, 88200, 96000];
const OFFSETS = [-2, -1, 0, 1, 2];

// One reproducible Float32 signal with changing slopes, both signs and clipped peaks.
function microphone(i) {
  const bits = Math.imul(i ^ (i >>> 3), 2654435761) >>> 16;
  return Math.fround((bits - 32768) / 24576);
}

function expectedSample(k, rate) {
  // Locate the kth output on the original input clock using integer arithmetic.
  const numerator = k * rate;
  const left = Math.floor(numerator / TARGET);
  const rightWeight = numerator % TARGET;
  const value = (microphone(left) * (TARGET - rightWeight) +
                 microphone(left + 1) * rightWeight) / TARGET;
  const clipped = Math.max(-1, Math.min(1, value));
  return Math.trunc(clipped * (clipped < 0 ? 32768 : 32767));
}

function samplesForPacket(number, rate) {
  // Linear interpolation needs the sample on the right of the last output position.
  return Math.floor((number * PACKET - 1) * rate / TARGET) + 2;
}

async function checkCase(root, rate, first, second) {
  let received = 0;
  let consumed = 0;
  let violation;
  function fail(property, expected, actual, outputSample) {
    violation ??= { property, rate, offsets: [first, second], inputSamples: consumed,
      outputSample, expected, actual };
  }
  const teacher = await page(root, (bytes) => {
    if (bytes.length !== 2 * PACKET) {
      fail("packet_bytes", 2 * PACKET, bytes.length, received * PACKET);
    } else {
      for (let i = 0; i < PACKET; i++) {
        const k = received * PACKET + i;
        const actual = bytes.readInt16LE(2 * i);
        const expected = expectedSample(k, rate);
        if (Math.abs(actual - expected) > 1) {
          fail("pcm_sample", expected, actual, k);
          break;
        }
      }
    }
    received++;
  }, undefined, rate);
  await teacher.click("start");
  const cuts = [samplesForPacket(1, rate) + first, samplesForPacket(2, rate) + second,
    samplesForPacket(3, rate)];
  for (const end of cuts) {
    while (consumed < end && !violation) {
      const start = consumed;
      consumed = Math.min(end, start + 128);
      teacher.pump(consumed - start, (i) => microphone(start + i));
      const available = Math.ceil((consumed - 1) * TARGET / rate);
      const due = Math.floor(available / PACKET);
      if (received !== due) fail("packet_count", due, received, Math.min(due, received) * PACKET);
    }
  }
  return violation;
}

async function bounded(root, only) {
  let cases = 0;
  let failed = 0;
  let firstViolation;
  for (const rate of RATES) {
    for (const first of OFFSETS) {
      for (const second of OFFSETS) {
        if (only && (rate !== only[0] || first !== only[1] || second !== only[2])) continue;
        const violation = await checkCase(root, rate, first, second);
        if (violation) {
          failed++;
          firstViolation ??= violation;
        }
        cases++;
      }
    }
  }
  if (!cases) throw new Error("case outside the declared bounds");
  return { status: failed ? "FAIL" : "PASS", cases, failed, firstViolation };
}

async function capture(request) {
  const { root, rate, seconds, input, output } = request;
  const samples = input ? fs.readFileSync(input) : null;
  const length = samples ? samples.length / 4 : seconds * rate;
  if (!Number.isSafeInteger(length) || length <= 0) throw new Error("invalid microphone input");
  const fd = fs.openSync(output, "w");
  try {
    let packets = 0;
    let firstViolation;
    const teacher = await page(root, (bytes) => {
      if (bytes.length !== 2 * PACKET) {
        firstViolation ??= { property: "packet_bytes", packet: packets, expected: 2 * PACKET,
          actual: bytes.length };
      }
      fs.writeSync(fd, bytes);
      packets++;
    }, undefined, rate);
    await teacher.click("start");
    teacher.pump(length, samples ? (i) => samples.readFloatLE(i * 4) : (i) => {
      const second = Math.floor(i / rate);
      return second % 5 < 4 ? (1001.25 + Math.floor(second / 5)) / 32767 : 0;
    });
    const expected = Math.floor(Math.ceil((length - 1) * TARGET / rate) / PACKET);
    if (expected !== packets) firstViolation ??= { property: "packet_count", expected, actual: packets };
    return { status: firstViolation ? "FAIL" : "PASS", rate, inputSamples: length, packets, firstViolation };
  } finally {
    fs.closeSync(fd);
  }
}

async function main() {
  const request = JSON.parse(fs.readFileSync(0, "utf8"));
  const report = request.output ? await capture(request) : await bounded(request.root, request.only);
  process.stdout.write(JSON.stringify(report));
  process.exitCode = Number(report.status === "FAIL");
}

main().catch((error) => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 2;
});
