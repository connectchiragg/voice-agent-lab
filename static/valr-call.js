// "Call me" in the VALR top bar, and the inline "Talk here" panel.
//
// The split button offers two equal choices: "Call my phone" (POST /valr/api/call-now,
// handled by valr.js's [data-action="callnow"]: VALR rings once) and "Talk here" (a WebRTC
// call in this panel, without leaving the page). Picking one runs it and makes it this
// device's default (localStorage "valr.call.default"); with nothing stored the default is
// "phone" when a phone is on file, else "talk".
//
// The panel: a docked card on desktop, full screen on a phone. States: Connecting ->
// Listening -> Hearing you -> Thinking -> VALR is speaking -> ... -> Ended / Error, with
// Paused when the app goes to the background or iOS takes the microphone. Mute stops the
// microphone's audio (the track is disabled: the server hears silence). Captions for both
// sides, a level meter each. The call itself is webrtc-call.js, shared with the old page.
import {CaptionScheduler, WebRtcCall, describeError, supported} from "./webrtc-call.js";

export const DEFAULT_KEY = "valr.call.default";
export const PAUSE_END_S = 60;  // A call paused this long ends (the server's own limit is 30 s disconnected).

// The remembered choice, if it can still be used; else phone when there is one.
export function defaultChoice(stored, hasPhone) {
  if (stored === "talk") return "talk";
  if (stored === "phone" && hasPhone) return "phone";
  return hasPhone ? "phone" : "talk";
}

export function readDefault(storage, hasPhone) {
  let stored = null;
  try { stored = storage?.getItem(DEFAULT_KEY) ?? null; } catch {}
  return defaultChoice(stored, hasPhone);
}

export function rememberDefault(storage, choice) {
  if (choice !== "phone" && choice !== "talk") return false;
  try { storage?.setItem(DEFAULT_KEY, choice); return true; } catch { return false; }
}

// What the conversation is doing, from the last events and the audio levels.
export function liveState({phase, now, lastPartialAt = 0, speakingUntil = 0, voiceActiveAt = -Infinity}) {
  if (now - voiceActiveAt < 350 || now < speakingUntil) return "speaking";
  if (phase === "thinking") return "thinking";
  if (now - lastPartialAt < 1200) return "hearing";
  return "listening";
}

export const STATE_LABEL = {
  idle: "Ready when you are", connecting: "Connecting…", listening: "Listening", hearing: "Hearing you",
  thinking: "Thinking…", speaking: "VALR is speaking", paused: "Paused", ended: "Call ended", error: "Couldn't connect",
};

export function isApple(nav = globalThis.navigator) {
  const ua = nav?.userAgent || "";
  return /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && (nav?.maxTouchPoints || 0) > 1);
}

export function micHelp(nav = globalThis.navigator) {
  return isApple(nav)
    ? ["Open the Settings app, then Apps → Safari → Microphone, and choose Allow.",
       "Come back to VALR and tap Try again."]
    : ["Click the lock or settings icon next to the web address.",
       "Set Microphone to Allow, then press Try again."];
}

export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// The page side (skipped when this module is imported by the node tests).
if (typeof document !== "undefined") init();

function init() {
  const split = document.querySelector("[data-call-split]");
  if (!split) return;
  const hasPhone = split.dataset.hasPhone === "1";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
  const main = split.querySelector("[data-call-main]");
  const phoneItem = split.querySelector('[data-call-choice="phone"]');
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let choice = readDefault(globalThis.localStorage, hasPhone);

  function showChoice() {
    main.dataset.choice = choice;
    main.querySelector(".split-label").textContent = choice === "phone" ? "Call my phone" : "Talk here";
    main.setAttribute("aria-label", choice === "phone" ? "Call my phone" : "Talk here, in the browser");
    split.querySelectorAll("[data-call-choice]").forEach(item =>
      item.setAttribute("aria-checked", String(item.dataset.callChoice === choice)));
  }
  showChoice();

  // The menu: either choice runs and becomes the default. The phone item's post is valr.js's.
  split.addEventListener("click", event => {
    const item = event.target.closest("[data-call-choice]");
    if (!item || item.disabled) return;
    choice = item.dataset.callChoice;
    rememberDefault(globalThis.localStorage, choice);
    showChoice();
    if (choice === "talk") panel.open({start: true});
  });
  main.addEventListener("click", () => {
    if (choice === "talk" || !hasPhone) panel.open({start: true});
    else phoneItem.click();  // valr.js posts /valr/api/call-now and says what happened.
  });

  const panel = createPanel({csrf, hasPhone, reducedMotion, callPhone: () => phoneItem?.click()});
  const params = new URLSearchParams(location.search);
  if (params.get("talk") === "1") {  // /call redirects here: open, and wait for the tap.
    params.delete("talk");
    history.replaceState(null, "", location.pathname + (params.size ? `?${params}` : "") + location.hash);
    panel.open({start: false});
  }
}

