// The call page's view, shared by both transports (voice-webrtc.js, voice.js).
// It owns the DOM: the talk button and its state, captions, the visualizer, the
// latency strip and the connection chip. The transports keep the protocol and
// the audio; they tell this module what happened.
//
// States: idle -> connecting -> listening <-> hearing (your words arrive)
//   -> thinking (your turn ended, no reply audio yet) -> speaking -> listening ...
const body = document.body;
const button = document.querySelector("#call");
const buttonText = button.querySelector(".talk-text");
const status = document.querySelector("#status");
const messages = document.querySelector("#messages");
const metricsSummary = document.querySelector("#metrics");
const stateLabel = document.querySelector("#state-label");
const hint = document.querySelector("#hint");
const connection = document.querySelector("#connection");
const connectionText = connection.querySelector(".conn-text");
const canvas = document.querySelector("#visualizer");
const youLevel = document.querySelector(".lvl.you i");
const valrLevel = document.querySelector(".lvl.valr i");
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const LABELS = {
  idle: ["Ready", "Tap the button and allow the microphone. Speak naturally; VALR answers when you pause."],
  connecting: ["Connecting…", "Opening the microphone and the audio link."],
  listening: ["Listening", "Go ahead, VALR is listening. Pause when you're done."],
  hearing: ["Hearing you", "Keep going; VALR replies when you pause."],
  thinking: ["Thinking…", "Your turn ended. The reply is being written and voiced."],
  speaking: ["VALR is speaking", "Just start talking to interrupt; it stops and listens."],
  ended: ["Call ended", "Tap to start a new call."],
};
const VOICE_ON = 0.02;  // RMS above this: VALR's audio is sounding.
const VOICE_HOLD_MS = 350;

let state = "idle";
let phase = "listening";  // What the conversation is doing, apart from the audio level.
let lastPartialAt = 0;
let speakingUntil = 0;  // From caption timings: VALR's audio is due to play until then.
let voiceActiveAt = 0;
let playerActive = false;  // WebSocket player: reply audio queued and playing.
let micAnalyser = null;
let voiceAnalyser = null;
let frame = null;
let statsTimer = null;
const history = [];

// The call button and the states.
function setState(next) {
  if (next === state) return;
  state = next;
  body.dataset.state = next;
  const [label, text] = LABELS[next];
  stateLabel.textContent = label;
  if (!hint.classList.contains("flash")) hint.textContent = text;
}

function refresh() {
  if (state === "idle" || state === "ended" || state === "connecting") return;
  const now = performance.now();
  if (playerActive || now - voiceActiveAt < VOICE_HOLD_MS || now < speakingUntil) {
    setState("speaking");
  } else if (phase === "thinking") {
    setState("thinking");
  } else if (now - lastPartialAt < 1200) {
    setState("hearing");
  } else {
    setState("listening");
  }
}

export function started() {
  phase = "listening";
  buttonText.textContent = "End call";
  button.setAttribute("aria-label", "End call");
  setState("connecting");
  setConnection("connecting", "Connecting…");
  messages.querySelector(".empty-log")?.remove();
  startVisualizer();
}

export function ready(text) {
  status.textContent = text;
  setState("listening");
}

export function ended(text = "Call ended.") {
  buttonText.textContent = "Start call";
  button.setAttribute("aria-label", "Start call");
  status.textContent = text;
  playerActive = false;
  speakingUntil = 0;
  setState("ended");
  clearInterval(statsTimer);
  statsTimer = null;
  setConnection("none", "Not connected");
  micAnalyser = voiceAnalyser = null;
  setLevel(youLevel, 0);
  setLevel(valrLevel, 0);
}

export function setStatus(text) {
  status.textContent = text;
}

export function onClick(handler) {
  button.addEventListener("click", handler);
}

// Captions.
export function scrollToEnd() {
  messages.scrollTop = messages.scrollHeight;
}

export function addLine(role, text) {
  const line = document.createElement("p");
  line.className = role;
  line.textContent = text;
  messages.append(line);
  scrollToEnd();
  return line;
}

export function appendWord(line, word) {
  line.textContent += (line.textContent ? " " : "") + word;
  scrollToEnd();
}

export function heardWords() {
  lastPartialAt = performance.now();
  if (phase !== "thinking") refresh();
}

export function turnEnded() {
  phase = "thinking";
  lastPartialAt = 0;
  refresh();
}

// A caption: VALR's audio for these words plays from `startsInS` for `seconds`.
export function replyAudio(startsInS, seconds) {
  phase = "listening";
  speakingUntil = Math.max(speakingUntil, performance.now() + (startsInS + seconds) * 1000);
  refresh();
}

