// The server removes the WAV header. Each binary message contains signed PCM.
export class StreamAudioPlayer {
  constructor(onActiveChange) {
    this.onActiveChange = onActiveChange;
    this.context = null;
    this.sources = new Set();
    this.format = null;
    this.nextStartTime = 0;
    this.streamEnded = true;
  }

  async unlock() {
    // Record is a user gesture, so unlock playback before the provider responds.
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    this.context ??= new AudioContext();
    await this.context.resume();
    if (this.context.state !== "running") throw new Error("Audio playback is blocked.");
  }

  start(format) {
    if (!this.context || this.context.state !== "running") throw new Error("Audio playback is blocked.");
    if (format.encoding !== "pcm_s16le" || ![1, 2].includes(format.channels) ||
        !Number.isInteger(format.sample_rate) || format.sample_rate < 8000 || format.sample_rate > 96000) {
      throw new Error("Unsupported audio format.");
    }
    this.reset();
    this.format = format;
    this.streamEnded = false;
    this.nextStartTime = this.context.currentTime + 0.08;
    this.onActiveChange(true);
  }

  enqueue(bytes) {
    if (!this.format || this.streamEnded) throw new Error("Audio arrived outside a stream.");
    const {channels, sample_rate: sampleRate} = this.format;
    if (!bytes.byteLength || bytes.byteLength % (channels * 2)) throw new Error("Incomplete audio samples.");
    const frames = bytes.byteLength / (channels * 2);
    const buffer = this.context.createBuffer(channels, frames, sampleRate);
    const samples = new DataView(bytes);
    for (let channel = 0; channel < channels; channel++) {
      const output = buffer.getChannelData(channel);
      for (let frame = 0; frame < frames; frame++) {
        output[frame] = samples.getInt16((frame * channels + channel) * 2, true) / 32768;
      }
    }
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.sources.add(source);
    source.onended = () => {
      source.disconnect();
      this.sources.delete(source);
      this.finishIfDrained();
    };
    // Schedule in order; a late packet starts after a small gap rather than in the past.
    const start = Math.max(this.nextStartTime, this.context.currentTime + 0.02);
    source.start(start);
    this.nextStartTime = start + buffer.duration;
  }

  finish() {
    this.streamEnded = true;
    this.finishIfDrained();
  }

  finishIfDrained() {
    if (this.streamEnded && this.sources.size === 0) {
      this.format = null;
      this.onActiveChange(false);
    }
  }

  reset() {
    for (const source of this.sources) {
      source.onended = null;
      source.stop();
      source.disconnect();
    }
    this.sources.clear();
    this.format = null;
    this.streamEnded = true;
    this.onActiveChange(false);
  }

  close() {
    this.reset();
    if (this.context) void this.context.close();
  }
}
