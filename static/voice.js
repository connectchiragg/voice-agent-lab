import {StreamAudioPlayer} from "./audio-player.js";
import * as ui from "./call-ui.js";
let assistantLine = null;
let userLine = null;  // The live transcript line while the user is speaking.
let segmentStart = null;  // When the audio of the segment being received starts playing.
const captionTimers = new Set();

// Reveal a spoken segment word by word across its audio, starting when it plays.
// The caption arrives after its audio, so words already due appear at once.
function scheduleCaption(player, caption, startTime) {
  const line = assistantLine ??= addLine("assistant", "");
  const words = caption.text.split(/\s+/).filter(Boolean);
  const startDelay = Math.max(0, startTime - player.context.currentTime) * 1000;
  words.forEach((word, index) => {
    const timer = setTimeout(() => {
      captionTimers.delete(timer);
      ui.appendWord(line, word);
    }, startDelay + (caption.seconds * 1000 * index) / words.length);
    captionTimers.add(timer);
  });
}

function cancelCaptions() {
  captionTimers.forEach(clearTimeout);
  captionTimers.clear();
  segmentStart = null;
}

const addLine = ui.addLine;
let call = null;

function endCall(expected = call) {
  if (!expected || expected !== call) return;
  call = null;
  cancelCaptions();
  expected.microphone?.getTracks().forEach(track => track.stop());
  expected.capture?.disconnect();
  expected.source?.disconnect();
  expected.player.close();
  expected.socket?.close();
  ui.ended("Call ended.");
}

async function startCall() {
  const current = {player: null, microphone: null, capture: null, source: null, socket: null};
  call = current;
  ui.started();
  ui.setStatus("Starting call…");
  current.player = new StreamAudioPlayer(active => {
    if (call === current) ui.playerPlaying(active);
    if (call === current && current.socket?.readyState === WebSocket.OPEN) {
      current.socket.send(JSON.stringify({type: "playback", active}));
    }
  });
  try {
    await current.player.unlock();
    // Reply audio passes through an analyser (the visualizer's VALR ring) on its way out.
    const voiceMeter = current.player.context.createAnalyser();
    voiceMeter.fftSize = 1024;
    voiceMeter.connect(current.player.context.destination);
    current.player.output = voiceMeter;
    ui.attachVoice(voiceMeter);
    current.microphone = await navigator.mediaDevices.getUserMedia({
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}
    });
    if (call !== current) {
      current.microphone.getTracks().forEach(track => track.stop());
      return;
    }
    await current.player.context.audioWorklet.addModule("/static/microphone-worklet.js");
    if (call !== current) return;
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = current.socket = new WebSocket(`${protocol}//${location.host}/ws/voice`);
    socket.binaryType = "arraybuffer";
    socket.onopen = () => socket.send(JSON.stringify({type: "start", sample_rate: 16000,
      encoding: "pcm_f32le", echo_cancelled:
        current.microphone.getAudioTracks()[0].getSettings().echoCancellation === true}));
    socket.onmessage = event => {
      if (call !== current) return;
      try {
        if (typeof event.data !== "string") {
          const startTime = current.player.enqueue(event.data);
          segmentStart ??= startTime;
          return;
        }
        const message = JSON.parse(event.data);
        if (message.type === "ready") {
          current.player.start(message);
          current.source = current.player.context.createMediaStreamSource(current.microphone);
          current.capture = new AudioWorkletNode(current.player.context, "microphone-capture");
          current.capture.port.onmessage = frame => {
            if (call !== current || socket.readyState !== WebSocket.OPEN) return;
            if (socket.bufferedAmount > 256000) {
              endCall(current);
              ui.setStatus("Network cannot keep up with live audio. Call ended.");
              return;
            }
            socket.send(frame.data);
          };
          current.source.connect(current.capture);
          const micMeter = current.player.context.createAnalyser();
          micMeter.fftSize = 1024;
          current.source.connect(micMeter);
          ui.attachMicrophone(micMeter);
          // Processor output is silence: never play the microphone back.
          current.capture.connect(current.player.context.destination);
          ui.ready("Connected. Microphone stays live while VALR speaks.");
          ui.webSocketConnected();
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
          segmentStart = null;
          current.player.markReply();
          ui.turnEnded();
        } else if (message.type === "interrupt") {
          // Barge-in: stop speaking now and tell the server how much was heard.
          const played = current.player.interrupt();
          socket.send(JSON.stringify({type: "interrupted", played_s: played}));
          cancelCaptions();  // The screen keeps only what was heard.
          assistantLine?.classList.add("interrupted");
          assistantLine = null;
          ui.interrupted();
        } else if (message.type === "caption") {
          scheduleCaption(current.player, message, segmentStart ?? current.player.context.currentTime);
          segmentStart = null;
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
      } catch {
        endCall(current);
        ui.setStatus("Invalid audio or call event. Call ended.");
      }
    };
    socket.onclose = () => endCall(current);
    socket.onerror = () => {
      endCall(current);
      ui.setStatus("Call connection failed.");
    };
  } catch {
    endCall(current);
    ui.setStatus("Could not start call. Check microphone permission and browser support.");
  }
}
ui.onClick(() => call ? endCall() : startCall());
window.addEventListener("pagehide", () => endCall());
