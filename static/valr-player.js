// Past interviews: the recording player, the key moments and the recording drawer.
//
// A Fathom recording plays here from Fathom's official download API: the page asks
// /valr/api/recording/media?id=fathom:<id> for a signed MP4 URL and the <video> element streams
// it straight from Fathom's storage (range requests; nothing passes through VALR). While
// Fathom prepares the video the page says so and asks again (5 s, backing off, ~5 min at most).
// If the signed URL stops working mid-session, it asks once for a fresh one and resumes at
// the same second. Key moments seek the player; "Open in Fathom" stays as the fallback.
//
// Self-contained (its own module), so nothing else on the page can stop the moments from
// rendering. Built with DOM calls only; URLs are checked again here. Pure parts are exported
// for tests/valr-player.test.mjs.

export const POLL_FIRST_MS = 5000;
export const POLL_MAX_MS = 20000;
export const POLL_LIMIT_MS = 5 * 60 * 1000;
export const FETCH_TIMEOUT_MS = 30000;
export const PREPARING = "Preparing the video… (first play can take a minute or two)";

// Wait before the next "is it ready?" question: 5 s, then x1.5 each time, at most 20 s.
export function nextDelay(attempt) {
  return Math.min(POLL_MAX_MS, Math.round(POLL_FIRST_MS * 1.5 ** Math.max(0, attempt)));
}

// A URL the page may load: https anywhere (the server allows only Fathom's storage hosts),
// or a same-origin path (the local preview's stand-in file).
export function playableUrl(url, origin = "") {
  try {
    const parsed = new URL(url, origin || undefined);
    if (parsed.protocol === "https:") return parsed.href;
    return origin && parsed.origin === new URL(origin).origin ? parsed.href : "";
  } catch { return ""; }
}

export function httpsUrl(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.href : "";
  } catch { return ""; }
}

export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

// The API's moments, cleaned: {seconds, clock, label, url}, in time order.
export function momentItems(moments) {
  return (Array.isArray(moments) ? moments : [])
    .filter(m => m && Number.isFinite(Number(m.seconds)) && String(m.label ?? "").trim())
    .map(m => ({seconds: Math.max(0, Number(m.seconds)), clock: String(m.clock || clock(Number(m.seconds))),
                label: String(m.label).trim(), url: httpsUrl(m.url)}))
    .sort((a, b) => a.seconds - b.seconds);
}

// What the player shows for one /valr/api/recording/media answer.
export function mediaView(answer) {
  const status = answer?.status;
  if (status === "completed" && answer.url) return {kind: "play", url: answer.url, type: answer.content_type || "video/mp4"};
  if (status === "processing") return {kind: "wait", text: answer.message || PREPARING};
  if (status === "busy") return {kind: "wait", text: answer.message || "Fathom is busy, try again in a minute."};
  if (status === "expired") return {kind: "wait", text: "Getting a fresh link…"};
  return {kind: "stop", text: answer?.message || "The video can't be played here. Use the link below."};
}

function node(doc, tag, attrs = {}, text = "") {
  const element = doc.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, value);
  if (text) element.textContent = text;
  return element;
}

// The key moments list. With `seek`, each time is a button that moves the player there;
// without one (no player), it links to the moment in the notetaker.
export function renderMoments(doc, moments, seek = null) {
  const list = node(doc, "ol", {class: "moments"});
  for (const moment of momentItems(moments)) {
    const item = node(doc, "li");
    if (seek) {
      const button = node(doc, "button", {type: "button", class: "mono moment-seek", "data-seconds": String(moment.seconds),
        "aria-label": `Play from ${moment.clock}`}, moment.clock);
      button.addEventListener("click", () => seek(moment.seconds));
      item.append(button);
    } else if (moment.url) {
      item.append(node(doc, "a", {class: "mono", href: moment.url, target: "_blank", rel: "noopener noreferrer"}, moment.clock));
    } else {
      item.append(node(doc, "span", {class: "mono"}, moment.clock));
    }
    item.append(node(doc, "span", {}, ` ${moment.label}`));
    list.append(item);
  }
  return list;
}

// A 16:9 player shell (the server renders the same markup on the past-interview page).
export function playerShell(doc, id, title = "") {
  const box = node(doc, "div", {class: "player-box", "data-player": id});
  const video = node(doc, "video", {controls: "", preload: "metadata", playsinline: "", "aria-label": title || "Interview recording"});
  video.hidden = true;
  const status = node(doc, "div", {class: "player-wait", "data-player-status": "", "aria-live": "polite"});
  status.append(node(doc, "span", {class: "muted small"}, PREPARING));
  box.append(video, status);
  return box;
}

