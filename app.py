"""
AI Takeoff Prototype - Concrete Quantity Extraction
Flask backend for PDF-based quantity takeoff
"""

import os
import gc
import io
import json
import math
import re
import hashlib
import traceback
import threading
import base64

from PIL import Image

from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from werkzeug.utils import secure_filename
import pypdfium2 as pdfium
from dotenv import load_dotenv
from anthropic import Anthropic
from learning import format_lessons_for_prompt, trigger_lesson_extraction, load_lessons

# Load .env relative to this file so the Flask reloader doesn't lose it
_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, '.env'), override=True)

# Persistent data directory. On Render, /data is a mounted disk that survives
# redeploys. Locally we fall back to the project directory.
DATA_DIR = "/data" if os.path.isdir("/data") else _HERE
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
CORS(app, origins='*', allow_headers=['Content-Type'], methods=['GET', 'POST', 'OPTIONS'])
app.config['UPLOAD_FOLDER'] = os.path.join(_HERE, 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 250 * 1024 * 1024  # 250MB max

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


@app.route('/api/upload', methods=['OPTIONS'])
@app.route('/api/health', methods=['OPTIONS'])
@app.route('/api/feedback', methods=['OPTIONS'])
def handle_preflight():
    return make_response('', 204)

# Initialize Anthropic client only if a key is present so the server still
# boots in dev mode without one (we'll surface a clean error on /api/upload).
_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# max_retries lifts the SDK default (2) so transient 429/500/529/timeout
# errors retry with exponential backoff before an upload fails outright.
client = Anthropic(api_key=_API_KEY, max_retries=4) if _API_KEY else None

# Model used for analysis. Can be overridden with CLAUDE_MODEL env var.
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Sampling temperature. The API defaults to 1.0 (full sampling), which makes a
# takeoff vary run-to-run on the same drawings. 0 takes the most-probable
# reading every time — as reproducible as the model allows, which is what a
# measurement tool needs. Override with CLAUDE_TEMPERATURE.
TEMPERATURE = float(os.environ.get("CLAUDE_TEMPERATURE", "0"))

# Cap text sent to Claude. Construction PDFs are text-light per page, and the
# vision tiles dominate the context budget — so this stays modest on purpose.
MAX_PDF_CHARS = int(os.environ.get("MAX_PDF_CHARS", "80000"))

# --- Vision rendering -----------------------------------------------------
# Drawing sheets are rendered at high DPI and sliced into overlapping tiles so
# dimension text stays legible — a full-size sheet shrunk to a single image is
# not. pdfium render scale: 1.0 == 72 DPI, so 2.1 ≈ 151 DPI (1/8" text ≈ 19px).
TILE_RENDER_SCALE = float(os.environ.get("TILE_RENDER_SCALE", "2.1"))
# Hard cap on a rendered page's long edge (px). Scale is reduced for oversized
# sheets so one render can't blow the memory budget.
MAX_RENDER_PX = int(os.environ.get("MAX_RENDER_PX", "6000"))
# Max tile edge. 1024px keeps a tile near ~1.05 MP — just under Anthropic's
# image cap — so the API does NOT downscale it and the model sees every pixel
# rendered. Larger tiles are silently downsampled, throwing away the DPI gain.
TILE_MAX_PX = 1024
# Tiles overlap by this many px so an element on a seam stays whole in at
# least one tile.
TILE_OVERLAP_PX = 128
# Vision budget in estimated image tokens. The 200K context window is the real
# limit: legible drawings are token-heavy, so the budget buys high-resolution
# tiles for the highest-priority sheets and thumbnails for the rest.
VISION_TOKEN_BUDGET = int(os.environ.get("VISION_TOKEN_BUDGET", "135000"))
# Hard cap on images per request (Anthropic allows 100; leave headroom).
MAX_VISION_IMAGES = int(os.environ.get("MAX_VISION_IMAGES", "95"))
# A page with less than this many characters of extractable text has no text
# layer — almost certainly a drawing, so vision is essential for it.
VISION_TEXT_THRESHOLD = 100
# Serialises page rendering across worker threads so peak memory stays bounded
# to a single rendered bitmap regardless of concurrent uploads.
_RENDER_LOCK = threading.Lock()

# Path to the bundled concrete-takeoff skill. The skill encodes the full
# professional workflow (ingest → extract → calculate → group → output) and
# is injected as the system prompt so every analysis follows it.
SKILL_PATH = os.path.join(_HERE, 'concrete-takeoff', 'SKILL.md')


def load_concrete_takeoff_skill():
    """Load the concrete-takeoff SKILL.md, stripped of YAML frontmatter."""
    try:
        with open(SKILL_PATH, 'r', encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return ""
    # Strip leading YAML frontmatter (--- ... ---) so only the instructional
    # body is sent to Claude.
    if text.startswith('---'):
        end = text.find('\n---', 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


# Cache the skill at import time — it doesn't change between requests.
SKILL_PROMPT = load_concrete_takeoff_skill()


# Probe pypdfium2 at boot so a broken wheel surfaces in /api/health instead of
# blowing up on the first upload. We create an empty in-memory document, which
# exercises the C-library binding (not just the Python wrapper import).
try:
    _probe = pdfium.PdfDocument.new()
    _probe.close()
    PDF_ENGINE_OK = True
    PDF_ENGINE_ERROR = None
except Exception as _e:
    PDF_ENGINE_OK = False
    PDF_ENGINE_ERROR = f"{type(_e).__name__}: {_e}"
PDF_ENGINE_VERSION = str(getattr(pdfium, 'V_PYPDFIUM2', 'unknown'))


def extract_pdf_text(pdf_path):
    """Extract text from a PDF page-by-page, releasing each page after use.

    Uses pypdfium2 (Google's PDFium C library) which streams pages on demand
    and exposes explicit close() calls, keeping peak memory bounded to one
    page at a time instead of the whole document.

    Returns (full_text, page_texts) where page_texts[i] is the raw extracted
    text of 0-based page i — used downstream to rank pages for the vision
    tile budget.
    """
    text_parts = []
    page_texts = []
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    page_text = tp.get_text_range() or ""
                except Exception as e:
                    page_text = f"[Error extracting page {i+1}: {e}]"
                finally:
                    tp.close()
            finally:
                page.close()
            page_texts.append(page_text)
            text_parts.append(f"\n--- PAGE {i+1} ---\n{page_text}")
    finally:
        pdf.close()
    return "".join(text_parts), page_texts


# Keywords that mark a sheet as dimension-bearing — the sheets a concrete
# takeoff depends on most. Weighted so structural plans, schedules, and
# sections win the tile budget (per SKILL.md, concrete dims live on S-sheets).
_PRIORITY_STRONG = [
    "schedule", "foundation plan", "footing", "section", "structural",
    "framing plan", "wall section", "slab on grade", "grade beam",
    "stem wall", "retaining wall", "pier", "caisson",
]
_PRIORITY_MED = ["plan", "elevation", "detail", "general notes", "typ"]
# Feet-and-inches callout, e.g. 8'-0" or 12' - 6 — dense on dimensioned sheets.
_DIM_CALLOUT_RE = re.compile(r"\d+'\s*-\s*\d")
# Structural sheet number in a title block, e.g. S1, S-1, S2.
_STRUCT_SHEET_RE = re.compile(r"\bS-?\d")


def _score_page_priority(text):
    """Heuristic score for how likely a page carries takeoff dimensions.

    Higher scores win the high-resolution tile budget. The 200K context window
    only fits a handful of fully-legible sheets, so the budget must go to the
    sheets that actually carry concrete dimensions.
    """
    raw = text or ""
    low = raw.lower()
    score = 3 * sum(low.count(k) for k in _PRIORITY_STRONG)
    score += sum(low.count(k) for k in _PRIORITY_MED)
    score += 2 * min(len(_DIM_CALLOUT_RE.findall(raw)), 12)
    if _STRUCT_SHEET_RE.search(raw):
        score += 4
    # No text layer at all — definitely a drawing; vision is the only signal.
    if len(raw.strip()) < VISION_TEXT_THRESHOLD:
        score += 6
    return score


def _grid_count(length_px, max_px, overlap):
    """Number of overlapping tiles of `max_px` needed to cover `length_px`."""
    if length_px <= max_px:
        return 1
    return math.ceil((length_px - overlap) / (max_px - overlap))


def _tile_boxes(w, h):
    """Tile a w×h image into overlapping (x0, y0, x1, y1, row, col) boxes.

    The last row/column is aligned to the far edge so the final tile is full
    size rather than a thin sliver.
    """
    cols = _grid_count(w, TILE_MAX_PX, TILE_OVERLAP_PX)
    rows = _grid_count(h, TILE_MAX_PX, TILE_OVERLAP_PX)
    step = TILE_MAX_PX - TILE_OVERLAP_PX
    boxes = []
    for r in range(rows):
        y0 = 0 if rows == 1 else min(r * step, h - TILE_MAX_PX)
        for c in range(cols):
            x0 = 0 if cols == 1 else min(c * step, w - TILE_MAX_PX)
            boxes.append((x0, y0, min(x0 + TILE_MAX_PX, w),
                          min(y0 + TILE_MAX_PX, h), r, c))
    return boxes, rows, cols


def _img_token_estimate(w, h):
    """Approximate Anthropic image token cost, mirroring its ~1.15 MP cap."""
    return int(min(w * h, 1_150_000) / 750)


def _render_tiles(page, page_no, scale):
    """Render one page at high resolution and slice it into overlapping tiles.

    PNG is used (not JPEG) because line art has no gradients and JPEG ringing
    blurs thin dimension lines and small callout text.
    """
    bitmap = page.render(scale=scale)
    try:
        pil = bitmap.to_pil()
        if pil.mode != "RGB":
            pil = pil.convert("RGB")
    finally:
        bitmap.close()
    boxes, rows, cols = _tile_boxes(pil.width, pil.height)
    tiles = []
    for (x0, y0, x1, y1, r, c) in boxes:
        buf = io.BytesIO()
        pil.crop((x0, y0, x1, y1)).save(buf, format="PNG")
        tiles.append({
            "page": page_no, "kind": "tile",
            "row": r + 1, "col": c + 1, "rows": rows, "cols": cols,
            "data": base64.b64encode(buf.getvalue()).decode("utf-8"),
            "media_type": "image/png",
        })
    pil.close()
    return tiles


def _render_overview(page, page_no):
    """Render one page as a single reduced-resolution overview thumbnail."""
    bitmap = page.render(scale=2.0)
    try:
        pil = bitmap.to_pil()
        if pil.mode != "RGB":
            pil = pil.convert("RGB")
    finally:
        bitmap.close()
    w, h = pil.size
    if max(w, h) > TILE_MAX_PX:
        ratio = TILE_MAX_PX / max(w, h)
        pil = pil.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="JPEG", quality=85)
    pil.close()
    return {
        "page": page_no, "kind": "overview",
        "row": 0, "col": 0, "rows": 1, "cols": 1,
        "data": base64.b64encode(buf.getvalue()).decode("utf-8"),
        "media_type": "image/jpeg",
    }


def select_and_render_vision(pdf_path, page_texts):
    """Choose which pages get high-resolution tiles vs. a thumbnail, and render.

    Pages are ranked by _score_page_priority so the token budget is spent on
    the dimension-bearing sheets first. A page is tiled at TILE_RENDER_SCALE
    while VISION_TOKEN_BUDGET lasts; once it is exhausted, remaining pages get
    a single overview thumbnail each (until MAX_VISION_IMAGES is hit); the
    lowest-priority pages get neither and are covered by extracted text only.

    Returns image dicts in document order:
      {page, kind: 'tile'|'overview', row, col, rows, cols, data, media_type}
    """
    n = len(page_texts)
    if n == 0:
        return []
    order = sorted(range(n),
                   key=lambda i: (-_score_page_priority(page_texts[i]), i))

    images = []
    tokens_used = 0
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        for idx in order:
            if len(images) >= MAX_VISION_IMAGES:
                break
            try:
                with _RENDER_LOCK:
                    page = pdf[idx]
                    try:
                        pw, ph = page.get_size()
                        long_pt = max(pw, ph) or 1.0
                        short_pt = min(pw, ph)
                        scale = TILE_RENDER_SCALE
                        if long_pt * scale > MAX_RENDER_PX:
                            scale = MAX_RENDER_PX / long_pt
                        boxes, _, _ = _tile_boxes(int(pw * scale),
                                                  int(ph * scale))
                        tile_cost = sum(
                            _img_token_estimate(x1 - x0, y1 - y0)
                            for (x0, y0, x1, y1, _, _) in boxes
                        )
                        ov_long = min(long_pt * 2.0, TILE_MAX_PX)
                        ov_cost = _img_token_estimate(
                            ov_long, ov_long * short_pt / long_pt)

                        if (len(images) + len(boxes) <= MAX_VISION_IMAGES
                                and tokens_used + tile_cost
                                <= VISION_TOKEN_BUDGET):
                            images.extend(_render_tiles(page, idx + 1, scale))
                            tokens_used += tile_cost
                        elif tokens_used + ov_cost <= VISION_TOKEN_BUDGET:
                            images.append(_render_overview(page, idx + 1))
                            tokens_used += ov_cost
                        # else: budget spent — this page is text-only
                    finally:
                        page.close()
            except Exception as e:
                app.logger.warning("Vision render failed for page %d: %s",
                                    idx + 1, e)
    finally:
        pdf.close()
    images.sort(key=lambda im: (im["page"], im["row"], im["col"]))
    return images


def _parse_json_block(text):
    """Pull the first {...} JSON object out of a Claude response."""
    # Try fenced ```json blocks first
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: find the outermost {...} by tracking brace depth
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# Canonical concrete categories. Ordering mirrors SKILL.md Step 4 (construction
# sequence) so the UI breakdown reads top-down as the work would be built.
# "Non-Concrete" is a sink for anything Claude misidentifies (electrical
# equipment, structural steel, etc.) — those rows are dropped before the
# response leaves the backend.
CONCRETE_CATEGORIES = [
    "Footings",
    "Walls",
    "Slab on Grade",
    "Suspended Slab",
    "Columns",
    "Beams",
    "Piers / Caissons",
    "Equipment Pads",
    "Sidewalks / Curbs",
    "Stairs / Landings",
    "Sumps / Pits",
    "Other Concrete",
]
# Anything tagged with one of these is non-concrete — filtered out before the
# response is returned. Claude is told to only emit concrete in the prompt;
# this is the safety net for when it slips and tags an electrical conduit or
# steel beam as a concrete element anyway.
NON_CONCRETE_CATEGORY = "Non-Concrete"

# Tool definition that forces Claude to return structured takeoff data.
# Using tool_choice={"type":"tool","name":"return_takeoff"} in the second
# API call guarantees a ToolUseBlock response rather than prose text.
_TAKEOFF_TOOL = {
    "name": "return_takeoff",
    "description": "Return the computed concrete takeoff as structured data.",
    "input_schema": {
        "type": "object",
        "required": ["elements", "summary"],
        "properties": {
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["name", "category", "width_ft", "length_ft",
                                 "depth_ft", "qty", "cubic_feet", "cubic_yards",
                                 "notes"],
                    "properties": {
                        "name":        {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": CONCRETE_CATEGORIES + [NON_CONCRETE_CATEGORY],
                            "description": (
                                "Concrete element type. Use 'Footings' for "
                                "continuous and isolated footings; 'Walls' for "
                                "retaining/shear/foundation walls; 'Slab on "
                                "Grade' for ground-bearing slabs; 'Suspended "
                                "Slab' for slabs on metal deck or structural "
                                "floors; 'Columns'; 'Beams'; 'Piers / "
                                "Caissons'; 'Equipment Pads' for transformer "
                                "pads, mechanical pads, light-pole bases, "
                                "etc.; 'Sidewalks / Curbs' for exterior "
                                "flatwork; 'Stairs / Landings'; 'Sumps / Pits' "
                                "for sloped or recessed slabs; 'Other "
                                "Concrete' for anything else that is concrete. "
                                "Use 'Non-Concrete' ONLY if you mistakenly "
                                "extracted electrical, plumbing, mechanical, "
                                "or steel — those rows will be discarded."
                            ),
                        },
                        "width_ft":    {"type": "number"},
                        "length_ft":   {"type": "number"},
                        "depth_ft":    {"type": "number"},
                        "qty":         {"type": "number"},
                        "cubic_feet":  {"type": "number"},
                        "cubic_yards": {"type": "number"},
                        "notes":       {"type": "string"},
                    },
                },
            },
            "summary": {
                "type": "object",
                "required": ["total_cubic_yards", "total_cubic_feet", "assumptions"],
                "properties": {
                    "total_cubic_yards": {"type": "number"},
                    "total_cubic_feet":  {"type": "number"},
                    "assumptions":       {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


# Default 5% waste/overbreak factor — matches SKILL.md convention. Override
# with WASTE_FACTOR=1.00 in env to disable, or any other value (e.g. 1.10).
WASTE_FACTOR = float(os.environ.get("WASTE_FACTOR", "1.05"))
# Drift threshold above which Claude's value triggers an audit entry.
VOLUME_DRIFT_THRESHOLD = 0.01  # 1%

# --- Geometric sanity-check thresholds ------------------------------------
# These flag (never drop) elements whose extracted geometry is physically
# impossible. The model's main failure mode is over-decomposing one element
# into overlapping pieces and double-counting it. All env-tunable.
_SLAB_CATEGORIES = ("Slab on Grade", "Suspended Slab")
# Slab-on-Grade total plan area beyond this multiple of the footprint implies
# a double-counted or over-decomposed slab.
SLAB_AREA_TOLERANCE = float(os.environ.get("SLAB_AREA_TOLERANCE", "1.5"))
# A single wall longer than this multiple of the footprint perimeter cannot
# physically belong to the structure.
WALL_LENGTH_TOLERANCE = float(os.environ.get("WALL_LENGTH_TOLERANCE", "1.15"))
# Slab / wall thicknesses (ft) above these are almost always misread callouts.
MAX_SLAB_THICKNESS_FT = float(os.environ.get("MAX_SLAB_THICKNESS_FT", "3.0"))
MAX_WALL_THICKNESS_FT = float(os.environ.get("MAX_WALL_THICKNESS_FT", "4.0"))


# Keyword fallback used when Claude returns an element without a category
# (older lessons, fallback prose parsing, or schema slip). Matches on name +
# notes, longest/most-specific first.
_CATEGORY_KEYWORDS = [
    ("Sumps / Pits",       ["sump", "pit", "wet well", "trench drain"]),
    ("Suspended Slab",     ["suspended slab", "slab on deck", "slab on metal deck",
                            "metal deck", "structural slab", "elevated slab",
                            "topping slab"]),
    ("Stairs / Landings",  ["stair", "landing", "tread", "riser"]),
    ("Sidewalks / Curbs",  ["sidewalk", "walkway", "curb", "gutter", "driveway",
                            "apron", "flatwork"]),
    ("Equipment Pads",     ["equipment pad", "transformer pad", "mechanical pad",
                            "housekeeping pad", "pad ", "light pole base",
                            "pole base"]),
    ("Piers / Caissons",   ["pier", "caisson", "drilled shaft"]),
    ("Beams",              ["beam", "grade beam", "tie beam"]),
    ("Columns",            ["column"]),
    ("Walls",              ["wall", "retaining", "shear wall", "stem wall"]),
    ("Slab on Grade",      ["slab on grade", "sog", "foundation slab",
                            "ground slab", "mat slab", "slab"]),
    ("Footings",           ["footing", "footer", "foundation", "spread footing",
                            "continuous footing"]),
]

# Heuristics for spotting a row Claude tagged as concrete but is obviously
# not. These are the categories of "junk" we've seen leak through — electrical
# gear, ductwork, conduits, structural steel, finishes. The filter is fired
# even when the schema enum constrained the value, because Claude can still
# stuff "Conduit" into the name field with a valid concrete category.
_NON_CONCRETE_KEYWORDS = [
    "electrical", "conduit", "wire", "cable tray", "luminaire", "lighting fixture",
    "transformer (", "switchgear", "panelboard", "junction box",
    "plumbing", "piping ", "pipe ", "valve", "hose bibb", "drain pipe",
    "hvac", "ductwork", "duct ", "vav box", "ahu", "rtu",
    "structural steel", "steel beam", "steel column", "steel joist", "w-flange",
    "rebar only", "anchor bolt", "embed plate",
    "drywall", "gypsum", "finish", "paint", "insulation", "membrane",
    "asphalt", "soil", "aggregate", "gravel",
    "wood ", "lumber", "framing",
]


def _classify_category(name, notes, current=None):
    """Return a CONCRETE_CATEGORIES value for an element.

    If `current` is already a valid concrete category we keep it. Otherwise
    we fall back to keyword matching on name + notes. Last resort: 'Other
    Concrete'.
    """
    if current and current in CONCRETE_CATEGORIES:
        return current
    haystack = f"{(name or '').lower()} {(notes or '').lower()}"
    for category, keywords in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in haystack:
                return category
    return "Other Concrete"


def _looks_non_concrete(el):
    """Best-effort detection of rows that should be filtered as non-concrete.

    Claude is told in the prompt to use the 'Non-Concrete' category for these,
    but it sometimes hides them behind a valid concrete category (e.g. tags
    'Electrical Conduit' as 'Other Concrete'). The keyword sweep catches both.
    """
    if (el.get("category") or "").strip().lower() == NON_CONCRETE_CATEGORY.lower():
        return True
    haystack = f"{(el.get('name') or '').lower()} {(el.get('notes') or '').lower()}"
    return any(kw in haystack for kw in _NON_CONCRETE_KEYWORDS)


def _verify_element_volumes(elements, waste_factor=WASTE_FACTOR):
    """Replace Claude's per-element cubic_feet / cubic_yards with values
    computed from width × length × depth × qty in Python. Also normalises
    every element's `category` against CONCRETE_CATEGORIES so the frontend
    can group by it without doing its own classification.

    Returns the audit list so the response can surface every override that
    happened. The geometric truth is w·l·d·qty in cubic feet. Claude's job is
    to extract dimensions and classify category; arithmetic on those
    dimensions is Python's job.
    """
    audit = []
    for el in elements or []:
        try:
            w = float(el.get("width_ft") or 0)
            l = float(el.get("length_ft") or 0)
            d = float(el.get("depth_ft") or 0)
            q = float(el.get("qty") or 1)
        except (TypeError, ValueError):
            continue

        raw_cf = w * l * d * q
        verified_cf = round(raw_cf * waste_factor, 2)
        verified_cy = round(verified_cf / 27.0, 2)

        try:
            claude_cf = float(el.get("cubic_feet") or 0)
            claude_cy = float(el.get("cubic_yards") or 0)
        except (TypeError, ValueError):
            claude_cf = claude_cy = 0.0

        def _drift(a, b):
            if b == 0:
                return 0.0 if a == 0 else float("inf")
            return abs(a - b) / abs(b)

        cf_drift = _drift(claude_cf, verified_cf)
        cy_drift = _drift(claude_cy, verified_cy)

        if cf_drift > VOLUME_DRIFT_THRESHOLD or cy_drift > VOLUME_DRIFT_THRESHOLD:
            audit.append({
                "element": el.get("name", "(unnamed)"),
                "claude_cubic_feet": claude_cf,
                "verified_cubic_feet": verified_cf,
                "claude_cubic_yards": claude_cy,
                "verified_cubic_yards": verified_cy,
                "formula": f"{w} × {l} × {d} × {q} × {waste_factor} = {verified_cf} CF",
            })

        el["cubic_feet"] = verified_cf
        el["cubic_yards"] = verified_cy
        el["category"] = _classify_category(
            el.get("name"), el.get("notes"), el.get("category")
        )

    return audit


def _filter_and_group_elements(elements):
    """Drop non-concrete rows and compute per-category CY/CF subtotals.

    Returns (kept_elements, dropped_elements, by_category). by_category is a
    list of {category, cubic_yards, cubic_feet, element_count} in
    CONCRETE_CATEGORIES order — categories with zero elements are omitted so
    the UI can render the breakdown directly.
    """
    kept = []
    dropped = []
    for el in elements or []:
        if _looks_non_concrete(el):
            dropped.append({
                "name": el.get("name", "(unnamed)"),
                "category": el.get("category") or NON_CONCRETE_CATEGORY,
                "reason": "Filtered as non-concrete (electrical, mechanical, "
                          "plumbing, steel, or finishes)",
            })
            continue
        kept.append(el)

    subtotals = {c: {"cubic_yards": 0.0, "cubic_feet": 0.0, "element_count": 0}
                 for c in CONCRETE_CATEGORIES}
    for el in kept:
        cat = el.get("category") or "Other Concrete"
        if cat not in subtotals:
            cat = "Other Concrete"
            el["category"] = cat
        try:
            subtotals[cat]["cubic_yards"] += float(el.get("cubic_yards") or 0)
            subtotals[cat]["cubic_feet"] += float(el.get("cubic_feet") or 0)
        except (TypeError, ValueError):
            pass
        subtotals[cat]["element_count"] += 1

    by_category = []
    for cat in CONCRETE_CATEGORIES:
        s = subtotals[cat]
        if s["element_count"] == 0:
            continue
        by_category.append({
            "category": cat,
            "cubic_yards": round(s["cubic_yards"], 2),
            "cubic_feet": round(s["cubic_feet"], 2),
            "element_count": s["element_count"],
        })
    return kept, dropped, by_category


def _elem_dims(el):
    """Parse an element's (width, length, depth, qty) as floats.

    Unparseable width/length/depth become 0.0 and a missing/bad qty becomes
    1.0, so geometry math can run without per-field guards.
    """
    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return (_f(el.get("width_ft")), _f(el.get("length_ft")),
            _f(el.get("depth_ft")), _f(el.get("qty"), 1.0) or 1.0)


def _geometric_sanity_checks(elements):
    """Flag elements whose extracted geometry is physically implausible.

    _verify_element_volumes recomputes volumes but trusts the dimensions the
    model extracted. These checks compare those dimensions against the
    building footprint to catch the model's dominant failure mode: over-
    decomposing one physical element into overlapping pieces and double-
    counting (e.g. a base slab split into "zones" that together cover far
    more area than the structure has). The footprint is anchored to the
    largest single slab, which the model reads far more reliably than a
    decomposed element.

    Returns a list of {check, element, detail} warnings. Nothing is dropped
    or recomputed — the takeoff is flagged so a wrong result is visible to
    the estimator instead of being trusted silently.
    """
    els = elements or []
    warnings = []

    # Per-element plausibility: impossible or misread dimensions.
    for el in els:
        w, l, d, _ = _elem_dims(el)
        name = el.get("name") or "(unnamed)"
        cat = (el.get("category") or "").strip()
        if min(w, l, d) <= 0:
            warnings.append({
                "check": "nonpositive_dimension",
                "element": name,
                "detail": (f"dimensions {w}x{l}x{d} ft — an element cannot "
                           f"have a zero or negative dimension; a callout "
                           f"was likely missed."),
            })
        if cat in _SLAB_CATEGORIES and d > MAX_SLAB_THICKNESS_FT:
            warnings.append({
                "check": "implausible_thickness",
                "element": name,
                "detail": (f"{cat} listed {d} ft thick — slabs are rarely "
                           f"over {MAX_SLAB_THICKNESS_FT} ft; verify the "
                           f"thickness callout."),
            })
        if cat == "Walls" and w > MAX_WALL_THICKNESS_FT:
            warnings.append({
                "check": "implausible_thickness",
                "element": name,
                "detail": (f"wall listed {w} ft thick — verify the width "
                           f"callout was not misread."),
            })

    # Footprint-anchored checks. Anchor to the largest suspended slab (a roof
    # or floor spans the whole structure as one piece); fall back to the
    # largest slab on grade only when no suspended slab exists.
    suspended = [e for e in els
                 if (e.get("category") or "").strip() == "Suspended Slab"]
    on_grade = [e for e in els
                if (e.get("category") or "").strip() == "Slab on Grade"]
    ref_area = ref_w = ref_l = 0.0
    for el in (suspended or on_grade):
        w, l, _, _ = _elem_dims(el)
        if w * l > ref_area:
            ref_area, ref_w, ref_l = w * l, w, l

    if ref_area > 0:
        # A slab on grade is poured once across the footprint; far more total
        # area means pieces of one slab were counted more than once.
        sog_area = sum(_elem_dims(e)[0] * _elem_dims(e)[1] * _elem_dims(e)[3]
                       for e in on_grade)
        if sog_area > SLAB_AREA_TOLERANCE * ref_area:
            warnings.append({
                "check": "slab_area_exceeds_footprint",
                "element": "(all Slab on Grade)",
                "detail": (f"Slab-on-Grade plan area totals {sog_area:,.0f} "
                           f"sq ft against a ~{ref_area:,.0f} sq ft footprint "
                           f"({sog_area / ref_area:.1f}x) — a ground slab is "
                           f"poured once; likely a double-counted or over-"
                           f"decomposed slab."),
            })
        # No single wall can be longer than the structure's perimeter.
        perimeter = 2.0 * (ref_w + ref_l)
        if perimeter > 0:
            for el in els:
                if (el.get("category") or "").strip() != "Walls":
                    continue
                _, l, _, _ = _elem_dims(el)
                if l > WALL_LENGTH_TOLERANCE * perimeter:
                    warnings.append({
                        "check": "wall_longer_than_perimeter",
                        "element": el.get("name") or "(unnamed)",
                        "detail": (f"wall length {l:,.0f} ft exceeds the "
                                   f"structure's ~{perimeter:,.0f} ft "
                                   f"perimeter — likely the whole perimeter "
                                   f"summed into one element, or a misread "
                                   f"dimension."),
                    })

    return warnings


def extract_quantities_with_claude(pdf_text, page_images, filename):
    """Single-call analysis: identify elements, compute volumes, return data.

    Claude identifies every concrete element, calculates each volume, and
    calls the return_takeoff tool in a single request. page_images is a list
    of {page, data, media_type} dicts rendered from sparse pages; when present
    they are attached so Claude can read dimension callouts directly from the
    vector/raster drawing layers.
    """
    if client is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Create a .env file with "
            "ANTHROPIC_API_KEY=sk-ant-... or export it in your shell."
        )

    has_text = pdf_text.strip() and len(pdf_text.strip()) >= 50
    has_images = bool(page_images)

    if not has_text and not has_images:
        return {
            "elements": [],
            "summary": {
                "total_cubic_yards": 0,
                "assumptions": [
                    "PDF appears to be scanned/image-based with no extractable text or renderable pages.",
                ],
                "status": "no_content_extracted",
            },
        }

    # Truncate but keep a small tail so totals/schedules at the end aren't lost.
    if len(pdf_text) > MAX_PDF_CHARS:
        head = pdf_text[: int(MAX_PDF_CHARS * 0.8)]
        tail = pdf_text[-int(MAX_PDF_CHARS * 0.2):]
        prompt_text = f"{head}\n\n[... middle truncated ...]\n\n{tail}"
    else:
        prompt_text = pdf_text

    history = []
    prose_response = ""  # reasoning Claude emits before the tool call

    lessons_block = format_lessons_for_prompt()
    lessons_section = f"\n{lessons_block}\n" if lessons_block else ""

    # Build the system prompt: the concrete-takeoff skill defines the
    # professional workflow; lessons add validated human corrections on top.
    system_prompt = (
        "You are a construction quantity takeoff specialist. Follow the "
        "concrete-takeoff skill below for the full workflow, formulas, "
        "defaults, and quality checks.\n\n"
        f"{SKILL_PROMPT}"
    ) if SKILL_PROMPT else (
        "You are a construction quantity takeoff specialist."
    )

    categories_list = ", ".join(f'"{c}"' for c in CONCRETE_CATEGORIES)
    initial = f"""Analyzing a PDF of construction drawings: {filename}
{lessons_section}
CRITICAL RULES (validated from expert human corrections — follow exactly, in addition to the concrete-takeoff skill workflow):
1. CONCRETE ONLY — DO NOT include electrical, plumbing, mechanical, HVAC, structural steel, wood framing, drywall, finishes, or any non-concrete material as elements. Electrical conduits, conductors, transformers, panelboards, switchgear, light fixtures, ductwork, pipes, valves, steel beams/columns/joists, embed plates and anchor bolts (without their concrete encasement), wood members, insulation, and membranes are NOT concrete elements and must NOT appear in your output with a cubic-yard value. If a piece of equipment sits on a concrete pad, include ONLY the pad (category "Equipment Pads"), not the equipment.
2. SLOPED / TAPERED SLAB VOLUMES: Any sump, pit, or base slab described with a percentage slope (e.g. "5% slope", "slopes to drain") is a wedge-shaped concrete element that must be computed as a SEPARATE, POSITIVE line item using: V = 0.5 × (h_start + h_end) × width × length ÷ 46656 (in³→yd³). h_end = h_start + (slope_pct/100) × length_in. Do NOT treat it as a simple deduction.
3. MULTIPLE ROOF SLAB SECTIONS: If the roof slab plan or sections show different annotated thicknesses for different zones, extract EACH zone as its own element with its correct depth. Never apply one uniform thickness to the entire footprint when multiple depths are shown.
4. INTERIOR WALL THICKNESS: Interior dividing walls are frequently thinner than perimeter walls. Always look for an explicit dimension callout on the interior wall — do NOT default to the perimeter wall thickness. If no callout exists, flag the row as uncertain.
5. Cross check dimensions with other sheets to confirm the correct measurement before going to calculations.

CATEGORY FIELD (REQUIRED) — every element you return MUST carry one of these category strings, chosen by what the element IS in construction terms:
{categories_list}
Group elements in this canonical order: Footings → Walls → Slab on Grade → Suspended Slab → Columns → Beams → Piers / Caissons → Equipment Pads → Sidewalks / Curbs → Stairs / Landings → Sumps / Pits → Other Concrete. If you cannot avoid extracting a non-concrete item (e.g. you misread a callout), set its category to "Non-Concrete" so the server can drop it cleanly — never assign a concrete category to a non-concrete item.

HOW THE DRAWINGS ARE PROVIDED: Drawing sheets are attached as images. A large sheet is sliced into a grid of overlapping high-resolution tiles, each labeled "[Page N — tile row R of …, col C of …]"; a smaller or lower-priority sheet is attached as one reduced-resolution thumbnail labeled "[Page N — whole-sheet overview …]". Every image carrying the same page number is part of ONE physical sheet — reassemble its tiles in your mind into the full sheet before reading dimensions. Because adjacent tiles overlap, the same footing, wall, column, or dimension callout can appear in two neighboring tiles — count each physical element ONLY ONCE. Pages with no attached image are represented by their extracted text only.

Below is the extracted text from the drawings (page markers included):

<drawings>
{prompt_text}
</drawings>

Work through the concrete-takeoff skill in this single response, in order:

STEPS 1-2 — IDENTIFY & EXTRACT (do this in prose first): Follow Step 1 (Ingest and Orient) and Step 2 (Extract Dimensions). Identify EVERY concrete structural element you can see (footings, slab on grade, walls, columns, piers, slabs on deck, equipment pads, sidewalks, driveways, sumps, sloped bases, etc.). For each, note:
- A descriptive name and unique element ID (F1, F2, W1, S1, C1, etc.)
- Its concrete category from the list above
- Width, length, depth/thickness (in feet; convert inches: 6\" = 0.5 ft)
- Quantity if there are multiples (e.g., 9 column footings)
- Any reinforcement / strength specs you noticed
- For sloped elements: starting height, ending height, slope percentage, and the wedge formula used
If a dimension is not stated, use a reasonable construction default and note it.

STEP 3 — CALCULATE: Perform Step 3 (Calculate Quantities) and compute the concrete volume for each element. Apply a 5% waste/overbreak factor unless drawings specify otherwise.
Standard rectangular elements:
  cubic_feet = width_ft × length_ft × depth_ft × qty
  cubic_yards = cubic_feet / 27
Tapered / wedge elements (sloped sumps, sloped bases):
  cubic_feet = 0.5 × (depth_start_ft + depth_end_ft) × width_ft × length_ft × qty
  cubic_yards = cubic_feet / 27
  Use depth_ft to store the AVERAGE depth = 0.5*(depth_start + depth_end).
Sum cubic_yards across ALL elements (tapered elements must be POSITIVE additions, not deductions).

FINALLY: Run the Quality Checks, then call the return_takeoff tool with every concrete element and the summary totals. Every element MUST include its `category` field (Footings, Walls, Slab on Grade, Suspended Slab, Columns, Beams, Piers / Caissons, Equipment Pads, Sidewalks / Curbs, Stairs / Landings, Sumps / Pits, or Other Concrete). Do not include electrical, mechanical, plumbing, or steel as concrete elements — those are filtered out."""
    # Build the request content: text prompt always present; attach rendered
    # page images when available so Claude can read dimension callouts directly.
    if page_images:
        user_content = [{"type": "text", "text": initial}]
        for img in page_images:
            if img.get("kind") == "tile" and img["rows"] * img["cols"] > 1:
                label = (f"\n[Page {img['page']} — tile row {img['row']} of "
                         f"{img['rows']}, col {img['col']} of {img['cols']}; "
                         f"high-resolution, tiles overlap]")
            elif img.get("kind") == "tile":
                label = f"\n[Page {img['page']} — full sheet, high-resolution]"
            else:
                label = (f"\n[Page {img['page']} — whole-sheet overview, "
                         f"reduced resolution]")
            user_content.append({"type": "text", "text": label})
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img['media_type'],
                    "data": img['data'],
                },
            })
    else:
        user_content = initial

    history.append({"role": "user", "content": user_content})

    # Single analysis call: Claude identifies every concrete element,
    # calculates volumes, and calls return_takeoff in one request. Collapsed
    # from a two-call flow on 2026-05-22 — the old second call re-sent every
    # drawing image, doubling input-token usage against the API rate limit for
    # no accuracy gain (Python recomputes all volumes in _verify_element_volumes
    # regardless). tool_choice="auto" lets Claude reason in prose before the
    # tool call; the retry loop below forces the tool if nothing usable returns.
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        temperature=TEMPERATURE,
        system=system_prompt,
        messages=history,
        tools=[_TAKEOFF_TOOL],
        tool_choice={"type": "auto"},
    )
    # Join every text block so multi-block responses aren't truncated. This is
    # the reasoning Claude emitted before the tool call — kept as fallback
    # analysis text and to gauge whether a retry is worthwhile.
    prose_response = "".join(getattr(b, "text", "") for b in resp.content)
    # A "max_tokens" stop means the response was cut off mid-output, so the
    # tool input may be incomplete — tracked to flag the result downstream.
    truncated = getattr(resp, "stop_reason", None) == "max_tokens"

    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_block:
        parsed = tool_block.input
    else:
        # auto chose not to call the tool — salvage JSON from the prose if any
        parsed = _parse_json_block(prose_response)

    # Re-prompt when the call returned no usable takeoff — either no tool call
    # at all, or a return_takeoff call with elements=[] despite reasoning that
    # clearly identified concrete. Without this the response leaks back to the
    # frontend as narrative text and the UI shows the "AI returned narrative
    # analysis" fallback instead of a table. Retries force tool_choice so the
    # structured result is guaranteed.
    MAX_TOOL_RETRIES = 2
    for _ in range(MAX_TOOL_RETRIES):
        if (parsed or {}).get("elements"):
            break
        if len((prose_response or "").strip()) < 100:
            break
        history.append({"role": "assistant", "content": resp.content})
        if tool_block:
            history.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "is_error": True,
                    "content": (
                        "Your return_takeoff call had elements=[] but your "
                        "analysis identified concrete elements. Call return_takeoff "
                        "again and populate elements with EVERY concrete element "
                        "from that analysis. For each: name (e.g. 'Foundation Slab', "
                        "'Column Footing F1'), category, width_ft, length_ft, "
                        "depth_ft, qty, cubic_feet (= width × length × depth × qty), "
                        "cubic_yards (= cubic_feet / 27), notes. Also fill "
                        "summary.total_cubic_yards, summary.total_cubic_feet, and "
                        "summary.assumptions. Do not return an empty elements list."
                    ),
                }],
            })
        else:
            history.append({
                "role": "user",
                "content": (
                    "You did not call the return_takeoff tool. Call it now and "
                    "populate elements with EVERY concrete element from your "
                    "analysis. For each: name (e.g. 'Foundation Slab', 'Column "
                    "Footing F1'), category, width_ft, length_ft, depth_ft, qty, "
                    "cubic_feet (= width × length × depth × qty), cubic_yards "
                    "(= cubic_feet / 27), notes. Also fill summary.total_cubic_yards, "
                    "summary.total_cubic_feet, and summary.assumptions. Do not "
                    "return an empty elements list."
                ),
            })
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,
            temperature=TEMPERATURE,
            system=system_prompt,
            messages=history,
            tools=[_TAKEOFF_TOOL],
            tool_choice={"type": "tool", "name": "return_takeoff"},
        )
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            parsed = tool_block.input

    if parsed:
        # Deterministic Python recompute of every element's volume from the
        # extracted dimensions. Replaces Claude's arithmetic (which drifts ~1%
        # across multiple trials) with a single canonical computation. Also
        # normalises the category field against CONCRETE_CATEGORIES.
        audit = _verify_element_volumes(parsed.get("elements") or [])

        # Drop electrical/mechanical/steel rows that snuck through and compute
        # per-category subtotals so the UI can group the table without doing
        # its own classification.
        kept, dropped, by_category = _filter_and_group_elements(
            parsed.get("elements") or []
        )
        parsed["elements"] = kept

        # Flag elements whose extracted geometry is physically implausible
        # (double-counted / over-decomposed) so a wrong takeoff is visible.
        geometry_warnings = _geometric_sanity_checks(kept)

        total_cf = 0.0
        total_cy = 0.0
        for el in kept:
            try:
                total_cf += float(el.get("cubic_feet") or 0)
                total_cy += float(el.get("cubic_yards") or 0)
            except (TypeError, ValueError):
                pass
        parsed.setdefault("summary", {})
        parsed["summary"]["total_cubic_feet"] = round(total_cf, 2)
        parsed["summary"]["total_cubic_yards"] = round(total_cy, 2)
        parsed["summary"]["by_category"] = by_category
        parsed["summary"]["verification"] = {
            "applied": True,
            "waste_factor": WASTE_FACTOR,
            "drift_threshold_pct": VOLUME_DRIFT_THRESHOLD * 100,
            "overrides": audit,
            "override_count": len(audit),
            "non_concrete_dropped": dropped,
            "non_concrete_dropped_count": len(dropped),
            "geometry_warnings": geometry_warnings,
            "geometry_warning_count": len(geometry_warnings),
        }
        # Surface a truncated response so a cut-off takeoff is not trusted silently.
        if truncated:
            parsed["summary"]["truncated"] = True
            warn = ("WARNING: the model response hit its max_tokens limit, so "
                    "this takeoff may be incomplete. Re-run to confirm.")
            if isinstance(parsed["summary"].get("assumptions"), list):
                parsed["summary"]["assumptions"].append(warn)
            else:
                parsed["summary"]["assumptions"] = [warn]

        # Surface geometry warnings the same way so the UI shows them.
        if geometry_warnings:
            gwarn = (f"WARNING: {len(geometry_warnings)} geometric sanity "
                     f"check(s) flagged this takeoff as possibly over-counted "
                     f"or misread — see verification.geometry_warnings.")
            if isinstance(parsed["summary"].get("assumptions"), list):
                parsed["summary"]["assumptions"].append(gwarn)
            else:
                parsed["summary"]["assumptions"] = [gwarn]

        # When the tool returned no elements, attach the reasoning prose so
        # the frontend parser can attempt to extract rows from it.
        if not parsed.get("elements") and prose_response:
            parsed["summary"]["analysis"] = prose_response
        return parsed

    # Reached only if the call made no tool call and no JSON could be salvaged
    return {
        "elements": [],
        "summary": {
            "total_cubic_yards": 0,
            "assumptions": ["Could not extract structured quantities from the AI response."],
            "analysis": prose_response or None,
            "status": "unparsed",
        },
    }


