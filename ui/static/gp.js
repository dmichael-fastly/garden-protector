/* gp.js — the ONE shared front-end helper module for Garden Protector (CHARTER: one
 * shared UI asset layer). Served at /static/gp.js by all three tiers (the edge
 * dashboard via include_str!, the Pi portal + console from disk) and linked from
 * <head> on every page so the pre-paint theme step runs before first render.
 *
 * Provides: the three-state theme system (pre-paint + #theme-toggle auto-wiring +
 * live OS-pref follow), an api() fetch wrapper with 401 -> sign-in redirect, an
 * sse() EventSource helper, a poll() timer, and nav active-marking. The api()/sse()
 * helpers are adopted incrementally by pages (existing inline fetch/EventSource
 * calls keep working).
 *
 * Mirrors the prior per-page theme logic verbatim (light/dark/system cycle, ☀/☾/◐),
 * so a page that deletes its inline copy renders identically.
 */

/* ---- pre-paint theme: apply the saved preference before first paint (no FOUC) ---- */
(function () {
  try {
    var pref = localStorage.getItem("gp-theme") || "system";
    var dark = pref === "dark" || (pref === "system" && window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
    // The 3-state PREFERENCE stays light/dark/system; the data-theme we WRITE is the
    // DaisyUI theme name (dark -> "forest", light -> "garden"). To swap the whole app's
    // theme, change these two strings here AND in applyTheme() + ui/tailwind/input.css.
    document.documentElement.setAttribute("data-theme", dark ? "forest" : "garden");
  } catch (e) {}
})();

window.GP = (function () {
  "use strict";

  // ---- inline SVG icon sprite ----
  // One hidden <svg> of <symbol>s injected into <body>; every page references an icon as
  // <svg class="gp-ic"><use href="#gp-NAME"/></svg>. Single-sourced here (served on every
  // page via /static/gp.js) so even the wizard/console — which don't splice the header
  // partial — get the same icon set, with no extra served file to cache-bust. Replaces the
  // emoji that used to stand in for icons. Stroke icons inherit color via currentColor;
  // .solid (set at the use site) switches to fill. viewBox is 0 0 24 24 throughout.
  var SPRITE = [
    ['gp-leaf', '<path d="M5 21c-1-7 3-14 14-17 1 8-2 16-9 17-3 .4-5-1-5-1z"/><path d="M9 18c1-5 4-8 8-10"/>'],
    ['gp-spray', '<path d="M12 3s5 6 5 9.5a5 5 0 0 1-10 0C7 9 12 3 12 3z"/>'],
    ['gp-shield', '<path d="M12 3l7 3v5c0 5-3 8-7 10-4-2-7-5-7-10V6z"/>'],
    ['gp-sun', '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19M19 5l-1.4 1.4M6.4 17.6L5 19"/>'],
    ['gp-moon', '<path d="M20 14.5A8 8 0 1 1 9.5 4 6.5 6.5 0 0 0 20 14.5z"/>'],
    ['gp-system', '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'],
    ['gp-camera', '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M8.5 7l1.5-3h4l1.5 3"/><circle cx="12" cy="13.5" r="3.2"/>'],
    ['gp-alert', '<path d="M12 3l9 16H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/>'],
    ['gp-rain', '<path d="M7 15a4 4 0 0 1 .5-8 5 5 0 0 1 9.6 1.4A3.5 3.5 0 0 1 17 15z"/><path d="M8 18l-1 2.5M12 18l-1 2.5M16 18l-1 2.5"/>'],
    ['gp-eye', '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>'],
    ['gp-check', '<path d="M5 13l4 4 10-11"/>'],
    ['gp-info', '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>'],
    ['gp-refresh', '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>'],
    ['gp-play', '<path d="M7 5l12 7-12 7z"/>'],
    ['gp-stop', '<rect x="6" y="6" width="12" height="12" rx="2"/>'],
    ['gp-trash', '<path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/>'],
    ['gp-plus', '<path d="M12 5v14M5 12h14"/>'],
    ['gp-gear', '<circle cx="12" cy="12" r="3.2"/><path d="M19.4 13a7.5 7.5 0 0 0 0-2l2-1.5-2-3.4-2.3 1a7.5 7.5 0 0 0-1.7-1L15 3h-4l-.4 2.6a7.5 7.5 0 0 0-1.7 1l-2.3-1-2 3.4 2 1.5a7.5 7.5 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7.5 7.5 0 0 0 1.7 1L11 21h4l.4-2.6a7.5 7.5 0 0 0 1.7-1l2.3 1 2-3.4z"/>'],
    ['gp-coin', '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>'],
    ['gp-scale', '<path d="M12 4v16M7 20h10"/><path d="M5 8l7-2 7 2"/><path d="M5 8l-2.2 5h4.4zM19 8l-2.2 5h4.4z"/>'],
    ['gp-sparkle', '<path d="M12 3l1.8 6.2L20 11l-6.2 1.8L12 19l-1.8-6.2L4 11l6.2-1.8z"/>'],
    // device-onboarding kinds/types + the UI glyphs the pages used to draw with emoji
    ['gp-video', '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/>'],
    ['gp-motion', '<circle cx="6" cy="12" r="2"/><path d="M11 7a8 8 0 0 1 0 10M15 4a13 13 0 0 1 0 16"/>'],
    ['gp-thermo', '<path d="M14 14V5a2 2 0 0 0-4 0v9a4 4 0 1 0 4 0z"/><path d="M12 14V9"/>'],
    ['gp-beam', '<path d="M5 4v16M19 4v16"/><path d="M5 12h14" stroke-dasharray="2 3"/>'],
    ['gp-mic', '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M6 11a6 6 0 0 0 12 0M12 17v4M9 21h6"/>'],
    ['gp-door', '<path d="M6 21V4a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v17M4 21h16M13.5 12h.01"/>'],
    ['gp-valve', '<path d="M3 8h7v4H3z"/><path d="M10 10h4a4 4 0 0 1 4 4M14 14v2a3 3 0 0 1-3 3"/>'],
    ['gp-speaker', '<path d="M4 9v6h4l5 4V5L8 9z"/><path d="M16 9.5a4 4 0 0 1 0 5M18.5 7a7 7 0 0 1 0 10"/>'],
    ['gp-plug', '<path d="M9 3v6M15 3v6M6 9h12v2a6 6 0 0 1-12 0zM12 17v4"/>'],
    ['gp-bolt', '<path d="M13 2L4 14h6l-1 8 9-12h-6z" fill="currentColor" stroke="none"/>'],
    ['gp-tag', '<path d="M3 12l9-9 8 1 1 8-9 9z"/><circle cx="14.5" cy="9.5" r="1.5"/>'],
    ['gp-search', '<circle cx="11" cy="11" r="6"/><path d="M15.5 15.5L20 20"/>'],
    ['gp-hand', '<path d="M8 12V6a1.5 1.5 0 0 1 3 0M11 11V5a1.5 1.5 0 0 1 3 0v6M14 11V7a1.5 1.5 0 0 1 3 0v7a6 6 0 0 1-6 6 6 6 0 0 1-4.2-1.7L5 15a1.6 1.6 0 0 1 2.3-2.2L8 13.5"/>'],
    ['gp-x', '<path d="M6 6l12 12M18 6L6 18"/>'],
    ['gp-edit', '<path d="M4 20l4-1L19 8l-3-3L5 16z"/><path d="M14 6l4 4"/>'],
    ['gp-pin', '<path d="M12 21s7-6 7-11a7 7 0 0 0-14 0c0 5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/>'],
    ['gp-upload', '<path d="M12 16V4M7 9l5-5 5 5M5 20h14"/>'],
    ['gp-lock', '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>'],
    ['gp-globe', '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>'],
    ['gp-cloud', '<path d="M7 18a4 4 0 0 1 .5-8 5 5 0 0 1 9.6 1.4A3.5 3.5 0 0 1 17 18z"/>'],
    ['gp-question', '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7M12 16.5h.01"/>'],
    ['gp-power', '<path d="M12 3v9"/><path d="M7.5 6.5a7 7 0 1 0 9 0"/>'],
    // log-line levels (rendered client-side from a {level} tag — see GP.logIcon)
    ['gp-step', '<path d="M9 6l6 6-6 6"/>'],
    ['gp-ok', '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.5 2.5 5-6"/>'],
    ['gp-warn', '<use href="#gp-alert"/>'],   // alias of gp-alert (same triangle path)
    ['gp-err', '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>']
  ];

  function buildSprite() {
    var syms = SPRITE.map(function (s) {
      return '<symbol id="' + s[0] + '" viewBox="0 0 24 24">' + s[1] + '</symbol>';
    }).join("");
    return '<svg width="0" height="0" aria-hidden="true" focusable="false" ' +
      'style="position:absolute;width:0;height:0;overflow:hidden" id="gp-sprite">' + syms + '</svg>';
  }

  function injectSprite() {
    if (!document.body || document.getElementById("gp-sprite")) return;
    var wrap = document.createElement("div");
    wrap.innerHTML = buildSprite();
    document.body.insertBefore(wrap.firstChild, document.body.firstChild);
  }

  // Build a <use> reference to a sprite symbol. cls is appended to the base "gp-ic" class.
  function svgUse(name, cls) {
    return '<svg class="gp-ic' + (cls ? " " + cls : "") + '" aria-hidden="true"><use href="#' +
      name + '"/></svg>';
  }

  // Map a log/level keyword to a colored sprite icon (for streamed deploy/provision logs).
  var LEVEL_ICON = { step: "gp-step", info: "gp-info", cmd: "gp-step",
                     ok: "gp-ok", warn: "gp-warn", err: "gp-err", error: "gp-err" };
  var LEVEL_CLS = { step: "", info: "info", cmd: "",
                    ok: "good", warn: "warn", err: "bad", error: "bad" };
  function logIcon(level) {
    var k = String(level || "step").toLowerCase();
    return svgUse(LEVEL_ICON[k] || "gp-step", LEVEL_CLS[k] || "");
  }

  // ---- theme toggle (light / dark / system) ----
  var KEY = "gp-theme";
  var ORDER = ["light", "dark", "system"];
  var ICON = { light: "gp-sun", dark: "gp-moon", system: "gp-system" };  // SVG sprite symbol ids
  var NAME = { light: "Light", dark: "Dark", system: "System" };
  var mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function getPref() { try { return localStorage.getItem(KEY) || "system"; } catch (e) { return "system"; } }
  function resolve(p) { return p === "system" ? (mq && mq.matches ? "dark" : "light") : p; }

  function applyTheme(pref) {
    // resolve() yields light/dark; map to the DaisyUI theme name (see the pre-paint note).
    document.documentElement.setAttribute("data-theme", resolve(pref) === "dark" ? "forest" : "garden");
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var next = ORDER[(ORDER.indexOf(pref) + 1) % ORDER.length];
    var desc = "Theme: " + NAME[pref] + " — click for " + NAME[next];
    btn.innerHTML = svgUse(ICON[pref]);
    btn.setAttribute("aria-label", desc);
    btn.title = desc;
  }

  function cycleTheme() {
    var next = ORDER[(ORDER.indexOf(getPref()) + 1) % ORDER.length];
    try { localStorage.setItem(KEY, next); } catch (e) {}
    applyTheme(next);
  }

  // Wire up the theme system: render the toggle icon and bind the click + OS-pref
  // listeners. Idempotent and safe to call from BOTH paths — gp.js auto-runs it on
  // DOMContentLoaded (see onReady) AND a page may still call GP.initTheme() inline
  // during the transition. The click handler binds once (dataset guard) and the
  // OS-pref listener once (mqBound flag), so a page that hasn't yet dropped its inline
  // call won't double-bind and cycle the theme twice per click. injectSprite() first so
  // the toggle's <use href="#gp-sun|moon|system"> resolves to a real symbol.
  var mqBound = false;
  function initTheme() {
    injectSprite();
    applyTheme(getPref());
    var btn = document.getElementById("theme-toggle");
    if (btn && !btn.dataset.gpThemeReady) {
      btn.dataset.gpThemeReady = "1";
      btn.addEventListener("click", cycleTheme);
    }
    if (!mqBound && mq && mq.addEventListener) {
      mqBound = true;
      mq.addEventListener("change", function () { if (getPref() === "system") applyTheme("system"); });
    }
  }

  // ---- fetch wrapper: JSON in/out, 401 -> sign-in ----
  // On a 401 the session has expired/absent; send the operator to /login (the portal
  // + console both serve a sign-in page there). Returns parsed JSON, or throws.
  async function api(path, opts) {
    opts = opts || {};
    var init = { cache: "no-store", headers: {} };
    for (var k in opts) if (k !== "json") init[k] = opts[k];
    if (opts.json !== undefined) {
      init.method = init.method || "POST";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.json);
    }
    var r = await fetch(path, init);
    if (r.status === 401) { try { location.assign("/login"); } catch (e) {} throw new Error("unauthorized"); }
    var ct = r.headers.get("Content-Type") || "";
    var data = ct.indexOf("application/json") >= 0 ? await r.json() : await r.text();
    if (!r.ok) { var err = new Error("HTTP " + r.status); err.status = r.status; err.data = data; throw err; }
    return data;
  }

  // ---- SSE helper over EventSource ----
  // handlers: { message, <named events...>, error, done }. Auto-closes on a terminal
  // "done" event so the stream isn't auto-reconnected (see AGENTS.md SSE traps).
  function sse(url, handlers) {
    handlers = handlers || {};
    var es = new EventSource(url);
    if (handlers.message) es.onmessage = function (e) { handlers.message(e.data, e); };
    Object.keys(handlers).forEach(function (name) {
      if (name === "message" || name === "error" || name === "done") return;
      es.addEventListener(name, function (e) { handlers[name](e.data, e); });
    });
    es.addEventListener("done", function (e) {
      try { es.close(); } catch (x) {}
      if (handlers.done) handlers.done(e.data, e);
    });
    es.onerror = function (e) { if (handlers.error) handlers.error(e); };
    return es;
  }

  // ---- simple repeating poller; returns a stop() function ----
  function poll(fn, ms) {
    var stopped = false;
    async function tick() { if (stopped) return; try { await fn(); } catch (e) {} if (!stopped) setTimeout(tick, ms); }
    tick();
    return function stop() { stopped = true; };
  }

  // ---- nav: mark the active link (the shared header partial renders the nav) ----
  function markNav(activeId) {
    var el = activeId && document.getElementById(activeId);
    if (el) el.classList.add("active");
  }

  // ---- display timezone: the zone all ABSOLUTE timestamps render in ----
  // Stored as an IANA id, or the sentinel "local" (= follow the browser). Defaults to the
  // viewer's local zone. The #tz-select control in the header is auto-wired below; changing
  // it dispatches a "gp:tzchange" event so pages can re-render their already-rendered times.
  // Relative "X ago" durations are tz-independent and intentionally not routed through here.
  var TZ_KEY = "gp-tz";
  var LOCAL = "local";
  var TZ_LIST = [
    { value: "local", label: "Local time" },
    { value: "UTC", label: "UTC" },
    { value: "America/New_York", label: "US Eastern" },
    { value: "America/Chicago", label: "US Central" },
    { value: "America/Denver", label: "US Mountain" },
    { value: "America/Los_Angeles", label: "US Pacific" },
    { value: "Europe/London", label: "London" },
    { value: "Europe/Berlin", label: "Berlin" },
    { value: "Asia/Tokyo", label: "Tokyo" },
    { value: "Australia/Sydney", label: "Sydney" }
  ];

  function localZone() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch (e) { return "UTC"; }
  }
  function getTzPref() { try { return localStorage.getItem(TZ_KEY) || LOCAL; } catch (e) { return LOCAL; } }
  function tzZone() { var p = getTzPref(); return p === LOCAL ? localZone() : p; }

  function setTz(v) {
    try { localStorage.setItem(TZ_KEY, v); } catch (e) {}
    var sel = document.getElementById("tz-select");
    if (sel && sel.value !== v) sel.value = v;
    try { document.dispatchEvent(new CustomEvent("gp:tzchange", { detail: { pref: v, zone: tzZone() } })); } catch (e) {}
  }

  // Coerce a Date or epoch-ms value to a valid Date, or null.
  function toDate(value) {
    var d = value instanceof Date ? value : new Date(Number(value));
    return value && !isNaN(d.getTime()) ? d : null;
  }

  // Format an epoch-ms (or Date) in the chosen zone. opts overrides the Intl date/time fields.
  function fmtTs(value, opts) {
    var d = toDate(value);
    if (!d) return "—";
    try {
      return new Intl.DateTimeFormat(undefined, Object.assign({ timeZone: tzZone() }, opts || {
        year: "numeric", month: "numeric", day: "numeric",
        hour: "numeric", minute: "2-digit", second: "2-digit"
      })).format(d);
    } catch (e) { return d.toLocaleString(); }
  }

  // Fixed "YYYY-MM-DD HH:mm:ss[.SSS]" layout in the chosen zone (used by the logs table).
  function fmtStamp(value, opts) {
    opts = opts || {};
    var d = toDate(value);
    if (!d) return "—";
    try {
      var p = {};
      new Intl.DateTimeFormat("en-CA", {
        timeZone: tzZone(), hour12: false,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      }).formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
      var hh = p.hour === "24" ? "00" : p.hour;   // some engines emit "24" for midnight
      var base = p.year + "-" + p.month + "-" + p.day + " " + hh + ":" + p.minute + ":" + p.second;
      if (opts.millis) base += "." + String(d.getMilliseconds()).padStart(3, "0");
      return base;
    } catch (e) { return d.toLocaleString(); }
  }

  // Short zone abbreviation (e.g. "PDT") for the chosen (or given) zone. Defaults to "now"
  // (not the epoch) so the abbreviation reflects the current DST state, e.g. MDT vs MST.
  function tzAbbr(value, zone) {
    var d = toDate(value) || new Date();
    try {
      var parts = new Intl.DateTimeFormat("en-US", { timeZone: zone || tzZone(), timeZoneName: "short" }).formatToParts(d);
      for (var i = 0; i < parts.length; i++) if (parts[i].type === "timeZoneName") return parts[i].value;
    } catch (e) {}
    return "";
  }

  // Populate + bind the header #tz-select. Safe to call once on DOMContentLoaded — the control
  // is brand-new so (unlike #theme-toggle) there is no inline handler to double-bind.
  function initTz() {
    var sel = document.getElementById("tz-select");
    if (!sel || sel.dataset.gpReady) return;
    sel.dataset.gpReady = "1";
    var pref = getTzPref();
    var abbr = tzAbbr(null, localZone());
    sel.innerHTML = "";
    TZ_LIST.forEach(function (tz) {
      var o = document.createElement("option");
      o.value = tz.value;
      o.textContent = tz.value === LOCAL && abbr ? "Local time (" + abbr + ")" : tz.label;
      sel.appendChild(o);
    });
    // A persisted zone outside the curated list (e.g. set by a future build) still shows.
    if (pref !== LOCAL && !TZ_LIST.some(function (t) { return t.value === pref; })) {
      var extra = document.createElement("option");
      extra.value = pref; extra.textContent = pref;
      sel.appendChild(extra);
    }
    sel.value = pref;
    sel.addEventListener("change", function () { setTz(sel.value); });
  }

  // ---- shared garden-state feed (Armed / Mitigation, node, latest event) ----
  // One underlying source fans out to N subscribers; late subscribers get the last
  // snapshot replayed immediately. Pi portal: SSE via /api/state/stream (push, snappy
  // cross-tab echo). Edge view-only build: SSE can't live within the ~5s Compute budget
  // (see AGENTS.md), so we poll /api/state instead. The payload is identical either way
  // (the same JSON GET /api/state returns), so subscribers don't care which path fed it.
  var stateSubs = [];
  var lastState = null;
  var stateStarted = false;
  var lastStateAt = 0;
  var stateCtl = null;        // active state-feed controller ({start,stop}) for suspend/resume

  // ---- live-connection lifecycle (single-sourced teardown so NO page can leak) ----
  // The persistent sockets a page holds open — the two header feeds (state + Pi-health) and
  // every live MJPEG <img> — are CLOSED when the page is backgrounded or navigated away, and
  // REOPENED when it returns. Without this a mobile browser freezes the page into its
  // back-forward cache with these sockets still open; they pile up across navigations past the
  // browser's ~6-connections-per-host limit until every new request queues forever and the
  // whole UI hangs — and only closing the tab frees them. See suspendLive/resumeLive below.
  var liveFeeds = [];         // [{start,stop}] — header state feed + Pi-health feed
  var mjpegStreams = [];      // [{img,url,retry}] — managed live MJPEG <img> elements
  var feedsSuspended = false;

  // ---- live-feed connection dot (#conn in the shared header) ----
  // Single-sourced here so EVERY page on BOTH tiers shows the same indicator without any
  // per-page JS. Driven off the same state feed that powers the mode pill: a fresh
  // snapshot means we're online; a 20s gap (watchdog below) or a hard feed error = offline.
  function setConn(online) {
    var el = document.getElementById("conn");
    if (!el) return;
    el.className = "conn " + (online ? "online" : "offline");
    var t = document.getElementById("conn-text");
    if (t) t.textContent = online ? "online" : "offline";
  }

  function emitState(s) {
    lastState = s;
    lastStateAt = Date.now();
    setConn(true);
    stateSubs.forEach(function (cb) { try { cb(s); } catch (e) {} });
  }

  function startPoll() {
    return poll(async function () {
      try { emitState(await api("/api/state")); } catch (e) { setConn(false); throw e; }
    }, 5000);
  }

  function startStateFeed() {
    if (stateStarted) return;
    stateStarted = true;
    stateCtl = makeStateFeed();
    liveFeeds.push(stateCtl);
    stateCtl.start();
  }

  // Build the garden-state feed controller. Pi portal: SSE /api/state/stream with a one-time
  // fallback to polling (older Pi without the route) and reconnect-on-drop once it has worked.
  // Edge view-only build: poll /api/state (SSE can't live within the ~5s Compute budget). The
  // controller exposes start()/stop() so the page-lifecycle hooks can tear the socket down and
  // rebuild it without disturbing the subscriber fan-out (stateStarted stays latched).
  function makeStateFeed() {
    var es = null, reconnect = null, watchdog = null, stopPoll = null;
    var gotOne = false, fellBack = false, stopped = false;
    function fallback() { if (fellBack || stopped) return; fellBack = true; stopPoll = startPoll(); }
    function connect() {
      if (stopped) return;
      try { es = new EventSource("/api/state/stream"); } catch (e) { fallback(); return; }
      es.onmessage = function (ev) { gotOne = true; try { emitState(JSON.parse(ev.data)); } catch (e) {} };
      es.onerror = function () {
        try { es.close(); } catch (e) {} es = null;
        setConn(false);
        if (stopped) return;
        if (!gotOne) { fallback(); return; }   // never delivered -> poll instead of looping
        if (!reconnect) reconnect = setTimeout(function () { reconnect = null; connect(); }, 5000);
      };
    }
    function start() {
      stopped = false; gotOne = false; fellBack = false;
      // Mark the feed stale if no snapshot has landed for 20s (mirrors the old dashboard
      // watchdog); the next snapshot flips it straight back to online via emitState.
      watchdog = setInterval(function () {
        if (lastStateAt && Date.now() - lastStateAt > 20000) setConn(false);
      }, 5000);
      if (window.GP_VIEW_ONLY) { stopPoll = startPoll(); return; }   // edge: poll only
      connect();
    }
    function stop() {
      stopped = true;
      if (watchdog) { clearInterval(watchdog); watchdog = null; }
      if (reconnect) { clearTimeout(reconnect); reconnect = null; }
      if (es) { try { es.close(); } catch (e) {} es = null; }
      if (stopPoll) { try { stopPoll(); } catch (e) {} stopPoll = null; }
    }
    return { start: start, stop: stop };
  }

  // Subscribe to garden state. cb(state) fires on every snapshot; returns an unsubscribe fn.
  function subscribeState(cb) {
    stateSubs.push(cb);
    if (lastState != null) { try { cb(lastState); } catch (e) {} }
    startStateFeed();
    return function () { stateSubs = stateSubs.filter(function (f) { return f !== cb; }); };
  }

  // ---- header status pill (operational mode) — auto-wired on every sentinel page ----
  function setPillText(el, text) {
    var t = el.querySelector(".t");
    if (t) t.textContent = text; else el.textContent = text;
  }
  // Repoint the pill's sprite icon in place (keeps the existing <svg class="gp-ic">).
  function setPillIcon(el, name) {
    var use = el.querySelector(".gp-ic use");
    if (use) use.setAttribute("href", "#" + name);
  }
  // A deterrent spray is brief; treat a "mitigate" event newer than this as currently
  // active, so the header pulses SPRAYING only when something genuinely just fired.
  var SPRAY_ACTIVE_MS = 60000;
  // Is rain currently suppressing sprays? The edge applies a rain veto when the node's
  // FRESH telemetry reports rain (critters shelter, so there's nothing to deter). Mirror
  // it here so the header can SAY so rather than showing a bare ACTIVE that implies
  // "ready to fire". Require the node ONLINE (stale telemetry is ignored by the veto too);
  // the edge /api/state carries no telemetry, so the edge build is simply never rain-held.
  function isRainHeld(s) {
    var node = s && s.node, t = node && node.telemetry;
    return !!(node && node.online && t && typeof t === "object" && t.raining);
  }

  // ---- the three operational modes (+ transient activity on top of ACTIVE) ----
  // mode is the new authoritative /api/state field: "off" | "monitor" | "active".
  // We derive a *display* state that layers two transient conditions over ACTIVE:
  //   spraying — a real deterrent fired in the last minute (red, pulsing)
  //   held     — ACTIVE but fresh rain telemetry is auto-suppressing sprays (blue)
  // Older edges that predate the field fall back to armed/override_stop. There is NO
  // persistent "stopped" state any more — Stop is a one-shot per-event abort.
  function stateMode(s) {
    if (s && (s.mode === "off" || s.mode === "monitor" || s.mode === "active")) return s.mode;
    // Back-compat derive: disarmed -> off; armed + log-only (override_stop) -> monitor.
    if (!s || !s.armed) return "off";
    return s.override_stop ? "monitor" : "active";
  }
  // The display state is the mode, except ACTIVE promotes to "spraying" (a deterrent is
  // firing now) or "held" (rain veto). Used for the header pill icon/color/text.
  function displayState(s) {
    var mode = stateMode(s);
    if (mode !== "active") return mode;
    var ev = s && s.latest_event;
    var firing = !!ev && ev.action === "mitigate" && (Date.now() - Number(ev.ts || 0) < SPRAY_ACTIVE_MS);
    if (firing) return "spraying";
    if (isRainHeld(s)) return "held";
    return "active";
  }
  // Map each display state to a sprite icon, a .gp-pill modifier class, label + tooltip.
  // OFF=neutral/grey power, MONITOR=amber eye, ACTIVE=green shield, SPRAYING=red spray,
  // HELD·rain=blue rain. Icons reuse the shared sprite — never emoji.
  var MODE_ICON = { off: "gp-power", monitor: "gp-eye", active: "gp-shield", spraying: "gp-spray", held: "gp-rain" };
  var MODE_PILLCLS = { off: "off", monitor: "monitor", active: "armed", spraying: "active", held: "held" };
  var MODE_TEXT = { off: "OFF", monitor: "MONITOR", active: "ACTIVE", spraying: "SPRAYING", held: "HELD · rain" };
  var MODE_TITLE = {
    off: "Off — not watching, not spraying",
    monitor: "Log mode — watching & alerting, never sprays",
    active: "Active — watching and will spray a confirmed critter",
    spraying: "Spraying a critter right now",
    held: "Sprays paused while it's raining — resumes on its own once it's dry"
  };

  // ---- send a control command (used by the labeled dashboard controls) ----
  // POSTs /api/control {cmd}, returns the new state JSON (same shape as GET /api/state),
  // and immediately fans the new snapshot to every subscriber (so the header status pill +
  // the dashboard controls update without waiting for the next feed snapshot). No-op (+ warn)
  // when view-only (the public edge: control is 403 and the UI is display-only). Throws on a
  // non-2xx so callers can toast. cmd is one of off|monitor|active|stop|resume (arm/disarm
  // aliases). The header status pill does NOT call this — it's display-only.
  async function control(cmd) {
    if (window.GP_VIEW_ONLY) { try { console.warn("GP.control ignored (view-only):", cmd); } catch (e) {} return null; }
    var r = await fetch("/api/control", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd: cmd })
    });
    if (!r.ok) { var err = new Error("control " + cmd + " -> " + r.status); err.status = r.status; throw err; }
    var s = await r.json();
    try { emitState(s); } catch (e) {}   // fan the new snapshot to every subscriber (header + page)
    return s;
  }

  // ---- alarm determinations ("good/neutral/bad" = real/unsure/false) ----
  // Unlike GP.control, these are NOT gated on GP_VIEW_ONLY: a public edge VIEWER is allowed to
  // TAG a NEW (untagged) alarm (the edge endpoint is viewer-gated). The edge enforces the real
  // policy — CHANGING an existing tag or DELETING an alarm requires a garden token (the admin
  // portal forwards it) -> a 403/401 the caller can toast.
  //   alarmTag(id,label)  -> POST a determination ({ok,label,edited}); throws on non-2xx (.status set)
  //   alarmDelete(id)     -> remove one alarm (admin only); throws on non-2xx
  //   alarmForKey(key)    -> the alarm for an archive key, or null (best-effort; drives the event toggle)
  async function alarmTag(id, label) {
    var r = await fetch("/api/alarm-tag", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id, label: label })
    });
    if (!r.ok) { var e = new Error("alarm-tag " + label + " -> " + r.status); e.status = r.status; throw e; }
    return await r.json();
  }
  async function alarmDelete(id) {
    var r = await fetch("/api/alarm/delete", {
      method: "POST", cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: id })
    });
    if (!r.ok) { var e = new Error("alarm-delete -> " + r.status); e.status = r.status; throw e; }
    return await r.json();
  }
  async function alarmForKey(key) {
    try {
      var r = await fetch("/api/alarm?key=" + encodeURIComponent(key), { cache: "no-store" });
      if (!r.ok) return null;
      return (await r.json()).alarm || null;
    } catch (e) { return null; }
  }

  // Render the single header mode pill from a state snapshot. This is a PLAIN, NON-INTERACTIVE
  // status badge on every tier — it shows the operational mode (OFF/MONITOR/ACTIVE, with
  // SPRAYING + HELD·rain layered on ACTIVE) and nothing more. The REAL controls live on the
  // dashboard (the OFF/MONITOR/ACTIVE segmented selector + the per-event "Stop this spray"
  // button), which call GP.control directly; the header pill never toggles anything.
  function renderStatusPills(s) {
    var pill = document.getElementById("pill-mode");
    if (!pill) return;
    var ds = displayState(s);
    pill.className = "gp-pill mode " + MODE_PILLCLS[ds];
    setPillIcon(pill, MODE_ICON[ds]);
    setPillText(pill, MODE_TEXT[ds]);
    pill.title = MODE_TITLE[ds];
  }
  function initStatusPills() {
    var pill = document.getElementById("pill-mode");
    if (!pill) return;
    // Status-only: just subscribe so the pill reflects live state. No click/keyboard
    // handlers, no button role/affordance — the dashboard carries the labeled controls.
    subscribeState(renderStatusPills);
  }

  // ---- Pi health (CPU / RAM) — shared #pi-health in the header, auto-wired on every page.
  // Single-sourced here (was copy-pasted + drifted across page templates). Streams
  // /api/system/stream via the SSE helper (one persistent EventSource, auto-reconnects).
  // Gated to the Pi/admin tier: the view-only edge has no system feed, so we never open
  // the stream there, and the box only reveals itself once a real sample lands. ----
  function initPiHealth() {
    var box = document.getElementById("pi-health");
    if (!box || window.GP_VIEW_ONLY) return;
    var cpu = document.getElementById("cpu-val");
    var ram = document.getElementById("ram-val");
    // Ink (luminance-shifted) tints, not the raw fill tokens: a raw --amber reading is ~1.35:1
    // on the light header (unreadable). The numeric value itself is the cue; the tint is just an
    // at-a-glance high/elevated hint, so it must stay legible in both themes.
    function tint(el, v) { if (el) el.style.color = v > 85 ? "var(--red-ink)" : v > 60 ? "var(--amber-ink)" : ""; }
    var es = null;
    function start() {
      es = sse("/api/system/stream", {
        message: function (data) {
          try {
            var d = JSON.parse(data);
            box.style.display = "flex";   // reveal only once we actually have data
            if (d.cpu !== undefined && cpu) { cpu.textContent = Math.round(d.cpu) + "%"; tint(cpu, d.cpu); }
            if (d.memory !== undefined && ram) { ram.textContent = Math.round(d.memory) + "%"; tint(ram, d.memory); }
          } catch (e) {}
        },
      });
    }
    function stop() { if (es) { try { es.close(); } catch (e) {} es = null; } }
    liveFeeds.push({ start: start, stop: stop });
    start();
  }

  // ---- archive helpers (History + Timelapse share one source) ----
  // The FOS object archive is read via /api/archive/days (the days that have photos),
  // /api/archive?date=&limit= (one day's events, newest-first; each event carries
  // date/time/action/species/confidence/device/key — parsed edge-side from the key), and
  // /api/archive/image?key= (the JPEG). These helpers format an event and fetch a date
  // RANGE by merging per-day fetches, so both the History grid and the Timelapse player
  // operate on the same shaped data without duplicating the formatting/fetch logic.
  function archFmtDay(d) {           // "2026-06-21" -> "Jun 21, 2026"
    var p = String(d).split("-");
    if (p.length !== 3) return d;
    var dt = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
    return isNaN(dt.getTime()) ? d : dt.toLocaleDateString(undefined,
      { month: "short", day: "numeric", year: "numeric" });
  }
  // Capture time is stored UTC (HH:MM:SS) next to its date; render in the viewer's zone.
  function archLocalTime(ev) {
    if (!ev || !ev.date || !ev.time) return (ev && ev.time) || "";
    var ms = Date.parse(ev.date + "T" + ev.time + "Z");
    return isNaN(ms) ? ev.time : fmtTs(ms, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  // Capture DATE in the viewer's zone (same UTC->local basis as archLocalTime) — "Jun 21".
  // Timelapses span multiple days, so each frame is labelled with its day next to the time.
  function archLocalDate(ev, opts) {
    if (!ev || !ev.date) return "";
    var ms = Date.parse(ev.date + "T" + (ev.time || "00:00:00") + "Z");
    return isNaN(ms) ? archFmtDay(ev.date)
      : fmtTs(ms, opts || { month: "short", day: "numeric" });
  }
  function archRealSpecies(ev) {
    return ev && ev.species && ev.species !== "none" && !String(ev.species).startsWith("class-");
  }
  // Mutually-exclusive bucket for the History "Type" filter + the icon/caption.
  function archType(ev) {
    if (ev && ev.action === "mitigate") return "sprayed";
    if (archRealSpecies(ev)) return "sighting";
    return "clear";
  }
  function archCaption(ev) {
    var conf = ev && ev.confidence != null ? " " + ev.confidence + "%" : "";
    if (ev && ev.action === "mitigate") return (ev.species || "intruder") + " — sprayed" + conf;
    if (archRealSpecies(ev)) return ev.species + conf;
    return "all clear";
  }
  function archIcon(ev) {
    var t = archType(ev);
    if (t === "sprayed") return svgUse("gp-alert", "bad");
    if (t === "sighting") return svgUse("gp-eye");
    return svgUse("gp-check", "good");
  }
  // A sortable key for an event: "YYYY-MM-DDTHH:MM:SS" (lexical == chronological).
  function archStamp(ev) { return (ev && ev.date ? ev.date : "") + "T" + (ev && ev.time ? ev.time : ""); }

  // The archive stores objects under UTC date keys, but the History/Timelapse views group and
  // label by the VIEWER's chosen display zone (tzZone) — so a photo taken at 9pm local shows
  // under "today", not tomorrow's UTC bucket. The server day list (UTC) is fetched once per
  // session; the UTC->local-day mapping is rebuilt whenever the chosen zone changes.
  var _archUtcDays = null;   // raw server UTC days (zone-independent), newest-first
  var _archExtent = null;    // { minMs, maxMs } of real events (zone-independent), from boundary fetches
  var _archModel = null;     // { zone, localDays, utcFor } cache for one zone

  async function archUtcDays() {     // raw UTC days from the server (cached for the session)
    if (_archUtcDays) return _archUtcDays;
    try {
      var r = await fetch("/api/archive/days", { cache: "no-store" });
      _archUtcDays = r.ok ? (((await r.json()) || {}).days || []) : [];
    } catch (e) { _archUtcDays = []; }
    return _archUtcDays;
  }
  // "YYYY-MM-DD" for an epoch-ms in the chosen display zone (en-CA == ISO field order).
  function localDayOf(ms) {
    var d = toDate(ms); if (!d) return "";
    try {
      var p = {};
      new Intl.DateTimeFormat("en-CA", { timeZone: tzZone(), year: "numeric", month: "2-digit", day: "2-digit" })
        .formatToParts(d).forEach(function (x) { p[x.type] = x.value; });
      return p.year + "-" + p.month + "-" + p.day;
    } catch (e) { return ""; }
  }
  // The local day an archive event falls on (its UTC date+time rendered in the chosen zone).
  function archEventDay(ev) {
    if (!ev || !ev.date || !ev.time) return (ev && ev.date) || "";
    var ms = Date.parse(ev.date + "T" + ev.time + "Z");
    return isNaN(ms) ? ev.date : localDayOf(ms);
  }
  // The real event extent (earliest + latest capture instants), so the day list isn't padded
  // with phantom local days. A UTC day spans 24h, but events cluster in daylight hours — naively
  // mapping a UTC day's 00:00/23:59 endpoints to local dates over-generates a tab at each end
  // (e.g. for UTC-6, UTC days [24,23] would imply local [24,23,22] when every photo is the 23rd).
  // We bound the mapping to [earliest, latest] real event. The min instant lives in the OLDEST
  // UTC day and the max in the NEWEST, so two boundary fetches suffice regardless of archive size.
  // Cached zone-independently (instants don't move; only their local-day label does).
  async function archExtent() {
    if (_archExtent) return _archExtent;
    var u = await archUtcDays();
    if (!u.length) return (_archExtent = { minMs: null, maxMs: null });
    var newest = await archEventsForDay(u[0], 5000);
    var oldest = u.length > 1 ? await archEventsForDay(u[u.length - 1], 5000) : newest;
    function ms(e) { return Date.parse((e.date || "") + "T" + (e.time || "") + "Z"); }
    var maxArr = newest.map(ms).filter(function (x) { return !isNaN(x); });
    var minArr = oldest.map(ms).filter(function (x) { return !isNaN(x); });
    _archExtent = {
      minMs: minArr.length ? Math.min.apply(null, minArr) : null,
      maxMs: maxArr.length ? Math.max.apply(null, maxArr) : null
    };
    return _archExtent;
  }
  // Map the server's UTC days to the LOCAL days they cover (a 24h UTC day touches at most two
  // local dates — its two endpoints'), trimmed to the real event extent, plus a reverse index
  // local-day -> the UTC day(s) whose objects may fall on it (so a local-day read fetches exactly
  // the UTC buckets it needs — a boundary local day straddles the two UTC days it spans).
  async function archDayModel() {
    var zone = tzZone();
    if (_archModel && _archModel.zone === zone) return _archModel;
    var utc = await archUtcDays();
    var ext = await archExtent();
    var lo = ext.minMs != null ? localDayOf(ext.minMs) : null;
    var hi = ext.maxMs != null ? localDayOf(ext.maxMs) : null;
    var localSet = {}, rev = {};
    utc.forEach(function (u) {
      [u + "T00:00:00Z", u + "T23:59:59Z"].forEach(function (iso) {
        var ld = localDayOf(Date.parse(iso));
        if (!ld || (lo && ld < lo) || (hi && ld > hi)) return;   // trim phantom boundary days
        localSet[ld] = true;
        (rev[ld] = rev[ld] || {})[u] = true;
      });
    });
    var utcFor = {};
    Object.keys(rev).forEach(function (ld) { utcFor[ld] = Object.keys(rev[ld]).sort(); });
    _archModel = { zone: zone, localDays: Object.keys(localSet).sort().reverse(), utcFor: utcFor };
    return _archModel;
  }
  async function archDays() {        // PUBLIC: local days (newest-first) for chips/date inputs
    return (await archDayModel()).localDays;
  }
  async function archEventsForDay(date, limit) {
    try {
      var url = "/api/archive?date=" + encodeURIComponent(date) +
        (limit ? "&limit=" + encodeURIComponent(limit) : "");
      var r = await fetch(url, { cache: "no-store" });
      if (!r.ok) return [];
      var j = await r.json();
      return (j && j.events) || [];
    } catch (e) { return []; }
  }
  // Fetch a date range by merging the LOCAL days that actually have photos. opts:
  //   from,to  (YYYY-MM-DD inclusive, in the chosen zone; default = whole archive)
  //   cam      (device_id; "" = all), action ("sprayed"|"sighting"|"clear"; "" = all)
  //   sort     ("newest" default | "oldest"), maxDays (cap, default 14), perDayLimit (1000)
  // Returns { events, days, daysFetched, capped } — `days` is the full local-day list (newest
  // first); `capped` is true when the range held more than maxDays local days of photos.
  async function archFetchRange(opts) {
    opts = opts || {};
    var maxDays = opts.maxDays || 14;
    var perDayLimit = opts.perDayLimit || 1000;
    var model = await archDayModel();
    var localAll = model.localDays;                    // newest-first local days
    var inRange = localAll.filter(function (d) {
      return (!opts.from || d >= opts.from) && (!opts.to || d <= opts.to);
    });
    var capped = inRange.length > maxDays;
    var pick = capped ? inRange.slice(0, maxDays) : inRange;   // newest-first -> keep newest
    // The UTC buckets needed to cover the picked local days (deduped; a boundary local day
    // pulls the two UTC days it straddles).
    var need = {};
    pick.forEach(function (ld) { (model.utcFor[ld] || []).forEach(function (u) { need[u] = true; }); });
    var perDay = await Promise.all(Object.keys(need).map(function (u) { return archEventsForDay(u, perDayLimit); }));
    var events = [];
    perDay.forEach(function (evs) { events = events.concat(evs); });
    // Drop spillover from boundary UTC buckets whose events land on local days outside the pick.
    var keep = {}; pick.forEach(function (d) { keep[d] = true; });
    events = events.filter(function (e) { return keep[archEventDay(e)]; });
    if (opts.cam) events = events.filter(function (e) { return e.device === opts.cam; });
    if (opts.action) events = events.filter(function (e) { return archType(e) === opts.action; });
    var asc = opts.sort === "oldest";
    events.sort(function (a, b) {
      var ka = archStamp(a), kb = archStamp(b);
      return asc ? (ka < kb ? -1 : ka > kb ? 1 : 0) : (ka > kb ? -1 : ka < kb ? 1 : 0);
    });
    return { events: events, days: localAll, daysFetched: pick, capped: capped };
  }
  function archImageUrl(key) { return "/api/archive/image?key=" + encodeURIComponent(key); }

  // Parse a durable archive object key into an event record — the client-side mirror of the
  // edge's parse_archive_key (archive.rs), so the event-detail page can render everything from
  // just the key (no extra fetch). Key shape:
  //   g/<gid>/evidence/YYYY/MM/DD/<INV>_HHMMSS_<action>_<species>_<confpct>_<did>_<batch>_<cid>.jpg
  // Returns {date,time,action,species,confidence,device,batch,cid,key} — same SHAPE as the
  // /api/archive feed (confidence as int %), so every helper above works on it unchanged; or
  // null if the key doesn't match. Two layouts (7 vs 8+ fields) disambiguated by count.
  function archParseKey(key) {
    if (!key) return null;
    var path = String(key).split("/");           // g / <gid> / evidence / YYYY / MM / DD / file.jpg
    if (path.length !== 7 || path[0] !== "g" || path[2] !== "evidence") return null;
    var fname = path[6];
    if (fname.slice(-4) !== ".jpg") return null;
    var parts = fname.slice(0, -4).split("_");
    if (parts.length < 7) return null;
    var inv = parts[0], hms = parts[1];
    if (!/^\d{5}$/.test(inv) || !/^\d{6}$/.test(hms)) return null;
    var conf = parseInt(parts[4], 10);
    if (isNaN(conf)) return null;
    var batch, cid;                              // 8+ fields -> [6]=batch,[7..]=cid; 7 -> no batch
    if (parts.length >= 8) { batch = parts[6]; cid = parts.slice(7).join("_"); }
    else { batch = ""; cid = parts.slice(6).join("_"); }
    return {
      date: path[3] + "-" + path[4] + "-" + path[5],
      time: hms.slice(0, 2) + ":" + hms.slice(2, 4) + ":" + hms.slice(4, 6),
      action: parts[2], species: parts[3], confidence: conf,
      device: parts[5], batch: batch, cid: cid, key: key
    };
  }

  var archive = {
    fmtDay: archFmtDay, localTime: archLocalTime, localDate: archLocalDate, caption: archCaption,
    icon: archIcon, type: archType, stamp: archStamp, imageUrl: archImageUrl, parseKey: archParseKey,
    days: archDays, eventsForDay: archEventsForDay, fetchRange: archFetchRange
  };

  // ---- managed live MJPEG streams (single-sourced lifecycle) ----
  // A live camera feed is an <img> whose src is a never-ending multipart/x-mixed-replace
  // response — it pins ONE of the browser's ~6 per-host connections for the whole time it's
  // in the DOM. Pages register their feed <img> here (GP.stream) instead of setting img.src by
  // hand, so gp.js can: (a) reconnect a dropped feed, (b) drop the socket the instant a card is
  // rebuilt (GP.stopStream), and (c) suspend/resume every feed on page-hide/return. This is the
  // shared replacement for the per-page startStream() copies that never tore their sockets down.
  function streamSrc(url) {
    return url + (url.indexOf("?") >= 0 ? "&" : "?") + "_gp=" + Date.now();   // bust cache -> fresh socket
  }
  function streamRec(img) {
    for (var i = 0; i < mjpegStreams.length; i++) if (mjpegStreams[i].img === img) return mjpegStreams[i];
    return null;
  }
  function stream(img, url) {
    if (!img) return;
    var rec = streamRec(img);
    if (!rec) {
      rec = { img: img, url: url, retry: null };
      mjpegStreams.push(rec);
      // An <img> never retries a dropped stream on its own. Reconnect here (once), but never
      // while suspended — the page-hide teardown clears src deliberately and must stay closed.
      img.addEventListener("error", function () {
        if (feedsSuspended) return;
        clearTimeout(rec.retry);
        rec.retry = setTimeout(function () { if (!feedsSuspended) img.src = streamSrc(rec.url); }, 5000);
      });
    }
    rec.url = url;
    if (!feedsSuspended) img.src = streamSrc(url);
  }
  function stopStream(img) {
    for (var i = 0; i < mjpegStreams.length; i++) {
      if (mjpegStreams[i].img === img) {
        clearTimeout(mjpegStreams[i].retry);
        try { img.removeAttribute("src"); } catch (e) {}   // release the socket now, don't wait for GC
        mjpegStreams.splice(i, 1);
        return;
      }
    }
  }

  // ---- page lifecycle: free every persistent socket on hide/navigate, reopen on return ----
  function suspendLive() {
    if (feedsSuspended) return;
    feedsSuspended = true;
    liveFeeds.forEach(function (f) { try { f.stop(); } catch (e) {} });
    mjpegStreams.forEach(function (r) {
      clearTimeout(r.retry);
      try { r.img.removeAttribute("src"); } catch (e) {}
    });
  }
  function resumeLive() {
    if (!feedsSuspended) return;
    feedsSuspended = false;
    liveFeeds.forEach(function (f) { try { f.start(); } catch (e) {} });
    mjpegStreams.forEach(function (r) { r.img.src = streamSrc(r.url); });
  }
  // pagehide/freeze cover real navigation + mobile bfcache freeze; visibilitychange covers
  // tab/app switches and screen-lock (where pagehide may not fire). pageshow/resume restore a
  // bfcache-restored page. Bound once, from gp.js, so EVERY page on BOTH tiers is covered.
  window.addEventListener("pagehide", suspendLive);
  window.addEventListener("freeze", suspendLive);
  window.addEventListener("pageshow", resumeLive);
  window.addEventListener("resume", resumeLive);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) suspendLive(); else resumeLive();
  });

  // ---- mobile nav: inject a hamburger that collapses the header nav into a vertical list.
  // Shared here so BOTH tiers get it with zero template change. No-op unless a real header
  // nav exists (skips the bare wizard/console headers). gp.css shows the button + collapses
  // the nav only at <=600px; on desktop the button is display:none and the nav is inline.
  // (Heads-up: gp.js is a Tailwind @source, so a bare DaisyUI component name written as a
  // plain word in here gets scraped as a class candidate and bloats app.css. Keep comments
  // free of bare DaisyUI component names; that's why this is a "list", not the m-word.) ----
  function initNavToggle() {
    var header = document.querySelector("header");
    if (!header) return;
    var nav = header.querySelector("nav.portal");
    if (!nav || header.querySelector(".nav-toggle")) return;
    if (!nav.id) nav.id = "gp-nav";
    var btn = document.createElement("button");
    btn.className = "nav-toggle";
    btn.type = "button";
    btn.setAttribute("aria-label", "Menu");
    btn.setAttribute("aria-controls", nav.id);
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true" ' +
      'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/>' +
      '<line x1="3" y1="18" x2="21" y2="18"/></svg>';
    function setOpen(open) {
      nav.classList.toggle("nav-open", open);
      btn.classList.toggle("is-open", open);
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    }
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      setOpen(!nav.classList.contains("nav-open"));
    });
    // Tapping a link navigates away — close the nav. (Admin <summary> is not an <a>, so it
    // expands its submenu in-flow without closing the whole nav.)
    nav.addEventListener("click", function (e) { if (e.target.closest("a")) setOpen(false); });
    // Tap outside the open nav (and not on the button) closes it.
    document.addEventListener("click", function (e) {
      if (nav.classList.contains("nav-open") && !nav.contains(e.target) && !btn.contains(e.target)) setOpen(false);
    });
    // Returning to desktop width must not leave a stale "open" state hiding/showing things.
    window.addEventListener("resize", function () { if (window.innerWidth > 600) setOpen(false); });
    header.insertBefore(btn, nav);
  }

  function onReady() { initTheme(); initTz(); initStatusPills(); initPiHealth(); initNavToggle(); }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", onReady);
  } else {
    onReady();
  }

  // gp.js auto-binds #theme-toggle on DOMContentLoaded (onReady -> initTheme). initTheme
  // is idempotent (dataset/flag guards), so pages that still call GP.initTheme() inline
  // keep working with no double-bind; new pages need only include the toggle markup.

  return { initTheme: initTheme, setTheme: function (p) { try { localStorage.setItem(KEY, p); } catch (e) {} applyTheme(p); },
           cycleTheme: cycleTheme, api: api, sse: sse, poll: poll, markNav: markNav,
           TZ_LIST: TZ_LIST, tzZone: tzZone, getTzPref: getTzPref, setTz: setTz,
           fmtTs: fmtTs, fmtStamp: fmtStamp, tzAbbr: tzAbbr, initTz: initTz,
           svgUse: svgUse, icon: svgUse, logIcon: logIcon, injectSprite: injectSprite,
           subscribeState: subscribeState, archive: archive,
           stream: stream, stopStream: stopStream,
           control: control, alarmTag: alarmTag, alarmDelete: alarmDelete,
           alarmForKey: alarmForKey, mode: stateMode, displayState: displayState };
})();
