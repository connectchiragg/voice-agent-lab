// The lead globe: an embeddable 3D sphere of the owner's leads (no build step).
//
//   import { mount } from "/static/leads-globe.js";
//   const globe = await mount(element, data, { onSelect(node) { ... } });
//   globe.update(data); globe.select(id); globe.focus(id); globe.destroy();
//
// `data` is {leads: [{id, company, temperature, band, stage, stage_label?, last_touch_at,
// status?}]}: the live leads (and the closed ones when the page shows them). Only leads are
// drawn: people and links belong in the lead card. Expired leads are never drawn.
//
// Layout: one node per lead on a Fibonacci sphere, ordered by temperature: the hottest at
// the top, the coldest at the bottom, closed (if shown) last, so the sphere reads like a
// thermometer and auto-rotation brings every lead round to the front. Colour = band (a
// distinct colour per band, a small ramp inside it); size = stage (later stages bigger), so
// colour and size say different things. Every lead has a label; a label that would overlap a
// nearer one is hidden until hovered or focused, and labels on the far side fade out.
// Hovering a node or label shows "Company · band · score · stage · last touch".
//
// Options: onSelect({type: "lead", id, lead} | null), maxNodes (default 150), labels (most
// labels drawn, default 80), reducedMotion (default: the media query; true = no
// auto-rotation, no inertia, no animated focus), threeUrl (an ES module build of three.js;
// default pinned below), now (Date for "3 days ago"; default the current time). Without
// WebGL it resolves to {supported: false} and draws nothing: the host shows its list. The
// pure helpers (bandOf, bandColor, layout, placeLabels, nodeRadius, tipText) need no browser.

export const THREE_URL = "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js";
export const RADIUS = 100;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const SPAN = 0.9;  // Latitudes kept within ±64°: no pile-up of labels at the poles.

// Bands, the same as app/leads.band_of: [name, lowest score, highest score].
export const BANDS = [["hot", 75, 100], ["warm", 50, 75], ["cool", 25, 50], ["cold", 0, 25]];
export const BAND_LABEL = { hot: "Hot", warm: "Warm", cool: "Cool", cold: "Cold", closed: "Closed" };
const ORDER = { hot: 0, warm: 1, cool: 2, cold: 3, closed: 4 };
// [cold end, warm end] per band; static/leads.css's dark set (the globe panel is always dark).
export const DEFAULT_PALETTE = {
  cold: ["#3553a8", "#4f7be6"], cool: ["#23877c", "#2fb3a0"], warm: ["#d9b64c", "#eda233"],
  hot: ["#f0703a", "#e8392b"], closed: ["#5a6170", "#5a6170"],
};
export const STAGE_SIZE = { sourced: 3, applied: 3.4, screening: 4.4, interviewing: 5.8, offer: 7.4,
  "closed-won": 5, "closed-lost": 3, ghosted: 3, closed: 3 };
const STAGE_LABEL = { sourced: "Sourced", applied: "Applied", screening: "Screening", interviewing: "Interviewing",
  offer: "Offer", "closed-won": "Won", "closed-lost": "Not moving forward", ghosted: "Gone quiet", closed: "Closed" };

// Pure helpers.

