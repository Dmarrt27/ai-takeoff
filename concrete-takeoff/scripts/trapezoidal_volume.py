"""
trapezoidal_volume.py — geometry helpers for sloped / non-rectangular
concrete elements with implied dimensions.

Use this module whenever a section drawing shows a slab whose thickness
varies linearly (a "X% SLOPE" annotation, or a single labeled depth like
'11" TYP' paired with a sloping top/bottom face). Treat such a slab as a
TRAPEZOIDAL PRISM, NOT a rectangular prism.

Volume of a trapezoidal prism:

        V = 0.5 * (d_min + d_max) * L * W

where d_min and d_max are the depths at the thin and thick edges, L is
the plan dimension along which depth varies (the "slope run"), and W is
the perpendicular plan dimension (constant depth direction).

All inputs must use CONSISTENT units within one call. Output volume is
returned in CUBIC YARDS (CY).
"""

from dataclasses import dataclass, field
from typing import Literal, List, Optional

CUBIC_INCHES_PER_CY = 46_656          # 36 in * 36 in * 36 in
CUBIC_FEET_PER_CY = 27


# ---------------------------------------------------------------------------
# Derivation records — every implied dimension MUST be returned wrapped in
# one of these so the takeoff output can show the derivation chain.
# ---------------------------------------------------------------------------

@dataclass
class DerivedDimension:
    value: float
    units: Literal["in", "ft"]
    label: str                 # what the dimension represents
    source: str                # which sheet / section it was read from
    formula: str               # exact arithmetic used
    confidence: Literal["high", "medium", "low"] = "high"
    assumptions: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        tag = f"[{self.confidence.upper()}]"
        return f"{tag} {self.label} = {self.value:g} {self.units}  ({self.formula}; {self.source})"


# ---------------------------------------------------------------------------
# 1. Derive max depth from min depth + slope % + slope run length
# ---------------------------------------------------------------------------

def derive_max_depth_from_slope(
    min_depth: float,
    slope_pct: float,
    slope_run: float,
    units: Literal["in", "ft"] = "in",
    source: str = "current section",
) -> DerivedDimension:
    """
    Compute the deep-end depth of a sloped slab.

    Args:
        min_depth: Thinnest depth, labeled on drawing (e.g. 11 for '11" TYP').
        slope_pct: Slope as percent (e.g. 5.0 for "5% SLOPE").
        slope_run: Horizontal distance along the slope direction.
        units:     Must be the same for min_depth and slope_run.

    Example:
        Wet well floor — 11" TYP at high end, 5% slope across 550" run:
            max = 11 + (0.05 * 550) = 38.5"
    """
    delta = (slope_pct / 100.0) * slope_run
    max_depth = min_depth + delta
    return DerivedDimension(
        value=round(max_depth, 3),
        units=units,
        label="max depth (deep end of slope)",
        source=source,
        formula=(
            f"{min_depth}{units} + ({slope_pct}% x {slope_run}{units}) "
            f"= {min_depth}{units} + {delta:g}{units} = {max_depth:g}{units}"
        ),
        confidence="high",
    )


# ---------------------------------------------------------------------------
# 2. Derive an interior dimension that is NOT labeled on the current section
# ---------------------------------------------------------------------------

def derive_interior_from_outer(
    outer_dim: float,
    deductions: List[tuple],          # list of (value, label) tuples
    units: Literal["in", "ft"] = "in",
    label: str = "interior dimension",
    source_sheet: str = "(specify sheet & section)",
) -> DerivedDimension:
    """
    Subtract wall thicknesses, offsets, sump widths, riser projections,
    or similar deductions from a known outer dimension to recover an
    unlabeled interior dimension.

    Use when the dimension is needed for a takeoff calc but is NOT shown
    on the current section. The deductions list must cite each subtracted
    value with a short label so the derivation is auditable.

    Example (wet well, Section B):
        Slope run = 53'-3" interior - (4'-3" sump offset + 3'-7" step)
                  = 639" - 94" = 545"  -> use as slope run
    """
    total_deduction = sum(v for v, _ in deductions)
    interior = outer_dim - total_deduction
    deduction_str = " + ".join(f"{v:g}{units} ({lbl})" for v, lbl in deductions)
    return DerivedDimension(
        value=round(interior, 3),
        units=units,
        label=label,
        source=source_sheet,
        formula=(
            f"{outer_dim:g}{units} - [{deduction_str}] "
            f"= {outer_dim:g}{units} - {total_deduction:g}{units} = {interior:g}{units}"
        ),
        confidence="high",
    )


