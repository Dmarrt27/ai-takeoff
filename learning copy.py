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
LESSONS_FILE = os.path.join(_HERE, "lessons.jsonl")
MAX_LESSONS_IN_PROMPT = 12


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


def generate_and_save_lesson(feedback_payload: dict, client, model: str) -> bool:
    """Generate a lesson from corrections and append it to lessons.jsonl."""
    corrections = extract_corrections(feedback_payload)
    if not corrections:
        return False

    source_file = feedback_payload.get("source_file", "unknown")
    human_notes = (feedback_payload.get("session") or {}).get("human_notes") or ""
    prompt = _build_lesson_prompt(corrections, source_file, human_notes)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        lesson_text = resp.content[0].text.strip()
    except Exception as e:
        print(f"[learning] Lesson generation failed: {e}")
        return False

    ai_example, user_example = corrections[0]
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source_file": source_file,
        "lesson": lesson_text,
        "ai_example": ai_example,
        "user_example": user_example,
    }
    with open(LESSONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    print(f"[learning] Lesson saved: {lesson_text[:80]}...")
    return True


def trigger_lesson_extraction(feedback_payload: dict, client, model: str) -> None:
    """Fire-and-forget: extract a lesson from feedback in a background thread."""
    t = threading.Thread(
        target=generate_and_save_lesson,
        args=(feedback_payload, client, model),
        daemon=True,
    )
    t.start()
