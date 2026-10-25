"""The dashboard's guided tour (static/tour.js): every spot it points at exists in every database state, it stays
read-only, its launch control is on every dashboard page (desktop sidebar, phone app bar) and nowhere else, and
serving it changed neither the static whitelist's strictness nor the Content-Security-Policy.

SYNC WARNING: this file is the enforcement half of the tour tripwire (README "Dashboard", the header of
static/tour.js, the SYNC WARNING beside the data-tour anchors in pages.py and the bridge in app.js). It reads
static/tour.js as TEXT: keep each step's `id` first and its `target` / `phoneTarget` as double-quoted literals (one
string, or a list of strings whose last one exists in every state), and keep the steps between `var STEPS = [` and
`// END STEPS`.
"""
import json
import re
import threading
import urllib.error
import urllib.request
from html import unescape
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from paper_advisor import clock, db, experiments, fixture_demo, runs, universe
from paper_advisor.dashboard import pages, server
from paper_advisor.paths import AppPaths

STATIC = Path(server.__file__).resolve().parent / "static"
PAGES_PY = Path(pages.__file__).resolve()
# The policy as it was before the tour existed. The tour must fit inside it, never loosen it.
CSP_BEFORE_TOUR = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                   "form-action 'self'; base-uri 'none'")
# The tour may switch pages, scroll and open an information-only menu or side sheet. It may never press, submit,
# type, fire events or talk to the network, and it builds its card with text nodes only (no HTML strings).
FORBIDDEN_IN_TOUR = [".click(", "dispatchEvent", "fetch(", "XMLHttpRequest", ".submit(", "requestSubmit", "sendBeacon",
                     "new Event(", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", ".value =", ".value=",
                     ".checked =", ".checked=", "WebSocket", "EventSource", "location.reload", "location.assign",
                     "location.href =", 'setAttribute("style"', "cssText"]
# The only page state the tour may change, through app.js (window.paperAdvisorTour).
BRIDGE = {"showPage", "openSheet", "closeSheet"}


def tour_src() -> str:
    return (STATIC / "tour.js").read_text(encoding="utf-8")


def _steps_block(src: str) -> tuple[str, str]:
    """(everything before the STEPS array, the array). Anchored to lines of code: the header comment names both markers."""
    start = re.search(r"^\s*var STEPS = \[", src, re.M)
    end = re.search(r"^\s*// END STEPS", src, re.M)
    return src[:start.start()], src[start.end():end.start()]


def tour_steps(src: str) -> list[dict]:
    block = _steps_block(src)[1]
    ids = list(re.finditer(r'\bid:\s*"([a-z0-9-]+)"', block))

    def names(m):
        if m is None:
            return None
        v = json.loads(m.group(1))
        return v if isinstance(v, list) else [v]
    steps = []
    for i, m in enumerate(ids):
        chunk = block[m.end(): ids[i + 1].start() if i + 1 < len(ids) else len(block)]
        target = names(re.search(r'\btarget:\s*(\[[^\]]*\]|"[^"]*")', chunk))
        phone = names(re.search(r'\bphoneTarget:\s*(\[[^\]]*\]|"[^"]*")', chunk))
        also = names(re.search(r'\balso:\s*(\[[^\]]*\]|"[^"]*")', chunk)) or []  # lit together with the target
        steps.append({"id": m.group(1), "target": target, "phone": phone or target, "also": also, "text": chunk})
    return steps


def anchors(html: str) -> set[str]:
    return set(re.findall(r'data-tour="([a-z0-9-]+)"', html))


def _render(conn, exp_id, kind="forward") -> str:
    return pages.page(conn, exp_id, pages.view_now(conn, kind), kind, "token")


def _forward(root: Path):
    paths = AppPaths(root)
    paths.ensure_dirs()
    conn = db.init_db(paths.db_path, db_mode="forward")
    universe.install(conn, [i for i in universe.FORWARD_UNIVERSE if i.symbol in ("SPY", "MSFT")], now=clock.now_iso())
    exp_id, _ = experiments.ensure_default_experiment(conn, clock.now_utc())
    return conn, exp_id


