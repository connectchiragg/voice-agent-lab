# Observation-only VAP diagnostics

Silero is replaced by dedicated English mono VAP (CPC, 10 Hz, 20-second context).
Use the pinned experimental MaAI source and Python 3.11 runtime documented in
the README. Set VAP_MODEL_PATH and VAP_CPC_PATH to local downloaded files.
Weights have a non-commercial license; this setup is for learning, not deployment.

`/ws/diagnostics` accepts a complete settings frame first:
`{"type":"settings","sample_rate":16000,"encoding":"pcm_f32le","echo_cancelled":true,"assistant_speaking":false}`.
Then send binary mono float32 little-endian PCM (up to one second per message).
Send another settings frame when playback status changes. Each connection owns
independent model state; disconnect clears buffers and prediction context.

Every 1600 samples (100 ms), VAP reports current speech probability, p_now
(expected voice activity over 0–600 ms), and p_future (600–2000 ms).
Silence is processed too. There is no separately imposed two-second window.
The provisional speech probability threshold 0.5 is not an interruption threshold.
Echo cancellation is requested in the browser, not performed by this observer.

The continuous call uses the same observer through a bounded diagnostic queue.
Only diagnostic copies can be skipped on overload; a gap resets model state.
VAP loading or inference failure disables diagnostics without ending the call.
No playback cancellation, input suppression, or history writes are performed.
Logs contain probabilities and timing, not audio or transcripts.

Real public recorded speech passed through both TestClient and a network socket.
Live mic, backchannel, deliberate interruption, echo and Hinglish checks remain
pending. VAP is a voice-activity predictor, not a trained interruption classifier.
