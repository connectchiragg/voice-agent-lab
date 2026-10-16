"""/valr/settings/phone-help: docs/phone-recordings.md as a VALR page (owner only), linked from
Settings › Phone call recordings as "iPhone shortcut steps". One source: the doc in the repo
(the app image copies it, see the Dockerfile).

The Markdown is the small subset that doc uses: #, ## and ### headings, paragraphs, numbered
and bulleted lists (nested by indentation), fenced code, `code`, **bold** and *italic*.
Everything is escaped first; nothing in the file can add HTML.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "phone-recordings.md"
LIST = re.compile(r"^(?P<indent> *)(?:(?P<num>\d+)\.|-) +(?P<text>.*)$")


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<em>\1</em>", out)


def render(markdown: str) -> str:
    out: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, "ol" | "ul") of the open lists
    para: list[str] = []
    lines = markdown.splitlines()
    i = 0

    def close_para() -> None:
        if para:
            out.append(f"<p>{inline(' '.join(para))}</p>")
            para.clear()

    def close_lists(indent: int = -1) -> None:
        while stack and stack[-1][0] > indent:
            out.append(f"</li></{stack.pop()[1]}>")

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            close_para()
            close_lists()
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(f"<pre><code>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
        elif line.startswith("#"):
            close_para()
            close_lists()
            level = min(len(line) - len(line.lstrip("#")), 3)
            out.append(f"<h{level + 1}>{inline(line.lstrip('#').strip())}</h{level + 1}>")
        elif match := LIST.match(line):
            close_para()
            indent, tag = len(match["indent"]), "ol" if match["num"] else "ul"
            close_lists(indent)
            if stack and stack[-1][0] == indent and stack[-1][1] == tag:
                out.append("</li><li>")
            else:
                if stack and stack[-1][0] == indent:
                    out.append(f"</li></{stack.pop()[1]}>")
                out.append(f'<{tag} class="help-list"><li>')
                stack.append((indent, tag))
            out.append(inline(match["text"]))
        elif not line.strip():
            close_para()
        elif stack and line.startswith(" "):
            out.append(" " + inline(line.strip()))  # A list item's continuation line.
        else:
            close_lists()
            para.append(line.strip())
        i += 1
    close_para()
    close_lists()
    return "\n".join(out)


def page_body() -> str:
    try:
        text = DOC.read_text(encoding="utf-8")
    except OSError:
        return '<p class="muted">The steps aren\'t in this build. See docs/phone-recordings.md in the repository.</p>'
    body = text.split("\n", 1)[1] if text.startswith("# ") else text  # The page has its own title.
    style = ("<style>.help-doc{max-width:46rem;overflow-wrap:anywhere}.help-doc pre{overflow-x:auto;white-space:pre}"
             ".help-doc h2,.help-doc h3,.help-doc h4{margin-top:1.6em}.help-doc li{margin:.25em 0}</style>")
    return f'{style}<article class="help-doc">{render(body)}</article>'