def _run(conn, exp_id):
    from datetime import timedelta
    from paper_advisor import market_calendar as cal
    session = cal.next_session_after(clock.now_utc() + timedelta(seconds=120)).date.isoformat()
    with db.transaction(conn):
        return runs.create_run_row(conn, exp_id, "morning", session, version=1, mode="analysis", now=clock.now_iso())


@pytest.fixture(scope="module")
def states(tmp_path_factory):
    """One rendered dashboard page per database state the tour must make sense in (the brief's list)."""
    out = {}
    base = tmp_path_factory.mktemp("tour_states")

    conn, exp_id = _forward(base / "new")
    out["forward, no runs"] = _render(conn, exp_id)
    conn.close()

    conn, exp_id = _forward(base / "failed")
    rid = _run(conn, exp_id)
    with db.transaction(conn):
        runs.set_status(conn, rid, "failed", clock.now_iso(), error="aborted: network down")
    out["forward, failed run"] = _render(conn, exp_id)
    conn.close()

    conn, exp_id = _forward(base / "running")
    rid = _run(conn, exp_id)
    with db.transaction(conn):
        runs.set_status(conn, rid, "awaiting_decisions", clock.now_iso())
        runs.acquire_lock_tx(conn, exp_id, rid, clock.now_utc(), 180)
    out["forward, run in progress"] = _render(conn, exp_id)
    with db.transaction(conn):  # its session ended without renewing the lock: a stopped run
        conn.execute("UPDATE locks SET expires_at='2000-01-01T00:00:00.000000Z'")
    out["forward, stopped run"] = _render(conn, exp_id)
    conn.close()

    conn, exp_id = _forward(base / "archived")
    experiments.create_experiment(conn, name="second", starting_capital_cents=100000, now=clock.now_utc(), created_by="test")
    out["forward, archived experiment"] = _render(conn, exp_id)
    conn.close()

    demo = AppPaths(base / "demo")
    demo.ensure_dirs()
    fixture_demo.run_demo(demo, rebuild=True)
    conn = db.open_existing(demo.fixture_db_path)
    out["fixture demo"] = _render(conn, experiments.active_experiment_id(conn), "fixture")
    conn.close()

    # Last: the stale scenario steps an injected clock, which would date every state built after it.
    from test_dashboard import _stale_scenario
    paths = AppPaths(base / "stale")
    paths.ensure_dirs()
    conn = db.init_db(paths.fixture_db_path, db_mode="fixture")
    w = _stale_scenario(conn)
    out["fixture, stale data and provisional valuation"] = pages.page(conn, w.exp_id, clock.now_utc(), "fixture", "token")
    conn.close()
    clock.clear_override()
    return out


def test_the_tour_is_a_list_of_steps_ending_where_to_start():
    steps = tour_steps(tour_src())
    assert 15 <= len(steps) <= 20, [s["id"] for s in steps]  # every page, the five decisions, and short enough to finish
    assert len({s["id"] for s in steps}) == len(steps)
    assert all(s["target"] for s in steps), "every step points at something (a centred card is only the fallback)"
    last = steps[-1]["text"]
    assert "Take the tour" in last  # the last card says how to reopen it (peak-end: where to start, how to come back)


def test_every_tour_target_is_rendered_in_every_database_state(states):
    # A step whose anchor is missing falls back to a centred card, but that must be a rare state, never a renamed
    # anchor. For a list of alternatives, the last one is the fallback that every state renders.
    steps = tour_steps(tour_src())
    missing = []
    for name, html in states.items():
        present = anchors(html)
        for s in steps:
            for layout in ("target", "phone"):
                alts = s[layout]
                if not set(alts) & present or alts[-1] not in present:
                    missing.append(f"{name}: step {s['id']} ({layout}) wants {alts}")
            missing += [f"{name}: step {s['id']} lights {a} with its target" for a in s["also"] if a not in present]
    assert missing == []


