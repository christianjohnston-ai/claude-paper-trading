"""Dashboard HTTP behavior: renders both databases, refuses cross-site/forged requests, fixes form
errors in place with input preserved, confirms successful actions, and refuses changes mid-run."""
import html as htmllib
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from paper_advisor import clock, config, db, experiments, fixture_demo, runs, universe
from paper_advisor.dashboard import server


@pytest.fixture
def dash(app_paths):
    conn = db.init_db(app_paths.db_path, db_mode="forward")
    universe.install(conn, [i for i in universe.FORWARD_UNIVERSE if i.symbol in ("SPY", "MSFT")], now=clock.now_iso())
    exp_id, _ = experiments.ensure_default_experiment(conn, clock.now_utc())
    conn.close()
    fixture_demo.run_demo(app_paths, rebuild=True)
    app = server.App(app_paths, "forward", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    app.port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield app, f"http://127.0.0.1:{app.port}", exp_id
    httpd.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)  # zips are binary
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def post(base, path, data, origin=None):
    body = urllib.parse.urlencode(data).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + path, data=body, headers=headers, method="POST")
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_renders_real_and_fixture_views(dash):
    app, base, exp_id = dash
    s, html, hdr = get(base + "/")
    assert s == 200 and "PAPER TRADING" in html and "$500.00" in html and "FIXTURE DEMO" not in html
    assert "script-src 'self'" in hdr["Content-Security-Policy"] and hdr["X-Frame-Options"] == "DENY"
    s, html, _ = get(base + "/?db=fixture")
    assert s == 200 and "FIXTURE DEMO — SYNTHETIC DATA" in html and "DEMO.A" in html and "$504.70" in html


def test_refuses_foreign_host_and_forged_posts(dash):
    app, base, exp_id = dash
    assert get(base + "/", host="evil.example:80")[0] == 403
    assert post(base, "/actions/backup", {"csrf": "wrong"})[0] == 403
    assert post(base, "/actions/backup", {"csrf": app.csrf}, origin="http://evil.example")[0] == 403
    assert get(base + "/download/export/..%2F..%2Fdata%2Fpaper_advisor.sqlite")[0] == 404


def test_config_error_is_shown_in_place_with_input_kept(dash):
    app, base, exp_id = dash
    s, html, _ = post(base, "/actions/config", {"csrf": app.csrf, "max_position_pct": "1.7", "reason": "testing"})
    assert s == 400
    assert "Nothing was saved" in html and "has-error" in html and 'value="1.7"' in html and ">testing</textarea>" in html
    conn = db.open_existing(app.paths.db_path)
    assert conn.execute("SELECT COUNT(*) FROM config_versions WHERE experiment_id=?", (exp_id,)).fetchone()[0] == 1


def test_config_success_creates_prospective_version_and_confirms(dash):
    app, base, exp_id = dash
    s, _, hdr = post(base, "/actions/config", {"csrf": app.csrf, "max_position_pct": "0.20", "reason": "smaller positions"})
    assert s == 303
    s, html, _ = get(base + hdr["Location"])
    assert "Done." in html and "version 2" in html and "future decisions only" in html
    conn = db.open_existing(app.paths.db_path)
    _, cfg = config.current(conn, exp_id)
    assert str(cfg.max_position_pct) == "0.20"


def test_changes_refused_while_a_morning_run_holds_the_lock(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        runs.acquire_lock_tx(conn, exp_id, None, clock.now_utc(), 30)
    s, html, _ = post(base, "/actions/config", {"csrf": app.csrf, "max_open_positions": "4", "reason": "x"})
    assert s == 409 and "in progress" in html


def test_new_experiment_requires_confirmation_then_works(dash):
    app, base, exp_id = dash
    s, html, _ = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "1000"})
    assert s == 400 and "Tick the box" in html and 'value="1000"' in html
    s, _, hdr = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "1,000.00", "confirm": "yes"})
    assert s == 303
    conn = db.open_existing(app.paths.db_path)
    assert experiments.active_experiment_id(conn) != exp_id
    assert experiments.get(conn, exp_id)["status"] == "archived"


def test_export_and_backup_actions(dash):
    app, base, exp_id = dash
    s, _, hdr = post(base, "/actions/export", {"csrf": app.csrf})
    assert s == 303
    s, html, _ = get(base + hdr["Location"])
    assert "/download/export/export_" in html
    link = html.split("/download/export/")[1].split("'")[0]
    s, body, h2 = get(base + "/download/export/" + link)
    assert s == 200 and h2["Content-Type"] == "application/zip"
    s, _, hdr = post(base, "/actions/backup", {"csrf": app.csrf})
    assert s == 303
    assert "integrity check passed" in get(base + hdr["Location"])[1]