function hexRgb(hex) {
  const h = String(hex).replace("#", "").trim();
  const v = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

export function bandOf(lead) {
  if (["closed", "closed-won", "closed-lost"].includes(lead.stage) || lead.status === "closed") return "closed";
  if (ORDER[lead.band] !== undefined) return lead.band;
  const t = Number(lead.temperature) || 0;
  return (BANDS.find(([, low]) => t >= low) || BANDS[3])[0];
}

/** [r, g, b] for a score in its band: the band's cold end at its lowest score, its warm end at the top. */
export function bandColor(temperature, band, palette = DEFAULT_PALETTE) {
  const [lo, hi] = (palette[band] || palette.cold).map(hexRgb);
  const range = BANDS.find(([name]) => name === band);
  const t = Math.max(0, Math.min(100, Number(temperature) || 0));
  const f = range ? Math.max(0, Math.min(1, (t - range[1]) / (range[2] - range[1]))) : 1;
  return lo.map((v, k) => Math.round(v + (hi[k] - v) * f));
}

export function nodeRadius(lead) {
  return STAGE_SIZE[lead.stage] ?? 3.4;
}

/** "Acme · Hot · 82 · Interviewing · 3 days ago" */
export function tipText(lead, now = new Date()) {
  const band = BAND_LABEL[bandOf(lead)];
  const stage = lead.stage_label || STAGE_LABEL[lead.stage] || lead.stage || "";
  let touch = "no touch yet";
  const at = lead.last_touch_at ? new Date(lead.last_touch_at) : null;
  if (at && !Number.isNaN(at.getTime())) {
    const days = Math.floor((now - at) / 86400000);
    touch = days < 1 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  }
  const score = Number.isFinite(Number(lead.temperature)) ? String(Math.round(lead.temperature)) : "–";
  return [lead.company, band, score, stage, touch].filter(Boolean).join(" · ");
}

/** Nodes on a sphere of `radius`: {nodes[{id, pos, r, band, temperature, ref}], hidden}. */
export function layout(data, { maxNodes = 150, radius = RADIUS } = {}) {
  const leads = (data.leads || []).filter((l) => l && l.status !== "expired")
    .map((l) => ({ lead: l, band: bandOf(l) }))
    .sort((a, b) => ORDER[a.band] - ORDER[b.band] || (Number(b.lead.temperature) || 0) - (Number(a.lead.temperature) || 0)
      || String(a.lead.id).localeCompare(String(b.lead.id)));
  const kept = leads.slice(0, Math.max(0, maxNodes));
  const n = kept.length;
  const nodes = kept.map(({ lead, band }, i) => {
    let pos;
    if (n === 1) pos = [0, 0, radius];
    else {
      const y = SPAN * (1 - (2 * (i + 0.5)) / n), r = Math.sqrt(1 - y * y), phi = i * GOLDEN;
      pos = [Math.cos(phi) * r * radius, y * radius, Math.sin(phi) * r * radius];
    }
    return { id: lead.id, pos, r: nodeRadius(lead), band, temperature: Number(lead.temperature) || 0, ref: lead };
  });
  return { nodes, hidden: leads.length - n };
}

/**
 * Which labels to show. items: [{id, x, y, w, h, depth (1 front .. -1 back), forced}]. Forced
 * labels go first, then nearer ones; a label overlapping one already placed is culled, and
 * labels on the far side are culled. Returns Map id -> {show, opacity}.
 */
export function placeLabels(items, { pad = 2 } = {}) {
  const out = new Map(), placed = [];
  const order = [...items].sort((a, b) => (b.forced - a.forced) || (b.depth - a.depth));
  for (const it of order) {
    const opacity = Math.max(0, Math.min(1, (it.depth + 0.2) / 0.45));
    const box = [it.x - pad, it.y - pad, it.x + it.w + pad, it.y + it.h + pad];
    const clash = placed.some((p) => box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1]);
    const show = Boolean(it.forced) || (opacity > 0.05 && !clash);
    if (show) placed.push(box);
    out.set(it.id, { show, opacity: it.forced ? 1 : opacity });
  }
  return out;
}

export function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (canvas.getContext("webgl2") || canvas.getContext("webgl")));
  } catch { return false; }
}

function cssColor(el, name, fallback) {
  const value = getComputedStyle(el).getPropertyValue(name).trim();
  return /^#[0-9a-f]{3,8}$/i.test(value) ? value.slice(0, 7) : fallback;
}

// The component.