# ---------------------------------------------------------------------------
# 3. Trapezoidal prism volume (the main calculation)
# ---------------------------------------------------------------------------

def trapezoidal_prism_volume_cy(
    depth_min: float,
    depth_max: float,
    plan_length: float,        # dimension along which depth varies (slope run)
    plan_width: float,         # perpendicular dimension (constant depth)
    units: Literal["in", "ft"] = "in",
    waste_factor: float = 0.05,
) -> dict:
    """
    Volume in CY for a slab whose thickness varies linearly from
    depth_min to depth_max across plan_length, with constant depth_width
    in the perpendicular direction.

    V_raw  = 0.5 * (d_min + d_max) * L * W
    V_with_waste = V_raw * (1 + waste_factor)
    """
    if depth_min < 0 or depth_max < 0:
        raise ValueError("Depths must be non-negative.")
    if depth_max < depth_min:
        depth_min, depth_max = depth_max, depth_min  # normalize

    avg_depth = 0.5 * (depth_min + depth_max)
    raw_volume = avg_depth * plan_length * plan_width   # units^3

    divisor = CUBIC_INCHES_PER_CY if units == "in" else CUBIC_FEET_PER_CY
    vol_cy = raw_volume / divisor
    vol_cy_with_waste = vol_cy * (1 + waste_factor)

    return {
        "volume_cy": round(vol_cy, 2),
        "volume_cy_with_waste": round(vol_cy_with_waste, 2),
        "average_depth": round(avg_depth, 3),
        "plan_area": round(plan_length * plan_width, 1),
        "waste_factor": waste_factor,
        "formula_string": (
            f"V = 0.5 x ({depth_min:g}{units} + {depth_max:g}{units}) "
            f"x {plan_length:g}{units} x {plan_width:g}{units} "
            f"= {raw_volume:,.1f} {units}^3 "
            f"= {vol_cy:.2f} CY (raw) "
            f"= {vol_cy_with_waste:.2f} CY (+ {int(waste_factor*100)}% waste)"
        ),
    }


# ---------------------------------------------------------------------------
# Worked example — City of Rifle Lift Station wet well sloped floor
# Sheet S4, Section B. Target answer: ~138 CY (raw).
# ---------------------------------------------------------------------------

def _example_wet_well_sloped_floor():
    """
    Demonstrates the full pipeline for the wet well sloped floor:

        - Min depth labeled directly on Section B:  11" TYP
        - Slope labeled directly on Section B:      5% SLOPE
        - Slope run (the 545" plan dimension):      NOT directly labeled on
            Section B; must be derived from the 53'-3" interior dimension
            minus the 4'-3" sump offset and 3'-7" step at the wall.
        - Perpendicular plan dimension (480"):      NOT labeled on Section B;
            taken from the plan view (Section A on the same sheet).

    This is exactly the type of multi-section cross-reference the takeoff
    skill is required to perform.
    """
    # --- Step A: derive the implied 545" slope-run dimension -----------
    slope_run = derive_interior_from_outer(
        outer_dim=53 * 12 + 3,                       # 53'-3" = 639"
        deductions=[(4 * 12 + 3, "sump/step offset"),
                    (3 * 12 + 7, "wall step at sump")],
        label="slope run (plan length over which depth varies)",
        source_sheet="S4 / Section B (interior dim) - offsets read off same section",
    )
    print(slope_run)

    # --- Step B: derive the deep-end depth from slope ------------------
    d_max = derive_max_depth_from_slope(
        min_depth=11.0,
        slope_pct=5.0,
        slope_run=slope_run.value,
        source="S4 / Section B (5% SLOPE annotation)",
    )
    print(d_max)

    # --- Step C: read the perpendicular plan dimension from Section A --
    # 480" (40'-0") — constant-depth direction, not shown on Section B.
    plan_width = DerivedDimension(
        value=480.0,
        units="in",
        label="perpendicular plan width (constant-depth direction)",
        source="S4 / Section A (plan view) — interior chamber width",
        formula="read directly from Section A plan dimensioning",
        confidence="high",
    )
    print(plan_width)

    # --- Step D: trapezoidal volume -----------------------------------
    result = trapezoidal_prism_volume_cy(
        depth_min=11.0,
        depth_max=d_max.value,
        plan_length=slope_run.value,
        plan_width=plan_width.value,
        units="in",
        waste_factor=0.05,
    )
    print(result["formula_string"])
    return result


if __name__ == "__main__":
    _example_wet_well_sloped_floor()
