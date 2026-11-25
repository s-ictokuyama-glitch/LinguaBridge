// A virtual microphone in a real browser. Playback advances even when the page
// has never requested a track. Only product code can connect it to the Worklet.
(() => {
  let clock;
  let output;
  async function microphone() {
    if (!clock) {
      clock = new AudioContext();
      output = clock.createMediaStreamDestination();
    }
    await clock.resume();
    return output;
  }
  navigator.mediaDevices.getUserMedia = async () => (await microphone()).stream.clone();
  window.playLessonPCM = async (base64) => {
    const destination = await microphone();
    const bytes = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
    const samples = new Int16Array(bytes.buffer);
    const buffer = clock.createBuffer(1, samples.length + 22400, 16000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;
    const source = clock.createBufferSource();
    source.buffer = buffer;
    source.connect(destination);
    const ended = new Promise(resolve => { source.onended = resolve; });
    // Leave 250 ms for the newly connected audio graph before the teacher speaks.
    source.start(clock.currentTime + 0.25);
    await ended;
    source.disconnect();
  };

  window.probe = { packets: 0, voiced: 0, joins: 0, closes: [], barrier: 0 };
  const NativeSocket = WebSocket;
  window.WebSocket = class extends NativeSocket {
    constructor(...args) {
      super(...args);
      this.addEventListener("close", event => probe.closes.push(event.code));
      this.addEventListener("message", event => {
        const message = JSON.parse(event.data);
        if (message.type === "joined") probe.joins++;
        if (message.type === "probe_barrier") probe.barrier = message.id;
      });
    }
    send(data) {
      if (data instanceof ArrayBuffer) {
        probe.packets++;
        if (new Int16Array(data).some(sample => Math.abs(sample) > 300)) probe.voiced++;
      }
      return super.send(data);
    }
  };
})();
