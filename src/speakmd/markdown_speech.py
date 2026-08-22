"""Turn Markdown tokens into explicit, speech-friendly narration blocks.

The goal is fidelity, not a visually stripped version of the document.  Every
content-bearing Markdown construct gets a spoken representation; structural
tokens only affect the wording and later chunk boundaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
import re
from typing import Iterable
from urllib.parse import urlparse

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin
from mdit_py_plugins.tasklists import tasklists_plugin


@dataclass(frozen=True)
class NarrationBlock:
    """A logical speech unit before it is packed into TTS-sized chunks."""

    kind: str
    text: str
    level: int = 0
    table_number: int | None = None
    row_number: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class NarratedDocument:
    blocks: list[NarrationBlock]
    warnings: list[str]


class _TextOnlyHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _spoken_url(url: str) -> str:
    """Keep a link target audible without asking the TTS engine to guess URLs."""
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        value = parsed.netloc + parsed.path
        if parsed.query:
            value += " question mark " + parsed.query
    elif parsed.scheme == "mailto":
        value = parsed.path.replace("@", " at ")
    else:
        value = url
    value = value.replace("www.", "www dot ")
    value = value.replace(".", " dot ").replace("/", " slash ")
    value = value.replace("_", " underscore ").replace("-", " dash ")
    value = value.replace("?", " question mark ").replace("&", " and ")
    value = value.replace("=", " equals ").replace("#", " hash ")
    return _space(value)


_CODE_PUNCTUATION = {
    "_": " underscore ",
    "-": " dash ",
    ".": " dot ",
    "/": " slash ",
    "\\": " backslash ",
    ":": " colon ",
    ";": " semicolon ",
    "=": " equals ",
    "+": " plus ",
    "*": " asterisk ",
    "%": " percent ",
    "#": " hash ",
    "@": " at ",
    "&": " ampersand ",
    "|": " pipe ",
    "<": " less than ",
    ">": " greater than ",
    "[": " open bracket ",
    "]": " close bracket ",
    "{": " open brace ",
    "}": " close brace ",
    "(": " open parenthesis ",
    ")": " close parenthesis ",
    "`": " backtick ",
}


def narrate_code(code: str, language: str = "") -> str:
    """Read code explicitly rather than silently dropping it.

    This is intentionally literal enough to be useful for short examples.  Large
    code samples remain complete; semantic chunking breaks their spoken lines up.
    """
    label = f"Code block in {language}." if language else "Code block."
    lines = code.rstrip("\n").splitlines() or [""]
    spoken_lines: list[str] = []
    for number, line in enumerate(lines, start=1):
        value = _spoken_code_line(line)
        spoken_lines.append(f"Line {number}: {value}.")
    return " ".join([label, *spoken_lines])


def _spoken_code_line(line: str) -> str:
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", line)
    for symbol, replacement in _CODE_PUNCTUATION.items():
        value = value.replace(symbol, replacement)
    return _space(value) or "blank"


def _html_text(raw: str) -> str:
    parser = _TextOnlyHtml()
    try:
        parser.feed(raw)
        parser.close()
        text = _space(" ".join(parser.parts))
    except Exception:  # malformed HTML should still be represented
        text = _space(re.sub(r"<[^>]*>", " ", raw))
    return text or "HTML markup with no readable text"


def render_inline(tokens: Iterable[Token] | None) -> str:
    """Render inline tokens while retaining links, code, images, and deleted text."""
    if not tokens:
        return ""
    parts: list[str] = []
    link_targets: list[str] = []
    for token in tokens:
        token_type = token.type
        if token_type == "text":
            parts.append(token.content)
        elif token_type in {"softbreak", "hardbreak"}:
            parts.append(" ")
        elif token_type == "code_inline":
            parts.append(f" inline code: {_spoken_code_line(token.content)}. ")
        elif token_type == "link_open":
            link_targets.append(token.attrGet("href") or "")
            parts.append(" link ")
        elif token_type == "link_close":
            target = link_targets.pop() if link_targets else ""
            if target:
                parts.append(f" link target {_spoken_url(target)} ")
        elif token_type == "image":
            alt = render_inline(token.children) or _space(token.content) or "unlabelled image"
            source = token.attrGet("src") or ""
            suffix = f", source {_spoken_url(source)}" if source else ""
            parts.append(f" Image: {alt}{suffix}. ")
        elif token_type == "html_inline":
            if "task-list-item-checkbox" in token.content:
                parts.append(" completed task " if "checked" in token.content else " incomplete task ")
            else:
                parts.append(f" HTML: {_html_text(token.content)}. ")
        elif token_type == "s_open":
            parts.append(" text marked deleted: ")
        elif token_type == "s_close":
            parts.append(" end deleted text. ")
        elif token_type == "footnote_ref":
            label = token.meta.get("label", token.meta.get("id", "")) if token.meta else ""
            parts.append(f" footnote {label}. ")
        elif token_type.endswith("_open") or token_type.endswith("_close"):
            # Emphasis and strong markup change presentation, not spoken content.
            continue
        elif token.content:
            parts.append(f" {token.content} ")
    return _space("".join(parts))


def _make_parser() -> MarkdownIt:
    return (
        MarkdownIt("commonmark", {"linkify": True, "typographer": True})
        .enable(["table", "strikethrough"])
        .use(front_matter_plugin)
        .use(footnote_plugin)
        .use(tasklists_plugin)
    )


def _consume_table(tokens: list[Token], start: int, table_number: int) -> tuple[list[NarrationBlock], int]:
    """Convert GFM table tokens to a header-aware intro plus self-contained rows."""
    rows: list[tuple[bool, list[str]]] = []
    in_header = False
    row: list[str] | None = None
    in_cell = False
    i = start + 1
    while i < len(tokens) and tokens[i].type != "table_close":
        token = tokens[i]
        if token.type == "thead_open":
            in_header = True
        elif token.type == "tbody_open":
            in_header = False
        elif token.type == "tr_open":
            row = []
        elif token.type in {"th_open", "td_open"}:
            in_cell = True
        elif token.type == "inline" and in_cell and row is not None:
            row.append(render_inline(token.children) or "empty")
            in_cell = False
        elif token.type == "tr_close" and row is not None:
            rows.append((in_header, row))
            row = None
        i += 1

    header = next((cells for is_header, cells in rows if is_header), [])
    body = [cells for is_header, cells in rows if not is_header]
    max_columns = max([len(header), *(len(cells) for cells in body)] or [0])
    if not header:
        header = [f"Column {number}" for number in range(1, max_columns + 1)]
    if len(header) < max_columns:
        header.extend(f"Column {number}" for number in range(len(header) + 1, max_columns + 1))
    names = ", ".join(header) if header else "no labelled columns"
    blocks = [
        NarrationBlock(
            "table_intro",
            f"Table {table_number} has {max_columns} columns: {names}. It has {len(body)} data rows.",
            table_number=table_number,
        )
    ]
    for row_number, cells in enumerate(body, start=1):
        pairs = []
        for column, name in enumerate(header):
            value = cells[column] if column < len(cells) else "empty"
            pairs.append(f"{name}: {value}")
        if len(cells) > len(header):
            for column, value in enumerate(cells[len(header) :], start=len(header) + 1):
                pairs.append(f"Column {column}: {value}")
        blocks.append(
            NarrationBlock(
                "table_row",
                f"Table {table_number}, row {row_number}: " + ". ".join(pairs) + ".",
                table_number=table_number,
                row_number=row_number,
            )
        )
    return blocks, i + 1


def narrate_markdown(markdown: str) -> NarratedDocument:
    """Create logical narration blocks from a Markdown document.

    Markdown-it supplies a CommonMark token stream with GFM tables enabled.  That
    avoids regex parsing errors and lets table cells and inline content keep their
    semantics.
    """
    tokens = _make_parser().parse(markdown)
    blocks: list[NarrationBlock] = []
    warnings: list[str] = []
    list_stack: list[dict[str, int | bool]] = []
    quote_depth = 0
    footnote_depth = 0
    table_number = 0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        token_type = token.type
        if token_type == "table_open":
            table_number += 1
            table_blocks, i = _consume_table(tokens, i, table_number)
            blocks.extend(table_blocks)
            continue
        if token_type in {"bullet_list_open", "ordered_list_open"}:
            list_stack.append({"ordered": token_type == "ordered_list_open", "count": 0})
        elif token_type in {"bullet_list_close", "ordered_list_close"}:
            if list_stack:
                list_stack.pop()
        elif token_type == "list_item_open" and list_stack:
            list_stack[-1]["count"] = int(list_stack[-1]["count"]) + 1
        elif token_type == "blockquote_open":
            quote_depth += 1
        elif token_type == "blockquote_close":
            quote_depth = max(0, quote_depth - 1)
        elif token_type == "footnote_open":
            footnote_depth += 1
        elif token_type == "footnote_close":
            footnote_depth = max(0, footnote_depth - 1)
        elif token_type == "heading_open" and i + 1 < len(tokens) and tokens[i + 1].type == "inline":
            level = int(token.tag[1:]) if token.tag.startswith("h") else 1
            text = render_inline(tokens[i + 1].children)
            if text:
                blocks.append(NarrationBlock("heading", f"Heading level {level}: {text}.", level=level))
            i += 2
            continue
        elif token_type == "paragraph_open" and i + 1 < len(tokens) and tokens[i + 1].type == "inline":
            text = render_inline(tokens[i + 1].children)
            if text:
                if footnote_depth:
                    prefix, kind = "Footnote: ", "footnote"
                elif list_stack:
                    current = list_stack[-1]
                    number = int(current["count"])
                    label = f"Item {number}" if bool(current["ordered"]) else f"List item {number}"
                    prefix, kind = f"{label}: ", "list_item"
                elif quote_depth:
                    prefix, kind = "Quotation: ", "blockquote"
                else:
                    prefix, kind = "", "paragraph"
                blocks.append(NarrationBlock(kind, prefix + text))
            i += 2
            continue
        elif token_type in {"fence", "code_block"}:
            blocks.append(NarrationBlock("code", narrate_code(token.content, token.info.strip())))
        elif token_type == "hr":
            blocks.append(NarrationBlock("thematic_break", "Section break."))
        elif token_type == "html_block":
            blocks.append(NarrationBlock("html", f"HTML content: {_html_text(token.content)}."))
        elif token_type == "front_matter":
            blocks.append(NarrationBlock("metadata", "Document metadata is present."))
        elif token_type == "inline":
            # It is normally consumed with its paragraph/heading.  A standalone
            # inline token is still content and must not disappear.
            text = render_inline(token.children)
            if text:
                blocks.append(NarrationBlock("inline", text))
        elif token.content and not token_type.endswith(("_open", "_close")):
            warnings.append(f"Represented uncommon Markdown token: {token_type}")
            blocks.append(NarrationBlock("other", f"{token_type.replace('_', ' ')}: {_space(token.content)}"))
        i += 1
    return NarratedDocument(blocks=blocks, warnings=list(dict.fromkeys(warnings)))
