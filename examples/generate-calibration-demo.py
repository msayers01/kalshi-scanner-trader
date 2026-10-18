"""Generate explicitly synthetic calibration inputs. No network calls or active config changes.

Run from the repository root: python examples/generate-calibration-demo.py
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from scipy.special import expit, logit

from kst.pricing.weather import climate_day_window_ms
from kst.pricing.weather_calibration import bandwidth


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/calibration-demo-inputs"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    rng = np.random.default_rng(771)
    base = 1_704_067_200_000
    p = rng.uniform(0.02, 0.98, 1200)
    y = rng.random(len(p)) < expit(0.25 + 1.8 * logit(p))
    with (args.output / "markets.jsonl").open("w", encoding="utf-8") as stream:
        for i in range(len(p)):
            row = {
                "ticker": f"DEMO-{i}",
                "event_ticker": f"DEMO-E-{i}",
                "category": "Synthetic A" if i % 2 else "Synthetic B",
                "price": float(p[i]),
                "outcome": int(y[i]),
                "observed_ms": base + i * 7_200_000,
                "close_ms": base + i * 7_200_000 + 3_600_000,
                "settled_ms": base + i * 7_200_000 + 5_400_000,
                "synthetic": True,
            }
            stream.write(json.dumps(row) + "\n")
    rng = np.random.default_rng(678)
    with (args.output / "weather.jsonl").open("w", encoding="utf-8") as stream:
        for i in range(300):
            day = date(2024, 1, 1) + timedelta(days=i)
            start, end = climate_day_window_ms(day, "America/New_York")
            members = 65 + 8 * np.sin(i / 30) + rng.normal(0, 3, 30)
            high = round(rng.choice(members) + 2.5 + rng.normal(0, bandwidth(members, 2, 1)))
            row = {
                "series": "KXHIGHNY",
                "station_id": "KNYC",
                "day": day.isoformat(),
                "issued_ms": start - 6 * 3_600_000,
                "members_max_f": members.tolist(),
                "observed_high_f": high,
                "observed_available_ms": end + 8 * 3_600_000,
                "source": "synthetic final daily report",
                "synthetic": True,
            }
            stream.write(json.dumps(row) + "\n")
    print(
        json.dumps(
            {
                "synthetic": True,
                "markets": str((args.output / "markets.jsonl").resolve()),
                "weather": str((args.output / "weather.jsonl").resolve()),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
