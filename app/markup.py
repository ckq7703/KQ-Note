import re

NUMBERED_RE = re.compile(r"^\d+\. ")
IMAGE_RE = re.compile(r"^\[\[image:(.+?)\]\]$")


def render_into_text(text_widget, content, on_image=None):
    text_widget.delete("1.0", "end")
    lines = (content or "").split("\n")
    for i, line in enumerate(lines):
        m = IMAGE_RE.match(line)
        if m and on_image:
            on_image(m.group(1))
        elif line.startswith("# "):
            text_widget.insert("end", line[2:], ("h1",))
        elif line.startswith("**") and line.endswith("**") and len(line) >= 4:
            text_widget.insert("end", line[2:-2], ("bold",))
        elif NUMBERED_RE.match(line):
            text_widget.insert("end", line, ("numbered",))
        elif line.startswith("+ "):
            text_widget.insert("end", line, ("bullet2",))
        elif line.startswith("- "):
            text_widget.insert("end", line, ("bullet1",))
        else:
            text_widget.insert("end", line)
        if i != len(lines) - 1:
            text_widget.insert("end", "\n")


def serialize_from_text(text_widget):
    last_line = int(text_widget.index("end-1c").split(".")[0])

    image_lines = {}
    for name in text_widget.image_names():
        if not name.startswith("img_"):
            continue
        lineno = int(text_widget.index(name).split(".")[0])
        image_lines[lineno] = name[len("img_"):]

    out_lines = []
    for lineno in range(1, last_line + 1):
        if lineno in image_lines:
            out_lines.append(f"[[image:{image_lines[lineno]}]]")
            continue
        line_start = f"{lineno}.0"
        line_end = f"{lineno}.end"
        text = text_widget.get(line_start, line_end)
        tags = text_widget.tag_names(line_start)
        if "h1" in tags:
            out_lines.append("# " + text)
        elif "bold" in tags:
            out_lines.append("**" + text + "**")
        else:
            out_lines.append(text)
    return "\n".join(out_lines)
