// Settings › API keys (static/valr-keys.js): the saved key's one-line summary.
import assert from "node:assert/strict";
import test from "node:test";

import {describe, statusLabel} from "../static/valr-keys.js";

test("a saved key reads as provider, model and its last four characters", () => {
  const row = {provider: "openai", label: "OpenAI", model: "gpt-6-luna", last4: "1234", status: "ok"};
  assert.equal(describe(row), "OpenAI · gpt-6-luna · key ending …1234");
  assert.equal(statusLabel(row), "Working");
});

test("a Deepgram key has no model, and no key says so", () => {
  assert.equal(describe({provider: "deepgram", label: "Deepgram", model: "", last4: "9876", status: "unchecked"}),
               "Deepgram · key ending …9876");
  assert.equal(statusLabel({status: "rejected"}), "Rejected");
  assert.equal(describe(null), "No key yet.");
  assert.equal(statusLabel(null), "");
});
