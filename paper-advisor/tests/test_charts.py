"""Chart axes say exactly where each gridline is: a reviewer found gridlines at $499.64, $500.94 ... labelled with
rounded whole dollars ($500, $501, $502, $504, $505: no $503, and the $500.00 cash line drawn above the "$500" line)."""
import re

from paper_advisor.dashboard import charts


def _grid(svg: str) -> list[tuple[float, str]]:
    """(y, label) for every y-axis gridline label."""
    return [(float(y), lab) for y, lab in re.findall(r'<text class="axis" x="[\d.]+" y="([\d.]+)" text-anchor="end">([^<]+)</text>', svg)]


def _scale_y(svg: str, v: float, grid: list[tuple[float, str]]) -> float:
    # Linear map from two labelled gridlines: every label must sit on the line of the value it names.
    (y1, l1), (y2, l2) = grid[0], grid[-1]
    a, b = float(l1.replace("$", "").replace(",", "")), float(l2.replace("$", "").replace(",", ""))
    return y1 + (v - a) * (y2 - y1) / (b - a)


def test_equity_axis_labels_name_the_values_their_gridlines_are_drawn_at():
    svg = charts.equity_chart(["2026-10-26", "2026-10-27"], {"Advisor": ([502.40, 502.40], "line-port"),
                                                             "DEMO.SPY": ([504.50, 504.50], "line-bench")}, 500.0)
    grid = _grid(svg)
    assert [lab for _, lab in grid] == ["$500", "$501", "$502", "$503", "$504", "$505"]  # $504.50 has a line above it
    ys = [y for y, _ in grid]
    steps = [b - a for a, b in zip(ys, ys[1:])]
    assert max(steps) - min(steps) <= 0.11  # evenly spaced, one dollar apart (y is printed to 0.1 px)
    cash_y = float(re.search(r'<line class="line-cash" x1="\d+" x2="\d+" y1="([\d.]+)"', svg).group(1))
    assert abs(cash_y - (_scale_y(svg, 500.0, grid) - 4)) < 0.2  # the $500.00 cash line lies on the "$500" gridline


def test_small_ranges_get_cent_labels_and_large_ones_round_numbers():
    svg = charts.equity_chart(["2026-10-26", "2026-10-27", "2026-10-28"],
                              {"Advisor": ([500.10, 500.30, 500.20], "line-port")}, 500.0)
    labels = [lab for _, lab in _grid(svg)]
    assert labels and all(re.fullmatch(r"\$500\.\d\d", lab) for lab in labels)  # $500.05-ish steps say their cents
    ticks, step = charts._nice_ticks(500.4, 514.4)  # a 2.5 step: the half-dollar lines keep their cents
    assert step == 2.5 and ticks == [502.5, 505.0, 507.5, 510.0, 512.5]
    svg = charts.equity_chart(["2026-10-26", "2026-10-27"], {"Advisor": ([501.0, 513.07], "line-port")}, 501.0)
    assert "$502.50" in [lab for _, lab in _grid(svg)]
    svg = charts.equity_chart(["2026-10-26", "2026-10-27"], {"Advisor": ([950.0, 1210.0], "line-port")}, 1000.0)
    labels = [lab for _, lab in _grid(svg)]
    assert labels and all(re.fullmatch(r"\$[\d,]+", lab) and int(lab[1:].replace(",", "")) % 50 == 0 for lab in labels)


def test_every_value_lies_between_the_outer_gridlines():
    # Capture, round 13: DEMO.SPY at $504.50 was drawn above the top gridline ($504) with no line above it to read it by.
    cases = [({"Advisor": ([502.40, 502.40], "line-port"), "DEMO.SPY": ([504.50, 504.50], "line-bench")}, 500.0),
             ({"Advisor": ([501.0, 513.07], "line-port")}, 501.0),
             ({"Advisor": ([950.0, 1210.0], "line-port")}, 1000.0),
             ({"Advisor": ([497.83, 500.0, 499.10], "line-port"), "SPY": ([500.0, 498.20, 503.95], "line-bench")}, 500.0)]
    for series, start in cases:
        dates = ["2026-10-26", "2026-10-27", "2026-10-28"][:len(next(iter(series.values()))[0])]
        values = [v for vals, _ in series.values() for v in vals] + [start]
        labels = [float(lab.replace("$", "").replace(",", "")) for _, lab in _grid(charts.equity_chart(dates, series, start))]
        assert labels[0] <= min(values) and labels[-1] >= max(values), (labels, min(values), max(values))


def _dd_grid(svg: str) -> list[tuple[float, float]]:
    """(y, value in percent) for every drawdown axis label, checked to sit on a gridline of its own."""
    out = []
    for y, lab in re.findall(r'<text class="axis" x="[\d.]+" y="([\d.]+)" text-anchor="end">([^<]+)</text>', svg):
        line_y = float(y) - 4
        assert f'y1="{line_y:.1f}" y2="{line_y:.1f}"' in svg, (lab, "has no gridline")
        out.append((line_y, float(lab.rstrip("%"))))
    return out


def test_drawdown_axis_labels_sit_on_gridlines_and_bracket_the_low():
    # Capture, round 13: only the 0% line was drawn, the bottom label ("-1.0%") floated without one, and a deeper low
    # would have been labelled with a rounded value ("-3.3%" at -3.27%), the defect round 12 fixed on the equity axis.
    dates = ["2026-10-26", "2026-10-27", "2026-10-28"]
    for dd in ([0.0, -0.0087, -0.0032], [0.0, -0.0327, -0.01], [0.0, -0.1840, -0.052]):
        grid = _dd_grid(charts.drawdown_chart(dates, dd))
        vals = [v for _, v in grid]
        assert max(vals) == 0.0 and min(vals) <= min(dd) * 100 and len(vals) >= 2
        steps = {round(b - a, 6) for a, b in zip(sorted(vals), sorted(vals)[1:])}
        assert len(steps) == 1  # evenly spaced round values
        assert all("-0.0" not in lab for lab in re.findall(r'text-anchor="end">([^<]+)<', charts.drawdown_chart(dates, dd)))