@app.route('/')
def index():
    return jsonify({
        "ok": True,
        "service": "AI Takeoff API",
        "endpoints": ["/api/health", "/api/upload", "/api/feedback", "/api/lessons"],
    })


@app.route('/api/health')
def health():
    return jsonify({
        "ok": True,
        "model": MODEL,
        "api_key_loaded": client is not None,
        "skill_loaded": bool(SKILL_PROMPT),
        "skill_chars": len(SKILL_PROMPT),
        "vision_render_scale": TILE_RENDER_SCALE,
        "vision_token_budget": VISION_TOKEN_BUDGET,
        "pdf_engine": "pypdfium2",
        "pdf_engine_loaded": PDF_ENGINE_OK,
        "pdf_engine_version": PDF_ENGINE_VERSION,
        "pdf_engine_error": PDF_ENGINE_ERROR,
    })


@app.route('/api/feedback', methods=['POST'])
def save_feedback():
    """Append user feedback/corrections to a JSONL log, then trigger lesson extraction."""
    try:
        payload = request.get_json(force=True)
        if not payload:
            return jsonify({'error': 'No JSON body'}), 400

        # Extract image snippets before logging — keep JSONL compact
        snippet_images = payload.pop('snippet_images', []) or []

        log_path = os.path.join(DATA_DIR, 'feedback_log.jsonl')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(payload) + '\n')

        # Persist snippet images to disk so they aren't lost
        safe_snippets = []
        if snippet_images:
            snippets_dir = os.path.join(DATA_DIR, 'snippets')
            os.makedirs(snippets_dir, exist_ok=True)
            uid = hashlib.md5(os.urandom(8)).hexdigest()[:8]
            for idx, img in enumerate(snippet_images[:4]):
                data_url = img.get('data_url', '')
                if not data_url.startswith('data:image'):
                    continue
                try:
                    header, b64_data = data_url.split(',', 1)
                    ext = 'png' if 'png' in header else 'jpg'
                    fname = f"snippet_{uid}_{idx}.{ext}"
                    with open(os.path.join(snippets_dir, fname), 'wb') as sf:
                        sf.write(base64.b64decode(b64_data))
                    safe_snippets.append(img)
                except Exception as e:
                    app.logger.warning("Snippet save failed: %s", e)

        learning_triggered = False
        if client:
            corrections_exist = any(
                el.get('edited') or el.get('flag') == 'incorrect'
                for el in payload.get('elements', [])
            )
            if corrections_exist:
                trigger_lesson_extraction(payload, client, MODEL, images=safe_snippets or None)
                learning_triggered = True

        return jsonify({'ok': True, 'learning_triggered': learning_triggered,
                        'snippets_received': len(safe_snippets)})
    except Exception as e:
        app.logger.error("save_feedback failed: %s", e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/lessons')
def get_lessons():
    """Return all lessons the system has learned from human corrections."""
    lessons = load_lessons()
    return jsonify({'count': len(lessons), 'lessons': lessons[-20:]})


@app.route('/api/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400

        f = request.files['file']
        if not f or f.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        if not f.filename.lower().endswith('.pdf'):
            return jsonify({'error': 'Only PDF files are supported'}), 400

        filename = secure_filename(f.filename)
        # Add a short hash so re-uploads of the same name don't collide
        digest = hashlib.md5(filename.encode()).hexdigest()[:6]
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], f"{digest}_{filename}")
        f.save(save_path)

        try:
            pdf_text, page_texts = extract_pdf_text(save_path)
            page_images = select_and_render_vision(save_path, page_texts)
            result = extract_quantities_with_claude(pdf_text, page_images, filename)
        finally:
            try:
                os.remove(save_path)
            except OSError:
                pass
            # Release PDF parse buffers and conversation history back to the
            # OS so the next request starts fresh instead of inheriting peak.
            gc.collect()

        tiles = sum(1 for im in page_images if im.get('kind') == 'tile')
        return jsonify({
            'success': True,
            'filename': filename,
            'pdf_page_count': len(page_texts),
            'pages_text_chars': len(pdf_text),
            'vision_pages_rendered': len({im['page'] for im in page_images}),
            'vision_tiles_rendered': tiles,
            'data': result,
        })
    except Exception as e:
        # Always return JSON, never HTML — frontend depends on response.json()
        app.logger.error("upload_file failed: %s\n%s", e, traceback.format_exc())
        return jsonify({'error': f'Processing error: {e}'}), 500


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'File too large (max 250MB)'}), 413


@app.errorhandler(404)
def not_found(_):
    return jsonify({'error': 'Not found'}), 404


if __name__ == '__main__':
    app.run(debug=True, port=5001)
