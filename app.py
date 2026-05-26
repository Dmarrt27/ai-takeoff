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
import concurrent.futures
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

# --- Anthropic concurrency / timeout --------------------------------------
# Per-call timeout (seconds). The Anthropic SDK default is ~600s which is the
# same as the gunicorn worker --timeout — so a single slow call plus retries
# can blow the whole worker. 150s leaves ~4 retries of headroom inside 600s.
PER_CALL_TIMEOUT = float(os.environ.get("PER_CALL_TIMEOUT_S", "150"))
# Module-level cap on total in-flight Anthropic calls across ALL uploads. With
# gunicorn --workers 1 --threads 4 the worker can serve 4 uploads concurrently;
# without this semaphore, multi-pass fan-out (Pass 1 + confirm passes) per
# upload could spawn 5+ outbound calls each → 20+ in-flight, blowing rate
# limits. The bound is global, not per-request, on purpose.
_API_CALL_CONCURRENCY = int(os.environ.get("API_CALL_CONCURRENCY", "4"))
_API_CALL_SEMAPHORE = threading.BoundedSemaphore(_API_CALL_CONCURRENCY)

# --- Multi-pass extraction (Fix 1) ----------------------------------------
# Below this sheet count, skip triage + multi-pass and use the single-call
# path. Small sets don't benefit from fan-out and pay extra latency.
SMALL_SET_THRESHOLD = int(os.environ.get("SMALL_SET_THRESHOLD", "4"))
# Triage thumbnail size (cell long-edge px) and grid layout. Every sheet
# renders as a small thumbnail; 6 thumbnails per grid image (3 × 2) lets a
# 95-sheet set fit in ~16 grid images, well under the 100-image API cap.
TRIAGE_THUMB_PX = int(os.environ.get("TRIAGE_THUMB_PX", "256"))
TRIAGE_GRID_COLS = int(os.environ.get("TRIAGE_GRID_COLS", "3"))
TRIAGE_GRID_ROWS = int(os.environ.get("TRIAGE_GRID_ROWS", "2"))
# Confirm-pass batch size: how many tiles get sent per confirm-pass call. The
# batch must fit inside the per-call image budget once the cached SKILL system
# prompt is on top, with headroom for the roster JSON.
CONFIRM_BATCH_TILES = int(os.environ.get("CONFIRM_BATCH_TILES", "30"))

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


def _system_blocks():
    """Build the system prompt as Anthropic content blocks with prompt caching.

    The SKILL is stable across every call in a request (and across requests),
    so wrapping it in a `cache_control: ephemeral` block lets later calls
    read the prefix from cache at ~0.1× the input-token price. The first
    call in a flow pays the cache-write premium (~1.25×); every subsequent
    call inside the 5-minute TTL pays the read price. Pass directly to
    `client.messages.create(system=...)`.
    """
    text = (
        "You are a construction quantity takeoff specialist. Follow the "
        "concrete-takeoff skill below for the full workflow, formulas, "
        "defaults, and quality checks.\n\n"
        f"{SKILL_PROMPT}"
    ) if SKILL_PROMPT else (
        "You are a construction quantity takeoff specialist."
    )
    return [{
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }]


def _chat_call(messages, tools=None, tool_choice=None, max_tokens=16000):
    """Wrap `client.messages.create` with the API semaphore + per-call timeout.

    Centralises three things every Anthropic call in this app needs:
    - Cached SKILL system prompt (see `_system_blocks`).
    - Module-level semaphore so concurrent uploads + multi-pass fan-out can't
      blow the rate limit (default 4 in-flight; configurable via env).
    - Per-call timeout under the gunicorn worker --timeout so a hung call
      fails fast inside budget instead of taking the worker down.
    Raises RuntimeError if the Anthropic client isn't configured.
    """
    if client is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Create a .env file with "
            "ANTHROPIC_API_KEY=sk-ant-... or export it in your shell."
        )
    kwargs = {
        "model": MODEL,
        "max_tokens": max_tokens,
        "temperature": TEMPERATURE,
        "system": _system_blocks(),
        "messages": messages,
        "timeout": PER_CALL_TIMEOUT,
    }
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    with _API_CALL_SEMAPHORE:
        return client.messages.create(**kwargs)


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


