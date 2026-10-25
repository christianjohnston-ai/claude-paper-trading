// paper-advisor dashboard: the guided tour. A spotlight walk through the seven pages and the five decisions the
// dashboard serves (is it adding value, can the record be trusted, what happens next, why each decision, what a change
// would do), started only when the owner presses Take the tour. Modelled on an earlier SpotlightTour engine
// and its live, read-only task-page tour, rebuilt for a vanilla-JS page under a strict Content-Security-Policy.
//
// SYNC WARNING (the tour tripwire): every step points at a data-tour="..." anchor rendered by
// paper_advisor/dashboard/pages.py (each anchor there carries a SYNC WARNING naming this file) and changes the page only
// through window.paperAdvisorTour, defined in app.js. tests/test_tour.py renders the dashboard in seven database states
// and fails when an anchor a step names is missing, when an anchor no step names exists, when this file gains a call
// that presses, submits, types, fires events or uses the network, or when the bridge grows. The test reads this file as
// TEXT: keep each step's `id` first, its `target` / `phoneTarget` / `also` as double-quoted literals, and the steps
// between `var STEPS = [` and `// END STEPS`. When you change a page a step describes, re-read that step's `verified:`
// comments: each sentence was traced to the template or handler named there (line numbers drift; names do not).
//
// READ-ONLY. The tour switches pages through the router, scrolls, and opens an information-only menu (Commands, or More
// on a phone) or side sheet (one decision's record). Nothing it does can save, create, archive, export or back up
// anything: the page under it is inert while it runs, and a layer over the page swallows every click.
//
// CSP (script-src 'self'; style-src 'self'): this file is external, its elements are built with createElement and
// textContent (never from HTML strings), and the spotlight and card are positioned with CSSOM (el.style.setProperty),
// which the policy allows; there is no style attribute in any markup.
(function () {
  "use strict";
  var launch = document.getElementById("tour-launch");
  // Simple pages (not initialized, reports) render no launch control and no bridge: they offer no tour.
  if (!launch || !window.paperAdvisorTour) return;

  // The only three calls that change the page (app.js). SYNC WARNING: tests/test_tour.py allows exactly these.
  function show(page) { window.paperAdvisorTour.showPage(page); }
  function openSheet(id) { return window.paperAdvisorTour.openSheet(id); }
  function closeSheet() { window.paperAdvisorTour.closeSheet(); }

  // SYNC WARNING: the phone breakpoint of app.css (@media (max-width: 760px)) and app.js.
  var PHONE = window.matchMedia("(max-width: 760px)");
  var REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)");

  // ----------------------------------------------------------------------------------------------------------------
  // The steps. `where` (the page or bar a step is on) is shown beside the step count. title, where and body may be
  // functions of s = {phone, fixture, archived, noruns, valued, archiveNote, decisions, found, wide, table, alsoAt}: the
  // copy changes only where the screen does (the fixture demo, an archived experiment, before the first run, the phone
  // layout, an empty record, a table wider than its card, where a lit companion sits). A body is a list of short
  // paragraphs; **bold** and `code` are the only markup. A card names only what is lit, or says where the rest is
  // ("further down", "beside it") in words that stay true however the page is scrolled (critic, rounds 1 and 2). A card
  // is read in seconds: every variant stays near 50 words or fewer (critic, round 2: cards of 60 to 80 words). Every
  // sentence is true in every state the tests render, and says "if" where the screen depends on the record. `pad`
  // widens the ring around a page-level target, whose text starts at its very edge (a card's own padding does that for
  // a card; critic, round 2: the ring hugged page headings).
  var STEPS = [
    {
      id: "paper",
      target: "paper",
      where: "Every page",
      title: "This is paper trading",
      enter: function () { show("home"); },
      // verified: pages.header (top bar: PAPER TRADING mark and "Simulated money, no real orders"; the phone strip);
      // README.md line 3 and tests/test_no_live_broker.py (no brokerage connection, no order path); broker_sim (a buy
      // fills at open x (1 + slippage), a sell at open x (1 - slippage), from the target session's daily bar);
      // pages.health "Execution and data assumptions" (market-on-open fills at the first session that opens after a
      // decision, from that session's official daily bar; no intraday stop fills); pages.header clock_note ("End-of-day
      // prices, never live quotes"); fixture: pages.header banner ("Invented tickers, prices and decisions ... not your
      // $500 forward paper test"); archived: pages.next_steps ("Its history is read-only"). The keyboard keys are in the
      // card's footer (critic, round 1). The fixture and archived cards say what they are instead of "only looks" (a
      // frozen demo and a read-only history change nothing either), to stay short.
      body: function (s) {
        var out = ["Everything here is simulated: paper money, no brokerage connection, no real orders.",
          "Orders fill at the next eligible open, at its official opening price after slippage; daily bars, no intraday precision."];
        if (s.fixture) out.push("This is the **fixture demo**: invented data, kept apart from your paper test.");
        else if (s.archived) out.push("You are viewing an **archived** experiment: its history is read-only.");
        else out.push("This tour only looks; it changes nothing.");
        return out;
      }
    },
    {
      id: "health",
      target: "health",
      phoneTarget: "paper",
      also: "fixture-switch",
      where: "Every page",
      title: function (s) { return s.phone ? "Health in one strip" : "Health in one pill"; },
      // verified: pages.health_pill (one status: a run in progress, else a stopped run, a failed run, a failed
      // reconciliation, "No runs yet", provisional valuations and stale data (both named when both hold), "Not
      // reconciled yet", "All checks pass"; on an archived view its record's own state, e.g. "Last run #N failed" or
      // "Never reconciled: archived before any run"; it links to the run's sheet or #health); pages.header
      // (archived_chip "ARCHIVED (read-only)"; the Fixture demo switch links to the other database; the phone strip:
      // "PAPER TRADING · FIXTURE DEMO" in the fixture, then strip_alert: a stopped or failed run or a failed
      // reconciliation, else a run in progress, else "archived #N", else provisional valuations and stale data, the last
      // only once a run exists); pages.tabbar (dots on Runs and More; More's is amber while data is not fresh, which
      // before the first run only means no run has fetched prices: health_pill's comment); pages.live_state
      // (last_run_id is null before the first run). No order is claimed: the pill and the strip rank a run in progress
      // differently, and "worst first" read as a severity ranking (critic, round 2); both cards list the five in one
      // order (critic, round 3: UX law 13), and say where the tab bar and More are.
      body: function (s) {
        var what = "what needs attention, if anything: ";
        if (s.phone) {
          var lead = s.fixture ? "After PAPER TRADING · FIXTURE DEMO (just the mode), the strip names " : "After PAPER TRADING, the strip names ";
          return [lead + "what needs attention: a run in progress, a stopped or failed run, a failed reconciliation, provisional valuations or stale data.",
            s.archived ? "On an archived experiment it says archived, unless a reconciliation failed."
              : s.noruns ? "Before the first run it names nothing, and a dot on **More** (bottom right) means no prices yet."
                : "If it names nothing, nothing needs a look."];
        }
        var sw = s.fixture ? "**Fixture demo** is on; switch it off for your paper test."
          : "**Fixture demo** opens a separate demo of invented data.";
        var list = "a run in progress, a stopped or failed run, a failed reconciliation, provisional valuations or stale data.";
        // An archived view keeps the pill's vocabulary (critic, round 4: a first tour there never learned it).
        if (s.archived) {
          return ["It names " + what + list, "**ARCHIVED (read-only)** beside it: you are viewing history, not the active experiment.", sw];
        }
        return [s.noruns ? "Before the first run it says so. Then it names " + what + list + " Press it for details."
          : "It names " + what + list + " **All checks pass** means none; press it for details.", sw];
      }
    },
    {
      id: "pages",
      target: "pages",
      phoneTarget: "tabs",
      phonePad: 0,  // the tab bar to its own edges: its margin lit the page scrolling behind it (critic, round 8)
      where: function (s) { return s.phone ? "Tab bar" : "Sidebar"; },
      title: "Seven pages",
      // verified: pages.PAGE_NAV and page_nav (a dot on Runs for a run in progress, stopped or failed, on Data & health
      // for a failed reconciliation, stale data or provisional valuations); pages.header (the switcher names the
      // experiment in view, and a click opens its menu of every experiment; clock_note in the sidebar foot);
      // pages.tabbar (Home, Portfolio, Decisions, Runs; More: Activity, Data & health, Settings, the fixture demo link,
      // the commands; dots on Runs and More).
      body: function (s) {
        if (s.phone) {
          // One topic per line on a phone: the switcher at the top is far from this card (critic, round 5); More's
          // contents get their own step (commands).
          return ["The tabs open Home, Portfolio, Decisions and Runs; **More** holds the rest.",
            "A dot on Runs or More flags a run in progress or something to check."];
        }
        return ["A dot on Runs or Data & health flags a run in progress or something to check.",
          "The box at the top names the experiment in view; click it for the others. " + (s.fixture
            ? "The note at the bottom gives the fixture's frozen clock."
            : "The bottom note says prices are end-of-day closes, never live quotes.")];
      }
    },
    {
      id: "performance",
      target: "performance",
      also: "chart",
      placement: "side",
      where: "Home",
      title: "Is it adding value?",
      enter: function () { show("home"); },
      // The chart is lit with the comparison on a desktop, so the cards of this page never sit on it unexplained
      // (critic, round 2), and the card sits beside the comparison, not under the chart (critic, round 3). On a phone the
      // chart is too far below to light with it (fitAlso): the money step lights it there instead (critic, round 3).
      // verified: pages._compare ("Return since inception, net of costs": Advisor, the benchmark and Keeping cash at
      // 0.00%; "Gap vs <benchmark>" in points via pts(); a missing value says why); pages.health assumptions ("Keeping
      // cash earns 0% (no interest is simulated ...)"); pages._charts (Equity: advisor, benchmark and keeping cash over
      // the valued sessions; Drawdown: "how far the advisor's equity ... is below its highest point so far"; "Not enough
      // history to chart" before a session is valued; the "Each valued session" link once there is a series, which
      // s.valued looks for). Fixture benchmark: DEMO.SPY (fixture_demo).
      body: function (s) {
        var bench = s.fixture ? "the benchmark" : "SPY";
        var first = "It compares the advisor, " + bench + " and keeping cash (no interest is simulated): return since inception, net of costs. **Gap** is the difference in points.";
        if (s.phone) return [first];
        return [first, s.valued ? "Below, the chart draws them over time; **Drawdown** shows the fall from the high; **Each valued session**, the numbers."
            : "Below, the chart draws them over time, and **Drawdown** shows the fall from the high, once a session is valued."];
      }
    },
    {
      id: "evidence",
      target: "evidence",
      where: "Home",
      title: "Is the evidence meaningful yet?",
      enter: function () { show("home"); },
      // verified: pages._evidence (badge "Too early to judge" while metrics' insufficient_sample: fewer than
      // MIN_SESSIONS_FOR_STATS = 20 valued sessions or MIN_TRADES_FOR_STATS = 10 closed trades; meters "Valued sessions
      // X of 20" and "Closed trades X of 10"; Learn more: "even then a small sample proves little"; the note line: mixed
      // builds, risk settings version, not annualized).
      body: function () {
        return ["Not before 20 valued sessions and 10 closed trades: until then it says **Too early to judge**, and the bars show the progress.",
          "Even then a small sample proves little (**Learn more**); the line below flags results mixing code builds or risk settings."];
      }
    },
    {
      id: "money",
      target: "money",
      phoneTarget: "chart",
      where: "Home",
      title: function (s) { return s.phone ? "How it moved over time" : "Every number opens its rows"; },
      enter: function () { show("home"); },
      // verified: pages.kpi_cards (Cash opens "cash", Invested links to #positions, Total P&L opens "pnl", Max drawdown
      // opens "valuations" once a session is valued and is a plain card before); pages.money_drawers (each cash movement
      // with the cash after it; realized, unrealized and dividends adding up to the total; "Costs are inside these figures
      // (paid in the fill prices)"); pages._costs (slippage and commission are in every P&L figure).
      // On a phone this step lights the chart (pages._charts: Equity, Drawdown, "Each valued session"), which the
      // comparison step cannot light there. It does not name the money cards below the chart: on a phone this card covers
      // them (critic, round 4).
      body: function (s) {
        if (s.phone) {
          var bench = s.fixture ? "the benchmark" : "SPY";
          return [(s.valued ? "The chart draws the advisor, " : "Once a session is valued, the chart draws the advisor, ") + bench +
            " and keeping cash over time; **Drawdown** shows the fall from the high." + (s.valued ? " **Each valued session** lists the numbers." : ""),
            "Simulated slippage and commission are already inside every number here."];
        }
        return ["Click a card for the rows that add up to it: every cash movement, the positions, the P&L's realized, unrealized and dividend parts, each valued session (max drawdown opens once one is valued).",
          "Simulated slippage and commission are already inside these numbers."];
      }
    },
    {
      id: "next",
      target: "next",
      where: "Home",
      title: function (s) { return s.fixture ? "The fixture scenario" : s.archived ? "An archived experiment" : "What happens next, and by when"; },
      enter: function () { show("home"); },
      // verified: pages.next_steps. Forward: a run in progress, a stopped run or a failed run comes first with its fix
      // as a copyable command (force-recover, takeover_words, retry_words); "Next analysis: ... run /paper-morning before
      // <time>" once the coming open is decided, else "Decisions submitted before <cutoff> fill at the <open>; later ones
      // at the following session's open"; then each pending order with the open it fills at. Fixture: "A finished
      // synthetic scenario ... Nothing here will fill or change." Archived: read-only, the commands act on the active
      // experiment, "Make #N active" (archives the active one; nothing is deleted), and archive_consequence. On a phone
      // the card covers the end of this panel, so the phone copy names only its top (critic, round 1).
      body: function (s) {
        if (s.fixture) {
          return ["In the fixture demo this panel describes the frozen scenario: nothing in it will fill or change.",
            "In your forward paper test this panel says what the next `/paper-morning` run will do and by when."];
        }
        if (s.archived) {
          return ["It is read-only, and the slash commands act on the active experiment.",
            "**Make active** resumes this one; it archives the active one and deletes nothing. The note under it says what archiving pauses."];
        }
        var first = "If a run failed, stopped or is still going, its fix comes first, with the command to copy; then when to run the next `/paper-morning` and what it will do.";
        return s.phone ? [first] : [first, "Below, each pending order shows the open it fills at."];
      }
    },
    {
      id: "health-card",
      target: "health-card",
      where: "Home",
      title: "Health at a glance",
      enter: function () { show("home"); },
      // verified: pages.health_summary (rows: the last run, or No runs yet; "Data N/M fresh"; "Identity N/M verified";
      // Reconciled, failed or not yet; provisional valuations when any; "Analysis tools N/M ran" (skills_summary) when
      // the run used any; "SQLite update advised" while the engine has the WAL-reset bug; glyphs: good = green tick,
      // warn = amber, bad = red, neutral = grey dash; the "Data & health details" link at the card's foot). The rows
      // shown only when they apply are said with "any" (critic, round 7: a listed row the panel did not have); a phone's
      // spotlight stops short of the lower rows and the "further down" line says so, not the list (round 7: the words
      // put a lit row further down).
      body: function (s) {
        var rows = "Each row is one check: the last run, fresh data, verified identities, reconciliation, and any provisional valuations, analysis tools or advised update.";
        return [rows, s.phone ? "Green passes; amber or red needs a look before you trust the numbers."
          : "Green passes; amber or red needs a look before you trust the numbers; grey is a note; **Data & health details** has more."];
      }
    },
    {
      id: "commands",
      target: "commands",
      pad: 0,  // a floating menu, lit to its own edges (critic, round 4: its margin lit the page behind it)
      also: "commands-button",
      where: function (s) { return s.phone ? "More" : "Every page"; },
      title: "The commands, ready to copy",
      // The menu is lit together with the button it opens from (Commands on a desktop, the More tab on a phone), so the
      // owner sees where to find it again (round 1: the menu alone left Commands in the dark).
      enter: function () { show("home"); openMenuWith("commands"); },
      // verified: pages.COMMANDS and _cmd_list ("/paper-morning each trading morning: settle, research, decide (or hold
      // cash)", "/paper-status any time: read-only snapshot, changes nothing", "/paper-review weekly"); pages.header
      // (the Commands menu, "Type these in Claude Code", in the top bar of every page); pages.tabbar (More: Activity,
      // Data & health, Settings, "Open the fixture demo" / "Leave the fixture demo", the same list); app.js copyText
      // (Copy puts the command on the clipboard); pages.next_steps ("there is no run button here: the advisor is your
      // Claude Code session"); cli.py (pa uses the forward database unless --fixture, which no command in
      // .claude/commands/paper-*.md passes: in the fixture demo the commands act on the forward paper test; critic,
      // round 2).
      // The menu already says when to use each command, so the card says what the menu does not (critic, round 7: the
      // card repeated each command's caption, UX law 19).
      body: function (s) {
        return [s.phone ? "**More** holds the other pages, " + (s.fixture ? "the link to leave the fixture demo" : "the fixture demo link (a separate demo of invented data)") + " and the slash commands with when to use each."
            : "**Commands**, on every page, lists each slash command with when to use it.",
          "No Run button: **Copy** puts a command on your clipboard, and the advisor runs in your Claude Code session" + (s.fixture ? ", on your forward paper test." : ".")];
      }
    },
    {
      id: "portfolio",
      target: "page-portfolio",
      pad: 16,
      where: "Portfolio",
      title: "What it holds now",
      enter: function () { show("portfolio"); },
      // verified: pages.positions_table (value with weight, unrealized, exit rules with how far the last close is from the
      // stop, last close, shares, held, thesis; a row opens the position's record; empty: "The account is all cash, which
      // is a valid outcome: the advisor is never required to trade."); pages.pending_table (each order fills at its
      // target session's official open and "is recorded on the next morning run"; a row opens its record).
      body: function (s) {
        // With no positions the section is one line (critic, round 3: the card described position rows not on screen).
        return [(s.positions ? "Each position: value, unrealized P&L, exit rules and thesis; " + (s.phone ? "tap" : "click") + " a row for its record."
            : "No positions now; each would show its value, unrealized P&L, exit rules and thesis."),
          // A newcomer can read the benchmark's own SPY buy as the advisor's (critic, round 6). SYNC WARNING:
          // pages.pending_table labels those rows "Benchmark" (origin BENCHMARK, the passive comparison's account).
          "Pending orders fill at their target open; a **Benchmark** row, if any, is the passive comparison's own buy, not the advisor's." + wideNote(s)];
      }
    },
    {
      id: "decisions",
      target: "page-decisions",
      more: false,  // its copy says "further down" when the spotlight is cut short (s.cut)
      pad: 16,
      where: "Decisions",
      title: "Why it decided",
      enter: function () { show("decisions"); },
      // verified: pages._page (the Decisions page: All decisions only when there are decisions, then the Advisor journal,
      // then Rejected proposals, always the last section); pages.decision_index (newest first; the Outcome column: filled,
      // pending, rejected, expired, canceled, recorded); pages.journal (run by run); pages.rejected_table ("Why it was
      // rejected": the failed checks; empty: "No rejected proposals."). Rejected proposals is said to be further down,
      // where it always is (critic, round 2: it was lit but never explained).
      body: function (s) {
        // "further down" only when the spotlight stops short of it (critic, round 5: the words mean outside the spotlight).
        var rejected = (s.cut ? "further down" : "last") + ", **Rejected proposals** shows what the validator refused and why.";
        if (!s.decisions) {
          return ["Each decision is listed here, newest first, with what became of it; the journal tells them run by run, and " + rejected,
            s.archived ? "This archived experiment has none." : "None yet: the first `/paper-morning` run fills this page."];
        }
        return ["**All decisions** lists each one, newest first, with what became of it: filled, pending, rejected, expired, canceled or recorded." + wideNote(s),
          "Below it the **Advisor journal** tells them run by run; " + rejected];
      }
    },
    {
      id: "sheet",
      target: "sheet",
      pad: 0,  // the side sheet, lit to its own edges
      optional: true,
      where: "Decisions",
      title: "One decision, start to finish",
      // The newest decision that placed an order opens in the side sheet, as a click on its row would (a NO TRADE's sheet
      // is mostly dashes: critic, round 1); with none, the newest decision; with no decisions there is nothing to open and
      // the card says so, centred. SYNC WARNING: pages.decision_index marks rows that placed an order with data-ordered.
      enter: function () {
        show("decisions");
        var row = document.querySelector('[data-tour="decision-index"] tr[data-detail][data-ordered]') ||
          document.querySelector('[data-tour="decision-index"] tr[data-detail]');
        if (row) opened.sheet = openSheet(row.getAttribute("data-detail"));
      },
      // verified: pages._decision_detail (Outcome first: _decision_outcome FILLED, PENDING, REJECTED, EXPIRED, CANCELED
      // or RECORDED; then Decided, Action, Symbol, Size, Thesis, Counterargument, Horizon, Exit rules, "Would invalidate
      // it", Confidence; dash() shows "—" for what the advisor left empty; then _checks_html "Deterministic checks" with
      // each observed value and limit, and the Evidence list, further down a long sheet).
      body: function (s) {
        if (!s.found) {
          // The same parts, in the same words, as the sheet card below (critic, round 7: two lists for one sheet).
          return ["When there are decisions, " + (s.phone ? "tap" : "click") + " one to open its side sheet: what became of it first, then the thesis and counterargument, the exit rules, what would invalidate it, each validator check with its limit, and the evidence.",
            s.archived ? "This archived experiment has none." : "None yet: they come from `/paper-morning` runs."];
        }
        // The sheet's parts in their order, and "scroll it for the rest": how much of a sheet is on screen depends on the
        // length of its thesis and on the layout, so the card never says which part is out of view (rounds 4 and 2: a
        // card sent the owner scrolling for exit rules that were in view, another named parts under the card).
        return ["A decision's side sheet opens on what became of it, then the thesis and counterargument, the exit rules, what would invalidate it, each validator check with its limit, and the evidence.",
          "Scroll it for the rest; a dash means the advisor gave none."];
      }
    },
    {
      id: "activity",
      target: "page-activity",
      pad: 16,
      where: "Activity",
      title: "What became of each order",
      enter: function () { show("activity"); },
      // verified: pages.fills_table (each fill: the session's official open, the fill price, Simulated at, Decided,
      // Recorded "(N h after the open)"; Closed round trips with "Why it exited"; Splits and dividends; empty: "No fills
      // yet. A recommendation is not a fill: fills appear only after a target session has opened, closed and been
      // settled by the next morning run.").
      body: function (s) {
        // The fills table comes first and is what is lit on a long page; the round trips and the splits and dividends
        // are said to be below it (round 4: named as if they were in view). With no fills the page is one line.
        var fills = s.table ? "Each fill: the session's official open, the price after slippage, when it was recorded. Below: round trips (why each exited), splits and dividends."
          : "Once there are fills, each shows the session's official open, the fill price after slippage and when it was recorded; then round trips, splits and dividends.";
        // With no fills the page's own line already says when a recommendation becomes a fill (critic, round 6).
        return s.table ? [fills, "A recommendation becomes a fill only after its session closes and is settled." + wideNote(s)] : [fills];
      }
    },
    {
      id: "runs",
      target: "page-runs",
      pad: 16,
      where: "Runs",
      title: "Every run, and what went wrong",
      enter: function () { show("runs"); },
      // verified: pages.runs_table (newest first; the reason a failed, interrupted or stopped run stopped sits under its
      // status, cause first (problem_short); Report: "open" when the run wrote one, "—" when not; a row opens the run's sheet with its
      // log in words and, for a run in progress, interrupted or failed, "What now", with the command to copy where there
      // is one).
      body: function (s) {
        return ["Every run, newest first; a failed, interrupted or stopped run says why under its status.",
          (s.phone ? "Tap" : "Click") + " one for its log and, if it failed or is stuck, what to do; **Report** (last column) opens its report." + wideNote(s)];
      }
    },
    {
      id: "data",
      target: "health-data",
      where: "Data & health",
      title: "Is the data fresh?",
      enter: function () { show("health"); },
      // verified: pages.health (the "Data freshness and instrument identity" panel: "N/M symbols have a bar for the last
      // completed session", "N/M identities verified"; "Needs attention (N)" lists each symbol whose data is STALE,
      // SUSPECT or MISSING or whose identity is unverified, and "What happens next" follows that list when a symbol is
      // stale or missing; else "Nothing needs attention."; the full list sits closed under "Show all N symbols", so
      // s.table, which counts only tables on screen, says whether a Needs attention list shows). Lit on its own: fresh
      // data, the first thing decision 2 asks, was never lit (critic, round 2).
      body: function (s) {
        return ["It checks each symbol for the last completed session's bar and a verified identity.",
          s.table ? "**Needs attention** lists each problem symbol (stale, missing or suspect price, unverified identity), then **What happens next** for stale or missing prices." + wideNote(s)
            : "Problem symbols (stale, missing or suspect price, unverified identity) would be listed here, then what happens next for stale or missing prices; now: **Nothing needs attention**."];
      }
    },
    {
      id: "recon",
      target: "health-recon",
      more: false,  // its copy says what is below (critic, round 5: the line made it point down twice)
      also: "health-assumptions",
      where: "Data & health",
      title: "Can the record be trusted?",
      enter: function () { show("health"); },
      // verified: pages.health (the Reconciliation (latest check) panel: "Checked <time> by run #N" or "outside a morning
      // run", one pass/FAIL per check; Provisional valuations: "kept, flagged and counted, never edited", each session
      // with its reasons from provisional_words, or none yet; the Calendar line; the "Execution and data assumptions"
      // panel beside it (two columns above 1100 px: app.css .health-grid) or below it: market-on-open fills from the
      // session's daily bar, slippage and commission, exits on closes filled at the next open, "Keeping cash earns 0%").
      // s.alsoAt is where the assumptions panel was measured to be, or "" when it was left dark (on a phone it is too far
      // below to light with this panel).
      body: function (s) {
        // A phone shows only this panel's checks above the card: its provisional valuations are said to be further down.
        return ["It shows whether the ledger, fills, orders and snapshots reconcile, checked when and by which run; then any" +
          " provisional valuation with its reason: flagged and kept, never edited.",
          (s.alsoAt === "beside" ? "Beside it, " : "Below, ") + "**Execution and data assumptions** lists the simulation's rules: fills, costs, exits, cash at 0%."];
      }
    },
    {
      id: "risk",
      target: "risk",
      more: false,  // its copy says "further down" when the spotlight is cut short (s.cut)
      where: "Settings",
      title: "Changing the risk settings",
      enter: function () { show("settings"); },
      // verified: pages.settings (risk form: "Saving creates version N+1, effective immediately and only for FUTURE
      // decisions. Earlier decisions keep the limits they were checked against."; an echo line under each field with the
      // meaning and the allowed range; the freeze hint under the field that makes every buy impossible (FREEZE_KEYS);
      // "Reason for the change (required)"; #cfg-diff, then "Save as new version", last in the form; disabled_reason for
      // the fixture demo ("frozen synthetic scenario; its settings are not editable here"), a run in progress and an
      // archived view ("only the active experiment's settings can change")); app.js echoText, rangeProblem, freezeSync,
      // renderCfgDiff ("Saving changes <field>: <was> → <new>"). Save and the change list are said to be further down,
      // where they are (critic, round 2). A locked form leads with its lock (critic, round 1).
      body: function (s) {
        var how = "saving creates a new version for future decisions only; earlier ones keep their limits.";
        if (s.fixture) return ["This form is locked in the fixture demo: its scenario is frozen.", "In your forward paper test, " + how];
        if (s.archived) return ["This form is locked on an archived experiment: only the active experiment's settings change.", "On the active experiment, " + how];
        // Not a repeat of the form's own "Saving creates version N ..." line (critic, round 6). verified: pages.history
        // ("each decision records the versions it was checked under"; every version with its reason).
        return ["Each saved version keeps your reason; every decision records the version it was checked against.",
          "Each field shows what its number means and warns if no buy could pass. " + (s.cut ? "Further down" : "Below the fields") + " come a required reason and, above **Save as new version**, what would change."];
      }
    },
    {
      id: "new-experiment",
      target: "new-experiment",
      more: false,  // its copy says "further down" when the spotlight is cut short (s.cut)
      where: "Settings",
      title: "A new experiment, and what it would do",
      enter: function () { show("settings"); },
      // verified: pages.settings (newexp: "Different capital means a new experiment with its own ledger and benchmark,
      // funded once with that amount; it starts with the current risk settings. The current experiment is archived
      // (never reset or deleted) ..."; archive_note (what archiving pauses) when the form is open and there is something
      // to pause, which s.archiveNote looks for; the confirm checkbox "Archive the current experiment and make the new one
      // active", further down; nx_reason in the fixture ("cannot create experiments ... belong to the forward paper
      // test") and on an archived view ("Switch to the active experiment ... open #N")).
      body: function (s) {
        if (s.fixture) return ["The fixture demo cannot create experiments: new ones belong to your forward paper test."];
        // Not a repeat of the form's own description (critic, round 6): what it means for the evidence. verified:
        // metrics.compute per experiment (a new one starts at 0 valued sessions).
        var first = "A new experiment starts its record from zero: its evidence restarts at 0 of 20 valued sessions.";
        if (s.archived) return [first, "From an archived experiment this form is locked: a new one starts from the active experiment, which its note links to."];
        return [first, "Creating one archives the current experiment and deletes nothing. " + (s.archiveNote
          ? "The form says first what archiving pauses and, " + (s.cut ? "further down" : "at its end") + ", asks you to confirm." : (s.cut ? "Further down" : "At its end") + ", the form asks you to confirm.")];
      }
    },
    {
      id: "experiments",
      target: "experiments",
      pad: 16,
      where: "Settings",
      title: "Compare experiments, and switch",
      enter: function () { show("settings"); },
      // verified: pages.settings (the Experiments table: capital, inception, Equity, Return, "Gap vs benchmark", status;
      // a Make active button on every row but the active one, none in the fixture demo, locked while a run is in progress;
      // "Make active switches the slash commands to the experiment you pick." with archive_consequence: pending orders
      // will not fill and positions are not valued while archived, caught up session by session if made active again);
      // pages.next_steps ("nothing is deleted"); server._act_activate.
      body: function (s) {
        var first = "It lists every experiment here, with its capital, equity, return and gap against the benchmark." + wideNote(s);
        if (s.fixture) return [first, "Switching experiments belongs to your forward paper test; the fixture demo is frozen."];
        return [first, "With more than one, **Make active** (last column) switches the slash commands to another; the current one is archived, nothing deleted."];
      }
    },
    {
      id: "finish",
      target: "next",
      more: false,  // the last words stay how to reopen the tour (critic, round 4); the copy says when the panel continues
      where: "Home",
      title: "Where to start",
      enter: function () { show("home"); },
      // verified: the steps above (every page); pages.next_steps ("What happens next": what /paper-morning will do and
      // the cutoff); pages.header (Take the tour under the seven pages; the phone's "Tour" button in the app bar). The
      // recap is a scannable list, not a paragraph (critic, round 2: the closing card ran past 60 words).
      body: function (s) {
        var start = s.fixture ? "In your forward paper test, start each trading morning at Home's **What happens next**."
          : s.archived ? "Start on the active experiment's Home, at **What happens next**: what to run, by when."
            : "Each trading morning, start at **What happens next**" + (s.cut ? " (it continues further down)" : "") + ": what to run, by when.";
        return ["You saw: Home (results, next steps, health); Portfolio, Decisions, Activity (the record); Runs, Data & health (trust); Settings (changes).",
          start,
          s.phone ? "Reopen the tour: **Tour**, top right." : "Reopen the tour: **Take the tour**, in the sidebar."];
      }
    }
  ];
  // END STEPS

  // A table in the spotlight that is wider than its card scrolls sideways: say so, so no column is hunted for (critic,
  // round 1: phone cards named columns that were scrolled off to the right).
  function wideNote(s) {
    return s.wide ? (s.phone ? " Swipe for more columns." : " Scroll sideways for more columns.") : "";
  }

  // ----------------------------------------------------------------------------------------------------------------
  // The engine. Placement is measured, never guessed (a lesson from that earlier engine): the card's real size decides
  // whether it sits below or above the spotlight, beside it, or, for a target taller than the screen, at the free end
  // with the spotlight clipped to the part of the target still on screen. The card never covers the spotlight.
  var M = 12;          // viewport margin
  var GAP = 14;        // between the spotlight and the card
  var PAD = 6;         // around the target (a step's `pad` widens it for a page-level target) and its `also` elements
  var CARD_W = 360;    // desktop card width (a phone uses the screen's width)
  var APPEAR_MS = 400; // the page renders synchronously, so a target that has not appeared by then is absent
  var SETTLE_MS = 650; // smooth scrolling has no end event: keep placing for this long

  var RING_R = 6;      // the ring's corner radius (app.css .tour-hole); a lit part with no margin uses its own radius
  var root = null, dim = null, card = null, rings = [], shades = [], caps = [], ui = {};
  var index = 0, isOpen = false, target = null, extra = [], pad = PAD, token = 0, lastFocus = null, inerted = [], placeQueued = false;
  var opened = { sheet: false, menu: null };

  function context() {
    var bd = document.body, st = null;
    // SYNC WARNING: pages.live_state renders data-state for app.js; last_run_id is null until the active experiment's
    // first run (tests/test_tour.py checks it in every state).
    try { st = JSON.parse(bd.getAttribute("data-state") || "null"); } catch (err) { st = null; }
    var fixture = bd.classList.contains("db-fixture");
    var archived = (bd.getAttribute("data-exp") || "") !== (bd.getAttribute("data-active") || "");
    return {
      phone: PHONE.matches,
      fixture: fixture,
      archived: archived,
      noruns: !fixture && !archived && !!st && st.last_run_id === null,
      // SYNC WARNING: pages._charts renders "Each valued session" (data-open="valuations") once a session is valued, and
      // pages.settings the new-experiment form's archive note only when that form is open and archiving pauses something.
      valued: !!document.querySelector('[data-tour="chart"] [data-open="valuations"]'),
      archiveNote: !!document.querySelector('[data-tour="new-experiment"] .archive-note'),
      decisions: !!document.querySelector('[data-tour="decision-index"]'),
      found: false,
      wide: false,
      table: false,
      alsoAt: ""
    };
  }

  // Laid out = has a box and is not visibility:hidden (the closed side sheet keeps its box but is hidden).
  function laidOut(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
  }

  // A desktop piece and its phone twin share one anchor name; the one that is laid out is the one on screen.
  function findTarget(names) {
    for (var i = 0; i < names.length; i++) {
      var all = document.querySelectorAll('[data-tour="' + names[i] + '"]');
      for (var j = 0; j < all.length; j++) if (laidOut(all[j])) return all[j];
    }
    return null;
  }

  function stepNames(step) {
    var t = PHONE.matches && step.phoneTarget ? step.phoneTarget : step.target;
    return Array.isArray(t) ? t : [t];
  }

  // A step's `also` anchors are lit with its target: a menu with the button it opens from, the pill with the switch,
  // the comparison with its chart, reconciliation with the assumptions beside it.
  function alsoEls(step) {
    var a = step.also ? (Array.isArray(step.also) ? step.also : [step.also]) : [];
    return a.map(function (name) { return findTarget([name]); }).filter(Boolean);
  }

  // The `also` elements to light: all of them when they fit on screen with an in-flow target, else none. On a phone
  // the chart sits far below the comparison and the assumptions panel far below reconciliation; lighting them would
  // clip the target itself, so they stay dark and the card says "further down" (critic, round 2). A pinned target
  // (the top bar's pill, a menu) keeps its companion: neither scrolls away.
  function fitAlso(step) {
    var a = alsoEls(step);
    if (!a.length || pinned(target)) return a;
    var u = frame(a), b = band();
    return u.height + 2 * pad <= b.bottom - b.top - 16 ? a : [];
  }

  // Where the first lit companion sits, measured: "beside" the target, "below" it, or "" when none is lit.
  function alsoAt() {
    if (!extra.length) return "";
    return extra[0].getBoundingClientRect().left >= target.getBoundingClientRect().right - 1 ? "beside" : "below";
  }

  // The box the card is placed against: the target, grown to take in its `also` elements (the lit ones by default).
  function frame(list) {
    var r = target.getBoundingClientRect(), u = { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
    (list || extra).forEach(function (e) {
      if (!laidOut(e)) return;
      var q = e.getBoundingClientRect();
      u.top = Math.min(u.top, q.top);
      u.bottom = Math.max(u.bottom, q.bottom);
      u.left = Math.min(u.left, q.left);
      u.right = Math.max(u.right, q.right);
    });
    u.width = u.right - u.left;
    u.height = u.bottom - u.top;
    return u;
  }

  // Pinned: fixed or sticky itself or inside such a box (the top bar, the strip, the sheet, the menus). Scrolling does
  // not move it, and nothing covers it.
  function pinned(el) {
    for (var n = el; n && n !== document.body; n = n.parentElement) {
      var p = getComputedStyle(n).position;
      if (p === "fixed" || p === "sticky") return true;
    }
    return false;
  }

  // The part of the window the page's own sticky bars leave free (in-flow targets scroll under the bars).
  // SYNC WARNING: app.css (header.topbar sticky on a desktop; .paper-strip under the app bar and the fixed .tabbar on
  // a phone).
  function band() {
    var top = 0, bottom = window.innerHeight;
    var bar = document.querySelector(PHONE.matches ? ".paper-strip" : "header.topbar");
    if (laidOut(bar)) top = bar.getBoundingClientRect().bottom;
    var tabs = PHONE.matches ? document.querySelector(".tabbar") : null;
    if (laidOut(tabs)) bottom = tabs.getBoundingClientRect().top;
    return { top: top, bottom: bottom };
  }

  function cardWidth() { return PHONE.matches ? window.innerWidth - 2 * M : Math.min(CARD_W, window.innerWidth - 2 * M); }

  function sideRoom(r, cw) {
    if (PHONE.matches) return false;
    return window.innerWidth - M - (r.right + pad + GAP) >= cw || r.left - pad - GAP - M >= cw;
  }

  // Where to scroll so the target and its card can be read together; null when they already can.
  function scrollFor(el) {
    if (pinned(el)) return null;
    var r = frame(), b = band();
    var top = b.top + 8, bottom = b.bottom - 8, room = bottom - top;
    var h = r.height + 2 * pad, ch = card.offsetHeight, cw = cardWidth();
    var delta;
    if (h + GAP + ch <= room) {
      var fits = (r.top - pad >= top && r.bottom + pad + GAP + ch <= bottom) || (r.top - pad - GAP - ch >= top && r.bottom + pad <= bottom);
      if (fits) return null;
      delta = r.top - pad - top - (room - h - GAP - ch) / 2;
    } else if (h <= room && sideRoom(r, cw)) {
      if (r.top - pad >= top && r.bottom + pad <= bottom) return null;
      delta = r.top - pad - top - (room - h) / 2;
    } else {
      delta = r.top - pad - top;  // taller than the screen: its top just under the sticky bar
    }
    var max = document.documentElement.scrollHeight - window.innerHeight;
    var y = Math.max(0, Math.min(window.scrollY + delta, max));
    return Math.abs(y - window.scrollY) < 2 ? null : y;
  }

  // A clipped spotlight ends on a whole row, panel or paragraph instead of cutting one in half (critic, round 1: the lit
  // edge sliced runs and panels). `edge` is where the clip falls; returns the nearest whole-block edge on the lit side.
  // SYNC WARNING: the block kinds the dashboard draws (table rows, list items, panels, cards, run blocks, notices,
  // headings, paragraphs, key-value rows, form fields).
  var BLOCKS = "tr, li, .panel, .card, .run-block, .notice, h2, h3, h4, p, dd, .field, .table-wrap, .exp-row2";
  // A snapped edge sits this close to its block, whatever the step's `pad`: a page-level target's 16 px inset reached
  // into the next row's text (critic, round 3: the ring ran through a Runs row).
  var SNAP = 1;  // round 4: at 4 px a 4 px sliver of the next element (a Save button) showed inside the ring
  var DOCK = 8;  // a docked card's distance from the snapped spotlight edge (the ring is 2 px)
  var CUT_PAD = 10;  // a cut-short spotlight's most room below its last lit line
  // Where a cut-short spotlight may end: never through a line of text, a field, a button, a chip or a table row
  // (critic, rounds 1-5). A whole block's bottom is preferred when one is near the edge; else the lowest line break.
  // The cut gets the ring's margin when the next thing starts far enough below (round 5: a 1 px margin read as
  // cramped), less when it does not, so nothing below shows inside the ring (round 4). `cutSpans` keeps the last
  // measurement for the docked card, which then starts no lower than the next line.
  // SYNC WARNING: .next-cmds is pages.next_steps' footer ("Run the advisor in Claude Code" and its command chips): a
  // cut through it split the chips (critic, round 7).
  var SOLID = "tr, input, select, textarea, button, svg, img, .chip, .badge, .cmd-pill, .attn, .notice, .meter, .seg, .next-cmds";
  var cutSpans = [];
  function spans() {
    var out = [], range = document.createRange();
    var walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    for (var n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!/\S/.test(n.nodeValue)) continue;
      range.selectNodeContents(n);
      // Cut to the text's own element: a line-clamped cell still reports its hidden lines, and a line box can stand a
      // pixel outside its cell (round 9 frames: both blocked every cut through the pending-orders table).
      var pr = n.parentElement.getBoundingClientRect();
      Array.prototype.forEach.call(range.getClientRects(), function (r) {
        var t = Math.max(r.top, pr.top), b = Math.min(r.bottom, pr.bottom);
        if (b - t > 0.5) out.push([t, b]);
      });
    }
    // A row is marked: its top edge is only the row's padding, so the docked card may sit over it (below).
    Array.prototype.forEach.call(target.querySelectorAll(SOLID), function (e) {
      var r = e.getBoundingClientRect();
      if (r.height > 0 && r.width > 0) out.push([r.top, r.bottom, e.tagName === "TR"]);
    });
    return out;
  }
  function nextTop(sp, y) {
    var t = Infinity;
    sp.forEach(function (x) { if (x[0] >= y - 0.5 && x[0] < t) t = x[0]; });
    return t;
  }
  function snapBottom(edge, top) {
    var sp = spans();
    cutSpans = sp;
    var blocked = function (y) { return sp.some(function (x) { return x[0] < y - 0.5 && x[1] > y + 0.5; }); };
    // A candidate b < edge. Roomy: the next thing starts at least a ring's margin below b, so the ring never sits on
    // a neighbour's edge (critic, round 5: a 1 px margin above the Save button read as cramped); the margin may then
    // shrink only to meet the edge. Otherwise the margin is what the gap allows.
    // The ring sits in the middle of the gap below b, up to the usual margin (critic, round 6: a ring right on a chip's
    // top border read as cramped); roomy means a gap of at least 8 px.
    // Up to CUT_PAD below the last lit line (critic, round 7: 7 px under the text read as cramped; elsewhere 9-14).
    var cut = function (b, roomy) {
      var g = nextTop(sp, b + 0.5) - b;
      if (roomy && g < 8) return null;
      var y = Math.min(edge, b + Math.max(0, Math.min(CUT_PAD, Math.floor(g / 2) - 1)));
      // Not done: putting the ring on a row divider just above the cut (round 8 polish). Rounded onto the divider, a cut
      // between table rows landed in the next row and was rejected, so desktop Runs lit no rows (round 9 frames).
      return blocked(y) ? null : y;
    };
    var pick = function (list, near, roomy) {
      list = list.filter(function (b) { return b > top + 60 && b < edge && (!near || edge - b <= near); }).sort(function (a, b) { return b - a; });
      for (var i = 0; i < list.length; i++) { var y = cut(list[i], roomy); if (y !== null) return y; }
      return null;
    };
    var blocks = [], rows = [], lines = sp.map(function (x) { return x[1]; });
    Array.prototype.forEach.call(target.querySelectorAll(BLOCKS), function (e) { var r = e.getBoundingClientRect(); if (r.height > 0) blocks.push(r.bottom); });
    Array.prototype.forEach.call(target.querySelectorAll("tr"), function (e) { var r = e.getBoundingClientRect(); if (r.height > 0) rows.push(r.bottom); });
    // Near the edge a whole block wins, roomy first, then a tight one such as the line between two table rows (round 9
    // frames: with rows touching, a roomy line far above the table won and left the phone card mostly empty).
    var y = pick(blocks, 120, true);
    if (y === null) y = pick(rows, 120, false);  // tight cuts only between table rows, which have their own borders
    if (y === null) y = pick(lines, 0, true);
    if (y === null) y = pick(blocks, 0, false);
    if (y === null) y = pick(lines, 0, false);
    return y === null ? edge : y;
  }
  function snapTop(edge, bottom) {
    var best = null;
    Array.prototype.forEach.call(target.querySelectorAll(BLOCKS), function (b) {
      var r = b.getBoundingClientRect();
      if (r.height > 0 && r.top - SNAP >= edge && r.top < bottom - 60 && (best === null || r.top < best)) best = r.top;
    });
    return best === null ? edge : best - SNAP;
  }

  // Whether an in-flow target cannot be lit whole beside or above or below its card (the same test as scrollFor):
  // then the spotlight is cut short and the card says the rest is further down (critic, round 3: tall targets were
  // cut on a phone with nothing saying so). Measured before placement, so the line never flickers while scrolling.
  function cutShort() {
    if (!target || pinned(target)) return false;
    var r = frame(), b = band(), room = b.bottom - b.top - 16, hh = r.height + 2 * pad;
    if (hh + GAP + card.offsetHeight <= room) return false;
    return hh > room || !sideRoom(r, cardWidth());
  }

  function px(el, prop, v) { el.style.setProperty(prop, Math.round(v) + "px"); }

  function place() {
    if (!isOpen) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    var cw = cardWidth();
    px(card, "width", cw);
    card.style.removeProperty("min-height");
    card.classList.remove("tour-flush", "tour-flush-top");
    var ch = card.offsetHeight, stretch = 0;
    var clampX = function (x) { return Math.min(Math.max(M, x), Math.max(M, vw - M - cw)); };
    var clampY = function (y) { return Math.min(Math.max(M, y), Math.max(M, vh - M - ch)); };
    var h = null;
    if (target && laidOut(target)) {
      var r = frame(), fp = extra.length ? Math.max(pad, PAD) : pad;  // room for a companion's margin (round 5)
      var lim = pinned(target) ? { top: 0, bottom: vh } : band();
      h = { t: Math.max(r.top - fp, lim.top + 2), b: Math.min(r.bottom + fp, lim.bottom - 2),
            l: Math.max(r.left - fp, 2), r: Math.min(r.right + fp, vw - 2) };
      // An in-flow target taller than the window ends on a whole block too, not mid-row at the window's edge (critic,
      // round 3: the ring sliced a command chip at the bottom of the window).
      if (!pinned(target) && r.bottom + pad > lim.bottom - 2) h.b = snapBottom(lim.bottom - 2, h.t);
      if (h.b - h.t < 12 || h.r - h.l < 12) h = null;  // scrolled out of view: plain dim, card at the bottom
    }
    var x, y;
    if (!h) {
      // Nothing to light (no decision to open yet): the card sits at the bottom, so the page's own empty-state line
      // above it stays readable (critic, round 3: a centred card hid "No morning runs yet ...").
      x = (vw - cw) / 2;
      y = Math.max(M, vh - M - ch);
    } else {
      var cx = clampX((h.l + h.r) / 2 - cw / 2);
      var sides = {
        below: function () { return vh - M - (h.b + GAP) >= ch ? [cx, h.b + GAP] : null; },
        above: function () { return h.t - GAP - M >= ch ? [cx, h.t - GAP - ch] : null; },
        right: function () { return !PHONE.matches && vw - M - (h.r + GAP) >= cw ? [h.r + GAP, clampY(h.t)] : null; },
        left: function () { return !PHONE.matches && h.l - GAP - M >= cw ? [h.l - GAP - cw, clampY(h.t)] : null; }
      };
      var order = STEPS[index].placement === "side" ? ["right", "left", "below", "above"] : ["below", "above", "right", "left"];
      var spot = null, side = null;
      for (var i = 0; i < order.length && !spot; i++) { spot = sides[order[i]](); side = order[i]; }
      // A phone card above a pinned target (the More menu) runs flush to the top edge: just above the menu, its top
      // edge sliced the app bar's words (critic, round 6).
      if (spot && side === "above" && PHONE.matches && pinned(target)) { spot[1] = 0; card.classList.add("tour-flush-top"); }
      if (!spot) {
        // Nothing fits beside the whole target: dock the card at one end of the window and stop the spotlight short of
        // it, so the card never sits on what it describes. A target in the page was scrolled so its top (a page's
        // title, a table's head) is in view: the card docks at the bottom. A pinned one (a menu over the tab bar, the
        // side sheet) keeps whichever end leaves more of it lit (round 1: Runs docked at the top and dimmed its title).
        var atBottom = vh - M - ch, keepBottom = Math.min(h.b, atBottom - GAP) - h.t;
        var keepTop = h.b - Math.max(h.t, M + ch + GAP);
        // Centred under the lit part, not in the window's far corner (critic, round 2: UX laws 4 and 8).
        var dx = PHONE.matches ? M : cx;
        // (Near the end of a page the target may not scroll up far enough: then the bottom would leave almost nothing lit.)
        var bottomFirst = (!pinned(target) && keepBottom >= 40) || keepBottom >= keepTop;
        // The card then moves up (or down) against the snapped edge, covering the part row the snap left out, so no
        // row the card names shows dimmed between the spotlight and the card (critic, round 3).
        // It sits DOCK px off the snapped edge: the next row's padding shows there, never a sliver of its text (round 7
        // frames: a 14 px gap showed the top of the next row's words).
        // On a phone the card then runs flush to the window's edge like a bottom (or top) sheet, so no dimmed sliver of
        // the target peeks out beyond it (critic, round 4: validator rows showed under the sheet step's card).
        if (bottomFirst) {
          // A phone's card runs to the window's edge, so the cut can come down to just above its natural height.
          var limit = PHONE.matches ? vh - ch - DOCK : atBottom - GAP;
          if (h.b > limit) h.b = snapBottom(limit, h.t);
          // No lower than the next line below the cut, so no sliver of it shows between the spotlight and the card.
          // A desktop card keeps the usual gap (critic, round 6: a card touching the ring); a phone's sits right under
          // the cut, no lower than the next line. A table row's top edge does not count: the row's padding is all that
          // shows above its words (round 11 frames: the row's edge pulled the card up against the ring).
          var below = cutSpans.filter(function (sp) { return !sp[2]; });
          spot = [dx, PHONE.matches ? Math.min(atBottom + M, Math.max(h.b + 2, Math.min(h.b + DOCK, nextTop(below, h.b)))) : Math.min(atBottom, h.b + GAP)];
          if (PHONE.matches) {
            // Stretched only by a few pixels (round 5: 120 pt of white looked unfinished; rounds 6 and 7: 50, then 17 pt
            // still did); otherwise the card keeps its height at the bottom and the band above it stays dim.
            card.classList.add("tour-flush");
            if (vh - spot[1] - ch <= 4) stretch = vh - spot[1];
            else spot[1] = vh - ch;
            // Its top edge never cuts a line of text in half (critic, round 8: the next row's words peeked out sliced
            // between the ring and the sheet): it rises to that line's top, by at most a line; the few pixels this
            // adds sit under its buttons (app.css .tour-flush .tour-foot).
            var sliced = below.filter(function (q) { return q[0] > h.b && q[0] < spot[1] - 0.5 && q[1] > spot[1] + 0.5; });
            if (!stretch && sliced.length) {
              var lineTop = Math.floor(Math.min.apply(null, sliced.map(function (q) { return q[0]; }))) - 1;
              if (spot[1] - lineTop <= 28 && lineTop >= h.b + 2) { spot[1] = lineTop; stretch = vh - lineTop; }
            }
          }
        } else {
          if (h.t < M + ch + GAP) h.t = snapTop(M + ch + GAP, h.b);
          spot = [dx, Math.max(M, h.t - DOCK - ch)];
          if (PHONE.matches) { if (h.t - DOCK - ch <= 4) stretch = h.t - DOCK; spot[1] = 0; card.classList.add("tour-flush-top"); }
        }
      }
      x = spot[0];
      y = spot[1];
      // SYNC WARNING: app.css .tour-flush and .tour-flush-top pad the card by M on each side, so its text keeps the width
      // it was measured at (cw = vw - 2M) and so its height.
      if (card.classList.contains("tour-flush") || card.classList.contains("tour-flush-top")) { x = 0; px(card, "width", vw); }
    }
    // What is lit: the target and each of its `also` elements, every one cut to the box above (the visible part, short
    // of the card when the target was clipped). Two separate holes, not their bounding box: one box lit the end of the
    // health pill and the fixture switch between the Commands button and its menu (round 2). The card was placed
    // against the bounding box, so it overlaps none of them.
    var lit = [];
    if (h) {
      [target].concat(extra).forEach(function (e, i) {
        if (!laidOut(e)) return;
        // A companion gets the ring's margin too (round 4: unpadded, the switch's text touched the dim), but never over
        // a neighbouring box (round 3: the chart's margin lit the evidence strip's border). A target lit with no
        // margin (a menu, the sheet) keeps its own corner radius.
        var q = e.getBoundingClientRect(), p = i === 0 ? pad : PAD;
        var o = { t: Math.max(q.top - p, h.t), b: Math.min(q.bottom + p, h.b), l: Math.max(q.left - p, h.l), r: Math.min(q.right + p, h.r),
                  rad: i === 0 && pad === 0 ? radiusOf(e) : RING_R };
        clipToNeighbours(e, q, o, i === 0);
        o.t = Math.round(o.t); o.b = Math.round(o.b); o.l = Math.round(o.l); o.r = Math.round(o.r);
        // A companion lines up with the target's edge when it nearly does (round 6: a 4 px step where the Commands
        // button met its menu).
        if (i > 0 && lit.length) {
          if (Math.abs(o.r - lit[0].r) <= PAD + 1) o.r = lit[0].r;
          if (Math.abs(o.l - lit[0].l) <= PAD + 1) o.l = lit[0].l;
        }
        if (i > 0 && lit.length) apart(lit[0], o, target.getBoundingClientRect(), q);
        if (o.b - o.t >= 4 && o.r - o.l >= 4) lit.push(o);
      });
    }
    // The dim is the exact complement of the lit rectangles, tiled from whole-pixel rectangles so no seam shows (a single
    // box with a 9999 px spread shadow did not paint its spread in headless Chromium; see app.css).
    dim.hidden = lit.length > 0;
    // The ring outlines the target only: rings around a button and the menu below it crossed where the two holes meet
    // (round 3). The `also` element is lit without an outline of its own.
    paint(rings, "tour-hole", lit.slice(0, 1).map(function (o) { return [o.l, o.t, o.r - o.l, o.b - o.t]; }));
    if (lit.length) px(rings[0], "border-radius", lit[0].rad);
    paint(shades, "tour-shade", lit.length ? complement(lit, vw, vh) : []);
    paintCaps(lit);
    px(card, "left", x);
    px(card, "top", y);
    if (stretch) px(card, "min-height", Math.max(stretch, ch));
  }

  function radiusOf(e) { return Math.round(parseFloat(getComputedStyle(e).borderTopLeftRadius) || 0); }

  // Keep a lit margin off the boxes beside it (its siblings, and its parent's). A companion's stops at the neighbour's
  // edge; the target's (`half`) stops halfway into the gap, so its ring never sits on the neighbour's border (critic,
  // round 7: the chart's ring took in the evidence strip's bottom border).
  function clipToNeighbours(e, q, o, half) {
    var reach = function (g) { return half ? Math.max(0, Math.floor(g / 2) - 1) : g; };
    [e.parentElement, e.parentElement && e.parentElement.parentElement].forEach(function (par, level) {
      if (!par) return;
      var self = level === 0 ? e : e.parentElement;
      Array.prototype.forEach.call(par.children, function (sib) {
        if (sib === self || sib.contains(e) || !laidOut(sib)) return;
        var b = sib.getBoundingClientRect();
        if (b.width < 4 || b.height < 4) return;  // a visually hidden heading (round 8: it pinned a ring to its corner)
        var across = b.right > o.l && b.left < o.r, along = b.bottom > o.t && b.top < o.b;
        if (across && b.bottom <= q.top + 1) o.t = Math.max(o.t, q.top - reach(q.top - b.bottom));
        if (across && b.top >= q.bottom - 1) o.b = Math.min(o.b, q.bottom + reach(b.top - q.bottom));
        if (along && b.right <= q.left + 1) o.l = Math.max(o.l, q.left - reach(q.left - b.right));
        if (along && b.left >= q.right - 1) o.r = Math.min(o.r, q.right + reach(b.left - q.right));
      });
    });
  }

  // Two lit parts keep up to 8 px of dim between them, each taking half of what is left of the gap between their boxes
  // (critic, round 7: the target's ring 3 px from the lit companion read as one outline or two; round 8: the Commands
  // button's margin ran into its menu's ring). Parts that touch stay joined (the More tab under its sheet).
  function apart(t, o, qt, q) {
    var ext = function (g) { return Math.max(0, Math.floor((g - 8) / 2)); };
    if (q.top < qt.bottom && qt.top < q.bottom) {
      if (q.left >= qt.right - 1 && o.l - t.r < 8) { var e1 = ext(q.left - qt.right); t.r = Math.min(t.r, Math.round(qt.right + e1)); o.l = Math.max(o.l, Math.round(q.left - e1)); }
      if (qt.left >= q.right - 1 && t.l - o.r < 8) { var e2 = ext(qt.left - q.right); t.l = Math.max(t.l, Math.round(qt.left - e2)); o.r = Math.min(o.r, Math.round(q.right + e2)); }
    }
    if (q.left < qt.right && qt.left < q.right) {
      if (q.top >= qt.bottom + 1 && o.t - t.b < 8) { var e3 = ext(q.top - qt.bottom); t.b = Math.min(t.b, Math.round(qt.bottom + e3)); o.t = Math.max(o.t, Math.round(q.top - e3)); }
      if (qt.top >= q.bottom + 1 && t.t - o.b < 8) { var e4 = ext(qt.top - q.bottom); t.t = Math.max(t.t, Math.round(qt.top - e4)); o.b = Math.min(o.b, Math.round(q.bottom + e4)); }
    }
  }

  // Rounded corners for every lit part: four small caps per rectangle dim what lies outside a quarter circle (app.css
  // .tour-cap), so a lit part has the ring's rounded shape, not square corners beyond a rounded panel (critic, round 4).
  // A cap that would fall on another lit part is left out.
  function paintCaps(lit) {
    var list = [];
    lit.forEach(function (o, i) {
      var r = Math.floor(Math.min(o.rad, (o.r - o.l) / 2, (o.b - o.t) / 2));
      if (r < 2) return;
      [["tl", o.l, o.t], ["tr", o.r - r, o.t], ["bl", o.l, o.b - r], ["br", o.r - r, o.b - r]].forEach(function (c) {
        var hit = lit.some(function (w, j) { return j !== i && c[1] < w.r && c[1] + r > w.l && c[2] < w.b && c[2] + r > w.t; });
        if (!hit) list.push([c[0], c[1], c[2], r]);
      });
    });
    while (caps.length < list.length) {
      var n = el("div", "tour-cap");
      root.insertBefore(n, card);
      caps.push(n);
    }
    caps.forEach(function (n, k) {
      n.hidden = k >= list.length;
      if (n.hidden) return;
      n.className = "tour-cap " + list[k][0];
      n.style.setProperty("--cap-r", list[k][3] + "px");
      box(n, list[k][1], list[k][2], list[k][3], list[k][3]);
    });
  }

  // The window minus the lit rectangles, as rectangles: cut the window into horizontal bands at every lit top and
  // bottom, and in each band shade what no lit rectangle covers.
  function complement(lit, vw, vh) {
    var ys = [0, vh];
    lit.forEach(function (o) { ys.push(o.t, o.b); });
    ys = ys.filter(function (v, i, a) { return v >= 0 && v <= vh && a.indexOf(v) === i; }).sort(function (a, b) { return a - b; });
    var out = [];
    for (var i = 0; i + 1 < ys.length; i++) {
      var y0 = ys[i], y1 = ys[i + 1], x = 0;
      lit.filter(function (o) { return o.t <= y0 && o.b >= y1; })
        .sort(function (a, b) { return a.l - b.l; })
        .forEach(function (o) { if (o.l > x) out.push([x, y0, o.l - x, y1 - y0]); x = Math.max(x, o.r); });
      if (x < vw) out.push([x, y0, vw - x, y1 - y0]);
    }
    return out;
  }

  // One element of the pool per rectangle (more are made as needed), the rest hidden.
  function paint(pool, cls, rects) {
    while (pool.length < rects.length) {
      var n = el("div", cls);
      root.insertBefore(n, card);
      pool.push(n);
    }
    pool.forEach(function (n, i) {
      n.hidden = i >= rects.length;
      if (!n.hidden) box(n, rects[i][0], rects[i][1], rects[i][2], rects[i][3]);
    });
  }

  function box(node, x, y, w, h) {
    px(node, "left", x);
    px(node, "top", y);
    px(node, "width", Math.max(0, w));
    px(node, "height", Math.max(0, h));
  }

  function schedulePlace() {
    if (placeQueued) return;
    placeQueued = true;
    window.requestAnimationFrame(function () { placeQueued = false; place(); });
  }

  // **bold** and `code` in a paragraph become <strong> and <code>; everything else is a text node.
  function paragraph(text) {
    var p = document.createElement("p");
    text.split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach(function (part) {
      if (!part) return;
      var node = null;
      if (/^\*\*[^*]+\*\*$/.test(part)) { node = document.createElement("strong"); node.textContent = part.slice(2, -2); }
      else if (/^`[^`]+`$/.test(part)) { node = document.createElement("code"); node.textContent = part.slice(1, -1); }
      p.appendChild(node || document.createTextNode(part));
    });
    return p;
  }

  function render(step, s) {
    var n = STEPS.length;
    var title = typeof step.title === "function" ? step.title(s) : step.title;
    var where = typeof step.where === "function" ? step.where(s) : step.where;
    // The page or bar beside the count orients the owner in a 20-step walk (critic, round 1: steps were not grouped).
    ui.count.textContent = (where ? where + " · " : "") + (index + 1) + " of " + n;
    ui.fill.style.setProperty("width", ((index + 1) / n * 100).toFixed(2) + "%");
    ui.title.textContent = title;
    ui.body.replaceChildren.apply(ui.body, step.body(s).map(paragraph));
    ui.back.disabled = index === 0;
    ui.next.textContent = index === n - 1 ? "Done" : "Next";
    ui.live.textContent = "Step " + (index + 1) + " of " + n + (where ? ", " + where : "") + ": " + title;
  }

  // Close what the tour itself opened (the sheet of the decision step, the Commands or More menu).
  function closeOpened() {
    if (opened.sheet) { closeSheet(); opened.sheet = false; }
    if (opened.menu) { opened.menu.open = false; opened.menu = null; }
  }

  // Open the <details> menu holding an anchor, the one whose summary is on screen (Commands on a desktop, More on a
  // phone): a menu that only shows information, like a press on its summary.
  function openMenuWith(name) {
    var menus = document.querySelectorAll('[data-tour="' + name + '"]');
    for (var i = 0; i < menus.length; i++) {
      var host = menus[i].closest("details");
      if (host && laidOut(host.querySelector("summary"))) { host.open = true; opened.menu = host; return; }
    }
  }

  // On screen: laid out and not inside a closed <details>. Chromium keeps a closed <details>' content laid out (it
  // hides it with content-visibility), so its "Show all 24 symbols" table measured as visible (round 5: the freshness
  // card described a Needs attention list the page did not show).
  function shown(el) { return laidOut(el) && !el.closest("details:not([open])"); }

  // Whether a table in the spotlight is wider than its card (it scrolls sideways).
  function hasWideTable(el) {
    return !!el && Array.prototype.some.call(el.querySelectorAll(".table-wrap"), function (w) {
      return shown(w) && w.scrollWidth > w.clientWidth + 2;
    });
  }

  function go(i) {
    var my = ++token;
    index = Math.max(0, Math.min(i, STEPS.length - 1));
    var step = STEPS[index];
    closeOpened();
    if (step.enter) step.enter();
    card.classList.add("tour-wait");
    var deadline = performance.now() + APPEAR_MS;
    var wait = function () {
      if (my !== token || !isOpen) return;
      target = findTarget(stepNames(step));
      if (!target && performance.now() < deadline && !step.optional) { window.requestAnimationFrame(wait); return; }
      var sp = PHONE.matches && step.phonePad !== undefined ? step.phonePad : step.pad;
      pad = sp !== undefined ? sp : PAD;
      extra = target ? fitAlso(step) : [];
      if (!target && !step.optional && window.console) {
        // A step is a promise that what it describes is on screen: a renamed anchor must not pass quietly.
        console.warn("[tour] step " + (index + 1) + " (" + step.id + ") found no laid-out [data-tour] target " + stepNames(step).join(" or "));
      }
      var s = context();
      s.found = !!target;
      s.wide = hasWideTable(target);
      // A table on screen: a page with records, not its empty-state line (a closed <details> list does not count).
      s.table = !!target && Array.prototype.some.call(target.querySelectorAll("table"), shown);
      // SYNC WARNING: pages._page renders the Portfolio's positions section as id="positions" (a table once it holds any).
      s.positions = !!target && Array.prototype.some.call(target.querySelectorAll("#positions table"), shown);
      s.alsoAt = alsoAt();
      render(step, s);
      px(card, "width", cardWidth());
      // "The rest is further down" when the spotlight will be cut short, unless the step's copy already says where the
      // rest is (`more: false`).
      ui.more.hidden = true;
      s.cut = cutShort();
      render(step, s);  // again, now that the copy knows whether the spotlight is cut short (s.cut)
      ui.more.hidden = !(step.more !== false && s.cut);
      var y = target ? scrollFor(target) : null;
      if (y !== null) window.scrollTo({ top: y, behavior: REDUCE.matches ? "auto" : "smooth" });
      card.classList.remove("tour-wait");
      place();
      // Each card fades in where it lands (app.css .tour-in; none under reduced motion). The spotlight does not glide:
      // its pieces change in number from step to step. Reading offsetWidth restarts the animation.
      card.classList.remove("tour-in");
      void card.offsetWidth;
      card.classList.add("tour-in");
      var until = performance.now() + (REDUCE.matches ? 0 : SETTLE_MS);
      var track = function () {
        if (my !== token || !isOpen) return;
        place();
        if (performance.now() < until) window.requestAnimationFrame(track);
      };
      window.requestAnimationFrame(track);
      if (!card.contains(document.activeElement)) ui.next.focus({ preventScroll: true });
    };
    window.requestAnimationFrame(wait);
  }

  function next() { if (index >= STEPS.length - 1) close(); else go(index + 1); }
  function back() { if (index > 0) go(index - 1); }

  function onKey(e) {
    if (!isOpen) return;
    var k = e.key;
    if (k === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }
    if (k === "ArrowRight" || k === "ArrowLeft") {
      e.preventDefault();
      e.stopPropagation();
      if (!e.repeat) { if (k === "ArrowRight") next(); else back(); }
      return;
    }
    if (k === "Enter" || k === " ") {
      // The earlier engine advanced twice on Enter over a focused Back or Next (its window handler and the
      // button's own activation). Here a focused button handles its own key and the tour does not; Enter elsewhere in
      // the card means Next; a held key never repeats a step.
      if (e.repeat) { e.preventDefault(); return; }
      var onButton = e.target && e.target.closest && e.target.closest("button");
      if (!onButton && k === "Enter") { e.preventDefault(); next(); }
      return;
    }
    if (k === "Tab") {
      // Focus stays in the card (the page is inert anyway; this keeps it off the browser's own bar too).
      var f = Array.prototype.filter.call(card.querySelectorAll("button"), function (b) { return !b.disabled; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || !card.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  function onResize() {
    if (!isOpen) return;
    target = findTarget(stepNames(STEPS[index]));
    extra = target ? fitAlso(STEPS[index]) : [];
    schedulePlace();
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  }

  function build() {
    root = el("div", "tour-root");
    root.appendChild(el("div", "tour-block"));  // swallows every click on the page while the tour runs
    dim = el("div", "tour-dim");  // the whole window, while a step has nothing to light
    rings = [];   // .tour-hole: the accent ring around the lit target (made by paint())
    shades = [];  // .tour-shade: the dim around everything lit (made by paint())
    caps = [];    // .tour-cap: the rounded corners of everything lit (made by paintCaps())
    card = el("section", "tour-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "tour-title");
    card.setAttribute("aria-describedby", "tour-body");
    card.tabIndex = -1;
    var progress = el("div", "tour-progress");
    ui.fill = el("span");
    progress.appendChild(ui.fill);
    // The mark and the page-and-count share a line when they fit and wrap when they do not; the close button has its own
    // column (app.css .tour-head), so the fixture's long mark can never push it out of the card (critic, round 2).
    var head = el("div", "tour-head");
    var meta = el("div", "tour-meta");
    var mark = el("span", "tour-mark", document.body.classList.contains("db-fixture") ? "PAPER TRADING · FIXTURE DEMO" : "PAPER TRADING");
    ui.count = el("span", "tour-count");
    // Words, not a ×: the decision step opens a side sheet with its own × right above this card, and two identical
    // marks did two different things (critic, round 3: UX law 13).
    ui.x = el("button", "tour-x", "End tour");
    ui.x.type = "button";
    meta.appendChild(mark);
    meta.appendChild(ui.count);
    head.appendChild(meta);
    head.appendChild(ui.x);
    ui.title = el("h2", "tour-title");
    ui.title.id = "tour-title";
    ui.body = el("div", "tour-body");
    ui.body.id = "tour-body";
    var foot = el("div", "tour-foot");
    var keys = el("span", "tour-keys", "← → to move · Esc to close");
    keys.setAttribute("aria-hidden", "true");
    ui.back = el("button", "btn sm ghost tour-back", "Back");
    ui.back.type = "button";
    ui.next = el("button", "btn sm primary tour-next", "Next");
    ui.next.type = "button";
    foot.appendChild(keys);
    foot.appendChild(el("span", "spacer"));
    foot.appendChild(ui.back);
    foot.appendChild(ui.next);
    ui.live = el("p", "sr tour-live");
    ui.live.setAttribute("aria-live", "polite");
    card.appendChild(progress);
    card.appendChild(head);
    card.appendChild(ui.title);
    card.appendChild(ui.body);
    ui.more = el("p", "tour-more", "↓ The rest is further down.");
    ui.more.hidden = true;
    card.appendChild(ui.more);
    card.appendChild(foot);
    card.appendChild(ui.live);
    root.appendChild(dim);
    root.appendChild(card);
    ui.x.addEventListener("click", close);
    ui.back.addEventListener("click", back);
    ui.next.addEventListener("click", next);
  }

  function start() {
    if (isOpen) return;
    dismissHint();
    lastFocus = document.activeElement;
    // The page starts clear: an open side sheet or menu holds no input, so closing it loses nothing.
    closeSheet();
    Array.prototype.forEach.call(document.querySelectorAll("details.menu-host[open]"), function (d) { d.open = false; });
    build();
    document.body.appendChild(root);
    // Everything else on the page becomes inert: no focus, no press, no key reaches a form while the tour runs.
    Array.prototype.forEach.call(document.body.children, function (n) {
      if (n === root || n.tagName === "SCRIPT" || n.tagName === "TEMPLATE" || n.inert) return;
      n.inert = true;
      inerted.push(n);
    });
    document.body.classList.add("tour-open");
    isOpen = true;
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("scroll", schedulePlace, true);
    window.addEventListener("resize", onResize);
    go(0);
  }

  function close() {
    if (!isOpen) return;
    isOpen = false;
    token++;
    closeOpened();
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("scroll", schedulePlace, true);
    window.removeEventListener("resize", onResize);
    inerted.forEach(function (n) { n.inert = false; });
    inerted = [];
    document.body.classList.remove("tour-open");
    if (root) root.remove();
    root = dim = card = null;
    rings = [];
    shades = [];
    caps = [];
    ui = {};
    target = null;
    extra = [];
    // Back to the control that opened it (or wherever focus was), on the page the tour ended on.
    var backTo = laidOut(launch) ? launch : lastFocus;
    if (backTo && backTo.focus) backTo.focus();
  }

  // A layout switch (a window dragged across 760 px) changes the targets and the copy: re-enter the step.
  if (PHONE.addEventListener) PHONE.addEventListener("change", function () { if (isOpen) go(index); });

  // ----------------------------------------------------------------------------------------------------------------
  // The launch control and the one-time hint. The button is rendered hidden (no dead button without JavaScript) and
  // shown here; the hint shows until it is dismissed or the tour is taken, remembered in this browser.
  // SYNC WARNING: pages.header renders .tour-launch-wrap, #tour-launch, #tour-hint and .tour-hint-x.
  var HINT_KEY = "pa-tour-hint-dismissed";
  var wrap = launch.closest(".tour-launch-wrap");
  var hint = document.getElementById("tour-hint");
  function hintDismissed() { try { return window.localStorage.getItem(HINT_KEY) === "1"; } catch (err) { return false; } }
  function dismissHint() {
    try { window.localStorage.setItem(HINT_KEY, "1"); } catch (err) { /* storage off: hidden for this page only */ }
    if (hint) hint.hidden = true;
  }
  if (wrap) wrap.hidden = false;
  if (hint && !hintDismissed()) hint.hidden = false;
  var hintX = hint ? hint.querySelector(".tour-hint-x") : null;
  if (hintX) hintX.addEventListener("click", dismissHint);
  launch.addEventListener("click", start);
})();
