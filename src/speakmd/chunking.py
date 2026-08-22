"""Semantic chunking for long-form TTS."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from .markdown_speech import NarrationBlock


CHUNKING_VERSION = "2026-08-semantic-v1"
DEFAULT_MAX_CHARS = 360


@dataclass(frozen=True)
class SpeechChunk:
    index: int
    text: str
    pause_after: float
    block_kinds: list[str]
    block_start: int
    block_end: int

    def as_dict(self) -> dict:
        return asdict(self)


_SENTENCES = re.compile(r"[^.!?…。]+[.!?…。]+[\"')\]”]*|\S[^.!?…。]*$")


def _sentences(text: str) -> list[str]:
    pieces = [piece.strip() for piece in _SENTENCES.findall(text) if piece.strip()]
    return pieces or ([text.strip()] if text.strip() else [])


def _words_under_limit(text: str, limit: int) -> list[str]:
    """Split only when necessary; hard-split a pathological unbroken token safely."""
    result: list[str] = []
    current = ""
    for word in text.split():
        words = [word[position : position + limit] for position in range(0, len(word), limit)] or [word]
        for part in words:
            candidate = f"{current} {part}".strip()
            if current and len(candidate) > limit:
                result.append(current)
                current = part
            else:
                current = candidate
    if current:
        result.append(current)
    return result


def split_block(text: str, max_chars: int) -> list[str]:
    """Prefer sentence endings, then whitespace, before an unavoidable hard split."""
    output: list[str] = []
    current = ""
    for sentence in _sentences(re.sub(r"\s+", " ", text).strip()):
        pieces = [sentence] if len(sentence) <= max_chars else _words_under_limit(sentence, max_chars)
        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if current and len(candidate) > max_chars:
                output.append(current)
                current = piece
            else:
                current = candidate
    if current:
        output.append(current)
    return output


def _pause_for(kind: str) -> float:
    return {
        "heading": 0.55,
        "thematic_break": 0.6,
        "paragraph": 0.35,
        "blockquote": 0.35,
        "list_item": 0.25,
        "table_intro": 0.3,
        "table_row": 0.2,
        "code": 0.3,
        "footnote": 0.3,
        "html": 0.35,
    }.get(kind, 0.3)


def plan_chunks(blocks: list[NarrationBlock], max_chars: int = DEFAULT_MAX_CHARS) -> list[SpeechChunk]:
    """Pack narration blocks into independently renderable chunks.

    A heading starts a new chunk but is allowed to share it with following prose.
    Table rows repeat their headers in the narrator, so a row chunk remains intelligible
    if it is retried or played by itself. Code starts a fresh chunk to keep literal text
    from blending into prose.  Only an oversize sentence is split at a word boundary.
    """
    if max_chars < 80:
        raise ValueError("max_chars must be at least 80")
    planned: list[SpeechChunk] = []
    text_parts: list[str] = []
    kinds: list[str] = []
    start = 0
    end = 0

    def flush() -> None:
        nonlocal text_parts, kinds, start, end
        text = " ".join(text_parts).strip()
        if text:
            planned.append(
                SpeechChunk(
                    index=len(planned) + 1,
                    text=text,
                    pause_after=_pause_for(kinds[-1]),
                    block_kinds=list(dict.fromkeys(kinds)),
                    block_start=start,
                    block_end=end,
                )
            )
        text_parts, kinds = [], []

    for block_index, block in enumerate(blocks, start=1):
        continuation = ""
        if block.kind == "code":
            continuation = "Code continues. "
        elif block.kind == "table_row":
            continuation = f"Table {block.table_number}, row {block.row_number}, continued. "
        # Reserve room before splitting, so a continuation never pushes a chunk
        # beyond the model-safe ceiling. It matters for a very large table cell
        # or code line, not normal prose/table rows.
        part_limit = max_chars - len(continuation) if len(block.text) > max_chars else max_chars
        parts = split_block(block.text, part_limit)
        if not parts:
            continue
        if block.kind in {"heading", "code", "thematic_break"} and text_parts:
            flush()
        for part_index, part in enumerate(parts):
            if continuation and part_index:
                part = continuation + part
            candidate = f"{' '.join(text_parts)} {part}".strip()
            if text_parts and len(candidate) > max_chars:
                flush()
            if not text_parts:
                start = block_index
            text_parts.append(part)
            kinds.append(block.kind)
            end = block_index
            # Structural blocks never merge with unrelated following text.  A heading is
            # deliberately excluded so it provides context for its following paragraph.
            if block.kind in {"code", "thematic_break"}:
                flush()
        if block.kind == "heading" and len(parts) > 1:
            flush()
    flush()
    return planned


def chunks_as_dicts(chunks: list[SpeechChunk]) -> list[dict]:
    return [chunk.as_dict() for chunk in chunks]
