// WebRTC call: microphone and reply audio travel as Opus media, call events on a
// data channel with the same messages as the WebSocket transport (voice.js).
// The server paces reply audio and works out what was heard from the playout
// delay this page reports, so the page only plays the remote track.
import {playoutDelay} from "./playout-delay.js";
import * as ui from "./call-ui.js";
const PLAYOUT_REPORT_MS = 500;
const GATHER_TIMEOUT_MS = 2000;
let assistantLine = null;
let userLine = null;  // The live transcript line while the user is speaking.
const captionTimers = new Set();
let call = null;

const addLine = ui.addLine;

// Reveal a spoken segment word by word across its audio. The caption follows
// its audio; the server says when that audio's first sample is (or was) heard
// here, so words already due appear at once.
function scheduleCaption(caption) {
  const line = assistantLine ??= addLine("assistant", "");
  const words = caption.text.split(/\s+/).filter(Boolean);
  ui.replyAudio(caption.starts_in_s, caption.seconds);
  words.forEach((word, index) => {
    const due = caption.starts_in_s + (caption.seconds * index) / words.length;
    const timer = setTimeout(() => {
      captionTimers.delete(timer);
      ui.appendWord(line, word);
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
  expected.meters?.close().catch(() => {});
  ui.ended("Call ended.");
}

function handleEvent(current, message) {
  if (message.type === "ready") {
    ui.ready("Connected. Microphone stays live while VALR speaks.");
    current.statsTimer = setInterval(() => reportPlayout(current).catch(() => {}), PLAYOUT_REPORT_MS);
  } else if (message.type === "partial") {
    if (message.text) {
      userLine ??= addLine("user partial", "");
      userLine.textContent = message.text;
      ui.scrollToEnd();
      ui.heardWords();
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
    ui.turnEnded();
  } else if (message.type === "interrupt") {
    // The server stopped sending; mute what is still in the jitter buffer.
    current.audio.muted = true;
    cancelCaptions();  // The screen keeps only what was heard.
    assistantLine?.classList.add("interrupted");
    assistantLine = null;
    ui.interrupted();
  } else if (message.type === "caption") {
    scheduleCaption(message);
  } else if (message.type === "reply_end") {
    // Captions may still be revealing; the next transcript starts a new line.
  } else if (message.type === "notice") {
    addLine("notice", message.text);
  } else if (message.type === "metrics") {
    ui.showMetrics(message);
  } else if (message.type === "error") {
    endCall(current);
    ui.setStatus(message.error);
  }
}

async function startCall() {
  const current = {pc: null, channel: null, microphone: null, audio: null, statsTimer: null, stats: {},
    meters: null};
  call = current;
  ui.started();
  ui.setStatus("Starting call…");
  // Created inside the click, so the browser lets it play.
  const audio = current.audio = new Audio();
  audio.autoplay = true;
  // Level meters only (never played): the visualizer's rings.
  const meters = current.meters = new AudioContext();
  meters.resume().catch(() => {});
  try {
    current.microphone = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}
    });
    if (call !== current) {
      current.microphone.getTracks().forEach(track => track.stop());
      return;
    }
    ui.attachMicrophone(analyser(meters, current.microphone));
    const {ice_servers} = await jsonOrError(await fetch("/webrtc/ice-servers"));
    if (call !== current) return;
    const pc = current.pc = new RTCPeerConnection({iceServers: ice_servers});
    const [track] = current.microphone.getAudioTracks();
    pc.addTrack(track, current.microphone);
    pc.ontrack = event => {
      audio.srcObject = event.streams[0] ?? new MediaStream([event.track]);
      audio.play().catch(() => {
        ui.setStatus("Audio playback is blocked. Allow sound for this site.");
      });
      ui.attachVoice(analyser(meters, new MediaStream([event.track])));
    };
    const channel = current.channel = pc.createDataChannel("control");
    channel.onmessage = event => {
      if (call !== current) return;
      try {
        handleEvent(current, JSON.parse(event.data));
      } catch {
        endCall(current);
        ui.setStatus("Invalid call event. Call ended.");
      }
    };
    channel.onclose = () => endCall(current);
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed") {
        endCall(current);
        ui.setStatus("Call connection failed.");
      }
    };
    ui.watchPeer(pc);
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
    ui.setStatus(error.message ||
      "Could not start call. Check microphone permission and browser support.");
  }
}

function analyser(context, stream) {
  const node = context.createAnalyser();
  node.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(node);
  return node;
}
ui.onClick(() => call ? endCall() : startCall());
window.addEventListener("pagehide", () => endCall());
