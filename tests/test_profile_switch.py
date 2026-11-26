import pytest

from spectra_alpha2.execution_profile import resolve_execution_profile
from spectra_alpha2.profile_switch import (
    ProfileRuntimeState,
    ProfileSwitchRejected,
    assert_profile_switch_allowed,
)


def _paper():
    return resolve_execution_profile({})


def _live():
    return resolve_execution_profile({
        "SPECTRA_EXECUTION_MODE": "LIVE",
        "SPECTRA_LIVE_PROFILE_ENABLED": "true",
        "SPECTRA_LIVE_ACCOUNT": "live_kraken",
        "SPECTRA_LIVE_CONNECTOR": "kraken",
    })


def test_switch_requires_stopped_supervisor_and_reconciled_exchange_state():
    assert_profile_switch_allowed(
        _paper(), _live(),
        ProfileRuntimeState(True, 0, 0, True),
    )


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (ProfileRuntimeState(False, 0, 0, True), "supervisor"),
        (ProfileRuntimeState(True, 1, 0, True), "orders are pending"),
        (ProfileRuntimeState(True, 0, 1, True), "fills remain unreconciled"),
        (ProfileRuntimeState(True, 0, 0, False), "balances are reconciled"),
    ],
)
def test_switch_refuses_non_quiescent_runtime(state, message):
    with pytest.raises(ProfileSwitchRejected, match=message):
        assert_profile_switch_allowed(_paper(), _live(), state)


def test_same_namespace_transition_is_rejected():
    paper = _paper()
    other_paper = resolve_execution_profile({
        "SPECTRA_PAPER_ACCOUNT": "other_paper",
    })
    with pytest.raises(ProfileSwitchRejected, match="separate state namespaces"):
        assert_profile_switch_allowed(
            paper, other_paper, ProfileRuntimeState(True, 0, 0, True)
        )


def test_negative_reconciliation_counts_are_invalid():
    with pytest.raises(ValueError, match="cannot be negative"):
        ProfileRuntimeState(True, -1, 0, True)
