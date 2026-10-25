"""Runs position-sizer's pure calculate_position() for a list of candidates supplied by paper-advisor.

Executed as a subprocess:  python -I -B sizer_runner.py <skill_scripts_dir> <input.json>
The audit found the upstream sizer applies NO position or cash cap unless flags are passed, and leaves
risk_pct and Kelly unbounded. This adapter therefore always passes max_position_pct, fixes risk_pct,
never uses Kelly, and caps the result at the cash actually available (in the parent). The output is
ADVISORY: the deterministic validator in paper-advisor remains the only authority on order size.
"""
import json
import sys

scripts_dir, input_path = sys.argv[1], sys.argv[2]
sys.path.insert(0, scripts_dir)

import position_sizer  # noqa: E402  (upstream module)

data = json.load(open(input_path, encoding="utf-8"))
rows = []
for c in data["candidates"]:
    try:
        p = position_sizer.SizingParameters(
            account_size=float(data["equity"]), entry_price=float(c["entry"]), atr=float(c["atr"]),
            atr_multiplier=float(data["atr_multiplier"]), risk_pct=float(data["risk_pct"]),
            max_position_pct=float(data["max_position_pct"]) * 100.0, fractional_shares=True, share_precision=6)
        r = position_sizer.calculate_position(p)
        rows.append({"symbol": c["symbol"], "entry": c["entry"], "atr": c["atr"],
                     "stop": round(float(c["entry"]) - float(data["atr_multiplier"]) * float(c["atr"]), 2),
                     "shares": r.get("final_recommended_shares"), "position_value": r.get("final_position_value"),
                     "risk_dollars": r.get("final_risk_dollars"), "binding_constraint": r.get("binding_constraint")})
    except Exception as exc:
        rows.append({"symbol": c["symbol"], "error": f"{type(exc).__name__}: {exc}"})
print(json.dumps(rows, default=str))
