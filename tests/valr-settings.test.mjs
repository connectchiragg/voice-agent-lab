// Settings and Profile: static/valr-settings.js's pure parts (phone check, square crop, initials).
import test from "node:test";
import assert from "node:assert/strict";
import {cropBox, initials, phoneProblem} from "../static/valr-settings.js";

test("the phone check matches the server's rules", () => {
  for (const ok of ["98000 00001", "098000 00001", "+91 98000-00001", "0091 9800000001", "+1 (415) 555-0123", "+44 7700 900123"]) {
    assert.equal(phoneProblem(ok), "", ok);
  }
  assert.match(phoneProblem(""), /Enter/);
  assert.match(phoneProblem("12345"), /country code/);
  assert.match(phoneProblem("+91 22234 56789"), /mobile/);
  assert.match(phoneProblem("+91 98000 0000"), /10 digits/);
  assert.match(phoneProblem("+12"), /doesn't look/);
});

test("the crop is a square inside the photo, centred by default", () => {
  assert.deepEqual(cropBox(800, 500), {x: 150, y: 0, side: 500});
  assert.deepEqual(cropBox(800, 500, 2), {x: 275, y: 125, side: 250});
  assert.deepEqual(cropBox(800, 500, 1, -1), {x: 0, y: 0, side: 500});
  assert.deepEqual(cropBox(800, 500, 1, 5), {x: 300, y: 0, side: 500});  // Clamped to the edge.
});

test("initials from the display name", () => {
  assert.equal(initials("Sam Rivera"), "SR");
  assert.equal(initials("  alex  "), "A");
  assert.equal(initials(""), "?");
});