def test_desktop_and_phone_variants_share_their_anchor(states):
    # The engine takes the first element that is laid out, so the top bar's piece (hidden on a phone) and the phone's
    # piece (hidden on a desktop) can carry the same anchor.
    for name, html in states.items():
        top = html.split('<header class="topbar"')[1].split("</header>")[0]
        strip = re.search(r'<div class="paper-strip"[^>]*>', html).group(0)
        tabbar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
        assert 'data-tour="paper"' in top and 'data-tour="paper"' in strip, name
        assert 'data-tour="commands"' in top and 'data-tour="commands"' in tabbar, name
        assert 'data-tour="commands-button"' in top and 'data-tour="commands-button"' in tabbar, name
        assert 'data-tour="health"' in top, name
        assert re.search(r'<nav class="tabbar"[^>]*data-tour="tabs"', html), name
        assert re.search(r'<aside class="sidebar"[^>]*data-tour="pages"', html), name


def test_no_anchor_is_left_without_a_step(states):
    src = tour_src()
    used = {n for s in tour_steps(src) for layout in ("target", "phone", "also") for n in s[layout]}
    used |= set(re.findall(r'\[data-tour="([a-z0-9-]+)"\]', src))  # looked up by a step's enter (a decision row)
    rendered = set().union(*(anchors(h) for h in states.values()))
    assert sorted(rendered - used) == []
    assert sorted(used - rendered) == []


def test_the_sheet_step_has_a_decision_to_open_when_there_are_decisions(states):
    # The tour opens the newest decision that placed an order (its sheet has an outcome, exit rules, checks with values
    # and an order lifecycle); a NO TRADE's sheet is mostly dashes (critic, round 1). Rows say which placed one: the
    # demo's newest decision is a NO TRADE, then filled BUYs, a HOLD and a rejected BUY (rejected = no order placed).
    fx = states["fixture demo"]
    index = fx.split('data-tour="decision-index"')[1].split("</table>")[0]
    rows = re.findall(r'<tr data-detail="(dec-\d+)"([^>]*)>', index)
    ordered = [did for did, attrs in rows if "data-ordered" in attrs]
    assert ordered and ordered[0] != rows[0][0] and len(ordered) < len(rows)
    assert f'<template id="{ordered[0]}">' in fx and f'<template id="{rows[0][0]}">' in fx
    assert "FILLED" in index.split(f'data-detail="{ordered[0]}"')[1].split("</tr>")[0]
    assert 'data-tour="decision-index"' not in states["forward, no runs"]  # no decisions: the step's centred note
    assert re.search(r'<aside id="drawer"[^>]*data-tour="sheet"', fx)


def test_the_tour_can_tell_that_no_run_has_happened_yet(states):
    # Before the active experiment's first run the phone strip names nothing while the More tab may carry a dot (no run
    # has fetched prices yet): the strip card says so then (critic, round 2: it listed stale data the strip never
    # named). tour.js reads this from the body's data-state, which pages.live_state renders for app.js.
    assert "last_run_id" in tour_src()
    no_run_yet = {"forward, no runs", "forward, archived experiment"}  # the new active experiment has not run either
    for name, html in states.items():
        state = json.loads(unescape(re.search(r'<body[^>]*\sdata-state="([^"]*)"', html).group(1)))
        assert "last_run_id" in state, name
        assert (state["last_run_id"] is None) == (name in no_run_yet), name


def test_the_tour_is_read_only():
    src = tour_src()
    assert [w for w in FORBIDDEN_IN_TOUR if w in src] == []
    # It reaches page state only through app.js's bridge, and only with these three calls. Comments are dropped first;
    # the one bare mention left is the existence check, so no alias can call around this test.
    code = re.sub(r"//[^\n]*", "", src)
    uses = re.findall(r"paperAdvisorTour(\.\w+)?", code)
    assert {u[1:] for u in uses if u} <= BRIDGE
    assert sum(1 for u in uses if not u) <= 1
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    bridge = app.split("window.paperAdvisorTour = Object.freeze({", 1)[1].split("});", 1)[0]
    assert set(re.findall(r"^\s*(\w+):", bridge, re.M)) == BRIDGE
    assert "tour-open" in app  # app.js never reloads the page under a running tour


