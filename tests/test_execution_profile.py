import pytest

from spectra_alpha2.execution_profile import (
    ExecutionMode,
    ExecutionProfileError,
    resolve_execution_profile,
)


def test_paper_is_default_and_matches_api_profile_contract():
    profile = resolve_execution_profile({})
    assert profile.mode is ExecutionMode.PAPER
    assert profile.profile_id == "PAPER:master_account:kraken_paper_trade"
    assert profile.portfolio_namespace == "paper"


def test_paper_connector_is_fixed():
    with pytest.raises(ExecutionProfileError, match="PAPER profile"):
        resolve_execution_profile({"SPECTRA_PAPER_CONNECTOR": "kraken"})


def test_live_requires_explicit_opt_in_and_separate_account():
    with pytest.raises(ExecutionProfileError, match="not explicitly enabled"):
        resolve_execution_profile({
            "SPECTRA_EXECUTION_MODE": "LIVE",
            "SPECTRA_LIVE_ACCOUNT": "live_kraken",
            "SPECTRA_LIVE_CONNECTOR": "kraken",
        })
    with pytest.raises(ExecutionProfileError, match="separate account"):
        resolve_execution_profile({
            "SPECTRA_EXECUTION_MODE": "LIVE",
            "SPECTRA_LIVE_PROFILE_ENABLED": "true",
            "SPECTRA_LIVE_ACCOUNT": "master_account",
            "SPECTRA_LIVE_CONNECTOR": "kraken",
        })


def test_live_resolves_only_native_non_paper_connector():
    profile = resolve_execution_profile({
        "SPECTRA_EXECUTION_MODE": "LIVE",
        "SPECTRA_LIVE_PROFILE_ENABLED": "true",
        "SPECTRA_LIVE_ACCOUNT": "live_kraken",
        "SPECTRA_LIVE_CONNECTOR": "kraken",
    })
    assert profile.mode is ExecutionMode.LIVE
    assert profile.profile_id == "LIVE:live_kraken:kraken"
    assert profile.portfolio_namespace == "live"
    with pytest.raises(ExecutionProfileError, match="non-paper"):
        resolve_execution_profile({
            "SPECTRA_EXECUTION_MODE": "LIVE",
            "SPECTRA_LIVE_PROFILE_ENABLED": "true",
            "SPECTRA_LIVE_ACCOUNT": "live_kraken",
            "SPECTRA_LIVE_CONNECTOR": "kraken_paper_trade",
        })


def test_order_scope_cannot_cross_profile():
    profile = resolve_execution_profile({})
    profile.assert_order_scope("master_account", "kraken_paper_trade")
    with pytest.raises(ExecutionProfileError, match="does not match"):
        profile.assert_order_scope("live_kraken", "kraken")


def test_invalid_mode_and_account_fail_closed():
    with pytest.raises(ExecutionProfileError, match="PAPER or LIVE"):
        resolve_execution_profile({"SPECTRA_EXECUTION_MODE": "AUTO"})
    with pytest.raises(ExecutionProfileError, match="account name"):
        resolve_execution_profile({"SPECTRA_PAPER_ACCOUNT": "../master"})
