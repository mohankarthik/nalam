"""One small pure-Python SVG line chart for an analyte's trend.

No chart.js, no CDN -- this is a LAN-only app and the container may have no
outbound internet at all. A polyline over plotted points is all a trend
needs; the codebook's reference band is drawn behind it so a value's
distance from "normal" is visible at a glance.
"""

from __future__ import annotations

from typing import Optional

WIDTH = 640
HEIGHT = 160
PAD = 24


def sparkline_svg(
    points: list[tuple[str, float]],
    ref_low: Optional[float] = None,
    ref_high: Optional[float] = None,
) -> str:
    """`points` is [(date, value), ...] in chronological (oldest-first) order.
    Returns a standalone <svg> string, or an empty string if there is nothing
    to plot -- a single point has no trend to draw."""
    if len(points) < 2:
        return ""

    values = [v for _, v in points]
    lo = min([*values, ref_low if ref_low is not None else values[0]])
    hi = max([*values, ref_high if ref_high is not None else values[0]])
    if lo == hi:
        lo, hi = lo - 1, hi + 1

    span_x = WIDTH - 2 * PAD
    span_y = HEIGHT - 2 * PAD
    n = len(points)

    def x_at(i: int) -> float:
        return PAD + (i / (n - 1)) * span_x

    def y_at(v: float) -> float:
        return PAD + span_y - ((v - lo) / (hi - lo)) * span_y

    coords = " ".join(f"{x_at(i):.1f},{y_at(v):.1f}" for i, (_d, v) in enumerate(points))

    band = ""
    if ref_low is not None and ref_high is not None:
        y_top = y_at(ref_high)
        y_bot = y_at(ref_low)
        band = (
            f'<rect x="{PAD}" y="{y_top:.1f}" width="{span_x}" '
            f'height="{(y_bot - y_top):.1f}" class="ref-band" />'
        )

    dots = "".join(
        f'<circle cx="{x_at(i):.1f}" cy="{y_at(v):.1f}" r="3" class="pt" />'
        for i, (_d, v) in enumerate(points)
    )

    return (
        f'<svg viewBox="0 0 {WIDTH} {HEIGHT}" class="trend" '
        f'preserveAspectRatio="none" role="img">'
        f"{band}"
        f'<polyline points="{coords}" class="line" fill="none" />'
        f"{dots}"
        f"</svg>"
    )
