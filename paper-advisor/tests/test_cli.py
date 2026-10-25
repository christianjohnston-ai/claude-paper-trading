"""The CLI accepts the commands exactly as the README and the demo's own output print them."""
from paper_advisor import cli


def test_dashboard_fixture_flag_works_in_both_documented_positions():
    p = cli.build_parser()
    # README and `pa demo` print "pa dashboard --fixture"; the flag is also a global option.
    assert p.parse_args(["dashboard", "--fixture"]).fixture is True
    assert p.parse_args(["--fixture", "dashboard"]).fixture is True
    assert p.parse_args(["dashboard"]).fixture is False
    assert p.parse_args(["dashboard", "--port", "8799"]).port == 8799


def test_new_experiment_below_the_tradable_floor_exits_with_the_reason(app_paths, capsys):
    import pytest
    assert cli.main(["--app-dir", str(app_paths.base), "init"]) == 0
    with pytest.raises(SystemExit) as e:
        cli.main(["--app-dir", str(app_paths.base), "experiment", "new", "--capital", "15"])
    msg = str(e.value)
    assert "at least $20.00" in msg and "could never place a buy" in msg
