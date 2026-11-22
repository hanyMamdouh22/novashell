import re
import os
import sys

IS_WINDOWS = os.name == 'nt'
if IS_WINDOWS and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
import unicodedata
from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax
from rich.text import Text

console = Console()

# ECMA-48 SCP (native RTL line mode in GNOME Terminal / VTE)
RTL_ON = "\x1b[2 k"
RTL_OFF = "\x1b[0 k"

# Unicode BiDi Isolates + LTR Mark
LRI = "\u2066"
PDI = "\u2069"
LRM = "\u200e"

_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_TASHKEEL_RE = re.compile(r"[\u064B-\u0652\u0670]")
_TOKEN_RE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*)")
_LTR_SPAN_RE = re.compile(
    r"(https?://\S+|(?:/[A-Za-z0-9._-]+)+/?|[A-Za-z][A-Za-z0-9_./:-]*(?:\s+[A-Za-z0-9_./:-]+)*)"
)


def _strip_tashkeel(text: str) -> str:
    """Removes combining Arabic diacritics that cause cell clipping in monospace terminals."""
    return _TASHKEEL_RE.sub("", unicodedata.normalize("NFC", text))


def _isolate_ltr(segment: str) -> str:
    return _LTR_SPAN_RE.sub(lambda m: f"{LRI}{LRM}{m.group(0)}{LRM}{PDI}", segment)


def _parse_segments(line: str, is_rtl: bool, base_style: str) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    pos = 0
    for match in _TOKEN_RE.finditer(line):
        if match.start() > pos:
            chunk = line[pos : match.start()].replace("**", "")
            if chunk:
                segments.append((chunk, base_style))

        token = match.group(0)
        if token.startswith("`"):
            segments.append((token[1:-1], "cyan"))
        else:
            # Avoid heavy bold on Arabic monospace to prevent overlapping glyphs
            highlight = "cyan" if "cyan" in base_style else ("bright_yellow" if is_rtl else "bold white")
            segments.append((token[2:-2], highlight))
        pos = match.end()

    if pos < len(line):
        tail = line[pos:].replace("**", "")
        if tail:
            segments.append((tail, base_style))
    return segments


def _wrap_styled_words(segments: list[tuple[str, str]], width: int) -> list[list[tuple[str, str]]]:
    """Wraps text cleanly at word boundaries so no Arabic or English word splits mid-word."""
    words: list[list[tuple[str, str]]] = []
    current_word: list[tuple[str, str]] = []

    for text, style in segments:
        for part in re.split(r"(\s+)", text):
            if not part:
                continue
            if part.isspace():
                if current_word:
                    words.append(current_word)
                    current_word = []
            else:
                current_word.append((part, style))
    if current_word:
        words.append(current_word)

    lines: list[list[tuple[str, str]]] = []
    cur_line: list[tuple[str, str]] = []
    cur_len = 0

    for word in words:
        w_len = sum(len(t) for t, _ in word)
        space_len = 1 if cur_line else 0
        if cur_line and cur_len + space_len + w_len > width:
            lines.append(cur_line)
            cur_line = list(word)
            cur_len = w_len
        else:
            if cur_line:
                space_style = cur_line[-1][1] if cur_line[-1][1] == word[0][1] else ""
                cur_line.append((" ", space_style))
                cur_len += 1
            cur_line.extend(word)
            cur_len += w_len

    if cur_line:
        lines.append(cur_line)

    merged_lines: list[list[tuple[str, str]]] = []
    for line_parts in lines:
        merged: list[tuple[str, str]] = []
        for text, style in line_parts:
            if merged and merged[-1][1] == style:
                merged[-1] = (merged[-1][0] + text, style)
            else:
                merged.append((text, style))
        merged_lines.append(merged)
    return merged_lines


def _print_line(line: str) -> None:
    stripped = line.strip()
    if not stripped:
        console.print()
        return

    if re.match(r"^[-_*]{3,}$", stripped):
        console.print(Rule(style="dim cyan"))
        return

    is_rtl = bool(_ARABIC_RE.search(stripped))
    use_vte_rtl = is_rtl and sys.stdout.isatty() and not IS_WINDOWS

    base_style = ""
    content = stripped
    if stripped.startswith("#"):
        content = re.sub(r"^#+\s*", "", stripped)
        base_style = "cyan"
    elif re.match(r"^[-*]\s+", stripped):
        content = "• " + re.sub(r"^[-*]\s+", "", stripped)

    if is_rtl:
        content = _strip_tashkeel(content)

    segments = _parse_segments(content, is_rtl=is_rtl, base_style=base_style)

    if use_vte_rtl:
        wrap_width = max(40, console.width - 4)
        for line_chunks in _wrap_styled_words(segments, wrap_width):
            rich_text = Text()
            for chunk, style in line_chunks:
                if style == "cyan" and not _ARABIC_RE.search(chunk):
                    rich_text.append(f"{LRI}{LRM}{chunk}{LRM}{PDI}", style=style)
                else:
                    rich_text.append(_isolate_ltr(chunk), style=style)
            sys.stdout.write(RTL_ON)
            sys.stdout.flush()
            try:
                console.print(rich_text, soft_wrap=True)
            finally:
                sys.stdout.write(RTL_OFF)
                sys.stdout.flush()
    else:
        rich_text = Text()
        for chunk, style in segments:
            rich_text.append(chunk, style=style)
        console.print(rich_text)


def render_ai_response(text: str, title: str, style: str = "cyan") -> None:
    """Renders AI output with clean word-wrapped RTL for Arabic and LTR for code/English."""
    console.print()
    console.print(Rule(f"[bold {style}] {title} [/bold {style}]", style=style))

    in_code = False
    code_lang = "bash"
    code_buf: list[str] = []

    try:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_code:
                    in_code = True
                    code_lang = stripped[3:].strip() or "bash"
                    code_buf = []
                else:
                    in_code = False
                    console.print(
                        Syntax("\n".join(code_buf), code_lang, theme="monokai", word_wrap=True)
                    )
                continue

            if in_code:
                code_buf.append(line)
            else:
                _print_line(line)

        if in_code and code_buf:
            console.print(Syntax("\n".join(code_buf), code_lang, theme="monokai", word_wrap=True))
    finally:
        if sys.stdout.isatty() and not IS_WINDOWS:
            sys.stdout.write(RTL_OFF)
            sys.stdout.flush()

    console.print(Rule(style=f"dim {style}"))
    console.print()
