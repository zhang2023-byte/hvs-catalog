"""Position-preserving TeX views, not a TeX interpreter.

Comment/verbatim rendering helpers adapted from stella_hvs (MIT); see
LICENSE-NOTICE.md. Every masking operation preserves characters and newlines.
The source sent to LaTeXML remains the actual author source.
"""

from __future__ import annotations

import re

VERBATIM_ENVIRONMENTS = ("verbatim", "verbatim*", "lstlisting", "minted", "Verbatim")


def _blank(text: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in text)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    pattern = re.compile(
        r"\\verb\*?([^A-Za-z\s])|\\begin\{("
        + "|".join(re.escape(x) for x in VERBATIM_ENVIRONMENTS)
        + r")\}"
    )
    cursor = 0
    while (match := pattern.search(text, cursor)) is not None:
        if match[1]:
            end = text.find(match[1], match.end())
            newline = text.find("\n", match.end())
            if end == -1 or (newline != -1 and newline < end):
                stop = len(text) if newline == -1 else newline
            else:
                stop = end + 1
        else:
            marker = r"\end{" + match[2] + "}"
            end = text.find(marker, match.end())
            stop = len(text) if end == -1 else end + len(marker)
        spans.append((match.start(), stop))
        cursor = stop
    return spans


def strip_tex_comments(text: str) -> str:
    # A comment can contain a fake verbatim opener; scan from left to right.
    out, i, env = [], 0, None
    while i < len(text):
        if env:
            marker = r"\end{" + env + "}"
            end = text.find(marker, i)
            stop = len(text) if end == -1 else end + len(marker)
            out.append(text[i:stop])
            i = stop
            env = None
            continue
        begin = next(
            (
                name
                for name in VERBATIM_ENVIRONMENTS
                if text.startswith(r"\begin{" + name + "}", i)
            ),
            None,
        )
        if begin:
            marker = r"\begin{" + begin + "}"
            out.append(marker)
            i += len(marker)
            env = begin
            continue
        verb = re.match(r"\\verb\*?([^A-Za-z\s])", text[i:])
        if verb:
            end = text.find(verb[1], i + verb.end())
            newline = text.find("\n", i + verb.end())
            stop = (
                end + 1
                if end != -1 and (newline == -1 or end < newline)
                else (newline if newline != -1 else len(text))
            )
            out.append(text[i:stop])
            i = stop
            continue
        if text[i] == "%":
            end = text.find("\n", i)
            if end == -1:
                end = len(text)
            out.append(_blank(text[i:end]))
            i = end
            continue
        if text[i] == "\\" and i + 1 < len(text):
            out.append(text[i : i + 2])
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def mask_verbatim(text: str) -> str:
    rendered, cursor = [], 0
    for start, end in _protected_spans(text):
        rendered.extend((text[cursor:start], _blank(text[start:end])))
        cursor = end
    rendered.append(text[cursor:])
    return "".join(rendered)


def mask_macro_bodies(text: str) -> str:
    from .tables import _balanced_group

    pattern = re.compile(
        r"\\(?:newcommand|renewcommand|providecommand)\s*\*?\s*"
        r"(?:\{\\[A-Za-z@]+\}|\\[A-Za-z@]+)\s*(?:\[[^\]]*\]\s*){0,2}\{"
        r"|\\(?:def|gdef|edef|xdef)\s*\\[A-Za-z@]+(?:\s*#[1-9])*\s*\{"
    )
    rendered, cursor = [], 0
    while (match := pattern.search(text, cursor)) is not None:
        end = _balanced_group(text, match.end() - 1)
        stop = len(text) if end is None else end + 1
        rendered.extend(
            (text[cursor : match.start()], _blank(text[match.start() : stop]))
        )
        cursor = stop
    rendered.append(text[cursor:])
    return "".join(rendered)


def scan_view(text: str) -> str:
    """Mask definitions/examples and provably inactive constant branches.

    Unknown conditionals remain candidates, never evidence of execution. Actual
    execution and table identity come from the native LaTeXML observer.
    """
    text = mask_macro_bodies(mask_verbatim(strip_tex_comments(text)))
    chars, stack, cursor = list(text), [], 0
    pattern = re.compile(r"\\(if[a-zA-Z@]*|else|fi)(?![A-Za-z@])")
    for match in pattern.finditer(text):
        if False in stack:
            chars[cursor : match.start()] = _blank(text[cursor : match.start()])
        token = match[1]
        if token.startswith("if"):
            stack.append(
                False if token == "iffalse" else True if token == "iftrue" else None
            )
        elif token == "else" and stack:
            if stack[-1] is not None:
                stack[-1] = not stack[-1]
        elif token == "fi" and stack:
            stack.pop()
        chars[match.start() : match.end()] = _blank(match[0])
        cursor = match.end()
    if False in stack:
        chars[cursor:] = _blank(text[cursor:])
    return "".join(chars)


def unknown_conditional_spans(text: str) -> list[tuple[int, int]]:
    """Locate uncertain scopes without evaluating them as TeX conditions."""
    text = mask_macro_bodies(mask_verbatim(strip_tex_comments(text)))
    stack, spans = [], []
    for match in re.finditer(r"\\(if[a-zA-Z@]*|fi)(?![A-Za-z@])", text):
        token = match[1]
        if token.startswith("if"):
            if re.search(
                r"\\newif\s*$", text[max(0, match.start() - 24) : match.start()]
            ):
                continue
            stack.append((match.start(), token not in {"iftrue", "iffalse"}))
        elif stack:
            start, uncertain = stack.pop()
            if uncertain:
                spans.append((start, match.end()))
    spans.extend((start, len(text)) for start, uncertain in stack if uncertain)
    return spans


def render_file_block(path: str, cleaned_text: str) -> str:
    lines = cleaned_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "\n".join(f"{number}|{line}" for number, line in enumerate(lines, 1))
    return f"===== BEGIN FILE: {path} =====\n{body}\n===== END FILE: {path} ====="


def render_manuscript_view(blocks: list[tuple[str, str]]) -> str:
    return "\n\n".join(render_file_block(path, text) for path, text in blocks) + "\n"
