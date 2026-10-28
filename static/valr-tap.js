// Instant feedback for every tap (every page; loaded first, before the other modules).
//
// - Pressed: .is-pressed on the control under the finger at pointerdown, in the same frame,
//   cleared when the finger lifts, slides off or scrolls. A touchstart listener is also what
//   makes iOS Safari apply :active at all.
// - Leaving: a tap on a link to another page (or a form that submits) shows the progress bar
//   and the tapped tab as current at once, while the server renders the next page; a page
//   restored from the back/forward cache clears it.

export const PRESSABLE = 'a[href], button:not([disabled]), summary, label, select, [role="button"], [data-pressable]';

// Is this click a plain navigation to another page of this site (not a new tab, a download,
// an in-page anchor or something another handler took over)?
export function isPageNavigation(event, link, here) {
  if (!link || event.defaultPrevented || event.button > 0) return false;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
  if ((link.target && link.target !== "_self") || link.hasAttribute("download")) return false;
  let url;
  try { url = new URL(link.getAttribute("href"), here.href); } catch { return false; }
  if (url.origin !== here.origin || !/^https?:$/.test(url.protocol)) return false;
  if (url.pathname === here.pathname && url.search === here.search) return false;  // Same page (an #anchor).
  return true;
}

// The nav entry (top nav or tab bar) for a path: the longest prefix wins ("/valr" only for Home).
export function sectionHref(path, hrefs) {
  let best = "";
  for (const href of hrefs) {
    const match = href === "/valr" ? path === "/valr" || path === "/valr/" : path === href || path.startsWith(href + "/");
    if (match && href.length > best.length) best = href;
  }
  return best;
}

function boot(doc = document, win = window) {
  const root = doc.documentElement;
  let pressed = null;
  let timer = 0;
  const release = () => {
    clearTimeout(timer);
    pressed?.classList.remove("is-pressed");
    pressed = null;
  };
  doc.addEventListener("touchstart", () => {}, {passive: true});  // iOS: :active only with a touch listener.
  doc.addEventListener("pointerdown", event => {
    if (event.button > 0) return;
    const target = event.target.closest?.(PRESSABLE);
    release();
    if (!target || target.matches('[aria-disabled="true"], input[type="range"]')) return;
    pressed = target;
    target.classList.add("is-pressed");
    timer = setTimeout(release, 1200);  // Never stuck pressed (a long press, a missed pointerup).
  }, {capture: true, passive: true});
  for (const type of ["pointerup", "pointercancel", "dragstart"]) {
    doc.addEventListener(type, () => setTimeout(release, 90), {capture: true, passive: true});  // Long enough to see.
  }
  win.addEventListener("scroll", release, {capture: true, passive: true});

  let bar = null;
  const leaving = href => {
    if (!bar) {
      bar = doc.createElement("div");
      bar.className = "nav-progress";
      bar.setAttribute("aria-hidden", "true");
      doc.body.append(bar);
    }
    root.classList.add("is-navigating");
    if (!href) return;
    const path = new URL(href, win.location.href).pathname;
    const navs = doc.querySelectorAll(".nav, .tabbar");
    for (const nav of navs) {
      const links = [...nav.querySelectorAll("a[href]")];
      const href = sectionHref(path, links.map(a => a.getAttribute("href")));
      if (!href) continue;
      for (const a of links) {
        if (a.getAttribute("href") === href) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
      }
    }
  };
  // Bubble phase on window: every other click handler has run (and may have prevented it).
  win.addEventListener("click", event => {
    const link = event.target.closest?.("a[href]");
    if (isPageNavigation(event, link, win.location)) leaving(link.href);
  });
  // A script that navigates itself (a notification, a saved correction) says so.
  doc.addEventListener("valr:leaving", event => leaving(event.detail?.href || null));
  win.addEventListener("submit", event => {
    if (!event.defaultPrevented && (event.target.method || "get").toLowerCase() !== "dialog") leaving(null);
  });
  // A script's own submit() (the Leads filters) fires no submit event.
  doc.addEventListener("change", event => {
    if (event.target.matches?.("[data-autosubmit]")) leaving(null);
  });
  win.addEventListener("pageshow", event => {
    if (!event.persisted) return;
    root.classList.remove("is-navigating");  // Back from the cache: this page again, as it was.
    release();
    doc.querySelectorAll(".nav a[href], .tabbar a[href]").forEach(a => {
      const current = a.dataset.current === "1";
      if (current) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
  });
  // Remember the server's current tab, to restore it after a back navigation.
  doc.querySelectorAll('.nav a[aria-current="page"], .tabbar a[aria-current="page"]').forEach(a => { a.dataset.current = "1"; });
}

if (typeof document !== "undefined") boot();