def _compose_triage_grid(cells, cell_px):
    """Composite up to TRIAGE_GRID_COLS×TRIAGE_GRID_ROWS thumbnails into one image.

    Cells are laid out row-major (left→right, top→bottom). Each thumbnail is
    centered in its cell so aspect-ratio differences don't bias the model
    toward certain grid positions. Returns the grid image as a dict carrying
    the 1-based page numbers in row-major order.
    """
    grid_w = cell_px * TRIAGE_GRID_COLS
    grid_h = cell_px * TRIAGE_GRID_ROWS
    canvas = Image.new("RGB", (grid_w, grid_h), "white")
    pages = []
    for idx, (thumb, page_no) in enumerate(cells):
        row = idx // TRIAGE_GRID_COLS
        col = idx % TRIAGE_GRID_COLS
        x0 = col * cell_px
        y0 = row * cell_px
        cw, ch = thumb.size
        canvas.paste(thumb,
                     (x0 + (cell_px - cw) // 2, y0 + (cell_px - ch) // 2))
        pages.append(page_no)
        thumb.close()
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=80)
    canvas.close()
    return {
        "data": base64.b64encode(buf.getvalue()).decode("utf-8"),
        "media_type": "image/jpeg",
        "pages": pages,
    }


def _render_triage_grids(pdf_path, page_count):
    """Render every page as a small thumbnail and composite N-up grid images.

    Pass 0 (triage) sends one image per grid, not one per page — a 95-page
    set composites into ~16 grid images, well under the 100-image API cap.
    Each grid cell is TRIAGE_THUMB_PX on its long edge; pages are laid out
    in row-major order so the model can map cell position to page number
    using the label above each grid.
    """
    cell_px = TRIAGE_THUMB_PX
    cells_per_grid = TRIAGE_GRID_COLS * TRIAGE_GRID_ROWS
    if cells_per_grid <= 0:
        return []
    grids = []
    pending = []  # (PIL image, 1-based page number)
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        for i in range(page_count):
            try:
                with _RENDER_LOCK:
                    page = pdf[i]
                    try:
                        pw, ph = page.get_size()
                        long_pt = max(pw, ph) or 1.0
                        # pdfium scale 1.0 == 72 DPI, so cell_px / long_pt
                        # gives the scale that puts the long edge ~= cell_px.
                        # Clamped so degenerate pages don't blow rendering.
                        scale = max(0.25, min(2.0, cell_px / long_pt))
                        bitmap = page.render(scale=scale)
                        try:
                            pil = bitmap.to_pil()
                            if pil.mode != "RGB":
                                pil = pil.convert("RGB")
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
                pil.thumbnail((cell_px, cell_px), Image.LANCZOS)
                pending.append((pil, i + 1))
            except Exception as e:
                app.logger.warning(
                    "Triage thumbnail failed for page %d: %s", i + 1, e)
                continue
            if len(pending) >= cells_per_grid:
                grids.append(_compose_triage_grid(pending, cell_px))
                pending = []
        if pending:
            grids.append(_compose_triage_grid(pending, cell_px))
    finally:
        pdf.close()
    return grids


def triage_sheets(pdf_path, page_count):
    """Pass 0: classify every sheet via a single vision call.

    Replaces the keyword heuristic in `_score_page_priority` — works on both
    vector and scanned PDFs because it reads pixels, not the text layer.
    Returns a dict {page_no: {sheet_type, carries_concrete_dims}} covering
    every page (defaults applied to anything the model omits), or None on
    any failure so the caller can fall back to the text-heuristic path.
    """
    if client is None or page_count <= 0:
        return None
    try:
        grids = _render_triage_grids(pdf_path, page_count)
    except Exception as e:
        app.logger.warning("Triage rendering failed: %s", e)
        return None
    if not grids:
        return None

    intro = (
        f"Below are {len(grids)} grid images. Each grid composites up to "
        f"{TRIAGE_GRID_COLS * TRIAGE_GRID_ROWS} construction drawing sheets "
        f"laid out in row-major order (left-to-right, top-to-bottom), at "
        f"~{TRIAGE_THUMB_PX}px per cell. The 1-based page number for each "
        f"cell is listed in the label above its grid.\n\n"
        f"For EVERY page from 1 to {page_count}, classify the sheet by "
        f"type and whether it carries concrete dimensions (callouts, "
        f"schedules, or sections with depth/thickness). Use "
        f"sheet_type='other' for any page you cannot classify confidently; "
        f"do not omit a page. Call classify_sheets once with the full list."
    )
    content = [{"type": "text", "text": intro}]
    for grid in grids:
        label = (f"\n[Grid pages, row-major: "
                 f"{', '.join(str(p) for p in grid['pages'])}]")
        content.append({"type": "text", "text": label})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": grid["media_type"],
                "data": grid["data"],
            },
        })

    try:
        resp = _chat_call(
            messages=[{"role": "user", "content": content}],
            tools=[_TRIAGE_TOOL],
            tool_choice={"type": "tool", "name": "classify_sheets"},
            max_tokens=4000,
        )
    except Exception as e:
        app.logger.warning("Triage call failed: %s", e)
        return None

    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_block:
        return None
    out = {}
    for s in (tool_block.input or {}).get("sheets") or []:
        try:
            page = int(s["page"])
        except (KeyError, TypeError, ValueError):
            continue
        if page < 1 or page > page_count:
            continue
        out[page] = {
            "sheet_type": s.get("sheet_type") or "other",
            "carries_concrete_dims": bool(s.get("carries_concrete_dims",
                                                False)),
        }
    # Pages the model skipped default to 'other' with no concrete dims —
    # the selection step decides what to do with them (typically: include
    # only if no roster/confirm sheets were classified).
    for page in range(1, page_count + 1):
        out.setdefault(page, {"sheet_type": "other",
                              "carries_concrete_dims": False})
    return out


def _bucket_pages_by_triage(page_count, triage):
    """Pure: route each page into the roster or confirm bucket.

    - Plans / schedules → roster (they carry the element list).
    - Sections / details / elevations / notes → confirm (they carry depths).
    - Anything else flagged carries_concrete_dims=True → confirm (safer to
      read than to drop; roster needs plan-style information specifically).
    """
    roster_pages = []
    confirm_pages = []
    for page in range(1, page_count + 1):
        info = (triage or {}).get(page) or {}
        st = info.get("sheet_type", "other")
        carries = info.get("carries_concrete_dims", False)
        if st in ROSTER_SHEET_TYPES:
            roster_pages.append(page)
        elif st in CONFIRM_SHEET_TYPES:
            confirm_pages.append(page)
        elif carries:
            confirm_pages.append(page)
    return roster_pages, confirm_pages


