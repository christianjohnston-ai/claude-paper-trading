"""Server-rendered SVG charts (no external libraries or CDNs: the dashboard works fully offline).

The equity and drawdown charts are stacked in one panel and share the x-axis, so both use the same
width and horizontal padding (PAD_L/PAD_R): keep them equal or the two date scales stop lining up.
PAD_R leaves room for end-of-line value labels such as "Advisor $504.70".
"""
from __future__ import annotations

import math
from datetime import date
from html import escape

PAD_L, PAD_R = 56, 124


def _date_label(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%b %-d")  # "Nov 24" reads faster than "11-24"
    except ValueError:
        return escape(iso)


def _scale(vals, lo, hi, out_lo, out_hi):
    span = (hi - lo) or 1.0
    return [out_lo + (v - lo) / span * (out_hi - out_lo) for v in vals]


def _nice_ticks(lo: float, hi: float, max_intervals: int = 6) -> tuple[list[float], float]:
    """Round-number gridline values inside [lo, hi] and their step (1, 2, 2.5 or 5 times a power of ten). Each label
    must name the value its line is drawn at: evenly spaced lines labelled with rounded values read "$502" for $502.25
    and skipped "$503" (reviewer, round 12)."""
    span = (hi - lo) or 1.0
    mag = 10 ** math.floor(math.log10(span / max_intervals))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if span / (m * mag) <= max_intervals)
    first = math.ceil(lo / step - 1e-9) * step
    count = int(math.floor((hi - first) / step + 1e-9)) + 1
    return [round(first + k * step, 10) for k in range(count)], step


