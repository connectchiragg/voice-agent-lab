import assert from "node:assert/strict";
import test from "node:test";
import { layout, leadRadius, RADIUS, tempColor } from "../static/leads-globe.js";

const lead = (id, temperature, activity = 1) => ({ id, company: id, temperature, activity, status: "active" });

const data = {
  leads: [lead("a", 90, 9), lead("b", 20), lead("c", 60, 4), lead("d", 5)],
  contacts: [{ id: "c-1", name: "Nina", role: "recruiter", leads: ["a", "d"] }, { id: "c-2", name: "Ravi", role: "interviewer", leads: ["c"] }],
  links: [{ source: "a", target: "c-1", kind: "contact" }, { source: "d", target: "c-1", kind: "contact" },
    { source: "c", target: "c-2", kind: "contact" }, { source: "a", target: "d", kind: "shared", weight: 1 }],
};

test("temperature colours run from cold blue to hot orange", () => {
  assert.deepEqual(tempColor(0), [0x3b, 0x7b, 0xff]);
  assert.deepEqual(tempColor(100), [0xff, 0x6a, 0x2b]);
  assert.deepEqual(tempColor(-5), tempColor(0));
  assert.ok(tempColor(80)[0] > tempColor(20)[0] && tempColor(80)[2] < tempColor(20)[2]);
});

test("leads sit on the sphere, people just outside, sized by activity", () => {
  const { nodes, edges, hidden } = layout(data);
  assert.equal(hidden, 0);
  const leads = nodes.filter((n) => n.type === "lead"), people = nodes.filter((n) => n.type === "contact");
  assert.equal(leads.length, 4); assert.equal(people.length, 2);
  for (const n of leads) assert.ok(Math.abs(Math.hypot(...n.pos) - RADIUS) < 1e-6);
  for (const n of people) assert.ok(Math.abs(Math.hypot(...n.pos) - RADIUS * 1.3) < 1e-6);
  assert.ok(leadRadius(data.leads[0]) > leadRadius(data.leads[1]));
  assert.equal(edges.filter((e) => e.kind === "contact").length, 3);
  assert.deepEqual(edges.filter((e) => e.kind === "shared"), [{ a: "a", b: "d", kind: "shared" }]);
  // Hottest first, and the lead sharing a person with it right after it.
  assert.deepEqual(leads.map((n) => n.id).slice(0, 2), ["a", "d"]);
});

test("the node cap keeps the hottest leads and drops their orphans", () => {
  const { nodes, hidden } = layout(data, { maxNodes: 2 });
  assert.deepEqual(nodes.filter((n) => n.type === "lead").map((n) => n.id).sort(), ["a", "c"]);
  assert.equal(hidden, 2);
  assert.equal(nodes.filter((n) => n.type === "contact").length, 2);
});

test("layout is deterministic and handles one or no lead", () => {
  assert.deepEqual(layout(data), layout(data));
  assert.deepEqual(layout({ leads: [lead("x", 50)], contacts: [], links: [] }).nodes[0].pos, [0, 0, RADIUS]);
  assert.deepEqual(layout({}).nodes, []);
});
