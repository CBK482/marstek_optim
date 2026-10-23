"""House-load forecast from P1 history (15-min slot-of-day profile)."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import numpy as np


def bucketize(samples: Sequence[tuple[datetime, float]], tz) -> dict[datetime, float]:
    """Mean value per 15-min bucket (keys in UTC)."""
    acc: dict[datetime, list[float]] = defaultdict(list)
    for ts, v in samples:
        if v is None or not math.isfinite(v):
            continue
        u = ts.astimezone(timezone.utc)
        key = u.replace(minute=u.minute - u.minute % 15, second=0, microsecond=0)
        acc[key].append(v)
    return {k: float(np.mean(v)) for k, v in acc.items()}


def default_profile(base_w: float) -> np.ndarray:
    h = np.arange(96) / 4.0
    return (base_w + 0.7 * base_w * np.exp(-((h - 7.5) ** 2) / 1.5)
            + 2.2 * base_w * np.exp(-((h - 18.8) ** 2) / 2.2)
            + 0.4 * base_w * np.exp(-((h - 12.5) ** 2) / 3))


def forecast_load(slot_starts: Sequence[datetime], samples: Sequence[tuple[datetime, float]], tz,
                  base_w: float = 350.0, current_w: Optional[float] = None, days: int = 14) -> np.ndarray:
    """Weighted slot-of-day profile (recent days weigh more, weekday/weekend
    aware), blended with the current measured load for the first hour."""
    buckets = bucketize(samples, tz)
    now = datetime.now(timezone.utc)
    prof_sum = np.zeros((2, 96))
    prof_w = np.zeros((2, 96))
    for k, v in buckets.items():
        age_d = (now - k).total_seconds() / 86400
        if age_d > days or age_d < 0:
            continue
        loc = k.astimezone(tz)
        idx = loc.hour * 4 + loc.minute // 15
        we = 1 if loc.weekday() >= 5 else 0
        w = math.exp(-age_d / 5.0)
        prof_sum[we, idx] += w * v
        prof_w[we, idx] += w
    fallback = default_profile(base_w)
    combined_w = prof_w.sum(axis=0)
    combined = np.where(combined_w > 0, prof_sum.sum(axis=0) / np.maximum(combined_w, 1e-9), fallback)

    out = np.zeros(len(slot_starts))
    for i, st in enumerate(slot_starts):
        loc = st.astimezone(tz)
        idx = loc.hour * 4 + loc.minute // 15
        we = 1 if loc.weekday() >= 5 else 0
        if prof_w[we, idx] >= 1.5:  # enough same-type-day data
            val = prof_sum[we, idx] / prof_w[we, idx]
        else:
            val = combined[idx]
        if current_w is not None:
            a = math.exp(-i / 3.0)  # persistence for the next ~hour
            val = a * current_w + (1 - a) * val
        out[i] = val
    return out
