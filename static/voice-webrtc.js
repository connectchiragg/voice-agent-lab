// The old stand-alone call page (static/index.html, served at / off the VALR host): its
// view (call-ui.js) on the shared WebRTC call (webrtc-call.js), which the VALR app's
// inline "Talk here" panel (valr-call.js) uses too.
import * as ui from "./call-ui.js";
import {CaptionScheduler, WebRtcCall, describeError} from "./webrtc-call.js";

const csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
let assistantLine = null;
let userLine = null;  // The live transcript line while the user is speaking.
let call = null;
const captions = new CaptionScheduler((line, word) => ui.appendWord(line, word));

function handleEvent(message) {
  if (message.type === "ready") {
    ui.ready("Connected. Microphone stays live while VALR speaks.");
  } else if (message.type === "partial") {
    if (message.text) {
      userLine ??= ui.addLine("user partial", "");
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
      ui.addLine("user", message.text);
    }
    userLine = null;
    assistantLine = null;
    ui.turnEnded();
  } else if (message.type === "interrupt") {
    captions.cancel();  // The screen keeps only what was heard.
    assistantLine?.classList.add("interrupted");
    assistantLine = null;
    ui.interrupted();
  } else if (message.type === "caption") {
    assistantLine ??= ui.addLine("assistant", "");
    ui.replyAudio(message.starts_in_s, message.seconds);
    captions.schedule(message, assistantLine);
  } else if (message.type === "notice") {
    ui.addLine("notice", message.text);
  } else if (message.type === "metrics") {
    ui.showMetrics(message);
  } else if (message.type === "error") {
    ui.setStatus(message.error);
  }
}

function startCall() {
  const current = call = new WebRtcCall({csrf, handlers: {
    event: handleEvent,
    connection: pc => ui.watchPeer(pc),
    meters: ({mic, voice}) => {
      if (mic) ui.attachMicrophone(mic);
      if (voice) ui.attachVoice(voice);
    },
    paused: () => current.resume().catch(() => current.end("The microphone stopped.")),
    ended: reason => {
      if (call === current) call = null;
      captions.cancel();
      ui.ended(reason);
    },
  }});
  ui.started();
  ui.setStatus("Starting call…");
  current.start().catch(error => {
    const {text} = describeError(error);
    current.end();
    ui.setStatus(text);
  });
}

ui.onClick(() => call ? call.end() : startCall());
window.addEventListener("pagehide", () => call?.end());
