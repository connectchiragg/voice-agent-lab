// A light 3D leads globe on a 2D canvas: the fallback until (or if) the leads branch's
// static/leads-globe.js is there. Same API: mount(el, data, {onSelect}) -> {select, destroy}.
// data: {leads: [{id, company, stage, ...}], stages: [...]}. Leads are spread evenly over
// the sphere, grouped by stage (a graph of the pipeline, not a map), the ones that need
// you facing front. Drag to turn; click a dot to select it.
// Reduced motion: no auto-rotation. Pauses when off-screen or the tab is hidden.

const TAU = Math.PI * 2;

function hash(text) {
  let h = 2166136261;
  for (const ch of String(text)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return (h >>> 0) / 4294967295;
}

function toXYZ(lat, lng) {
  const phi = (lat * Math.PI) / 180;
  const lambda = (lng * Math.PI) / 180;
  return [Math.cos(phi) * Math.sin(lambda), Math.sin(phi), Math.cos(phi) * Math.cos(lambda)];
}

export function mount(el, data, {onSelect} = {}) {
  const ORDER = ["offer", "interviewing", "screening", "applied", "other", "closed"];
  const sorted = [...(data?.leads ?? [])].sort((a, b) =>
    (ORDER.indexOf(a.stage) - ORDER.indexOf(b.stage)) || (hash(a.id) - hash(b.id)));
  const count = Math.max(sorted.length, 10);
  const leads = sorted.map((lead, i) => {
    // A Fibonacci spiral from the front pole, a little jitter so it doesn't look ruled.
    const t = (i + 0.5) / count;
    const lat = 90 - (Math.acos(1 - 2 * t) * 180) / Math.PI + (hash(lead.id) - 0.5) * 8;
    const lng = (i * 137.508) % 360 + (hash(`${lead.id}:x`) - 0.5) * 10;
    return {lead, p: toXYZ(lat * 0.85, lng)};
  });
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const canvas = document.createElement("canvas");
  canvas.setAttribute("aria-hidden", "true");
  el.append(canvas);
  const ctx = canvas.getContext("2d");
  const style = getComputedStyle(el);
  const color = name => style.getPropertyValue(name).trim() || "#8fd0ff";
  const stageColor = stage => color(`--st-${stage}`) || color("--signal");
  const ink = color("--bone");

  // The planet: a Fibonacci sphere of faint dots.
  const dots = [];
  const N = 900;
  for (let i = 0; i < N; i++) {
    const y = 1 - (i / (N - 1)) * 2;
    const r = Math.sqrt(1 - y * y);
    const t = i * Math.PI * (3 - Math.sqrt(5));
    dots.push([Math.cos(t) * r, y, Math.sin(t) * r]);
  }
  // Threads: each live lead to its nearest live neighbour of the same stage (short arcs).
  const threads = [];
  const angle = (a, b) => Math.acos(Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2])));
  for (const n of leads) {
    if (n.lead.stage === "closed") continue;
    let best = null, bestA = 1.3;
    for (const m of leads) {
      if (m === n || m.lead.stage !== n.lead.stage) continue;
      const a = angle(n.p, m.p);
      if (a < bestA) { best = m; bestA = a; }
    }
    if (best && !threads.some(([x, y]) => x === best && y === n)) threads.push([n, best, n.lead.stage]);
  }

  let width = 0, height = 0, dpr = 1, cx = 0, cy = 0, radius = 0;
  let yaw = 0.2, pitch = 0.95, spin = reduce ? 0 : 0.05;
  let selected = null, hover = null, dragging = null, frame = 0, visible = true, last = performance.now();
  const projected = [];

  function layout() {
    const box = el.getBoundingClientRect();
    width = box.width; height = box.height;
    dpr = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const wide = width >= 900;
    const home = el.dataset.mode !== "leads";
    radius = Math.min(width * (wide ? 0.3 : 0.4), height * (wide ? 0.42 : 0.3));
    cx = wide ? width * (home ? 0.66 : 0.68) : width / 2;
    cy = wide ? height / 2 : height - radius - 26;
    draw();
  }

  function rotate([x, y, z]) {
    const cyw = Math.cos(yaw), syw = Math.sin(yaw);
    const x1 = x * cyw + z * syw, z1 = -x * syw + z * cyw;
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    return [x1, y * cp - z1 * sp, y * sp + z1 * cp];
  }
  const screen = ([x, y, z]) => [cx + x * radius, cy - y * radius, z];

  function arc(a, b) {
    // Great-circle points between two unit vectors, lifted a little off the surface.
    const pts = [];
    const dot = Math.max(-1, Math.min(1, a[0] * b[0] + a[1] * b[1] + a[2] * b[2]));
    const omega = Math.acos(dot) || 1e-6;
    for (let i = 0; i <= 24; i++) {
      const t = i / 24;
      const s1 = Math.sin((1 - t) * omega) / Math.sin(omega), s2 = Math.sin(t * omega) / Math.sin(omega);
      const lift = 1 + Math.sin(Math.PI * t) * 0.08 * omega;
      pts.push(screen(rotate([(a[0] * s1 + b[0] * s2) * lift, (a[1] * s1 + b[1] * s2) * lift, (a[2] * s1 + b[2] * s2) * lift])));
    }
    return pts;
  }

  function draw() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    // Atmosphere.
    const glow = ctx.createRadialGradient(cx, cy, radius * 0.7, cx, cy, radius * 1.35);
    glow.addColorStop(0, "rgba(75,139,255,0.10)");
    glow.addColorStop(1, "rgba(75,139,255,0)");
    ctx.fillStyle = glow;
    ctx.beginPath(); ctx.arc(cx, cy, radius * 1.35, 0, TAU); ctx.fill();
    ctx.strokeStyle = "rgba(143,208,255,0.18)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, radius, 0, TAU); ctx.stroke();
    // Surface dots.
    for (const d of dots) {
      const [x, y, z] = screen(rotate(d));
      ctx.fillStyle = z > 0 ? `rgba(160,190,255,${0.10 + z * 0.32})` : "rgba(160,190,255,0.05)";
      ctx.fillRect(x - 0.7, y - 0.7, 1.4, 1.4);
    }
    // Threads.
    for (const [a, b, stage] of threads) {
      const pts = arc(a.p, b.p);
      ctx.strokeStyle = stageColor(stage);
      ctx.lineWidth = 1;
      for (let i = 1; i < pts.length; i++) {
        ctx.globalAlpha = pts[i][2] > 0 ? 0.45 : 0.08;
        ctx.beginPath(); ctx.moveTo(pts[i - 1][0], pts[i - 1][1]); ctx.lineTo(pts[i][0], pts[i][1]); ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;
    // Leads, back to front.
    projected.length = 0;
    for (const n of leads) projected.push({n, s: screen(rotate(n.p))});
    projected.sort((a, b) => a.s[2] - b.s[2]);
    ctx.font = "500 11px 'JetBrains Mono', ui-monospace, monospace";
    for (const {n, s} of projected) {
      const [x, y, z] = s;
      const front = z > -0.05;
      const c = stageColor(n.lead.stage);
      const size = (n.lead.stage === "interviewing" || n.lead.stage === "offer" ? 5 : 3.6) * (0.75 + Math.max(z, 0) * 0.4);
      ctx.globalAlpha = front ? 1 : 0.22;
      if (front) {
        const halo = ctx.createRadialGradient(x, y, 0, x, y, size * 4);
        halo.addColorStop(0, c); halo.addColorStop(1, "rgba(0,0,0,0)");
        ctx.globalAlpha = 0.35; ctx.fillStyle = halo;
        ctx.beginPath(); ctx.arc(x, y, size * 4, 0, TAU); ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.fillStyle = c;
      ctx.beginPath(); ctx.arc(x, y, size, 0, TAU); ctx.fill();
      const isSel = selected === n.lead.id, isHover = hover === n;
      if (isSel || isHover) {
        ctx.strokeStyle = ink; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, y, size + 5, 0, TAU); ctx.stroke();
      }
      const labelled = front && z > 0.25 && (n.lead.stage === "interviewing" || n.lead.stage === "offer" || n.lead.waiting_on_you);
      if (labelled || isSel || isHover) {
        const text = n.lead.company.toUpperCase();
        ctx.fillStyle = "rgba(5,7,12,0.7)";
        const w = ctx.measureText(text).width;
        ctx.fillRect(x + size + 6, y - 9, w + 10, 18);
        ctx.fillStyle = ink;
        ctx.fillText(text, x + size + 11, y + 4);
      }
      ctx.globalAlpha = 1;
    }
  }

  function tick(now) {
    frame = 0;
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    if (!dragging && spin) yaw += spin * dt * 6;
    draw();
    if (visible && !document.hidden && (spin || dragging)) frame = requestAnimationFrame(tick);
  }
  const start = () => { if (!frame && (spin || dragging)) { last = performance.now(); frame = requestAnimationFrame(tick); } };

  function pick(event) {
    const box = canvas.getBoundingClientRect();
    const x = event.clientX - box.left, y = event.clientY - box.top;
    let best = null, bestD = 18;
    for (const {n, s} of projected) {
      if (s[2] < -0.05) continue;
      const d = Math.hypot(s[0] - x, s[1] - y);
      if (d < bestD) { best = n; bestD = d; }
    }
    return best;
  }
  canvas.addEventListener("pointerdown", event => {
    dragging = {x: event.clientX, y: event.clientY, yaw, pitch, moved: false};
    canvas.setPointerCapture(event.pointerId);
    start();
  });
  canvas.addEventListener("pointermove", event => {
    if (dragging) {
      const dx = event.clientX - dragging.x, dy = event.clientY - dragging.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) dragging.moved = true;
      yaw = dragging.yaw + dx / radius;
      pitch = Math.max(-1.2, Math.min(1.2, dragging.pitch + dy / radius));
      if (!spin) draw();
      return;
    }
    const found = pick(event);
    if (found !== hover) { hover = found; canvas.style.cursor = found ? "pointer" : ""; if (!frame) draw(); }
  });
  canvas.addEventListener("pointerup", event => {
    const wasDrag = dragging?.moved;
    dragging = null;
    if (!wasDrag) {
      const found = pick(event);
      if (found) { selected = found.lead.id; onSelect?.(found.lead); draw(); }
    }
  });
  canvas.addEventListener("pointerleave", () => { hover = null; if (!frame) draw(); });

  const resize = new ResizeObserver(layout);
  resize.observe(el);
  const seen = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; if (visible) start(); });
  seen.observe(el);
  const onVisible = () => { if (!document.hidden) start(); };
  document.addEventListener("visibilitychange", onVisible);
  layout();
  start();

  return {
    select(id) { selected = id; draw(); },
    destroy() {
      cancelAnimationFrame(frame); resize.disconnect(); seen.disconnect();
      document.removeEventListener("visibilitychange", onVisible); canvas.remove();
    },
  };
}
