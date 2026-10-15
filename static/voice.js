import {StreamAudioPlayer} from "./audio-player.js";
const button = document.querySelector("#call");
const status = document.querySelector("#status");
const messages = document.querySelector("#messages");
const metrics = document.querySelector("#metrics");
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
      line.textContent += (line.textContent ? " " : "") + word;
      messages.scrollTop = messages.scrollHeight;
    }, startDelay + (caption.seconds * 1000 * index) / words.length);
    captionTimers.add(timer);
  });
}

function cancelCaptions() {
  captionTimers.forEach(clearTimeout);
  captionTimers.clear();
  segmentStart = null;
}

function addLine(role, text) {
  const line = document.createElement("p");
  line.className = role;
  line.textContent = text;
  messages.append(line);
  messages.scrollTop = messages.scrollHeight;
  return line;
}
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
  button.textContent = "Start call";
  status.textContent = "Call ended.";
}

async function startCall() {
  const current = {player: null, microphone: null, capture: null, source: null, socket: null};
  call = current;
  button.textContent = "End call";
  status.textContent = "Starting call…";
  current.player = new StreamAudioPlayer(active => {
    if (call === current && current.socket?.readyState === WebSocket.OPEN) {
      current.socket.send(JSON.stringify({type: "playback", active}));
    }
  });
  try {
    await current.player.unlock();
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
              status.textContent = "Network cannot keep up with live audio. Call ended.";
              return;
            }
            socket.send(frame.data);
          };
          current.source.connect(current.capture);
          // Processor output is silence: never play the microphone back.
          current.capture.connect(current.player.context.destination);
          status.textContent = "Connected. Microphone stays live while the assistant speaks.";
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
          segmentStart = null;
          current.player.markReply();
        } else if (message.type === "interrupt") {
          // Barge-in: stop speaking now and tell the server how much was heard.
          const played = current.player.interrupt();
          socket.send(JSON.stringify({type: "interrupted", played_s: played}));
          cancelCaptions();  // The screen keeps only what was heard.
          assistantLine?.classList.add("interrupted");
          assistantLine = null;
        } else if (message.type === "caption") {
          scheduleCaption(current.player, message, segmentStart ?? current.player.context.currentTime);
          segmentStart = null;
        } else if (message.type === "reply_end") {
          // Captions may still be revealing; the next transcript starts a new line.
        } else if (message.type === "notice") {
          addLine("notice", message.text);
        } else if (message.type === "metrics") {
          // LLM first token counts from Flux's end of turn: negative when a
          // speculative reply (started at the eager end of turn) was committed.
          metrics.textContent = `Response ${message.response_ms} ms = end of turn ${message.endpoint_ms} + LLM first token ${message.llm_first_token_ms} (${message.llm_model}) + TTS ${message.tts_first_audio_ms} (+ overhead); speculative ${message.speculative}${message.eager_lead_ms === null ? "" : `, eager lead ${message.eager_lead_ms} ms`}`;
        } else if (message.type === "error") {
          endCall(current);
          status.textContent = message.error;
        }
      } catch {
        endCall(current);
        status.textContent = "Invalid audio or call event. Call ended.";
      }
    };
    socket.onclose = () => endCall(current);
    socket.onerror = () => {
      endCall(current);
      status.textContent = "Call connection failed.";
    };
  } catch {
    endCall(current);
    status.textContent = "Could not start call. Check microphone permission and browser support.";
  }
}
button.addEventListener("click", () => call ? endCall() : startCall());
window.addEventListener("pagehide", () => endCall());
