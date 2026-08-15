import re
import tkinter as tk

HEADING_RE = re.compile(r"^(#{1,6}) (.*)$")
BLOCKQUOTE_RE = re.compile(r"^> ?(.*)$")
CHECKBOX_RE = re.compile(r"^- \[([ xX])\] (.*)$")
NUMBERED_RE = re.compile(r"^(\d+\. )(.*)$")
BULLET_RE = re.compile(r"^([-+*] )(.*)$")
HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
FENCE_RE = re.compile(r"^(```|~~~)")
IMAGE_RE = re.compile(r"^(?:\[\[image:(.+?)\]\]|!\[\]\(kqnote-image:(.+?)\))$")
TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")

INLINE_RE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\[(?P<linktext>[^\]]+)\]\((?P<linkurl>[^)]+)\)"
    r"|(?P<bi_mark>\*\*\*|___)(?P<bi_text>.+?)(?P=bi_mark)"
    r"|(?P<b_mark>\*\*|__)(?P<b_text>.+?)(?P=b_mark)"
    r"|(?<![\w*_])(?P<i_mark>\*|_)(?P<i_text>[^\s*_](?:[^*_]*[^\s*_])?)(?P=i_mark)(?!\w)"
)

INLINE_TAGS = {"bold", "italic", "bolditalic", "code"}
HEADING_TAGS = [f"h{i}" for i in range(1, 7)]
BLOCK_TAGS = HEADING_TAGS + ["blockquote", "codeblock", "numbered", "bullet1",
                             "bullet2", "checkbox_off", "checkbox_on", "hr",
                             "table", "tableheader", "tableborder"]


def image_marker(file_id):
    return f"![](kqnote-image:{file_id})"


def _is_table_row(line):
    return "|" in line.strip()


def _is_table_separator_row(line):
    cells = _split_row(line)
    return bool(cells) and all(TABLE_SEPARATOR_CELL_RE.match(c) for c in cells)


def _split_row(line, ncols=None):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and len(s) > 0:
        s = s[:-1]
    cells = [c.strip() for c in s.split("|")]
    if ncols is not None:
        if len(cells) < ncols:
            cells += [""] * (ncols - len(cells))
        else:
            cells = cells[:ncols]
    return cells


def _parse_inline(text):
    """Split a line of markdown into (text, tags, link_url) segments."""
    segments = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos:m.start()], frozenset(), None))
        if m.group("code") is not None:
            segments.append((m.group("code"), frozenset({"code"}), None))
        elif m.group("linktext") is not None:
            segments.append((m.group("linktext"), frozenset({"link"}), m.group("linkurl")))
        elif m.group("bi_text") is not None:
            segments.append((m.group("bi_text"), frozenset({"bold", "italic"}), None))
        elif m.group("b_text") is not None:
            segments.append((m.group("b_text"), frozenset({"bold"}), None))
        elif m.group("i_text") is not None:
            segments.append((m.group("i_text"), frozenset({"italic"}), None))
        pos = m.end()
    if pos < len(text) or not segments:
        segments.append((text[pos:], frozenset(), None))
    return segments


def _inline_tagname(tags):
    if "code" in tags:
        return "code"
    if "bold" in tags and "italic" in tags:
        return "bolditalic"
    if "bold" in tags:
        return "bold"
    if "italic" in tags:
        return "italic"
    return None


def _insert_inline(text_widget, body, index="end", extra_tag=None):
    """Insert one line's worth of markdown-inline text, tagging spans as it goes."""
    for seg_text, tags, url in _parse_inline(body):
        insert_tags = []
        if extra_tag:
            insert_tags.append(extra_tag)
        tagname = _inline_tagname(tags)
        if tagname:
            insert_tags.append(tagname)
        if url is not None:
            link_tag = f"link_{len(text_widget._kq_links)}"
            text_widget._kq_links[link_tag] = url
            insert_tags.append(link_tag)
        text_widget.insert(index, seg_text, tuple(insert_tags))


