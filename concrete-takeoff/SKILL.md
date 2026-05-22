---
name: concrete-takeoff
description: >
  Perform concrete quantity takeoffs from construction plans and drawings. Use this skill whenever
  the user uploads or references construction drawings, PDFs, or plans and wants to extract,
  calculate, or estimate concrete volumes, rebar quantities, or formwork areas. Triggers include
  any mention of "takeoff", "quantity takeoff", "concrete quantities", "estimate concrete",
  "how much concrete", "rebar quantities", "formwork", or when a user shares a plan/drawing
  and asks what materials are needed. Also use for civil/infrastructure and industrial projects
  involving footings, slabs, walls, columns, beams, or piers. Always use this skill when
  construction drawings are present and quantities are needed — even if the user just says
  "what do I need for this" or "can you pull quantities from this."
---

# Concrete Quantity Takeoff Skill

You are performing a professional concrete quantity takeoff for civil/infrastructure and industrial construction projects. Your job is to extract and calculate concrete volumes (CY), rebar quantities (lbs), and formwork areas (SF) from uploaded PDF drawings or user-provided dimensions.

---

## Workflow

### Step 1 — Ingest and Orient

When the user provides drawings (PDF or image):
1. Identify all sheets present: plan views, sections, elevations, detail sheets
2. Note the drawing scale on each sheet (e.g., 1"=10', 1:50). If no scale bar is visible, note this and work with labeled dimensions only
3. List the structural elements visible that contain concrete (footings, slabs, walls, columns, beams, piers, caissons)
4. Confirm your understanding with a brief one-paragraph summary before calculating

### Step 1.5 — Drawing Review Sequence (Martinez Western Workflow)

This is the canonical sheet-review order used by Martinez Western Constructors. Walk through these sheets in this order BEFORE extracting dimensions — each pass surfaces information later passes depend on. **Every cross-section detail referenced from a plan view must be opened and read** — section details routinely contain dimensions that the plan view itself does not show. The mechanics for spotting a cross-section callout, navigating to the referenced sheet, and pairing section dimensions with plan dimensions are formalized in **Step 1.7 — Cross-Section Callout Navigation Protocol**.

#### A. Cover Sheet
- Review 3D renderings if present
- Count and note: number of floors, balconies, overhangs, parking levels, patios, decks
- Capture anything you want to be sure to find later

#### B. Drawing Index
- Verify completeness — no gaps in sheet numbering, all referenced sheets present
- Flag gaps before proceeding

#### C. Architectural Drawings
- Read **all four exterior elevations**
- Note floors, decks, patios, floor types per level
- These sheets carry the **finished floor elevations**

#### D. Building Sections & Wall Sections
Cross sections will typically show:
- Footings (continuous and pad)
- Walls (with top-of-wall heights)
- Slab on grade
- Slab on metal deck
- Structural or post-tensioned suspended slabs
- Concrete floors over wood-framed assemblies

#### E. Stair & Elevator Section Plans
If present, these provide concrete floor types and dimensions for stair landings and elevator pits/shafts.

#### F. Structural Plans (S-sheets)
This is where the dimensioned concrete information lives. Process in this order:
1. **General notes** — concrete mix required at various components, reinforcing lap and development lengths
2. **Footing schedule** — dimensions for each footing type, keyed to the identifier shown on the foundation plan
3. **Foundation plan** — start the takeoff here (footings are placed first in construction sequence)

> **For concrete quantities, almost all important dimensions live on S-sheets.** Always cross-check against A-sheet elevations and details before calculating (see Step 2 — Extract Dimensions).

### Step 1.6 — Takeoff Order (Construction Sequence)

Take off elements in the order they will be built, level by level. This catches step-change interactions — e.g. a step in a continuous footing changes the height of the wall above it — that are routinely missed when working strictly by element type.

#### Foundation level — process in this exact order

1. **Continuous footings** — dimension length, width, depth by type
   - Outputs: SF formed surface, SF finished surface, neat CY of concrete
   - **Identify every step** in the continuous footing — each step is extra forming, extra concrete, AND a change in wall height above

2. **Footing pads / isolated footings** — dimension length, width, depth, and total pad count
   - Outputs: SF formed surface, SF finished surface, neat CY of concrete
   - Note foundations requiring columns and/or anchor bolts

3. **Walls** — measure continuously where height and width are constant
   - Wall height breaks at: footing steps, top-of-wall changes, floor-type changes above
   - Top of wall is usually called out on the foundation sheet; if not, the wall extends to the next level
   - Walls spanning multiple levels: if continuous through the upper level → measure full height; if it terminates and a new wall starts on the slab above → measure each segment separately
   - **Doors, windows, blockouts measured separately** so concrete CY can be deducted — BUT measure the wall to its full height so forming includes the two sides of each opening
   - Outputs: total SF of forming (openings included in forming), total CY before deducting blockout volume, blockout deduction CY
   - If structural steel is present, count embed plates that bolt to or embed in the wall

4. **Concrete columns** — count and dimension (height, width / diameter) for forming SF and CY

5. **Interior slab on grade** (less any blockouts)
   - Separate different concrete sections (different mix, thickness, or finish)
   - Measure SF and thickness
   - Measure perimeters and any locations where a wall-expansion or thickened-edge detail is shown
   - Identify and measure **sumps and floor trench drains** as their own line items

#### Upper levels (Level 1, 2, 3, …)

Repeat the foundation-level passes for every subsequent level. Additional considerations:

- **Cast-in-place structural floor**: requires shoring — list shoring quantities separately from slabs placed on metal deck
- **Steps in structural slabs**: identify and count separately
- **Slabs on metal deck (composite)**: measure deck plan area and topping thickness; no shoring line

### Step 1.7 — Cross-Section Callout Navigation Protocol (Mandatory)

Cross-section callouts are the mechanism by which a plan view tells you "the complete detail for this cut lives on another sheet." You must learn to recognize them on sight, navigate to the referenced sheet to read the section, then return to the plan to pair dimensions. **The plan view alone never contains enough information to compute concrete volume** — plan shows length and width in plan; cross-section shows heights, depths, thicknesses, embedments, layers, and reinforcement. Both are required for every element.

#### A. Recognizing a cross-section callout on a plan or elevation

A cross-section callout appears as:
- A **circle (or diamond) divided into two halves** by a horizontal line
- The **top half** carries a letter or number identifier — `A`, `B`, `C`, `1`, `2`, …
- The **bottom half** carries the sheet reference where the section is drawn — typically a structural sheet such as `S5`, `S6`
- A **triangular arrow / flag** attached to the circle points in the direction the cut is "looking" (which side of the cutting plane the section view faces)
- A short tick or filled rectangle marks the **cutting-plane location** — the exact line along the plan where the section is sliced
- Often a `TYP` note nearby — meaning the same cross-section applies at every similar condition

Common forms you will see:
| Marker | Reads as |
|---|---|
| `A / S5` | "Section A is drawn on Sheet S5" |
| `B / S5  TYP` | "Section B (typical condition) is drawn on Sheet S5" |
| `1 / S6` | "Detail 1 is drawn on Sheet S6" |
| `3 / A4.1` | Detail callouts can also reference architectural sheets |

Two marker arrows pointing at each other across a structure indicate the **start and end of a cutting plane line** — both arrows reference the same section.

#### B. Navigation workflow — what to do every time you spot a callout

1. **On the plan view**, record before navigating away:
   - Callout ID (e.g., `B / S5`)
   - Which structural element the cut passes through (footing, wall, slab, beam)
   - Direction the arrow faces (orients the section view)
   - All **plan-view dimensions** along and across the cut line — these are the L / W you will multiply by the section's depths

2. **Navigate to the referenced sheet** (e.g., Sheet S5)
   - Locate the section drawing labeled with the matching identifier (e.g., `SECTION B`)
   - Confirm the marker letter inside its title bubble matches the callout you came from

3. **On the section drawing, extract every dimension visible**:
   - Vertical heights — wall H, footing depth, slab T
   - Top-of-wall and bottom-of-footing elevations
   - Layer thicknesses — slab on grade, topping, waterstop, starter wall
   - Steps, keys, dowels, batter, taper
   - Reinforcement size and spacing, mat positions (TOP / BTM, EF / IF, EW)
   - Clear-cover dimensions (affects no volume but flags rebar placement)

4. **Return to the original plan view** carrying the section's dimensions
   - Pair `plan length × plan width × section depth/thickness` in the volume formula
   - Use section height for walls, section depth for footings, section thickness for slabs
   - Pair the section's rebar callouts (spacing, size) with plan length to compute LF of bar

5. **If the section is marked `TYP`**, the section dimensions apply at every visually identical condition on the plan. Do not re-navigate for each, BUT verify each location actually matches — corner conditions, terminations, step locations, and openings are usually NOT typical and have their own callouts.

#### C. Which dimension lives in which view

| Dimension | Read from | Notes |
|---|---|---|
| Length of footing/wall along structure | Plan view | Measured between plan dimension lines |
| Footprint width in plan | Plan view | Confirm against section if both shown |
| Wall thickness | **Cross-section** | Plan often shows centerline only |
| Footing depth / slab thickness | **Cross-section** | Never shown on plan |
| Wall height (top-of-footing → top-of-wall) | **Cross-section** + A-sheet elevations | Cross-check with finished-floor elevations |
| Footing-to-wall key / dowel detail | **Cross-section** | Affects rebar takeoff, sometimes adds a small CY for the key |
| Reinforcement size & spacing | **Cross-section** | Plan rarely shows bar callouts directly |
| Top-of-wall variation | Plan callout + cross-section | Plan calls TOW elevation; section confirms height build-up |
| Step locations along a footing | Plan view | Section shows what each step looks like; plan shows where it occurs |
| Count of elements | Plan view | Number of footings, columns, walls |
| Waterstop / blockout details | **Cross-section** | Affects layout and embeds, not usually CY |

#### D. The mental model

Every cast-in-place concrete element is a **3D solid**. The plan view shows its footprint in the X-Y plane; the cross-section shows its profile in the X-Z (or Y-Z) plane. Volume requires all three axes — so until both views have been read, the element does not have enough data for volume math. **Never start CY calculations on an element whose cross-section has not been opened and read.** If a callout points to a sheet that is missing from the set, flag it and stop the takeoff for that element rather than guessing depths from typical values.

#### E. Example — typical retaining-wall callout pair

Plan view (Sheet S2, Foundation Plan) shows a continuous wall footing with callout `B / S5 TYP` pointing perpendicular to the wall. Plan dimensions give a wall run of 64'-0".

Navigate to Sheet S5, find `SECTION B`. Read:
- Footing depth: **1'-6"**; footing extends 3'-3" one side and 8'-6" the other from wall centerline
- Wall height (top of footing → top of wall): **10'-8"**
- Wall thickness: **1'-8"**, with 2" clear cover
- Wall reinforcement: #6 at 7" O.C. vertical EF, #6 at 7" O.C. horizontal EF
- Footing top mat: #6 at 8" O.C. EW; bottom mat: #6 at 5" O.C. EW
- Dowels from footing to wall: #6 at 7" O.C. EF
- Hydrophilic waterstop at top of wall (no CY impact, note for embeds)

Return to S2 and compute, pairing plan length with section dimensions:

```
Wall CY:      64' × 10.667' × 1.667' ÷ 27   = 42.16 CY
Footing CY:   64' × (3.25' + 1.667' + 8.5') × 1.5' ÷ 27   (footing width built up from section)
```

Because the callout is `TYP`, reuse these section dimensions for every visually identical wall on the foundation plan — but re-measure plan length for each run, and look for corner / step callouts that override the typical.

#### F. Adjacent sections on the same sheet

When a referenced sheet (e.g., S5) holds multiple sections (e.g., Section A and Section B on the same page), read **both** before leaving:
- Adjacent sections often share dimensions with one shown on Section A and another on Section B (e.g., the perpendicular interior chamber width may only be dimensioned on Section A, while Section B labels everything else). This is the same cross-referencing pattern formalized in Step 2.6.
- Note any geometric differences between the sections — different wall heights, different reinforcement, different footing widths — these signal that the structure changes condition between the two cut locations.

### Step 2 — Extract Dimensions

For each element, extract or derive:
- **Length** (L), **Width** (W), **Depth/Thickness** (D or T)
- If dimensions are labeled on the drawing: use them directly
- If dimensions must be scaled: use the scale bar or stated scale ratio to derive dimensions from the drawing geometry. Always note when a dimension is scaled vs. labeled.
-Cross check dimensions with other "S" sheets to verify all dimensions are correct before moving to calculations
-Note that not all concrete sections are perfectly rectangular, be aware of these sections
- **Never assume the layout is symmetrical.** Do not infer a dimension by mirroring the opposite side, and do not assume the structure is a clean rectangle. Opposite walls are frequently different lengths, footings differ side to side, interior chambers are unequal, and slab edges step in and out. Read the dimension callout for EVERY side of EVERY element individually — if a callout is genuinely missing, derive it per Step 2.6, never copy it from a "matching" element.
-For concrete quantities, almost all important dimensions will be listed on "S" sheets
- Record each element with a unique identifier (e.g., F1, F2, W1, S1, C1)

### Step 2.5 — Classify Geometry BEFORE Calculating (Mandatory)

Before applying any volume formula you MUST output a one-line geometry classification for every element. Default-treating everything as rectangular is the #1 cause of takeoff errors on this skill — sloped slabs, stepped footings, and tapered walls are repeatedly under- or over-counted when this step is skipped.

For each element, classify as ONE of:

| Tag | When to use | Volume model |
|---|---|---|
| `RECT_PRISM` | Constant thickness in all directions | L × W × T |
| `TRAPEZOIDAL_PRISM` | Thickness varies LINEARLY across one plan dimension (e.g. sloped floor of a wet well, tapered mat, sumps with sloped bottom) | 0.5 × (d_min + d_max) × L × W |
| `STEPPED_PRISM` | Thickness changes in discrete steps | Sum of rectangular sub-volumes |
| `TAPERED_WALL` | Wall thickness varies from top to bottom (battered) | 0.5 × (t_top + t_bot) × H × L |
| `CYLINDER` | Round column / caisson | π × r² × H |
| `FRUSTUM` | Round pier with varying radius (rare) | (π × H / 3) × (R² + R·r + r²) |
| `CUSTOM` | Anything else — break into sub-shapes and document |

**Hard rule — trigger words for `TRAPEZOIDAL_PRISM`:** If the section drawing contains ANY of the following, the element is trapezoidal, NEVER rectangular:

- A `% SLOPE` annotation (e.g. "5% SLOPE", "2% SLOPE TO DRAIN")
- A single labeled thickness with `TYP` next to a non-horizontal top or bottom face
- Two different thicknesses labeled at opposite ends of the same slab
- A "slope to drain", "slope to sump", "create slope" note
- A leader line showing depth at one end and a separate leader showing depth at the other end
- The bottom of the slab is flat (foundation requirement) but the top is sloped, or vice versa

When any of those are present, output the classification with the exact trigger you saw:
> Element WW-FL: `TRAPEZOIDAL_PRISM` (trigger: "5% SLOPE" annotation on Section B; min thickness "11\" TYP")

### Step 2.6 — Derive Implied Dimensions from Cross-Referenced Sheets (Mandatory)

It is **routine** for a section drawing to omit one or more dimensions required by the volume formula. The omitted dimension must be DERIVED, with the derivation chain shown, before any volume math runs. Never substitute a guess or "reasonable default" for an implied dimension.

A dimension is **implied** (not "shown") when any of these are true:

1. The dimension is needed for the volume formula but no leader/arrow on the current section labels it directly
2. The current section shows only outer-to-outer dimensions; you need an interior dimension (subtract wall thicknesses)
3. The current section shows a span but the slope only acts on a portion of that span (subtract sump width / step offset / wall projection)
4. The dimension you need is in a different orientation than this section view (look on the plan view or perpendicular section on the same sheet)
5. A `TYP` callout implies one dimension and the other end requires slope-based derivation

**Derivation protocol — show all four lines for every implied dimension:**

```
Implied dim   : <label, e.g. "slope-run length for wet well floor">
Needed for    : <which volume calc>
Source        : <sheet ID / section ID where each ingredient was read>
Derivation    : <explicit arithmetic — outer minus walls minus offsets, or
                 min_depth + (slope% × run), etc.>
```

Examples of the two most common derivations on civil/industrial work:

| Pattern | Derivation |
|---|---|
| Max depth of a sloped slab | `d_max = d_min + (slope% / 100) × slope_run` |
| Interior chamber dimension | `D_int = D_outer - Σ(wall thicknesses + offsets)` |
| Slope run when slab doesn't cover full span | `run = interior_span - sump_width - step_offsets` |
| Perpendicular plan dim missing from this section | Read from plan view (Section A) or opposite section on same sheet |

Cross-reference rule: when a dimension is missing on Section B, **look at Section A and any plan/key views on the same sheet number before scaling**. Scaling is the last resort and must be flagged as `confidence: low`. The full callout-to-section-and-back navigation flow is documented in Step 1.7; this step focuses on what to do once you are reading the section and need a dimension the section omits.

A reusable Python helper for these computations is bundled at:
`concrete-takeoff/scripts/trapezoidal_volume.py` — see the `_example_wet_well_sloped_floor()` block for a fully worked derivation chain that produces 138 CY on the City of Rifle wet well floor.

### Step 3 — Calculate Quantities

Apply the following formulas. Always show your math inline so the user can verify.

#### Concrete Volume
Convert all dimensions to feet first, then:

| Element | Formula |
|---|---|
| Rectangular footing | L × W × D ÷ 27 = CY |
| Continuous wall footing | L × W × D ÷ 27 = CY |
| Slab on grade (constant thickness) | L × W × T ÷ 27 = CY |
| **Sloped slab / trapezoidal prism** | **0.5 × (d_min + d_max) × L × W ÷ 27 = CY** |
| Stepped slab / footing | Σ (L_i × W_i × T_i) ÷ 27 = CY |
| Retaining / shear wall (constant) | L × H × T ÷ 27 = CY |
| Tapered (battered) wall | 0.5 × (t_top + t_bot) × H × L ÷ 27 = CY |
| Column (rectangular) | W × D × H ÷ 27 = CY |
| Column (round) | π × r² × H ÷ 27 = CY |
| Beam | W × D × L ÷ 27 = CY |
| Pier / caisson (round) | π × r² × H ÷ 27 = CY |
| Pier / caisson (rectangular) | L × W × H ÷ 27 = CY |
| Frustum (varying-radius pier) | (π × H / 3) × (R² + R·r + r²) ÷ 27 = CY |

> **Unit hygiene for trapezoidal calcs:** if any input dimension is in inches, divide the raw cubic-inch product by **46,656** (not 27) to get CY. Mixing inches and feet in the same formula is the second-most-common error after rectangular default — always convert first.

Add a **5% waste/overbreak factor** to all volumes unless the user specifies otherwise.

#### Rebar (Reinforcing Steel)
If rebar is shown or specified on the drawings:
- Count bars, note size (e.g., #4, #5, #6) and spacing
- Calculate total linear feet per bar size
- Apply standard weight factors (lbs/LF): see reference table below
- Sum total weight in **lbs**, and also express as **tons** (÷ 2000)

If rebar is not shown but element type is known, note that rebar was not quantified for that element.

**Standard rebar weights (lbs per linear foot):**
| Bar Size | lbs/LF |
|---|---|
| #3 | 0.376 |
| #4 | 0.668 |
| #5 | 1.043 |
| #6 | 1.502 |
| #7 | 2.044 |
| #8 | 2.670 |
| #9 | 3.400 |
| #10 | 4.303 |
| #11 | 5.313 |

#### Formwork (Surface Area)
Calculate contact area (SF) for all formed surfaces — surfaces that require a form to retain concrete during pour:

| Element | Formed Surfaces |
|---|---|
| Isolated footing | 4 sides (L×D×2 + W×D×2) |
| Continuous footing | 2 long sides (L×D×2) |
| Slab on grade | Edge forms only (perimeter × T) |
| Wall | 2 faces (L×H×2) |
| Column (rect.) | 4 faces (perimeter × H) |
| Column (round) | Circumference × H |
| Beam (formed soffit) | Bottom + 2 sides |
| Pier/caisson | Typically not formed (drilled); note if casing used |

Do **not** include top surfaces (screeded/finished, not formed) or surfaces cast against earth (SOG bottom, pile sides in soil).

---

### Step 4 — Organize Output by Element Type

Group all quantities by structural element type in this order:
1. Foundations & Footings
2. Slabs on Grade
3. Walls (Retaining / Shear)
4. Columns & Beams
5. Piers & Caissons

Within each group, list individual elements (F1, F2, etc.), then a **subtotal** for that group.

End with a **Project Total** summary table.

---

### Step 5 — Output Format

Produce three outputs:

#### A) Summary Narrative
One paragraph describing what was found, any scaling assumptions made, elements that lacked full dimension data, and any notes the estimator should verify.

#### B) Structured Quantity Table (inline)
Present a clean markdown table with columns:

| Element ID | Description | L (ft) | W (ft) | D/H (ft) | Count | Concrete (CY) | Rebar (lbs) | Formwork (SF) | Notes |
|---|---|---|---|---|---|---|---|---|---|

Include subtotals per group and a project total row.

#### C) Downloadable Spreadsheet
After presenting the table inline, generate an `.xlsx` file using the xlsx skill with:
- Sheet 1: Quantity takeoff table (same as above, formatted)
- Sheet 2: Assumptions & Notes log
- Sheet 3: Rebar schedule (if rebar was quantified)

To generate the spreadsheet, read `/mnt/skills/public/xlsx/SKILL.md` first.

---

## Handling Scale Calculations

When dimensions must be scaled from the drawing:
1. Identify the scale bar or stated scale (e.g., "1 inch = 20 feet" or "1:100")
2. If the drawing is a PDF rendered as an image, estimate pixel lengths of the scale bar vs. the element to derive a ratio
3. Always state: *"Dimension scaled from drawing at [scale]. Verify against labeled dimensions."*
4. Flag any element where no scale and no labeled dimension is available — estimate based on context (e.g., standard footing depths) and note clearly as an assumption

---

## Assumptions & Defaults

Apply these defaults unless the user specifies otherwise:
- Concrete waste/overbreak: **+5%**
- Rebar lap splices: add **15%** to calculated bar lengths
- Formwork: use **contact area** method (no deductions for openings < 10 SF)
- Units: all volumes in **cubic yards (CY)**, weights in **lbs** (also shown as tons), areas in **square feet (SF)**
- If an element count is not explicit, assume **1** and note it

---

## Quality Checks

Before presenting output, verify:

**Drawing review (Step 1.5):**
- [ ] Cover sheet reviewed; floor count and exterior features (balconies, overhangs, parking, patios, decks) captured
- [ ] Drawing index reviewed for completeness — no missing sheet numbers
- [ ] All four architectural exterior elevations reviewed; finished floor elevations captured
- [ ] All building sections and wall sections reviewed
- [ ] Stair/elevator sections reviewed (if present)
- [ ] **Every cross-section detail referenced from a plan view has been opened and read**
- [ ] For each callout: marker ID, sheet reference, cut location, and arrow direction were recorded on the plan before navigating away (Step 1.7)
- [ ] No element has volume math run until both its plan view AND its referenced cross-section have been read
- [ ] `TYP` callouts verified at every location (corners, terminations, steps re-checked for non-typical conditions)
- [ ] When a referenced sheet holds multiple sections (e.g., Section A and Section B on S5), both were read for cross-referenced dimensions
- [ ] Structural general notes captured (concrete mix specs, rebar laps and development)
- [ ] Footing schedule cross-referenced with the foundation plan

**Construction-sequence takeoff (Step 1.6):**
- [ ] Continuous footings separated from isolated pad footings
- [ ] Every step in continuous footings counted; wall-height changes propagated above
- [ ] Walls measured continuously between height/width changes only
- [ ] Wall openings (doors/windows/blockouts) measured as separate deductions; forming SF still includes the opening sides
- [ ] Embed plates counted on walls that touch structural steel
- [ ] Sumps and trench drains measured as separate SOG line items
- [ ] Each level processed in construction sequence (footings → walls → columns → SOG → sumps)
- [ ] Cast-in-place structural decks (require shoring) distinguished from composite metal-deck slabs (no shoring)

**Geometry & math:**
- [ ] No dimension was assumed by symmetry — every side of every element has its own callout read or derived (layouts are routinely asymmetrical)
- [ ] Every element has an explicit geometry classification (Step 2.5) — no element silently treated as rectangular
- [ ] Any element with a `% SLOPE` annotation, "TYP" thickness paired with a sloping face, or two different end thicknesses is classified as `TRAPEZOIDAL_PRISM` and computed with `0.5 × (d_min + d_max) × L × W`
- [ ] Every implied dimension has a 4-line derivation block (label / needed-for / source / derivation arithmetic)
- [ ] Inch-based trapezoidal volumes divided by 46,656 (not 27); foot-based by 27
- [ ] All CY calculations divided by the correct cubic-units-to-CY constant
- [ ] Round columns use π × r² (not diameter²)
- [ ] Waste factor applied to all volumes
- [ ] Rebar lap splice factor applied
- [ ] Formwork excludes earth-formed and top/finished surfaces
- [ ] Subtotals and project total match sum of line items

---

## Worked Example — Wet Well Sloped Floor (Reference Case)

This is the canonical example for sloped-slab handling. Whenever you see a wet well, vault, sump, or any slab with a `% SLOPE` annotation, follow this exact derivation pattern.

**Drawing inputs (City of Rifle Lift Station, Sheet S4, Section B):**
- `11" TYP` — minimum slab thickness at high end of slope (labeled)
- `5% SLOPE` — slope annotation on the top face of the slab (labeled)
- `53'-3"` — interior dimension of the wet well along the slope direction (labeled)
- `4'-3"` and `3'-7"` — sump offset and wall-step segments at the low end (labeled)
- Perpendicular plan dimension: NOT labeled on Section B — must be read from Section A (plan view) on the same sheet → 480"

**Step 2.5 — Geometry classification:**
> Element WW-FL: `TRAPEZOIDAL_PRISM` (trigger: "5% SLOPE" annotation; min thickness "11\" TYP" at one end)

**Step 2.6 — Implied dimension derivations:**

```
Implied dim   : Slope-run length (the plan dim across which depth varies)
Needed for    : L in V = 0.5 × (d_min + d_max) × L × W
Source        : S4 / Section B — interior 53'-3" minus the 4'-3" sump offset
                and 3'-7" wall step at the low end
Derivation    : 53'-3" − (4'-3" + 3'-7") = 639" − 94" = 545"
```

```
Implied dim   : Maximum slab depth (deep end of the slope)
Needed for    : d_max in trapezoidal volume formula
Source        : S4 / Section B — "5% SLOPE" annotation, applied across 545" run
Derivation    : 11" + (0.05 × 545") = 11" + 27.25" = 38.25"   (≈ 38.5")
```

```
Implied dim   : Perpendicular plan width (constant-depth direction)
Needed for    : W in trapezoidal volume formula
Source        : S4 / Section A (plan view) — interior chamber width
                (NOT shown on Section B)
Derivation    : Read directly from Section A dimensioning = 480"
```

**Step 3 — Volume calculation:**

```
V = 0.5 × (11" + 38.25") × 545" × 480"
  = 0.5 × 49.25" × 545" × 480"
  = 6,441,900 in³
  = 6,441,900 ÷ 46,656
  = 138.07 CY (raw)
  × 1.05 waste factor
  = 144.98 CY (with 5% waste)
```

**Final entry in takeoff table:**

| Element ID | Description | Geometry | d_min | d_max | L | W | CY (raw) | CY (+5%) |
|---|---|---|---|---|---|---|---|---|
| WW-FL | Wet well sloped floor | TRAPEZOIDAL_PRISM | 11" | 38.25" | 545" | 480" | 138.07 | 144.98 |

> **Common failure modes this example guards against:**
> 1. Computing as `L × W × 11"` (rectangular default) — gives 90 CY, undercount of ~35%
> 2. Computing as `L × W × 38.5"` (max-depth default) — gives 315 CY, overcount of 130%
> 3. Using the labeled 53'-3" / 639" as slope-run without subtracting the sump offset — gives `d_max = 42.95"` and volume ≈ 150 CY (10% overcount)
> 4. Mixing inches and feet in the formula and dividing by 27 — gives ~3,600 CY (units error)

---

## Tips for Civil/Infrastructure & Industrial Projects

- **Retaining walls**: Check if battered (tapered) — use average thickness if so
- **Pile caps**: Treat as isolated footings; confirm pile embedment depth is excluded
- **Equipment pads / industrial slabs**: Note if thickened edges or trenches are shown
- **Culverts / box structures**: Break into walls, slab top, and slab bottom components
- **Grade beams**: Treat as continuous footings; confirm if below or at grade
- **Elevated slabs / decks**: All surfaces require formwork — include soffit and edges
