import {StreamAudioPlayer} from "./audio-player.js";

const recordButton = document.querySelector("#record");
const messages = document.querySelector("#messages");
const status = document.querySelector("#status");
const microphoneSupported = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);
let recorder = null;
let microphone = null;
let recordingTimer = null;
let leavingPage = false;
let busy = true;
let playing = false;
let turnError = false;
let audioFailed = false;
const player = new StreamAudioPlayer(active => {
  playing = active;
  if (!active && !busy && !turnError && socket.readyState === WebSocket.OPEN) {
    status.textContent = "Ready. Click Record for another turn.";
  }
  updateButton();
});

let conversationId = sessionStorage.getItem("conversationId");
if (!conversationId) {
  conversationId = crypto.randomUUID();
  sessionStorage.setItem("conversationId", conversationId);
}
const protocol = location.protocol === "https:" ? "wss:" : "ws:";
const socket = new WebSocket(`${protocol}//${location.host}/ws/voice?conversation_id=${encodeURIComponent(conversationId)}`);
socket.binaryType = "arraybuffer";

function updateButton() {
  recordButton.disabled = !microphoneSupported || socket.readyState !== WebSocket.OPEN || busy || playing;
}

function displayMessage(sender, text) {
  const paragraph = document.createElement("p");
  paragraph.textContent = `${sender}: ${text}`;
  messages.appendChild(paragraph);
  messages.scrollTop = messages.scrollHeight;
  return paragraph;
}

function releaseMicrophone() {
  clearTimeout(recordingTimer);
  microphone?.getTracks().forEach(track => track.stop());
  microphone = null;
  recorder = null;
  recordButton.textContent = "Record";
}

function playbackFailed() {
  audioFailed = true;
  turnError = true;
  player.reset();
  status.textContent = "Audio playback failed. The text reply is still saved; record again to retry.";
  displayMessage("Error", status.textContent);
}

socket.onmessage = event => {
  if (typeof event.data !== "string") {
    if (!audioFailed) {
      try {
        if (player.enqueue(event.data)) status.textContent = "Playing streamed reply…";
      } catch {
        playbackFailed();
      }
    }
    return;
  }
  const message = JSON.parse(event.data);
  if (message.type === "history") {
    messages.replaceChildren();
    for (const turn of message.messages) {
      displayMessage(turn.role === "user" ? "You" : "Gemini",
        turn.parts[0].text + (turn.status === "interrupted" ? " [Interrupted]" : ""));
    }
    busy = false;
    status.textContent = microphoneSupported
      ? "Ready. Click Record, speak, then Stop." : "Microphone recording requires a supported browser on localhost/HTTPS.";
    updateButton();
  } else if (message.type === "transcript") {
    displayMessage("You", message.text);
    status.textContent = "Waiting for Gemini…";
  } else if (message.type === "reply_complete") {
    displayMessage("Gemini", message.text);
    status.textContent = "Generating speech…";
  } else if (message.type === "interrupted") {
    displayMessage("Gemini", message.text + " [Interrupted]");
    displayMessage("Error", message.error);
    turnError = true;
    status.textContent = message.error;
  } else if (message.type === "audio_start") {
    try {
      player.start();
    } catch {
      playbackFailed();
    }
  } else if (message.type === "audio_end") {
    if (!audioFailed) {
      try {
        player.finish();
      } catch {
        playbackFailed();
      }
    }
  } else if (message.type === "error" || message.type === "notice") {
    displayMessage(message.type === "error" ? "Error" : "Notice", message.error || message.text);
    turnError = true;
    status.textContent = message.error || message.text;
  } else if (message.type === "done") {
    busy = false;
    if (!playing && !turnError) status.textContent = "Ready. Click Record for another turn.";
    updateButton();
  }
};
socket.onclose = () => {
  if (recorder) recorder.onstop = null;
  releaseMicrophone();
  player.reset();
  status.textContent = "Disconnected. Reload to reconnect.";
  updateButton();
};
socket.onerror = () => { status.textContent = "Connection error."; };
async function startRecording() {
  busy = true;
  turnError = false;
  audioFailed = false;
  updateButton();
  player.reset();
  status.textContent = "Waiting for microphone permission…";
  try {
    await player.unlock();
    microphone = await navigator.mediaDevices.getUserMedia({audio: true});
    if (leavingPage || socket.readyState !== WebSocket.OPEN) {
      releaseMicrophone();
      return;
    }
    const mediaType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"]
      .find(type => MediaRecorder.isTypeSupported(type));
    recorder = new MediaRecorder(microphone, mediaType ? {mimeType: mediaType} : {});
    const chunks = [];
    let bytes = 0;
    let recordingError = null;
    recorder.ondataavailable = event => {
      if (!event.data.size) return;
      bytes += event.data.size;
      if (bytes > 5 * 1024 * 1024) {
        recordingError = "Recording exceeds the 5 MB limit.";
        if (recorder?.state === "recording") recorder.stop();
      } else {
        chunks.push(event.data);
      }
    };
    recorder.onerror = () => {
      recordingError = "Microphone recording failed. Try again.";
      if (recorder?.state === "recording") recorder.stop();
    };
    recorder.onstop = () => {
      const clip = new Blob(chunks, {type: recorder.mimeType});
      releaseMicrophone();
      busy = true;
      updateButton();
      if (leavingPage) return;
      if (recordingError || !clip.size || socket.readyState !== WebSocket.OPEN) {
        busy = false;
        status.textContent = recordingError || "No recording was sent. Check your connection and record again.";
        updateButton();
        return;
      }
      socket.send(JSON.stringify({type: "recording", media_type: clip.type}));
      socket.send(clip);
      status.textContent = "Transcribing…";
    };
    recorder.start(1000);
    recordingTimer = setTimeout(() => {
      if (recorder?.state === "recording") recorder.stop();
    }, 60000);
    // While recording the only allowed action is Stop.
    recordButton.textContent = "Stop";
    recordButton.disabled = false;
    status.textContent = "Recording… click Stop when finished (60-second maximum).";
  } catch (error) {
    releaseMicrophone();
    busy = false;
    status.textContent = error.name === "NotAllowedError"
      ? "Microphone permission was denied. Allow it in your browser, then try again."
      : "Could not start the microphone.";
    updateButton();
  }
}

recordButton.addEventListener("click", () => {
  if (recorder?.state === "recording") {
    recordButton.disabled = true;
    recorder.stop();
  } else {
    startRecording();
  }
});
document.querySelector("#new-conversation").addEventListener("click", () => {
  sessionStorage.setItem("conversationId", crypto.randomUUID());
  location.reload();
});
window.addEventListener("pagehide", () => {
  leavingPage = true;
  if (recorder) recorder.onstop = null;
  releaseMicrophone();
  player.close();
  socket.close();
});
