"""Predictive token analytics — trend analysis and forecasting.

Analyses historical session data to predict:
- Daily/weekly burn rate
- Estimated days until rate-limit hit
- Usage trend (accelerating, steady, decelerating)
- Peak usage hours and recommendations

Uses simple linear regression on daily token counts — no ML deps required.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.token_tracker.session_parser import parse_all_sessions

logger = logging.getLogger(__name__)


@dataclass
class TrendPoint:
    date: str
    tokens: int
    input_tokens: int
    output_tokens: int


@dataclass
class Forecast:
    """Token usage forecast."""

    avg_daily_tokens: float
    avg_daily_input: float
    avg_daily_output: float
    trend: str  # "accelerating" | "steady" | "decelerating" | "insufficient_data"
    trend_slope: float  # tokens/day change rate
    projected_7d_tokens: int
    projected_30d_tokens: int
    session_limit_days_remaining: float | None  # days until 5hr session cap
    weekly_limit_days_remaining: float | None  # days until weekly cap
    peak_hours: list[int]  # top 3 hours of day (UTC) by usage
    recommendations: list[str]


def _linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Simple OLS regression. Returns (slope, intercept)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    ss_xx = sum((x - x_mean) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0, y_mean
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    return slope, intercept


def compute_forecast(
    session_cap: int = 15_000_000,
    weekly_cap: int = 150_000_000,
) -> Forecast:
    """Analyse session data and produce a usage forecast."""
    report = parse_all_sessions()
    daily_usage = report.daily_usage  # list of DailyUsage dataclass

    if not daily_usage or len(daily_usage) < 2:
        return Forecast(
            avg_daily_tokens=0,
            avg_daily_input=0,
            avg_daily_output=0,
            trend="insufficient_data",
            trend_slope=0,
            projected_7d_tokens=0,
            projected_30d_tokens=0,
            session_limit_days_remaining=None,
            weekly_limit_days_remaining=None,
            peak_hours=[],
            recommendations=["Not enough usage data yet. Use the system for a few days."],
        )

    # Build daily points
    points: list[TrendPoint] = []
    for du in daily_usage:
        points.append(TrendPoint(
            date=du.date,
            tokens=du.total_tokens,
            input_tokens=du.input_tokens,
            output_tokens=du.output_tokens,
        ))

    # Sort by date
    points.sort(key=lambda p: p.date)

    # Last 14 days for trend analysis
    recent = points[-14:]
    xs = list(range(len(recent)))
    ys = [float(p.tokens) for p in recent]

    slope, intercept = _linear_regression(xs, ys)

    # Classify trend
    avg_daily = sum(ys) / len(ys) if ys else 0
    if avg_daily == 0:
        trend = "insufficient_data"
    elif abs(slope) < avg_daily * 0.05:
        trend = "steady"
    elif slope > 0:
        trend = "accelerating"
    else:
        trend = "decelerating"

    avg_input = sum(p.input_tokens for p in recent) / len(recent) if recent else 0
    avg_output = sum(p.output_tokens for p in recent) / len(recent) if recent else 0

    # Projections
    proj_7d = int(avg_daily * 7 + slope * 7 * 3.5)  # linear extrapolation
    proj_30d = int(avg_daily * 30 + slope * 30 * 15)

    # Estimate days until limits
    session_days = None
    weekly_days = None
    if avg_daily > 0:
        # Weekly cap: how many days at current rate until weekly_cap
        weekly_days = max(0, round(weekly_cap / avg_daily, 1))
        # Session cap per 5hr window: assume ~3 sessions/day
        session_days = max(0, round(session_cap / (avg_daily / 3), 1)) if avg_daily > 0 else None

    # Peak hours analysis
    hour_totals: dict[int, int] = defaultdict(int)
    for q in report.queries:
        try:
            ts = datetime.fromisoformat(q.timestamp.replace("Z", "+00:00"))
            hour_totals[ts.hour] += q.input_tokens + q.output_tokens
        except Exception:
            continue
    peak_hours = sorted(hour_totals, key=hour_totals.get, reverse=True)[:3]  # type: ignore[arg-type]

    # Recommendations
    recs: list[str] = []
    if trend == "accelerating":
        recs.append("Usage is increasing — consider batching prompts to reduce token waste.")
    if avg_daily > weekly_cap / 7 * 0.8:
        recs.append("You're using >80% of estimated weekly capacity. Space out heavy sessions.")
    if peak_hours and len(peak_hours) >= 2:
        recs.append(f"Peak hours (UTC): {', '.join(f'{h}:00' for h in peak_hours)}. Spread work to avoid session-limit spikes.")
    if avg_output > avg_input * 2:
        recs.append("Output tokens are 2x+ input — consider shorter system prompts to reduce output verbosity.")
    if not recs:
        recs.append("Usage looks healthy. Keep it up!")

    return Forecast(
        avg_daily_tokens=round(avg_daily),
        avg_daily_input=round(avg_input),
        avg_daily_output=round(avg_output),
        trend=trend,
        trend_slope=round(slope, 1),
        projected_7d_tokens=max(0, proj_7d),
        projected_30d_tokens=max(0, proj_30d),
        session_limit_days_remaining=session_days,
        weekly_limit_days_remaining=weekly_days,
        peak_hours=peak_hours,
        recommendations=recs,
    )


def forecast_to_dict(f: Forecast) -> dict[str, Any]:
    """Serialize a Forecast to a JSON-safe dict."""
    return {
        "avg_daily_tokens": f.avg_daily_tokens,
        "avg_daily_input": f.avg_daily_input,
        "avg_daily_output": f.avg_daily_output,
        "trend": f.trend,
        "trend_slope": f.trend_slope,
        "projected_7d_tokens": f.projected_7d_tokens,
        "projected_30d_tokens": f.projected_30d_tokens,
        "session_limit_days_remaining": f.session_limit_days_remaining,
        "weekly_limit_days_remaining": f.weekly_limit_days_remaining,
        "peak_hours": f.peak_hours,
        "recommendations": f.recommendations,
    }
