// Provider sends raw mono Float32 PCM; no WAV container needs decoding.
const MAX_QUEUED_SECONDS = 60;  // Sanity bound only.
export class StreamAudioPlayer {
  constructor(onActiveChange) {
    this.onActiveChange = onActiveChange;
    this.context = null;
    this.sources = new Set();
    this.sampleRate = null;
    this.pendingBytes = new Uint8Array();
    this.hasAudio = false;
    this.nextStartTime = 0;
    this.streamEnded = true;
    this.replyId = 0;
    this.replyPlayed = 0;  // Seconds of the current reply fully played.
    this.output = null;  // Optional node replies play through (e.g. a level meter); else the speakers.
  }

  // Start counting played audio for a new reply (barge-in reports this count).
  markReply() {
    this.replyId += 1;
    this.replyPlayed = 0;
  }

  async unlock() {
    // Start call is a user gesture: unlock before the provider responds.
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    this.context ??= new AudioContext();
    await this.context.resume();
    if (this.context.state !== "running") throw new Error("Audio playback is blocked.");
  }

  start({sample_rate, encoding}) {
    if (!this.context || this.context.state !== "running") throw new Error("Audio playback is blocked.");
    this.reset();
    if (encoding !== "pcm_f32le" || !Number.isInteger(sample_rate) || sample_rate < 8000 || sample_rate > 96000) {
      throw new Error("Unsupported speech format.");
    }
    this.sampleRate = sample_rate;
    this.streamEnded = false;
    this.nextStartTime = this.context.currentTime + 0.08;
  }

  enqueue(bytes) {
    if (this.streamEnded) throw new Error("Audio arrived outside a stream.");
    const combined = new Uint8Array(this.pendingBytes.length + bytes.byteLength);
    combined.set(this.pendingBytes);
    combined.set(new Uint8Array(bytes), this.pendingBytes.length);
    const sampleCount = Math.floor(combined.length / 4);
    this.pendingBytes = combined.slice(sampleCount * 4);
    if (!sampleCount) return null;
    const view = new DataView(combined.buffer);
    const samples = new Float32Array(sampleCount);
    for (let index = 0; index < sampleCount; index++) {
      const sample = view.getFloat32(index * 4, true);
      if (!Number.isFinite(sample)) throw new Error("Invalid PCM sample.");
      samples[index] = sample;
    }
    const buffer = this.context.createBuffer(1, sampleCount, this.sampleRate);
    buffer.copyToChannel(samples, 0);
    this.hasAudio = true;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.replyId = this.replyId;
    source.connect(this.output ?? this.context.destination);
    this.sources.add(source);
    this.onActiveChange(true);
    source.onended = () => {
      if (source.replyId === this.replyId) this.replyPlayed += buffer.duration;
      source.disconnect();
      this.sources.delete(source);
      this.finishIfDrained();
    };
    // Schedule in order; a late packet starts after a small gap rather than in the past.
    // TTS runs faster than real time, so replies queue well ahead; barge-in flushes them.
    const start = Math.max(this.nextStartTime, this.context.currentTime + 0.02);
    if (start - this.context.currentTime > MAX_QUEUED_SECONDS) {
      this.sources.delete(source);
      source.disconnect();
      throw new Error("Too much audio queued.");
    }
    source.start(start);
    source.scheduledStart = start;
    this.nextStartTime = start + buffer.duration;
    return start;  // AudioContext time when this audio begins playing.
  }

  finish() {
    if (!this.hasAudio) throw new Error("Speech stream contained no playable audio.");
    if (this.pendingBytes.length) throw new Error("Incomplete PCM sample.");
    this.streamEnded = true;
    this.finishIfDrained();
  }

  finishIfDrained() {
    if (this.sources.size === 0) {
      this.onActiveChange(false);
    }
  }

  // Barge-in: silence everything queued now, keep the stream open for the next
  // reply, and return how many seconds of the current reply the user heard.
  interrupt() {
    const now = this.context?.currentTime ?? 0;
    let played = this.replyPlayed;
    for (const source of this.sources) {
      if (source.replyId === this.replyId) {
        played += Math.min(source.buffer.duration, Math.max(0, now - source.scheduledStart));
      }
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
    this.pendingBytes = new Uint8Array();
    this.nextStartTime = now;
    this.replyPlayed = 0;
    this.onActiveChange(false);
    return played;
  }

  reset() {
    for (const source of this.sources) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
    this.sampleRate = null;
    this.pendingBytes = new Uint8Array();
    this.hasAudio = false;
    this.streamEnded = true;
    this.onActiveChange(false);
  }

  close() {
    this.reset();
    if (this.context) void this.context.close();
  }
}
