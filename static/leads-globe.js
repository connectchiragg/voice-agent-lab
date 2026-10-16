// The lead globe: an embeddable 3D spherical map of the owner's leads (no build step).
//
//   import { mount } from "/static/leads-globe.js";
//   const globe = await mount(element, data, { onSelect(node) { ... } });
//   globe.update(data); globe.select(id); globe.focus(id); globe.destroy();
//
// `data` is GET /valr/api/leads: {leads[{id, company, temperature, activity, stage, status, ...}],
// contacts[{id, name, role, leads[]}], links[{source, target, kind: "contact"|"shared"}]}.
// Leads sit on a sphere (Fibonacci spiral, hottest first, leads that share people placed
// next to each other), sized by activity and coloured by temperature (cold blue -> hot
// orange). People float just outside, above the leads they link. Drag to rotate, wheel or
// pinch to zoom, click a node to select it.
//
// Options: onSelect({type: "lead"|"contact", id, lead?, contact?} | null), maxNodes (leads
// kept, hottest first; default 120, 40 under 640 px wide), labels (lead labels drawn, 24),
// reducedMotion (default: the prefers-reduced-motion media query; true = no auto-rotation,
// no inertia, no animated focus), threeUrl (an ES module build of three.js; default pinned
// below). Without WebGL it resolves to a handle with `supported: false` and draws nothing:
// the host shows its list instead. The pure helpers (layout, tempColor) need no browser.

export const THREE_URL = "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js";
export const RADIUS = 100;
const GOLDEN = Math.PI * (3 - Math.sqrt(5));
const DEFAULT_STOPS = [[0, "#3b7bff"], [35, "#8fd0ff"], [65, "#ffc46b"], [100, "#ff6a2b"]];

// Pure helpers.

function hexRgb(hex) {
  const h = hex.replace("#", "").trim();
  const v = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

export function tempColor(t, stops = DEFAULT_STOPS) {
  const x = Math.max(0, Math.min(100, Number(t) || 0));
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i][0]) {
      const [a, ca] = stops[i - 1], [b, cb] = stops[i];
      const f = b === a ? 0 : (x - a) / (b - a);
      const A = hexRgb(ca), B = hexRgb(cb);
      return A.map((v, k) => Math.round(v + (B[k] - v) * f));
    }
  }
  return hexRgb(stops[stops.length - 1][1]);
}

