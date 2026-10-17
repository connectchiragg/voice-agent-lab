// Interviews → Processes: the replay. The server draws the chart as it stands today
// (app/valr_ui/processes.py); this plays it back. Press Play (or drag the scrubber) and the
// chart shows the pipeline on that date: a company lights up when its process starts, its bar
// grows as time passes, milestones appear as they happen, and it fades when the process ends.
// The feed below lists the events in the order they happened.
//
// Pure functions first (tests/valr-processes.test.mjs runs them in node), then the page.
// No library: the rows are SVG, updated in place; one requestAnimationFrame loop while playing.

export const DAY_MS = 86400000;
export const REPLAY_S = 24;  // The whole range at 1×.
export const SPEEDS = [1, 2, 4];

const time = iso => (iso ? Date.parse(iso) : NaN);

// The JSON as numbers, sorted, ready for lookups.
export function prepare(data) {
  const start = time(data?.range?.start);
  const end = time(data?.range?.end);
  const processes = (data?.processes || []).map(p => ({
    ...p, t0: time(p.start), t1: p.status === "active" ? Infinity : time(p.status === "fizzled" ? p.fizzle_at : p.end),
    tLast: time(p.last_activity),
    marks: (p.milestones || []).map(m => ({...m, t: time(m.at)})),
  }));
  const events = (data?.events || []).map(e => ({...e, t: time(e.at)})).filter(e => !Number.isNaN(e.t))
    .sort((a, b) => a.t - b.t);
  return {start, end: Math.max(end, start), processes, events};
}

// One process on date t: "future" (not started), "live", or its outcome once it has ended.
export function statusAt(process, t) {
  if (t < process.t0) return "future";
  if (t < process.t1) return "live";
  return process.status;
}

// How far its bar reaches on date t (ms, clamped to the process).
export function barEnd(process, t) {
  const stop = process.status === "active" ? Infinity : (process.status === "fizzled" ? process.tLast : process.t1);
  return Math.max(process.t0, Math.min(t, stop));
}

// The pipeline on date t: how many processes are live and how many ended each way.
export function snapshot(prepared, t, only = "") {
  const counts = {live: 0, offer: 0, rejected: 0, withdrew: 0, fizzled: 0, future: 0};
  for (const p of prepared.processes) {
    if (only && p.id !== only) continue;
    counts[statusAt(p, t)] += 1;
  }
  return counts;
}

// Index of the first event after t (binary search): events[0..i) happened by t.
export function countUpTo(events, t) {
  let low = 0;
  let high = events.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (events[mid].t <= t) low = mid + 1; else high = mid;
  }
  return low;
}

// The latest `limit` events up to t, newest first (optionally one company's).
export function recentEvents(prepared, t, limit = 6, only = "") {
  const out = [];
  for (let i = countUpTo(prepared.events, t) - 1; i >= 0 && out.length < limit; i -= 1) {
    const event = prepared.events[i];
    if (!only || event.process === only) out.push(event);
  }
  return out;
}

// Playback: the date after `elapsedMs` of real time at `speed`, never past the end.
export function advance(prepared, t, elapsedMs, speed = 1) {
  const span = prepared.end - prepared.start;
  const perMs = span / (REPLAY_S * 1000);
  return Math.min(prepared.end, t + elapsedMs * perMs * speed);
}

// Reduced motion: no animation, but Play still steps through the events, one per tick.
export function nextEventTime(prepared, t, only = "") {
  for (let i = countUpTo(prepared.events, t); i < prepared.events.length; i += 1) {
    const event = prepared.events[i];
    if (!only || event.process === only) return event.t;
  }
  return prepared.end;
}

// The scrubber's 0..1000 and back.
export const toPosition = (prepared, t) =>
  prepared.end > prepared.start ? Math.round(1000 * (t - prepared.start) / (prepared.end - prepared.start)) : 1000;
