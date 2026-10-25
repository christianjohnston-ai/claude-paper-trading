"""Strict schema for the decision payload the Claude session submits each morning.

Why strict: the payload is the auditable record of what the advisor decided and why. Unknown keys are
rejected (so nothing can be smuggled past the record, including long free-form "reasoning" dumps;
the brief asks for concise evidence-based summaries, not hidden chain of thought), lengths are
capped, and every BUY must carry a thesis, a counterargument, a horizon, a size, an uncertainty
statement and precommitted exit rules. Structural errors reject the WHOLE payload before anything
is persisted; risk-limit failures are not schema errors, they are recorded per decision as rejections.

SYNC WARNING: the field list is documented for the advisor in .claude/commands/paper-morning.md and
in DECISION_SCHEMA_DOC below (printed into every briefing). Change all three together.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from . import clock

SCHEMA_VERSION = 1
MAX_TEXT = 1500
MAX_DECISIONS = 30
ACTIONS = ("BUY", "SELL", "HOLD", "NO_TRADE", "CANCEL")
EVIDENCE_KINDS = ("web", "news", "filing", "note", "macro", "earnings")
VERDICTS = ("selected", "rejected", "watch", "not_reviewed")

TOP_KEYS = {"schema_version", "run_id", "model", "market_view", "what_changed", "evidence", "candidates", "decisions"}
MODEL_KEYS = {"provider", "model_id", "effort"}
EVIDENCE_KEYS = {"ref", "kind", "title", "url", "publisher", "published_at", "retrieved_at", "summary", "symbol"}
CANDIDATE_KEYS = {"symbol", "verdict", "assessment", "reason", "evidence_refs"}
DECISION_KEYS = {"action", "symbol", "notional_usd", "quantity", "cancel_order_id", "thesis", "counterargument",
                 "horizon_sessions", "horizon_text", "exit_rules", "confidence", "uncertainty", "key_risks",
                 "evidence_refs", "skills_used"}
EXIT_KEYS = {"stop_loss_pct", "take_profit_pct", "max_holding_sessions", "invalidation"}

DECISION_SCHEMA_DOC = {
    "schema_version": 1,
    "run_id": "<the run id printed by `morning start`>",
    "model": {"provider": "anthropic/claude-code", "model_id": "<exact model id from your system context>",
              "effort": "<effort level if stated in your context, else 'unknown'>"},
    "market_view": "<= 1500 chars: what you concluded about the market today, citing evidence refs",
    "what_changed": "<= 1500 chars: what changed since the previous run (positions, data, news)",
    "evidence": [{"ref": "W1", "kind": "web|news|filing|note|macro|earnings", "title": "...", "url": "https://...",
                  "publisher": "...", "published_at": "ISO-8601 with timezone, or null if unknown",
                  "retrieved_at": "ISO-8601 with timezone", "summary": "<= 1500 chars, what it says", "symbol": "optional"}],
    "candidates": [{"symbol": "MSFT", "verdict": "selected|rejected|watch|not_reviewed", "assessment": "...",
                    "reason": "...", "evidence_refs": ["W1", 123]}],
    "decisions": [{
        "action": "BUY|SELL|HOLD|NO_TRADE|CANCEL", "symbol": "required except NO_TRADE",
        "notional_usd": "BUY only, e.g. '100.00' (cash to commit incl. costs)",
        "quantity": "SELL only: 'ALL' or shares with <= 6 decimals", "cancel_order_id": "CANCEL only",
        "thesis": "required", "counterargument": "required for BUY/SELL/HOLD",
        "horizon_sessions": "BUY: integer sessions", "horizon_text": "plain-language horizon",
        "exit_rules": {"stop_loss_pct": "e.g. '0.08' or null", "take_profit_pct": "e.g. '0.15' or null",
                       "max_holding_sessions": "integer or null", "invalidation": "required text: what would prove the thesis wrong"},
        "confidence": "BUY: probability-like 0..1 that the thesis plays out over the horizon (not a return forecast)",
        "uncertainty": "BUY: what you do not know", "key_risks": "...",
        "evidence_refs": ["refs from evidence[] or existing evidence ids from the briefing"],
        "skills_used": ["names of audited skills whose output or guidance you used"]}],
}


class SchemaError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _text(errors, where, v, required=False, max_len=MAX_TEXT):
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            errors.append(f"{where} is required")
        return
    if not isinstance(v, str):
        errors.append(f"{where} must be a string")
    elif len(v) > max_len:
        errors.append(f"{where} is {len(v)} chars; max {max_len} (keep summaries concise and evidence-based)")


def _iso_or_none(errors, where, v, required=False):
    if v is None:
        if required:
            errors.append(f"{where} is required")
        return
    try:
        clock.parse_iso(str(v))
    except ValueError:
        errors.append(f"{where} must be an ISO-8601 timestamp with timezone")


def _unknown(errors, where, d, allowed):
    extra = set(d) - allowed
    if extra:
        errors.append(f"{where} has unknown keys {sorted(extra)}")


def validate(payload, run_id: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    _unknown(errors, "payload", payload, TOP_KEYS)
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if payload.get("run_id") != run_id:
        errors.append(f"run_id must be {run_id} (the run this payload answers)")
    model = payload.get("model")
    if not isinstance(model, dict):
        errors.append("model must be an object with provider, model_id, effort")
    else:
        _unknown(errors, "model", model, MODEL_KEYS)
        _text(errors, "model.model_id", model.get("model_id"), required=True, max_len=100)
        _text(errors, "model.effort", model.get("effort"), required=True, max_len=40)
        _text(errors, "model.provider", model.get("provider"), required=True, max_len=100)
    _text(errors, "market_view", payload.get("market_view"), required=True)
    _text(errors, "what_changed", payload.get("what_changed"))
    refs = set()
    for i, e in enumerate(payload.get("evidence") or []):
        w = f"evidence[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{w} must be an object")
            continue
        _unknown(errors, w, e, EVIDENCE_KEYS)
        ref = e.get("ref")
        if not isinstance(ref, str) or not ref.strip():
            errors.append(f"{w}.ref is required (e.g. 'W1')")
        elif ref in refs:
            errors.append(f"{w}.ref {ref!r} is duplicated")
        else:
            refs.add(ref)
        if e.get("kind") not in EVIDENCE_KINDS:
            errors.append(f"{w}.kind must be one of {EVIDENCE_KINDS}")
        _text(errors, f"{w}.title", e.get("title"), required=True, max_len=500)
        _text(errors, f"{w}.summary", e.get("summary"), required=True)
        _iso_or_none(errors, f"{w}.published_at", e.get("published_at"))
        _iso_or_none(errors, f"{w}.retrieved_at", e.get("retrieved_at"), required=e.get("kind") in ("web", "news", "filing"))
        if e.get("kind") in ("web", "news", "filing") and not str(e.get("url") or "").startswith(("http://", "https://")):
            errors.append(f"{w}.url is required for {e.get('kind')} evidence")
    for i, c in enumerate(payload.get("candidates") or []):
        w = f"candidates[{i}]"
        if not isinstance(c, dict):
            errors.append(f"{w} must be an object")
            continue
        _unknown(errors, w, c, CANDIDATE_KEYS)
        _text(errors, f"{w}.symbol", c.get("symbol"), required=True, max_len=12)
        if c.get("verdict") not in VERDICTS:
            errors.append(f"{w}.verdict must be one of {VERDICTS}")
        _text(errors, f"{w}.assessment", c.get("assessment"), required=True)
        _text(errors, f"{w}.reason", c.get("reason"), required=True)
    decs = payload.get("decisions")
    if not isinstance(decs, list) or not decs:
        errors.append("decisions must be a non-empty list (use a single NO_TRADE decision when you choose not to act)")
        return errors
    if len(decs) > MAX_DECISIONS:
        errors.append(f"at most {MAX_DECISIONS} decisions per run")
    for i, d in enumerate(decs):
        w = f"decisions[{i}]"
        if not isinstance(d, dict):
            errors.append(f"{w} must be an object")
            continue
        _unknown(errors, w, d, DECISION_KEYS)
        a = str(d.get("action", "")).upper()
        if a not in ACTIONS:
            errors.append(f"{w}.action must be one of {ACTIONS}")
            continue
        _text(errors, f"{w}.thesis", d.get("thesis"), required=True)
        if a in ("BUY", "SELL", "HOLD"):
            _text(errors, f"{w}.counterargument", d.get("counterargument"), required=True)
        if a != "NO_TRADE" and not d.get("symbol") and a != "CANCEL":
            errors.append(f"{w}.symbol is required for {a}")
        if a == "BUY":
            if d.get("notional_usd") in (None, ""):
                errors.append(f"{w}.notional_usd is required for BUY")
            hs = d.get("horizon_sessions")
            if not isinstance(hs, int) or isinstance(hs, bool) or not 1 <= hs <= 1260:
                errors.append(f"{w}.horizon_sessions must be an integer 1..1260 for BUY")
            _text(errors, f"{w}.uncertainty", d.get("uncertainty"), required=True)
            try:
                conf = Decimal(str(d.get("confidence")))
                if not (Decimal(0) <= conf <= Decimal(1)):
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                errors.append(f"{w}.confidence must be a number between 0 and 1 for BUY")
            er = d.get("exit_rules")
            if not isinstance(er, dict):
                errors.append(f"{w}.exit_rules is required for BUY")
            else:
                _unknown(errors, f"{w}.exit_rules", er, EXIT_KEYS)
                _text(errors, f"{w}.exit_rules.invalidation", er.get("invalidation"), required=True)
        if a == "SELL" and d.get("quantity") in (None, ""):
            errors.append(f"{w}.quantity is required for SELL ('ALL' or a share quantity)")
        if a == "CANCEL" and d.get("cancel_order_id") is None:
            errors.append(f"{w}.cancel_order_id is required for CANCEL")
        if a == "HOLD" and d.get("exit_rules") is not None and not isinstance(d.get("exit_rules"), dict):
            errors.append(f"{w}.exit_rules must be an object")
        for k in ("horizon_text", "uncertainty", "key_risks"):
            _text(errors, f"{w}.{k}", d.get(k))
        for r in d.get("evidence_refs") or []:
            if isinstance(r, str) and r not in refs:
                errors.append(f"{w}.evidence_refs has unknown ref {r!r}")
            elif not isinstance(r, (str, int)) or isinstance(r, bool):
                errors.append(f"{w}.evidence_refs entries must be refs or evidence ids")
        if not isinstance(d.get("skills_used") or [], list):
            errors.append(f"{w}.skills_used must be a list")
    return errors