function hash(text) {
  let h = 2166136261;
  for (const ch of String(text)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return (h >>> 0) / 4294967296;
}

const norm = (v) => { const l = Math.hypot(...v) || 1; return v.map((x) => x / l); };

export function leadRadius(lead) {
  return Math.max(3, Math.min(11, 3 + 1.6 * Math.sqrt(Number(lead.activity) || 0)));
}

/** Positions on a sphere of `radius`: {nodes[{id, type, pos, r, temperature, ref}], edges[{a, b, kind}], hidden}. */
export function layout(data, { maxNodes = 120, radius = RADIUS } = {}) {
  const leads = [...(data.leads || [])].sort((a, b) => b.temperature - a.temperature || String(a.id).localeCompare(b.id));
  const kept = leads.slice(0, Math.max(0, maxNodes));
  const keep = new Set(kept.map((l) => l.id));
  const near = new Map(kept.map((l) => [l.id, []]));
  for (const link of data.links || []) {
    if (link.kind === "shared" && keep.has(link.source) && keep.has(link.target)) {
      near.get(link.source).push(link.target); near.get(link.target).push(link.source);
    }
  }
  const order = [], seen = new Set();
  for (const lead of kept) {  // Hottest first; its neighbours right after it.
    if (seen.has(lead.id)) continue;
    const queue = [lead.id]; seen.add(lead.id);
    while (queue.length) {
      const id = queue.shift(); order.push(id);
      for (const next of near.get(id)) if (!seen.has(next)) { seen.add(next); queue.push(next); }
    }
  }
  const byId = new Map(kept.map((l) => [l.id, l]));
  const nodes = [], where = new Map(), n = order.length;
  order.forEach((id, i) => {
    let pos;
    if (n === 1) pos = [0, 0, radius];
    else {
      const y = 1 - (2 * (i + 0.5)) / n, r = Math.sqrt(1 - y * y), phi = i * GOLDEN;
      pos = [Math.cos(phi) * r * radius, y * radius, Math.sin(phi) * r * radius];
    }
    where.set(id, pos);
    const lead = byId.get(id);
    nodes.push({ id, type: "lead", pos, r: leadRadius(lead), temperature: lead.temperature, ref: lead });
  });
  const edges = [];
  for (const contact of data.contacts || []) {
    const mine = (contact.leads || []).filter((id) => where.has(id));
    if (!mine.length) continue;
    const sum = mine.reduce((acc, id) => acc.map((v, k) => v + where.get(id)[k]), [0, 0, 0]);
    let dir = Math.hypot(...sum) < radius * 0.2 ? norm(where.get(mine[0])) : norm(sum);
    const j = hash(contact.id) * Math.PI * 2;  // A small, stable offset around the lead.
    const t1 = norm([-dir[2], 0, dir[0]].some((v) => v) ? [-dir[2], 0, dir[0]] : [1, 0, 0]);
    const t2 = [dir[1] * t1[2] - dir[2] * t1[1], dir[2] * t1[0] - dir[0] * t1[2], dir[0] * t1[1] - dir[1] * t1[0]];
    const spread = 0.09 + 0.05 * hash(contact.id + "r");
    dir = norm(dir.map((v, k) => v + spread * (Math.cos(j) * t1[k] + Math.sin(j) * t2[k])));
    const pos = dir.map((v) => v * radius * 1.3);
    nodes.push({ id: contact.id, type: "contact", pos, r: 1.6 + 0.6 * Math.min(3, mine.length - 1), temperature: null, ref: contact });
    for (const id of mine) edges.push({ a: id, b: contact.id, kind: "contact" });
  }
  for (const link of data.links || []) {
    if (link.kind === "shared" && where.has(link.source) && where.has(link.target)) edges.push({ a: link.source, b: link.target, kind: "shared" });
  }
  return { nodes, edges, hidden: leads.length - kept.length };
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
  const maxNodes = options.maxNodes ?? (small ? 40 : 120);
  const maxLabels = options.labels ?? (small ? 10 : 24);
  if (!webglAvailable()) {
    el.classList.add("globe-unsupported");
    return { supported: false, update() {}, select() {}, focus() {}, destroy() {} };
  }
  const THREE = await import(options.threeUrl || THREE_URL);
  const stops = [[0, cssColor(el, "--temp-cold", DEFAULT_STOPS[0][1])], [35, cssColor(el, "--temp-cool", DEFAULT_STOPS[1][1])],
    [65, cssColor(el, "--temp-warm", DEFAULT_STOPS[2][1])], [100, cssColor(el, "--temp-hot", DEFAULT_STOPS[3][1])]];
  const ink = cssColor(el, "--ash", "#9aa5ba"), signal = cssColor(el, "--signal", "#4b8bff");

  const renderer = new THREE.WebGLRenderer({ antialias: !small, alpha: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setClearColor(0x000000, 0);
  const canvas = renderer.domElement;
  canvas.className = "globe-canvas";
  canvas.setAttribute("aria-hidden", "true");
  const labels = document.createElement("div");
  labels.className = "globe-labels";
  el.append(canvas, labels);

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
  let graph = null, meshes = [], byId = new Map(), edgeObjects = [], labelEls = new Map();
  let selected = null;

  function clear() {
    for (const object of [...meshes, ...edgeObjects]) {
      world.remove(object);
      object.geometry !== ball && object.geometry.dispose();
      object.material.dispose();
    }
    meshes = []; edgeObjects = []; byId = new Map();
    labels.replaceChildren(); labelEls = new Map();
  }

  function build(next) {
    clear();
    graph = layout(next, { maxNodes });
    for (const node of graph.nodes) {
      // CSS strings are read as sRGB, so the nodes match the page's own colours.
      const color = new THREE.Color(node.type === "lead" ? `rgb(${tempColor(node.temperature, stops).join(",")})` : ink);
      const material = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: node.type === "lead" ? (node.ref.status === "expired" ? 0.45 : 1) : 0.8 });
      const mesh = new THREE.Mesh(ball, material);
      mesh.position.set(...node.pos);
      mesh.scale.setScalar(node.r);
      mesh.userData = node;
      world.add(mesh); meshes.push(mesh); byId.set(node.id, mesh);
      if (node.type === "lead" && node.temperature >= 60) {  // A soft halo on hot leads.
        const halo = new THREE.Mesh(ball, new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.16, depthWrite: false }));
        halo.scale.setScalar(1.9); halo.userData = { halo: true };
        mesh.add(halo);
      }
    }
    const plain = [];
    for (const edge of graph.edges) {
      const a = byId.get(edge.a), b = byId.get(edge.b);
      if (!a || !b) continue;
      if (edge.kind === "contact") { plain.push(...a.position.toArray(), ...b.position.toArray()); continue; }
      const mid = a.position.clone().add(b.position).multiplyScalar(0.5);
      const lift = mid.clone().normalize().multiplyScalar(RADIUS * 1.18 + a.position.distanceTo(b.position) * 0.15);
      const curve = new THREE.QuadraticBezierCurve3(a.position.clone(), lift, b.position.clone());
      const arc = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(24)),
        new THREE.LineBasicMaterial({ color: signal, transparent: true, opacity: 0.55 }));
      arc.userData = edge; world.add(arc); edgeObjects.push(arc);
    }
    if (plain.length) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(plain, 3));
      const lines = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color: ink, transparent: true, opacity: 0.22 }));
      world.add(lines); edgeObjects.push(lines);
    }
    graph.nodes.filter((n) => n.type === "lead").slice(0, maxLabels).forEach((node) => {
      const label = document.createElement("span");
      label.className = "globe-label";
      label.textContent = node.ref.company;
      labels.append(label); labelEls.set(node.id, label);
    });
    for (const node of graph.nodes.filter((n) => n.type === "contact")) {  // Shown around a selection.
      const label = document.createElement("span");
      label.className = "globe-label is-person is-hidden";
      label.textContent = node.ref.name;
      labels.append(label); labelEls.set(node.id, label);
    }
    el.dataset.nodes = String(graph.nodes.length);
    el.dataset.hidden = String(graph.hidden);
    if (selected && !byId.has(selected)) selected = null;
    highlight();
    dirty = true;
  }

  function neighbours(id) {
    const set = new Set([id]);
    for (const edge of graph.edges) {
      if (edge.a === id) set.add(edge.b);
      if (edge.b === id) set.add(edge.a);
    }
    return set;
  }

  function highlight() {
    const near = selected ? neighbours(selected) : null;
    for (const mesh of meshes) {
      const node = mesh.userData, on = !near || near.has(node.id);
      const base = node.type === "lead" ? (node.ref.status === "expired" ? 0.45 : 1) : 0.8;
      mesh.material.opacity = on ? base : 0.12;
      mesh.scale.setScalar(node.r * (node.id === selected ? 1.25 : 1));
    }
    for (const object of edgeObjects) {
      const edge = object.userData;
      if (edge && edge.kind === "shared") object.material.opacity = !near || (near.has(edge.a) && near.has(edge.b)) ? 0.55 : 0.08;
      else if (!edge || !edge.kind) object.material.opacity = near ? 0.1 : 0.22;
    }
    for (const [id, label] of labelEls) {
      const person = label.classList.contains("is-person");
      label.classList.toggle("is-dim", !person && !!near && !near.has(id));
      if (person) label.classList.toggle("is-hidden", !near || !near.has(id));
    }
    dirty = true;
  }

  // Rotation, zoom and picking.
  const spin = new THREE.Quaternion();
  let velocity = { x: 0, y: 0 }, dragging = false, moved = 0, last = null, lastInput = 0, dirty = true;
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
    const found = raycaster.intersectObjects(meshes, false).find((h) => h.object.material.opacity > 0.2 || !selected);
    return found ? found.object.userData : null;
  }
  function hover(event) {
    canvas.style.cursor = hit(event) ? "pointer" : "grab";
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
      options.onSelect(node ? { type: node.type, id: node.id, [node.type]: node.ref } : null);
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
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.style.touchAction = "none";

  function resize() {
    const width = Math.max(1, el.clientWidth), height = Math.max(1, el.clientHeight);
    renderer.setSize(width, height, false);
    canvas.style.width = width + "px"; canvas.style.height = height + "px";
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    if (!zoomed) {  // Fit the sphere and its people in the narrower side.
      const half = Math.atan(Math.tan(camera.fov * Math.PI / 360) * Math.min(1, camera.aspect));
      distance = Math.max(260, (RADIUS * 1.42) / Math.tan(half));
    }
    dirty = true;
  }
  const observer = new ResizeObserver(resize);
  observer.observe(el);
  resize();

  const projected = new THREE.Vector3();
  function placeLabels() {
    const width = el.clientWidth, height = el.clientHeight;
    for (const [id, label] of labelEls) {
      const mesh = byId.get(id);
      mesh.getWorldPosition(projected);
      const front = projected.z > -RADIUS * 0.15;
      projected.project(camera);
      label.style.transform = `translate(${((projected.x + 1) / 2 * width).toFixed(1)}px, ${((1 - projected.y) / 2 * height).toFixed(1)}px)`;
      label.classList.toggle("is-back", !front);
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
    } else if (!dragging && auto && !selected && performance.now() - lastInput > 2500) {
      rotate(0.0016, 0);
    }
    camera.position.set(0, 0, distance);
    camera.lookAt(0, 0, 0);
    if (!dirty) return;
    renderer.render(scene, camera);
    placeLabels();
    dirty = false;
  }

  build(data);
  world.quaternion.setFromEuler(new THREE.Euler(0.35, -0.4, 0));
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
      canvas.remove(); labels.remove();
    },
  };
}
