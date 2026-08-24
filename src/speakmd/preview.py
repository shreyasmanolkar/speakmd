"""Short passages for auditioning Kokoro voices before converting a document."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path


PREVIEW_MAX_CHARS = 800
DEFAULT_SAMPLE_ID = "narration"

VOICE_SAMPLES: dict[str, dict[str, str]] = {
    "narration": {
        "label": "Narration",
        "text": (
            "The morning light reached the far wall of the studio, slow and even. "
            "Across the valley, a pale river turned through the trees and disappeared behind the hill. "
            "She waited until the room was quiet, then began. This voice should feel calm, close, and easy to follow, "
            "as if someone were reading beside you rather than announcing from a stage. "
            "Listen for the shape of each sentence: the rise, the pause, and the gentle landing at the end."
        ),
    },
    "conversation": {
        "label": "Conversation",
        "text": (
            "Hi. Can you hear me clearly? Good. "
            "I didn't think we would finish today, but here we are. Are you ready for the next part, or do you need a moment? "
            'He asked, "What happens if we wait until Thursday?" She laughed and said, "Then we start anyway." '
            "Please don't rush. Leave a little space between the question and the answer. Thanks. I'll talk to you soon."
        ),
    },
    "numbers": {
        "label": "Numbers and names",
        "text": (
            "On March 3rd, 2026, forty-seven people arrived at 3:45 in the afternoon. "
            "Tickets were $19.99, or twelve pounds fifty if you paid at the door. "
            "Please turn to page 108, section 2.4. The meeting is in room 16-B, not 60-B. "
            "Call 555-0142 after 9:00. Ask for Sarah, Adam, Emma, or George. "
            "The order number is A-73019, and the total is one thousand two hundred and six."
        ),
    },
    "instruction": {
        "label": "Instructions",
        "text": (
            "First, open the document. Second, choose a voice that matches the tone. "
            "Third, listen to a short sample before you convert the whole file. "
            "Keep the pace steady, and make names, dates, and next steps easy to write down. "
            "If a table appears, speak every column with each row. For example: Name: Sarah. Age: 28. Location: Paris. "
            "When you reach the end, pause, then say thank you for listening."
        ),
    },
}


@dataclass
class VoicePreviewRequest:
    text: str
    voice: str
    speed: float
    device: str
    path: Path
    future: Future


def sample_catalog() -> dict[str, object]:
    return {
        "max_chars": PREVIEW_MAX_CHARS,
        "default_id": DEFAULT_SAMPLE_ID,
        "samples": [
            {"id": sample_id, "label": sample["label"], "text": sample["text"]}
            for sample_id, sample in VOICE_SAMPLES.items()
        ],
    }


def resolve_preview_text(sample_id: str | None, text: str | None) -> str:
    if text is not None:
        if not isinstance(text, str):
            raise ValueError("preview text must be a string")
        sample = text.strip()
        if not sample:
            raise ValueError("preview text is empty")
    else:
        key = str(sample_id or DEFAULT_SAMPLE_ID)
        if key not in VOICE_SAMPLES:
            raise ValueError("unknown sample id")
        sample = VOICE_SAMPLES[key]["text"]
    if len(sample) > PREVIEW_MAX_CHARS:
        raise ValueError(f"preview text is limited to {PREVIEW_MAX_CHARS} characters")
    return sample