// The player: asks for the signed URL, waits while Fathom prepares it, recovers once from an
// expired URL. `env` = {fetchMedia(id, refresh) -> answer, wait(ms) -> Promise, origin, now()}.
export function createPlayer(box, env) {
  let video = box.querySelector("video");
  const status = box.querySelector("[data-player-status]");
  const id = box.dataset?.player ?? box.getAttribute("data-player");
  let refreshed = false;
  let pendingSeek = null;
  const say = text => {
    if (!status) return;
    status.hidden = false;
    const span = status.querySelector("span.muted") ?? status;
    span.textContent = text;
  };
  const play = (url, resumeAt = null, type = "") => {
    if (String(type).startsWith("audio/") && video.tagName === "VIDEO") {  // Audio only (Fireflies on a Pro plan).
      const audio = (box.ownerDocument ?? document).createElement("audio");
      audio.setAttribute("controls", "");
      audio.setAttribute("preload", "metadata");
      audio.setAttribute("aria-label", video.getAttribute("aria-label") || "Interview recording");
      audio.addEventListener("error", onError);
      video.replaceWith(audio);
      video = audio;
    }
    video.setAttribute("src", url);
    video.hidden = false;
    if (status) status.hidden = true;
    const at = resumeAt ?? pendingSeek;
    if (at !== null) {
      video.addEventListener("loadedmetadata", () => { video.currentTime = at; }, {once: true});
    }
  };
  async function load(refresh = false, resumeAt = null) {
    const started = env.now();
    for (let attempt = 0; ; attempt++) {
      let answer;
      try {
        answer = await env.fetchMedia(id, refresh && attempt === 0);
      } catch (error) {
        answer = {status: "failed", message: `The video couldn't be loaded: ${error.message}`};
      }
      const view = mediaView(answer);
      if (view.kind === "play") {
        const url = playableUrl(view.url, env.origin);
        if (!url) { say("The video link looked wrong, so it wasn't played. Use the link below."); return "stopped"; }
        play(url, resumeAt, view.type);
        return "playing";
      }
      if (view.kind === "stop") { say(view.text); return "stopped"; }
      say(view.text);
      if (env.now() - started >= POLL_LIMIT_MS) {
        say("Fathom is still preparing the video. Try again in a few minutes, or open it in Fathom.");
        return "gave_up";
      }
      await env.wait(nextDelay(attempt));
    }
  }
  // A signed URL that stopped working (it lives ~24 h): one fresh one, same second.
  function onError() {
    if (refreshed || !video.getAttribute("src")) {
      say("The recording stopped loading. Reload the page, or open it in the notetaker.");
      return;
    }
    refreshed = true;
    const at = video.currentTime || 0;
    load(true, at);
  }
  video.addEventListener("error", onError);
  return {
    load,
    seek(seconds) {
      if (video.hidden || !video.getAttribute("src")) { pendingSeek = seconds; return; }
      video.currentTime = seconds;
      video.play?.()?.catch?.(() => {});
      video.scrollIntoView?.({block: "nearest"});
    },
  };
}

// The recording answer (/valr/api/recording) into `body`: player (Fathom), link, moments.
export function renderRecording(doc, body, data, players = {}, makePlayer = null) {
  const out = [];
  if (!data.found) {
    out.push(node(doc, "p", {class: "muted small"}, "No recording found for this interview in your connected notetakers."));
    out.push(node(doc, "a", {class: "btn small", href: "#nt-fathom", "data-connect-open": "fathom"}, "Connect a notetaker"));
    body.replaceChildren(...out);
    return null;
  }
  let player = null;
  if (data.player && data.id && makePlayer) {
    const box = playerShell(doc, data.id, data.title);
    out.push(box);
    player = makePlayer(box);
    players[data.id] = player;
  }
  const verb = data.provider === "granola" ? `Open the note in ${data.label}` : `${player ? "Open" : "Watch"} in ${data.label}`;
  const share = httpsUrl(data.share_url);
  if (share) {
    out.push(node(doc, "a", {class: `btn ${player ? "small" : "primary"} watch`, href: share, target: "_blank",
      rel: "noopener noreferrer"}, `${verb} ↗`));
  }
  if (momentItems(data.moments).length) {
    out.push(node(doc, "h4", {class: "sub-title"}, "Key moments"));
    out.push(renderMoments(doc, data.moments, player ? s => player.seek(s) : null));
  }
  if (data.note) out.push(node(doc, "p", {class: "muted small"}, data.note));
  body.replaceChildren(...out);
  return player;
}