def test_tripwire_rule_is_written_where_an_editor_meets_it():
    head = _steps_block(tour_src())[0]
    assert "SYNC WARNING" in head and "pages.py" in head and "test_tour.py" in head
    py = PAGES_PY.read_text(encoding="utf-8")
    assert py.count("SYNC WARNING: static/tour.js") >= 3  # beside the anchors
    readme = (PAGES_PY.parents[2] / "README.md").read_text(encoding="utf-8")
    assert "Take the tour" in readme and "tests/test_tour.py" in readme


def test_the_launch_control_is_on_desktop_and_phone(states):
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    phone_css = "".join(b.split("\n}\n", 1)[0] for b in css.split("@media (max-width: 760px) {")[1:])
    hides_on_phone = re.search(r"\.sidebar \.brand, \.sidebar \.nav, \.sidebar \.sidebar-foot \{ display: none; \}", css)
    assert hides_on_phone, "the phone rule that hides the sidebar's other parts moved: re-check the launcher"
    assert ".tour-launch" in phone_css  # the phone gives it its own compact look in the app bar
    for name, html in states.items():
        side = html.split('<aside class="sidebar"')[1].split("</aside>")[0]
        assert side.count('id="tour-launch"') == 1 and html.count('id="tour-launch"') == 1, name
        # Not inside the parts the phone hides (brand, nav, foot), so the one control serves both layouts.
        nav = side.split('<nav class="nav"', 1)[1].split("</nav>", 1)[0]
        brand = side.split('<div class="brand"', 1)[1].split("</div>", 1)[0]
        foot = side.split('<div class="sidebar-foot"', 1)[1].split("</div>", 1)[0]
        assert all('id="tour-launch"' not in part for part in (nav, brand, foot)), name
        assert re.search(r'<div class="tour-launch-wrap"[^>]*hidden', side), name  # shown by tour.js: no dead button without it
        assert "Take the tour" in side, name
        scripts = re.findall(r'<script src="([^"]+)"></script>', html)
        assert scripts == ["/static/app.js", "/static/tour.js"], name  # the bridge exists before the tour reads it


def test_pages_without_a_dashboard_offer_no_tour(app_paths):
    # Not initialized (nothing to tour) and report pages are simple pages: they never load the tour.
    report = pages.simple_page("Report, run 1", "<p>x</p>")
    assert "tour.js" not in report and "tour-launch" not in report
    app = server.App(app_paths, "forward", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{app.port}/", timeout=10)
        body = e.value.read().decode()
    finally:
        httpd.shutdown()
    assert e.value.code == 404 and "Not initialized" in body
    assert "tour.js" not in body and "tour-launch" not in body


def test_the_tour_file_is_served_and_the_csp_is_unchanged(app_paths):
    assert server.CSP == CSP_BEFORE_TOUR
    conn = db.init_db(app_paths.db_path, db_mode="forward")
    universe.install(conn, [i for i in universe.FORWARD_UNIVERSE if i.symbol in ("SPY", "MSFT")], now=clock.now_iso())
    experiments.ensure_default_experiment(conn, clock.now_utc())
    conn.close()
    app = server.App(app_paths, "forward", 0)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    app.port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{app.port}"
    try:
        with urllib.request.urlopen(base + "/static/tour.js", timeout=10) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "application/javascript"
            assert r.headers["Content-Security-Policy"] == CSP_BEFORE_TOUR
            assert r.read() == (STATIC / "tour.js").read_bytes()
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            assert r.headers["Content-Security-Policy"] == CSP_BEFORE_TOUR
            page = r.read().decode()
        assert '<script src="/static/tour.js"></script>' in page and 'id="tour-launch"' in page
        with pytest.raises(urllib.error.HTTPError) as e:  # the whitelist names files; it did not become a folder
            urllib.request.urlopen(base + "/static/pages.py", timeout=10)
        assert e.value.code == 404
    finally:
        httpd.shutdown()
