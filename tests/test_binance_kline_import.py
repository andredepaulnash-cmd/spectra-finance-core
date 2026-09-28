from datetime import date, datetime, timedelta, timezone
import zipfile

import pytest

from spectra_core.binance_kline_import import load_archives


def archive(root, base, day, *, missing_minute=None):
    symbol = f"{base}USDT"
    name = f"{symbol}-1m-{day.isoformat()}"
    path = root / f"{name}.zip"
    start = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    rows = []
    for minute in range(1440):
        if minute == missing_minute:
            continue
        opened = (start + minute * 60) * 1_000_000
        closed = opened + 59_999_999
        rows.append(
            f"{opened},10,11,9,10,100,{closed},1000,20,50,500,0"
        )
    with zipfile.ZipFile(path, "w") as target:
        target.writestr(f"{name}.csv", "\n".join(rows) + "\n")
    return path


def test_three_consecutive_days_are_strictly_synchronized(tmp_path):
    first = date(2026, 9, 24)
    paths = [
        archive(tmp_path, base, first + timedelta(days=offset))
        for base in ("AAA", "BBB")
        for offset in range(3)
    ]
    dataset = load_archives(paths, minimum_assets=2)
    assert dataset.pairs == ("AAA-USDT", "BBB-USDT")
    assert dataset.frames == 4320
    assert dataset.manifest["days"] == (
        "2026-09-24",
        "2026-09-25",
        "2026-09-26",
    )
    assert dataset.manifest["forward_fill"] is False
    assert "bid" in dataset.manifest["not_recorded"]


def test_missing_minute_or_excluded_asset_fails_closed(tmp_path):
    day = date(2026, 9, 24)
    bad = archive(tmp_path, "AAA", day, missing_minute=7)
    with pytest.raises(ValueError, match="1439 rows"):
        load_archives([bad], required_days=1, minimum_assets=1)
    btc = archive(tmp_path, "BTC", day)
    with pytest.raises(ValueError, match="excluded"):
        load_archives([btc], required_days=1, minimum_assets=1)