export function playerPlaying(active) {
  playerActive = active;
  if (active) phase = "listening";
  refresh();
}

export function interrupted() {
  speakingUntil = 0;
  playerActive = false;
  phase = "listening";
  hint.textContent = "Interrupted. Go ahead, VALR is listening.";
  hint.classList.add("flash");
  setTimeout(() => {
    hint.classList.remove("flash");
    refresh();
    hint.textContent = LABELS[state][1];
  }, 2500);
  refresh();
}

// The latency strip.
export function showMetrics(m) {
  const set = (key, value) => {
    const node = document.querySelector(`[data-m="${key}"]`);
    if (node) node.textContent = value;
  };
  const ms = value => (value === null || value === undefined ? "–" : `${Math.round(value)} ms`);
  set("response_ms", ms(m.response_ms));
  set("endpoint_ms", ms(m.endpoint_ms));
  set("llm_first_token_ms", ms(m.llm_first_token_ms));
  set("tts_first_audio_ms", ms(m.tts_first_audio_ms));
  set("speculative", m.speculative ? `yes${m.eager_lead_ms === null || m.eager_lead_ms === undefined ? "" : `, ${m.eager_lead_ms} ms early`}` : "no");
  set("llm_model", m.llm_model || "–");
  // LLM first token counts from Flux's end of turn: negative when a speculative
  // reply (started at the eager end of turn) was committed.
  metricsSummary.textContent = `${Math.round(m.response_ms)} ms · ${m.llm_model || "model unknown"}`;
  metricsSummary.title = `Response ${m.response_ms} ms = end of turn ${m.endpoint_ms} + LLM first token ${m.llm_first_token_ms} (${m.llm_model}) + TTS ${m.tts_first_audio_ms} (+ overhead); speculative ${m.speculative}${m.eager_lead_ms === null ? "" : `, eager lead ${m.eager_lead_ms} ms`}`;
  history.push(m.response_ms);
  if (history.length > 30) history.shift();
  drawSpark();
}

function drawSpark() {
  const svg = document.querySelector("#spark");
  const top = Math.max(1200, ...history) * 1.1;
  const x = i => (history.length < 2 ? 100 : (i / (history.length - 1)) * 200);
  const y = v => 40 - (v / top) * 38;
  const line = history.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const goal = y(800).toFixed(1);
  svg.innerHTML = `<line class="goal" x1="0" x2="200" y1="${goal}" y2="${goal}"></line>` +
    (history.length > 1 ? `<path class="area" d="${line}L200,40L0,40Z"></path>` : "") +
    `<path d="${line || "M0,40"}"></path>`;
  const sorted = [...history].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  document.querySelector("#spark-label").textContent = `median ${Math.round(median)} ms · ${history.length} turns`;
}

// The connection chip.
function setConnection(quality, text) {
  connection.dataset.quality = quality;
  connectionText.textContent = text;
}

// WebRTC: read the selected candidate pair every 2 s (TURN relay or direct, RTT, loss).
export function watchPeer(pc) {
  clearInterval(statsTimer);
  let previous = null;
  const read = async () => {
    if (pc.connectionState === "closed") return;
    const stats = await pc.getStats();
    let pair = null;
    let inbound = null;
    stats.forEach(report => {
      if (report.type === "transport" && report.selectedCandidatePairId) pair = stats.get(report.selectedCandidatePairId);
      if (report.type === "inbound-rtp" && report.kind === "audio") inbound = report;
    });
    if (!pair) {
      stats.forEach(report => {
        if (!pair && report.type === "candidate-pair" && report.nominated && report.state === "succeeded") pair = report;
      });
    }
    if (!pair) {
      setConnection("connecting", `WebRTC · ${pc.iceConnectionState}`);
      return;
    }
    const local = stats.get(pair.localCandidateId);
    const remote = stats.get(pair.remoteCandidateId);
    const relayed = local?.candidateType === "relay" || remote?.candidateType === "relay";
    const path = relayed ? `TURN relay${local?.relayProtocol ? ` (${local.relayProtocol})` : ""}` : "direct";
    const rtt = pair.currentRoundTripTime === undefined ? null : Math.round(pair.currentRoundTripTime * 1000);
    let loss = 0;
    if (inbound && previous) {
      const lost = (inbound.packetsLost ?? 0) - previous.lost;
      const got = (inbound.packetsReceived ?? 0) - previous.got;
      loss = lost + got > 0 ? Math.max(0, lost) / (lost + got) : 0;
    }
    if (inbound) previous = {lost: inbound.packetsLost ?? 0, got: inbound.packetsReceived ?? 0};
    const quality = rtt === null ? "good" : rtt < 150 && loss < 0.02 ? "good" : rtt < 300 && loss < 0.05 ? "fair" : "poor";
    setConnection(quality, `WebRTC · ${path}${rtt === null ? "" : ` · ${rtt} ms`}${loss >= 0.01 ? ` · ${(loss * 100).toFixed(0)}% loss` : ""}`);
    connection.title = relayed
      ? "Audio goes through a TURN relay server: works behind strict firewalls, adds a little delay."
      : "Audio flows directly between your browser and the server (UDP): the fastest path.";
  };
  statsTimer = setInterval(() => read().catch(() => {}), 2000);
  read().catch(() => {});
}

