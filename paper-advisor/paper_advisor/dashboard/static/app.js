// paper-advisor dashboard behavior: row-click detail drawer (and links between drawers), copy buttons,
// experiment switcher, live meaning and range of typed numbers (and the warning when typed limits would
// pause every buy), focus on the field to fix after a refused save, relative times and warnings that stay
// current on a tab left open (plus a self-reload while a run is in progress, and a reload when a run starts
// or ends), typed entries kept across reloads, and the current-section mark.
// Loaded as an external file because the Content-Security-Policy forbids inline scripts.
(function () {
  "use strict";
  var drawer = document.getElementById("drawer");
  var scrim = document.getElementById("drawer-scrim");
  var body = drawer ? drawer.querySelector(".drawer-body") : null;
  var lastFocus = null;
  var backBtn = drawer ? drawer.querySelector(".drawer-back") : null;
  var stack = [];      // records opened before the current one via a link inside the drawer
  var currentId = null;
  // Deep in a long drawer the head showed PAPER TRADING but not which record was being read: once the record's
  // own heading scrolls under the head, its title appears there. SYNC WARNING: .drawer-title in pages._page.
  var titleEl = drawer ? drawer.querySelector(".drawer-title") : null;
  var headEl = drawer ? drawer.querySelector(".drawer-head") : null;
  function syncDrawerTitle() {
    if (!titleEl || !headEl) return;
    var h2 = body.querySelector("h2");
    titleEl.textContent = h2 ? h2.textContent : "";
    titleEl.hidden = !h2 || h2.getBoundingClientRect().bottom > headEl.getBoundingClientRect().bottom;
  }
  if (drawer) drawer.addEventListener("scroll", syncDrawerTitle, { passive: true });

  function openDetail(id, fromLink) {
    var tpl = document.getElementById(id);
    if (!tpl || !drawer) return false;
    if (!drawer.classList.contains("open")) { lastFocus = document.activeElement; stack = []; }
    else if (fromLink && currentId && currentId !== id) stack.push(currentId);
    currentId = id;
    if (backBtn) backBtn.hidden = stack.length === 0;
    body.replaceChildren(tpl.content.cloneNode(true));
    drawer.classList.toggle("drawer-wide", tpl.hasAttribute("data-wide"));  // a wide table opens a wider drawer (pages._detail)
    drawer.scrollTop = 0;
    if (titleEl) titleEl.hidden = true;  // the heading is in view at the top; syncDrawerTitle shows the title on scroll
    drawer.classList.add("open");
    document.body.classList.add("drawer-open");
    drawer.setAttribute("aria-hidden", "false");
    scrim.hidden = false;
    drawer.querySelector(".drawer-close").focus();
    return true;
  }
  function closeDetail() {
    if (!drawer) return;
    drawer.classList.remove("open");
    document.body.classList.remove("drawer-open");
    drawer.setAttribute("aria-hidden", "true");
    scrim.hidden = true;
    stack = [];
    currentId = null;
    if (lastFocus) lastFocus.focus();
  }
  document.addEventListener("click", function (e) {
    var t = e.target;
    var fix = t.closest && t.closest(".fix-btn");
    if (fix) {
      var field = document.getElementById(fix.getAttribute("data-set"));
      if (field && !field.disabled) {
        field.value = fix.getAttribute("data-value");
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.focus();
      }
      return;
    }
    // A link inside a drawer that opens another record (e.g. a fill -> the decision behind it).
    var link = t.closest && t.closest("[data-open]");
    if (link) {
      if (openDetail(link.getAttribute("data-open"), true)) e.preventDefault();
      return;
    }
    var hashLink = t.closest && t.closest('a[href^="#"]');
    if (hashLink && hashLink.getAttribute("href").length > 1 && drawer && drawer.contains(hashLink)) {
      closeDetail();  // then the browser follows the #section link on the page itself
      return;
    }
    if (t.closest && t.closest(".drawer-back")) {
      var prev = stack.pop();
      if (prev) { currentId = null; openDetail(prev, false); }
      return;
    }
    if (t.closest && t.closest("a, button, input, select, textarea, summary")) {
      if (t.classList.contains("drawer-close")) closeDetail();
      if (t.classList.contains("copy")) copyText(t);
      return;
    }
    var row = t.closest && t.closest("tr[data-detail]");
    if (row) openDetail(row.getAttribute("data-detail"));
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDetail();
    if (e.key === "Enter" && e.target.matches && e.target.matches("tr[data-detail]")) openDetail(e.target.getAttribute("data-detail"));
  });
  if (scrim) scrim.addEventListener("click", closeDetail);

  function copyText(btn) {
    var text = btn.getAttribute("data-copy");
    var done = function () { var old = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = old; }, 1500); };
    var fail = function () { btn.textContent = "Select the command and copy it"; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fail);
    } else {
      fail();
    }
  }
  var sel = document.querySelector("select.autosubmit");
  if (sel) sel.addEventListener("change", function () { sel.form.submit(); });

  // Typed entries survive any reload of this tab, and the tab follows morning runs (Rule 19 #3-#5). A tab opened
  // before a run started offered Save as if nothing had changed, and a save refused mid-run left its entries on a
  // page whose every reload re-sent the form. Now a refused save gets this view's GET address; entries typed in the
  // risk and new-experiment forms are kept in this tab's sessionStorage across reloads and restored with a note; and
  // the page polls the server and reloads to the server-rendered state when a run starts or ends (further below).
  // Restoring happens here, before the live checks below read the fields.
  // SYNC WARNING: data-db/exp/active/self/state/state-url/kept come from pages._page and /api/state from
  // server.do_GET (pages.live_state); server._busy_words promises the refresh done here.
  var bd = document.body;
  var dbKind = bd.getAttribute("data-db") || "";
  var activeId = bd.getAttribute("data-active") || "";
  var keepForms = dbKind !== "" && activeId !== "" && bd.getAttribute("data-exp") === activeId;  // archived views: forms locked
  var kept = bd.getAttribute("data-kept") || "";
  var rendered = null;
  try { rendered = JSON.parse(bd.getAttribute("data-state") || "null"); } catch (err) { rendered = null; }
  var held = false;  // a run started while this page could not reload yet: Save and Create stay off
  var selfUrl = bd.getAttribute("data-self");
  if (kept && selfUrl && window.history && history.replaceState) {
    // The refusal is the response to a POST: give it the dashboard's address, so a reload asks the server again
    // instead of re-sending the form (and the browser asking to "resubmit").
    try { history.replaceState(null, "", selfUrl + "#" + kept); } catch (err) { /* keep the POST address */ }
  }
  var DRAFT_FORMS = ["risk", "newexp"];
  var touched = {};
  if (kept) touched[kept] = true;
  var restored = {};
  function draftKey(id) { return "pa-draft:" + dbKind + ":" + id; }
  function fieldsOf(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      return el.name && el.name !== "csrf" && !/^(hidden|submit|button)$/.test(el.type);
    });
  }
  function differs(form) {  // only entries that differ from what the server rendered as saved are worth keeping
    return fieldsOf(form).some(function (el) {
      if (el.type === "checkbox") return el.checked;
      return String(el.value).trim() !== String(el.getAttribute("data-initial") || "").trim();
    });
  }
  function saveDrafts() {
    if (!keepForms) return;
    DRAFT_FORMS.forEach(function (id) {
      var form = document.getElementById(id);
      if (!form || !touched[id]) return;
      try {
        if (!differs(form)) { sessionStorage.removeItem(draftKey(id)); return; }
        var vals = {};
        fieldsOf(form).forEach(function (el) { vals[el.name] = el.type === "checkbox" ? el.checked : el.value; });
        // lockRun: the run this page showed in progress, so the page after the reload can say that run has finished
        // even when a plain minute reload (not the poll) brought it there.
        sessionStorage.setItem(draftKey(id), JSON.stringify({ exp: activeId, vals: vals, last: id === "risk" ? lastFreezeField : null,
          lockRun: rendered && rendered.locked ? String(rendered.lock_run_id || "") : null }));
      } catch (err) { /* storage off: the entries live as long as this page */ }
    });
  }
  // Written half a second after the last keystroke as well as when the page goes away: a browser crash skips pagehide.
  var saveTimer = null;
  function saveSoon() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { saveTimer = null; saveDrafts(); }, 500);
  }
  if (keepForms) {
    DRAFT_FORMS.forEach(function (id) {
      var form = document.getElementById(id), draft = null;
      if (!form) return;
      form.addEventListener("input", function () { touched[id] = true; saveSoon(); });
      form.addEventListener("change", function () { touched[id] = true; saveSoon(); });
      // A pressed form's entries are either saved or shown again by the server's refusal (which keeps them itself).
      form.addEventListener("submit", function () { touched[id] = false; try { sessionStorage.removeItem(draftKey(id)); } catch (err) { /* off */ } });
      if (id === kept) return;  // this refusal page already shows that form's entries
      try { draft = JSON.parse(sessionStorage.getItem(draftKey(id)) || "null"); } catch (err) { draft = null; }
      if (!draft || !draft.vals) return;
      if (String(draft.exp) !== activeId) {  // typed while another experiment was active: not this one's limits
        try { sessionStorage.removeItem(draftKey(id)); } catch (err) { /* off */ }
        return;
      }
      fieldsOf(form).forEach(function (el) {
        if (!Object.prototype.hasOwnProperty.call(draft.vals, el.name)) return;
        if (el.type === "checkbox") el.checked = !!draft.vals[el.name]; else el.value = draft.vals[el.name];
      });
      touched[id] = true;
      restored[id] = draft;
    });
    window.addEventListener("pagehide", saveDrafts);
  }

  // Say what a typed number means while it is typed ("0.5" -> "= 50%"), so a fraction typed as a
  // percentage is caught before saving. The server renders the same text for the current values.
  function echoText(kind, raw) {
    var v = Number(String(raw).trim());
    if (String(raw).trim() === "" || !isFinite(v)) return "";
    if (kind === "pct") return "= " + +(v * 100).toFixed(4) + "%";
    if (kind === "bps") return "= " + +(v / 100).toFixed(4) + "%";
    if (kind === "usd") return v >= 0 ? "= $" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "";
    return "";
  }
  // Check the allowed range while typing (Rule 19: show the problem before the button is pressed).
  // SYNC WARNING: the data-lo/data-hi attributes come from pages.RANGES, which mirrors config.normalize;
  // the server still validates every save.
  // Bounds are said in the unit the owner reads in the echo: dollars, or the fraction with its percentage.
  function bound(kind, x) {
    if (kind === "usd") return "$" + x.toFixed(2);
    if (kind === "pct") return x + " (" + +(x * 100).toFixed(4) + "%)";
    if (kind === "bps") return x + " bps";
    return String(x);
  }
  function rangeProblem(span, raw) {
    if (!span.hasAttribute("data-lo") || String(raw).trim() === "") return "";
    var kind = span.getAttribute("data-echo");
    var lo = Number(span.getAttribute("data-lo")), loIn = span.getAttribute("data-lo-incl") === "1";
    var hasHi = span.hasAttribute("data-hi"), hi = Number(span.getAttribute("data-hi")), hiIn = span.getAttribute("data-hi-incl") === "1";
    var words = (span.hasAttribute("data-int") ? "a whole number " : "") + (loIn ? "at least " : "above ") + bound(kind, lo) +
      (hasHi ? (hiIn ? " and at most " : " and below ") + bound(kind, hi) : "");
    var v = Number(String(raw).trim().replace(/[$,]/g, ""));
    if (!isFinite(v)) return "enter a number: " + words;
    var bad = (loIn ? v < lo : v <= lo) || (hasHi && (hiIn ? v > hi : v >= hi)) || (span.hasAttribute("data-int") && v % 1 !== 0);
    return bad ? "allowed: " + words : "";
  }
  Array.prototype.forEach.call(document.querySelectorAll(".echo[data-for]"), function (span) {
    var input = document.getElementById(span.getAttribute("data-for"));
    if (!input) return;
    var update = function () {
      var kind = span.getAttribute("data-echo"), echo = kind === "none" ? "" : echoText(kind, input.value);
      var problem = rangeProblem(span, input.value);
      span.textContent = problem ? (echo ? echo + " · " : "") + problem : echo;
      span.classList.toggle("echo-bad", !!problem);
      input.classList.toggle("bad-input", !!problem);  // the box itself is marked, as the hint "marked in red" says
      if (problem) input.setAttribute("aria-invalid", "true"); else if (!input.closest(".has-error")) input.removeAttribute("aria-invalid");
    };
    input.addEventListener("input", update);
    update();
  });

  // Limits can contradict each other at this account's size (a minimum order above the max position, say),
  // which pauses every new buy. Recompute the smallest account that could place one from the TYPED limits,
  // exactly: integer cents, and decimal fractions as BigInt ratios, so it agrees with the server to the cent.
  // SYNC WARNING: mirrors config.min_fillable_order_cents / config.min_tradable_capital_cents, and freezeText
  // words the warning like pages.freeze_words (the server renders it for the saved limits and repeats it on save).
  function ratio(raw) {  // "0.25" -> [25n, 100n]; null unless a plain non-negative decimal
    var s = String(raw).trim().replace(/[$,]/g, "");
    if (!/^(\d+\.?\d*|\.\d+)$/.test(s)) return null;
    var p = s.split("."), frac = p[1] || "";
    return [BigInt((p[0] || "0") + frac), BigInt("1" + "0".repeat(frac.length))];
  }
  function ceilDiv(a, b) { return (a + b - 1n) / b; }  // a >= 0, b > 0
  function centsOf(raw) {  // dollars with at most 2 decimals -> BigInt cents
    var r = ratio(raw);
    if (!r || (r[0] * 100n) % r[1] !== 0n) return null;
    return (r[0] * 100n) / r[1];
  }
  function minTradableCents(v) {
    var M = centsOf(v.min_order_usd), c = centsOf(v.commission_per_order_usd);
    var b = ratio(v.commission_bps), P = ratio(v.max_position_pct), R = ratio(v.min_cash_reserve_pct);
    if (M === null || c === null || !b || !P || !R || P[0] === 0n || R[0] >= R[1]) return null;
    var den = 10000n * b[1] - b[0];  // n* = ceil((M + c) / (1 - bps/10000))
    if (den <= 0n) return null;
    var nStar = ceilDiv((M + c) * 10000n * b[1], den);
    var byPos = ceilDiv(nStar * P[1], P[0]);          // ceil(n* / max_position_pct)
    var byRes = ceilDiv(nStar * R[1], R[1] - R[0]);    // ceil(n* / (1 - min_cash_reserve_pct))
    return byPos > byRes ? byPos : byRes;
  }
  function usdText(cents) {  // BigInt cents -> "$1,234.56" (as money.fmt_usd)
    return "$" + (cents / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + (cents % 100n).toString().padStart(2, "0");
  }
  function centsText(c) { return (c / 100n).toString() + "." + (c % 100n).toString().padStart(2, "0"); }  // 12500n -> "125.00"
  function fracText(k) { return (k / 100n).toString() + "." + (k % 100n).toString().padStart(2, "0"); }    // 40n -> "0.40"
  // The one-field changes that let this equity buy again. SYNC WARNING: mirrors config.tradability_fixes (exact integers).
  function tradabilityFixes(v, E) {
    var M = centsOf(v.min_order_usd), c = centsOf(v.commission_per_order_usd);
    var b = ratio(v.commission_bps), P = ratio(v.max_position_pct), R = ratio(v.min_cash_reserve_pct);
    if (M === null || c === null || !b || !P || !R || P[0] === 0n || R[0] >= R[1]) return [];
    var den = 10000n * b[1] - b[0];
    if (den <= 0n) return [];
    var nStar = ceilDiv((M + c) * 10000n * b[1], den);
    var cap = (E * P[0]) / P[1];                      // floor(equity * max position)
    var room = E - ceilDiv(E * R[0], R[1]);           // equity - ceil(equity * reserve)
    if (cap >= nStar && room >= nStar) return [];
    var out = [], nMax = cap < room ? cap : room;
    var mMax = (nMax * den) / (10000n * b[1]) - c;    // floor(nMax * (1 - bps/1e4)) - per-order commission
    if (mMax >= 1n) out.push(["min_order_usd", centsText(mMax)]);
    if (cap < nStar && nStar <= room) { var pMin = ceilDiv(nStar * 100n, E); if (pMin <= 100n) out.push(["max_position_pct", fracText(pMin)]); }
    if (room < nStar && nStar <= cap) { out.push(["min_cash_reserve_pct", fracText(((E - nStar) * 100n) / E)]); }
    return out;
  }
  function fixLabel(key, val) {  // SYNC WARNING: pages.fix_label words the same buttons
    if (key === "min_order_usd") return "Use a $" + val + " minimum order";
    var pctWhole = String(Math.round(Number(val) * 100)) + "%";
    return (key === "max_position_pct" ? "Use max position " : "Use cash reserve ") + val + " (" + pctWhole + ")";
  }
  function freezeText(need, equity) {
    return "With these limits no new buy can pass: an account needs at least " + usdText(need) + " to place one, and this one has " +
      usdText(equity) + " of equity. Saving is still allowed (it pauses new buys); sells and exit rules keep working.";
  }

  // Saving risk settings needs a change, valid values and a reason (the server checks all three too):
  // keep Save off until then and name what is missing, the same way as Create below.
  var cfgForm = document.getElementById("risk");
  var cfgSave = document.getElementById("cfg-save");
  var cfgHint = document.getElementById("cfg-hint");
  var cfgReason = document.getElementById("cfg-reason");
  // SYNC WARNING: pages.FREEZE_KEYS lists the same fields (each has a .freeze-hint box under its echo line).
  var FREEZE_KEYS = ["min_order_usd", "commission_per_order_usd", "commission_bps", "max_position_pct", "min_cash_reserve_pct"];
  var lastFreezeField = "cfg-min_order_usd";
  if (cfgForm && cfgSave && !cfgSave.disabled) {
    var cfgFields = cfgForm.querySelectorAll("[data-initial]");
    var equityCents = cfgForm.hasAttribute("data-equity-cents") ? BigInt(cfgForm.getAttribute("data-equity-cents")) : null;
    // The warning goes under the limit the owner just changed (Rule 19: next to its cause), with one-press fixes.
    var freezeSync = function (e) {
      if (equityCents === null) return;
      if (e && e.target && FREEZE_KEYS.indexOf(String(e.target.id).replace("cfg-", "")) >= 0) lastFreezeField = e.target.id;
      var v = {}, invalid = false;
      FREEZE_KEYS.forEach(function (k) {
        var el = document.getElementById("cfg-" + k);
        v[k] = el ? el.value : "";
        if (el && el.classList.contains("bad-input")) invalid = true;
      });
      // An out-of-range value is already marked in red; the freeze warning only speaks about limits that could be saved.
      var need = invalid ? null : minTradableCents(v);
      var frozen = need !== null && equityCents < need;
      Array.prototype.forEach.call(cfgForm.querySelectorAll(".freeze-hint"), function (box) {
        var mine = frozen && box.getAttribute("data-freeze-for") === lastFreezeField;
        box.hidden = !mine;
        if (!mine) { box.replaceChildren(); return; }
        var text = document.createElement("span");
        text.textContent = freezeText(need, equityCents);
        box.replaceChildren(text);
        var fixes = tradabilityFixes(v, equityCents);
        if (fixes.length) {
          var lead = document.createElement("span");
          lead.className = "fix-lead";
          lead.textContent = " One-field fixes: ";
          box.appendChild(lead);
          fixes.forEach(function (f) {
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "fix-btn";
            btn.setAttribute("data-set", "cfg-" + f[0]);
            btn.setAttribute("data-value", f[1]);
            btn.textContent = fixLabel(f[0], f[1]);
            box.appendChild(btn);
          });
        }
      });
    };
    cfgForm.addEventListener("input", freezeSync);
    // On load the server has already placed the warning (under Minimum order) for frozen saved limits.
    var cfgSync = function () {
      var changed = Array.prototype.some.call(cfgFields, function (el) { return String(el.value).trim() !== String(el.getAttribute("data-initial")); });
      var bad = cfgForm.querySelectorAll(".echo.echo-bad").length > 0;
      var need = [];
      if (!changed) need.push("change at least one setting");
      if (!cfgReason || cfgReason.value.trim() === "") need.push("give a reason");
      cfgSave.disabled = held || need.length > 0 || bad;
      if (!cfgHint) return;
      cfgHint.hidden = need.length === 0 && !bad;
      cfgHint.replaceChildren();
      if (cfgHint.hidden) return;
      // Name each field to fix as a button that jumps to it (Rule 19: the hint leads to the control that fixes it).
      cfgHint.appendChild(document.createTextNode("To save: "));
      var badFields = cfgForm.querySelectorAll("input.bad-input");
      if (badFields.length) {
        cfgHint.appendChild(document.createTextNode("fix "));
        Array.prototype.forEach.call(badFields, function (el, i) {
          var lab = cfgForm.querySelector('label[for="' + el.id + '"]');
          var b = document.createElement("button");
          b.type = "button";
          b.className = "hint-field";
          b.textContent = lab ? lab.textContent : el.name;
          b.addEventListener("click", function () { el.scrollIntoView({ block: "center" }); el.focus(); });
          if (i) cfgHint.appendChild(document.createTextNode(" and "));
          cfgHint.appendChild(b);
        });
        if (need.length) cfgHint.appendChild(document.createTextNode(", "));
      }
      cfgHint.appendChild(document.createTextNode(need.join(", ") + "."));
    };
    cfgForm.addEventListener("input", cfgSync);
    cfgForm.addEventListener("change", cfgSync);
    cfgSync();
  }

  // Say exactly what Save would change, from the saved values (decision 5: what would this do?), whether or not Save can
  // be pressed now: entries kept in a form locked by a run say what they would change too (reviewer, round 8). The
  // "Done." notice repeats it from the server. SYNC WARNING: #cfg-diff is rendered by pages.settings; value words follow
  // the echo kinds (pages._ECHO), so "$6.00 → $6.50" and "0.25 (25%)" read like the field echoes.
  var cfgDiffEl = document.getElementById("cfg-diff");
  function settingWords(el, raw) {
    raw = String(raw).trim();
    if (el.tagName === "SELECT") return raw === "true" ? "yes" : "no";
    if (raw === "") return "(empty)";
    var span = document.querySelector('.echo[data-for="' + el.id + '"]');
    var kind = span ? span.getAttribute("data-echo") : "none", v = Number(raw);
    if (!isFinite(v)) return raw;
    if (kind === "usd") return "$" + v.toFixed(2);
    if (kind === "pct") return raw + " (" + +(v * 100).toFixed(4) + "%)";
    if (kind === "bps") return raw + " bps";
    return raw;
  }
  function renderCfgDiff() {
    if (!cfgForm || !cfgDiffEl) return;
    var diffs = [];
    Array.prototype.forEach.call(cfgForm.querySelectorAll("[data-initial]"), function (el) {
      var before = String(el.getAttribute("data-initial")), after = String(el.value).trim();
      if (after === before) return;
      var lab = cfgForm.querySelector('label[for="' + el.id + '"]');
      diffs.push((lab ? lab.textContent : el.name) + ": " + settingWords(el, before) + " → " + settingWords(el, after));
    });
    // "Saving changes" only when Save can do it: an out-of-range value is a typed change, and a run holds the form.
    var invalid = cfgForm.querySelectorAll(".echo.echo-bad").length > 0;
    var locked = held || !!cfgForm.querySelector("[data-initial]:disabled");
    cfgDiffEl.hidden = diffs.length === 0;
    cfgDiffEl.textContent = !diffs.length ? "" : (invalid ? "Typed changes, not saved: " : locked
      ? "Your changes, not saved yet (Save waits for the run): " : "Saving changes ") + diffs.join("; ") + ".";
  }
  if (cfgForm) { cfgForm.addEventListener("input", renderCfgDiff); cfgForm.addEventListener("change", renderCfgDiff); renderCfgDiff(); }

  // Creating an experiment needs the confirmation box: keep the button off until it is ticked and say why.
  var nxBox = document.querySelector('#newexp input[name="confirm"]');
  var nxBtn = document.getElementById("nx-create");
  var nxHint = document.getElementById("nx-hint");
  var nxCap = document.getElementById("nx-capital");
  var nxEcho = document.querySelector('.echo[data-for="nx-capital"]');
  if (nxBox && nxBtn && !nxBox.disabled) {
    // The hint names every missing step, so a disabled button never leaves the owner guessing.
    var sync = function () {
      var capOk = nxCap && nxCap.value.trim() !== "" && !(nxEcho && rangeProblem(nxEcho, nxCap.value));
      var need = [];
      // The floor comes from the limits the new experiment would start with (pages.settings data-lo).
      if (!capOk) need.push("enter at least " + bound("usd", nxEcho ? Number(nxEcho.getAttribute("data-lo")) : 10));
      if (!nxBox.checked) need.push("tick the box");
      nxBtn.disabled = held || need.length > 0;
      if (nxHint) { nxHint.hidden = need.length === 0; nxHint.textContent = need.length ? "To create it: " + need.join(" and ") + "." : ""; }
    };
    nxBox.addEventListener("change", sync);
    if (nxCap) nxCap.addEventListener("input", sync);
    sync();
  }

  // Restored entries say why they are back, what still waits and how to drop them (the page's reason for the
  // reload, set by the poll below, survives it in sessionStorage).
  var reloadWhy = "";
  try { reloadWhy = sessionStorage.getItem("pa-reload-why") || ""; sessionStorage.removeItem("pa-reload-why"); } catch (err) { /* off */ }
  function discardDraft(form, note) {
    fieldsOf(form).forEach(function (el) {
      if (el.type === "checkbox") el.checked = false; else el.value = el.getAttribute("data-initial") || "";
      el.dispatchEvent(new Event(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
    });
    touched[form.id] = false;
    try { sessionStorage.removeItem(draftKey(form.id)); } catch (err) { /* off */ }
    note.remove();
  }
  var restoredNotes = [];  // placeInitially() brings one into view if a restored scroll position leaves it above
  Object.keys(restored).forEach(function (id) {
    var form = document.getElementById(id);
    var doing = id === "risk" ? "saving" : "creating the experiment";
    var text;
    var ended = restored[id].lockRun !== null && restored[id].lockRun !== undefined ? restored[id].lockRun
      : (/^finished:\d+$/.test(reloadWhy) ? reloadWhy.split(":")[1] : null);
    if (rendered && rendered.locked) {
      var run = rendered.lock_run_id ? "morning run #" + rendered.lock_run_id : "the morning run";
      text = "Your entries are kept here. " + (id === "risk" ? "Saving" : "Creating the experiment") + " waits for " + run +
        " to finish; this page refreshes when it does.";
    } else if (ended !== null) {
      // "is no longer running": true whether it finished or its lock expired (it had not "finished" then).
      text = (ended ? "Morning run #" + ended : "The morning run") + " is no longer running, so " + doing + " works again. Your entries are kept below.";
    } else {
      text = "Your unsaved entries were kept when this page refreshed.";
    }
    var note = document.createElement("p");
    note.className = "draft-note";
    note.setAttribute("role", "status");
    note.appendChild(document.createTextNode(text + " "));
    var drop = document.createElement("button");
    drop.type = "button";
    drop.className = "link-btn";
    drop.textContent = "Discard them";
    drop.addEventListener("click", function () { discardDraft(form, note); });
    note.appendChild(drop);
    // Directly above the fields it is about (after any lock note or refusal, which say why the form is off).
    var anchor = form.querySelector(":scope > .fields") || form.querySelector(":scope > .field");
    form.insertBefore(note, anchor || null);
    restoredNotes.push(note);
    // Re-place the "no new buy can pass" warning for the restored limits under the field last edited.
    var last = restored[id].last ? document.getElementById(restored[id].last) : null;
    if (last && !last.disabled) last.dispatchEvent(new Event("input", { bubbles: true }));
  });

  // Section anchors must land below whatever stays stuck at the top: the whole header on wide screens,
  // the slim PAPER TRADING strip on phones. Its height changes with wrapping, so measure it (CSSOM
  // custom property; allowed by the CSP, unlike inline style attributes).
  function setStickyHeight() {
    // SYNC WARNING: the phone breakpoint of app.css (@media (max-width: 760px)); at 640 px the strip between 641 and
    // 760 px stuck at a guessed offset because the hidden desktop top bar was measured.
    var phone = window.matchMedia("(max-width: 760px)").matches;
    var el = document.querySelector(phone ? ".paper-strip" : "header.topbar");
    // On a phone the strip sticks under the app bar (the sidebar), whose height depends on the experiment's name.
    var bar = phone ? document.querySelector(".sidebar") : null;
    if (bar) document.documentElement.style.setProperty("--appbar-h", bar.offsetHeight + "px");
    if (el) document.documentElement.style.setProperty("--sticky-h", (el.offsetHeight + (bar ? bar.offsetHeight : 0)) + "px");
  }
  setStickyHeight();
  window.addEventListener("resize", setStickyHeight);

  // Relative times and time-triggered warnings stay true on a tab left open (critic, round 5: "started just
  // now" never changed). While a run is in progress the page also reloads itself every data-refresh seconds,
  // except while a detail drawer is open or a field has focus, so the lock's latest renewal is what is shown.
  // SYNC WARNING: agoText mirrors pages.ago(); data-show-after / data-refresh come from pages.next_steps/_page.
  function agoText(ms) {
    var mins = Math.floor(ms / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " min ago";
    return Math.floor(mins / 60) + " h " + (mins % 60) + " min ago";
  }
  function tick() {
    var now = Date.now();
    Array.prototype.forEach.call(document.querySelectorAll("time[data-ago]"), function (t) {
      var at = Date.parse(t.getAttribute("datetime"));
      if (!isNaN(at)) t.textContent = agoText(now - at);
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-show-after]"), function (el) {
      var at = Date.parse(el.getAttribute("data-show-after"));
      if (!isNaN(at)) el.hidden = now < at;
    });
  }
  tick();
  setInterval(tick, 30000);
  // Pages: the sidebar's seven, one shown at a time. A link to any id opens the page that holds it and scrolls to it,
  // so every existing anchor (#journal, #risk, a refused save's form, a drawer's "see it in the Advisor journal")
  // lands where it did. Without JavaScript every page shows, stacked: the "routed" class is what hides the others.
  // On load the browser has already jumped to the hash with every page visible, so the target is scrolled to again
  // once the other pages are hidden (the reload's saved position, restored just below, wins over that).
  // SYNC WARNING: pages.PAGE_NAV and the section-to-page map in pages._page.
  var views = Array.prototype.slice.call(document.querySelectorAll("section.view[data-view]"));
  var navItems = Array.prototype.slice.call(document.querySelectorAll(".sidebar .nav-item[data-view], .tabbar [data-view]"));
  var moreTabs = Array.prototype.slice.call(document.querySelectorAll(".tabbar [data-views]"));
  function showView(id, scroll) {
    var el = id ? document.getElementById(id) : null;
    var view = el ? (el.matches("section.view") ? el : el.closest("section.view")) : null;
    if (!view) { view = views[0]; el = null; }
    views.forEach(function (v) { v.classList.toggle("on", v === view); });
    var name = view.getAttribute("data-view");
    navItems.forEach(function (a) {
      var on = a.getAttribute("data-view") === name;
      a.classList.toggle("active", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    // The phone's More tab is marked when its page is in view, and its menu closes once a page is picked.
    moreTabs.forEach(function (m) {
      var on = (" " + m.getAttribute("data-views") + " ").indexOf(" " + name + " ") >= 0;
      m.classList.toggle("active", on);
      var host = m.closest("details");
      if (host) host.open = false;
    });
    if (scroll) {
      // A page's only section is the page itself: open it at the top, where its title and purpose are.
      if (el && el !== view && !el.matches("section.block.solo")) el.scrollIntoView({ block: "start" });
      else window.scrollTo(0, 0);
    }
  }
  if (views.length) {
    bd.classList.add("routed");
    window.addEventListener("hashchange", function () { showView(location.hash.slice(1), true); });
    // A second press on the page already shown returns to its top (no hashchange fires for the same hash).
    navItems.forEach(function (a) {
      a.addEventListener("click", function () {
        if (a.getAttribute("href") === location.hash) showView(location.hash.slice(1), true);
      });
    });
    showView(location.hash.slice(1), false);  // placeInitially() decides where a load opens
  }
  // The guided tour (static/tour.js, loaded after this file) changes the page ONLY through this object: show a
  // page with the router, and open or close a record's side sheet. All three change what is shown and nothing else; none presses, submits
  // or loads anything. Menus are plain <details> the tour opens itself.
  // SYNC WARNING: static/tour.js calls exactly these three, and tests/test_tour.py fails if a name is added here or the
  // tour calls anything else: widening the bridge means checking first that the new call writes nothing.
  window.paperAdvisorTour = Object.freeze({
    showPage: function (id) {
      if (!views.length) return;
      // The address follows the page shown without one history entry per step, so Back after the tour leaves the
      // dashboard as before and a reload reopens the page the tour ended on.
      try { history.replaceState(null, "", "#" + id); } catch (err) { /* keep the address */ }
      showView(id, true);
    },
    openSheet: function (id) { return openDetail(id, false); },
    closeSheet: function () { if (drawer && drawer.classList.contains("open")) closeDetail(); }
  });
  // Home's chart card: one segmented control switches Equity and Drawdown (without JavaScript both show).
  Array.prototype.forEach.call(document.querySelectorAll(".chart-head .seg"), function (seg) {
    var card = seg.closest(".card");
    var show = function (which) {
      Array.prototype.forEach.call(seg.querySelectorAll("button[data-chart]"), function (b) {
        var on = b.getAttribute("data-chart") === which;
        b.classList.toggle("on", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      // The panes, and the legend (it names the equity chart's three lines; the drawdown chart has one).
      Array.prototype.forEach.call(card.querySelectorAll("[data-pane]"), function (p) {
        p.hidden = p.getAttribute("data-pane") !== which;
      });
    };
    seg.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest("button[data-chart]");
      if (b) show(b.getAttribute("data-chart"));
    });
    show("eq");
  });
  // Where a full load opens. The browser jumps to a URL's #section after this script has run, and again at load, which
  // opened Runs or Settings scrolled past their title (capture, round 14), so a fresh load is placed now and once more
  // after load unless the owner has scrolled meanwhile: a page, or a page's only section, at its top (showView); another
  // section at its own top. A self-reload returns to where the owner was reading (browsers do not always restore it); an
  // ordinary reload or Back keeps the browser's own position. Then a restored-entries note above the fold is brought into
  // view: the restored position once left it off-screen with Save enabled and no word why (capture, round 14).
  var savedY = null, navType = "", ownerMoved = false;
  try {
    var sy = sessionStorage.getItem("pa-reload-scroll");
    if (sy !== null) { sessionStorage.removeItem("pa-reload-scroll"); savedY = Number(sy) || 0; }
  } catch (err) { /* storage off */ }
  try { navType = (performance.getEntriesByType("navigation")[0] || {}).type || ""; } catch (err) { /* older browser */ }
  ["wheel", "touchstart", "keydown", "mousedown"].forEach(function (t) {
    window.addEventListener(t, function () { ownerMoved = true; }, { once: true, passive: true });
  });
  function stickyTop() { return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sticky-h")) || 0; }
  function placeInitially() {
    if (savedY !== null) window.scrollTo(0, savedY);
    else if (views.length && location.hash && navType !== "reload" && navType !== "back_forward") showView(location.hash.slice(1), true);
    restoredNotes.forEach(function (n) {
      var top = n.getBoundingClientRect().top;
      if (top < stickyTop()) window.scrollBy(0, top - stickyTop() - 12);
    });
  }
  placeInitially();
  window.addEventListener("load", function () { requestAnimationFrame(function () { if (!ownerMoved) placeInitially(); }); });
  function reloadAllowed() {
    if (bd.classList.contains("drawer-open")) return false;
    // A reload mid-tour would drop the owner out of it: the minute reload during a run and the reload when a run starts
    // or ends wait until the tour closes, as they wait for an open drawer. SYNC WARNING: static/tour.js adds and
    // removes body.tour-open.
    if (bd.classList.contains("tour-open")) return false;
    if (document.querySelector("details[open]")) return false;  // a reload would collapse what is being read
    var a = document.activeElement;
    return !(a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }
  function reloadNow(why) {
    saveDrafts();  // pagehide saves them too; this one does not depend on the browser firing it
    try {
      sessionStorage.setItem("pa-reload-scroll", String(window.scrollY));
      if (why) sessionStorage.setItem("pa-reload-why", why);
    } catch (err) { /* storage off: keep default */ }
    window.location.reload();
  }
  var refreshSec = Number(bd.getAttribute("data-refresh") || 0);
  if (refreshSec > 0) setInterval(function () { if (reloadAllowed()) reloadNow(""); }, refreshSec * 1000);

  // A run started while this page cannot reload yet (a field has focus, details are open): turn Save, Create and
  // Make active off right away and say why, before anything is pressed (the server refuses them anyway).
  // The hold follows whichever run holds the lock now (a reconcile-only run can start and finish between two polls,
  // then a new analysis starts: the note named the first), and lifts when none does while the page still cannot reload.
  function holdForRun(runId) {
    var run = runId ? "Morning run #" + runId : "A morning run";
    held = true;
    [["cfg-save", "saving"], ["nx-create", "creating an experiment"]].forEach(function (t) {
      var b = document.getElementById(t[0]);
      if (!b) return;
      b.disabled = true;
      var p = b.parentNode.querySelector(".run-hold[data-for='" + t[0] + "']");
      if (!p) {
        p = document.createElement("p");
        p.className = "disabled-reason run-hold";
        p.setAttribute("role", "status");
        p.setAttribute("data-for", t[0]);
        b.insertAdjacentElement("afterend", p);
      }
      p.textContent = run + " has just started, so " + t[1] + " waits until it finishes. Your entries are kept, and the page " +
        "refreshes to show the run.";
    });
    Array.prototype.forEach.call(document.querySelectorAll('form[id^="activate-"] button, button[form^="activate-"]'), function (b) {
      if (!b.hasAttribute("data-held")) b.setAttribute("data-held", b.disabled ? "was-off" : "on");
      b.disabled = true;
      b.title = run + " is in progress";
    });
    renderCfgDiff();
  }
  function releaseHold() {
    if (!held) return;
    held = false;
    Array.prototype.forEach.call(document.querySelectorAll(".run-hold"), function (p) { p.remove(); });
    Array.prototype.forEach.call(document.querySelectorAll("[data-held]"), function (b) {
      if (b.getAttribute("data-held") === "on") { b.disabled = false; b.removeAttribute("title"); }
      b.removeAttribute("data-held");
    });
    // Save and Create return to what their own checks say (they were enabled before the hold, or the form says why not).
    if (cfgForm) cfgForm.dispatchEvent(new Event("input", { bubbles: true }));
    if (nxBox) nxBox.dispatchEvent(new Event("change", { bubbles: true }));
    renderCfgDiff();
  }
  var stateUrl = bd.getAttribute("data-state-url");
  if (stateUrl && rendered && window.fetch) {
    var stateKey = function (s) { return [s.active_id, s.locked, s.lock_run_id, s.last_run_id].join("|"); };
    var poll = function () {
      if (document.visibilityState === "hidden") return;
      fetch(stateUrl, { cache: "no-store", credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s || stateKey(s) === stateKey(rendered)) return;
          var why = s.locked ? "started:" + (s.lock_run_id || "")
            : (rendered.locked || s.last_run_id !== rendered.last_run_id ? "finished:" + (s.last_run_id || "") : "");
          if (reloadAllowed()) { reloadNow(why); return; }
          if (s.locked && !rendered.locked) holdForRun(s.lock_run_id);
          else if (!s.locked && !rendered.locked) releaseHold();  // the run that held it ended before the page could reload
        })
        .catch(function () { /* the dashboard server stopped: nothing to follow */ });
    };
    setInterval(poll, 15000);
    document.addEventListener("visibilitychange", function () { if (document.visibilityState === "visible") poll(); });
  }


  var focus = document.querySelector(".focus-target");
  if (focus) focus.scrollIntoView({ block: "start" });
  // After a refused save, put the cursor in the first field that needs fixing (without moving the view
  // away from the summary), so correcting it and pressing Enter resubmits in one step.
  var bad = document.querySelector('[aria-invalid="true"]');
  if (bad) bad.focus({ preventScroll: true });
})();