function createPanel({csrf, hasPhone, reducedMotion, callPhone}) {
  const box = document.createElement("dialog");
  box.className = "call-panel";
  box.setAttribute("aria-labelledby", "call-panel-title");
  box.dataset.state = "idle";
  box.innerHTML = `
    <div class="cp-head">
      <div class="cp-heading"><p class="cp-eyebrow">Talk here</p>
        <h2 class="cp-title" id="call-panel-title">VALR</h2>
        <p class="cp-mode" data-cp-mode>In your browser, with the same brief as a phone call.</p></div>
      <button class="icon-btn cp-close" type="button" data-cp-close aria-label="Close">
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg></button>
    </div>
    <div class="cp-status"><span class="cp-dot" aria-hidden="true"></span>
      <span class="cp-state" data-cp-state role="status">${STATE_LABEL.idle}</span>
      <span class="cp-timer num" data-cp-timer></span></div>
    <div class="cp-meters" aria-hidden="true">
      <div class="cp-meter you"><span>You</span><i><b data-cp-level="you"></b></i></div>
      <div class="cp-meter valr"><span>VALR</span><i><b data-cp-level="valr"></b></i></div>
    </div>
    <div class="cp-notice" data-cp-notice hidden></div>
    <div class="cp-captions" data-cp-captions role="log" aria-live="polite" aria-label="Live captions">
      <p class="cp-empty">Captions appear here as you talk.</p></div>
    <div class="cp-controls">
      <button class="btn primary cp-start" type="button" data-cp-start>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>
        <span>Start talking</span></button>
      <button class="btn cp-mute" type="button" data-cp-mute aria-pressed="false" hidden>
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/></svg>
        <span>Mute</span></button>
      <button class="btn danger cp-end" type="button" data-cp-end hidden>End call</button>
    </div>
    <audio data-cp-audio autoplay playsinline></audio>`;
  document.body.append(box);
  const $ = selector => box.querySelector(selector);
  const stateText = $("[data-cp-state]");
  const timer = $("[data-cp-timer]");
  const notice = $("[data-cp-notice]");
  const log = $("[data-cp-captions]");
  const mode = $("[data-cp-mode]");
  const startButton = $("[data-cp-start]");
  const muteButton = $("[data-cp-mute]");
  const endButton = $("[data-cp-end]");
  const levels = {you: $('[data-cp-level="you"]'), valr: $('[data-cp-level="valr"]')};
  const audio = $("[data-cp-audio]");
  const captions = new CaptionScheduler((line, word) => {
    line.textContent += (line.textContent ? " " : "") + word;
    log.scrollTop = log.scrollHeight;
  });

  let call = null;
  let state = "idle";
  let live = {phase: "listening", lastPartialAt: 0, speakingUntil: 0, voiceActiveAt: -Infinity};
  let meters = {mic: null, voice: null};
  let assistantLine = null;
  let userLine = null;
  let startedAt = 0;
  let frame = null;
  let pausedTimer = null;

  function setState(next) {
    if (next === state) return;
    state = next;
    box.dataset.state = next;
    stateText.textContent = STATE_LABEL[next];
    const active = !["idle", "ended", "error"].includes(next);
    startButton.hidden = active;
    muteButton.hidden = endButton.hidden = !active;
    startButton.querySelector("span").textContent = next === "idle" ? "Start talking" : "Talk again";
  }

  function line(role, text) {
    log.querySelector(".cp-empty")?.remove();
    const row = document.createElement("p");
    row.className = `cp-line ${role}`;
    const who = document.createElement("span");
    who.className = "cp-who";
    who.textContent = role.startsWith("user") ? "You" : role === "notice" ? "" : "VALR";
    const said = document.createElement("span");
    said.className = "cp-said";
    said.textContent = text;
    row.append(who, said);
    log.append(row);
    log.scrollTop = log.scrollHeight;
    return said;
  }

  function showNotice(html, actions = []) {
    notice.innerHTML = html;
    if (actions.length) {
      const row = document.createElement("div");
      row.className = "cp-notice-actions";
      for (const [label, cls, run] of actions) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `btn sm ${cls}`;
        button.textContent = label;
        button.addEventListener("click", run);
        row.append(button);
      }
      notice.append(row);
    }
    notice.hidden = false;
  }

  function hideNotice() {
    notice.hidden = true;
    notice.textContent = "";
  }

  function rms(analyser, buffer) {
    if (!analyser) return 0;
    analyser.getFloatTimeDomainData(buffer);
    let sum = 0;
    for (const sample of buffer) sum += sample * sample;
    return Math.sqrt(sum / buffer.length);
  }

  function tick() {
    frame = requestAnimationFrame(tick);
    const buffer = tick.buffer ??= new Float32Array(1024);
    const now = performance.now();
    const you = call?.muted || call?.paused ? 0 : Math.min(1, rms(meters.mic, buffer) * 6);
    const voice = Math.min(1, rms(meters.voice, buffer) * 6);
    if (voice > 0.12) live.voiceActiveAt = now;
    levels.you.style.setProperty("--level", you.toFixed(3));
    levels.valr.style.setProperty("--level", voice.toFixed(3));
    if (startedAt) timer.textContent = clock((now - startedAt) / 1000);
    if (["listening", "hearing", "thinking", "speaking"].includes(state)) setState(liveState({...live, now}));
  }

  function stopMeters() {
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    levels.you.style.setProperty("--level", 0);
    levels.valr.style.setProperty("--level", 0);
  }

  function handle(message) {
    const now = performance.now();
    if (message.type === "ready") {
      setState("listening");
      mode.textContent = call.mode === "brief" ? "Briefing your next interview, like the phone call."
        : "Nothing coming up, so it's a general chat. Ask about your day or a lead.";
    } else if (message.type === "partial") {
      if (message.text) {
        userLine ??= line("user partial", "");
        userLine.textContent = message.text;
        live.lastPartialAt = now;
      } else {
        userLine?.closest("p").remove();
        userLine = null;
      }
    } else if (message.type === "transcript") {
      if (userLine) {
        userLine.textContent = message.text;
        userLine.closest("p").classList.remove("partial");
      } else {
        line("user", message.text);
      }
      userLine = assistantLine = null;
      live = {...live, phase: "thinking", lastPartialAt: 0};
    } else if (message.type === "caption") {
      assistantLine ??= line("assistant", "");
      live = {...live, phase: "listening",
              speakingUntil: Math.max(live.speakingUntil, now + (message.starts_in_s + message.seconds) * 1000)};
      captions.schedule(message, assistantLine);
    } else if (message.type === "interrupt") {
      captions.cancel();
      assistantLine?.closest("p").classList.add("interrupted");
      assistantLine = null;
      live = {...live, phase: "listening", speakingUntil: 0};
    } else if (message.type === "notice") {
      line("notice", message.text);
    }
  }

  function paused(reason) {
    setState("paused");
    const why = reason === "connection" ? "The connection dropped." : "VALR went to the background.";
    showNotice(`<p><b>Call paused.</b> ${why} Tap Resume to carry on.</p>`, [
      ["Resume", "primary", () => resume()],
      ["End call", "danger", () => call?.end("Call ended.")],
    ]);
    clearTimeout(pausedTimer);
    pausedTimer = setTimeout(() => call?.end("The call ended while paused."), PAUSE_END_S * 1000);
  }

  async function resume() {
    if (!call) return;
    clearTimeout(pausedTimer);
    try {
      await call.resume();  // A tap: a fresh microphone if iOS took the old one.
      hideNotice();
      setState("listening");
    } catch (error) {
      failed(error);
    }
  }

  function ended(reason) {
    clearTimeout(pausedTimer);
    captions.cancel();
    stopMeters();
    call = null;
    if (state !== "error") {
      setState("ended");
      if (reason && reason !== "Call ended.") line("notice", reason);
    }
    document.body.classList.remove("call-live");
  }

  function failed(error) {
    const {kind, text} = describeError(error);
    call?.end();
    setState("error");
    mode.textContent = "The call didn't start.";
    if (kind === "mic-denied") {
      const steps = micHelp().map(step => `<li>${step}</li>`).join("");
      showNotice(`<p><b>Microphone is off for VALR.</b> To talk here, allow it:</p><ol>${steps}</ol>`,
                 [["Try again", "primary", () => start()]]);
    } else if (kind === "unsupported" || kind === "no-mic") {
      showNotice(`<p><b>${text}</b></p>`, hasPhone ? [["Call my phone", "primary", () => { close(); callPhone(); }]] : []);
    } else {
      showNotice(`<p><b>${text.replace(/[<>&]/g, "")}</b></p>`, [["Try again", "primary", () => start()]]);
    }
  }

  // Inside the tap: WebRtcCall.start awaits the microphone first.
  function start() {
    if (call) return;
    hideNotice();
    if (!supported()) return failed(Object.assign(new Error("This browser can't make calls. Use Call my phone instead."), {name: "Unsupported"}));
    log.querySelectorAll(".cp-line").forEach(row => row.remove());
    live = {phase: "listening", lastPartialAt: 0, speakingUntil: 0, voiceActiveAt: -Infinity};
    assistantLine = userLine = null;
    muteButton.setAttribute("aria-pressed", "false");
    muteButton.querySelector("span").textContent = "Mute";
    box.classList.remove("is-muted");
    setState("connecting");
    mode.textContent = "Connecting to VALR…";
    startedAt = 0;
    timer.textContent = "";
    const current = call = new WebRtcCall({intent: "brief_next", csrf, audio, handlers: {
      event: message => { if (call === current) handle(message); },
      meters: next => { meters = {mic: next.mic ?? meters.mic, voice: next.voice === undefined ? meters.voice : next.voice}; },
      connection: pc => pc.addEventListener("connectionstatechange", () => {
        if (pc.connectionState === "connected" && !startedAt) startedAt = performance.now();
      }),
      paused,
      ended,
    }});
    document.body.classList.add("call-live");
    if (frame === null) tick();
    current.start().catch(error => { if (call === current || call === null) failed(error); });
  }

  function open({start: begin}) {
    if (!box.open) box.show();
    if (begin) start();
    (begin ? endButton : startButton).focus({preventScroll: true});
  }

  function close() {
    if (call && !confirm("End the call?")) return;
    call?.end("Call ended.");
    box.close();
    hideNotice();
    setState("idle");
    mode.textContent = "In your browser, with the same brief as a phone call.";
  }

  startButton.addEventListener("click", () => start());
  endButton.addEventListener("click", () => call?.end("Call ended."));
  muteButton.addEventListener("click", () => {
    const muted = call?.setMuted(!call.muted) ?? false;
    muteButton.setAttribute("aria-pressed", String(muted));
    muteButton.querySelector("span").textContent = muted ? "Unmute" : "Mute";
    box.classList.toggle("is-muted", muted);
    stateText.textContent = muted ? "Muted: VALR can't hear you" : STATE_LABEL[state];
  });
  $("[data-cp-close]").addEventListener("click", close);
  box.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); close(); } });

  // Backgrounding (iOS suspends the Home Screen app's microphone): pause with a clear notice.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && call && !call.paused && state !== "connecting") call.pause("background");
  });
  // While a call is live: ask before leaving the page.
  addEventListener("beforeunload", event => { if (call) { event.preventDefault(); event.returnValue = ""; } });
  addEventListener("pagehide", () => call?.end());
  document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!call || !link || box.contains(link) || link.target === "_blank" || link.href.startsWith("#")) return;
    if (!confirm("End the call?")) { event.preventDefault(); return; }
    call.end("Call ended.");
  }, true);
  if (reducedMotion) box.classList.add("reduced-motion");
  return {open, close};
}