def test_fixture_view_uses_its_own_frozen_clock(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "Fixture clock frozen at" in fx
    # Judging the 2025 scenario against today's date marked every symbol stale and every identity
    # unverified (verification is valid for 14 days); the frozen clock removes both false alarms.
    assert "Data fresh: 0/" not in fx and "unverified" not in fx


def test_numbers_render_without_scientific_notation_or_signed_zero(dash):
    app, base, _ = dash
    # The random CSRF token is embedded in every form and can itself contain e.g. "4e-1" (about 1 page
    # load in 2000), which once failed this test spuriously. Pin a token that contains the pattern, then
    # strip it, so the check is about rendered numbers only and is deterministic.
    app.csrf = "tok4e-1tok"
    _, fx, _ = get(base + "/?db=fixture")
    fx = fx.replace(app.csrf, "")
    assert not re.search(r"\d[eE][+-]\d", fx)  # Decimal("100.0000").normalize() prints 1E+2
    assert '<td class="num">100.00</td>' in fx  # DEMO.SPY reference open, at least 2 decimals
    _, fw, _ = get(base + "/")
    assert "+0.00%" not in fw and "0.00%" in fw


def test_counts_are_pluralized_and_spy_gap_is_in_points(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "1 closed trades" not in fx and "1 closed trade" in fx
    assert " pts</span>" in fx


def test_checks_are_shown_in_plain_language_with_their_values(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "Within max position size" in fx and "Cash reserve kept after buys" in fx
    assert "position after the order" in fx and "$200.00" in fx  # drawer: observed vs limit in words
    assert ">max_position_pct<" not in fx and ">cash_reserve<" not in fx  # no raw keys as visible text
    assert "max_new_orders_per_session;" not in fx  # the stored rejection reason is translated in the drawer


def test_config_error_says_how_to_fix_it_next_to_the_field(dash):
    app, base, _ = dash
    s, html, _ = post(base, "/actions/config", {"csrf": app.csrf, "max_position_pct": "1.7", "reason": "testing"})
    assert s == 400 and "is out of range" not in html
    assert "Enter a fraction above 0 and at most 1" in html and "You entered 1.7" in html
    # The summary sits in the settings section beside the fields it refers to, and the section is
    # marked for the scroll (one class attribute: a duplicate attribute is ignored by browsers).
    assert html.index("Nothing was saved") > html.index('id="settings"')
    assert 'class="block solo focus-target" id="settings"' in html and 'id="settings" class=' not in html  # solo: Settings' only section


def test_success_notice_appears_where_the_action_was_taken(dash):
    app, base, _ = dash
    _, _, hdr = post(base, "/actions/backup", {"csrf": app.csrf})
    _, html, _ = get(base + hdr["Location"])
    assert html.index("Done.") > html.index('id="settings"')


def test_overview_puts_health_and_next_steps_above_the_tables(dash):
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    assert "Health at a glance" in fw and "What happens next" in fw
    assert fw.index("Health at a glance") < fw.index('id="positions"')
    assert 'rel="icon" href="data:,"' in fw  # no favicon request, so no 404 in the console


def test_a_run_in_progress_is_described_in_words_not_status_codes(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", "2026-10-26", version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "awaiting_decisions", clock.now_iso())
    _, fw, _ = get(base + "/")
    assert "awaiting_decisions" not in fw  # raw status codes are for the database, not the owner
    assert "waiting for the advisor's decisions" in fw
    assert "No decisions in this run" not in fw  # untrue while the run is still waiting for them


def test_decision_drawer_states_the_outcome_first(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    tpl = fx.split('<template id="dec-')[1:]
    rejected = next(t for t in tpl if "Decision: BUY DEMO.D" in t)
    assert rejected.index("Outcome") < rejected.index("Thesis")
    assert "Rejected by the validator: 3 checks failed; no order was placed" in rejected


def test_check_values_and_reconciliation_names_are_readable(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "verified at 2025-11-21T22:30:00.000000Z" not in fx and "verified at Fri Nov 21, 2:30 PM PT" in fx
    assert "COMMON_STOCK" not in fx
    assert ":PORTFOLIO" not in fx and "ledger replay (portfolio)" in fx
    assert "Mon Nov 24 open</td>" in fx  # tables use the short open label; the drawer keeps ET/PT times


def test_expired_order_outcome_is_explained_in_words(fixture_db):
    # Reuses the broker scenario where DEMO.A has no bar for its target session, so its order expires.
    from test_broker_sim import MON, settle, setup, submit
    from test_risk import d
    from paper_advisor.dashboard import pages
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", {k: v for k, v in MON.items() if k != "DEMO.A"})
    w.at("2026-10-27T13:00:00")
    settle(w)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    assert "no daily bar for the target session" in html
    assert "no_bar_for_target_session" not in html  # stored reason codes are translated for the owner


def _next_target():
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from paper_advisor import market_calendar as cal
    t = cal.next_session_after(clock.now_utc() + timedelta(seconds=120))
    cutoff = (t.open_utc - timedelta(seconds=120)).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%-I:%M %p PT")
    return t.date.isoformat(), cutoff


def test_failed_last_run_is_flagged_on_top_with_the_recovery_step(dash):
    app, base, exp_id = dash
    session, cutoff = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: network down")
    _, fw, _ = get(base + "/")
    header = fw.split("</header>")[0]
    assert f"Run #{rid} failed" in header  # in the always-visible status row, not only lower down
    assert "settles, reconciles and analyzes it" in fw and cutoff in fw  # how to recover (nothing submitted yet), and by when
    assert "Aborted: network down" in fw and "aborted: network down" not in fw


def test_run_in_progress_shows_age_lock_expiry_and_how_to_take_over(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "awaiting_decisions", clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    _, fw, _ = get(base + "/")
    assert "lock last renewed" in fw and ", expires " in fw and "started" in fw.split("</header>")[0]
    assert 'data-copy="/paper-morning force-recover"' in fw  # the recovery step as a copyable command
    assert f"Current run #{rid}" in fw and f"Last run #{rid}" not in fw


def test_drawer_and_benchmark_labels_keep_paper_and_fixture_identity(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    drawer = fx.split('<aside id="drawer"')[1].split("</aside>")[0]
    assert "PAPER TRADING" in drawer and "FIXTURE DEMO" in drawer  # the phone drawer covers the sticky strip
    assert "DEMO.SPY benchmark" in fx and ">SPY $" not in fx and "vs SPY" not in fx  # synthetic, not the real ticker


def test_splits_dividends_and_exit_reasons_are_itemized(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "2-for-1 split" in fx and "2.497502 → 4.995004 shares" in fx  # why Fills and Positions differ
    assert "Cash dividend" in fx and "+$0.59" in fx
    assert "Stop-loss: the Tue Nov 25 close ($46.50) was at or below the stop" in fx
    assert 'data-open="dec-' in fx  # a fill opens the decision behind it


def test_codes_and_free_text_booleans_are_gone(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert ">NO_TRADE<" not in fx and "close &lt;=" not in fx and "close &gt;=" not in fx
    assert "Over the max position size" in fx  # a failed check is worded as the failure it is
    _, fw, _ = get(base + "/")
    assert '<select id="cfg-require_exit_rule_on_buy"' in fw
    assert "at least 20 valued sessions and 10 closed trades" in fw


def test_not_initialized_page_offers_what_exists_instead_of_looping(app_paths):
    fixture_demo.run_demo(app_paths, rebuild=True)  # only the fixture DB exists
    app = server.App(app_paths, "forward", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s, html, _ = get(f"http://127.0.0.1:{app.port}/")
    httpd.shutdown()
    assert s == 404 and "Back to dashboard" not in html  # the link pointed back at this same page
    assert 'href="/?db=fixture"' in html


def test_not_initialized_page(app_paths):
    app = server.App(app_paths, "forward", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s, html, _ = get(f"http://127.0.0.1:{app.port}/")
    httpd.shutdown()
    assert s == 404 and "bin/pa init" in html and "Nothing was created" in html


def test_provisional_valuations_are_explained_in_words(fixture_db):
    from test_broker_sim import MON, settle, setup, submit
    from test_risk import d
    from paper_advisor.dashboard import pages
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.B", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", MON)
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {k: v for k, v in MON.items() if k != "DEMO.B"})  # DEMO.B: no bar on Tuesday
    w.at("2026-10-28T13:00:00")
    settle(w)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    assert "Provisional valuations" in html
    assert "DEMO.B had no bar that session; its latest earlier close was used" in html
    assert "stale_mark" not in html
    assert "filled 2.497502 shares at $40.04 at the Mon Oct 26 open" in html  # lifecycle reasons in words
    assert "accepted for the Mon Oct 26 open" in html and "market-on-open 2026" not in html


def test_fixture_panel_describes_this_database_not_a_fixed_story(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "5 morning runs and 1 review" in fx and "Nothing here will fill or change" in fx


def test_non_session_dates_render_as_words():
    from paper_advisor.dashboard import pages
    assert pages.et_open("2026-10-24") == "Sat Oct 24"  # a Saturday has no open; never fall back to ISO


def test_archived_experiment_view_says_what_the_commands_act_on(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    newer = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(),
                                          created_by="test")
    _, html, _ = get(base + f"/?exp={exp_id}")
    assert "This experiment is archived" in html and f"#{newer}" in html
    assert "type /paper-morning in Claude Code to run the first one" not in html  # that would act on the active one
    assert 'class="drawer-back"' in html  # a linked record can return to the one it came from


def _stale_scenario(fixture_db):
    from test_broker_sim import MON, settle, setup, submit
    from test_risk import d
    w = setup(fixture_db)
    submit(w, [d("BUY", "DEMO.A", notional_usd="100"), d("BUY", "DEMO.B", notional_usd="100")])
    w.at("2026-10-26T21:05:00")
    w.bars_all("2026-10-26", {k: v for k, v in MON.items() if k != "DEMO.A"})  # DEMO.A: no Monday bar -> expires
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", {k: v for k, v in MON.items() if k != "DEMO.B"})  # DEMO.B: no Tuesday bar -> stale
    w.at("2026-10-28T13:00:00")
    settle(w)
    return w


def test_headline_numbers_say_when_they_rest_on_a_stale_price(fixture_db):
    from paper_advisor.dashboard import pages
    w = _stale_scenario(fixture_db)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    kpis = html.split('aria-label="Account summary"')[1].split("</section>")[0]
    assert "Provisional: DEMO.B is priced at its Mon Oct 26 close (no newer bar)" in kpis
    health = html.split("Health at a glance")[1].split("</div>")[0]
    assert "1 provisional valuation" in health
    # Stale data comes with what happens next and what the owner can do (Rule 19), not just a flag.
    assert "The next /paper-morning run downloads prices again before anything else" in html
    assert "No DEMO.A bar was stored for Mon Oct 26" in html  # an expiry names the exact missing bar


def test_run_progress_is_amber_with_renewal_and_typical_length(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "awaiting_decisions", clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    _, fw, _ = get(base + "/")
    nxt = fw.split("What happens next")[1].split("</div>")[0]
    assert "attn-info" in nxt and "class='attn'" not in nxt  # a healthy run is not shown as an alarm
    assert "last renewed" in nxt and "no completed run yet to compare" in nxt


def test_decisions_trace_forward_to_their_exits_and_back(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    hold = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: HOLD DEMO.A" in t)
    assert "Later" in hold and "Stop-loss: the Tue Nov 25 close" in hold  # the HOLD's stop fired afterwards
    closed = fx.split("Closed round trips")[1].split("</table>")[0]
    assert 'data-detail="dec-' in closed  # a closed trade opens the decision that entered it


def test_internal_codes_are_shown_as_words(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "OK (FIXTURE)" not in fx and "VERIFIED within 14 days" not in fx
    # The raw version/hash may sit in a tooltip or a run drawer's collapsed, labeled provenance block (the only
    # way to read them on a phone), never in visible cell text.
    visible = re.sub(r"<details class='small provenance'>.*?</details>", "", fx, flags=re.S)
    assert fx.count("<details class='small provenance'>") >= 1
    assert ">1.0.0+" not in visible and "· commit " not in visible and "re-analysis v" not in visible


def test_notices_appear_in_the_panel_where_the_action_was_taken(dash):
    app, base, _ = dash
    _, _, hdr = post(base, "/actions/backup", {"csrf": app.csrf})
    assert hdr["Location"].endswith("#tools")
    _, html, _ = get(base + hdr["Location"])
    assert html.index("Done.") > html.index('id="tools"')
    s, html, _ = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "abc"})
    assert "fix Starting capital and the confirmation box" in html
    assert html.index("Nothing was saved") > html.index('id="newexp"')


def test_entry_decisions_show_how_their_round_trip_ended(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    buy = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: BUY DEMO.A" in t)
    assert "Round trip closed" in buy and "data-open=\"fill-" in buy  # the BUY leads to its exit fill...
    assert "open the decision that set the exit" in buy  # ...and to the HOLD that restated the stop


def test_capital_and_benchmark_text_before_anything_happens(dash):
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    assert 'data-for="nx-capital" data-lo="20.00"' in fw  # "abc" or $15 is flagged while typing, before the press
    assert "which returned n/a" not in fw and "benchmark starts at the" in fw


def test_run_identity_is_shown_in_words_with_the_raw_ids_one_hover_away(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        r1 = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso(),
                                 model_provider="anthropic/claude-code", model_id="claude-opus-5-5", model_effort="max")
        runs.set_status(conn, r1, "failed", clock.now_iso(), error="aborted: test")
        # A second run whose code differs (inserted directly: code identity is immutable once written).
        conn.execute("INSERT INTO runs(experiment_id, run_kind, analysis_session_date, version, mode, status, started_at, model_provider,"
                     " model_id, model_effort, code_version, code_tree_sha256) VALUES (?, 'morning', ?, 2, 'analysis', 'failed', ?,"
                     " 'anthropic/claude-code', 'claude-opus-5-5', 'max', '1.0.0+abcdef123456', ?)",
                     (exp_id, session, clock.now_iso(), "f" * 64))
    _, fw, _ = get(base + "/")
    assert "Claude Opus 5.5" in fw and "max effort" in fw
    assert ">claude-opus-5-5" not in fw and "model claude-opus-5-5" not in fw  # raw ids only in tooltips
    assert "· commit " not in fw
    assert f"build 2, changed since run #{r1}" in fw  # whether the advisor's code changed mid-experiment, in words


def test_save_is_gated_like_create_and_archiving_states_its_consequences(dash):
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    # Save waits for a change, valid values and a reason, and says what is missing (app.js reads these).
    assert 'id="cfg-save"' in fw and 'id="cfg-hint"' in fw and 'data-initial="0.25"' in fw
    # What archiving does to the current experiment, where the choice is made.
    assert "Archiving #1 pauses its settlement" in fw and "1 pending order" in fw
    assert "catches it up session by session" in fw


def test_hold_drawer_shows_what_it_replaced_and_links_back(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    hold = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: HOLD DEMO.A" in t)
    assert "Replaced" in hold and "set by" in hold and 'data-open="dec-1"' in hold


def test_benchmark_risk_and_cash_yield_are_part_of_the_comparison(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "benchmark max drawdown" in fx and "Keeping cash earns 0%" in fx


def test_provisional_reaches_drawdown_and_chart(fixture_db):
    from paper_advisor.dashboard import pages
    w = _stale_scenario(fixture_db)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    dd = html.split("Max drawdown")[1].split("</div></div>")[0]
    assert "provisional" in dd
    assert 'class="prov-dot"' in html  # the provisional session is marked on the equity curve


def test_mixed_code_versions_are_flagged_next_to_the_results(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        conn.execute("INSERT INTO runs(experiment_id, run_kind, analysis_session_date, version, mode, status, started_at,"
                     " code_version, code_tree_sha256) VALUES (?, 'morning', ?, 2, 'analysis', 'failed', ?, '1.0.1+abc', ?)",
                     (exp_id, session, clock.now_iso(), "e" * 64))
    _, fw, _ = get(base + "/")
    assert "code changed during this experiment (2 builds" in fw  # (the apostrophe in "advisor's" is HTML-escaped)


def test_new_experiment_minimum_comes_from_the_limits_it_will_start_with(dash):
    """Rule 19: an experiment too small to ever place a buy is flagged while the amount is typed (the field's
    floor comes from the limits it would start with) and refused by the server in place, entries kept."""
    app, base, _ = dash
    _, fw, _ = get(base + "/")
    newexp = fw.split('id="newexp"')[1].split("</form>")[0]
    assert 'data-lo="20.00"' in newexp and "At least $20.00" in newexp
    assert "could never place a buy" in newexp  # the reason is next to the field, not discovered later
    s, html, _ = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "15", "confirm": "yes"})
    assert s == 400 and "Use at least $20.00" in html and 'value="15"' in html
    s, _, _ = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "20", "confirm": "yes"})
    assert s == 303


def test_risk_settings_say_when_no_buy_could_pass(dash):
    """Limits that contradict each other at this account's size freeze new buys. The form carries what app.js
    needs to say so while typing, and the saved notice says it too (the server stays the authority)."""
    app, base, _ = dash
    _, fw, _ = get(base + "/")
    risk = fw.split('id="risk"')[1].split("</form>")[0]
    assert 'data-equity-cents="50000"' in risk and 'class="freeze-hint"' in risk
    _, _, hdr = post(base, "/actions/config", {"csrf": app.csrf, "min_order_usd": "200.00", "reason": "freeze new buys"})
    _, html, _ = get(base + hdr["Location"])
    notice = html.split('id="notice"')[1].split("</div>")[0]
    assert "no new buy can pass" in notice and "$800.00" in notice  # $200 / 25% = $800 > $500 equity


def test_run_times_are_absolute_and_kept_current_on_an_open_tab(dash):
    """A tab left open must not keep saying "just now": times are absolute too, app.js refreshes the relative
    ones, and the stuck and expired warnings appear by themselves when their time comes."""
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "awaiting_decisions", clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    _, fw, _ = get(base + "/")
    nxt = fw.split("<h3>What happens next</h3>")[1].split("</ul>")[0]
    assert "<time datetime=" in nxt and "data-ago" in nxt and " PT)" in nxt
    assert nxt.count("data-show-after=") == 2 and "may be stuck" in nxt and "reload this page" in nxt
    assert 'data-copy="/paper-morning force-recover"' in nxt  # the exact recovery command, copyable


def test_code_builds_are_numbered_so_results_can_be_attributed(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        r1 = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        conn.execute("INSERT INTO runs(experiment_id, run_kind, analysis_session_date, version, mode, status, started_at,"
                     " code_version, code_tree_sha256) VALUES (?, 'morning', ?, 2, 'analysis', 'failed', ?, '1.0.0+abc', ?)",
                     (exp_id, session, clock.now_iso(), "e" * 64))
    _, fw, _ = get(base + "/")
    table = fw.split('id="runs"')[1].split("</table>")[0]
    cells = re.sub(r"<[^>]+>", " ", re.sub(r'title="[^"]*"', "", table))  # what the owner reads, not the tooltips
    assert "build 1" in cells and "build 2" in cells and f"changed since #{r1}" in cells


def test_run_history_says_once_what_every_run_shares_and_shows_what_differs(dash):
    """Capture, round 14: eleven one-line columns (model, session and data on every row) made the table 1848 px wide,
    so the reason a run stopped and its report were a sideways scroll away. What every run shares is said once above
    the table, a column returns when rows differ, and the reason sits under the status badge."""
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        r1 = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso(),
                                 model_provider="anthropic/claude-code", model_id="claude-opus-5-5", model_effort="max")
        runs.set_status(conn, r1, "failed", clock.now_iso(), error="aborted: network down")
    _, fw, _ = get(base + "/")
    sec = fw.split('id="runs"')[1].split("</table>")[0]
    assert "<b>1 run</b>, failed" in sec and "for the <b>" in sec and "advisor <b>Claude Opus 5.5, max effort</b>" in sec
    assert '<th class="c-model">' not in sec and '<th class="c-data">' not in sec
    status = sec.split("<tbody>")[1].split("</td>")[1]  # the Status cell: the badge, then the reason under it
    assert "failed" in status and 'class="prob" title="Aborted: network down">Aborted: network down<' in status
    with db.transaction(conn):
        runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="analysis", now=clock.now_iso(),
                            model_provider="anthropic/claude-code", model_id="claude-sonnet-5-5", model_effort="high")
    _, fw, _ = get(base + "/")
    sec = fw.split('id="runs"')[1].split("</table>")[0]
    assert '<th class="c-model">Advisor</th>' in sec and "advisor <b>" not in sec  # the rows differ: the column returns
    assert "Claude Sonnet 5.5 · high effort" in sec and "Every run: for the <b>" in sec  # the open is still shared


def test_positions_say_their_exit_rules_in_two_short_lines(dash):
    # Capture, round 14: the exit-rule sentence wrapped to twelve lines in a 94 px column and pushed Thesis off the card.
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    cells = re.sub(r'title="[^"]*"', "", fx.split('id="positions"')[1].split("</table>")[0])
    assert "exit on a close at or below" not in cells and re.search(r'<span class="sub">stop \$\d+\.\d\d', cells)
    drawer = fx.split('<template id="pos-DEMO.B">')[1].split("</template>")[0]
    assert "exit on a close at or below" in drawer  # the full sentence stays in the position's record


def test_backups_and_exports_are_named_in_words(dash):
    # Critic, round 9: backups were listed by their stored codes ("pre-morning run 16", "dashboard backup") and the
    # export link was its raw file name. The file names stay available (tooltip, small print) for finding the file.
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    db.backup(conn, app.paths.backups_dir, reason="pre-morning run 16")
    s, _, hdr = post(base, "/actions/backup", {"csrf": app.csrf})
    _, html, _ = get(base + hdr["Location"])
    tools = html.split("Recent backups")[1].split("</ul>")[0]
    assert "before morning run #16 · " in tools and "made from this dashboard · " in tools and " MB · integrity ok" in tools
    assert "pre-morning run" not in tools and "dashboard backup" not in tools
    s, _, hdr = post(base, "/actions/export", {"csrf": app.csrf})
    _, html, _ = get(base + hdr["Location"])
    assert ">Download the export (zip)</a>" in html and "(file export_" in html


def test_a_report_says_it_is_a_snapshot_of_its_run(dash):
    # Critic, round 9: a report's figures read as current beside the live dashboard; it is the record of one run.
    _, base, _ = dash
    s, html, _ = get(base + "/report?run=5&db=fixture")
    assert s == 200
    snap = html.split('class="notice info report-snap"')[1].split("</div></div>")[0]
    assert "A snapshot from run #5" in snap and "never change" in snap and 'href="/?db=fixture#runs"' in snap
    assert html.index("report-snap") < html.index("report-doc")  # read before the figures


def test_each_chart_has_its_own_caption(dash):
    # Capture, round 14: the benchmark's note stayed under the drawdown chart.
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    eq_foot = fx.split('<div class="chart-foot" data-pane="eq">')[1].split("</div>")[0]
    dd_foot = fx.split('<div class="chart-foot" data-pane="dd">')[1].split("</div>")[0]
    assert "benchmark" in eq_foot and dd_foot.startswith("Drawdown:") and "benchmark" not in dd_foot


def test_the_phone_strip_carries_the_pills_warning_in_the_fixture_too(fixture_db):
    """Capture, round 14: the phone hides the top bar, and on the states fixture its strip read only "PAPER TRADING ·
    FIXTURE DEMO" while the pill's provisional valuation and stale data showed nowhere at the top of a phone."""
    from paper_advisor.dashboard import pages
    w = _stale_scenario(fixture_db)
    parts = lambda html: (html.split('class="health-pill')[1].split("</a>")[0], html.split('class="paper-strip"')[1].split("</div>")[0])
    pill, strip = parts(pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token"))
    assert "stopped (lock expired)" in pill  # the scenario's run never finished
    assert '>PAPER TRADING · FIXTURE DEMO<span class="strip-alert">' in strip and "stopped (lock expired)" in strip  # a chip, no "·"
    with db.transaction(fixture_db):
        for r in fixture_db.execute("SELECT id FROM runs WHERE experiment_id=? AND finished_at IS NULL", (w.exp_id,)).fetchall():
            runs.set_status(fixture_db, r["id"], "completed", clock.now_iso())
    pill, strip = parts(pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token"))
    assert "provisional valuation" in pill
    assert '>PAPER TRADING · FIXTURE DEMO<span class="strip-warn">' in strip and "provisional valuation" in strip


def test_settings_changes_mid_experiment_are_flagged_next_to_the_results(dash):
    app, base, _ = dash
    post(base, "/actions/config", {"csrf": app.csrf, "min_order_usd": "6.00", "reason": "test"})
    _, fw, _ = get(base + "/")
    note = fw.split('class="sample-note"')[1].split("</p>")[0]
    assert "risk settings changed during this experiment" in note and "version 2" in note


def test_return_tile_itself_says_it_is_too_early_to_judge(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    # Redesign: the verdict sits in the performance card's evidence meter, beside the return it qualifies.
    perf = fx.split('class="card perf"')[1].split('<div class="chart-head">')[0]
    assert "Too early to judge" in perf and "Valued sessions <b>6 of 20</b>" in perf and "Closed trades <b>1 of 10</b>" in perf


def test_provisional_last_point_is_labelled_on_the_chart_itself(fixture_db):
    from paper_advisor.dashboard import pages
    w = _stale_scenario(fixture_db)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    eq = html.split("<h3>Equity:")[1].split("Drawdown from peak")[0]
    assert ">provisional</tspan>" in eq  # the end label says so; a hover title is not readable on a phone
    assert "Hollow dots mark provisional valuations" in eq  # explained right under the chart it describes


def test_hold_drawer_names_the_decision_it_replaced(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    hold = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: HOLD DEMO.A" in t)
    replaced = re.sub(r"<[^>]+>", "", hold.split("<dt>Replaced</dt><dd>")[1].split("</dd>")[0])  # the visible text
    assert "set by the BUY decided Fri Nov 21 in run #1" in replaced and "decision #" not in replaced


def test_rule_exit_wording_matches_what_has_happened(dash):
    from paper_advisor.dashboard import pages
    note = "STOP_LOSS: close 46.5000 on 2025-11-25 vs rule [stop 6.0% (close <= 47.05); max 30 sessions]"
    assert pages.exit_words(note).endswith("; sold at the next open")
    assert pages.exit_words(note, tail="sells").endswith("; sells at the next open")  # still pending
    assert pages.exit_words(note, tail=None).endswith("in force")  # the caller says what became of it
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    buy = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: HOLD DEMO.A" in t)
    later = buy.split("<dt>Later</dt><dd>")[1].split("</dd>")[0]
    assert later.count("sold") == 1


def test_zero_counts_and_empty_history_read_naturally(dash):
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    assert "None yet: no session has been valued" in fw
    assert "0 open positions" not in fw and "0 pending orders" not in fw
    # What archiving means for the archived experiment's own record, said where the choice is made.
    assert "still count in #1" in fw


def test_fixture_identity_checks_say_how_the_instrument_was_verified(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "against official listings" not in fx
    assert "synthetic fixture instrument" in fx


def test_not_initialized_page_offers_the_fixture_demo_to_look_around(app_paths):
    app = server.App(app_paths, "forward", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    s, html, _ = get(f"http://127.0.0.1:{app.port}/")
    httpd.shutdown()
    assert s == 404 and 'data-copy="paper-advisor/bin/pa demo"' in html


def test_tool_notices_name_the_file_without_repeating_the_folder(dash):
    app, base, _ = dash
    _, _, hdr = post(base, "/actions/backup", {"csrf": app.csrf})
    _, html, _ = get(base + hdr["Location"])
    notice = html.split('id="notice"')[1].split("</div>")[0]
    assert "in the backups folder listed below" in notice and "code class='path'" not in notice


def test_pending_orders_and_runs_open_their_full_record(dash):
    """A fresh forward account's only rows are the benchmark's pending order and run history; both open a
    drawer like every other table (every aggregate drills down), with the run log in words."""
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.log(conn, rid, "started", f"morning run v1 (analysis) for session {session}")
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: network down")
        runs.log(conn, rid, "aborted", "network down")
    _, fw, _ = get(base + "/")
    pend = fw.split('id="pending"')[1].split("</table>")[0]
    found = re.search(r'data-detail="(pend-\d+)"', pend)
    assert found and f'<template id="{found.group(1)}">' in fw
    table = fw.split('id="runs"')[1].split("</table>")[0]
    assert f'data-detail="run-{rid}"' in table
    tpl = fw.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    assert "Aborted: network down" in tpl and "Started: morning analysis" in tpl and "v1 (analysis)" not in tpl


def test_every_decision_link_has_a_drawer_even_past_the_journal(dash, monkeypatch):
    """Decision drawers are rendered with the journal's latest runs; a position held longer than that kept a
    dead "open the decision" link. Every linked decision must have its drawer."""
    from paper_advisor.dashboard import pages
    monkeypatch.setattr(pages, "JOURNAL_RUNS", 1)
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    linked = set(re.findall(r'data-(?:open|detail)="(dec-\d+)"', fx))
    have = set(re.findall(r'<template id="(dec-\d+)">', fx))
    assert linked and linked <= have, sorted(linked - have)


def test_settings_strategy_and_hypothesis_history_is_shown_with_reasons(dash):
    """Every save needs a reason and makes a new version, but the reason was stored and never shown. The
    history lists each version with what changed and why, the strategy versions, and the dated hypotheses
    from reviews (each opening its full record: falsification test, minimum sample, proposed change)."""
    app, base, _ = dash
    post(base, "/actions/config", {"csrf": app.csrf, "min_order_usd": "6.00", "reason": "raise the floor"})
    _, fw, _ = get(base + "/")
    hist = fw.split('id="history"')[1].split("</section>")[0]
    assert "raise the floor" in hist and "Minimum order (USD): 5.00 → 6.00" in hist and "initial configuration" in hist
    assert "Evidence-gated discretionary advisor v1" in hist and "No hypotheses yet" in hist
    _, fx, _ = get(base + "/?db=fixture")
    hx = fx.split('id="history"')[1].split("</section>")[0]
    assert 'data-detail="hyp-1"' in hx and '<template id="hyp-1">' in fx
    tpl = fx.split('<template id="hyp-1">')[1].split("</template>")[0]
    assert "Falsification test" in tpl and "30 entries" in tpl and "observation only" in tpl
    assert 'href="/report?run=6&amp;db=fixture"' in tpl  # the review that recorded it is one click away


def test_an_archived_experiment_that_never_ran_says_so_instead_of_pending(dash):
    app, base, _ = dash
    conn = db.open_existing(app.paths.db_path)
    newer = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    experiments.set_active(conn, 1)
    _, html, _ = get(base + f"/?exp={newer}")
    status = html.split('class="status-row"')[1].split("</div>")[0]
    assert "Never reconciled" in status
    assert "Not reconciled yet" not in html  # the health card says the same, not "pending"


def test_recent_backups_say_how_many_exist_and_all_are_reachable(dash):
    app, base, _ = dash
    post(base, "/actions/backup", {"csrf": app.csrf})
    _, html, _ = get(base + "/")
    assert "Recent backups (1 of 1):" in html
    for _ in range(5):
        post(base, "/actions/backup", {"csrf": app.csrf})
    _, html, _ = get(base + "/")
    assert "Recent backups (5 of 6):" in html and "<summary>1 older backup</summary>" in html  # not the 5 again


def test_typed_value_problems_sit_directly_under_their_field(dash):
    """Rule 19: the live meaning or problem of a typed value is on the line under the box, not at the end of
    the field's help text (where a long help paragraph pushed it out of sight)."""
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    newexp = fw.split('id="newexp"')[1].split("</form>")[0]
    assert newexp.index('data-for="nx-capital"') < newexp.index("At least $20.00")
    risk = fw.split('id="risk"')[1].split("</form>")[0]
    assert risk.index('data-for="cfg-max_position_pct"') < risk.index("Includes pending buys and the new order.")


def test_benchmark_is_described_from_its_actual_open_after_a_requeue(fixture_db):
    """If the benchmark's first open cannot fill it, it moves to the next open (broker_sim): the page must say
    when it really bought (or will buy), not the original first eligible open."""
    from paper_advisor.dashboard import pages
    from test_broker_sim import MON, settle, setup
    w = setup(fixture_db)
    w.at("2026-10-31T23:00:00")  # Monday never got data and its wait window is over; Tuesday has none yet
    settle(w)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    assert "starts at the Tue Oct 27 open" in html  # the re-queued order, not Mon Oct 26
    pend = next(t for t in html.split('<template id="pend-')[1:] if "BUY DEMO.SPY" in t)
    assert "moves to the next open" in pend
    w.at("2026-10-27T21:05:00")
    w.bars_all("2026-10-27", MON)
    w.at("2026-11-02T15:00:00")
    settle(w)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    note = html.split("Drawdown from peak")[1]
    assert "bought DEMO.SPY at the Tue Oct 27 open" in note and "its first eligible open, the Mon Oct 26 open, could not fill it" in note


def test_costs_paid_are_shown_with_the_pnl(dash):
    """Returns are net of costs, but "net of costs" alone hid how much they were: show slippage and commission."""
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    tile = fx.split('kpi-label">Total P&amp;L')[1].split("</div></div>")[0]  # the Total P&L card (redesign)
    assert "costs included: slippage about $" in tile and "commission $0.00" in tile
    _, fw, _ = get(base + "/")
    assert "costs included" not in fw  # no fills yet: "$0.00 of costs" is noise, not information


def test_positions_say_how_far_the_last_close_is_from_the_stop(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    pos = fx.split('id="positions"')[1].split("</table>")[0]
    # (close - stop) / close is how far the close can fall before the stop: say it with the close as the base.
    assert re.search(r"stop \d+\.\d% below the last close", pos) and "above the stop" not in pos


def test_an_archived_experiments_failed_run_is_history_not_a_call_to_action(dash):
    """/paper-morning acts on the ACTIVE experiment, so "run /paper-morning again" on an archived experiment's
    failed run sends the owner to retry something that would run a different experiment."""
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: test")
    experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, html, _ = get(base + f"/?exp={exp_id}")
    top = html.split("</header>")[0] + html.split("</header>")[1].split("<main>")[0]
    strip = html.split('class="paper-strip"')[1].split("</div>")[0]
    assert "run /paper-morning again" not in top and "failed" not in strip  # no call to action, no alert strip
    assert f"Last run #{rid} failed" in top  # still visible, as history
    health = html.split("Health at a glance")[1].split("</div>")[0]
    run_item = next(li for li in health.split("<li>") if f"Last run #{rid}" in li)
    assert "check bad" not in run_item and "check neu" in run_item  # the health card agrees: history, not an alarm (redesign glyphs)


def test_creating_an_experiment_backs_up_first_and_names_the_open_in_words(dash):
    app, base, _ = dash
    _, _, hdr = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "1000", "confirm": "yes"})
    _, html, _ = get(base + hdr["Location"])
    notice = html.split('id="notice"')[1].split("</div>")[0]
    assert re.search(r"First investable open: the \w{3} \w{3} \d{1,2} open", notice) and not re.search(r"\d{4}-\d{2}-\d{2}", notice)
    assert "before creating experiment #2" in html  # the archived experiment's last state is in a backup


def test_a_failed_runs_free_text_reason_is_not_wrapped_in_parentheses(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: feed down (retrying later)")
    _, fw, _ = get(base + "/")
    assert f"<strong>Run #{rid} failed.</strong> Aborted: feed down (retrying later)." in fw


def test_an_archived_view_locks_both_forms_and_drops_the_saving_promise(dash):
    """POST actions act on the ACTIVE experiment, so the new-experiment form on an archived experiment's page
    would copy the active one's limits under a floor computed from the archived one's. Lock it like the risk form."""
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, html, _ = get(base + f"/?exp={exp_id}")
    newexp = html.split('id="newexp"')[1].split("</form>")[0]
    assert 'id="nx-capital"' in newexp and re.search(r'id="nx-capital"[^>]* disabled', newexp)
    assert "Switch to the active experiment, #2, to start a new one" in newexp
    assert "pauses its settlement" not in newexp  # consequences of an action that is locked here are noise
    risk = html.split('id="risk"')[1].split("</form>")[0]
    assert "Saving creates version" not in risk  # nothing can be saved from this view


def test_make_active_is_locked_during_a_run(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    second = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    session, _ = _next_target()
    with db.transaction(conn):
        rid = runs.create_run_row(conn, second, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, second, rid, clock.now_utc(), 180)
    _, html, _ = get(base + "/")
    table = html.split('id="experiments"')[1].split("</table>")[0]
    assert re.search(r'Make active</button>', table) and re.search(r'<button type="submit" class="secondary"[^>]* disabled>Make active', table)
    assert f"Run #{rid} is in progress" in html.split('id="experiments"')[1].split("<table")[0]
    _, _, hdr = post(base, "/actions/activate", {"csrf": app.csrf, "id": str(exp_id)})
    assert experiments.active_experiment_id(conn) == second  # refused by the server too
    _, html, _ = get(base + hdr["Location"])
    assert "Not changed" in html.split('id="notice"')[1].split("</div>")[0]


def test_freeze_warning_sits_at_its_cause_with_one_press_fixes(dash):
    """Rule 19: the warning is on the field that caused it (not ~600px away by Save) and offers the computed fixes."""
    app, base, _ = dash
    post(base, "/actions/config", {"csrf": app.csrf, "min_order_usd": "200.00", "reason": "freeze"})
    _, html, _ = get(base + "/")
    risk = html.split('id="risk"')[1].split("</form>")[0]
    field = risk.split('id="cfg-min_order_usd"')[1].split('<div class="field')[0]
    assert "no new buy can pass" in field
    assert 'data-set="cfg-min_order_usd" data-value="125.00"' in field and 'data-set="cfg-max_position_pct" data-value="0.40"' in field


def test_settings_refusal_during_a_run_carries_the_recovery_command(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    _, fw, _ = get(base + "/")
    reason = fw.split('id="risk"')[1].split('class="disabled-reason"')[1].split("</p>")[0]
    assert 'data-copy="/paper-morning force-recover"' in reason and "lock expires" in reason
    nxt = fw.split("<h3>What happens next</h3>")[1].split("</ul>")[0]
    assert "--force-recover" not in nxt  # one way to say it: the copyable command
    strip = fw.split('class="paper-strip"')[1].split("</div>")[0]
    assert f"run #{rid} in progress" in strip  # the phone's sticky strip keeps the state once scrolled


def test_archived_view_puts_the_switch_beside_each_lock(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    second = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, html, _ = get(base + f"/?exp={exp_id}")
    risk_reason = html.split('id="risk"')[1].split('class="disabled-reason"')[1].split("</p>")[0]
    assert f'form="activate-{exp_id}"' in risk_reason and f"Make #{exp_id} active" in risk_reason
    nx_reason = html.split('id="newexp"')[1].split('class="disabled-reason"')[1].split("</p>")[0]
    assert f'href="/?db=forward&amp;exp={second}#newexp"' in nx_reason
    assert f'<form id="activate-{exp_id}"' in html
    exps = html.split('id="experiments"')[1].split("</table>")[0]
    assert f"Archiving #{second} pauses its settlement" in exps  # what Make active does, said at the button
    strip = html.split('class="paper-strip"')[1].split("</div>")[0]
    assert f"archived #{exp_id}" in strip
    pend = html.split('id="pending"')[1].split("</table>")[0]
    assert "paused while archived" in pend


def test_unfillable_rules_say_how_the_benchmark_differs(dash, fixture_db):
    from paper_advisor.dashboard import pages
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    pend = fw.split('<template id="pend-')[1].split("</template>")[0]
    assert "Advisor orders expire instead" in pend
    w = _stale_scenario(fixture_db)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    assert "good for one open only" in html  # the expired DEMO.A entry says why it was not carried forward


def test_check_details_are_words_not_notation(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "&lt;= 0.08" not in fx and "allowed <strong>at most 8.0%</strong>" in fx
    assert "stop/target/max-holding" not in fx and "COMMON_STOCK" not in fx and "unleveraged" in fx


def test_done_notice_command_is_copyable_and_positions_count_sessions_since_entry(dash):
    app, base, _ = dash
    _, _, hdr = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "1000", "confirm": "yes"})
    _, html, _ = get(base + hdr["Location"])
    assert 'data-copy="/paper-morning"' in html.split('id="notice"')[1].split("</div>")[0]
    _, fx, _ = get(base + "/?db=fixture")
    assert "Sessions since entry" in fx and "Sessions held" not in fx


def test_status_chips_and_strategy_rows_drill_down(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: test")
    _, fw, _ = get(base + "/")
    header = fw.split("</header>")[0]
    assert f'data-open="run-{rid}"' in header and 'href="#health"' in header
    assert 'class="nav-item" href="#activity" data-view="activity"' in header  # pages replaced the section strip (redesign)
    assert 'data-detail="strat-' in fw and '<template id="strat-' in fw


def test_report_page_reads_as_a_document_with_provenance_collapsed(dash):
    """The run report was raw markdown with ISO timestamps, internal codes and raw model/code ids on a human page."""
    _, base, _ = dash
    s, html, _ = get(base + "/report?run=5&db=fixture")
    assert s == 200 and "<table" in html and "<h2" in html and "PAPER TRADING" in html
    visible = re.sub(r"<details.*?</details>", "", html, flags=re.S)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", visible)  # times in words, never ISO stamps
    assert "fixture-script" not in visible and "<summary>Provenance (raw identifiers)</summary>" in html


def test_archived_pending_orders_and_fixture_locks_say_what_unlocks_them(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, html, _ = get(base + f"/?exp={exp_id}")
    pend = html.split('<template id="pend-')[1].split("</template>")[0]
    assert "paused while this experiment is archived" in pend
    _, fx, _ = get(base + "/?db=fixture")
    risk_lock = fx.split('id="risk"')[1].split('class="disabled-reason"')[1].split("</p>")[0]
    assert 'data-copy="paper-advisor/bin/pa demo --rebuild"' in risk_lock
    nx_lock = fx.split('id="newexp"')[1].split('class="disabled-reason"')[1].split("</p>")[0]
    assert 'href="/?db=forward#newexp"' in nx_lock


def test_save_and_create_hints_are_announced_and_every_passed_check_says_what_it_checked(dash):
    _, base, _ = dash
    _, fw, _ = get(base + "/")
    assert 'id="cfg-save" aria-describedby="cfg-diff cfg-hint"' in fw and 'id="cfg-diff"' in fw and 'id="nx-create" aria-describedby="nx-hint"' in fw
    _, fx, _ = get(base + "/?db=fixture")
    hold = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: HOLD DEMO.A" in t)
    item = hold.split("Exit rule within allowed ranges")[1].split("</li>")[0]
    assert 'class="ck-note"' in item


def test_run_durations_agree_with_the_clock_times_shown(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now="2026-10-24T11:13:55.000000Z")
        runs.set_status(conn, rid, "failed", "2026-10-24T11:20:16.000000Z", error="aborted: test")
    _, fw, _ = get(base + "/")
    tpl = fw.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    assert "(after 6 min 21 s)" in tpl


def test_report_tables_fit_and_its_sentences_are_words(dash):
    _, base, _ = dash
    _, html, _ = get(base + "/report?run=5&db=fixture")
    doc = html.split("report-doc")[1]
    assert 'class="grid-table compact report-table"' in doc and '<td class="wrap">' not in doc  # columns size to content
    assert "1 closed trade:" in doc and "&lt; 252" not in doc and "of the 252 sessions needed" in doc
    assert "Keeping the $500.00 in cash" in doc  # the experiment's own capital, not a hard-coded $500


def test_experiments_lock_carries_its_fix_and_wording_has_no_stray_punctuation(dash):
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    second = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    session, _ = _next_target()
    with db.transaction(conn):
        rid = runs.create_run_row(conn, second, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, second, rid, clock.now_utc(), 180)
    _, html, _ = get(base + "/")
    exps = html.split('id="experiments"')[1].split("</table>")[0]
    assert 'id="exps-lock"' in exps and 'data-copy="/paper-morning force-recover"' in exps
    assert 'aria-describedby="exps-lock" disabled' in exps
    _, archived, _ = get(base + f"/?exp={exp_id}")
    assert "ARCHIVED (read-only)" in archived.split("</header>")[0]
    _, fx, _ = get(base + "/?db=fixture")
    assert "</span>, or" not in fx  # a comma crowding the Copy button
    hold = next(t for t in fx.split('<template id="dec-')[1:] if "Decision: HOLD DEMO.A" in t)
    assert "). · <a" not in hold  # no stray period before a link


def test_report_account_table_headers_are_short_and_archived_panel_offers_the_switch(dash):
    app, base, exp_id = dash
    _, html, _ = get(base + "/report?run=5&db=fixture")
    doc = html.split("report-doc")[1]
    assert "<th>Gap vs DEMO.SPY</th>" in doc and "Return since inception" not in doc  # the fixture's own benchmark
    conn = db.open_existing(app.paths.db_path)
    second = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, arch, _ = get(base + f"/?exp={exp_id}")
    panel = arch.split("This experiment is archived")[1].split("</div>")[0]
    assert f'form="activate-{exp_id}"' in panel and f"Make #{exp_id} active" in panel
    session, _ = _next_target()
    with db.transaction(conn):
        rid = runs.create_run_row(conn, second, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, second, rid, clock.now_utc(), 180)
    _, arch, _ = get(base + f"/?exp={exp_id}")
    panel = arch.split("This experiment is archived")[1].split("</div>")[0]
    assert "disabled" in panel and f"while run #{rid} is in progress" in panel


def test_an_advisor_orders_why_is_its_thesis_not_a_status_code(fixture_db):
    from paper_advisor.dashboard import pages
    from test_broker_sim import setup, submit
    from test_risk import d as dec
    w = setup(fixture_db)
    submit(w, [dec("BUY", "DEMO.A", notional_usd="100", thesis="QA: pullback to support with breadth improving")])
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    pend = html.split('id="pending"')[1].split("</table>")[0]
    assert "pullback to support" in pend and "market-on-open 2026" not in pend
    drawer = next(t for t in html.split('<template id="pend-')[1:] if "BUY DEMO.A" in t)
    assert "pullback to support" in drawer and "2026-10-26" not in drawer


def test_fixture_report_page_and_files_are_labelled_and_served_from_their_folder(dash):
    app, base, _ = dash
    _, html, _ = get(base + "/report?run=5&db=fixture")
    top = html.split("</header>")[0] + html.split("</header>")[1][:1200]
    assert "FIXTURE DEMO" in top and "SYNTHETIC DATA" in top  # the page itself says so, not only the colour
    _, _, hdr = post(base, "/actions/export?db=fixture", {"csrf": app.csrf})
    _, page, _ = get(base + hdr["Location"])
    name = re.search(r"/download/export/(export_fixture_\w+\.zip)", page).group(1)
    s, _, _ = get(base + f"/download/export/{name}")
    assert s == 200
    folders = page.split('id="tools"')[1].split("Recent backups")[0]
    assert "exports/fixture/" in folders and "backups/fixture/" in folders  # the folders shown are the fixture's own


def test_every_session_and_a_reviews_evaluation_open_in_place(dash):
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    tpl = re.search(r'<template id="valuations"[^>]*>(.*?)</template>', fx, re.S).group(1)
    assert tpl.count("<tr") >= 7  # header + one row per valued session (6)
    kpis = fx.split('aria-label="Account summary"')[1].split("</section>")[0]
    assert 'data-open="valuations"' in kpis
    review = next(t for t in fx.split('<template id="run-')[1:] if "review" in t.split("</h2>")[1][:400])
    assert "Observations" in review and 'data-open="hyp-1"' in review


def test_strategy_rules_and_stale_warnings_are_words_with_their_remedy(dash, fixture_db):
    from paper_advisor.dashboard import pages
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    strat = fx.split('<template id="strat-')[1].split("</template>")[0]
    assert "MOO_NEXT_ELIGIBLE_SESSION" not in strat and "market-on-open at the next eligible session" in strat
    assert "FORWARD_UNIVERSE" not in strat
    w = _stale_scenario(fixture_db)
    html = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    flag = html.split('class="k-flag"')[1].split("</div>")[0]
    assert "clears when" in flag


def test_during_a_run_the_morning_command_says_to_wait(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    _, fw, _ = get(base + "/")
    cmds = fw.split("<ul class='cmds'>")[1].split("</ul>")[0]
    assert f"run #{rid} is in progress" in cmds.split("/paper-status")[0]
    drawer = fw.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    assert "written when the run finishes" in drawer  # not a bare "—" for the report of a run still in progress


def test_round_eleven_wording(dash):
    app, base, exp_id = dash
    _, fx, _ = get(base + "/?db=fixture")
    fills = fx.split('id="trades"')[1].split("</table>")[0]
    assert re.search(r">\d+\.\d{4}<", fills) and not re.search(r">\d+\.\d{3}<", fills)  # fill prices to 4 decimals
    assert "with its observed value and limit where it has one" in fx
    _, _, hdr = post(base, "/actions/backup", {"csrf": app.csrf})
    _, page, _ = get(base + hdr["Location"])
    notice = page.split('id="notice"')[1].split("</div>")[0]
    assert notice.index("backups folder listed below") < notice.index("paper_advisor-")  # the place before the file name
    conn = db.open_existing(app.paths.db_path)
    experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, arch, _ = get(base + f"/?exp={exp_id}")
    cmp = arch.split('<div class="compare">')[1].split('<div class="compare-gap">')[0]  # the comparison bars (redesign)
    assert "Keeping cash" in cmp and "0.00%" in cmp and "archived before its first eligible open" in cmp


def _start_run(app, exp_id):
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    return conn, rid


def test_an_open_tab_can_learn_that_a_run_started_or_finished(dash):
    # app.js polls this, so a tab opened before a run started holds Save before it is pressed, and a tab whose save
    # was refused mid-run refreshes when the run ends (Rule 19 #4, #5). Same lock rule as the forms' own refusal.
    app, base, exp_id = dash
    s, txt, hdr = get(base + "/api/state")
    assert s == 200 and hdr["Content-Type"].startswith("application/json")
    assert json.loads(txt) == {"active_id": exp_id, "locked": False, "lock_run_id": None, "last_run_id": None}
    conn, rid = _start_run(app, exp_id)
    assert json.loads(get(base + "/api/state")[1]) == {"active_id": exp_id, "locked": True, "lock_run_id": rid, "last_run_id": rid}
    with db.transaction(conn):  # an expired lock does not count: the next run takes it over
        conn.execute("UPDATE locks SET expires_at='2000-01-01T00:00:00Z'")
    assert json.loads(get(base + "/api/state")[1])["locked"] is False
    assert get(base + "/api/state", host="evil.example:80")[0] == 403


def test_a_save_refused_mid_run_keeps_its_entries_and_says_what_happens_next(dash):
    # The refusal is the response to a POST: the page carries its own GET address (app.js puts it in the address bar,
    # so no reload re-sends the form) and names the form whose entries app.js keeps across the reloads that follow.
    app, base, exp_id = dash
    _, page, _ = get(base + "/")
    tag = page.split("<body")[1].split(">")[0]
    assert f'data-self="/?db=forward&amp;exp={exp_id}"' in tag and f'data-active="{exp_id}"' in tag and f'data-exp="{exp_id}"' in tag
    assert 'data-state-url="/api/state?db=forward"' in tag and "data-kept" not in tag
    state = json.loads(htmllib.unescape(re.search(r'data-state="([^"]*)"', tag).group(1)))
    assert state == {"active_id": exp_id, "locked": False, "lock_run_id": None, "last_run_id": None}
    _, rid = _start_run(app, exp_id)
    s, page, _ = post(base, "/actions/config", {"csrf": app.csrf, "min_order_usd": "6.50", "reason": "QA check"})
    tag = page.split("<body")[1].split(">")[0]
    assert s == 409 and 'data-kept="risk"' in tag and 'value="6.50"' in page and ">QA check</textarea>" in page
    banner = page.split('id="notice"')[1].split("</div>")[0]  # one message in the form, not a notice plus a banner
    assert f"run #{rid} is in progress" in banner and "refreshes when the run ends" in banner and 'class="error-banner"' not in page
    s, page, _ = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "1000", "confirm": "yes"})
    assert 'data-kept="newexp"' in page.split("<body")[1].split(">")[0] and 'value="1000"' in page
    banner = page.split('id="notice"')[1].split("</div>")[0]
    assert f"run #{rid} is in progress" in banner and "refreshes when the run ends" in banner
    _, fx, _ = get(base + "/?db=fixture")
    assert "data-state-url" not in fx.split("<body")[1].split(">")[0]  # the fixture's runs are scripted: nothing to poll


def test_records_say_what_they_are_round_twelve(dash):
    # Round-12 review of the round-11 frames: each record says what it is in words that are true for it.
    app, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    runs_tpl = [t.split("</template>")[0] for t in fx.split('<template id="run-')[1:]]
    review = next(t for t in runs_tpl if "review run" in t)
    assert "a review places no orders" in review and "orders target" not in review  # a review places none
    assert "in under a second" in fx and "after 0 min 0 s" not in fx
    strat = htmllib.unescape(fx.split('<template id="strat-')[1].split("</template>")[0])
    assert "the fixture's synthetic universe (7 DEMO symbols" in strat and "list of" not in strat
    assert "none (no trade is ever required)" in strat
    assert '<template id="valuations" data-wide>' in fx  # nine columns: a wider drawer instead of sideways scrolling
    pos = fx.split('<template id="pos-DEMO.B">')[1].split("</template>")[0]
    assert "of equity" in pos and "below the last close" in pos  # value, weight and stop distance in the record
    fills = [t.split("</template>")[0] for t in fx.split('<template id="fill-')[1:]]
    held = next(t for t in fills if "Fill: BUY DEMO.B" in t)
    assert "still open: the position is held" in held and 'data-open="pos-DEMO.B"' in held
    assert "closed at the" in next(t for t in fills if "Fill: BUY DEMO.A" in t)
    assert re.search(r"Checked [^<]+ outside a morning run \(a stand-alone check\)", fx.split('id="health"')[1])  # the demo's last check
    _, rep, _ = get(base + "/report?db=fixture&run=5")
    assert "Gap vs DEMO.SPY" in rep and "Gap vs SPY" not in rep  # the fixture's benchmark is synthetic, never "SPY"
    assert "before the open" in rep and "pre open" not in rep and "(attempt 1)" not in rep
    assert "<td>-1.66 pts</td>" in rep  # a gap between returns is in points, as the line under the table says
    assert "<title>PAPER TRADING · FIXTURE DEMO · Report, run 5</title>" in rep and "(synthetic data) · Morning report, run 5" in rep
    val = re.search(r'<template id="valuations"[^>]*>(.*?)</template>', fx, re.S).group(1)
    heads = re.findall(r"<th[^>]*>([^<]+)</th>", val.split("</thead>")[0])
    assert heads[:4] == ["Session", "Return", "DEMO.SPY", "Gap"]  # the decision columns fit on a phone's first screen
    jr = fx.split('id="journal"')[1].split('id="rejected"')[0]
    assert jr.count('class="grid-table journal-table"') == jr.count("<table")  # one column layout for every run's table
    s, _, hdr = post(base, "/actions/new-experiment", {"csrf": app.csrf, "capital": "30", "confirm": "yes"})
    assert s == 303
    _, page, _ = get(base + hdr["Location"])
    assert "<title>PAPER TRADING · Forward paper experiment ($30)</title>" in page  # like "($500)", not "($30.00)"


def test_money_tiles_drill_down_to_the_rows_that_add_up_to_them(dash):
    # Critic round 8 (C): Cash, Invested and Total P&L had no drill-down, unlike Return and Drawdown.
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    tiles = fx.split('aria-label="Account summary"')[1].split("</section>")[0]
    assert 'data-open="cash"' in tiles and 'data-open="pnl"' in tiles and 'href="#positions"' in tiles and 'href="#pending"' in tiles
    cash = htmllib.unescape(re.search(r'<template id="cash"[^>]*>(.*?)</template>', fx, re.S).group(1))
    assert "Starting capital" in cash
    balances = re.findall(r'class="num cash-run[^"]*">([^<]+)<', cash)
    assert balances and balances[-1] == "$200.29"  # the running balance ends at the Cash tile
    assert 'data-open="fill-' in cash  # each fill row opens its fill
    pnl = htmllib.unescape(re.search(r'<template id="pnl"[^>]*>(.*?)</template>', fx, re.S).group(1))
    assert "-$10.30" in pnl and "+$14.41" in pnl and "$0.59" in pnl and "+$4.70" in pnl  # realized + unrealized + dividends = total
    assert "add up to" in pnl and "+ +" not in pnl and '<template id="cash" data-wide>' in fx  # four columns: a wide drawer
    fills = [t.split("</template>")[0] for t in fx.split('<template id="fill-')[1:]]
    assert "open the entry fill" in next(t for t in fills if "Fill: SELL DEMO.A" in t)  # the exit leads back to its entry


def test_round_thirteen_next_steps_links_and_wording(dash, fixture_db):
    from paper_advisor.dashboard import pages
    app, base, exp_id = dash
    _, fx, _ = get(base + "/?db=fixture")
    fills = [t.split("</template>")[0] for t in fx.split('<template id="fill-')[1:]]
    closed = next(t for t in fills if "Fill: BUY DEMO.A" in t)
    assert "open the exit fill" in closed and 'data-open="fill-' in closed.split("Round trip")[1]  # no dead end (C)
    assert re.search(r"Fixture clock frozen at \w{3} \w{3} \d+, 2025", fx)  # the frozen clock names its year
    assert "&gt;=" not in fx  # scripted hypothesis text in words
    review = next(t.split("</template>")[0] for t in fx.split('<template id="run-')[1:] if "review run" in t.split("</template>")[0])
    assert "not refreshed (a review reads the record)" in review
    # Forward: a failed latest run's drawer offers the command that follows it; a running one offers recovery (E).
    conn, rid = _start_run(app, exp_id)
    _, fw, _ = get(base + "/")
    drawer = fw.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    assert "force-recover" in drawer and "lock expires" in drawer
    assert "numbered across all experiments" not in fw  # no other experiment has runs yet: nothing is missing
    with db.transaction(conn):
        conn.execute("DELETE FROM locks")
        runs.set_status(conn, rid, "failed", clock.now_iso())
    _, fw, _ = get(base + "/")
    drawer = fw.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    assert 'data-copy="/paper-morning"' in drawer  # what to run next, copyable
    # Make active states what archiving the active experiment does, before the press (B, E).
    second = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, arch, _ = get(base + f"/?exp={exp_id}")
    panel = arch.split("This experiment is archived")[1].split("</div>")[0]
    assert f"still count in #{second}'s record" in panel and "pending order" in panel
    table_note = arch.split('id="experiments"')[1].split("<table")[0]
    assert f"still count in #{second}'s record" in table_note
    exps = arch.split('id="experiments"')[1].split("</table>")[0]
    assert "<th class=\"num\">Return</th>" in exps  # each experiment's result, to compare them (decision 5)
    # The archived panel's lock offers the recovery command too (E).
    with db.transaction(conn):
        rid2 = runs.create_run_row(conn, second, "morning", _next_target()[0], version=1, mode="analysis", now=clock.now_iso())
        runs.acquire_lock_tx(conn, second, rid2, clock.now_utc(), 180)
    _, arch, _ = get(base + f"/?exp={exp_id}")
    panel = arch.split("This experiment is archived")[1].split("</div>")[0]
    assert "force-recover" in panel
    assert "numbered across all experiments" in arch.split('id="runs"')[1]  # run #rid2 belongs to another experiment
    # The database engine's known issue: in Health at a glance, with the upgrade copyable (E).
    if db.engine_issues():
        assert 'data-copy="brew upgrade sqlite"' in arch and "SQLite" in arch.split("Health at a glance")[1].split("</div></div>")[0]
    # Forward wording of the stale-price remedy offers /paper-morning to copy.
    w = _stale_scenario(fixture_db)
    page = pages.page(fixture_db, w.exp_id, clock.now_utc(), "forward", "token")
    flag = page.split('class="k-flag"')[1].split("</div>")[0]
    assert 'data-copy="/paper-morning"' in flag
    # The name field says what an empty name becomes.
    _, fw, _ = get(base + f"/?exp={second}")
    assert "named after its capital" in fw.split('id="newexp"')[1].split("</form>")[0]


def test_a_run_whose_lock_expired_is_named_with_what_takes_it_over(dash):
    # Critic round 8 asked for the lock-expired screen: once the lock expired, the page said nothing about the run.
    app, base, exp_id = dash
    conn, rid = _start_run(app, exp_id)
    with db.transaction(conn):
        conn.execute("UPDATE locks SET expires_at='2000-01-01T00:00:00.000000Z'")
    _, fw, _ = get(base + "/")
    strip = fw.split('class="status-row"')[1].split("</div>")[0] if 'class="status-row"' in fw else fw.split("</header>")[0]
    assert f"Run #{rid} stopped (lock expired)" in strip
    nxt = fw.split("What happens next")[1].split("</ul>")[0]
    assert f"Run #{rid} stopped without finishing" in nxt and 'data-copy="/paper-morning"' in nxt
    card = fw.split("Health at a glance")[1].split("</ul>")[0]
    assert f"Last run #{rid}: stopped (lock expired)" in card
    assert 'data-refresh' not in fw.split("<body")[1].split(">")[0]  # no run holds a live lock: no minute reload
    assert f"run #{rid} stopped" in fw.split('class="paper-strip"')[1].split("</div>")[0]  # the phone strip says it too
    drawer = fw.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    assert "stopped (lock expired)" in drawer and "none: the run stopped before writing one" in drawer
    assert "waiting for the advisor" not in drawer  # the row and drawer agree with the status row


def test_e2_wording_links_and_report_sentences(dash, fixture_db):
    from paper_advisor.dashboard import pages
    from test_risk import d as dec_, submit, world_with_fresh_data
    _, base, _ = dash
    run = {"run_kind": "morning", "mode": "analysis", "supersedes_run_id": None, "version": 1}
    assert pages._run_event_words(run, {"kind": "submitted", "message": "1 decisions"}) == "Decisions submitted (1 decision)"
    _, rep, _ = get(base + "/report?db=fixture&run=1")
    doc = htmllib.unescape(rep.split("report-doc")[1])
    assert "<strong>BUY DEMO.A.</strong> Counterargument:" in doc and " - counterargument" not in doc
    assert "No session has been valued yet." in doc and "Valued through n/a" not in doc
    assert "REJECTED: Too many orders for the session, Over the max position size" in doc  # the dashboard's words, not check names
    pend = doc.split("Pending orders")[1].split("</ul>")[0]
    assert "passive benchmark BUY DEMO.SPY" in pend  # the benchmark's pending buy is part of the record too
    w = world_with_fresh_data(fixture_db)
    submit(w, [dec_("BUY", "DEMO.A", notional_usd="100")])
    page = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    drawer = next(t.split("</template>")[0] for t in page.split('<template id="dec-')[1:] if "Decision: BUY DEMO.A" in t)
    assert 'data-open="pend-' in drawer and "open the order" in drawer  # a pending decision opens its order
    run_tpl = next(t.split("</template>")[0] for t in page.split('<template id="run-')[1:])
    assert 'href="#journal"' in run_tpl


def test_typical_run_length_counts_analysis_runs_only(dash):
    # A reconcile-only rerun takes seconds: counted in, the median read "completed runs have taken about 0 min".
    from datetime import timedelta
    from paper_advisor.dashboard import pages
    app, _, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    session, _ = _next_target()
    t0 = clock.now_utc()
    with db.transaction(conn):
        a = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.iso(t0))
        runs.set_status(conn, a, "completed", clock.iso(t0 + timedelta(seconds=30)))
        r = runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="reconcile_only", now=clock.iso(t0))
        runs.set_status(conn, r, "completed", clock.iso(t0 + timedelta(seconds=1)))
    assert 0.45 < pages._median_run_minutes(conn, exp_id) < 0.55


def test_a_takeover_is_named_on_both_runs(dash):
    # Reviewer, round 13: the interrupted run said "recovered by a later run" and the run that took it over said nothing.
    # Written as morning.start writes a takeover (status, error text, "recovered" event, and the new run's "started" event).
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    session, _ = _next_target()
    with db.transaction(conn):
        a = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, a, "interrupted", clock.now_iso(),
                        error="interrupted before finish (before decisions were submitted); recovered by a later run")
        runs.log(conn, a, "recovered", "marked interrupted by a later run", {"had_submission": False}, clock.now_iso())
        b = runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="analysis", now=clock.now_iso())
        runs.log(conn, b, "started", f"morning run v2 (analysis) for session {session}",
                 {"recovered": [{"run_id": a, "had_submission": False}], "previous_lock": None}, clock.now_iso())
    _, fw, _ = get(base + "/")
    da = fw.split(f'<template id="run-{a}">')[1].split("</template>")[0]
    assert f"taken over by run #{b}" in da and f"Marked interrupted by run #{b}" in da and f'data-open="run-{b}"' in da
    db_ = fw.split(f'<template id="run-{b}">')[1].split("</template>")[0]
    assert f'data-open="run-{a}"' in db_ and f"took over run #{a}" in db_
    jr = fw.split('id="journal"')[1].split('id="rejected"')[0]
    assert f"<p class='small warn-text'>Interrupted before finish (before decisions were submitted); taken over by run #{b}</p>" in jr


def test_report_signs_profit_and_loss_like_the_dashboard(dash):
    # Capture, round 13: the report's account row read "-$10.30" realized next to "$14.41" unrealized while the dashboard
    # says "+$14.41"; a gain must not look like a plain amount in one place and a signed one in the other.
    _, base, _ = dash
    _, html, _ = get(base + "/report?run=5&db=fixture")
    acct = html.split("<h3>Account</h3>")[1].split("</table>")[0]
    row = dict(zip(re.findall(r"<th>(.*?)</th>", acct), re.findall(r"<td>(.*?)</td>", acct)))
    for k in ("Realized", "Unrealized", "Dividends"):
        assert re.fullmatch(r"[+-]\$[\d,]+\.\d\d|\$0\.00", row[k]), (k, row[k])
    assert row["Realized"] == "-$10.30" and row["Unrealized"] == "+$14.41" and row["Dividends"] == "+$0.59"


def test_positions_row_shows_the_average_cost_its_drawer_and_stop_use(dash):
    # Capture, round 13: the row said 25.03 next to "$23.02 (8.0% stop)", which comes from the 25.025 entry; 8% below 25.03
    # is 23.03, so the row read as an arithmetic error. The row now carries the drawer's (and the fills') precision.
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    rows = re.findall(r'<tr data-detail="pos-([\w.]+)" tabindex="0">(.*?)</tr>', fx)
    assert len(rows) >= 2
    for sym, row in rows:
        avg = re.search(r'avg cost (\$[\d.]+)<', row).group(1)  # under the shares (round 14)
        drawer = fx.split(f'<template id="pos-{sym}">')[1].split("</template>")[0]
        assert avg == drawer.split("<dt>Average cost</dt><dd>")[1].split("</dd>")[0], sym
    assert "avg cost $25.025<" in dict(rows)["DEMO.F"]


def test_a_dividend_line_names_its_date_once(dash):
    # Capture, round 13: "Wed Nov 26: Cash dividend: ... ex-date Wed Nov 26 (+$0.59)". broker_sim credits a dividend on its
    # ex-date, and both places that list one (a position's drawer, the Ex-date column) already show that date.
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    assert "credited on the ex-date" in fx
    assert not re.search(r"Cash dividend:[^<]*ex-date \w{3} ", fx)


def _submitted_run(conn, exp_id, session, version, now):
    """A completed analysis run with a stored submission (what morning.start counts when it picks its mode)."""
    rid = runs.create_run_row(conn, exp_id, "morning", session, version=version, mode="analysis", now=now)
    conn.execute("INSERT INTO run_submissions(run_id, submitted_at, payload_json, payload_sha256, market_view, what_changed, "
                 "model_id, model_effort) VALUES (?, ?, '{}', 'x', 'test', '', 'test-model', 'unknown')", (rid, now))
    runs.set_status(conn, rid, "completed", now)
    return rid


def test_a_retry_after_a_failed_run_is_described_as_morning_start_will_run_it(dash):
    # Capture, round 13: after run #14 (a deliberate re-analysis) failed, What happens next said "Nothing was submitted for
    # the Mon Oct 26 open, so running /paper-morning again starts a fresh analysis", though runs #10 and #13 had submitted
    # for that open. morning.start picks reconcile-only when ANY run for the session submitted, not just the failed one.
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    now = clock.now_iso()
    with db.transaction(conn):
        done = _submitted_run(conn, exp_id, session, 1, now)
        f = runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="analysis", now=now, supersedes_run_id=done)
        runs.set_status(conn, f, "failed", now, error="aborted: test")
    _, fw, _ = get(base + "/")
    nxt = htmllib.unescape(fw.split("What happens next")[1].split("</ul>")[0])
    assert "Nothing was submitted" not in nxt and "fresh analysis" not in nxt
    assert f"Run #{done}'s decisions for the" in nxt and "only settles, reconciles and reports" in nxt
    assert 'data-copy="/paper-morning new-analysis"' in fw.split("What happens next")[1].split("</ul>")[0]
    drawer = htmllib.unescape(fw.split(f'<template id="run-{f}">')[1].split("</template>")[0])
    assert "fresh analysis" not in drawer and "only settles, reconciles and reports" in drawer


def test_a_stopped_run_is_named_one_way_and_its_takeover_says_what_will_run(dash):
    # Capture, round 13: three names for one state ("stopped: its lock expired", "stopped (lock expired)", "stopped: lock
    # expired"), a claim the dashboard cannot know ("the session running it has ended"), "analyse", the drawer's Status
    # and What now rows saying the same thing, and a takeover promised to analyze when a submission already existed.
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        done = _submitted_run(conn, exp_id, session, 1, clock.now_iso())
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="analysis", now=clock.now_iso(),
                                  supersedes_run_id=done)
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
        conn.execute("UPDATE locks SET expires_at='2000-01-01T00:00:00.000000Z'")
    _, fw, _ = get(base + "/")
    txt = htmllib.unescape(fw)
    assert txt.count("stopped (lock expired)") >= 3 and "stopped: its lock expired" not in txt and "stopped: lock expired" not in txt
    assert "has ended" not in txt.split("<template")[0].split("What happens next")[1] and "analyse" not in txt
    nxt = txt.split("What happens next")[1].split("</ul>")[0]
    assert f"takes it over: it marks run #{rid} interrupted" in nxt and f"Run #{done}'s decisions for the" in nxt
    drawer = txt.split(f'<template id="run-{rid}">')[1].split("</template>")[0]
    status = drawer.split("<dt>Status</dt><dd>")[1].split("</dd>")[0]
    assert "takes it over" not in status and "before it finished" in status  # the problem, not the next step
    assert "takes it over" in drawer.split("<dt>What now</dt><dd>")[1].split("</dd>")[0]


def test_attempt_numbers_count_every_morning_run_for_the_open(dash):
    # Capture, round 13: Run history read "attempt 9", a rerun with no number, then "attempt 11" (the rerun is attempt 10).
    from paper_advisor.dashboard import pages
    rerun = {"run_kind": "morning", "mode": "reconcile_only", "supersedes_run_id": None, "version": 10}
    assert pages.run_kind_words(rerun) == "morning rerun (reconcile only, attempt 10)"


def test_dividends_carry_their_sign_wherever_pnl_is_added_up(dash):
    # Capture, round 13: after the report signed dividends ("+$0.59"), the dashboard's own sum still read "Realized -$10.30,
    # unrealized +$14.41 and dividends $0.59 add up to +$4.70": one part of the same sentence unsigned.
    _, base, _ = dash
    _, fx, _ = get(base + "/?db=fixture")
    pnl = fx.split('<template id="pnl">')[1].split("</template>")[0]
    assert 'dividends <strong><span class="pos">+$0.59</span></strong> add up to' in pnl
    tile = fx.split('kpi-label">Total P&amp;L')[1].split("</div></div>")[0]  # the Total P&L card (redesign)
    assert 'dividends <span class="pos">+$0.59</span>' in tile


def test_the_command_list_says_when_a_plain_morning_run_only_reconciles(dash):
    # Capture, round 13: under a box saying a plain /paper-morning "does not analyze again", the command list still read
    # "each trading morning: settle, research, decide (or hold cash)".
    app, base, exp_id = dash
    _, fw, _ = get(base + "/")
    assert "only reconciles and reports" not in fw.split("class='cmds'")[1].split("</ul>")[0]  # nothing decided yet
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        done = _submitted_run(conn, exp_id, session, 1, clock.now_iso())
    _, fw, _ = get(base + "/")
    cmds = htmllib.unescape(fw.split("class='cmds'")[1].split("</ul>")[0])
    assert f"it only reconciles and reports (run #{done}'s decisions for the" in cmds


def _takeover(conn, exp_id, session, prev_expires_at):
    """Run a stopped and run b taking it over, written as morning.start writes them, with the lock b replaced."""
    now = clock.now_iso()
    a = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=now)
    runs.set_status(conn, a, "interrupted", now,
                    error="interrupted before finish (before decisions were submitted); recovered by a later run")
    runs.log(conn, a, "recovered", "marked interrupted by a later run", {"had_submission": False}, now)
    b = runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="analysis", now=now)
    lock = {"run_id": a, "expires_at": prev_expires_at, "heartbeat_at": "2026-10-24T16:12:40.000000Z", "name": f"experiment:{exp_id}"}
    runs.log(conn, b, "started", f"morning run v2 (analysis) for session {session}",
             {"recovered": [{"run_id": a, "had_submission": False}], "previous_lock": lock}, now)
    return a, b


def test_an_interrupted_run_keeps_saying_why_after_the_takeover(dash):
    # Capture, round 13: once run #16 took over run #15, nothing said that #15's lock had expired at 9:22 (the whole reason it
    # was interrupted); run #16's row said only "it had stopped without finishing". morning.start records the lock it replaced.
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        a, b = _takeover(conn, exp_id, session, "2000-01-01T16:22:02.000000Z")  # expired before b started
    _, fw, _ = get(base + "/")
    txt = htmllib.unescape(fw)
    when = "Sat Jan 1, 8:22 AM PT"
    da = txt.split(f'<template id="run-{a}">')[1].split("</template>")[0]
    assert f"its lock expired at {when} without being renewed, and run #{b} took it over" in da
    db_ = txt.split(f'<template id="run-{b}">')[1].split("</template>")[0]
    assert f"its lock had expired at {when} without being renewed; this run marked it interrupted" in db_
    jr = txt.split('id="journal"')[1].split('id="rejected"')[0]
    assert f"its lock expired at {when} without being renewed, and run #{b} took it over" in jr


def test_a_stopped_runs_table_line_leads_with_its_cause(dash):
    # Capture, round 14: the table repeated the sheet's sentence ("Interrupted before finish (before decisions were
    # submitted); its lock expired at ... without being renewed, and run #N took it over") clamped to two lines, which
    # cut the cause. The table leads with it; the tooltip and the sheet keep the whole sentence.
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        a, b = _takeover(conn, exp_id, session, "2000-01-01T16:22:02.000000Z")
    _, fw, _ = get(base + "/")
    row = htmllib.unescape(fw).split('id="runs"')[1].split(f'data-detail="run-{a}"')[1].split("</tr>")[0]
    shown = re.sub(r'title="[^"]*"', "", row)
    assert "Jan\xa01,\xa08:22\xa0AM\xa0PT" in shown  # one unbreakable time (a phone broke "7:12 PM / PT")
    shown = shown.replace("\xa0", " ")
    assert f"Lock expired Jan 1, 8:22 AM PT without renewal; run #{b} took it over" in shown  # another day: with its date
    assert "Interrupted before finish" not in shown and f"and run #{b} took it over" in row  # the tooltip keeps the sentence


def test_the_failed_run_pill_says_what_a_retry_would_do(dash):
    # Capture, round 14: after a failed re-analysis the pill said "run /paper-morning again" while the card beside it
    # said a plain /paper-morning would only reconcile, because an earlier run's decisions for the open were recorded.
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        first = runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())
        runs.set_status(conn, first, "failed", clock.now_iso(), error="aborted: test")
    pill = lambda: htmllib.unescape(get(base + "/")[1].split('class="health-pill')[1].split("</a>")[0])
    assert f"Run #{first} failed: run /paper-morning again" in pill()  # nothing recorded for the open: retry analyzes
    with db.transaction(conn):
        done = _submitted_run(conn, exp_id, session, 2, clock.now_iso())
        rid = runs.create_run_row(conn, exp_id, "morning", session, version=3, mode="analysis", now=clock.now_iso(),
                                  supersedes_run_id=done)
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: test")
    assert f"Run #{rid} failed: run #{done}'s decisions stand" in pill() and "again" not in pill()


def test_a_force_recovered_run_says_its_lock_was_still_held(dash):
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        a, b = _takeover(conn, exp_id, session, "2999-01-01T00:00:00.000000Z")  # still held when b started
    _, fw, _ = get(base + "/")
    da = htmllib.unescape(fw).split(f'<template id="run-{a}">')[1].split("</template>")[0]
    assert f"run #{b} took it over with force-recover while its lock was still held" in da


def test_one_decision_is_it_and_a_report_check_is_not_an_advisory_input(dash, fixture_db):
    # Capture, round 13: "1 decision recorded · see them in the Advisor journal"; the report data check listed under
    # "advisory inputs" although it checks the report after the decisions.
    from paper_advisor.dashboard import pages
    from test_risk import d as dec_, submit, world_with_fresh_data
    w = world_with_fresh_data(fixture_db)
    submit(w, [dec_("BUY", "DEMO.A", notional_usd="100")])
    page = pages.page(fixture_db, w.exp_id, clock.now_utc(), "fixture", "token")
    run_tpl = next(t.split("</template>")[0] for t in page.split('<template id="run-')[1:])
    assert "1 decision recorded · " in run_tpl and "see it in the Advisor journal" in run_tpl
    _, _, detail = pages.skills_summary([{"skill_name": "market-breadth-analyzer", "status": "ok"},
                                         {"skill_name": "data-quality-checker", "status": "ok"}])
    assert detail == "advisory inputs: market breadth; checks: report data check"


def test_the_journal_numbers_a_rerun_like_run_history(dash):
    # Capture, round 13: the journal headed run #10 "attempt 9", run #11 (a reconcile-only rerun) with no number, run #12
    # "attempt 11"; Run history now numbers the rerun, and the journal must agree.
    app, base, exp_id = dash
    session, _ = _next_target()
    conn = db.open_existing(app.paths.db_path)
    with db.transaction(conn):
        _submitted_run(conn, exp_id, session, 1, clock.now_iso())
        rr = runs.create_run_row(conn, exp_id, "morning", session, version=2, mode="reconcile_only", now=clock.now_iso())
        runs.set_status(conn, rr, "completed", clock.now_iso())
    _, fw, _ = get(base + "/")
    card = fw.split('id="journal"')[1].split(f"Run #{rr}</strong>" if f"Run #{rr}</strong>" in fw else f"Run #{rr}")[1].split("</article>")[0]
    assert " · attempt 2 · " in card


def test_make_active_says_which_experiment_it_archived(dash):
    # Capture, round 13: "Done. Experiment #5 is now active" said nothing of #1, which the press had just archived; the
    # Create notice already says "#1 is archived with its history intact".
    app, base, exp_id = dash
    conn = db.open_existing(app.paths.db_path)
    second = experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    _, _, hdr = post(base, "/actions/activate", {"csrf": app.csrf, "id": str(exp_id)})
    assert experiments.active_experiment_id(conn) == exp_id
    _, html, _ = get(base + hdr["Location"])
    notice = htmllib.unescape(html.split('id="notice"')[1].split("</div>")[0])
    assert f"Experiment #{exp_id} is now active; #{second} is archived with its history intact" in notice


# SYNC WARNING: the page names and the sections each holds are mirrored by app.js (the router opens the page that
# holds a linked id).
PAGES = [("home", []), ("portfolio", ["positions", "pending"]), ("decisions", ["journal", "rejected"]),
         ("activity", ["trades"]), ("runs", ["runs"]), ("health", ["health"]), ("settings", ["settings", "risk", "newexp", "experiments"])]


def test_the_sidebar_names_seven_pages_and_every_section_lives_in_one(dash):
    # Owner, after round 13: "this ui is not intuitive" — one 6,800 px page of twelve sections behind an anchor bar.
    # Seven pages in a sidebar, each holding the sections it is about; every existing anchor still reaches its section.
    _, base, _ = dash
    for url in (base + "/", base + "/?db=fixture"):
        _, page, _ = get(url)
        side = page.split('<aside class="sidebar"')[1].split("</aside>")[0]
        assert re.findall(r'class="nav-item[^"]*"[^>]*data-view="(\w+)"', side) == [p for p, _ in PAGES]
        assert "PAPER TRADING" in page.split('<header class="topbar"')[1].split("</header>")[0]
        main = page.split('<main class="page"')[1].split("</main>")[0]
        views = re.split(r'<section class="view"', main)[1:]
        assert [re.search(r'data-view="(\w+)"', v).group(1) for v in views] == [p for p, _ in PAGES]
        for (name, ids), v in zip(PAGES, views):
            for sid in ids:
                assert f'id="{sid}"' in v, (name, sid)
