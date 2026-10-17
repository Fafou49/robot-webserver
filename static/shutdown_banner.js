/*
 * Site-wide shutdown alert banner (2026-10-12, explicit user request).
 *
 * Verbatim: "je veux que si je ne peux pas atteindre la journée de
 * charge suivante, alors le site internet affiche un bandeau d'alerte
 * rouge et jaune indiquant l'heure ou la pi1 va s'éteindre [...] sur
 * toutes les pages. utilise la moyenne glissante [...] affiche un
 * décompte dans le bandeau critique. de plus rédiges ce bandeau et
 * tout le site en anglais."
 *
 * One shared module, loaded from all three "live" pages (/control,
 * /power, /media -- the static documentation/login pages don't poll
 * anything and have nothing to show a live banner about) rather than
 * three copies of the same markup/logic, since app.py's _control_page()
 * and _media_page() are standalone inline-HTML Python f-strings with no
 * shared template layer, and pages/power.html is a separate static
 * file.
 *
 * The banner is driven entirely by ONE number from Pi #1 -- the PWR
 * sentence's 18th field, shutdown_eta_ts (see link/server.py and
 * link/shutdown_policy.py in the robot repository): 0 means "no
 * shutdown currently scheduled" (hide the banner entirely -- no
 * banner at all is what "everything's fine" looks like, not a green
 * one), otherwise a Unix timestamp, the single authoritative answer to
 * "when will Pi #1 shut itself down". Pi #1 already uses the 5-minute
 * rolling average of load/PV power for that projection (link.
 * power_projection.PowerAverager, shared with the camera reactive/eco
 * policy) -- this module does no projection of its own, no day/night
 * math, nothing: it only ever renders the number it's given, so there
 * is exactly one place (Pi #1) that can be wrong about it.
 *
 * Two severity tiers, matching the mockup (mockup_shutdown_alert.html
 * in the robot repository) this was built from:
 *   - "warning" (yellow): shutdown scheduled, more than
 *     SHUTDOWN_CRITICAL_H away.
 *   - "critical" (red, hazard-stripe background): shutdown scheduled
 *     within SHUTDOWN_CRITICAL_H -- real urgency, not just "keep an
 *     eye on it". Explicit user request: a live countdown ticks here,
 *     specifically (not in the warning tier).
 * SHUTDOWN_CRITICAL_H=2.0 is NOT a value the user specified -- same
 * "reasonable default, worth tuning after watching real nights'
 * telemetry" spirit as link.camera_power_policy's SAFETY_MARGIN_HOURS,
 * carried over unchanged from the mockup.
 */
