// static/valr-globe-lite.js (the fallback globe: empty Home, no WebGL) in a minimal fake DOM.
import test from "node:test";
import assert from "node:assert/strict";

let observers = [];
function fakeDom() {
  observers = [];
  const noop = () => {};
  const ctx = new Proxy({}, {get: (target, key) => key in target ? target[key]
    : key === "measureText" ? () => ({width: 40})
    : key === "createRadialGradient" ? () => ({addColorStop: noop}) : noop,
    set: (target, key, value) => { target[key] = value; return true; }});
  globalThis.document = {
    hidden: false, addEventListener: noop, removeEventListener: noop,
    createElement: () => ({
      style: {}, width: 0, height: 0, setAttribute: noop, addEventListener: noop, remove: noop,
      getContext: () => ctx, getBoundingClientRect: () => ({left: 0, top: 0, width: 600, height: 440}),
    }),
  };
  globalThis.getComputedStyle = () => ({getPropertyValue: () => ""});
  globalThis.matchMedia = () => ({matches: true});  // Reduced motion: no animation loop.
  globalThis.devicePixelRatio = 2;
  globalThis.requestAnimationFrame = () => 1;
  globalThis.cancelAnimationFrame = noop;
  globalThis.ResizeObserver = class { constructor(fn) { this.fn = fn; observers.push(this); } observe() {} disconnect() {} };
  globalThis.IntersectionObserver = class { observe() {} disconnect() {} };
}

// A container whose height is its min-height plus whatever in-flow content it holds.
function container() {
  const children = [];
  return {
    dataset: {mode: "home"}, children,
    append(node) { children.push(node); },
    getBoundingClientRect() {
      const inFlow = children.filter(c => c.style.position !== "absolute")
        .reduce((h, c) => h + (c.height || 0) / 2, 0);  // An unstyled canvas adds its own height.
      return {left: 0, top: 0, width: 600, height: Math.max(440, inFlow)};
    },
  };
}

test("the lite globe's canvas is out of flow, so a resize never makes the box taller", async () => {
  fakeDom();
  const {mount} = await import("../static/valr-globe-lite.js");
  const el = container();
  const view = mount(el, {leads: []});
  const canvas = el.children[0];
  assert.equal(canvas.style.position, "absolute");
  assert.equal(canvas.className, "globe-canvas");
  const heights = [];
  for (let i = 0; i < 5; i++) {  // What the ResizeObserver did every frame.
    observers[0].fn();
    heights.push(el.getBoundingClientRect().height);
  }
  assert.deepEqual(heights, [440, 440, 440, 440, 440]);
  assert.equal(canvas.height, 880);  // 440 css px at dpr 2, once.
  view.destroy();
});
