"""Runs ibd-distribution-day-monitor's pure analyze_index() on bars supplied by paper-advisor.

Executed as a subprocess:  python -I -B ibd_runner.py <skill_scripts_dir> <input.json>
The upstream module is imported unmodified from the installed skill directory; only its FMP data
loader is bypassed. Output: JSON on stdout. The skill's TQQQ-based portfolio_action is discarded on
purpose (leveraged products are outside this experiment's rules).
"""
import json
import sys

scripts_dir, input_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts_dir)

import ibd_monitor  # noqa: E402  (upstream module)

data = json.load(open(input_path, encoding="utf-8"))
config = ibd_monitor.load_config(None)
rule = ibd_monitor._build_rule(config)
thresholds = ibd_monitor._build_thresholds(config)
out = {}
for symbol, history in data["histories"].items():
    try:
        res, flags = ibd_monitor.analyze_index(symbol, symbol, history, config, rule, thresholds, data.get("as_of"))
        d = ibd_monitor._index_result_to_dict(res)
        out[symbol] = {"risk_level": d.get("risk_level"), "d5_count": d.get("d5_count"), "d15_count": d.get("d15_count"),
                       "d25_count": d.get("d25_count"), "is_distribution_day_today": d.get("is_distribution_day_today"),
                       "trend_filters": d.get("trend_filters"), "active_distribution_days": d.get("active_distribution_days"),
                       "audit_flags": flags}
    except Exception as exc:  # report per-symbol failure, keep others
        out[symbol] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(out, default=str))
