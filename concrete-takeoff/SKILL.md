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

### Step 2 — Extract Dimensions

For each element, extract or derive:
- **Length** (L), **Width** (W), **Depth/Thickness** (D or T)
- If dimensions are labeled on the drawing: use them directly
- If dimensions must be scaled: use the scale bar or stated scale ratio to derive dimensions from the drawing geometry. Always note when a dimension is scaled vs. labeled.
-Cross check dimensions with other "S" sheets to verify all dimensions are correct before moving to calculations
-Note that not all concrete sections are perfectly rectangular, be aware of these sections
-For concrete quantities, almost all important dimensions will be listed on "S" sheets
- Record each element with a unique identifier (e.g., F1, F2, W1, S1, C1)

### Step 3 — Calculate Quantities

Apply the following formulas. Always show your math inline so the user can verify.

#### Concrete Volume
Convert all dimensions to feet first, then:

| Element | Formula |
|---|---|
| Rectangular footing | L × W × D ÷ 27 = CY |
| Continuous wall footing | L × W × D ÷ 27 = CY |
| Slab on grade | L × W × T ÷ 27 = CY |
| Retaining / shear wall | L × H × T ÷ 27 = CY |
| Column (rectangular) | W × D × H ÷ 27 = CY |
| Column (round) | π × r² × H ÷ 27 = CY |
| Beam | W × D × L ÷ 27 = CY |
| Pier / caisson (round) | π × r² × H ÷ 27 = CY |
| Pier / caisson (rectangular) | L × W × H ÷ 27 = CY |

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
- [ ] All CY calculations divided by 27 (not 9 or 3)
- [ ] Round columns use π × r² (not diameter²)
- [ ] Waste factor applied to all volumes
- [ ] Rebar lap splice factor applied
- [ ] Formwork excludes earth-formed and top/finished surfaces
- [ ] Subtotals and project total match sum of line items

---

## Tips for Civil/Infrastructure & Industrial Projects

- **Retaining walls**: Check if battered (tapered) — use average thickness if so
- **Pile caps**: Treat as isolated footings; confirm pile embedment depth is excluded
- **Equipment pads / industrial slabs**: Note if thickened edges or trenches are shown
- **Culverts / box structures**: Break into walls, slab top, and slab bottom components
- **Grade beams**: Treat as continuous footings; confirm if below or at grade
- **Elevated slabs / decks**: All surfaces require formwork — include soffit and edges
