// マイク入力（AudioContextのネイティブレート、通常48kHz）を
// 16kHz mono PCM16 に変換し、1600サンプル（100ms / 3200byte）ごとに
// ArrayBuffer で main スレッドへ postMessage する。
const TARGET_RATE = 16000;
const CHUNK_SAMPLES = 1600; // 100ms

class PCM16Downsampler extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / TARGET_RATE; // sampleRate は Worklet のグローバル
    this.inputBuffer = new Float32Array(0);
    this.pos = 0.0; // inputBuffer 上の読み出し位置（小数、線形補間）
    this.out = new Int16Array(CHUNK_SAMPLES);
    this.outLen = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) {
      return true;
    }
    const buf = new Float32Array(this.inputBuffer.length + channel.length);
    buf.set(this.inputBuffer);
    buf.set(channel, this.inputBuffer.length);

    let pos = this.pos;
    while (pos + 1 < buf.length) {
      const i = Math.floor(pos);
      const frac = pos - i;
      const sample = buf[i] * (1 - frac) + buf[i + 1] * frac;
      const clamped = Math.max(-1, Math.min(1, sample));
      this.out[this.outLen++] = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
      if (this.outLen === CHUNK_SAMPLES) {
        this.port.postMessage(this.out.buffer.slice(0));
        this.outLen = 0;
      }
      pos += this.step;
    }
    const keepFrom = Math.floor(pos);
    this.inputBuffer = buf.slice(keepFrom);
    this.pos = pos - keepFrom;
    return true;
  }
}

registerProcessor("pcm16-downsampler", PCM16Downsampler);
