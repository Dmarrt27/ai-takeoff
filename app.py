"""
AI Takeoff Prototype - Concrete Quantity Extraction
Flask backend for PDF-based quantity takeoff
"""

import os
import gc
import io
import json
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
client = Anthropic(api_key=_API_KEY) if _API_KEY else None

# Model used for analysis. Can be overridden with CLAUDE_MODEL env var.
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

# Cap text sent to Claude. Drawings PDFs are text-light per page; this gives
# good coverage of a 25-30 page set without blowing the context window.
MAX_PDF_CHARS = 40000

# Pages with fewer than this many characters are assumed to be vector/raster
# drawings with no machine-readable text; they are rendered to images for vision.
VISION_TEXT_THRESHOLD = 100
# Maximum pages to render — limits memory and per-request vision token cost.
MAX_VISION_PAGES = 10

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
    and exposes explicit close() calls. For large image-heavy drawing PDFs
    this keeps peak memory bounded to one page at a time instead of the
    whole document, which is what PyPDF2 was doing.

    Returns (full_text, sparse_page_indices) where sparse_page_indices are
    0-based page numbers whose text yield fell below VISION_TEXT_THRESHOLD —
    candidates for vision-based rendering.
    """
    text_parts = []
    sparse_pages = []
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
            text_parts.append(f"\n--- PAGE {i+1} ---\n{page_text}")
            if len(page_text.strip()) < VISION_TEXT_THRESHOLD:
                sparse_pages.append(i)
    finally:
        pdf.close()
    return "".join(text_parts), sparse_pages


def render_pages_as_images(pdf_path, page_indices):
    """Render sparse PDF pages to JPEG images for Claude vision.

    Pages are rendered at 2x scale (144 DPI) then resized to fit within
    1568px — Claude's optimal input size. Returns a list of dicts with
    page number, base64-encoded JPEG data, and media_type.
    """
    images = []
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        for i in page_indices[:MAX_VISION_PAGES]:
            page = pdf[i]
            try:
                bitmap = page.render(scale=2.0)
                try:
                    pil_img = bitmap.to_pil()
                    w, h = pil_img.size
                    if max(w, h) > 1568:
                        ratio = 1568 / max(w, h)
                        pil_img = pil_img.resize(
                            (int(w * ratio), int(h * ratio)), Image.LANCZOS
                        )
                    buf = io.BytesIO()
                    pil_img.save(buf, format='JPEG', quality=85)
                    images.append({
                        'page': i + 1,
                        'data': base64.b64encode(buf.getvalue()).decode('utf-8'),
                        'media_type': 'image/jpeg',
                    })
                finally:
                    bitmap.close()
            finally:
                page.close()
    finally:
        pdf.close()
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
                    "required": ["name", "width_ft", "length_ft", "depth_ft",
                                 "qty", "cubic_feet", "cubic_yards", "notes"],
                    "properties": {
                        "name":        {"type": "string"},
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


def _verify_element_volumes(elements, waste_factor=WASTE_FACTOR):
    """Replace Claude's per-element cubic_feet / cubic_yards with values
    computed from width × length × depth × qty in Python. Returns the audit
    list so the response can surface every override that happened.

    The geometric truth is w·l·d·qty in cubic feet. Claude's job is to
    extract dimensions; arithmetic on those dimensions is Python's job.
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

    return audit


def extract_quantities_with_claude(pdf_text, page_images, filename):
    """Two-turn conversation: identify elements, then compute volumes.

    page_images is a list of {page, data, media_type} dicts rendered from
    sparse pages. When present they are attached to Turn 1 so Claude can
    read dimension callouts directly from the vector/raster drawing layers.
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
    prose_response = ""  # first-turn prose — saved as fallback analysis text

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

    initial = f"""Analyzing a PDF of construction drawings: {filename}
{lessons_section}
CRITICAL RULES (validated from expert human corrections — follow exactly, in addition to the concrete-takeoff skill workflow):
1. SLOPED / TAPERED SLAB VOLUMES: Any sump, pit, or base slab described with a percentage slope (e.g. "5% slope", "slopes to drain") is a wedge-shaped concrete element that must be computed as a SEPARATE, POSITIVE line item using: V = 0.5 × (h_start + h_end) × width × length ÷ 46656 (in³→yd³). h_end = h_start + (slope_pct/100) × length_in. Do NOT treat it as a simple deduction.
2. MULTIPLE ROOF SLAB SECTIONS: If the roof slab plan or sections show different annotated thicknesses for different zones, extract EACH zone as its own element with its correct depth. Never apply one uniform thickness to the entire footprint when multiple depths are shown.
3. INTERIOR WALL THICKNESS: Interior dividing walls are frequently thinner than perimeter walls. Always look for an explicit dimension callout on the interior wall — do NOT default to the perimeter wall thickness. If no callout exists, flag the row as uncertain.
4. Cross check dimensions with other sheets to confirm the correct measurement before going to calculations.  

