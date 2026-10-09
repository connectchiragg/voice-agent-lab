# Observation-only speech diagnostics

Install `pip install -e '.[diagnostics,dev]'`. Download the official Silero model:
https://raw.githubusercontent.com/snakers4/silero-vad/1e261b036686cd0017d500ee96acd1c4ba572a9d/src/silero_vad/data/silero_vad.onnx
Place it at `.cache/speech-diagnostics/silero_vad.onnx`, or set `SILERO_MODEL_PATH`.
SHA256: `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`.
Silero is MIT licensed. The implementation follows its pinned utils_vad.py.

`/ws/diagnostics` accepts this complete settings frame first:
`{"type":"settings","sample_rate":16000,"encoding":"pcm_f32le","echo_cancelled":true,"assistant_speaking":false}`.
Then send binary mono float32 little-endian PCM (up to one second per frame).
Send another complete settings frame when playback status changes.
One socket owns one session; closing clears audio buffers and recurrent state.

Silero evaluates 512 samples (32 ms). Reports occur at the first completed
VAD frame after each 100 ms boundary, not exactly every 100 ms. The rolling
window retains two seconds in memory. A provisional 0.5 speech gate is NOT an
interruption threshold. Echo cancellation must come from the capture client;
the flag reports the client's claim, not independently verified AEC.

The classifier stays disabled because its published preprocessing is incomplete.
No fabricated classification scores are emitted. No cancellation, playback
control, input dropping or history writes exist in this path. Logs contain
probabilities, timing and state only, not raw audio or transcripts.

The browser is unchanged and does not connect to this endpoint yet. Live capture
integration and real microphone/backchannel/echo evaluation remain deferred.
