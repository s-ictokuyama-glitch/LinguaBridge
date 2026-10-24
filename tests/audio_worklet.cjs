const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/audio-worklet.js"), "utf8");

function convert(rate, input, blocks, emptyInputs = false) {
  const chunks = [];
  let Processor;
  vm.runInNewContext(source, {
    sampleRate: rate,
    AudioWorkletProcessor: class {
      port = { postMessage: (buffer) => chunks.push(new Int16Array(buffer)) };
    },
    registerProcessor(name, implementation) {
      assert.equal(name, "pcm16-downsampler");
      Processor = implementation;
    },
  });
  const processor = new Processor();
  let offset = 0;
  let block = 0;
  while (offset < input.length) {
    const end = Math.min(offset + blocks[block++ % blocks.length], input.length);
    if (emptyInputs) {
      for (const inputs of [[], [[]], [[new Float32Array(0)]]]) {
        assert.equal(processor.process(inputs), true);
      }
    }
    assert.equal(processor.process([[input.subarray(offset, end)]]), true);
    offset = end;
  }
  const output = new Int16Array(chunks.length * 1600);
  chunks.forEach((chunk, index) => {
    assert.equal(chunk.length, 1600);
    output.set(chunk, index * 1600);
  });
  return output;
}

for (const rate of [44100, 48000]) {
  test(`${rate} Hz emits 960000 samples for 60 seconds`, () => {
    const input = new Float32Array(rate * 60);
    const output = convert(rate, input, [128]);
    assert.equal(output.length, 960000);
    assert.ok(output.every((sample) => sample === 0));
  });
}

for (const rate of [16000, 44100, 48000]) {
  test(`${rate} Hz preserves waveform across block boundaries`, () => {
    const input = Float32Array.from({ length: rate + 1 }, (_, i) =>
      0.7 * Math.sin(2 * Math.PI * 437 * i / rate));
    const expected = Int16Array.from({ length: 16000 }, (_, i) => {
      const position = i * rate / 16000;
      const left = Math.floor(position);
      const fraction = position - left;
      const sample = input[left] * (1 - fraction) + input[left + 1] * fraction;
      return sample * (sample < 0 ? 0x8000 : 0x7fff);
    });
    for (const blocks of [[128], [1], [1, 2, 7, 127, 3, 256, 5]]) {
      const actual = convert(rate, input, blocks, true);
      assert.equal(actual.length, expected.length, `blocks ${blocks}`);
      for (let i = 0; i < expected.length; i++) {
        assert.ok(Math.abs(actual[i] - expected[i]) <= 1,
          `blocks ${blocks}, sample ${i}: ${actual[i]} vs ${expected[i]}`);
      }
    }
  });

  test(`${rate} Hz clips PCM16 and keeps posted chunks independent`, () => {
    const input = Float32Array.from({ length: rate + 1 }, (_, i) =>
      i < rate / 2 ? -2 : 2);
    const output = convert(rate, input, [128]);
    assert.equal(output.length, 16000);
    assert.ok(output.subarray(0, 7999).every((sample) => sample === -32768));
    assert.ok(output.subarray(8001).every((sample) => sample === 32767));
  });
}
