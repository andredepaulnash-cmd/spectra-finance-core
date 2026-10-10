"""Frozen, causal market-breadth gate for offline profitability research.

The input is the authenticated fixed 619-asset, five-minute calendar array.
``index`` identifies its latest completed bar. Only its close vector and the
close vector exactly 288 bars earlier are read. This descriptive gate does not
authorize funds, place orders, or establish a profitable market regime.
"""

from __future__ import annotations

import numpy as np


BARS_PER_DAY = 288
FIXED_ASSET_COUNT = 619
BAR_FIELD_COUNT = 6
CLOSE_COLUMN = 3
MINIMUM_VALID_ASSETS = 50


def _close_vector(rows, index: int) -> np.ndarray:
    values = np.asarray(rows[index, :, CLOSE_COLUMN])
    if values.shape != (FIXED_ASSET_COUNT,):
        raise ValueError("close anchors must contain exactly 619 assets")
    if (not np.issubdtype(values.dtype, np.number)
            or np.issubdtype(values.dtype, np.complexfloating)):
        raise ValueError("close anchors must be real numeric values")
    return values


def causal_market_regime(rows, index: int) -> tuple[bool, dict]:
    """Return permission for new admission and exact breadth diagnostics.

    Both close anchors must be finite and positive for an asset to contribute.
    Flat returns are valid denominator observations but are not positive.
    At least 50 assets must contribute and at least half must have a strictly
    higher current close. An unavailable 24-hour anchor denies admission.

    Price magnitudes are compared directly, avoiding overflow when dividing
    very large prices by very small prices. No suffix scan, forward filling,
    daily aggregation, or search for an alternative missing anchor occurs.
    The caller must provide a correctly aligned five-minute calendar and
    identify a completed bar; timestamps and asset identities are authenticated
    by the campaign's input admission boundary.
    """
    try:
        shape = tuple(rows.shape)
    except (AttributeError, TypeError) as exc:
        raise ValueError("rows must expose a three-dimensional array shape") from exc
    if len(shape) != 3 or shape[1:] != (FIXED_ASSET_COUNT, BAR_FIELD_COUNT):
        raise ValueError("rows must have shape (frames, 619, 6)")
    if isinstance(index, (bool, np.bool_)) or not isinstance(index, (int, np.integer)):
        raise ValueError("index must be an integer completed-bar index")
    index = int(index)
    if index < 0 or index >= shape[0]:
        raise ValueError("index is outside the available calendar")

    diagnostic = {
        "decision_index": index,
        "anchor_index_24h": index - BARS_PER_DAY if index >= BARS_PER_DAY else None,
        "universe_assets": FIXED_ASSET_COUNT,
        "minimum_valid_assets": MINIMUM_VALID_ASSETS,
        "required_positive_fraction": 0.5,
        "valid_assets": 0,
        "excluded_assets": FIXED_ASSET_COUNT,
        "positive_assets": 0,
        "positive_fraction": None,
        "allowed": False,
        "reason": "insufficient_24h_history",
    }
    if index < BARS_PER_DAY:
        return False, diagnostic

    current = _close_vector(rows, index)
    earlier = _close_vector(rows, index - BARS_PER_DAY)
    valid = (np.isfinite(current) & np.isfinite(earlier)
             & (current > 0) & (earlier > 0))
    valid_count = int(np.count_nonzero(valid))
    positive_count = int(np.count_nonzero(valid & (current > earlier)))
    enough_history = valid_count >= MINIMUM_VALID_ASSETS
    allowed = enough_history and positive_count * 2 >= valid_count
    diagnostic.update(
        valid_assets=valid_count,
        excluded_assets=FIXED_ASSET_COUNT - valid_count,
        positive_assets=positive_count,
        positive_fraction=positive_count / valid_count if valid_count else None,
        allowed=allowed,
        reason=("allowed" if allowed else "insufficient_valid_assets"
                if not enough_history else "weak_market_breadth"),
    )
    return allowed, diagnostic
