import json
from pathlib import Path

from scripts.configure_alpha_paper_state import configure


def test_creates_25k_paper_policy_and_separate_halted_live_state(tmp_path: Path):
    paper = configure(tmp_path)
    live = tmp_path / "data" / "spectra"

    paper_policy = json.loads((paper / "capital_policy.json").read_text())
    paper_breaker = json.loads((paper / "breaker_state.json").read_text())
    live_policy = json.loads((live / "capital_policy.json").read_text())
    live_breaker = json.loads((live / "breaker_state.json").read_text())

    assert paper_policy["reserve_floor_quote"] == "6250"
    assert paper_policy["max_total_committed_quote"] == "18750"
    assert paper_policy["max_pair_quote"] == "1250"
    assert paper_policy["max_position_quote"] == "1250"
    assert paper_policy["max_recovery_quote"] == "1250"
    assert paper_policy["max_leverage"] == "1"
    assert paper_breaker == {"mode": "NORMAL"}
    assert live_policy["max_total_committed_quote"] == "0"
    assert live_policy["max_pair_quote"] == "0"
    assert live_policy["max_position_quote"] == "0"
    assert live_policy["max_recovery_quote"] == "0"
    assert live_breaker == {"mode": "HALTED"}
    assert paper.resolve() != live.resolve()
    for filename in ("capital_policy.json", "breaker_state.json"):
        assert (paper / filename).stat().st_mode & 0o777 == 0o600
        assert (live / filename).stat().st_mode & 0o777 == 0o600


def test_rerun_preserves_operator_changes(tmp_path: Path):
    paper = configure(tmp_path)
    policy_path = paper / "capital_policy.json"
    policy_path.write_text('{"max_total_committed_quote":"250"}\n')

    configure(tmp_path)

    assert json.loads(policy_path.read_text()) == {
        "max_total_committed_quote": "250"
    }


def test_upgrades_exact_legacy_policy_when_no_position_history_exists(tmp_path: Path):
    from scripts.configure_alpha_paper_state import LEGACY_PAPER_POLICY, PAPER_POLICY

    paper = tmp_path / "data" / "spectra-paper"
    paper.mkdir(parents=True)
    (paper / "capital_policy.json").write_text(json.dumps(LEGACY_PAPER_POLICY))

    configure(tmp_path)

    assert json.loads((paper / "capital_policy.json").read_text()) == PAPER_POLICY


def test_preserves_legacy_policy_when_any_position_history_exists(tmp_path: Path):
    from scripts.configure_alpha_paper_state import LEGACY_PAPER_POLICY
    from spectra_core.economic_position import EconomicPositionLedger

    paper = tmp_path / "data" / "spectra-paper"
    paper.mkdir(parents=True)
    (paper / "capital_policy.json").write_text(json.dumps(LEGACY_PAPER_POLICY))
    ledger = EconomicPositionLedger(paper / "economic_positions.sqlite3")
    ledger.open_position(
        position_id="existing-paper-position",
        pair="BTC-USD",
        created_at=1,
    )

    configure(tmp_path)

    assert json.loads((paper / "capital_policy.json").read_text()) == LEGACY_PAPER_POLICY
