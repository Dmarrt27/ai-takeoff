"""
In-context learning module for the AI Takeoff app.

After a user submits corrected quantities, this module calls Claude once to
distill the corrections into a generalizable rule ("lesson"). Lessons are
stored in lessons.jsonl and injected into the Turn 1 prompt on every future
upload so the model gets better with each corrected drawing.
"""

import os
import json
import threading
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
# On Render, use the persistent disk at /data. Locally, use the project dir.
_DEFAULT_LESSONS = "/data/lessons.jsonl" if os.path.isdir("/data") else os.path.join(_HERE, "lessons.jsonl")
LESSONS_FILE = os.environ.get("LESSONS_FILE", _DEFAULT_LESSONS)
MAX_LESSONS_IN_PROMPT = 12

# One-time seed: if the persistent file doesn't exist yet but the repo ships
# a starter lessons.jsonl, copy it over so the deployed app starts with the
# accumulated lessons instead of empty.
_SEED = os.path.join(_HERE, "lessons.jsonl")
if LESSONS_FILE != _SEED and not os.path.exists(LESSONS_FILE) and os.path.exists(_SEED):
    try:
        os.makedirs(os.path.dirname(LESSONS_FILE), exist_ok=True)
        with open(_SEED, "r", encoding="utf-8") as src, open(LESSONS_FILE, "w", encoding="utf-8") as dst:
            dst.write(src.read())
        print(f"[learning] Seeded {LESSONS_FILE} from repo starter")
    except OSError as e:
        print(f"[learning] Seed failed: {e}")


def load_lessons() -> list:
    if not os.path.exists(LESSONS_FILE):
        return []
    lessons = []
    with open(LESSONS_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lessons.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return lessons


def format_lessons_for_prompt(max_lessons: int = MAX_LESSONS_IN_PROMPT) -> str:
    """Return an injection-ready block for Turn 1, or '' if no lessons yet."""
    lessons = load_lessons()
    if not lessons:
        return ""
    recent = lessons[-max_lessons:]
    lines = ["LESSONS LEARNED FROM PAST HUMAN EXPERT CORRECTIONS:"]
    for i, entry in enumerate(recent, 1):
        lines.append(f"{i}. {entry['lesson']}")
    lines.append("")  # trailing blank line for readability
    return "\n".join(lines)


def extract_corrections(feedback_payload: dict) -> list:
    """Return (ai_extracted, user_corrected, correction_note) triples for every element the user changed."""
    corrections = []
    for el in feedback_payload.get("elements", []):
        if not (el.get("edited") or el.get("flag") == "incorrect"):
            continue
        ai = el.get("ai_extracted", {})
        user = el.get("user_corrected", {})
        note = el.get("correction_note") or ""
        if ai != user:
            corrections.append((ai, user, note))
    return corrections


def _build_lesson_messages(corrections: list, source_file: str, human_notes: str = "", images: list = None) -> list:
    """Build the messages list for lesson extraction.

    When images are provided (drawing snippets from the user's feedback), they
    are included as base64 image blocks so Claude can see the visual context
    that caused the extraction error and write a more precise lesson.
    """
    prompt_text = _build_lesson_prompt(corrections, source_file, human_notes)

    if not images:
        return [{"role": "user", "content": prompt_text}]

    content = [{"type": "text", "text": prompt_text + "\n\nThe user attached the following drawing snippet(s) to illustrate the correction:"}]
    for img in images[:4]:
        data_url = img.get("data_url", "")
        if not data_url.startswith("data:"):
            continue
        try:
            header, b64_data = data_url.split(",", 1)
            media_type = header.split(":")[1].split(";")[0]
            if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                media_type = "image/jpeg"
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64_data},
            })
            caption = img.get("caption", "").strip()
            if caption:
                content.append({"type": "text", "text": f"[Caption: {caption}]"})
        except Exception as e:
            print(f"[learning] Skipping malformed snippet: {e}")

    return [{"role": "user", "content": content}]


def _build_lesson_prompt(corrections: list, source_file: str, human_notes: str = "") -> str:
    correction_block = ""
    for ai, user, note in corrections[:5]:
        correction_block += (
            f"\nAI extracted: {json.dumps(ai)}"
            f"\nHuman corrected to: {json.dumps(user)}"
        )
        if note:
            correction_block += f"\nHuman explanation: {note}"
        correction_block += "\n---"

    human_notes_section = ""
    if human_notes and human_notes.strip():
        human_notes_section = (
            f"\nAdditional notes from the human expert:\n{human_notes.strip()}\n"
        )

    return (
        f'You are a construction quantity takeoff expert reviewing AI extraction errors.\n\n'
        f'A PDF named "{source_file}" was analyzed. The AI extracted quantities, then a human '
        f"expert corrected them. Here are the corrections:\n"
        f"{correction_block}\n"
        f"{human_notes_section}\n"
        "Write ONE concise rule (max 2 sentences) that helps an AI avoid this type of error "
        "in future construction quantity takeoffs. Focus on the generalizable pattern, not the "
        "specific case. Be specific to construction drawing interpretation and unit conversion."
    )


def generate_and_save_lesson(feedback_payload: dict, client, model: str, images: list = None) -> bool:
    """Generate a lesson from corrections and append it to lessons.jsonl.

    When images is provided (drawing snippets), Claude receives a multimodal
    prompt so it can ground the lesson in the actual visual evidence.
    """
    corrections = extract_corrections(feedback_payload)
    if not corrections:
        return False

    source_file = feedback_payload.get("source_file", "unknown")
    human_notes = (feedback_payload.get("session") or {}).get("human_notes") or ""
    messages = _build_lesson_messages(corrections, source_file, human_notes, images)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            messages=messages,
        )
        # Join text blocks rather than indexing content[0] — newer models can
        # lead the response with non-text blocks (e.g. thinking).
        lesson_text = "".join(
            b.text for b in resp.content if b.type == "text"
        ).strip()
    except Exception as e:
        print(f"[learning] Lesson generation failed: {e}")
        return False
    if not lesson_text:
        print("[learning] Lesson generation returned no text")
        return False

    ai_example, user_example, _ = corrections[0]
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_file": source_file,
        "lesson": lesson_text,
        "ai_example": ai_example,
        "user_example": user_example,
        "has_snippets": bool(images),
    }
    with open(LESSONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[learning] Lesson saved: {lesson_text[:80]}...")
    return True


def trigger_lesson_extraction(feedback_payload: dict, client, model: str, images: list = None) -> None:
    """Fire-and-forget: extract a lesson from feedback in a background thread."""
    t = threading.Thread(
        target=generate_and_save_lesson,
        args=(feedback_payload, client, model, images),
        daemon=True,
    )
    t.start()
