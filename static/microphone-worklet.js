// Convert capture samples to 100 ms packets of 16 kHz mono float32 PCM.
class MicrophoneCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.position = 0;
    this.nextSample = 0;
    this.samples = [];
    this.previous = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) {
      while (this.nextSample <= this.position) {
        const fraction = this.nextSample - (this.position - 1);
        this.samples.push(this.previous + (sample - this.previous) * fraction);
        this.nextSample += sampleRate / 16000;
        if (this.samples.length === 1600) {
          const bytes = new ArrayBuffer(6400);
          const view = new DataView(bytes);
          this.samples.forEach((value, index) => view.setFloat32(index * 4, value, true));
          this.port.postMessage(bytes, [bytes]);
          this.samples = [];
        }
      }
      this.previous = sample;
      this.position++;
    }
    return true;
  }
}
registerProcessor("microphone-capture", MicrophoneCapture);