def _write_markdown(text_widget, content, index, on_image, on_hr=None, on_codeblock=None):
    lines = (content or "").split("\n")
    n = len(lines)
    i = 0
    first = True

    def newline():
        nonlocal first
        if not first:
            text_widget.insert(index, "\n")
        first = False

    while i < n:
        line = lines[i]

        if FENCE_RE.match(line.strip()):
            newline()
            i += 1
            code_lines = []
            while i < n and not FENCE_RE.match(lines[i].strip()):
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # skip closing fence
            for j, code_line in enumerate(code_lines):
                if j:
                    text_widget.insert(index, "\n")
                text_widget.insert(index, code_line, ("codeblock",))
            if on_codeblock:
                on_codeblock("\n".join(code_lines))
            continue

        if (_is_table_row(line) and i + 1 < n and _is_table_separator_row(lines[i + 1])):
            newline()
            header_cells = _split_row(line)
            ncols = len(header_cells)
            i += 2  # skip header + separator row
            body_rows = []
            while i < n and lines[i].strip() and _is_table_row(lines[i]):
                body_rows.append(_split_row(lines[i], ncols))
                i += 1

            widths = [max(1, len(h)) for h in header_cells]
            for row in body_rows:
                for c, cell in enumerate(row):
                    widths[c] = max(widths[c], len(cell))

            def hline():
                return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

            def content_line(cells):
                return "| " + " | ".join(cell.ljust(widths[c]) for c, cell in enumerate(cells)) + " |"

            rows = [(header_cells, "tableheader")] + [(r, None) for r in body_rows]

            text_widget.insert(index, hline(), ("table", "tableborder"))
            for cells, tag in rows:
                text_widget.insert(index, "\n")
                row_tags = ("table", tag) if tag else ("table",)
                text_widget.insert(index, content_line(cells), row_tags)
                text_widget.insert(index, "\n")
                text_widget.insert(index, hline(), ("table", "tableborder"))
            continue

        m = IMAGE_RE.match(line)
        if m and on_image:
            newline()
            on_image(m.group(1) or m.group(2))
            i += 1
            continue

        if HR_RE.match(line) and on_hr:
            newline()
            on_hr()
            i += 1
            continue

        newline()

        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            _insert_inline(text_widget, m.group(2), index, extra_tag=f"h{level}")
            i += 1
            continue

        m = BLOCKQUOTE_RE.match(line)
        if m:
            _insert_inline(text_widget, m.group(1), index, extra_tag="blockquote")
            i += 1
            continue

        m = CHECKBOX_RE.match(line)
        if m:
            checked = m.group(1).lower() == "x"
            prefix = f"- [{'x' if checked else ' '}] "
            tag = "checkbox_on" if checked else "checkbox_off"
            text_widget.insert(index, prefix, (tag,))
            _insert_inline(text_widget, m.group(2), index, extra_tag=tag)
            i += 1
            continue

        m = NUMBERED_RE.match(line)
        if m:
            text_widget.insert(index, m.group(1), ("numbered",))
            _insert_inline(text_widget, m.group(2), index, extra_tag="numbered")
            i += 1
            continue

        m = BULLET_RE.match(line)
        if m:
            marker = m.group(1)
            tag = "bullet2" if marker[0] == "+" else "bullet1"
            text_widget.insert(index, marker, (tag,))
            _insert_inline(text_widget, m.group(2), index, extra_tag=tag)
            i += 1
            continue

        if HR_RE.match(line):
            text_widget.insert(index, line, ("hr",))
            i += 1
            continue

        _insert_inline(text_widget, line, index)
        i += 1


def render_into_text(text_widget, content, on_image=None, on_hr=None, on_codeblock=None):
    text_widget.delete("1.0", "end")
    text_widget._kq_links = {}
    _write_markdown(text_widget, content, "end", on_image, on_hr, on_codeblock)


def insert_markdown_at_cursor(text_widget, content, on_image=None, on_hr=None, on_codeblock=None):
    """Parse a markdown snippet (e.g. pasted from the clipboard) and insert it, formatted, at the cursor."""
    if not hasattr(text_widget, "_kq_links"):
        text_widget._kq_links = {}
    _write_markdown(text_widget, content, "insert", on_image, on_hr, on_codeblock)


