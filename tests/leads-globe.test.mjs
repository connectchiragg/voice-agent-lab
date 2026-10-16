import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { bandColor, bandOf, BANDS, DEFAULT_PALETTE, layout, nodeRadius, placeLabels, RADIUS, tipText } from "../static/leads-globe.js";

const lead = (id, temperature, stage = "applied", extra = {}) => ({ id, company: id, temperature, stage, status: "active", ...extra });
const NOW = new Date("2026-10-16T09:00:00Z");

test("bands follow app/leads.band_of; closed is its own band", () => {
  assert.deepEqual([0, 24, 25, 49, 50, 74, 75, 100].map((t) => bandOf(lead("x", t))),
    ["cold", "cold", "cool", "cool", "warm", "warm", "hot", "hot"]);
  assert.equal(bandOf(lead("x", 90, "closed-lost")), "closed");
  assert.equal(bandOf(lead("x", 90, "interviewing", { status: "closed" })), "closed");
  assert.equal(bandOf(lead("x", 90, "screening", { band: "cool" })), "cool");  // The server's band wins.
});

test("colour: a distinct colour per band, a smooth ramp inside it", () => {
  for (const [band, low, high] of BANDS) {
    assert.deepEqual(bandColor(low, band), hexRgb(DEFAULT_PALETTE[band][0]));
    assert.deepEqual(bandColor(high, band), hexRgb(DEFAULT_PALETTE[band][1]));
  }
  const step = (a, b) => dE(oklab(rgbLin(a)), oklab(rgbLin(b)));
  // Across a band edge the colour jumps; one point inside a band it barely moves.
  for (const [t, lower, upper] of [[25, "cold", "cool"], [50, "cool", "warm"], [75, "warm", "hot"]]) {
    assert.ok(step(bandColor(t - 1, lower), bandColor(t, upper)) > 5 * step(bandColor(t - 2, lower), bandColor(t - 1, lower)));
  }
});