export const fromPosition = (prepared, position) =>
  prepared.start + (prepared.end - prepared.start) * Math.min(1000, Math.max(0, position)) / 1000;

export function dateLabel(t) {
  const d = new Date(t);
  return d.toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric"});
}

export function describe(counts) {
  const parts = [`${counts.live} live`];
  if (counts.offer) parts.push(`${counts.offer} ${counts.offer === 1 ? "offer" : "offers"}`);
  if (counts.rejected) parts.push(`${counts.rejected} rejected`);
  if (counts.withdrew) parts.push(`${counts.withdrew} withdrew`);
  if (counts.fizzled) parts.push(`${counts.fizzled} fizzled out`);
  return parts.join(" · ");
}

// The page side (skipped in node).
if (typeof document !== "undefined") mount(document.querySelector("[data-proc]"));

function mount(root) {
  const source = document.getElementById("processes-data");
  if (!root || !source) return;
  let data;
  try { data = JSON.parse(source.textContent); } catch { return; }
  const prepared = prepare(data);
  if (!prepared.processes.length || !(prepared.end > prepared.start)) return;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const span = prepared.end - prepared.start;
  const pctOf = t => (100 * (t - prepared.start)) / span;
  const $ = selector => root.querySelector(selector);
  const player = $("[data-proc-player]");
  const playButton = $("[data-proc-play]");
  const playText = playButton.querySelector("span");
  const scrub = $("[data-proc-scrub]");
  const company = $("[data-proc-company]");
  const now = $("[data-proc-now]");
  const head = $("[data-proc-playhead]");
  const feed = $("[data-proc-feed]");
  const feedList = feed.querySelector("ol");
  const rows = new Map();
  root.querySelectorAll("[data-pid]").forEach(row => {
    const process = prepared.processes.find(p => p.id === row.dataset.pid);
    if (!process) return;
    rows.set(process.id, {row, process, bar: row.querySelector(".proc-bar"), tail: row.querySelector(".proc-tail"),
                          marks: [...row.querySelectorAll(".proc-m")].map(node => ({node, t: time(node.dataset.at)})),
                          state: ""});
  });

  let t = prepared.end;
  let speed = 1;
  let only = "";
  let playing = false;
  let frame = null;
  let last = 0;
  let stepTimer = null;
  let shownEvents = "";
  player.hidden = false;
  feed.hidden = false;
  root.classList.add("is-interactive");
  if (reducedMotion) root.classList.add("reduced-motion");

  function draw() {
    const atEnd = t >= prepared.end;
    root.classList.toggle("is-replaying", !atEnd || playing);
    head.hidden = atEnd && !playing;
    head.style.setProperty("--tf", (pctOf(t) / 100).toFixed(5));
    for (const entry of rows.values()) {
      const {row, process, bar, tail, marks} = entry;
      const hidden = only && process.id !== only;
      row.hidden = Boolean(hidden);
      if (hidden) continue;
      const state = statusAt(process, t);
      if (state !== entry.state) {
        row.dataset.at = state;  // CSS: future dim, live lit, ended faded.
        entry.state = state;
      }
      const x0 = pctOf(process.t0);
      bar.setAttribute("width", `${Math.max(state === "future" ? 0 : 0.6, pctOf(barEnd(process, t)) - x0).toFixed(3)}%`);
      if (tail) tail.style.opacity = t >= process.t1 ? "1" : "0";
      for (const mark of marks) mark.node.classList.toggle("is-hidden", mark.t > t);
    }
    const counts = snapshot(prepared, t, only);
    const label = atEnd && !playing ? "Today" : dateLabel(t);
    now.textContent = `${label}: ${describe(counts)}`;
    scrub.value = String(toPosition(prepared, t));
    scrub.setAttribute("aria-valuetext", `${dateLabel(t)}. ${describe(counts)}`);
    const events = recentEvents(prepared, t, 6, only);
    const key = events.map(e => `${e.t}${e.process}${e.kind}`).join("|");
    if (key !== shownEvents) {
      shownEvents = key;
      feedList.replaceChildren(...events.map((event, index) => {
        const item = document.createElement("li");
        item.className = `proc-ev k-${event.kind}${index === 0 && playing ? " is-new" : ""}`;
        const when = document.createElement("span");
        when.className = "proc-ev-when num";
        when.textContent = new Date(event.t).toLocaleDateString("en-GB", {day: "numeric", month: "short"});
        const kind = document.createElement("span");
        kind.className = "proc-ev-kind";
        kind.textContent = event.label;
        const what = document.createElement("span");
        what.className = "proc-ev-what";
        const strong = document.createElement("b");
        strong.textContent = event.company;
        what.append(strong, event.title ? ` · ${event.title}` : "");
        item.append(when, kind, what);
        return item;
      }));
    }
  }

  function setPlaying(next) {
    playing = next;
    root.classList.toggle("is-playing", playing);
    playButton.setAttribute("aria-label", playing ? "Pause the timeline" : "Play the timeline");
    playText.textContent = playing ? "Pause" : t >= prepared.end ? "Replay" : "Play";
    now.setAttribute("aria-live", playing ? "off" : "polite");  // No running commentary while it plays.
    cancelAnimationFrame(frame);
    clearTimeout(stepTimer);
    if (!playing) return draw();
    if (t >= prepared.end) t = prepared.start;
    if (reducedMotion) {
      const step = () => {  // One event at a time, no in-between frames.
        t = nextEventTime(prepared, t + 1, only);
        draw();
        if (t >= prepared.end) setPlaying(false);
        else stepTimer = setTimeout(step, 900 / speed);
      };
      draw();
      stepTimer = setTimeout(step, 600);
      return;
    }
    last = performance.now();
    const loop = stamp => {
      t = advance(prepared, t, Math.min(100, stamp - last), speed);
      last = stamp;
      draw();
      if (t >= prepared.end) setPlaying(false);
      else frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);
  }

  playButton.addEventListener("click", () => setPlaying(!playing));
  scrub.addEventListener("input", () => {
    if (playing) setPlaying(false);
    t = fromPosition(prepared, Number(scrub.value));
    draw();
    playText.textContent = t >= prepared.end ? "Replay" : "Play";
  });
  root.querySelectorAll("[data-speed]").forEach(button => button.addEventListener("click", () => {
    speed = Number(button.dataset.speed) || 1;
    root.querySelectorAll("[data-speed]").forEach(b => b.setAttribute("aria-checked", String(b === button)));
  }));
  // The speed radios: arrow keys move between them, like native radios.
  $(".proc-speed").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault();
    const buttons = [...root.querySelectorAll("[data-speed]")];
    const index = buttons.findIndex(b => b.getAttribute("aria-checked") === "true");
    const next = buttons[(index + (event.key === "ArrowLeft" || event.key === "ArrowUp" ? -1 : 1) + buttons.length) % buttons.length];
    next.focus();
    next.click();
  });
  root.querySelectorAll("[data-speed]").forEach(b => b.setAttribute("tabindex", b.getAttribute("aria-checked") === "true" ? "0" : "-1"));
  root.addEventListener("click", event => {
    if (event.target.closest("[data-speed]")) {
      root.querySelectorAll("[data-speed]").forEach(b => b.setAttribute("tabindex", b.getAttribute("aria-checked") === "true" ? "0" : "-1"));
    }
  });
  company.addEventListener("change", () => {
    only = company.value;
    root.classList.toggle("is-filtered", Boolean(only));
    shownEvents = "";
    draw();
  });
  // Space or K plays and pauses while the player has focus (not inside the select).
  player.addEventListener("keydown", event => {
    if ((event.key === "k" || event.key === "K") && event.target.tagName !== "SELECT") {
      event.preventDefault();
      setPlaying(!playing);
    }
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden && playing) setPlaying(false); });
  draw();
}
