// One WebRTC call to VALR, without any page: the old call page (voice-webrtc.js) and the
// inline "Talk here" panel (valr-call.js) both drive this.
//
// Microphone and reply audio travel as Opus media; call events come on a data channel
// with the same messages as the WebSocket transport. The server paces reply audio and
// works out what was heard from the playout delay this page reports, so the page only
// plays the remote track.
//
// iPhone (Safari and the Home Screen app): `start()` must run inside the tap. The audio
// context is resumed and the speaker element primed before the first await, and the
// microphone prompt (getUserMedia) is that first await. The route (speaker, AirPods) is
// iOS's choice; `navigator.audioSession` asks for a call-style session where it exists.
import {playoutDelay} from "./playout-delay.js";

const PLAYOUT_REPORT_MS = 500;
const GATHER_TIMEOUT_MS = 2000;
const MIC = {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true};

// Why a call could not start, in words for the owner. `kind` picks the panel's help.
export function describeError(error) {
  const name = error?.name || "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return {kind: "mic-denied", text: "Microphone is off for VALR."};
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return {kind: "no-mic", text: "No microphone found. Plug one in or use Call my phone."};
  }
  if (name === "NotReadableError" || name === "AbortError") {
    return {kind: "mic-busy", text: "Another app is using the microphone. Close it and try again."};
  }
  if (name === "Unsupported") return {kind: "unsupported", text: error.message};
  return {kind: "failed", text: error?.message || "Could not start the call. Try again."};
}

export function supported(scope = globalThis) {
  return Boolean(scope.RTCPeerConnection && scope.navigator?.mediaDevices?.getUserMedia);
}

// Reveal a spoken segment word by word across its audio. The server says when that
// audio's first sample is (or was) heard here, so words already due appear at once.
export class CaptionScheduler {
  constructor(onWord, timers = globalThis) {
    this.onWord = onWord;
    this.timers = timers;
    this.pending = new Set();
  }

  schedule(caption, line) {
    const words = String(caption.text || "").split(/\s+/).filter(Boolean);
    words.forEach((word, index) => {
      const due = caption.starts_in_s + (caption.seconds * index) / words.length;
      const timer = this.timers.setTimeout(() => {
        this.pending.delete(timer);
        this.onWord(line, word);
      }, Math.max(0, due * 1000));
      this.pending.add(timer);
    });
  }

  cancel() {
    this.pending.forEach(timer => this.timers.clearTimeout(timer));
    this.pending.clear();
  }
}

function gatheringComplete(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise(resolve => {
    const done = () => {
      if (pc.iceGatheringState !== "complete") return;
      pc.removeEventListener("icegatheringstatechange", done);
      resolve();
    };
    pc.addEventListener("icegatheringstatechange", done);
    setTimeout(resolve, GATHER_TIMEOUT_MS);  // Send what has been gathered so far.
  });
}

async function jsonOrError(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
  return body;
}

function analyser(context, stream) {
  const node = context.createAnalyser();
  node.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(node);
  return node;
}

// handlers: {event(message), ended(reason), connection(pc), meters({mic, voice}), paused(reason)}
export class WebRtcCall {
  constructor({intent = null, ref = "", csrf = "", audio = null, handlers = {}} = {}) {
    this.intent = intent;
    this.ref = ref;
    this.csrf = csrf;
    this.audio = audio;
    this.handlers = handlers;
    this.pc = null;
    this.channel = null;
    this.microphone = null;
    this.meters = null;
    this.stats = {};
    this.statsTimer = null;
    this.callId = "";
    this.mode = "";
    this.muted = false;
    this.paused = false;
    this.ended = false;
  }

  emit(name, ...args) {
    try { this.handlers[name]?.(...args); } catch (error) { console.warn(error); }
  }

  get track() {
    return this.microphone?.getAudioTracks()[0] ?? null;
  }