def _pack_confirm_batches(by_page, confirm_pages, remaining_budget, batch_cap):
    """Pure: pack per-page tile lists into batches under batch_cap tiles each.

    Truncates total tiles to remaining_budget (the global image cap minus
    what the roster already consumed). A page whose tile count alone exceeds
    batch_cap becomes its own oversize batch — splitting one sheet's tiles
    across two confirm calls would break the "same page = one sheet" rule
    the model uses to reassemble overlapping tiles.
    """
    batches = []
    current = []
    used = 0
    for page in confirm_pages:
        if used >= remaining_budget:
            break
        tiles = by_page.get(page, [])
        if not tiles:
            continue
        if used + len(tiles) > remaining_budget:
            tiles = tiles[: remaining_budget - used]
        if not tiles:
            continue
        if current and len(current) + len(tiles) > batch_cap:
            batches.append(current)
            current = []
        current.extend(tiles)
        used += len(tiles)
    if current:
        batches.append(current)
    return batches


def _render_tiles_for_pages(pdf_path, pages_1based):
    """Render given 1-based pages as high-res tiles in a single PDF open.

    Returns {page_no: [tile dicts]}. A failed page gets an empty list so
    the caller can iterate without per-page guards.
    """
    by_page = {}
    if not pages_1based:
        return by_page
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        for page_no in pages_1based:
            try:
                with _RENDER_LOCK:
                    page = pdf[page_no - 1]
                    try:
                        pw, ph = page.get_size()
                        long_pt = max(pw, ph) or 1.0
                        scale = TILE_RENDER_SCALE
                        if long_pt * scale > MAX_RENDER_PX:
                            scale = MAX_RENDER_PX / long_pt
                        by_page[page_no] = _render_tiles(page, page_no, scale)
                    finally:
                        page.close()
            except Exception as e:
                app.logger.warning("Tile render failed for page %d: %s",
                                   page_no, e)
                by_page.setdefault(page_no, [])
    finally:
        pdf.close()
    return by_page


def select_vision_buckets(pdf_path, triage, page_texts):
    """Bucket sheets by triage role, render full-res tiles per bucket.

    Returns (roster_images, confirm_batches):
    - roster_images: one flat tile list — Pass 1 reads them all in one call.
    - confirm_batches: list of tile lists, each sized to fit one confirm
      call's image budget; confirm passes fan out one batch per call.

    Respects the global MAX_VISION_IMAGES cap across both buckets so a
    sprawling sheet set can't overflow the API's per-call image limit.
    """
    n = len(page_texts)
    if not triage or n == 0:
        return [], []
    roster_pages, confirm_pages = _bucket_pages_by_triage(n, triage)
    by_page = _render_tiles_for_pages(pdf_path, roster_pages + confirm_pages)

    roster_images = []
    for page in roster_pages:
        roster_images.extend(by_page.get(page, []))
    if len(roster_images) > MAX_VISION_IMAGES:
        roster_images = roster_images[:MAX_VISION_IMAGES]
    remaining = max(0, MAX_VISION_IMAGES - len(roster_images))

    confirm_batches = _pack_confirm_batches(
        by_page, confirm_pages, remaining, CONFIRM_BATCH_TILES)
    return roster_images, confirm_batches


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

