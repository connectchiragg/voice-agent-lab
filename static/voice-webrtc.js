// WebRTC call: microphone and reply audio travel as Opus media, call events on a
// data channel with the same messages as the WebSocket transport (voice.js).
// The server paces reply audio and works out what was heard from the playout
// delay this page reports, so the page only plays the remote track.
import {playoutDelay} from "./playout-delay.js";
const button = document.querySelector("#call");
const status = document.querySelector("#status");
const messages = document.querySelector("#messages");
const metrics = document.querySelector("#metrics");
const PLAYOUT_REPORT_MS = 500;
const GATHER_TIMEOUT_MS = 2000;
let assistantLine = null;
let userLine = null;  // The live transcript line while the user is speaking.
const captionTimers = new Set();
let call = null;

function addLine(role, text) {
  const line = document.createElement("p");
  line.className = role;
  line.textContent = text;
  messages.append(line);
  messages.scrollTop = messages.scrollHeight;
  return line;
}

// Reveal a spoken piece word by word across its audio. The server says when
// its first sample will be heard here; words already due appear at once.
function scheduleCaption(caption) {
  const line = assistantLine ??= addLine("assistant", "");
  const words = caption.text.split(/\s+/).filter(Boolean);
  words.forEach((word, index) => {
    const due = caption.starts_in_s + (caption.seconds * index) / words.length;
    const timer = setTimeout(() => {
      captionTimers.delete(timer);
      line.textContent += (line.textContent ? " " : "") + word;
      messages.scrollTop = messages.scrollHeight;
    }, Math.max(0, due * 1000));
    captionTimers.add(timer);
  });
}

function cancelCaptions() {
  captionTimers.forEach(clearTimeout);
  captionTimers.clear();
}

async function reportPlayout(current) {
  if (call !== current || current.channel.readyState !== "open") return;
  const {delay, next} = playoutDelay(await current.pc.getStats(), current.stats);
  current.stats = next;
  current.channel.send(JSON.stringify({type: "playout", delay_s: Math.min(5, Math.max(0, delay))}));
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
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "");
  return body;
}

function endCall(expected = call) {
  if (!expected || expected !== call) return;
  call = null;
  cancelCaptions();
  clearInterval(expected.statsTimer);
  expected.microphone?.getTracks().forEach(track => track.stop());
  expected.channel?.close();
  expected.pc?.close();
  if (expected.audio) expected.audio.srcObject = null;
  button.textContent = "Start call";
  status.textContent = "Call ended.";
}

function handleEvent(current, message) {
  if (message.type === "ready") {
    status.textContent = "Connected. Microphone stays live while the assistant speaks.";
    current.statsTimer = setInterval(() => reportPlayout(current).catch(() => {}), PLAYOUT_REPORT_MS);
  } else if (message.type === "partial") {
    if (message.text) {
      userLine ??= addLine("user partial", "");
      userLine.textContent = message.text;
      messages.scrollTop = messages.scrollHeight;
    } else {
      userLine?.remove();  // Noise, not words: drop the live line.
      userLine = null;
    }
  } else if (message.type === "transcript") {
    if (userLine) {
      userLine.textContent = message.text;
      userLine.classList.remove("partial");
    } else {
      addLine("user", message.text);
    }
    userLine = null;
    assistantLine = null;
    current.audio.muted = false;  // A new reply: unmute after an interrupt.
  } else if (message.type === "interrupt") {
    // The server stopped sending; mute what is still in the jitter buffer.
    current.audio.muted = true;
    cancelCaptions();  // The screen keeps only what was heard.
    assistantLine?.classList.add("interrupted");
    assistantLine = null;
  } else if (message.type === "caption") {
    scheduleCaption(message);
  } else if (message.type === "reply_end") {
    // Captions may still be revealing; the next transcript starts a new line.
  } else if (message.type === "notice") {
    addLine("notice", message.text);
  } else if (message.type === "metrics") {
    metrics.textContent = `Response ${message.response_ms} ms = end of turn ${message.endpoint_ms} + STT ${message.stt_ms} + LLM first token ${message.llm_first_token_ms} (${message.llm_model}) + TTS ${message.tts_first_audio_ms} (+ overhead); speech to turn end ${message.heard_to_decided_ms} ms; speculative ${message.speculative}`;
  } else if (message.type === "error") {
    endCall(current);
    status.textContent = message.error;
  }
}

async function startCall() {
  const current = {pc: null, channel: null, microphone: null, audio: null, statsTimer: null, stats: {}};
  call = current;
  button.textContent = "End call";
  status.textContent = "Starting call…";
  // Created inside the click, so the browser lets it play.
  const audio = current.audio = new Audio();
  audio.autoplay = true;
  try {
    current.microphone = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}
    });
    if (call !== current) {
      current.microphone.getTracks().forEach(track => track.stop());
      return;
    }
    const {ice_servers} = await jsonOrError(await fetch("/webrtc/ice-servers"));
    if (call !== current) return;
    const pc = current.pc = new RTCPeerConnection({iceServers: ice_servers});
    const [track] = current.microphone.getAudioTracks();
    pc.addTrack(track, current.microphone);
    pc.ontrack = event => {
      audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
      audio.play().catch(() => {
        status.textContent = "Audio playback is blocked. Allow sound for this site.";
      });
    };
    const channel = current.channel = pc.createDataChannel("control");
    channel.onmessage = event => {
      if (call !== current) return;
      try {
        handleEvent(current, JSON.parse(event.data));
      } catch {
        endCall(current);
        status.textContent = "Invalid call event. Call ended.";
      }
    };
    channel.onclose = () => endCall(current);
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") {
        endCall(current);
        status.textContent = "Call connection failed.";
      }
    };
    await pc.setLocalDescription(await pc.createOffer());
    await gatheringComplete(pc);
    if (call !== current) return;
    const answer = await jsonOrError(await fetch("/webrtc/offer", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({type: "offer", sdp: pc.localDescription.sdp,
        echo_cancelled: track.getSettings().echoCancellation === true})
    }));
    if (call !== current) return;
    await pc.setRemoteDescription(answer);
  } catch (error) {
    endCall(current);
    status.textContent = error.message ||
      "Could not start call. Check microphone permission and browser support.";
  }
}
button.addEventListener("click", () => call ? endCall() : startCall());
window.addEventListener("pagehide", () => endCall());