  // Inside the tap: nothing may be awaited before getUserMedia.
  async start() {
    if (!supported()) {
      const error = new Error("This browser can't make calls. Use Call my phone instead.");
      error.name = "Unsupported";
      throw error;
    }
    try { if (navigator.audioSession) navigator.audioSession.type = "play-and-record"; } catch {}
    const audio = this.audio ??= new Audio();
    audio.autoplay = true;
    audio.setAttribute?.("playsinline", "");
    audio.play?.().catch(() => {});  // Primes playback inside the gesture (iOS).
    const Context = globalThis.AudioContext || globalThis.webkitAudioContext;
    this.meters = Context ? new Context() : null;  // Level meters only, never played.
    this.meters?.resume().catch(() => {});
    this.microphone = await navigator.mediaDevices.getUserMedia({audio: MIC});
    if (this.ended) return this.releaseMicrophone();
    this.watchTrack(this.track);
    const micMeter = this.meters ? analyser(this.meters, this.microphone) : null;
    this.emit("meters", {mic: micMeter, voice: null});
    const {ice_servers} = await jsonOrError(await fetch("/webrtc/ice-servers", {credentials: "same-origin"}));
    if (this.ended) return;
    const pc = this.pc = new RTCPeerConnection({iceServers: ice_servers});
    pc.addTrack(this.track, this.microphone);
    pc.ontrack = event => {
      audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
      audio.play().catch(() => this.emit("event", {type: "notice", text: "Sound is blocked. Tap the page to hear VALR."}));
      const voice = this.meters ? analyser(this.meters, new MediaStream([event.track])) : null;
      this.emit("meters", {mic: micMeter, voice});
    };
    const channel = this.channel = pc.createDataChannel("control");
    channel.onmessage = event => {
      if (this.ended) return;
      let message;
      try { message = JSON.parse(event.data); } catch { return this.end("Invalid call event."); }
      this.handle(message);
    };
    channel.onclose = () => this.end("Call ended.");
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") this.end("The connection failed.");
      else if (pc.connectionState === "disconnected") this.pause("connection");
    };
    this.emit("connection", pc);
    await pc.setLocalDescription(await pc.createOffer());
    await gatheringComplete(pc);
    if (this.ended) return;
    const body = {type: "offer", sdp: pc.localDescription.sdp,
                  echo_cancelled: this.track?.getSettings?.().echoCancellation === true};
    if (this.intent) Object.assign(body, {intent: this.intent, ref: this.ref || ""});
    const answer = await jsonOrError(await fetch("/webrtc/offer", {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": this.csrf},
      body: JSON.stringify(body),
    }));
    if (this.ended) return this.hangup(answer.call_id);
    this.callId = answer.call_id || "";
    this.mode = answer.mode || "";
    await pc.setRemoteDescription({type: answer.type, sdp: answer.sdp});
  }

  handle(message) {
    if (message.type === "ready") {
      this.statsTimer = setInterval(() => this.reportPlayout().catch(() => {}), PLAYOUT_REPORT_MS);
    } else if (message.type === "transcript") {
      if (this.audio) this.audio.muted = this.paused;  // A new reply: unmute after an interrupt.
    } else if (message.type === "interrupt") {
      if (this.audio) this.audio.muted = true;  // The server stopped sending; mute the jitter buffer.
    } else if (message.type === "error") {
      this.emit("event", message);
      return this.end(message.error || "The call ended.");
    }
    this.emit("event", message);
  }

  async reportPlayout() {
    if (this.ended || this.channel?.readyState !== "open") return;
    const {delay, next} = playoutDelay(await this.pc.getStats(), this.stats);
    this.stats = next;
    this.channel.send(JSON.stringify({type: "playout", delay_s: Math.min(5, Math.max(0, delay))}));
  }

  // The mic stops or is taken (iOS backgrounding, a phone call): pause, don't drop.
  watchTrack(track) {
    if (!track) return;
    track.addEventListener("ended", () => { if (!this.ended && track === this.track) this.pause("microphone"); });
    track.addEventListener("mute", () => { if (!this.ended && track === this.track) this.pause("microphone"); });
  }

  setMuted(muted) {
    this.muted = Boolean(muted);
    if (this.track) this.track.enabled = !this.muted && !this.paused;  // Silence reaches the server.
    return this.muted;
  }

  pause(reason) {
    if (this.ended || this.paused) return;
    this.paused = true;
    if (this.track) this.track.enabled = false;
    if (this.audio) this.audio.muted = true;
    this.emit("paused", reason);
  }

  // From a tap: a fresh microphone if iOS took the old one, then sound again.
  async resume() {
    if (this.ended) return;
    try { if (navigator.audioSession) navigator.audioSession.type = "play-and-record"; } catch {}
    this.meters?.resume().catch(() => {});
    this.audio?.play?.().catch(() => {});
    if (!this.track || this.track.readyState === "ended") {
      const fresh = await navigator.mediaDevices.getUserMedia({audio: MIC});
      this.microphone?.getTracks().forEach(track => track.stop());
      this.microphone = fresh;
      this.watchTrack(this.track);
      const sender = this.pc?.getSenders().find(s => s.track === null || s.track?.kind === "audio");
      await sender?.replaceTrack(this.track);
      const mic = this.meters ? analyser(this.meters, fresh) : null;
      this.emit("meters", {mic, voice: undefined});
    }
    this.paused = false;
    if (this.audio) this.audio.muted = false;
    this.setMuted(this.muted);
  }

  hangup(callId = this.callId) {
    if (!callId) return;
    fetch("/webrtc/hangup", {method: "POST", credentials: "same-origin", keepalive: true,
      headers: {"Content-Type": "application/json", "X-CSRF-Token": this.csrf},
      body: JSON.stringify({call_id: callId})}).catch(() => {});
  }

  releaseMicrophone() {
    this.microphone?.getTracks().forEach(track => track.stop());
  }

  end(reason = "Call ended.") {
    if (this.ended) return;
    this.ended = true;
    this.hangup();
    clearInterval(this.statsTimer);
    this.releaseMicrophone();
    try { this.channel?.close(); } catch {}
    try { this.pc?.close(); } catch {}
    if (this.audio) this.audio.srcObject = null;
    this.meters?.close().catch(() => {});
    this.emit("ended", reason);
  }
}
