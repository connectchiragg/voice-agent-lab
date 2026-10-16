// The lead globe: an embeddable 3D sphere of the owner's leads (no build step).
//
//   import { mount } from "/static/leads-globe.js";
//   const globe = await mount(element, data, { onSelect(node) { ... } });
//   globe.update(data); globe.select(id); globe.focus(id); globe.hover(id); globe.destroy();
//
// `data` is {leads: [{id, company, temperature, band, stage, stage_label?, last_touch_at,
// status?}]}: the live leads (and the closed ones when the page shows them). Only leads are
// drawn: people and links belong in the lead card. Expired leads are never drawn.
//
// Layout: one node per lead on a Fibonacci sphere, ordered by temperature: the hottest at
// the top, the coldest at the bottom, closed (if shown) last, so the sphere reads like a
// thermometer and auto-rotation brings every lead round to the front. Colour = band (a
// distinct colour per band, a small ramp inside it); size = stage (later stages bigger), so
// colour and size say different things. Every lead has a label, placed in a free slot around
// its dot or further out with a leader line (`placeLabels`); one is hidden only when no slot is
// free, or while its dot is on the far side (it fades in as the dot turns to the front).
// Hovering a node or label shows "Company · band · stage · last touch" (no score: the band is
// the word the owner reads).
//
// Look: a lit core with a soft indigo rim and an outer atmosphere glow, faint latitude and
// longitude lines on the near side only, every lead a bright dot with an additive halo (hot
// ones larger), leads on the far side dimmed rather than hidden, and a sparse, slowly drifting
// starfield behind. Reduced motion: no rotation, no drift.
//
// Options: onSelect({type: "lead", id, lead} | null), maxNodes (default 150), labels (most
// labels drawn, default 150: every drawn lead), reducedMotion (default: the media query; true = no
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
export const STAGE_SIZE = { sourced: 3, applied: 3.4, screening: 4.4, interviewing: 5.8, final: 6.6, offer: 7.4,
  "closed-won": 5, "closed-lost": 3, ghosted: 3, closed: 3 };
const STAGE_LABEL = { sourced: "Sourced", applied: "Applied", screening: "Screening", interviewing: "Interviewing",
  final: "Final stages", offer: "Offer", "closed-won": "Won", "closed-lost": "Not moving forward", ghosted: "Gone quiet", closed: "Closed" };

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