export async function getJson(fetchImpl, url, timeoutMs = FETCH_TIMEOUT_MS) {
  const controller = typeof AbortController === "function" ? new AbortController() : null;
  const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
  try {
    const response = await fetchImpl(url, {credentials: "same-origin", cache: "no-store", signal: controller?.signal});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
    return data;
  } catch (error) {
    if (error?.name === "AbortError") throw new Error("it took too long; reload to try again");
    throw error;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// The past-interview page's key moments (or, with data-full, the whole recording).
export async function fillMoments(doc, box, fetchImpl, players, makePlayer) {
  const body = box.querySelector("[data-recording-body]");
  const id = box.dataset?.momentsId ?? box.getAttribute("data-moments-id");
  const full = box.hasAttribute("data-full");
  if (full) body.textContent = "Looking for the recording…";
  let data;
  try {
    data = await getJson(fetchImpl, `/valr/api/recording?id=${encodeURIComponent(id)}`);
  } catch (error) {
    body.textContent = full ? `The recording couldn't be loaded: ${error.message}`
                            : `Key moments couldn't be loaded: ${error.message}`;
    return "error";
  }
  if (full) { renderRecording(doc, body, data, players, makePlayer); return "full"; }
  const moments = momentItems(data.moments);
  if (!data.found || !moments.length) {
    body.textContent = data.note || "No key moments for this recording.";
    return "empty";
  }
  const player = players[id];
  const out = [node(doc, "h4", {class: "sub-title"}, "Key moments"),
               renderMoments(doc, moments, player ? s => player.seek(s) : null)];
  if (data.note) out.push(node(doc, "p", {class: "muted small"}, data.note));
  body.replaceChildren(...out);
  return "moments";
}

// The past interview's "Transcript" section (owner only: /valr/api/recording/transcript).
// Each turn's time seeks the player; the search box hides the turns that don't match.
export function renderTranscript(doc, body, data, seek = null) {
  const turns = Array.isArray(data?.turns) ? data.turns : [];
  if (!turns.length) {
    body.replaceChildren(node(doc, "p", {class: "muted small"}, data?.message || "No transcript for this recording."));
    return null;
  }
  const list = node(doc, "ol", {class: "moments transcript"});
  for (const turn of turns) {
    const item = node(doc, "li", {"data-transcript-turn": ""});
    if (turn.clock && seek && Number.isFinite(turn.seconds)) {
      const button = node(doc, "button", {type: "button", class: "mono moment-seek", "aria-label": `Play from ${turn.clock}`}, turn.clock);
      button.addEventListener("click", () => seek(turn.seconds));
      item.append(button);
    } else if (turn.clock) {
      item.append(node(doc, "span", {class: "mono"}, turn.clock));
    }
    item.append(node(doc, "strong", {}, ` ${turn.who}: `), node(doc, "span", {}, String(turn.text ?? "")));
    list.append(item);
  }
  body.replaceChildren(list);
  return list;
}

export function filterTranscript(list, query) {
  const q = String(query ?? "").trim().toLowerCase();
  let shown = 0;
  for (const item of list?.querySelectorAll?.("[data-transcript-turn]") ?? []) {
    item.hidden = Boolean(q) && !item.textContent.toLowerCase().includes(q);
    if (!item.hidden) shown++;
  }
  return shown;
}

function bootTranscript(doc, box, fetchImpl, players) {
  const id = box.getAttribute("data-transcript-id");
  const body = box.querySelector("[data-transcript-body]");
  const search = box.querySelector("[data-transcript-search]");
  let list = null;
  const open = async () => {
    if (box.dataset.loaded) return;
    box.dataset.loaded = "1";
    body.textContent = "Loading the transcript…";
    try {
      const data = await getJson(fetchImpl, `/valr/api/recording/transcript?id=${encodeURIComponent(id)}`);
      const player = players[id];
      list = renderTranscript(doc, body, data, player ? s => player.seek(s) : null);
    } catch (error) {
      body.textContent = `The transcript couldn't be loaded: ${error.message}`;
    }
  };
  box.addEventListener("toggle", () => { if (box.open) open(); });
  if (box.open) open();
  search?.addEventListener("input", () => filterTranscript(list, search.value));
}

export function boot(doc = document, fetchImpl = (...args) => fetch(...args), origin = location.origin) {
  const players = {};
  const env = {
    origin, now: () => Date.now(), wait: ms => new Promise(resolve => setTimeout(resolve, ms)),
    fetchMedia: (id, refresh) => getJson(fetchImpl, `/valr/api/recording/media?id=${encodeURIComponent(id)}${refresh ? "&refresh=1" : ""}`),
  };
  const makePlayer = box => { const p = createPlayer(box, env); p.load(); return p; };
  doc.querySelectorAll("[data-player]").forEach(box => { players[box.dataset.player] = makePlayer(box); });
  doc.querySelectorAll("[data-moments-id]").forEach(box => fillMoments(doc, box, fetchImpl, players, makePlayer));
  doc.querySelectorAll("[data-transcript-id]").forEach(box => bootTranscript(doc, box, fetchImpl, players));
  // The interview drawer (static/valr.js asks) and the scorecard's "Watch recording" dialog.
  const into = async (body, query) => {
    body.textContent = "Looking for the recording…";
    try {
      renderRecording(doc, body, await getJson(fetchImpl, `/valr/api/recording?${query}`), players, makePlayer);
    } catch (error) {
      body.textContent = `The recording couldn't be loaded: ${error.message}`;
    }
  };
  doc.addEventListener("valr:recording", event => into(event.detail.body, event.detail.query));
  doc.addEventListener("click", event => {
    const button = event.target.closest?.("[data-recording-id]");
    if (!button) return;
    const dialog = doc.getElementById("recording-dialog");
    if (!dialog) return;
    dialog.querySelector("#recording-title").textContent =
      button.closest(".card-score")?.querySelector("h4")?.firstChild?.textContent ?? "Recording";
    if (!dialog.open) dialog.showModal();
    into(dialog.querySelector("[data-recording-body]"), `id=${encodeURIComponent(button.dataset.recordingId)}`);
  });
  return players;
}

if (typeof document !== "undefined" && typeof window !== "undefined") boot();