Below is the extracted text from the drawings (page markers included):

<drawings>
{prompt_text}
</drawings>

Follow Step 1 (Ingest and Orient) and Step 2 (Extract Dimensions) of the concrete-takeoff skill. Identify EVERY concrete structural element you can see (footings, slab on grade, walls, columns, piers, slabs on deck, equipment pads, sidewalks, driveways, sumps, sloped bases, etc.). For each, list:
- A descriptive name and unique element ID (F1, F2, W1, S1, C1, etc.)
- Width, length, depth/thickness (in feet; convert inches: 6\" = 0.5 ft)
- Quantity if there are multiples (e.g., 9 column footings)
- Any reinforcement / strength specs you noticed
- For sloped elements: starting height, ending height, slope percentage, and the wedge formula used

If a dimension is not stated, use a reasonable construction default and note it. Respond with prose first, then a JSON list."""
    # Build Turn 1 content: text prompt always present; attach rendered page
    # images when available so Claude can read dimension callouts directly.
    if page_images:
        turn1_content = [{"type": "text", "text": initial}]
        for img in page_images:
            turn1_content.append({
                "type": "text",
                "text": f"\nRendered drawing — page {img['page']}:",
            })
            turn1_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img['media_type'],
                    "data": img['data'],
                },
            })
    else:
        turn1_content = initial

    history.append({"role": "user", "content": turn1_content})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=2500,
        system=system_prompt,
        messages=history,
    )
    prose_response = resp.content[0].text
    history.append({"role": "assistant", "content": prose_response})

    calc = """Now perform Step 3 (Calculate Quantities) of the concrete-takeoff skill and compute the concrete volume for each element. Apply a 5% waste/overbreak factor unless drawings specify otherwise, and run the Quality Checks before calling the return_takeoff tool.

Standard rectangular elements:
  cubic_feet = width_ft × length_ft × depth_ft × qty
  cubic_yards = cubic_feet / 27

Tapered / wedge elements (sloped sumps, sloped bases):
  cubic_feet = 0.5 × (depth_start_ft + depth_end_ft) × width_ft × length_ft × qty
  cubic_yards = cubic_feet / 27
  Use depth_ft to store the AVERAGE depth = 0.5*(depth_start + depth_end).

Sum cubic_yards across ALL elements (tapered elements must be POSITIVE additions, not deductions).
Call the return_takeoff tool with all elements and the summary totals."""
    history.append({"role": "user", "content": calc})

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=system_prompt,
        messages=history,
        tools=[_TAKEOFF_TOOL],
        tool_choice={"type": "tool", "name": "return_takeoff"},
    )

    # tool_choice forces a ToolUseBlock as the first content item
    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if tool_block:
        parsed = tool_block.input
    else:
        # Should never happen with forced tool_choice, but keep a text fallback
        raw = "".join(getattr(b, "text", "") for b in resp.content)
        parsed = _parse_json_block(raw)

    if parsed:
        # Deterministic Python recompute of every element's volume from the
        # extracted dimensions. Replaces Claude's arithmetic (which drifts ~1%
        # across multiple trials) with a single canonical computation.
        audit = _verify_element_volumes(parsed.get("elements") or [])

        total_cf = 0.0
        total_cy = 0.0
        for el in parsed.get("elements", []) or []:
            try:
                total_cf += float(el.get("cubic_feet") or 0)
                total_cy += float(el.get("cubic_yards") or 0)
            except (TypeError, ValueError):
                pass
        parsed.setdefault("summary", {})
        parsed["summary"]["total_cubic_feet"] = round(total_cf, 2)
        parsed["summary"]["total_cubic_yards"] = round(total_cy, 2)
        parsed["summary"]["verification"] = {
            "applied": True,
            "waste_factor": WASTE_FACTOR,
            "drift_threshold_pct": VOLUME_DRIFT_THRESHOLD * 100,
            "overrides": audit,
            "override_count": len(audit),
        }
        # When the tool returned no elements, attach the first-turn prose so
        # the frontend parser can attempt to extract rows from it.
        if not parsed.get("elements") and prose_response:
            parsed["summary"]["analysis"] = prose_response
        return parsed

    # Shouldn't be reached with forced tool_choice, but keep as safety net
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
            pdf_text, sparse_pages = extract_pdf_text(save_path)
            page_images = render_pages_as_images(save_path, sparse_pages) if sparse_pages else []
            result = extract_quantities_with_claude(pdf_text, page_images, filename)
        finally:
            try:
                os.remove(save_path)
            except OSError:
                pass
            # Release PDF parse buffers and conversation history back to the
            # OS so the next request starts fresh instead of inheriting peak.
            gc.collect()

        return jsonify({
            'success': True,
            'filename': filename,
            'pages_text_chars': len(pdf_text),
            'vision_pages_rendered': len(page_images),
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