def equity_chart(dates, series: dict, start_value: float, width=640, height=240, title="", marks: dict | None = None,
                 pad_l: int = PAD_L, pad_r: int = PAD_R) -> str:
    """series: {label: (values, css_class)} aligned to dates. A flat 'cash' baseline is drawn at start_value.
    marks: {label: [indices]} drawn as hollow dots on that series (provisional valuations)."""
    if len(dates) < 2:
        return (f'<div class="chart-empty" role="img" aria-label="{escape(title)}: not enough data yet">'
                f'<p><strong>Not enough history to chart.</strong></p><p>The equity curve starts after the first valued session '
                f'({len(dates)} so far).</p></div>')
    pad_t, pad_b = 14, 28
    allv = [v for vals, _ in series.values() for v in vals if v is not None] + [start_value]
    lo, hi = min(allv), max(allv)
    margin = (hi - lo) * 0.08 or max(1.0, hi * 0.01)
    lo, hi = lo - margin, hi + margin
    n = len(dates)
    xs = [pad_l + i * (width - pad_l - pad_r) / (n - 1) for i in range(n)]
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">']
    ticks, step = _nice_ticks(lo, hi)
    # Every value lies between two labelled gridlines: one more round step where the data runs past the outermost line
    # (DEMO.SPY at $504.50 was drawn above a top line of $504, with nothing above it to read it by: capture, round 13).
    # One step always suffices: the ticks start and end within a step of the padded range, which contains the data.
    if max(allv) > ticks[-1] + 1e-9:
        ticks.append(round(ticks[-1] + step, 10))
    if min(allv) < ticks[0] - 1e-9:
        ticks.insert(0, round(ticks[0] - step, 10))
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    for v in ticks:
        y = _scale([v], lo, hi, height - pad_b, pad_t)[0]
        # Cents only when the step is not whole dollars (0.1, 0.25, 2.5): "$502" for $502.50 would mislabel the line.
        lab = f"${v:,.0f}" if float(step).is_integer() else f"${v:,.2f}"
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="axis" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{lab}</text>')
    for i in sorted({0, n // 2, n - 1}):
        parts.append(f'<text class="axis" x="{xs[i]:.1f}" y="{height - 8}" text-anchor="middle">{_date_label(dates[i])}</text>')
    yb = _scale([start_value], lo, hi, height - pad_b, pad_t)[0]
    parts.append(f'<line class="line-cash" x1="{pad_l}" x2="{width - pad_r}" y1="{yb:.1f}" y2="{yb:.1f}"/>'
                 f'<text class="label label-cash" x="{width - pad_r + 6}" y="{yb + 4:.1f}">Cash ${start_value:,.2f}</text>')
    label_ys = [yb]  # line labels must not overprint the cash baseline's label
    for label, (vals, cls) in series.items():
        pts = [(x, v) for x, v in zip(xs, vals) if v is not None]
        if not pts:
            continue
        ys = _scale([v for _, v in pts], lo, hi, height - pad_b, pad_t)
        d = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, ((x, _), y) in enumerate(zip(pts, ys)))
        parts.append(f'<path class="{cls}" d="{d}"/>')
        for i in (marks or {}).get(label, []):
            if 0 <= i < len(vals) and vals[i] is not None:
                yi = _scale([vals[i]], lo, hi, height - pad_b, pad_t)[0]
                parts.append(f'<circle class="prov-dot" cx="{xs[i]:.1f}" cy="{yi:.1f}" r="4.5"><title>provisional valuation, {_date_label(dates[i])}</title></circle>')
        # The end label says "provisional" itself when its own point is: the dot's <title> is hover-only, which
        # a phone cannot do, and "Advisor $502.40" alone read as a firm number.
        last_i = max(i for i, v in enumerate(vals) if v is not None)
        prov_last = last_i in set((marks or {}).get(label, []))
        rows = 2 if prov_last else 1
        ly = ys[-1]
        while any(abs(ly + k * 12 - o) < 13 for k in range(rows) for o in label_ys):
            ly += 13
        label_ys.extend(ly + k * 12 for k in range(rows))
        lx = width - pad_r + 6
        extra = f'<tspan class="label-prov" x="{lx}" dy="12">provisional</tspan>' if prov_last else ""
        parts.append(f'<text class="label {cls}-label" x="{lx}" y="{ly + 4:.1f}">{escape(label)} ${pts[-1][1]:,.2f}{extra}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pct_label(x: float) -> str:
    return "0.00%" if abs(x) < 0.00005 else f"{x * 100:.2f}%"  # never "-0.00%"


def drawdown_chart(dates, dd, width=640, height=150, title="", pad_l: int = PAD_L, pad_r: int = PAD_R) -> str:
    if len(dates) < 2:
        return f'<div class="chart-empty" role="img" aria-label="{escape(title)}: not enough data yet"><p>No drawdown history yet.</p></div>'
    pad_t, pad_b = 10, 24
    # Round-number gridlines from 0% down past the low, each label on its own line (as on the equity axis): the bottom
    # label used to float without a line and to name a rounded value ("-3.3%" drawn at -3.27%; capture, round 13).
    # At least -1% deep, so a flat start does not stretch tiny dips across the whole height. Values in percent here.
    ticks, step = _nice_ticks(min(min(dd), -0.01) * 100, 0.0, max_intervals=3)
    if min(dd) * 100 < ticks[0] - 1e-9:
        ticks.insert(0, round(ticks[0] - step, 10))
    lo = ticks[0] / 100
    decimals = 0 if float(step).is_integer() else (1 if float(step * 10).is_integer() else 2)
    n = len(dates)
    xs = [pad_l + i * (width - pad_l - pad_r) / (n - 1) for i in range(n)]
    ys = _scale(dd, lo, 0.0, height - pad_b, pad_t)
    y0 = pad_t
    area = f"M{xs[0]:.1f},{y0:.1f} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y in zip(xs, ys)) + f" L{xs[-1]:.1f},{y0:.1f} Z"
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">']
    for v in ticks:
        y = round(_scale([v / 100], lo, 0.0, height - pad_b, pad_t)[0], 1)  # one rounding: the label sits 4 px below its line
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}"/>'
                     f'<text class="axis" x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end">{(v or 0.0):.{decimals}f}%</text>')
    parts += [f'<path class="dd-area" d="{area}"/>',
              f'<text class="label" x="{width - pad_r + 6}" y="{ys[-1] + 4:.1f}">{_pct_label(dd[-1])}</text>']
    for i in sorted({0, n // 2, n - 1}):  # first, middle and last session, as on the equity chart
        parts.append(f'<text class="axis" x="{xs[i]:.1f}" y="{height - 6}" text-anchor="middle">{_date_label(dates[i])}</text>')
    parts.append("</svg>")
    return "".join(parts)