def _dump_segments(text_widget, lineno, col_start, col_end):
    """Group a line range into (tag_frozenset, text) runs for inline serialization."""
    segments = []
    cur_tags = None
    cur_chars = []
    for col in range(col_start, col_end):
        idx = f"{lineno}.{col}"
        tags = text_widget.tag_names(idx)
        link_url = None
        for t in tags:
            if t.startswith("link_"):
                link_url = text_widget._kq_links.get(t)
                break
        inline_tags = frozenset(t for t in tags if t in INLINE_TAGS)
        key = (inline_tags, link_url)
        ch = text_widget.get(idx, f"{lineno}.{col + 1}")
        if key != cur_tags:
            if cur_tags is not None:
                segments.append((cur_tags, "".join(cur_chars)))
            cur_tags = key
            cur_chars = [ch]
        else:
            cur_chars.append(ch)
    if cur_tags is not None:
        segments.append((cur_tags, "".join(cur_chars)))
    return segments


def _wrap_segment(tags_and_url, text):
    inline_tags, link_url = tags_and_url
    if not text:
        return text
    if "code" in inline_tags:
        out = f"`{text}`"
    elif "bolditalic" in inline_tags or ("bold" in inline_tags and "italic" in inline_tags):
        out = f"***{text}***"
    elif "bold" in inline_tags:
        out = f"**{text}**"
    elif "italic" in inline_tags:
        out = f"*{text}*"
    else:
        out = text
    if link_url is not None:
        out = f"[{out}]({link_url})"
    return out


def _serialize_inline(text_widget, lineno, col_start, col_end):
    segments = _dump_segments(text_widget, lineno, col_start, col_end)
    return "".join(_wrap_segment(tags, text) for tags, text in segments)


def serialize_from_text(text_widget):
    last_line = int(text_widget.index("end-1c").split(".")[0])
    links = getattr(text_widget, "_kq_links", {})
    if links:
        text_widget._kq_links = links

    image_lines = {}
    for name in text_widget.image_names():
        if not name.startswith("img_"):
            continue
        lineno = int(text_widget.index(name).split(".")[0])
        image_lines[lineno] = name[len("img_"):]

    hr_lines = set()
    skip_lines = set()  # decorative embedded widgets (code-block copy button, etc.)
    for name in text_widget.window_names():
        try:
            widget = text_widget.nametowidget(name)
        except (KeyError, tk.TclError):
            continue
        lineno = int(text_widget.index(name).split(".")[0])
        if getattr(widget, "_kq_is_hr", False):
            hr_lines.add(lineno)
        elif getattr(widget, "_kq_is_codecopy", False):
            skip_lines.add(lineno)

    out_lines = []
    in_code_block = False
    for lineno in range(1, last_line + 1):
        if lineno in image_lines:
            if in_code_block:
                out_lines.append("```")
                in_code_block = False
            out_lines.append(image_marker(image_lines[lineno]))
            continue

        if lineno in hr_lines:
            if in_code_block:
                out_lines.append("```")
                in_code_block = False
            out_lines.append("---")
            continue

        if lineno in skip_lines:
            if in_code_block:
                out_lines.append("```")
                in_code_block = False
            continue

        line_start = f"{lineno}.0"
        line_end = f"{lineno}.end"
        tags = text_widget.tag_names(line_start)
        line_len = int(text_widget.index(line_end).split(".")[1])

        if "codeblock" in tags:
            if not in_code_block:
                out_lines.append("```")
                in_code_block = True
            out_lines.append(text_widget.get(line_start, line_end))
            continue
        elif in_code_block:
            out_lines.append("```")
            in_code_block = False

        heading_level = next((i for i in range(1, 7) if f"h{i}" in tags), None)
        if heading_level:
            body = _serialize_inline(text_widget, lineno, 0, line_len)
            out_lines.append("#" * heading_level + " " + body)
        elif "blockquote" in tags:
            body = _serialize_inline(text_widget, lineno, 0, line_len)
            out_lines.append("> " + body)
        elif "table" in tags:
            if "tableborder" in tags:
                pass  # decorative border line, nothing to serialize
            elif "tableheader" in tags:
                cells = _split_row(text_widget.get(line_start, line_end))
                out_lines.append("| " + " | ".join(cells) + " |")
                out_lines.append("| " + " | ".join("---" for _ in cells) + " |")
            else:
                cells = _split_row(text_widget.get(line_start, line_end))
                out_lines.append("| " + " | ".join(cells) + " |")
        else:
            out_lines.append(_serialize_inline(text_widget, lineno, 0, line_len))

    if in_code_block:
        out_lines.append("```")

    return "\n".join(out_lines)
