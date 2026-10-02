// Speech transport stays separate from the existing text-chat WebSocket.
(() => {
  const recordButton = document.querySelector("#record");
  const speechStatus = document.querySelector("#speech-status");
  const messageInput = document.querySelector("#message");
  const messageControls = document.querySelector("#message-controls");
  const replyAudio = document.querySelector("#reply-audio");
  const playbackStatus = document.querySelector("#playback-status");
  let audioUrl = null;
  let speechRequest = null;
  let recorder = null;
  let microphone = null;
  let recordingTimer = null;
  let leavingPage = false;

  function releaseMicrophone() {
    clearTimeout(recordingTimer);
    microphone?.getTracks().forEach((track) => track.stop());
    microphone = null;
    recorder = null;
  }

  function ready() {
    recordButton.textContent = "Record";
    recordButton.disabled = false;
    messageControls.disabled = false;
  }

  async function sendRecording(audio) {
    if (audio.size === 0) throw new Error("The recording is empty. Try again.");
    if (audio.size > 5 * 1024 * 1024) throw new Error("Recording exceeds the 5 MB limit.");
    speechStatus.textContent = "Transcribing with Groq…";
    const response = await fetch("/api/transcribe", {
      method: "POST",
      headers: { "Content-Type": audio.type },
      body: audio,
      signal: AbortSignal.timeout(40000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "Transcription failed.");
    if (leavingPage) return;
    messageInput.value = result.text;
    speechStatus.textContent = "Transcript ready. Edit if needed, then click Send.";
  }

  async function startRecording() {
    speechRequest?.abort(new DOMException("Playback cancelled to record.", "AbortError"));
    replyAudio.pause();
    recordButton.disabled = true;
    messageControls.disabled = true;
    speechStatus.textContent = "Waiting for microphone permission…";
    try {
      microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (leavingPage) {
        releaseMicrophone();
        return;
      }
      const mediaType = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"]
        .find((type) => MediaRecorder.isTypeSupported(type));
      recorder = new MediaRecorder(microphone, mediaType ? { mimeType: mediaType } : {});
      const chunks = [];
      let recordingError = null;
      let recordedBytes = 0;
      recorder.ondataavailable = (event) => {
        if (!event.data.size) return;
        recordedBytes += event.data.size;
        if (recordedBytes > 5 * 1024 * 1024) {
          recordingError = "Recording exceeds the 5 MB limit.";
          if (recorder?.state === "recording") recorder.stop();
          return;
        }
        chunks.push(event.data);
      };
      recorder.onerror = () => {
        recordingError = "Microphone recording failed. Try again.";
        if (recorder?.state === "recording") recorder.stop();
      };
      recorder.onstop = async () => {
        const audio = new Blob(chunks, { type: recorder.mimeType });
        releaseMicrophone();
        recordButton.disabled = true;
        recordButton.textContent = "Record";
        try {
          if (leavingPage) return;
          if (recordingError) throw new Error(recordingError);
          await sendRecording(audio);
        } catch (error) {
          speechStatus.textContent = error.name === "TimeoutError"
            ? "Transcription timed out. Record again when ready." : error.message;
        } finally {
          ready();
          messageInput.focus();
        }
      };
      recorder.start(1000);
      recordingTimer = setTimeout(() => {
        if (recorder?.state === "recording") recorder.stop();
      }, 60000);
      recordButton.disabled = false;
      recordButton.textContent = "Stop";
      speechStatus.textContent = "Recording… click Stop when finished (60-second maximum).";
    } catch (error) {
      releaseMicrophone();
      speechStatus.textContent = error.name === "NotAllowedError"
        ? "Microphone permission was denied. You can still type a message." : "Could not start the microphone.";
      ready();
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

  document.querySelector("#messages").addEventListener("click", async (event) => {
    const playButton = event.target.closest("button[data-speech-text]");
    if (!playButton || speechRequest) return;
    if (messageControls.disabled) {
      playbackStatus.textContent = "Finish recording/transcription before playing a reply.";
      return;
    }
    speechRequest = new AbortController();
    playButton.disabled = true;
    replyAudio.pause();
    replyAudio.hidden = true;
    playbackStatus.textContent = "Generating English speech with Groq…";
    const timer = setTimeout(() => speechRequest?.abort(
      new DOMException("Speech request timed out. Click Play to try again.", "TimeoutError"),
    ), 40000);
    try {
      const response = await fetch("/api/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: playButton.dataset.speechText }),
        signal: speechRequest.signal,
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(typeof error.detail === "string" ? error.detail : "Speech input was invalid.");
      }
      const audio = await response.blob();
      if (leavingPage || messageControls.disabled) return;
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = URL.createObjectURL(audio);
      replyAudio.src = audioUrl;
      replyAudio.hidden = false;
      await replyAudio.play();
      playbackStatus.textContent = "Playing. Use the audio controls to pause or replay.";
    } catch (error) {
      if (leavingPage) return;
      playbackStatus.textContent = error.name === "NotAllowedError"
        ? "Audio is ready. Press Play in the audio controls." : error.message;
    } finally {
      clearTimeout(timer);
      speechRequest = null;
      playButton.disabled = false;
    }
  });

  window.addEventListener("pagehide", () => {
    leavingPage = true;
    speechRequest?.abort();
    replyAudio.pause();
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    // Don't upload a clip when the user leaves the page.
    if (recorder) recorder.onstop = null;
    releaseMicrophone();
  });
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    recordButton.disabled = true;
    speechStatus.textContent = "Microphone recording is unavailable. Use localhost/HTTPS, or type instead.";
  }
})();
