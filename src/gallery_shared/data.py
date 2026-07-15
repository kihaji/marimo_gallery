"""Synthetic datasets shared by gallery notebooks."""

from __future__ import annotations

import numpy as np
import pandas as pd

from gallery_shared.storage import get_cache

REGIONS = ["North", "South", "East", "West"]
CATEGORIES = ["Hardware", "Software", "Services", "Support"]


def load_sales(seed: int = 42, days: int = 365) -> pd.DataFrame:
    """A deterministic synthetic sales dataset (~one row per region/category/day),
    cached across sessions via the shared cache."""

    def _build() -> pd.DataFrame:
        rng = np.random.default_rng(seed)
        dates = pd.date_range(end=pd.Timestamp.today().normalize(), periods=days)
        frames = []
        for r_i, region in enumerate(REGIONS):
            for c_i, category in enumerate(CATEGORIES):
                base = 1000 + 400 * r_i + 250 * c_i
                trend = np.linspace(0, base * 0.3, days)
                season = base * 0.15 * np.sin(np.arange(days) * 2 * np.pi / 91)
                noise = rng.normal(0, base * 0.1, days)
                revenue = np.clip(base + trend + season + noise, 50, None)
                frames.append(
                    pd.DataFrame(
                        {
                            "date": dates,
                            "region": region,
                            "category": category,
                            "revenue": revenue.round(2),
                            "units": np.maximum(1, (revenue / rng.uniform(40, 90))).astype(int),
                        }
                    )
                )
        return pd.concat(frames, ignore_index=True)

    return get_cache("data").get_or_compute(f"sales:{seed}:{days}", _build, ttl=24 * 3600)
