// The bell's pure parts (static/valr-notify.js).
import test from "node:test";
import assert from "node:assert/strict";
import {badgeCount, dropLeadDots, groupByDay, mailSource, opener, safeHref} from "../static/valr-notify.js";

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

test("a bell item opens at once; its read mark doesn't hold up the navigation", async () => {
  const log = [];
  let settle;
  const open = opener({
    mark: id => { log.push(`mark ${id}`); return new Promise(resolve => { settle = resolve; }); },
    show: n => log.push(`show ${n}`), cleared: id => log.push(`cleared ${id}`), go: href => log.push(`go ${href}`)});
  const item = {id: 7, read: false};
  open(item, "/valr/leads?lead=x#lead");
  assert.deepEqual(log, ["cleared 7", "mark 7", "go /valr/leads?lead=x#lead"]);  // Before the mark answered.
  settle({unread: 2});
  await new Promise(r => setTimeout(r, 0));
  assert.equal(item.read, true);
  open(item, "/valr");  // Already read: no second mark.
  assert.equal(log.filter(x => x.startsWith("mark")).length, 1);
});

test("nothing unread clears the Leads dots", () => {
  const removed = [];
  const doc = {querySelectorAll: sel => (sel.includes("data-lead-dot") ? [{remove: () => removed.push(1)}, {remove: () => removed.push(2)}] : [])};
  dropLeadDots(doc, 3);
  assert.equal(removed.length, 0);
  dropLeadDots(doc, 0);
  assert.equal(removed.length, 2);
});
