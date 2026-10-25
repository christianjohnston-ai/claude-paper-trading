"""Local dashboard server (stdlib http.server), bound to 127.0.0.1 only.

Security model for a localhost tool (a web page in your browser can still try to talk to it):
- Binds to 127.0.0.1, and rejects requests whose Host header is not 127.0.0.1:<port>/localhost:<port>
  (DNS-rebinding protection).
- State-changing actions are POST-only, require a per-process CSRF token and a same-origin
  Origin/Referer, and never run while a morning run holds the experiment lock.
- Strict Content-Security-Policy (no inline scripts, no external origins), no framing, no caching.
- Downloads are limited to export zips by exact filename pattern inside paper-advisor/exports/.
Nothing here can place an order: actions are limited to prospective config versions, creating or
activating an experiment, backups and exports.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .. import clock, config, db, experiments, exporter, runs
from ..paths import PROJECT_ROOT
from .. import money as m
from . import pages

STATIC = Path(__file__).resolve().parent / "static"
EXPORT_RE = re.compile(r"^export_(forward|fixture)_\d{8}T\d{6}Z\.zip$")  # SYNC WARNING: exporter.export_all names files so
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"


class App:
    def __init__(self, paths, default_db: str, port: int):
        self.paths = paths
        self.default_db = default_db
        self.port = port
        self.csrf = secrets.token_urlsafe(32)
        self.notices: dict[str, tuple] = {}

    def db_path(self, kind: str) -> Path:
        return self.paths.fixture_db_path if kind == "fixture" else self.paths.db_path

    @staticmethod
    def show_path(p) -> str:
        """A path as the owner should look for it: relative to the project root when inside it."""
        try:
            return str(Path(p).resolve().relative_to(PROJECT_ROOT.resolve()))
        except ValueError:
            return str(p)

    def dirs(self, kind: str = "forward") -> tuple[str, str]:
        """Export/backup folders as the owner should look for them (relative to the project when inside it). The
        fixture demo's files live in fixture/ subfolders. SYNC WARNING: db.backup and exporter.export_all."""
        sub = "fixture/" if kind == "fixture" else ""
        return (self.show_path(self.paths.exports_dir) + "/" + sub, self.show_path(self.paths.backups_dir) + "/" + sub)

    def add_notice(self, kind: str, html_text: str, panel: str = "settings") -> str:
        nid = secrets.token_urlsafe(8)
        self.notices[nid] = (kind, html_text, time.time() + 600, panel)
        for k in [k for k, v in self.notices.items() if v[2] < time.time()]:
            self.notices.pop(k, None)
        return nid


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "paper-advisor"
        sys_version = ""

        def log_message(self, fmt, *args):  # keep the terminal quiet; no request bodies are ever logged
            sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8", extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, location: str):
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _forbid(self, why: str):
            self._send(403, pages.simple_page("Forbidden", f"<h1>Request refused</h1><p>{pages.h(why)}</p>").encode())

        def _query(self):
            u = urllib.parse.urlsplit(self.path)
            return u.path, {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}

        def _kind(self, q) -> str:
            return "fixture" if q.get("db", app.default_db) == "fixture" else "forward"

        def _not_initialized(self, kind):
            cmd = "paper-advisor/bin/pa demo" if kind == "fixture" else "paper-advisor/bin/pa init"
            other = "forward" if kind == "fixture" else "fixture"
            # Offer the database that does exist; a "back to dashboard" link would reload this same page.
            other_cmd = "paper-advisor/bin/pa demo" if other == "fixture" else "paper-advisor/bin/pa init"
            if app.db_path(other).exists():
                alt = f'<p><a href="/?db={other}">Open the {"forward paper test" if other == "forward" else "fixture demo"} instead</a></p>'
            elif other == "fixture":
                # A first-time owner may want to look around before creating the real account.
                alt = ("<p>To look around first, build the fixture demo, a separate database of synthetic data that never "
                       f"touches your paper account: <span class='cmd-pill'><code>{other_cmd}</code><button type='button' class='copy' "
                       f'data-copy="{other_cmd}" aria-label="Copy the demo command">Copy</button></span> '
                       "then open <a href='/?db=fixture'>the fixture demo</a>.</p>")
            else:
                alt = ""
            what = ("It builds the synthetic fixture scenario in its own database, separate from your paper account."
                    if kind == "fixture" else
                    "It creates your paper account once: an experiment funded with $500 of simulated money and a passive SPY "
                    "benchmark. Running it again never adds money or resets anything.")
            body = (f"<h1>{'Fixture demo' if kind == 'fixture' else 'Forward paper test'} database not found</h1>"
                    f"<p>Expected at <code class='path'>{pages.h(app.show_path(app.db_path(kind)))}</code>. Create it from the project root:</p>"
                    f"<p class='cmds'><span class='cmd-pill'><code>{pages.h(cmd)}</code><button type='button' class='copy' data-copy='{pages.h(cmd)}' "
                    f"aria-label='Copy the command'>Copy</button></span></p><p>{what}</p>"
                    f"<p>Then reload this page. Nothing was created or changed by opening it.</p>{alt}")
            self._send(404, pages.simple_page("Not initialized", body, kind, back=False).encode())

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._forbid("Host header not allowed (this server only answers on 127.0.0.1/localhost).")
            path, q = self._query()
            if path.startswith("/static/"):
                name = path.split("/static/", 1)[1]
                f = STATIC / name
                # Exact names, never a folder: tour.js is the guided tour (pages._page loads it after app.js).
                # SYNC WARNING: tests/test_tour.py checks it is served and that CSP above is unchanged.
                if name not in ("app.css", "app.js", "tour.js") or not f.exists():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, f.read_bytes(), "text/css" if name.endswith(".css") else "application/javascript")
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            kind = self._kind(q)
            if path.startswith("/download/export/"):
                name = path.rsplit("/", 1)[1]
                f = app.paths.exports_dir / ("fixture" if name.startswith("export_fixture_") else "") / name
                if not EXPORT_RE.match(name) or not f.exists():
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, f.read_bytes(), "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
            if not app.db_path(kind).exists():
                return self._not_initialized(kind)
            conn = db.connect(app.db_path(kind), readonly=True)
            try:
                if path == "/api/state":
                    # Polled by app.js on open forward-test tabs, which reload to the server-rendered state when a run
                    # starts or ends (pages.live_state). Read-only; the actions still refuse on their own mid-run.
                    return self._send(200, json.dumps(pages.live_state(conn, pages.view_now(conn, kind))).encode(), "application/json")
                exp_id = int(q["exp"]) if q.get("exp", "").isdigit() else experiments.active_experiment_id(conn)
                if exp_id is None or experiments.get(conn, exp_id) is None:
                    return self._not_initialized(kind)
                if path == "/report":
                    return self._report(conn, q, kind)
                if path != "/":
                    return self._send(404, pages.simple_page("Not found", "<h1>Not found</h1>", kind).encode())
                notice, panel = None, None
                if q.get("n") in app.notices:
                    k, text, _, panel = app.notices[q["n"]]
                    notice = (k, text)
                # Every action lives in the settings section, so its "Done." is shown there, under the
                # #settings anchor the redirect lands on (at the page top it was scrolled out of view).
                html_out = pages.page(conn, exp_id, pages.view_now(conn, kind), kind, app.csrf, notice,
                                      notice_target="settings" if notice else None, dirs=app.dirs(kind),
                                      notice_panel=panel if notice else None)
                return self._send(200, html_out.encode())
            finally:
                conn.close()

        def _report(self, conn, q, kind):
            rid = q.get("run", "")
            row = conn.execute("SELECT report_path, finished_at FROM runs WHERE id=?", (int(rid),)).fetchone() if rid.isdigit() else None
            p = Path(row["report_path"]) if row and row["report_path"] else None
            if p is None or not p.exists() or app.paths.reports_dir.resolve() not in p.resolve().parents:
                return self._send(404, pages.simple_page("Report not found", "<h1>Report not found</h1>", kind).encode())
            # A report is the record of one run, frozen when it finished: said first, because its figures read as current
            # beside the live dashboard (critic, round 9).
            fin = f" ({pages.h(pages.pt(row['finished_at']))})" if row["finished_at"] else ""
            snap = (f'<div class="notice info report-snap">{pages.icon("info")}<div class="notice-title">A snapshot from run #{rid}</div>'
                    f'<div class="notice-body"><p>Written when the run finished{fin}; its figures are as of then and never change. '
                    f'The dashboard shows the current state: <a href="/?db={pages.h(kind)}#runs">Run history</a>.</p></div></div>')
            # Rendered as a document (the markdown file stays the record); raw identifiers are collapsed at the end.
            body = (f"{snap}<div class='report-doc'>{pages.report_html(p.read_text())}</div>"
                    f"<p class='small muted'>Saved as <code class='path'>{pages.h(app.show_path(p))}</code></p>")
            return self._send(200, pages.simple_page(f"Report, run {rid}", body, kind).encode())

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._forbid("Host header not allowed.")
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if origin and not re.match(rf"^http://(127\.0\.0\.1|localhost):{app.port}(/|$)", origin):
                return self._forbid("Cross-origin request refused.")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 * 1024:
                return self._forbid("Request too large.")
            form = {k: v[-1] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).items()}
            if not secrets.compare_digest(form.get("csrf", ""), app.csrf):
                return self._forbid("Missing or invalid form token. Reload the dashboard and try again.")
            path, q = self._query()
            kind = self._kind(q)
            if not app.db_path(kind).exists():
                return self._not_initialized(kind)
            conn = db.open_existing(app.db_path(kind))
            try:
                exp_id = experiments.active_experiment_id(conn)
                handler = {"/actions/config": self._act_config, "/actions/new-experiment": self._act_new_experiment,
                           "/actions/activate": self._act_activate, "/actions/backup": self._act_backup,
                           "/actions/export": self._act_export}.get(path)
                if handler is None:
                    return self._send(404, b"not found", "text/plain")
                return handler(conn, exp_id, form, kind)
            finally:
                conn.close()

        def _lock_busy(self, conn, exp_id):
            lock = runs.current_lock(conn, exp_id)
            return lock if lock is not None and lock["expires_at"] > clock.now_iso() else None

        @staticmethod
        def _busy_words(busy, what: str) -> str:
            # Says what happens next, not only why (Rule 19 #5): the entries stay, and app.js refreshes the page when
            # the run ends (pages.live_state polling), keeping them. SYNC WARNING: app.js draft notes word the sequel.
            run = f"Morning run #{busy['run_id']}" if busy["run_id"] else "A morning run"
            return f"{run} is in progress; {what} when it finishes. Your entries stay in this form, and this page refreshes when the run ends."

        def _rerender(self, conn, exp_id, kind, errors, values, status=400, panel="risk"):
            # form_target marks the settings section for app.js to scroll to, and the summary renders
            # inside the form that failed (panel), so the message, the highlighted fields and the kept
            # input are seen together.
            names = {**{k: v[0] for k, v in pages.SETTING_LABELS.items()}, "reason": "Reason for the change",
                     "capital": "Starting capital", "confirm": "the confirmation box"}
            fields = [names[k] for k in errors if k in names]
            listed = fields[0] if len(fields) == 1 else ", ".join(fields[:-1]) + " and " + fields[-1] if fields else ""
            form_level = {k: v for k, v in errors.items() if k in ("_form", "_newexp")}
            if not fields and form_level:
                # One message, not a notice that points at a banner right under it (reviewer, round 8: two stacked banners).
                msg = " ".join(form_level.values())
                notice = ("error", f"Nothing was saved: {pages.h(msg)}" + ("" if "entries" in msg else " Your entries are kept."))
                errors = {k: v for k, v in errors.items() if k not in form_level}
            else:
                what = ("fix " + listed + " below" if fields else "see the message below")
                notice = ("error", f"Nothing was saved: {pages.h(what)}. Your entries are kept.")
            html_out = pages.page(conn, exp_id, pages.view_now(conn, kind), kind, app.csrf, notice,
                                  errors, values, "settings", notice_target="settings", dirs=app.dirs(kind), notice_panel=panel)
            self._send(status, html_out.encode())

        def _done(self, kind, exp_id, html_text, kind_notice="success", panel="risk"):
            # The redirect lands on the panel whose control was used, and the notice renders inside it.
            nid = app.add_notice(kind_notice, html_text, panel)
            self._redirect(f"/?db={kind}&exp={exp_id}&n={nid}#{panel}")

        def _act_config(self, conn, exp_id, form, kind):
            values = {k: form.get(k, "") for k in pages.SETTING_LABELS} | {"reason": form.get("reason", "")}
            if kind == "fixture":
                return self._rerender(conn, exp_id, kind, {"_form": "The fixture demo's settings are not editable."}, values, 409)
            busy = self._lock_busy(conn, exp_id)
            if busy:
                return self._rerender(conn, exp_id, kind, {"_form": self._busy_words(busy, "saving works again")}, values, 409)
            current = json.loads(config.current_version(conn, exp_id)["params_json"])
            overrides, errors = {}, {}
            for key in pages.SETTING_LABELS:
                raw = (form.get(key) or "").strip()
                cur = current.get(key)
                if raw == "" or raw == (str(cur).lower() if isinstance(cur, bool) else str(cur)):
                    continue
                overrides[key] = raw
            if not values["reason"].strip():
                errors["reason"] = "Say why you are changing the limits (it is stored with the new version)."
            if not overrides:
                errors["_form"] = "No setting differs from the current version; nothing to save."
            for key, raw in overrides.items():
                try:
                    config.normalize({**{k: v for k, v in current.items() if k in config.DEFAULTS}, key: raw})
                except config.ConfigError as exc:
                    # config.normalize names internal keys ("max_position_pct=1.7 is out of range");
                    # the owner needs the allowed range and what they typed, next to the field.
                    errors[key] = f"{pages.SETTING_RULES.get(key, str(exc))} You entered {raw}."
            if errors:
                return self._rerender(conn, exp_id, kind, errors, values)
            with db.transaction(conn):
                vid = config.create_version(conn, exp_id, overrides, created_by="dashboard", reason=values["reason"].strip(), now=clock.now_iso())
            ver = conn.execute("SELECT version FROM config_versions WHERE id=?", (vid,)).fetchone()[0]
            changed = ", ".join(f"{pages.h(pages.SETTING_LABELS[k][0])}: {pages.h(current.get(k))} → {pages.h(v)}" for k, v in overrides.items())
            # Saving limits that pause new buys is allowed (it can be deliberate), but never silent: the form warned
            # while typing (app.js) and the confirmation repeats it, computed by the server from the saved version.
            saved = json.loads(config.current_version(conn, exp_id)["params_json"])
            equity = pages.current_equity_cents(conn, exp_id, pages.view_now(conn, kind))
            need = config.min_tradable_capital_cents(saved)
            note = f" <strong>Note:</strong> {pages.h(pages.freeze_words(need, equity))}" if equity < need else ""
            return self._done(kind, exp_id, f"<strong>Done.</strong> Saved risk settings version {ver}, effective now for future decisions only. "
                                            f"Changed: {changed}. Earlier decisions keep the limits they were validated under.{note}")

        def _act_new_experiment(self, conn, exp_id, form, kind):
            values = {"capital": form.get("capital", ""), "name": form.get("name", ""), "confirm": form.get("confirm", "")}
            errors = {}
            # "_newexp" renders inside the new-experiment form ("_form" belongs to the risk settings form).
            if kind == "fixture":
                errors["_newexp"] = "The fixture demo database cannot create experiments."
            busy = self._lock_busy(conn, exp_id)
            if busy:
                errors["_newexp"] = self._busy_words(busy, "you can create the experiment")
            # The new experiment starts with the current limits, which also set its smallest workable capital.
            # SYNC WARNING: pages.settings renders the same floor as the field's data-lo for app.js.
            params = json.loads(config.current_version(conn, exp_id)["params_json"])
            floor = experiments.minimum_capital_cents(params)
            cents = None
            try:
                cents = m.parse_usd(values["capital"].replace(",", "").replace("$", "").strip())
                if cents < floor:
                    errors["capital"] = (f"Use at least {m.fmt_usd(floor)}: {experiments.capital_floor_reason(params)}. "
                                         f"You entered {m.fmt_usd(cents)}.")
            except ValueError:
                errors["capital"] = f"Enter an amount in dollars with at most 2 decimals, e.g. 1000 or 1000.00. You entered {values['capital'] or 'nothing'}."
            if values["confirm"] != "yes":
                errors["confirm"] = "Tick the box to confirm archiving the current experiment (its history is kept)."
            if errors:
                return self._rerender(conn, exp_id, kind, errors, values, panel="newexp")
            # Archiving the current experiment is the biggest state change the dashboard makes: keep a consistent
            # copy of the database as it was just before it (the same backup a morning run takes at its start).
            nxt = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM experiments").fetchone()[0]
            db.backup(conn, app.paths.backups_dir, reason=f"before creating experiment #{nxt} (archiving #{exp_id})")
            # Whole dollars without ".00", like the first experiment's "Forward paper experiment ($500)".
            amount = m.fmt_usd(cents)[:-3] if cents % 100 == 0 else m.fmt_usd(cents)
            new_id = experiments.create_experiment(conn, name=values["name"].strip() or f"Forward paper experiment ({amount})",
                                                   starting_capital_cents=cents, now=clock.now_utc(), created_by="dashboard", config_params=params)
            e = experiments.get(conn, new_id)
            return self._done(kind, new_id, f"<strong>Done.</strong> Created experiment #{new_id} with {m.fmt_usd(cents)}, funded once; "
                                            f"#{exp_id} is archived with its history intact (backed up first). First investable open: the "
                                            f"{pages.et_open(e['benchmark_start_session'])}. Next: run {pages.cmd_pill('/paper-morning')}", panel="newexp")

        def _act_activate(self, conn, exp_id, form, kind):
            if kind == "fixture":
                return self._forbid("Not available for the fixture demo.")
            try:
                target = int(form.get("id", ""))
                prev = experiments.active_experiment_id(conn)  # the one this press archives, named in the notice
                experiments.set_active(conn, target)
            except experiments.RunInProgress as exc:
                # The button is disabled during a run; this is the server's own refusal (a stale tab, a second window).
                return self._done(kind, exp_id, f"<strong>Not changed.</strong> {pages.h(str(exc)[:1].upper() + str(exc)[1:])}.",
                                  "error", panel="experiments")
            except (ValueError, TypeError):
                return self._forbid("Unknown experiment.")
            # Say what the press archived too, as Create does ("Done. Experiment #5 is now active" left #1 unsaid: capture, round 13).
            if prev is not None and prev != target:
                said = (f"<strong>Done.</strong> Experiment #{target} is now active; #{prev} is archived with its history intact "
                        f"(its settlement waits until it is made active again). {pages.cmd_pill('/paper-morning')} will manage #{target}.")
            else:
                said = f"<strong>Done.</strong> Experiment #{target} is now active; {pages.cmd_pill('/paper-morning')} will manage it."
            return self._done(kind, target, said, panel="experiments")

        def _act_backup(self, conn, exp_id, form, kind):
            info = db.backup(conn, app.paths.backups_dir, reason="dashboard backup")
            ok = "integrity check passed" if info["integrity_ok"] else "INTEGRITY CHECK FAILED"
            when = clock.now_utc().astimezone(pages.LA).strftime("%-I:%M %p PT")
            fname = Path(info["path"]).name
            # The folder is listed once, under the buttons; repeating its full path in every notice made the
            # panel a wall of paths.
            return self._done(kind, exp_id, f"<strong>Done.</strong> Backup written at {when} ({ok}) in the backups folder listed below "
                                            f"<span class='muted small'>(file {pages.h(fname)})</span>.",
                              "success" if info["integrity_ok"] else "error", panel="tools")

        def _act_export(self, conn, exp_id, form, kind):
            out = exporter.export_all(conn, app.paths, exp_id)
            name = Path(out["zip"]).name
            return self._done(kind, exp_id, f"<strong>Done.</strong> Exported {out['tables']} tables as CSV, plus metrics and a "
                                            f"SHA-256 manifest ({out['files']} files). "
                                            # The link says what it is; the file name is small print (critic, round 9).
                                            f"<a href='/download/export/{pages.h(name)}'>Download the export (zip)</a> "
                                            f"<span class='muted small'>(file {pages.h(name)})</span>; "
                                            "a copy is kept in the exports folder listed below.", panel="tools")

    return Handler


def serve(paths, fixture: bool = False, host: str = "127.0.0.1", port: int = 8765) -> int:
    if host != "127.0.0.1":
        raise SystemExit("the dashboard only binds to 127.0.0.1")
    app = App(paths, "fixture" if fixture else "forward", port)
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(app))
    except OSError as exc:
        print(f"Could not bind 127.0.0.1:{port}: {exc}. Is the dashboard already running? Try --port {port + 1}.")
        return 1
    print(f"paper-advisor dashboard (PAPER TRADING) at http://127.0.0.1:{port}/" + ("?db=fixture" if fixture else ""))
    print("Press Ctrl+C to stop. The database is never reset by starting or stopping the dashboard.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
