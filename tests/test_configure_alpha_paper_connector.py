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
    assert result["paper_trade"]["paper_trade_account_balance"]["USDC"] == 1234
    assert result["paper_trade"]["paper_trade_account_balance"]["USD"] == 25000.0
    assert result["balance_asset_limit"]["kraken_paper_trade"] == {}
    assert result["global_token"]["global_token_name"] == "USDT"


def test_configuration_is_idempotent(tmp_path: Path):
    path = tmp_path / "conf_client.yml"
    path.write_text("paper_trade: {}\n", encoding="utf-8")
    configure(path)
    first = path.read_text(encoding="utf-8")
    configure(path)
    assert path.read_text(encoding="utf-8") == first

def test_preserves_existing_simulated_balances_and_other_paper_connectors(tmp_path: Path):
    path = tmp_path / "conf_client.yml"
    original = {
        "paper_trade": {
            "paper_trade_exchanges": ["binance", "kucoin"],
            "paper_trade_account_balance": {
                "USD": 1234.5,
                "BTC": 0.25,
                "USDT": 500.0,
            },
        }
    }
    path.write_text(yaml.safe_dump(original), encoding="utf-8")

    configure(path)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert result["paper_trade"]["paper_trade_exchanges"] == [
        "binance",
        "kucoin",
        "kraken",
    ]
    assert result["paper_trade"]["paper_trade_account_balance"] == {
        "USD": 1234.5,
        "BTC": 0.25,
        "USDT": 500.0,
        "ETH": 0.0,
        "XRP": 0.0,
    }

