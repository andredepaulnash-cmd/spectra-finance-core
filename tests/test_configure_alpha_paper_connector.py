from pathlib import Path

import yaml

from scripts.configure_alpha_paper_connector import configure


def test_adds_kraken_paper_connector_and_preserves_existing_config(tmp_path: Path):
    path = tmp_path / "conf_client.yml"
    original = {
        "instance_id": "alpha-test",
        "paper_trade": {
            "paper_trade_exchanges": ["binance"],
            "paper_trade_account_balance": {"USDC": 1234},
        },
        "balance_asset_limit": {"binance_paper_trade": {}},
        "global_token": {"global_token_name": "USDT"},
    }
    path.write_text(yaml.safe_dump(original), encoding="utf-8")

    configure(path)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert result["instance_id"] == "alpha-test"
    assert result["paper_trade"]["paper_trade_exchanges"] == ["binance", "kraken"]
    assert result["paper_trade"]["paper_trade_account_balance"] == {"USDC": 1234}
    assert result["balance_asset_limit"]["kraken_paper_trade"] == {}
    assert result["global_token"]["global_token_name"] == "USDT"


def test_fresh_install_starts_with_exactly_25000_usd(tmp_path: Path):
    path = tmp_path / "conf_client.yml"
    path.write_text(
        yaml.safe_dump({
            "paper_trade": {
                "paper_trade_exchanges": ["binance"],
                "paper_trade_account_balance": {
                    "BTC": 1.0, "USDT": 1000.0, "USDC": 1000.0
                },
            }
        }),
        encoding="utf-8",
    )

    configure(path, reset_balances=True)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert result["paper_trade"]["paper_trade_exchanges"] == ["binance", "kraken"]
    assert result["paper_trade"]["paper_trade_account_balance"] == {"USD": 25000.0}


def test_configuration_is_idempotent(tmp_path: Path):
    path = tmp_path / "conf_client.yml"
    path.write_text("paper_trade: {}\n", encoding="utf-8")
    configure(path)
    first = path.read_text(encoding="utf-8")
    configure(path)
    assert path.read_text(encoding="utf-8") == first


def test_migrates_exact_legacy_balance_only_without_position_history(tmp_path: Path):
    from scripts.configure_alpha_paper_connector import LEGACY_ALPHA_BALANCES

    path = tmp_path / "conf_client.yml"
    path.write_text(
        yaml.safe_dump({"paper_trade": {"paper_trade_account_balance": LEGACY_ALPHA_BALANCES}}),
        encoding="utf-8",
    )

    configure(path, core_root=tmp_path, upgrade_legacy_default=True)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert result["paper_trade"]["paper_trade_account_balance"] == {"USD": 25000.0}


def test_preserves_legacy_balance_when_paper_position_history_exists(tmp_path: Path):
    from scripts.configure_alpha_paper_connector import LEGACY_ALPHA_BALANCES
    from spectra_core.economic_position import EconomicPositionLedger

    path = tmp_path / "conf_client.yml"
    path.write_text(
        yaml.safe_dump({"paper_trade": {"paper_trade_account_balance": LEGACY_ALPHA_BALANCES}}),
        encoding="utf-8",
    )
    ledger_path = tmp_path / "data" / "spectra-paper" / "economic_positions.sqlite3"
    ledger_path.parent.mkdir(parents=True)
    ledger = EconomicPositionLedger(ledger_path)
    ledger.open_position(
        position_id="existing-paper-position",
        pair="BTC-USD",
        created_at=1,
    )

    configure(path, core_root=tmp_path, upgrade_legacy_default=True)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert result["paper_trade"]["paper_trade_account_balance"] == LEGACY_ALPHA_BALANCES
