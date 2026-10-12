import {StreamAudioPlayer} from "./audio-player.js";
const button = document.querySelector("#call");
const status = document.querySelector("#status");
const messages = document.querySelector("#messages");
let call = null;

function endCall(expected = call) {
  if (!expected || expected !== call) return;
  call = null;
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
          current.player.enqueue(event.data);
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
        } else if (message.type === "text") {
          messages.textContent += message.text;
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