# Geometry classification tags — mirror SKILL.md Step 2.5 exactly so the model
# uses one vocabulary across the skill instructions and the tool schema. The
# server still computes volume rectangularly (width x length x depth x qty);
# these tags are metadata until the verifier dispatches on them.
GEOMETRY_TYPES = [
    "RECT_PRISM",
    "TRAPEZOIDAL_PRISM",
    "STEPPED_PRISM",
    "TAPERED_WALL",
    "CYLINDER",
    "FRUSTUM",
    "CUSTOM",
]

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
                    "required": ["element_id", "name", "category",
                                 "geometry_type", "width_ft", "length_ft",
                                 "depth_ft", "qty", "cubic_feet",
                                 "cubic_yards", "notes"],
                    "properties": {
                        "element_id": {
                            "type": "string",
                            "description": (
                                "Stable unique identifier for this physical "
                                "element (e.g. F1, F2, W1, S1, C1). One ID "
                                "per element — the same ID identifies it "
                                "wherever it appears across plan, schedule, "
                                "and section sheets."
                            ),
                        },
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
                        "geometry_type": {
                            "type": "string",
                            "enum": GEOMETRY_TYPES,
                            "description": (
                                "Geometry classification — the SKILL.md Step "
                                "2.5 tag for this element. RECT_PRISM = "
                                "constant thickness; TRAPEZOIDAL_PRISM = "
                                "thickness varies linearly (sloped slabs, "
                                "sump floors, tapered mats); STEPPED_PRISM = "
                                "discrete thickness steps; TAPERED_WALL = "
                                "battered wall; CYLINDER = round "
                                "column/caisson; FRUSTUM = round pier with "
                                "varying radius; CUSTOM = decomposed into "
                                "sub-shapes. width_ft, length_ft and depth_ft "
                                "MUST still be populated so width × length × "
                                "depth × qty equals the true volume "
                                "regardless of this tag; d_min, d_max and "
                                "radius_ft are metadata."
                            ),
                        },
                        "width_ft":    {"type": "number"},
                        "length_ft":   {"type": "number"},
                        "depth_ft":    {"type": "number"},
                        "d_min": {
                            "type": "number",
                            "description": (
                                "Minimum thickness in feet — set for "
                                "TRAPEZOIDAL_PRISM and TAPERED_WALL elements."
                            ),
                        },
                        "d_max": {
                            "type": "number",
                            "description": (
                                "Maximum thickness in feet — set for "
                                "TRAPEZOIDAL_PRISM and TAPERED_WALL elements."
                            ),
                        },
                        "radius_ft": {
                            "type": "number",
                            "description": (
                                "Radius in feet — set for CYLINDER and "
                                "FRUSTUM elements."
                            ),
                        },
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


# --- Pass 0: vision-based sheet triage ------------------------------------
# Per-sheet roles. Roster sheets carry the element list (plan + schedule);
# confirm sheets carry the depths/heights/thicknesses that plans omit.
ROSTER_SHEET_TYPES = {"plan", "schedule"}
CONFIRM_SHEET_TYPES = {"section", "detail", "elevation", "notes"}
SHEET_TYPES = sorted(ROSTER_SHEET_TYPES | CONFIRM_SHEET_TYPES |
                     {"cover", "other"})

# Triage tool — Pass 0 classifies every sheet by type so the full-res tile
# budget targets dimension-bearing sheets directly, replacing the brittle
# text-keyword heuristic in _score_page_priority. Scanned sets have no text
# layer, so vision-based classification is the only signal that works on
# both vector and scanned input.
_TRIAGE_TOOL = {
    "name": "classify_sheets",
    "description": (
        "Classify every drawing sheet by its role (plan, section, etc.) "
        "and whether it carries concrete dimensions."
    ),
    "input_schema": {
        "type": "object",
        "required": ["sheets"],
        "properties": {
            "sheets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["page", "sheet_type",
                                 "carries_concrete_dims"],
                    "properties": {
                        "page": {
                            "type": "integer",
                            "description": (
                                "1-based page number of the sheet in the PDF."
                            ),
                        },
                        "sheet_type": {
                            "type": "string",
                            "enum": SHEET_TYPES,
                            "description": (
                                "Role of this sheet. 'plan' = plan view "
                                "(foundation/structural/framing plan); "
                                "'schedule' = tabular schedule of footings, "
                                "columns, walls, etc.; 'section' = cut "
                                "section showing depths/heights; 'detail' = "
                                "enlarged detail with reinforcement or "
                                "dimensions; 'elevation' = building "
                                "elevation; 'notes' = general notes or "
                                "specifications; 'cover' = title or index "
                                "sheet; 'other' = anything else."
                            ),
                        },
                        "carries_concrete_dims": {
                            "type": "boolean",
                            "description": (
                                "True iff this sheet shows concrete "
                                "callouts, schedules, or sections with "
                                "depth/thickness — the sheets a takeoff "
                                "needs to read."
                            ),
                        },
                    },
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


# --- Multi-pass merge (Fix 1) ---------------------------------------------
# Roster wins on plan-view data (the geometry it sees first-hand); confirm
# passes win on section/detail data (depths, thicknesses, geometry tags the
# plan view doesn't show).
_ROSTER_AUTHORITATIVE_FIELDS = (
    "width_ft", "length_ft", "qty", "category", "name", "element_id",
)
_CONFIRM_AUTHORITATIVE_FIELDS = (
    "depth_ft", "d_min", "d_max", "radius_ft", "geometry_type", "notes",
)
# Values that count as "no data" for merge purposes — never overwrite a real
# value with one of these. Lets a confirm pass that omits depth_ft (or sets
# it to 0 because it couldn't read the callout) keep the roster's provisional.
_EMPTY_MERGE_VALUES = (None, "", 0, 0.0)


def _normalize_name(name):
    """Lowercase + collapse whitespace for fingerprint comparison."""
    return " ".join((name or "").lower().split())


def _fingerprint(el):
    """Stable hash of (category, plan-dim, name) for dedup of confirm additions.

    Width/length rounded to 0.1 ft (~1") so minor extraction noise doesn't
    create false negatives. Category is included so two same-shape elements
    of different types (a 2×4 footing vs. a 2×4 pad) don't collapse.
    """
    cat = (el.get("category") or "").strip()
    try:
        w = round(float(el.get("width_ft") or 0), 1)
        l = round(float(el.get("length_ft") or 0), 1)
    except (TypeError, ValueError):
        w = l = 0.0
    return (cat, w, l, _normalize_name(el.get("name")))


def _merge_element_field(existing, new, field, prefer_new):
    """Update one field on `existing` from `new`. Returns True if changed.

    Never overwrites a real value with an empty/zero/None. When both sides
    have real values: `prefer_new=True` updates, `False` keeps existing.
    """
    if field not in new:
        return False
    new_val = new[field]
    if new_val in _EMPTY_MERGE_VALUES:
        return False
    old_val = existing.get(field)
    if old_val in _EMPTY_MERGE_VALUES:
        existing[field] = new_val
        return True
    if not prefer_new or old_val == new_val:
        return False
    existing[field] = new_val
    return True


def _merge_elements(roster_elements, confirm_results):
    """Merge roster + confirm-pass element lists by element_id, dedup additions.

    Algorithm:
    1. Seed `by_id` from the roster. Auto-assign IDs to any roster element
       that didn't get one from Pass 1.
    2. For each confirm-pass element: if its element_id matches an existing
       row, update authoritative fields (roster wins width/length/qty;
       confirm wins depth/thickness/geometry/notes).
    3. Otherwise, the confirm pass is adding a new element. Compute its
       fingerprint (category, plan dims, normalized name) and dedup against
       everything already in `by_id` — a confirm pass that re-discovers a
       roster element gets dropped, and two confirm passes adding the same
       new element only land it once. Server assigns a `auto_C{pass}_{n}`
       ID so two passes can't collide on a suggested ID.

    Returns (merged_list, audit_entries). audit_entries describes every
    auto-ID assignment, field update, addition, and dedup for the response.
    """
    audit = []
    by_id = {}
    auto_seq = 0
    for el in roster_elements or []:
        eid = (el.get("element_id") or "").strip()
        if not eid:
            auto_seq += 1
            eid = f"auto_R{auto_seq}"
            el = dict(el, element_id=eid)
            audit.append({"kind": "roster_id_assigned",
                          "element_id": eid,
                          "name": el.get("name", "(unnamed)")})
        if eid in by_id:
            audit.append({"kind": "duplicate_roster_id",
                          "element_id": eid,
                          "name": el.get("name", "(unnamed)")})
            continue
        by_id[eid] = dict(el)

    seen_fingerprints = {_fingerprint(el): eid for eid, el in by_id.items()}

    for pass_idx, confirm_pass in enumerate(confirm_results or []):
        add_seq = 0
        for el in confirm_pass or []:
            eid = (el.get("element_id") or "").strip()
            if eid and eid in by_id:
                changed = []
                for f in _ROSTER_AUTHORITATIVE_FIELDS:
                    if _merge_element_field(by_id[eid], el, f,
                                            prefer_new=False):
                        changed.append(f)
                for f in _CONFIRM_AUTHORITATIVE_FIELDS:
                    if _merge_element_field(by_id[eid], el, f,
                                            prefer_new=True):
                        changed.append(f)
                if changed:
                    audit.append({"kind": "confirm_update",
                                  "element_id": eid,
                                  "pass": pass_idx,
                                  "fields": changed})
                continue
            fp = _fingerprint(el)
            if fp in seen_fingerprints:
                audit.append({"kind": "deduped_addition",
                              "name": el.get("name", "(unnamed)"),
                              "pass": pass_idx,
                              "matched_existing": seen_fingerprints[fp]})
                continue
            add_seq += 1
            new_eid = f"auto_C{pass_idx + 1}_{add_seq}"
            new_el = dict(el, element_id=new_eid)
            by_id[new_eid] = new_el
            seen_fingerprints[fp] = new_eid
            audit.append({"kind": "confirm_addition",
                          "element_id": new_eid,
                          "pass": pass_idx,
                          "name": new_el.get("name", "(unnamed)")})

    return list(by_id.values()), audit


# Fields persisted in the roster JSON sent to confirm passes. Keeps the
# payload small (skip notes / cubic_* / raw model output) while preserving
# every field needed to match a section drawing back to its roster row.
_ROSTER_JSON_FIELDS = ("element_id", "name", "category", "geometry_type",
                       "width_ft", "length_ft", "depth_ft", "qty",
                       "d_min", "d_max", "radius_ft")


def _roster_json(elements):
    """Compact JSON view of the roster for inclusion in confirm-pass prompts."""
    return json.dumps([
        {k: el.get(k) for k in _ROSTER_JSON_FIELDS if el.get(k) is not None}
        for el in elements or []
    ], indent=2)


def _build_pass_user_content(prompt_text, images, pass_intro,
                             role_specific_instructions, filename,
                             lessons_section):
    """Assemble the user-message content for one extraction pass.

    Carries the full critical-rules block, category list, and geometry pin
    on every pass — the accuracy constraint says don't trade those for
    cost. `pass_intro` and `role_specific_instructions` vary per pass; the
    rest is identical to the single-call prompt.
    """
    categories_list = ", ".join(f'"{c}"' for c in CONCRETE_CATEGORIES)
    body = f"""{pass_intro}
{lessons_section}
CRITICAL RULES (validated from expert human corrections — follow exactly, in addition to the concrete-takeoff skill workflow):
1. CONCRETE ONLY — DO NOT include electrical, plumbing, mechanical, HVAC, structural steel, wood framing, drywall, finishes, or any non-concrete material as elements. If a piece of equipment sits on a concrete pad, include ONLY the pad (category "Equipment Pads"), not the equipment.
2. SLOPED / TAPERED SLAB VOLUMES: Any sump, pit, or base slab described with a percentage slope is a wedge-shaped concrete element that must be computed as a SEPARATE, POSITIVE line item using: V = 0.5 × (h_start + h_end) × width × length. Do NOT treat it as a simple deduction.
3. MULTIPLE ROOF SLAB SECTIONS: If the roof slab plan or sections show different annotated thicknesses for different zones, extract EACH zone as its own element with its correct depth.
4. INTERIOR WALL THICKNESS: Interior dividing walls are frequently thinner than perimeter walls. Always look for an explicit dimension callout on the interior wall — do NOT default to the perimeter wall thickness.
5. Cross-check dimensions with other sheets to confirm the correct measurement before going to calculations.

CATEGORY FIELD (REQUIRED) — every element you return MUST carry one of these category strings, chosen by what the element IS in construction terms:
{categories_list}
If you cannot avoid extracting a non-concrete item, set its category to "Non-Concrete" so the server can drop it cleanly.

GEOMETRY & ELEMENT-ID FIELDS (REQUIRED) — per SKILL.md Step 2.5:
- element_id: every physical element gets a stable unique ID (F1, F2, W1, S1, C1, …); the SAME ID must identify that element across plan, schedule, and section sheets.
- geometry_type: one of RECT_PRISM, TRAPEZOIDAL_PRISM, STEPPED_PRISM, TAPERED_WALL, CYLINDER, FRUSTUM, CUSTOM.
- d_min / d_max for TRAPEZOIDAL_PRISM and TAPERED_WALL; radius_ft for CYLINDER / FRUSTUM.

CRITICAL — width_ft, length_ft, and depth_ft MUST always be populated so that width × length × depth × qty equals the element's TRUE volume, for EVERY geometry_type. geometry_type / d_min / d_max / radius_ft are metadata only — the server computes volume from width × length × depth × qty.
- TRAPEZOIDAL_PRISM (sloped slab): set depth_ft to the volume-effective average 0.5 × (d_min + d_max); width_ft and length_ft are plan dimensions.
- TAPERED_WALL (battered wall): set width_ft to the volume-effective average 0.5 × (d_min + d_max); length_ft is the wall run, depth_ft is the wall height.
- CYLINDER / FRUSTUM (round): set width_ft and length_ft so their product equals π × radius², depth_ft = height.
- STEPPED_PRISM / CUSTOM: emit one RECT_PRISM row per rectangular sub-shape, OR one row whose depth_ft = total volume ÷ (width_ft × length_ft).

HOW THE DRAWINGS ARE PROVIDED: Drawing sheets are attached as images. Tiles with the same page number belong to ONE physical sheet — reassemble them in your mind before reading dimensions. Adjacent tiles overlap, so the same element / callout can appear in two neighbors — count each physical element ONLY ONCE.

Below is the extracted text from the drawings (page markers included):

<drawings>
{prompt_text}
</drawings>

{role_specific_instructions}"""

    if images:
        user_content = [{"type": "text", "text": body}]
        for img in images:
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
        return user_content
    return body


def _call_takeoff(user_content, max_tokens=12000, force_tool=False):
    """Issue one takeoff API call and return (parsed_input, prose, truncated).

    `force_tool=True` uses tool_choice={"type":"tool","name":"return_takeoff"}
    to guarantee a structured response. Returns (None, prose, truncated) when
    the model returned no tool call and no salvageable JSON.
    """
    tool_choice = ({"type": "tool", "name": "return_takeoff"} if force_tool
                   else {"type": "auto"})
    resp = _chat_call(
        messages=[{"role": "user", "content": user_content}],
        tools=[_TAKEOFF_TOOL],
        tool_choice=tool_choice,
        max_tokens=max_tokens,
    )
    prose = "".join(getattr(b, "text", "") for b in resp.content)
    truncated = getattr(resp, "stop_reason", None) == "max_tokens"
    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_block:
        return tool_block.input, prose, truncated
    return _parse_json_block(prose), prose, truncated


def _extract_multi_pass(pdf_text, vision_plan, filename):
    """Fix 1 multi-pass extraction.

    Pass 1 (serial) reads the roster sheets and warms the SKILL cache. Pass
    2…N run concurrently (bounded by `_API_CALL_SEMAPHORE`), each receiving
    the roster JSON and a batch of confirm-sheet tiles. Reduce merges all
    pass outputs via `_merge_elements`; the existing verifier pipeline runs
    on the merged list unchanged.

    Falls back to the single-call path if Pass 1 fails or returns nothing.
    """
    roster_images = vision_plan.get("roster") or []
    confirm_batches = vision_plan.get("confirm_batches") or []

    if len(pdf_text) > MAX_PDF_CHARS:
        head = pdf_text[: int(MAX_PDF_CHARS * 0.8)]
        tail = pdf_text[-int(MAX_PDF_CHARS * 0.2):]
        prompt_text = f"{head}\n\n[... middle truncated ...]\n\n{tail}"
    else:
        prompt_text = pdf_text

    lessons_block = format_lessons_for_prompt()
    lessons_section = f"\n{lessons_block}\n" if lessons_block else ""

    roster_intro = (
        f"Analyzing a PDF of construction drawings: {filename}\n\n"
        f"This is the ROSTER PASS — you are reading the PLAN and SCHEDULE "
        f"sheets. Enumerate EVERY concrete element you can see, with a "
        f"stable element_id (F1, F2, W1, S1, …), category, geometry_type, "
        f"plan dimensions (width_ft, length_ft), and qty. For depths / "
        f"heights / thicknesses you cannot read in the plan or schedule, "
        f"use a reasonable construction default and add 'provisional' to "
        f"the notes — confirm passes will refine them from section "
        f"drawings."
    )
    roster_instructions = (
        "Work through SKILL Steps 1, 2, and 2.5 in prose first, then call "
        "return_takeoff. Include cubic_feet and cubic_yards based on the "
        "best dimensions you have; the server recomputes volumes from "
        "width × length × depth × qty."
    )

    roster_content = _build_pass_user_content(
        prompt_text, roster_images, roster_intro, roster_instructions,
        filename, lessons_section)

    try:
        roster_parsed, roster_prose, roster_trunc = _call_takeoff(
            roster_content, max_tokens=16000, force_tool=False)
    except Exception as e:
        app.logger.warning("Roster pass failed: %s — falling back to "
                           "single-call.", e)
        return None
    if not (roster_parsed or {}).get("elements"):
        # Retry once with forced tool, then give up to single-call fallback.
        try:
            roster_parsed, roster_prose, roster_trunc = _call_takeoff(
                roster_content, max_tokens=12000, force_tool=True)
        except Exception as e:
            app.logger.warning("Roster retry failed: %s", e)
            return None
    if not (roster_parsed or {}).get("elements"):
        app.logger.warning("Roster pass returned no elements; falling back.")
        return None

    roster_elements = roster_parsed.get("elements") or []

    confirm_results = []
    truncated_any = roster_trunc
    if confirm_batches:
        roster_json = _roster_json(roster_elements)
        confirm_intro_tmpl = (
            f"Analyzing a PDF of construction drawings: {filename}\n\n"
            f"This is a CONFIRM PASS — you are reading SECTION / DETAIL "
            f"sheets to refine depths, heights, thicknesses, and geometry "
            f"for elements identified by the plan/schedule roster. You may "
            f"also find concrete elements that the roster missed (e.g. "
            f"interior walls or sumps visible only in section)."
        )
        confirm_instructions = (
            f"CURRENT ROSTER (from Pass 1; JSON):\n"
            f"<roster>\n{roster_json}\n</roster>\n\n"
            f"For elements in the roster whose depths/heights/thicknesses "
            f"you can read in the attached sections, return an updated "
            f"element with the SAME element_id and the corrected depth_ft, "
            f"d_min/d_max, geometry_type, radius_ft, and notes — width_ft/"
            f"length_ft/qty should match the roster.\n\n"
            f"For NEW concrete elements only visible in section that are "
            f"NOT in the roster, return them with any element_id you like "
            f"(the server will assign a final unique ID).\n\n"
            f"Do NOT return roster elements you are not updating, and do "
            f"NOT duplicate elements you can see are already in the "
            f"roster. Call return_takeoff with only the updated and "
            f"newly-added elements."
        )

        max_workers = max(1, min(_API_CALL_CONCURRENCY, len(confirm_batches)))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers) as ex:
            futures = []
            for batch in confirm_batches:
                batch_content = _build_pass_user_content(
                    prompt_text, batch, confirm_intro_tmpl,
                    confirm_instructions, filename, lessons_section)
                futures.append(ex.submit(_call_takeoff, batch_content,
                                          12000, True))
            for fut in futures:
                try:
                    parsed, _prose, trunc = fut.result()
                    truncated_any = truncated_any or trunc
                    confirm_results.append(
                        (parsed or {}).get("elements") or [])
                except Exception as e:
                    app.logger.warning("Confirm pass failed: %s", e)
                    confirm_results.append([])

    merged_elements, merge_audit = _merge_elements(roster_elements,
                                                    confirm_results)

    parsed = {
        "elements": merged_elements,
        "summary": dict((roster_parsed or {}).get("summary") or {}),
    }
    return _finalize_takeoff_result(
        parsed, roster_prose, truncated_any,
        multi_pass_meta={
            "applied": True,
            "roster_count": len(roster_elements),
            "confirm_pass_count": len(confirm_results),
            "merge_audit": merge_audit,
        })


def _finalize_takeoff_result(parsed, prose_response, truncated,
                              multi_pass_meta=None):
    """Run the shared verifier / grouping / sanity-check pipeline on a parsed
    takeoff and assemble the final response dict. Used by both the single-call
    and multi-pass paths so the response shape is identical."""
    audit = _verify_element_volumes(parsed.get("elements") or [])
    kept, dropped, by_category = _filter_and_group_elements(
        parsed.get("elements") or [])
    parsed["elements"] = kept
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
    if multi_pass_meta is not None:
        parsed["summary"]["multi_pass"] = multi_pass_meta
    if truncated:
        parsed["summary"]["truncated"] = True
        warn = ("WARNING: the model response hit its max_tokens limit, so "
                "this takeoff may be incomplete. Re-run to confirm.")
        if isinstance(parsed["summary"].get("assumptions"), list):
            parsed["summary"]["assumptions"].append(warn)
        else:
            parsed["summary"]["assumptions"] = [warn]
    if geometry_warnings:
        gwarn = (f"WARNING: {len(geometry_warnings)} geometric sanity "
                 f"check(s) flagged this takeoff as possibly over-counted "
                 f"or misread — see verification.geometry_warnings.")
        if isinstance(parsed["summary"].get("assumptions"), list):
            parsed["summary"]["assumptions"].append(gwarn)
        else:
            parsed["summary"]["assumptions"] = [gwarn]
    if not parsed.get("elements") and prose_response:
        parsed["summary"]["analysis"] = prose_response
    return parsed


def extract_quantities_with_claude(pdf_text, page_images, filename,
                                    vision_plan=None):
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

    # If the upload route built a multi-pass plan from triage, try it
    # first. _extract_multi_pass returns None on any failure (roster
    # pass empty, triage was useless, etc.) so we fall through to the
    # single-call path using page_images as a complete render set.
    if vision_plan and (vision_plan.get("roster")
                        or vision_plan.get("confirm_batches")):
        try:
            mp_result = _extract_multi_pass(pdf_text, vision_plan, filename)
        except Exception as e:
            app.logger.warning("Multi-pass extraction errored: %s — "
                                "falling back to single-call.", e)
            mp_result = None
        if mp_result is not None:
            return mp_result

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

    # System prompt (SKILL + role) is built and cached centrally by
    # _chat_call/_system_blocks — no per-request reconstruction needed.
    # The cache_control breakpoint lives on the SKILL block in _system_blocks
    # so every call inside the 5-minute window reads the prefix at ~10% cost.

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

GEOMETRY & ELEMENT-ID FIELDS (REQUIRED) — in addition to the SKILL.md Step 2.5 geometry classification you already perform:
- element_id: give every physical element a stable unique ID (F1, F2, W1, S1, C1, …) and return it in the `element_id` field. One ID per physical element — the same ID must identify that element wherever it appears across plan, schedule, and section sheets.
- geometry_type: return the Step 2.5 tag — one of RECT_PRISM, TRAPEZOIDAL_PRISM, STEPPED_PRISM, TAPERED_WALL, CYLINDER, FRUSTUM, CUSTOM.
- d_min / d_max: for TRAPEZOIDAL_PRISM and TAPERED_WALL, return the minimum and maximum thickness in feet.
- radius_ft: for CYLINDER and FRUSTUM, return the radius in feet.

CRITICAL — width_ft, length_ft, and depth_ft MUST always be populated so that width_ft × length_ft × depth_ft × qty equals the element's TRUE volume, for EVERY geometry_type. geometry_type, d_min, d_max, and radius_ft are metadata only — the server computes volume from width × length × depth × qty, so a blank or zero dimension silently zeroes the element. Map each non-rectangular shape onto the three dimensions:
- TRAPEZOIDAL_PRISM (sloped slab, sump floor, tapered mat): the slab THICKNESS varies — set depth_ft to the volume-effective average 0.5 × (d_min + d_max); width_ft and length_ft are the plan dimensions.
- TAPERED_WALL (battered wall): the wall THICKNESS varies — set width_ft to the volume-effective average 0.5 × (d_min + d_max); length_ft is the wall run and depth_ft is the wall height.
- CYLINDER / FRUSTUM (round column, pier, caisson): set width_ft and length_ft so their product equals the circular area π × radius², and depth_ft to the height — so width × length × depth = π × radius² × height.
- STEPPED_PRISM / CUSTOM: either emit one RECT_PRISM row per rectangular sub-shape, or one row whose depth_ft = total true volume ÷ (width_ft × length_ft) so the product still equals the true volume.

HOW THE DRAWINGS ARE PROVIDED: Drawing sheets are attached as images. A large sheet is sliced into a grid of overlapping high-resolution tiles, each labeled "[Page N — tile row R of …, col C of …]"; a smaller or lower-priority sheet is attached as one reduced-resolution thumbnail labeled "[Page N — whole-sheet overview …]". Every image carrying the same page number is part of ONE physical sheet — reassemble its tiles in your mind into the full sheet before reading dimensions. Because adjacent tiles overlap, the same footing, wall, column, or dimension callout can appear in two neighboring tiles — count each physical element ONLY ONCE. Pages with no attached image are represented by their extracted text only.

Below is the extracted text from the drawings (page markers included):

<drawings>
{prompt_text}
</drawings>

Work through the concrete-takeoff skill in this single response, in order:

STEPS 1-2.5 — IDENTIFY, EXTRACT & CLASSIFY (do this in prose first): Follow Step 1 (Ingest and Orient), Step 2 (Extract Dimensions), and Step 2.5 (Classify Geometry). Identify EVERY concrete structural element you can see (footings, slab on grade, walls, columns, piers, slabs on deck, equipment pads, sidewalks, driveways, sumps, sloped bases, etc.). For each, note:
- A descriptive name and unique element ID (F1, F2, W1, S1, C1, etc.) — return the ID in the element_id field
- Its concrete category from the list above
- Its geometry_type — the Step 2.5 tag (RECT_PRISM, TRAPEZOIDAL_PRISM, STEPPED_PRISM, TAPERED_WALL, CYLINDER, FRUSTUM, or CUSTOM)
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
    resp = _chat_call(
        messages=history,
        tools=[_TAKEOFF_TOOL],
        tool_choice={"type": "auto"},
        max_tokens=16000,
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
    #
    # The retry only restructures the prose analysis Claude already produced
    # into a return_takeoff call — it does not re-read the drawings. So the
    # retry conversation is seeded with a TEXT-ONLY copy of the first user turn
    # (the `initial` prompt, no image blocks), keeping the ~135K-token drawing-
    # tile payload out of every retry request. Same rationale as collapsing the
    # old two-call flow: re-sending drawings to a structuring step spends
    # tokens for no accuracy gain — Python recomputes every volume regardless.
    MAX_TOOL_RETRIES = 2
    retry_messages = None
    for _ in range(MAX_TOOL_RETRIES):
        if (parsed or {}).get("elements"):
            break
        if len((prose_response or "").strip()) < 100:
            break
        if retry_messages is None:
            retry_messages = [{"role": "user", "content": initial}]
        retry_messages.append({"role": "assistant", "content": resp.content})
        if tool_block:
            retry_messages.append({
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
            retry_messages.append({
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
        resp = _chat_call(
            messages=retry_messages,
            tools=[_TAKEOFF_TOOL],
            tool_choice={"type": "tool", "name": "return_takeoff"},
            max_tokens=8000,
        )
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_block:
            parsed = tool_block.input

    if parsed:
        # Deterministic Python recompute + grouping + sanity checks, shared
        # with the multi-pass path so the response shape is identical.
        return _finalize_takeoff_result(parsed, prose_response, truncated)

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
            # Try Pass 0 vision triage first when the sheet set is large
            # enough to benefit from multi-pass. Small sets and triage
            # failures fall back to the keyword-heuristic single-call path.
            vision_plan = None
            page_images = []
            if len(page_texts) > SMALL_SET_THRESHOLD:
                triage = triage_sheets(save_path, len(page_texts))
                if triage:
                    roster_images, confirm_batches = select_vision_buckets(
                        save_path, triage, page_texts)
                    if roster_images or confirm_batches:
                        vision_plan = {
                            "mode": "multi_pass",
                            "roster": roster_images,
                            "confirm_batches": confirm_batches,
                            "triage": triage,
                        }
                        # For metrics + single-call fallback: every tile the
                        # multi-pass plan would send, in one flat list.
                        page_images = roster_images + [
                            t for b in confirm_batches for t in b]
            if vision_plan is None:
                page_images = select_and_render_vision(save_path, page_texts)
            result = extract_quantities_with_claude(
                pdf_text, page_images, filename, vision_plan=vision_plan)
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
