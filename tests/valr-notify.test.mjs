// The bell's pure parts (static/valr-notify.js).
import test from "node:test";
import assert from "node:assert/strict";
import {badgeCount, groupByDay, mailSource, safeHref} from "../static/valr-notify.js";

test("items are grouped by day, one company's items folded together", () => {
  const days = groupByDay([
    {id: 1, day: "Today", company: "Juniper"}, {id: 2, day: "Today", company: "Kite"},
    {id: 3, day: "Today", company: "Juniper"}, {id: 4, day: "Yesterday", company: ""}]);
  assert.deepEqual(days.map(d => d.day), ["Today", "Yesterday"]);
  assert.deepEqual(days[0].rows.map(r => [r.company, r.items.map(i => i.id)]), [["Juniper", [1, 3]], ["Kite", [2]]]);
});

test("the count adds action items and caps at 99+", () => {
  assert.equal(badgeCount(0, 0), "");
  assert.equal(badgeCount(3, 1), "4");
  assert.equal(badgeCount(120, 0), "99+");
});

test("links are https or same-origin paths only", () => {
  assert.equal(safeHref("/valr/leads?lead=a", "https://valr.example.test"), "/valr/leads?lead=a");
  assert.equal(safeHref("https://mail.google.com/mail/#all/t1"), "https://mail.google.com/mail/#all/t1");
  assert.equal(safeHref("javascript:alert(1)", "https://valr.example.test"), "");
});

test("message links say where they open: LinkedIn threads, else Gmail", () => {
  assert.equal(mailSource("https://www.linkedin.com/messaging/thread/2-abc/"), "LinkedIn");
  assert.equal(mailSource("https://linkedin.com/in/x"), "LinkedIn");
  assert.equal(mailSource("https://mail.google.com/mail/u/0/#all/abc"), "Gmail");
  assert.equal(mailSource("https://evil-linkedin.com.example/x"), "Gmail");
  assert.equal(mailSource("not a url"), "Gmail");
});