export function webSocketConnected() {
  setConnection("good", "WebSocket · fallback transport");
  connection.title = "Audio travels over a WebSocket (TCP). WebRTC is the default; this is the ?transport=ws fallback.";
}

// The visualizer: a ring for each voice around the button, and the two level bars.
export function attachMicrophone(analyser) {
  micAnalyser = analyser;
}

export function attachVoice(analyser) {
  voiceAnalyser = analyser;
}

function level(analyser, buffer) {
  if (!analyser) return 0;
  analyser.getFloatTimeDomainData(buffer);
  let sum = 0;
  for (const sample of buffer) sum += sample * sample;
  return Math.sqrt(sum / buffer.length);
}

function setLevel(bar, value) {
  bar.style.setProperty("--level", Math.min(1, value).toFixed(3));
}

function startVisualizer() {
  if (frame !== null) return;
  const context = canvas.getContext("2d");
  const buffer = new Float32Array(1024);
  let you = 0;
  let valr = 0;
  let phaseAngle = 0;
  const styles = getComputedStyle(document.documentElement);
  const draw = () => {
    frame = requestAnimationFrame(draw);
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    const ratio = devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    const micRms = level(micAnalyser, buffer);
    const voiceRms = level(voiceAnalyser, buffer);
    if (voiceRms > VOICE_ON) voiceActiveAt = performance.now();
    you += (Math.min(1, micRms * 6) - you) * 0.3;
    valr += (Math.min(1, voiceRms * 6) - valr) * 0.3;
    setLevel(youLevel, you);
    setLevel(valrLevel, valr);
    refresh();
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const cx = width / 2;
    const cy = height / 2;
    const base = Math.min(width, height) * 0.25;
    phaseAngle += reduceMotion ? 0 : 0.012;
    const rings = [
      [styles.getPropertyValue("--violet").trim(), you, base + 8, 1],
      [styles.getPropertyValue("--cyan").trim(), valr, base + 26, -1],
    ];
    for (const [color, amount, radius, direction] of rings) {
      context.beginPath();
      const points = 96;
      for (let i = 0; i <= points; i++) {
        const angle = (i / points) * Math.PI * 2;
        const wobble = reduceMotion ? 0 : Math.sin(angle * 6 + phaseAngle * 8 * direction) * amount * 14 +
          Math.sin(angle * 11 - phaseAngle * 5 * direction) * amount * 7;
        const r = radius + amount * 22 + wobble;
        const x = cx + Math.cos(angle) * r;
        const y = cy + Math.sin(angle) * r;
        if (i) context.lineTo(x, y); else context.moveTo(x, y);
      }
      context.closePath();
      context.strokeStyle = color;
      context.globalAlpha = 0.35 + amount * 0.65;
      context.lineWidth = 2 + amount * 3;
      context.shadowColor = color;
      context.shadowBlur = 12 + amount * 30;
      context.stroke();
    }
    context.globalAlpha = 1;
    context.shadowBlur = 0;
    if (state === "ended" && you < 0.01 && valr < 0.01) {
      cancelAnimationFrame(frame);
      frame = null;
      drawIdle();
    }
  };
  draw();
}

function drawIdle() {
  const context = canvas.getContext("2d");
  const ratio = devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  const styles = getComputedStyle(document.documentElement);
  const base = Math.min(width, height) * 0.25;
  [[styles.getPropertyValue("--violet").trim(), base + 8], [styles.getPropertyValue("--cyan").trim(), base + 26]]
    .forEach(([color, radius]) => {
      context.beginPath();
      context.arc(width / 2, height / 2, radius, 0, Math.PI * 2);
      context.strokeStyle = color;
      context.globalAlpha = 0.3;
      context.lineWidth = 2;
      context.stroke();
    });
  context.globalAlpha = 1;
}
drawIdle();
addEventListener("resize", () => { if (frame === null) drawIdle(); });
