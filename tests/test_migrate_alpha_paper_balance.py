from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from scripts import migrate_alpha_paper_balance as migration


def _account_config(path: Path, *, usd=1000.0, btc=0.0):
    config = {
        "paper_trade": {
            "paper_trade_exchanges": ["kraken"],
            "paper_trade_account_balance": {
                "USD": usd,
                "BTC": btc,
                "USDT": 0.0,
                "ETH": 0.0,
                "XRP": 0.0,
            },
        }
    }
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _api_env(path: Path):
    path.write_text("USERNAME=spectra_alpha\nPASSWORD=test-secret\n", encoding="utf-8")


def _mock_clean_api(monkeypatch):
    monkeypatch.setattr(
        migration,
        "_verify_paper_runtime",
        lambda **_kwargs: {
            "mode": "PAPER_ONLY",
            "paper_only": True,
            "paper_connectors_only": True,
            "live_dispatch_available": False,
            "paper_connectors": ["master_account/kraken_paper_trade"],
        },
    )
    monkeypatch.setattr(
        migration,
        "_verify_empty_history",
        lambda **_kwargs: {
            "trades": 0,
            "orders": 0,
            "active_orders": 0,
            "positions": 0,
        },
    )


def test_migration_dry_run_preserves_existing_account_file(tmp_path, monkeypatch):
    config_path = tmp_path / "conf_client.yml"
    env_path = tmp_path / ".env"
    _account_config(config_path)
    _api_env(env_path)
    original = config_path.read_text(encoding="utf-8")
    _mock_clean_api(monkeypatch)

    result = migration.migrate(
        config_path=config_path,
        api_env_path=env_path,
        api_url="http://127.0.0.1:8000",
        account_name="master_account",
        target_usd=Decimal("25000"),
        apply=False,
    )

    assert result == 0
    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.bak"))


def test_migration_apply_backs_up_and_updates_only_usd(tmp_path, monkeypatch):
    config_path = tmp_path / "conf_client.yml"
    env_path = tmp_path / ".env"
    _account_config(config_path, usd=1000.0)
    _api_env(env_path)
    _mock_clean_api(monkeypatch)

    result = migration.migrate(
        config_path=config_path,
        api_env_path=env_path,
        api_url="http://127.0.0.1:8000",
        account_name="master_account",
        target_usd=Decimal("25000"),
        apply=True,
    )

    updated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert result == 0
    assert updated["paper_trade"]["paper_trade_account_balance"] == {
        "USD": 25000.0,
        "BTC": 0.0,
        "USDT": 0.0,
        "ETH": 0.0,
        "XRP": 0.0,
    }
    backups = list(tmp_path.glob("conf_client.yml.before-paper-capital-*.bak"))
    assert len(backups) == 1
    assert yaml.safe_load(backups[0].read_text(encoding="utf-8"))[
        "paper_trade"
    ]["paper_trade_account_balance"]["USD"] == 1000.0


def test_migration_refuses_non_usd_holdings_without_modifying_config(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "conf_client.yml"
    env_path = tmp_path / ".env"
    _account_config(config_path, usd=1000.0, btc=0.01)
    _api_env(env_path)
    original = config_path.read_text(encoding="utf-8")
    _mock_clean_api(monkeypatch)

    with pytest.raises(RuntimeError, match="non-USD paper holdings"):
        migration.migrate(
            config_path=config_path,
            api_env_path=env_path,
            api_url="http://127.0.0.1:8000",
            account_name="master_account",
            target_usd=Decimal("25000"),
            apply=True,
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.bak"))


def test_migration_refuses_existing_history_without_modifying_config(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "conf_client.yml"
    env_path = tmp_path / ".env"
    _account_config(config_path)
    _api_env(env_path)
    original = config_path.read_text(encoding="utf-8")
    monkeypatch.setattr(migration, "_verify_paper_runtime", lambda **_kwargs: {})
    monkeypatch.setattr(
        migration,
        "_verify_empty_history",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("refusing balance migration: master_account has existing trades")
        ),
    )

    with pytest.raises(RuntimeError, match="existing trades"):
        migration.migrate(
            config_path=config_path,
            api_env_path=env_path,
            api_url="http://127.0.0.1:8000",
            account_name="master_account",
            target_usd=Decimal("25000"),
            apply=True,
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.bak"))


def test_migration_never_reduces_a_balance_above_target(tmp_path, monkeypatch):
    config_path = tmp_path / "conf_client.yml"
    env_path = tmp_path / ".env"
    _account_config(config_path, usd=26000.0)
    _api_env(env_path)
    original = config_path.read_text(encoding="utf-8")
    _mock_clean_api(monkeypatch)

    with pytest.raises(RuntimeError, match="refusing to reduce existing USD balance"):
        migration.migrate(
            config_path=config_path,
            api_env_path=env_path,
            api_url="http://127.0.0.1:8000",
            account_name="master_account",
            target_usd=Decimal("25000"),
            apply=True,
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.bak"))
