#!/bin/sh
# Download the open-weight models used by the voice pipeline (~1 GB).
set -eu
dir="${MODELS_DIR:-$(dirname "$0")/../.cache/models}"
mkdir -p "$dir"
fetch() {
  [ -s "$dir/$1" ] && { echo "have $1"; return; }
  echo "get  $1"
  curl -fL --retry 3 -o "$dir/$1.part" "$2" && mv "$dir/$1.part" "$dir/$1"
}
# Silero VAD (MIT)
fetch silero_vad.onnx https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx
# Smart Turn v3.2 CPU (BSD-2-Clause)
fetch smart-turn-v3.2-cpu.onnx https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx
# NVIDIA Nemotron Speech Streaming EN 0.6B, int8 sherpa-onnx export (NVIDIA Open Model License)
stt=sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25
if [ -s "$dir/$stt/encoder.int8.onnx" ]; then echo "have $stt"; else
  echo "get  $stt"
  curl -fL --retry 3 "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/$stt.tar.bz2" | tar xj -C "$dir"
fi
# Kokoro-82M (Apache-2.0), ONNX export by thewh1teagle/kokoro-onnx
# KOKORO_MODEL picks the weights: kokoro-v1.0.onnx (fp32, default), .fp16.onnx or .int8.onnx.
kokoro="${KOKORO_MODEL:-kokoro-v1.0.onnx}"
fetch "$kokoro" "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$kokoro"
fetch voices-v1.0.bin https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