(function () {
  "use strict";

  // 18th PWR field (0-based index 17) -- see this module's docstring
  // above and link/server.py's PWR sentence builder in the robot repo.
  // An older Pi #1 that doesn't send it yet simply never shows a
  // banner (fields[17] is undefined -> treated the same as "0" below).
  var SHUTDOWN_ETA_FIELD_INDEX = 17;

  // Not a user-specified value -- see the module docstring above.
  var SHUTDOWN_CRITICAL_H = 2.0;

  // Default cadence for mountAndPoll()'s own PWR polling, for pages
  // that don't already poll PWR for their own dashboard (/control,
  // /media). pages/power.html instead feeds this module from its own
  // existing 3s PWR poll via updateFromPwrFields() directly -- see
  // that page's pollOnce().
  var DEFAULT_POLL_INTERVAL_MS = 5000;

  var STYLE_ID = "shutdown-banner-style";
  var BANNER_ID = "shutdown-banner";

  var state = {
    mounted: false,
    shutdownEtaTs: 0, // 0 = nothing scheduled, matches the wire value
    tickTimer: null,
    pollTimer: null,
  };

  var CSS = [
    "#" + BANNER_ID + " {",
    "  display: none;",
    "  position: fixed; top: 0; left: 0; right: 0; z-index: 9999;",
    "  align-items: center; gap: 12px;",
    "  padding: 12px 16px;",
    "  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;",
    "  box-sizing: border-box;",
    "}",
    "#" + BANNER_ID + ".is-visible { display: flex; }",
    "#" + BANNER_ID + " .shutdown-banner-icon { font-size: 22px; line-height: 1; flex: 0 0 auto; }",
    "#" + BANNER_ID + " .shutdown-banner-text {",
    "  flex: 1 1 auto; min-width: 0;",
    "  display: flex; flex-direction: column;",
    "}",
    "#" + BANNER_ID + " .shutdown-banner-headline {",
    "  display: block;",
    "  font-size: 13.5px; font-weight: 700; text-transform: uppercase;",
    "  letter-spacing: 0.03em; margin-bottom: 2px;",
    "}",
    "#" + BANNER_ID + " .shutdown-banner-detail { display: block; font-size: 12.5px; line-height: 1.4; }",
    "#" + BANNER_ID + " .shutdown-banner-detail b { font-variant-numeric: tabular-nums; }",
    "#" + BANNER_ID + ".warning {",
    "  background: #3a2f0f; border: 1px solid #d29922;",
    "}",
    "#" + BANNER_ID + ".warning .shutdown-banner-icon,",
    "#" + BANNER_ID + ".warning .shutdown-banner-headline { color: #d29922; }",
    "#" + BANNER_ID + ".warning .shutdown-banner-detail { color: #e6edf3; }",
    "#" + BANNER_ID + ".critical {",
    "  background: repeating-linear-gradient(",
    "    135deg,",
    "    #3a1414 0px, #3a1414 14px,",
    "    #3a2f0f 14px, #3a2f0f 28px",
    "  );",
    "  border: 1px solid #d03b3b;",
    "}",
    "#" + BANNER_ID + ".critical .shutdown-banner-icon,",
    "#" + BANNER_ID + ".critical .shutdown-banner-headline { color: #ff6b6b; }",
    "#" + BANNER_ID + ".critical .shutdown-banner-detail { color: #e6edf3; }",
  ].join("\n");

  function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var styleEl = document.createElement("style");
    styleEl.id = STYLE_ID;
    styleEl.textContent = CSS;
    document.head.appendChild(styleEl);
  }

  function injectMarkup() {
    if (document.getElementById(BANNER_ID)) return;
    var banner = document.createElement("div");
    banner.id = BANNER_ID;
    banner.innerHTML =
      '<span class="shutdown-banner-icon" id="shutdown-banner-icon">&#9888;</span>' +
      '<span class="shutdown-banner-text">' +
      '<span class="shutdown-banner-headline" id="shutdown-banner-headline"></span>' +
      '<span class="shutdown-banner-detail" id="shutdown-banner-detail"></span>' +
      "</span>";
    // DOM position doesn't actually matter (the banner is
    // `position: fixed`, see injectStyle() -- it never occupies flow
    // space on its own), but inserted as the very first element of
    // <body> anyway so it's unambiguously the first thing in the
    // document. This module intentionally never reaches into any
    // page's own layout beyond the --shutdown-banner-h CSS variable it
    // publishes (see setBannerHeightVar()) -- three very differently-
    // shaped pages share this one file (see the module docstring), so
    // each page opts in with its own small, explicit top-offset tweak
    // (app.py's .nav/.clock/.status-bar/.main-area, pages/power.html's
    // body padding-top) rather than this module assuming anything.
    document.body.insertBefore(banner, document.body.firstChild);
  }

  function mount() {
    if (state.mounted) return;
    injectStyle();
    injectMarkup();
    state.mounted = true;
  }

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function fmtClockTime(ts) {
    var d = new Date(ts * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  // includeSeconds=true for the critical tier's live countdown
  // (explicit user request); false for the warning tier's coarser
  // "in Xh YYm" -- recomputed on every PWR poll, no local ticking
  // needed there.
  function fmtDuration(totalSeconds, includeSeconds) {
    if (totalSeconds < 0) totalSeconds = 0;
    var hh = Math.floor(totalSeconds / 3600);
    var mm = Math.floor((totalSeconds % 3600) / 60);
    if (includeSeconds) {
      var ss = Math.floor(totalSeconds % 60);
      return hh + ":" + pad2(mm) + ":" + pad2(ss);
    }
    return hh + "h" + pad2(mm);
  }

  function setBannerHeightVar(heightPx) {
    document.documentElement.style.setProperty("--shutdown-banner-h", heightPx + "px");
  }

  function stopTicking() {
    if (state.tickTimer !== null) {
      clearInterval(state.tickTimer);
      state.tickTimer = null;
    }
  }

  function renderNow() {
    var banner = document.getElementById(BANNER_ID);
    if (!banner) return; // mount() not called yet -- nothing to render into

    var eta = state.shutdownEtaTs;
    if (!eta) {
      // Nothing scheduled -- hide entirely. No banner at all is what
      // "everything's fine" looks like here, not a green one (see the
      // module docstring).
      banner.classList.remove("is-visible", "warning", "critical");
      stopTicking();
      setBannerHeightVar(0);
      return;
    }

    var remainingSeconds = eta - Date.now() / 1000;
    var isCritical = remainingSeconds <= SHUTDOWN_CRITICAL_H * 3600;

    banner.classList.add("is-visible");
    banner.classList.toggle("warning", !isCritical);
    banner.classList.toggle("critical", isCritical);

    var icon = document.getElementById("shutdown-banner-icon");
    var headline = document.getElementById("shutdown-banner-headline");
    var detail = document.getElementById("shutdown-banner-detail");

    var clockStr = fmtClockTime(eta);
    var durationStr = fmtDuration(remainingSeconds, isCritical);

    if (isCritical) {
      icon.innerHTML = "&#9940;"; // ⛔
      headline.textContent = "Critical — Computer 1 shutdown imminent";
      detail.innerHTML =
        "Computer 1 is estimated to shut down at <b>" + clockStr + "</b> (in <b>" +
        durationStr + "</b>) — not enough reserve left to reach the next " +
        "charging day. Act quickly if you need to intervene remotely.";
    } else {
      icon.innerHTML = "&#9888;"; // ⚠
      headline.textContent = "Warning — insufficient battery reserve";
      detail.innerHTML =
        "At the current discharge rate, Computer 1 could shut down at <b>" +
        clockStr + "</b> (in about " + durationStr +
        ") — before it can recharge for the next day.";
    }

    // Live 1-second countdown, critical tier only (explicit user
    // request) -- started/kept running while critical, stopped
    // otherwise. Each PWR poll (updateFromPwrFields) re-syncs
    // state.shutdownEtaTs and calls renderNow() again, so this never
    // drifts for more than one poll interval.
    if (isCritical) {
      if (state.tickTimer === null) {
        state.tickTimer = setInterval(renderNow, 1000);
      }
    } else {
      stopTicking();
    }

    // Measured LAST, after every content change above -- offsetHeight
    // must reflect the banner's final rendered size (icon/headline/
    // detail text actually in place), not a stale pre-update height.
    // The banner is `position: fixed` (see injectStyle()) so it never
    // takes up flow space on its own -- each page instead reads this
    // CSS variable to shift its own fixed nav/clock bar and top-of-
    // content margin down by exactly the banner's rendered height, so
    // nothing ends up hidden underneath it. See app.py's .nav/.clock/
    // .status-bar/.main-area rules and pages/power.html's body
    // padding-top for the other half of this.
    setBannerHeightVar(banner.offsetHeight);
  }

  function updateFromPwrFields(fields) {
    if (!fields || fields.length <= SHUTDOWN_ETA_FIELD_INDEX) {
      // Older Pi #1 that doesn't send this field yet, or a malformed
      // response -- same as "nothing scheduled", never guess.
      state.shutdownEtaTs = 0;
    } else {
      var parsed = parseInt(fields[SHUTDOWN_ETA_FIELD_INDEX], 10);
      state.shutdownEtaTs = Number.isFinite(parsed) ? parsed : 0;
    }
    renderNow();
  }

  function pollOnce() {
    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: "PWR" }),
    })
      .then(function (res) {
        return res.json();
      })
      .then(function (data) {
        if (data && data.ok && data.fields) {
          updateFromPwrFields(data.fields);
        }
      })
      .catch(function () {
        // Silent on failure, same spirit as this project's other
        // polling loops (e.g. pollStatus() in app.py's /control page)
        // -- a transient network hiccup shouldn't spam the console,
        // and the banner simply keeps showing its last known state
        // until the next successful poll.
      });
  }

  function mountAndPoll(intervalMs) {
    mount();
    if (state.pollTimer !== null) return; // already polling -- avoid a second loop
    pollOnce();
    state.pollTimer = setInterval(pollOnce, intervalMs || DEFAULT_POLL_INTERVAL_MS);
  }

  window.ShutdownBanner = {
    mount: mount,
    updateFromPwrFields: updateFromPwrFields,
    mountAndPoll: mountAndPoll,
  };
})();
