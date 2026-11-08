import json
from pathlib import Path

from scripts.configure_alpha_paper_state import configure


def test_creates_separate_conservative_paper_policy_and_normal_paper_breaker(tmp_path: Path):
    paper = configure(tmp_path)
    live = tmp_path / "data" / "spectra"

    policy = json.loads((paper / "capital_policy.json").read_text())
    breaker = json.loads((paper / "breaker_state.json").read_text())

    assert policy["reserve_floor_quote"] == "100"
    assert policy["max_total_committed_quote"] == "1000"
    assert policy["max_pair_quote"] == "1000"
    assert policy["max_position_quote"] == "1000"
    assert policy["max_recovery_quote"] == "0"
    assert policy["max_leverage"] == "1"
    assert breaker == {"mode": "NORMAL"}
    assert not live.exists()
    assert (paper / "capital_policy.json").stat().st_mode & 0o777 == 0o600
    assert (paper / "breaker_state.json").stat().st_mode & 0o777 == 0o600


def test_rerun_preserves_operator_changes(tmp_path: Path):
    paper = configure(tmp_path)
    policy_path = paper / "capital_policy.json"
    policy_path.write_text('{"max_total_committed_quote":"250"}\n')

    configure(tmp_path)

    assert json.loads(policy_path.read_text()) == {
        "max_total_committed_quote": "250"
    }