/** "Acme · Hot · Interviewing · 3 days ago" */
export function tipText(lead, now = new Date()) {
  const band = BAND_LABEL[bandOf(lead)];
  const stage = lead.stage_label || STAGE_LABEL[lead.stage] || lead.stage || "";
  let touch = "no touch yet";
  const at = lead.last_touch_at ? new Date(lead.last_touch_at) : null;
  if (at && !Number.isNaN(at.getTime())) {
    const days = Math.floor((now - at) / 86400000);
    touch = days < 1 ? "today" : days === 1 ? "yesterday" : `${days} days ago`;
  }
  return [lead.company, band, stage, touch].filter(Boolean).join(" · ");
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

// Where a label may go, around its dot: [dx, dy] in units of (dot radius + gap); the label's
// box is anchored by side (right of the point, left of it, or centred).
const NEAR = [[1, 0, "l"], [-1, 0, "r"], [0.8, -0.9, "l"], [0.8, 0.9, "l"], [-0.8, -0.9, "r"], [-0.8, 0.9, "r"],
  [0, -1.3, "c"], [0, 1.3, "c"]];

/**
 * Places labels without overlaps. items: [{id, x, y (the dot's centre, px), r (its radius, px),
 * w, h, depth (1 front .. -1 back), forced}]. Forced labels (hovered, focused, selected) go
 * first, then nearer ones. Each label tries 8 slots around its dot, then the same 8 slots
 * further out (twice) with a leader line; it is hidden only when all 24 collide, or on the far side.
 * Slots that cover another dot are tried after the free ones. Returns Map id -> {show,
 * opacity, x, y (the label box's top-left), leader (bool), ax, ay (the leader's dot end)}.
 */
export function placeLabels(items, { width = Infinity, height = Infinity, pad = 2, gap = 3, far = 26 } = {}) {
  const out = new Map(), placed = [];
  const dots = items.filter((it) => it.depth > -0.05).map((it) => [it.x - it.r, it.y - it.r, it.x + it.r, it.y + it.r, it.id]);
  const hits = (box, list) => list.some((p) => box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1]);
  const order = [...items].sort((a, b) => (b.forced - a.forced) || (b.depth - a.depth));
  for (const it of order) {
    const opacity = it.forced ? 1 : Math.max(0, Math.min(1, (it.depth + 0.15) / 0.4));
    let spot = null;
    if (opacity > 0.05) {
      const slots = [];
      for (const [extra, leader] of [[0, false], [far, true], [far * 2, true]]) {
        for (const [dx, dy, side] of NEAR) {
          const reach = it.r + gap + extra;
          const px = it.x + dx * reach, py = it.y + dy * reach;
          const x = side === "l" ? px : side === "r" ? px - it.w : px - it.w / 2;
          const y = py - it.h / 2;
          slots.push({ x, y, leader, box: [x - pad, y - pad, x + it.w + pad, y + it.h + pad] });
        }
      }
      const inside = slots.filter((s) => s.box[0] >= 0 && s.box[1] >= 0 && s.box[2] <= width && s.box[3] <= height);
      const others = dots.filter((d) => d[4] !== it.id);
      spot = inside.find((s) => !hits(s.box, placed) && !hits(s.box, others))
        || inside.find((s) => !hits(s.box, placed))
        || (it.forced ? (inside[0] || slots[0]) : null);
    }
    if (spot) placed.push(spot.box);
    out.set(it.id, spot ? { show: true, opacity, x: spot.x, y: spot.y, leader: spot.leader, ax: it.x, ay: it.y }
      : { show: false, opacity, x: it.x, y: it.y, leader: false, ax: it.x, ay: it.y });
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
  const maxLabels = options.labels ?? 150;
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
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "globe-leaders"); svg.setAttribute("aria-hidden", "true");
  labels.append(svg);
  const leaders = [];
  function leaderLine(i) {
    while (leaders.length <= i) {
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      svg.append(line); leaders.push(line);
    }
    return leaders[i];
  }
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
  const gridLines = new THREE.LineSegments(gridGeometry, new THREE.LineBasicMaterial({ color: ink, transparent: true, opacity: 0.11 }));
  gridLines.renderOrder = 1;
  world.add(gridLines);
  // The core: dark at the centre, an indigo rim towards the edge. It writes depth first, so the
  // far side's grid lines stay hidden and the sphere reads as a solid, lit body.
  const shaded = (vertex, fragment, uniforms, extra = {}) => new THREE.ShaderMaterial({ uniforms, vertexShader: vertex, fragmentShader: fragment, transparent: true, ...extra });
  const VERTEX = "varying vec3 vN; varying vec3 vV; void main(){ vec4 mv = modelViewMatrix * vec4(position, 1.0); vN = normalize(normalMatrix * normal); vV = normalize(-mv.xyz); gl_Position = projectionMatrix * mv; }";
  const coreMaterial = shaded(VERTEX,
    "uniform vec3 inner; uniform vec3 rim; varying vec3 vN; varying vec3 vV; void main(){ float f = 1.0 - max(dot(vN, vV), 0.0); float r = pow(f, 2.4); gl_FragColor = vec4(mix(inner, rim, r), 0.82 + 0.18 * r); }",
    { inner: { value: new THREE.Color("#0a1030") }, rim: { value: new THREE.Color(signal).lerp(new THREE.Color("#8f93ff"), 0.4) } });
  const core = new THREE.Mesh(new THREE.SphereGeometry(RADIUS * 0.985, 64, 48), coreMaterial);
  core.renderOrder = 0;
  world.add(core);
  // The atmosphere: a back-facing shell, glowing at the limb.
  const glowMaterial = shaded(VERTEX,
    "uniform vec3 glow; varying vec3 vN; varying vec3 vV; void main(){ float i = pow(clamp(0.72 + dot(vN, vV), 0.0, 1.0), 3.2); gl_FragColor = vec4(glow * i, i); }",
    { glow: { value: new THREE.Color("#5b62ff") } }, { side: THREE.BackSide, blending: THREE.AdditiveBlending, depthWrite: false });
  const atmosphere = new THREE.Mesh(new THREE.SphereGeometry(RADIUS * 1.16, 64, 48), glowMaterial);
  scene.add(atmosphere);
  // A sparse starfield, far behind.
  const starPositions = [];
  let seed = 7;
  const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < (small ? 260 : 520); i++) {
    const u = rand() * 2 - 1, a = rand() * Math.PI * 2, d = 620 + rand() * 420, r = Math.sqrt(1 - u * u);
    starPositions.push(Math.cos(a) * r * d, u * d, Math.sin(a) * r * d);
  }
  const starGeometry = new THREE.BufferGeometry();
  starGeometry.setAttribute("position", new THREE.Float32BufferAttribute(starPositions, 3));
  const stars = new THREE.Points(starGeometry, new THREE.PointsMaterial({ color: 0xb9c2ff, size: 1.4, sizeAttenuation: false,
    transparent: true, opacity: 0.55, depthWrite: false }));
  scene.add(stars);
  // The halo every lead wears: a soft radial sprite, added on top.
  const haloCanvas = document.createElement("canvas");
  haloCanvas.width = haloCanvas.height = 64;
  const hc = haloCanvas.getContext("2d");
  const grad = hc.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)"); grad.addColorStop(0.25, "rgba(255,255,255,.55)");
  grad.addColorStop(0.6, "rgba(255,255,255,.12)"); grad.addColorStop(1, "rgba(255,255,255,0)");
  hc.fillStyle = grad; hc.fillRect(0, 0, 64, 64);
  const haloTexture = new THREE.CanvasTexture(haloCanvas);

  const ball = new THREE.SphereGeometry(1, small ? 12 : 20, small ? 8 : 14);
  let graph = null, meshes = [], byId = new Map(), labelEls = new Map(), sizes = new Map();
  let selected = null, hovered = null, focused = null, dirty = true, inside = false;

  // Far-side leads stay visible but faint; the near side is bright.
  const worldPos = new THREE.Vector3();
  function shade() {
    for (const mesh of meshes) {
      mesh.getWorldPosition(worldPos);
      const depth = worldPos.z / RADIUS;
      const t = Math.max(0, Math.min(1, (depth + 0.25) / 0.5));
      const front = 0.16 + 0.84 * t * t * (3 - 2 * t);
      const fade = mesh.userData.fade ?? 1;
      mesh.material.opacity = front * fade;
      const halo = mesh.children[0];
      if (halo) halo.material.opacity = halo.userData.base * front * fade;
    }
  }

  function clear() {
    for (const mesh of meshes) {
      world.remove(mesh); mesh.material.dispose();
      mesh.children.forEach((child) => child.material.dispose());
    }
    meshes = []; byId = new Map();
    labels.replaceChildren(svg); labelEls = new Map(); sizes = new Map();
  }

  function build(next) {
    clear();
    graph = layout(next, { maxNodes });
    for (const node of graph.nodes) {
      // CSS strings are read as sRGB, so the nodes match the page's own colours.
      const color = new THREE.Color(`rgb(${bandColor(node.temperature, node.band, palette).join(",")})`);
      const material = new THREE.MeshBasicMaterial({ color: color.clone().lerp(new THREE.Color("#ffffff"), 0.18), transparent: true,
        opacity: node.band === "closed" ? 0.6 : 1, depthTest: false });
      const mesh = new THREE.Mesh(ball, material);
      mesh.position.set(...node.pos);
      mesh.scale.setScalar(node.r * 0.55);
      mesh.renderOrder = 3;
      mesh.userData = node;
      world.add(mesh); meshes.push(mesh); byId.set(node.id, mesh);
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: haloTexture, color, transparent: true, depthTest: false, depthWrite: false,
        blending: THREE.AdditiveBlending, opacity: node.band === "hot" ? 0.8 : node.band === "closed" ? 0.2 : 0.45 }));
      halo.scale.setScalar(node.band === "hot" ? 5.4 : 3.8);
      halo.renderOrder = 2;
      halo.userData = { halo: true, base: halo.material.opacity };
      mesh.add(halo);
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
      node.fade = (node.band === "closed" ? 0.6 : 1) * (selected && node.id !== selected ? 0.35 : 1);
      mesh.scale.setScalar(node.r * 0.55 * (node.id === selected || node.id === hovered ? 1.35 : 1));
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
  el.addEventListener("pointerenter", (event) => { if (event.pointerType === "mouse") inside = true; });
  el.addEventListener("pointerleave", () => { inside = false; lastInput = performance.now(); });
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
      // The sphere fills most of the panel; a portrait panel keeps room for side labels.
      distance = Math.max(220, (RADIUS * (camera.aspect < 1 ? 1.34 : 1.24)) / Math.tan(half));
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
    const items = [], height = el.clientHeight;
    const unit = height / (2 * Math.tan(camera.fov * Math.PI / 360));  // Pixels per world unit at distance 1.
    for (const [id, label] of labelEls) {
      const mesh = byId.get(id);
      mesh.getWorldPosition(projected);
      const r = mesh.scale.x * unit / Math.max(1, distance - projected.z);  // The dot's radius on screen.
      const [x, y, depth] = project(id);
      const [w, h] = sizes.get(id);
      const forced = id === hovered || id === focused || id === selected ? 1 : 0;
      items.push({ id, x, y, r, w, h, depth, forced, label });
    }
    const shown = placeLabels(items, { width: el.clientWidth, height });
    let shownFront = 0, front = 0, used = 0;
    for (const it of items) {
      const state = shown.get(it.id);
      if (it.depth > 0) { front++; if (state.show) shownFront++; }
      it.label.style.transform = `translate(${state.x.toFixed(1)}px, ${state.y.toFixed(1)}px)`;
      it.label.style.opacity = state.opacity.toFixed(2);
      it.label.classList.toggle("is-culled", !state.show);
      it.label.classList.toggle("is-forced", Boolean(it.forced));
      if (state.show && state.leader) {
        const line = leaderLine(used++);
        const ex = Math.max(state.x, Math.min(state.x + it.w, it.x)), ey = Math.max(state.y, Math.min(state.y + it.h, it.y));
        line.setAttribute("x1", it.x.toFixed(1)); line.setAttribute("y1", it.y.toFixed(1));
        line.setAttribute("x2", ex.toFixed(1)); line.setAttribute("y2", ey.toFixed(1));
        line.style.opacity = (0.55 * state.opacity).toFixed(2);
        line.style.display = "";
      }
    }
    for (let i = used; i < leaders.length; i++) leaders[i].style.display = "none";
    el.dataset.labelledFront = String(shownFront);  // Measured: labelled front-facing dots / front-facing dots.
    el.dataset.front = String(front);
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
    } else if (!dragging && auto && !selected && !hovered && !focused && !inside && performance.now() - lastInput > 2500) {
      rotate(0.0011, 0);  // Slow enough to read the labels as they come round.
    }
    if (auto) { stars.rotation.y += 0.00012; dirty = true; }
    camera.position.set(0, 0, distance);
    camera.lookAt(0, 0, 0);
    if (!dirty) return;
    shade();
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
    /** Point at a lead from outside (a list row): highlight it, show its tooltip, turn it to the front. */
    hover(id) { if (id && byId.has(id)) { setHover(id); focus(id); } else setHover(null); },
    get hidden() { return graph ? graph.hidden : 0; },
    destroy() {
      alive = false; cancelAnimationFrame(frame); observer.disconnect(); io.disconnect();
      clear(); gridGeometry.dispose(); ball.dispose(); starGeometry.dispose(); haloTexture.dispose();
      coreMaterial.dispose(); glowMaterial.dispose(); renderer.dispose();
      canvas.remove(); labels.remove(); tip.remove();
    },
  };
}
