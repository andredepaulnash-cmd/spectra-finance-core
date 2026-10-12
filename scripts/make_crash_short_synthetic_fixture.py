"""Create a small deterministic synthetic scenario; never fetch market data.

Eight flat days establish history. The ninth day falls from 100 to 90; the next
midnight produces the frozen daily CRASH/fresh24h signal. Prices rebound later
so the normal daily exit is exercised. These are invented prices, not a forecast.
"""

import argparse
import json
from pathlib import Path

START = 1767225600
DAY = 86400


def write_fixture(path: Path) -> None:
    with path.open("w") as handle:
        for index in range(12 * 288):
            timestamp = START + index * 300
            price = "100" if index < 8 * 288 or index >= 10 * 288 else "90"
            candle = {key: price for key in ("open", "high", "low", "close")}
            candle.update(volume="1", trades="1")
            handle.write(
                json.dumps(
                    {"timestamp": timestamp, "bars": {"BTC": candle, "ETH": candle}},
                    sort_keys=True,
                )
                + "\n"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    write_fixture(parser.parse_args().output)