test("the four bands stay apart for colour-blind eyes, in both themes (static/leads.css)", () => {
  const css = readFileSync(new URL("../static/leads.css", import.meta.url), "utf8");
  const block = (selector) => {
    const body = css.slice(css.indexOf(selector)).split("}")[0];
    return Object.fromEntries([...body.matchAll(/--band-([a-z]+(?:-lo)?):\s*(#[0-9a-f]{6})/g)].map((m) => [m[1], m[2]]));
  };
  const dark = block(":root {"), light = block(':root[data-theme="light"]');
  assert.deepEqual(block(".hero-globe {"), dark);
  for (const [name, set] of [["dark", dark], ["light", light]]) {
    const keys = ["cold", "cool", "warm", "hot"];
    for (const vision of [null, "deutan", "protan", "tritan"]) {
      for (let i = 0; i < keys.length; i++) for (let j = i + 1; j < keys.length; j++) {
        for (const end of ["", "-lo"]) {
          const a = simulate(rgbLin(hexRgb(set[keys[i] + end])), vision), b = simulate(rgbLin(hexRgb(set[keys[j] + end])), vision);
          assert.ok(dE(oklab(a), oklab(b)) >= 0.06, `${name} ${vision || "normal"}: ${keys[i]}${end} vs ${keys[j]}${end}`);
        }
      }
    }
    // Closed is a neutral grey: no hue.
    const [, a, b] = oklab(rgbLin(hexRgb(set.closed)));
    assert.ok(Math.hypot(a, b) < 0.03, `${name} closed is grey`);
  }
});

test("only leads are drawn: no people, no links; size follows stage, not temperature", () => {
  const data = { leads: [lead("a", 90, "interviewing"), lead("b", 20, "applied"), lead("c", 60, "offer")],
    contacts: [{ id: "c-1", name: "Nina", leads: ["a"] }], links: [{ source: "a", target: "c-1", kind: "contact" }] };
  const { nodes, hidden } = layout(data);
  assert.equal(hidden, 0);
  assert.deepEqual(nodes.map((n) => n.id), ["a", "c", "b"]);
  assert.ok(nodes.every((n) => Math.abs(Math.hypot(...n.pos) - RADIUS) < 1e-6));
  assert.ok(nodeRadius(lead("x", 10, "offer")) > nodeRadius(lead("y", 99, "interviewing")));
  assert.ok(nodeRadius(lead("x", 10, "interviewing")) > nodeRadius(lead("y", 99, "applied")));
  assert.equal(nodeRadius(lead("x", 10, "screening")), nodeRadius(lead("y", 99, "screening")));
});

test("hot at the top, cold at the bottom, closed last; expired never", () => {
  const leads = [lead("cold", 10), lead("hot", 80, "interviewing"), lead("closed", 5, "closed-lost", { status: "closed" }),
    lead("warm", 55), lead("cool", 30), lead("gone", 40, "applied", { status: "expired" })];
  const { nodes } = layout({ leads });
  assert.deepEqual(nodes.map((n) => n.id), ["hot", "warm", "cool", "cold", "closed"]);
  const ys = nodes.map((n) => n.pos[1]);
  assert.deepEqual(ys, [...ys].sort((a, b) => b - a));
});

test("80 leads spread evenly: no clumps", () => {
  const leads = Array.from({ length: 80 }, (_, i) => lead(`l${i}`, (i * 37) % 100));
  const { nodes } = layout({ leads });
  let min = Infinity;
  for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
    min = Math.min(min, Math.hypot(...nodes[i].pos.map((v, k) => v - nodes[j].pos[k])));
  }
  const ideal = RADIUS * Math.sqrt((4 * Math.PI * 0.9) / 80);  // The spacing of a perfect grid on the band used.
  assert.ok(min > 0.55 * ideal, `closest pair ${min.toFixed(1)} vs ideal ${ideal.toFixed(1)}`);
});

test("labels: a colliding label moves to a free slot; hidden only when all are taken or on the back", () => {
  const base = { w: 60, h: 12, r: 4, forced: 0 };
  const items = [
    { ...base, id: "near", x: 100, y: 100, depth: 0.9 },
    { ...base, id: "close", x: 100, y: 110, depth: 0.4 },  // Its right-hand slot is taken: it goes elsewhere.
    { ...base, id: "back", x: 400, y: 100, depth: -0.6 },
    { ...base, id: "edge", x: 790, y: 300, depth: 0.0 },
  ];
  const out = placeLabels(items, { width: 800, height: 600 });
  assert.equal(out.get("near").show, true);
  assert.equal(out.get("near").x, 100 + 4 + 3);  // First choice: right of its dot.
  assert.equal(out.get("close").show, true);
  assert.notDeepEqual([out.get("close").x, out.get("close").y], [107, 110 - 6]);
  assert.equal(out.get("back").show, false);
  const edge = out.get("edge");
  assert.ok(edge.show && edge.x + 60 <= 800, "flips left at the right edge");
  assert.ok(edge.opacity > 0 && edge.opacity < 1, "fades as it turns to the front");
  // Boxed in on all sides: a leader line takes it further out.
  const crowd = [{ ...base, id: "c", x: 300, y: 300, depth: 1 }];
  for (const [i, [dx, dy]] of [[0, 1], [0, -1], [1, 0], [-1, 0], [1, 1], [1, -1], [-1, 1], [-1, -1]].entries()) {
    crowd.push({ ...base, id: `n${i}`, x: 300 + dx * 40, y: 300 + dy * 16, depth: 1.1 });
  }
  const boxed = placeLabels(crowd, { width: 800, height: 600 }).get("c");
  assert.ok(boxed.show);
});

test("at least 90% of the front-facing dots get a label (70 leads, desktop panel)", () => {
  const leads = Array.from({ length: 70 }, (_, i) => lead(`${["ARC", "BRIGHT", "CINDER", "DELTA", "ECHO", "FABLE", "GLINT"][i % 7]}${["WORKS", "LOOP", "FORGE", "STACK"][i % 4]}${i}`, 100 - i, "applied"));
  const { nodes } = layout({ leads });
  const W = 900, H = 660, scale = H / 2 / 1.12 / RADIUS;  // The sphere fills about 89% of the panel height.
  let worst = 1;
  for (const turn of [0, 0.7, 1.4, 2.1, 2.8, 3.5, 4.2, 4.9, 5.6]) {
    const items = nodes.map((n) => {
      const [x0, y0, z0] = n.pos;
      const x = x0 * Math.cos(turn) + z0 * Math.sin(turn), z = -x0 * Math.sin(turn) + z0 * Math.cos(turn);
      return { id: n.id, x: W / 2 + x * scale, y: H / 2 - y0 * scale, r: n.r * scale, w: n.ref.company.length * 6.6 + 4, h: 13,
        depth: z / RADIUS, forced: 0 };
    });
    const out = placeLabels(items, { width: W, height: H });
    const front = items.filter((it) => it.depth > 0);
    worst = Math.min(worst, front.filter((it) => out.get(it.id).show).length / front.length);
  }
  assert.ok(worst >= 0.9, `worst share ${worst.toFixed(2)}`);
});

test("the tooltip: company · band · score · stage · last touch", () => {
  const l = lead("Acme", 82, "interviewing", { last_touch_at: "2026-10-13T08:00:00Z" });
  assert.equal(tipText(l, NOW), "Acme · Hot · 82 · Interviewing · 3 days ago");
  assert.equal(tipText({ ...l, stage: "closed-lost", status: "closed", last_touch_at: null }, NOW),
    "Acme · Closed · 82 · Not moving forward · no touch yet");
});

test("layout is deterministic and handles one or no lead", () => {
  const data = { leads: [lead("a", 90), lead("b", 20)] };
  assert.deepEqual(layout(data), layout(data));
  assert.deepEqual(layout({ leads: [lead("x", 50)] }).nodes[0].pos, [0, 0, RADIUS]);
  assert.deepEqual(layout({}).nodes, []);
});

// Colour maths: sRGB -> OKLab, and Machado et al. (2009) colour-blindness simulation at full severity.
function hexRgb(hex) { const v = parseInt(hex.slice(1), 16); return [(v >> 16) & 255, (v >> 8) & 255, v & 255]; }
function rgbLin(rgb) { return rgb.map((c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; }); }
function oklab([r, g, b]) {
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s, 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s];
}
const MACHADO = {
  deutan: [[0.367322, 0.860646, -0.227968], [0.280085, 0.672501, 0.047413], [-0.01182, 0.04294, 0.968881]],
  protan: [[0.152286, 1.052583, -0.204868], [0.114503, 0.786281, 0.099216], [-0.003882, -0.048116, 1.051998]],
  tritan: [[1.255528, -0.076749, -0.178779], [-0.078411, 0.930809, 0.147602], [0.004733, 0.691367, 0.3039]],
};
function simulate(rgb, vision) {
  if (!vision) return rgb;
  return MACHADO[vision].map((row) => Math.max(0, Math.min(1, row[0] * rgb[0] + row[1] * rgb[1] + row[2] * rgb[2])));
}
function dE(a, b) { return Math.hypot(...a.map((v, i) => v - b[i])); }

test("final stages sit between interviewing and an offer in size", () => {
  const r = (stage) => nodeRadius({ stage });
  assert.ok(r("interviewing") < r("final") && r("final") < r("offer"));
  assert.equal(tipText({ company: "Acme", temperature: 92, stage: "final" }, NOW), "Acme · Hot · 92 · Final stages · no touch yet");
});