export async function mount(el, data, options = {}) {
  const reduced = options.reducedMotion ?? matchMedia("(prefers-reduced-motion: reduce)").matches;
  const small = el.clientWidth < 640 || innerWidth < 640;
  const maxNodes = options.maxNodes ?? 150;
  const maxLabels = options.labels ?? 80;
  if (!webglAvailable()) {
    el.classList.add("globe-unsupported");
    return { supported: false, update() {}, select() {}, focus() {}, destroy() {} };
  }
  const THREE = await import(options.threeUrl || THREE_URL);
  const palette = Object.fromEntries(Object.entries(DEFAULT_PALETTE).map(([band, [lo, hi]]) =>
    [band, [cssColor(el, `--band-${band}-lo`, lo), cssColor(el, `--band-${band}`, hi)]]));
  const ink = cssColor(el, "--ash", "#9aa5ba"), signal = cssColor(el, "--signal", "#4b8bff");
  const now = () => options.now || new Date();

  const renderer = new THREE.WebGLRenderer({ antialias: !small, alpha: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = "globe-canvas";
  canvas.setAttribute("aria-hidden", "true");
  const labels = document.createElement("div");
  labels.className = "globe-labels";
  const tip = document.createElement("div");
  tip.className = "globe-tip"; tip.hidden = true; tip.setAttribute("aria-hidden", "true");
  el.append(canvas, labels, tip);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, 1, 2000);
  let distance = 340, zoomed = false;
  const world = new THREE.Group();
  scene.add(world);

  // The sphere: faint latitude and longitude lines.
  const grid = [];
  for (let lat = -60; lat <= 60; lat += 30) {
    const y = Math.sin(lat * Math.PI / 180) * RADIUS, r = Math.cos(lat * Math.PI / 180) * RADIUS;
    for (let i = 0; i < 64; i++) {
      const a = i / 64 * Math.PI * 2, b = (i + 1) / 64 * Math.PI * 2;
      grid.push(Math.cos(a) * r, y, Math.sin(a) * r, Math.cos(b) * r, y, Math.sin(b) * r);
    }
  }
  for (let lon = 0; lon < 180; lon += 30) {
    const c = Math.cos(lon * Math.PI / 180), s = Math.sin(lon * Math.PI / 180);
    for (let i = 0; i < 64; i++) {
      const a = i / 64 * Math.PI * 2, b = (i + 1) / 64 * Math.PI * 2;
      grid.push(Math.cos(a) * c * RADIUS, Math.sin(a) * RADIUS, Math.cos(a) * s * RADIUS,
        Math.cos(b) * c * RADIUS, Math.sin(b) * RADIUS, Math.cos(b) * s * RADIUS);
    }
  }
  const gridGeometry = new THREE.BufferGeometry();
  gridGeometry.setAttribute("position", new THREE.Float32BufferAttribute(grid, 3));
  world.add(new THREE.LineSegments(gridGeometry, new THREE.LineBasicMaterial({ color: ink, transparent: true, opacity: 0.07 })));
  const core = new THREE.Mesh(new THREE.SphereGeometry(RADIUS * 0.985, 48, 32),
    new THREE.MeshBasicMaterial({ color: signal, transparent: true, opacity: 0.035, depthWrite: false }));
  world.add(core);

  const ball = new THREE.SphereGeometry(1, small ? 12 : 20, small ? 8 : 14);
  let graph = null, meshes = [], byId = new Map(), labelEls = new Map(), sizes = new Map();
  let selected = null, hovered = null, focused = null, dirty = true;

  function clear() {
    for (const mesh of meshes) {
      world.remove(mesh); mesh.material.dispose();
      mesh.children.forEach((child) => child.material.dispose());
    }
    meshes = []; byId = new Map();
    labels.replaceChildren(); labelEls = new Map(); sizes = new Map();
  }

  function build(next) {
    clear();
    graph = layout(next, { maxNodes });
    for (const node of graph.nodes) {
      // CSS strings are read as sRGB, so the nodes match the page's own colours.
      const color = new THREE.Color(`rgb(${bandColor(node.temperature, node.band, palette).join(",")})`);
      const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: node.band === "closed" ? 0.6 : 1 });
      const mesh = new THREE.Mesh(ball, material);
      mesh.position.set(...node.pos);
      mesh.scale.setScalar(node.r);
      mesh.userData = node;
      world.add(mesh); meshes.push(mesh); byId.set(node.id, mesh);
      if (node.band === "hot") {  // A soft halo on hot leads.
        const halo = new THREE.Mesh(ball, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.18, depthWrite: false }));
        halo.scale.setScalar(1.8); halo.userData = { halo: true };
        mesh.add(halo);
      }
    }
    graph.nodes.slice(0, maxLabels).forEach((node) => {
      const label = document.createElement("button");
      label.type = "button";
      label.className = `globe-label is-${node.band}`;
      label.textContent = node.ref.company;
      label.setAttribute("aria-label", tipText(node.ref, now()));
      label.dataset.id = node.id;
      label.style.opacity = "0";  // Until the first frame places it.
      labels.append(label); labelEls.set(node.id, label);
    });
    for (const [id, label] of labelEls) sizes.set(id, [label.offsetWidth || 80, label.offsetHeight || 14]);
    el.dataset.nodes = String(graph.nodes.length);
    el.dataset.hidden = String(graph.hidden);
    if (selected && !byId.has(selected)) selected = null;
    highlight();
  }

  function highlight() {
    for (const mesh of meshes) {
      const node = mesh.userData;
      mesh.material.opacity = (node.band === "closed" ? 0.6 : 1) * (selected && node.id !== selected ? 0.35 : 1);
      mesh.scale.setScalar(node.r * (node.id === selected || node.id === hovered ? 1.3 : 1));
    }
    for (const [id, label] of labelEls) label.classList.toggle("is-dim", !!selected && id !== selected && id !== hovered);
    dirty = true;
  }

  function showTip(id) {
    const mesh = id && byId.get(id);
    if (!mesh) { tip.hidden = true; dirty = true; return; }
    const node = mesh.userData;
    const [company, ...rest] = tipText(node.ref, now()).split(" · ");
    const name = document.createElement("b");
    name.textContent = company;
    tip.replaceChildren(name, document.createTextNode(" · " + rest.join(" · ")));
    tip.style.setProperty("--tip", `rgb(${bandColor(node.temperature, node.band, palette).join(",")})`);
    tip.hidden = false;
    dirty = true;
  }
  function setHover(id) {
    if (id === hovered) return;
    hovered = id; highlight(); showTip(hovered || focused);
  }

  // Rotation, zoom and picking.
  let velocity = { x: 0, y: 0 }, dragging = false, moved = 0, last = null, lastInput = 0;
  const pointers = new Map();
  let pinch = null;
  const auto = !reduced;

  function rotate(dx, dy) {
    const qy = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), dx);
    const qx = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), dy);
    world.quaternion.premultiply(qy).premultiply(qx);
    dirty = true;
  }

  function onDown(event) {
    canvas.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    if (pointers.size === 2) {
      const [p, q] = [...pointers.values()];
      pinch = { d: Math.hypot(p[0] - q[0], p[1] - q[1]), distance };
    }
    dragging = true; moved = 0; last = [event.clientX, event.clientY]; velocity = { x: 0, y: 0 };
    focusing = null; lastInput = performance.now();
  }
  function onMove(event) {
    if (!pointers.has(event.pointerId)) { hover(event); return; }
    pointers.set(event.pointerId, [event.clientX, event.clientY]);
    if (pinch && pointers.size === 2) {
      const [p, q] = [...pointers.values()];
      zoom(pinch.distance * pinch.d / Math.max(20, Math.hypot(p[0] - q[0], p[1] - q[1])) - distance);
      return;
    }
    const dx = event.clientX - last[0], dy = event.clientY - last[1];
    moved += Math.abs(dx) + Math.abs(dy);
    last = [event.clientX, event.clientY];
    const k = 0.005 * (distance / 340);
    rotate(dx * k, dy * k);
    velocity = { x: dx * k, y: dy * k };
    lastInput = performance.now();
  }
  function onUp(event) {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinch = null;
    if (pointers.size) return;
    dragging = false;
    if (reduced) velocity = { x: 0, y: 0 };
    if (moved < 6) pick(event);
  }
  function zoom(delta) {
    zoomed = true;
    distance = Math.max(170, Math.min(1000, distance + delta));
    dirty = true; lastInput = performance.now();
  }
  function onWheel(event) {
    event.preventDefault();
    zoom(event.deltaY * 0.35);
  }

  const raycaster = new THREE.Raycaster();
  function hit(event) {
    const box = canvas.getBoundingClientRect();
    const point = new THREE.Vector2(((event.clientX - box.left) / box.width) * 2 - 1, -((event.clientY - box.top) / box.height) * 2 + 1);
    raycaster.setFromCamera(point, camera);
    const found = raycaster.intersectObjects(meshes, false)[0];
    return found ? found.object.userData : null;
  }
  function hover(event) {
    const node = hit(event);
    canvas.style.cursor = node ? "pointer" : "grab";
    setHover(node ? node.id : null);
  }
  function pick(event) {
    const node = hit(event);
    select(node ? node.id : null, true);
  }

  function select(id, fromUser = false) {
    selected = id && byId.has(id) ? id : null;
    highlight();
    if (fromUser && options.onSelect) {
      const node = selected ? byId.get(selected).userData : null;
      options.onSelect(node ? { type: "lead", id: node.id, lead: node.ref } : null);
    }
  }

  let focusing = null;
  function focus(id) {
    const mesh = byId.get(id);
    if (!mesh) return;
    const from = mesh.position.clone().normalize().applyQuaternion(world.quaternion);
    const turn = new THREE.Quaternion().setFromUnitVectors(from, new THREE.Vector3(0, 0, 1));
    const target = turn.multiply(world.quaternion.clone());
    if (reduced) { world.quaternion.copy(target); dirty = true; }
    else focusing = { target, start: world.quaternion.clone(), t: 0 };
    velocity = { x: 0, y: 0 }; lastInput = performance.now();
  }

  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("pointerup", onUp);
  canvas.addEventListener("pointercancel", onUp);
  canvas.addEventListener("pointerleave", () => setHover(null));
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.style.touchAction = "none";
  // Labels are buttons: hover or focus shows the tooltip (focus also turns the lead to the
  // front), a click selects the lead.
  const labelId = (event) => event.target.closest?.(".globe-label")?.dataset.id || null;
  labels.addEventListener("pointerover", (event) => { const id = labelId(event); if (id) setHover(id); });
  labels.addEventListener("pointerout", (event) => { if (labelId(event)) setHover(null); });
  labels.addEventListener("focusin", (event) => {
    const id = labelId(event);
    if (!id) return;
    focused = id; focus(id); showTip(id);
  });
  labels.addEventListener("focusout", () => { focused = null; showTip(hovered); });
  labels.addEventListener("click", (event) => { const id = labelId(event); if (id) select(id, true); });

  function resize() {
    const width = Math.max(1, el.clientWidth), height = Math.max(1, el.clientHeight);
    renderer.setSize(width, height, false);
    canvas.style.width = width + "px"; canvas.style.height = height + "px";
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    if (!zoomed) {  // Fit the sphere and its labels in the narrower side.
      const half = Math.atan(Math.tan(camera.fov * Math.PI / 360) * Math.min(1, camera.aspect));
      distance = Math.max(260, (RADIUS * 1.32) / Math.tan(half));
    }
    dirty = true;
  }
  const observer = new ResizeObserver(resize);
  observer.observe(el);
  resize();

  const projected = new THREE.Vector3();
  function project(id) {
    byId.get(id).getWorldPosition(projected);
    const depth = projected.z / RADIUS;
    projected.project(camera);
    return [(projected.x + 1) / 2 * el.clientWidth, (1 - projected.y) / 2 * el.clientHeight, depth];
  }
  function drawLabels() {
    const items = [];
    for (const [id, label] of labelEls) {
      const [x, y, depth] = project(id);
      const [w, h] = sizes.get(id);
      const r = byId.get(id).userData.r;
      const forced = id === hovered || id === focused || id === selected ? 1 : 0;
      const right = x + r * 0.9 + 3;  // Beside the node; on its left near the right edge.
      const left = right + w > el.clientWidth - 4 ? x - r * 0.9 - 3 - w : right;
      items.push({ id, x: Math.max(2, left), y: y - h / 2, w, h, depth, forced, label });
    }
    const shown = placeLabels(items);
    for (const it of items) {
      const state = shown.get(it.id);
      it.label.style.transform = `translate(${it.x.toFixed(1)}px, ${it.y.toFixed(1)}px)`;
      it.label.style.opacity = state.opacity.toFixed(2);
      it.label.classList.toggle("is-culled", !state.show);
      it.label.classList.toggle("is-forced", Boolean(it.forced));
    }
    const tipId = hovered || focused;
    if (!tip.hidden && tipId && byId.has(tipId)) {
      const [x, y] = project(tipId);
      const w = tip.offsetWidth, h = tip.offsetHeight;
      const left = Math.max(4, Math.min(el.clientWidth - w - 4, x - w / 2));
      const top = y - h - 14 < 4 ? y + 14 : y - h - 14;
      tip.style.transform = `translate(${left.toFixed(1)}px, ${top.toFixed(1)}px)`;
    }
  }

  let frame = 0, alive = true, visible = true;
  const io = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; });
  io.observe(el);
  function tick() {
    if (!alive) return;
    frame = requestAnimationFrame(tick);
    if (!visible || document.hidden) return;
    if (focusing) {
      focusing.t = Math.min(1, focusing.t + 0.06);
      world.quaternion.slerpQuaternions(focusing.start, focusing.target, 1 - Math.pow(1 - focusing.t, 3));
      dirty = true;
      if (focusing.t >= 1) focusing = null;
    } else if (!dragging && !reduced && (Math.abs(velocity.x) + Math.abs(velocity.y) > 1e-4)) {
      rotate(velocity.x, velocity.y);
      velocity = { x: velocity.x * 0.94, y: velocity.y * 0.94 };
    } else if (!dragging && auto && !selected && !hovered && !focused && performance.now() - lastInput > 2500) {
      rotate(0.0016, 0);
    }
    camera.position.set(0, 0, distance);
    camera.lookAt(0, 0, 0);
    if (!dirty) return;
    renderer.render(scene, camera);
    drawLabels();
    dirty = false;
  }

  build(data);
  world.quaternion.setFromEuler(new THREE.Euler(0.22, -0.4, 0));  // A slight tilt: the hot top and cold bottom both in view.
  tick();

  return {
    supported: true,
    update(next) { build(next); },
    select(id) { select(id, false); },
    focus,
    get hidden() { return graph ? graph.hidden : 0; },
    destroy() {
      alive = false; cancelAnimationFrame(frame); observer.disconnect(); io.disconnect();
      clear(); gridGeometry.dispose(); ball.dispose(); renderer.dispose();
      canvas.remove(); labels.remove(); tip.remove();
    },
  };
}
