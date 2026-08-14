import os
import re
import tkinter as tk
import uuid
import webbrowser

from PIL import Image, ImageDraw, ImageGrab, ImageTk

from app import markup, store
from app.config import load_config, save_config
from app.winfx import round_window

URL_RE = re.compile(r"(https?://[^\s]+|www\.[^\s]+)")
IMAGE_MAX_SIZE = (300, 400)
IMAGE_CHECK_INTERVAL_MS = 300
LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "logo-kqnote.png")

BG = "#141416"
BG_HEADER = "#1a1a1d"
BG_MENU = "#202024"
BORDER = "#2c2c30"
FG_TEXT = "#e7e7ea"
FG_MUTED = "#75757d"
FG_ACCENT = "#5b9df0"
FG_TITLE_TAG = "#6fd0a0"
MATCH_BG = "#4a4520"
MATCH_CURRENT_BG = "#8a7a20"
SELECT_BG = "#4a4a4f"

MIN_W, MIN_H = 300, 260
WINDOW_RADIUS = 16
MENU_RADIUS = 8


class ContextMenu(tk.Toplevel):
    def __init__(self, parent, items):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=BG_MENU)

        frame = tk.Frame(self, bg=BG_MENU)
        frame.pack(fill="both", expand=True)

        for item in items:
            if item is None:
                sep = tk.Frame(frame, bg=BORDER, height=1)
                sep.pack(fill="x", padx=6, pady=4)
                continue
            label, command = item
            row = tk.Label(frame, text=label, bg=BG_MENU, fg=FG_TEXT, anchor="w",
                            font=("Segoe UI", 9), padx=16, pady=7, cursor="hand2")
            row.pack(fill="x")
            row.bind("<Enter>", lambda e, r=row: r.config(bg=FG_ACCENT, fg="#ffffff"))
            row.bind("<Leave>", lambda e, r=row: r.config(bg=BG_MENU, fg=FG_TEXT))
            row.bind("<Button-1>", lambda e, c=command: self._invoke(c))

        self.bind("<Escape>", self._dismiss)
        self.bind("<FocusOut>", self._dismiss)
        # grab_set() routes every app click here while open; catch clicks that
        # land on empty space (not on an item row) and dismiss instead of no-op.
        self.bind("<Button-1>", self._dismiss)
        frame.bind("<Button-1>", self._dismiss)

    def _invoke(self, command):
        self.destroy()
        if command:
            command()

    def _dismiss(self, _event=None):
        if self.winfo_exists():
            self.destroy()

    def popup(self, x, y):
        self.update_idletasks()
        self.geometry(f"+{x}+{y}")
        round_window(self, MENU_RADIUS)
        self.deiconify()
        self.focus_force()
        self.grab_set()


class NotesWidget(tk.Toplevel):
    def __init__(self, root):
        super().__init__(root)
        self.root = root
        self._drag = {"x": 0, "y": 0}
        self._resize = {"x": 0, "y": 0, "w": 0, "h": 0}
        self._save_after_id = None
        self._matches = []
        self._match_idx = -1
        self._photo_refs = {}
        self._pending_images = set()

        cfg = load_config()
        self._always_on_top = cfg.get("always_on_top", True)

        self.title("KQ Note")
        if os.path.exists(LOGO_PATH):
            try:
                self._icon_photo = tk.PhotoImage(file=LOGO_PATH)
                self.iconphoto(True, self._icon_photo)
            except tk.TclError:
                pass

        self.overrideredirect(True)
        self.attributes("-topmost", self._always_on_top)
        self.attributes("-alpha", 0.92)
        self.configure(bg=BG)

        self.geometry(cfg.get("widget_geometry", "380x520+880+70"))

        self._build()
        markup.render_into_text(self.text, store.load_content(), on_image=self._on_image_marker)
        self._highlight_urls()
        if self._pending_images:
            self.after(IMAGE_CHECK_INTERVAL_MS, self._check_lazy_images)

        self.bind("<Configure>", self._on_window_configure)
        self.after(10, lambda: round_window(self, WINDOW_RADIUS))

    # ---------- UI construction ----------
    def _build(self):
        outer = tk.Frame(self, bg=BG, highlightthickness=0)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG_HEADER, cursor="fleur")
        header.pack(fill="x")
        header.bind("<ButtonPress-1>", self._drag_start)
        header.bind("<B1-Motion>", self._drag_move)
        header.bind("<ButtonRelease-1>", self._drag_end)

        title_lbl = tk.Label(header, text="KQ Note", bg=BG_HEADER, fg=FG_TEXT,
                              font=("Segoe UI", 10, "bold"), padx=12, pady=9)
        title_lbl.pack(side="left")
        title_lbl.bind("<ButtonPress-1>", self._drag_start)
        title_lbl.bind("<B1-Motion>", self._drag_move)
        title_lbl.bind("<ButtonRelease-1>", self._drag_end)

        self.pin_btn = tk.Label(header, text="\U0001F4CC", bg=BG_HEADER,
                                 fg=(FG_ACCENT if self._always_on_top else FG_MUTED),
                                 font=("Segoe UI", 9), padx=10, cursor="hand2")
        self.pin_btn.pack(side="right")
        self.pin_btn.bind("<Button-1>", lambda e: self.toggle_always_on_top())

        search_row = tk.Frame(outer, bg=BG, padx=10, pady=8)
        search_row.pack(fill="x")

        search_wrap = tk.Frame(search_row, bg=BG)
        search_wrap.pack(fill="x", expand=True)

        search_box = tk.Frame(search_wrap, bg=BG)
        search_box.pack(fill="x")

        tk.Label(search_box, text="\U0001F50D", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 6))
        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_box, textvariable=self.search_var, bg=BG, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", font=("Segoe UI", 10),
            highlightthickness=0, selectbackground=SELECT_BG, selectforeground=FG_TEXT,
        )
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        self.search_entry.bind("<Return>", self._next_match)
        self.search_entry.bind("<Escape>", lambda e: self._clear_search())

        search_underline = tk.Frame(search_wrap, bg=BORDER, height=1)
        search_underline.pack(fill="x", pady=(2, 0))

        toolbar = tk.Frame(outer, bg=BG)
        toolbar.pack(fill="x", padx=10, pady=(6, 4))

        h1_btn = tk.Label(toolbar, text="H1", bg=BG, fg=FG_MUTED,
                           font=("Segoe UI", 9, "bold"), cursor="hand2", padx=6)
        h1_btn.pack(side="left")
        h1_btn.bind("<Button-1>", lambda e: self._toggle_tag("h1"))

        bold_btn = tk.Label(toolbar, text="B", bg=BG, fg=FG_MUTED,
                             font=("Segoe UI", 9, "bold"), cursor="hand2", padx=6)
        bold_btn.pack(side="left")
        bold_btn.bind("<Button-1>", lambda e: self._toggle_tag("bold"))

        numbered_btn = tk.Label(toolbar, text="1.", bg=BG, fg=FG_MUTED,
                                 font=("Segoe UI", 9, "bold"), cursor="hand2", padx=6)
        numbered_btn.pack(side="left")
        numbered_btn.bind("<Button-1>", lambda e: self._toggle_list("numbered"))

        dash_btn = tk.Label(toolbar, text="—", bg=BG, fg=FG_MUTED,
                             font=("Segoe UI", 9, "bold"), cursor="hand2", padx=6)
        dash_btn.pack(side="left")
        dash_btn.bind("<Button-1>", lambda e: self._toggle_list("dash"))

        plus_btn = tk.Label(toolbar, text="+", bg=BG, fg=FG_MUTED,
                             font=("Segoe UI", 9, "bold"), cursor="hand2", padx=6)
        plus_btn.pack(side="left")
        plus_btn.bind("<Button-1>", lambda e: self._toggle_list("plus"))

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, padx=(10, 10), pady=(0, 8))

        self.text = tk.Text(
            body, bg=BG, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", wrap="word", font=("Segoe UI", 10), padx=4, pady=4,
            undo=True, borderwidth=0, highlightthickness=0,
            selectbackground=SELECT_BG, selectforeground=FG_TEXT,
        )
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("h1", font=("Segoe UI", 13, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("bold", font=("Segoe UI", 10, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("numbered", lmargin1=20, lmargin2=36)
        self.text.tag_configure("bullet1", lmargin1=40, lmargin2=56)
        self.text.tag_configure("bullet2", lmargin1=60, lmargin2=76)
        self.text.tag_configure("match", background=MATCH_BG)
        self.text.tag_configure("match_current", background=MATCH_CURRENT_BG)
        self.text.tag_configure("url", foreground=FG_ACCENT, underline=True)
        self.text.tag_raise("url")
        self.text.tag_bind("url", "<Button-1>", self._on_url_click)
        self.text.tag_bind("url", "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind("url", "<Leave>", lambda e: self.text.config(cursor="xterm"))

        self.text.bind("<KeyRelease>", self._on_text_changed)
        self.text.bind("<Return>", self._on_return_key)
        self.text.bind("<Control-v>", self._on_ctrl_v)
        self.text.bind("<MouseWheel>", lambda e: self.text.yview_scroll(-1 * int(e.delta / 120), "units"))
        self.text.bind("<Button-3>", self._show_text_menu)
        self.search_entry.bind("<Button-3>", self._show_entry_menu)

        grip = tk.Label(outer, text="⋰", bg=BG, fg=FG_MUTED, cursor="size_nw_se",
                         font=("Segoe UI", 10))
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)
        grip.bind("<ButtonRelease-1>", self._resize_end)

    # ---------- drag / resize (overrideredirect window) ----------
    def _drag_start(self, event):
        self._drag["x"] = event.x
        self._drag["y"] = event.y

    def _drag_move(self, event):
        x = self.winfo_x() + (event.x - self._drag["x"])
        y = self.winfo_y() + (event.y - self._drag["y"])
        self.geometry(f"+{x}+{y}")

    def _drag_end(self, _event):
        self._save_geometry()

    def _resize_start(self, event):
        self._resize = {"x": event.x_root, "y": event.y_root,
                         "w": self.winfo_width(), "h": self.winfo_height()}

    def _resize_move(self, event):
        dw = event.x_root - self._resize["x"]
        dh = event.y_root - self._resize["y"]
        w = max(MIN_W, self._resize["w"] + dw)
        h = max(MIN_H, self._resize["h"] + dh)
        self.geometry(f"{w}x{h}")

    def _resize_end(self, _event):
        self._save_geometry()

    def _save_geometry(self):
        cfg = load_config()
        cfg["widget_geometry"] = self.geometry()
        save_config(cfg)

    def _on_window_configure(self, _event=None):
        round_window(self, WINDOW_RADIUS)

    # ---------- editing / autosave ----------
    def _on_text_changed(self, _event=None):
        self._highlight_urls()
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
        self._save_after_id = self.after(500, self.flush_save)

    def _highlight_urls(self):
        self.text.tag_remove("url", "1.0", "end")
        last_line = int(self.text.index("end-1c").split(".")[0])
        for lineno in range(1, last_line + 1):
            line_text = self.text.get(f"{lineno}.0", f"{lineno}.end")
            if not line_text:
                continue
            for m in URL_RE.finditer(line_text):
                url = m.group(0).rstrip(").,;!?")
                start = f"{lineno}.{m.start()}"
                end = f"{lineno}.{m.start() + len(url)}"
                self.text.tag_add("url", start, end)

    def _on_url_click(self, event):
        index = self.text.index(f"@{event.x},{event.y}")
        ranges = self.text.tag_prevrange("url", f"{index}+1c")
        if ranges:
            url = self.text.get(ranges[0], ranges[1])
            if url.startswith("www."):
                url = "http://" + url
            webbrowser.open(url)
        return "break"

    def flush_save(self):
        if self._save_after_id is not None:
            self.after_cancel(self._save_after_id)
            self._save_after_id = None
        content = markup.serialize_from_text(self.text)
        store.save_content(content)

    def _toggle_tag(self, tagname):
        other = "bold" if tagname == "h1" else "h1"
        try:
            start = self.text.index("sel.first linestart")
            end = self.text.index("sel.last lineend")
        except tk.TclError:
            start = self.text.index("insert linestart")
            end = self.text.index("insert lineend")

        current_tags = self.text.tag_names(start)
        if tagname in current_tags:
            self.text.tag_remove(tagname, start, end)
        else:
            self.text.tag_remove(other, start, end)
            self.text.tag_remove("numbered", start, end)
            self.text.tag_remove("bullet1", start, end)
            self.text.tag_remove("bullet2", start, end)
            self.text.tag_add(tagname, start, end)
        self._on_text_changed()

    _LIST_TAGS = {"numbered": "numbered", "dash": "bullet1", "plus": "bullet2"}
    _LIST_PREFIX_RE = re.compile(r"^(\d+\. |- |\+ )")

    def _toggle_list(self, kind):
        tagname = self._LIST_TAGS[kind]

        try:
            start_line = int(self.text.index("sel.first").split(".")[0])
            end_line = int(self.text.index("sel.last").split(".")[0])
        except tk.TclError:
            start_line = end_line = int(self.text.index("insert").split(".")[0])

        turning_on = tagname not in self.text.tag_names(f"{start_line}.0")

        next_num = 1
        if kind == "numbered" and turning_on and start_line > 1:
            prev_start = f"{start_line - 1}.0"
            if "numbered" in self.text.tag_names(prev_start):
                prev_text = self.text.get(prev_start, f"{start_line - 1}.end")
                m = re.match(r"^(\d+)\. ", prev_text)
                if m:
                    next_num = int(m.group(1)) + 1

        for lineno in range(start_line, end_line + 1):
            line_start = f"{lineno}.0"
            line_end = f"{lineno}.end"
            line_text = self.text.get(line_start, line_end)

            self.text.tag_remove("h1", line_start, line_end)
            self.text.tag_remove("bold", line_start, line_end)
            self.text.tag_remove("numbered", line_start, line_end)
            self.text.tag_remove("bullet1", line_start, line_end)
            self.text.tag_remove("bullet2", line_start, line_end)

            m = self._LIST_PREFIX_RE.match(line_text)
            if m:
                self.text.delete(line_start, f"{line_start}+{m.end()}c")

            if turning_on:
                if kind == "numbered":
                    prefix = f"{next_num}. "
                    next_num += 1
                elif kind == "dash":
                    prefix = "- "
                else:
                    prefix = "+ "
                self.text.insert(line_start, prefix)
                self.text.tag_add(tagname, line_start, f"{lineno}.end")

        self._on_text_changed()

    def _on_return_key(self, _event):
        lineno = int(self.text.index("insert").split(".")[0])
        line_start = f"{lineno}.0"
        line_end = f"{lineno}.end"
        line_text = self.text.get(line_start, line_end)
        tags = self.text.tag_names(line_start)

        if "numbered" in tags:
            tagname, prefix = "numbered", None
            m = re.match(r"^(\d+)\. (.*)$", line_text)
            if m and not m.group(2).strip():
                self.text.delete(line_start, line_end)
                self._on_text_changed()
                return "break"
            next_num = int(m.group(1)) + 1 if m else 1
            prefix = f"{next_num}. "
        elif "bullet1" in tags or "bullet2" in tags:
            tagname = "bullet1" if "bullet1" in tags else "bullet2"
            prefix = "- " if tagname == "bullet1" else "+ "
            body = line_text[len(prefix):] if line_text.startswith(prefix) else line_text
            if not body.strip():
                self.text.delete(line_start, line_end)
                self._on_text_changed()
                return "break"
        else:
            return None

        self.text.insert("insert", f"\n{prefix}")
        new_line = int(self.text.index("insert").split(".")[0])

        for ln in (lineno, new_line):
            ln_start, ln_end = f"{ln}.0", f"{ln}.end"
            for t in ("h1", "bold", "numbered", "bullet1", "bullet2"):
                self.text.tag_remove(t, ln_start, ln_end)
            self.text.tag_add(tagname, ln_start, ln_end)

        self._on_text_changed()
        return "break"

    # ---------- right-click context menus ----------
    def _show_text_menu(self, event):
        menu = ContextMenu(self, [
            ("Cắt", self._cut_text),
            ("Sao chép", lambda: self.text.event_generate("<<Copy>>")),
            ("Dán", self._paste_text),
            None,
            ("Chọn tất cả", self._select_all_text),
        ])
        menu.popup(event.x_root, event.y_root)

    def _select_all_text(self):
        self.text.tag_add("sel", "1.0", "end")

    def _cut_text(self):
        self.text.event_generate("<<Cut>>")
        self._on_text_changed()

    def _paste_text(self):
        self._paste_clipboard()

    def _on_ctrl_v(self, _event):
        self._paste_clipboard()
        return "break"

    def _paste_clipboard(self):
        try:
            clip = ImageGrab.grabclipboard()
        except Exception:
            clip = None

        if isinstance(clip, Image.Image):
            self._insert_image_now(clip)
        else:
            self.text.event_generate("<<Paste>>")
            self._on_text_changed()

    # ---------- images (paste, embed, lazy-load) ----------
    def _insert_image_now(self, pil_image):
        file_id = uuid.uuid4().hex[:12]
        path = os.path.join(store.get_images_dir(), f"{file_id}.png")
        try:
            pil_image.convert("RGB").save(path, "PNG")
        except Exception:
            return

        line_start = self.text.index("insert linestart")
        line_text = self.text.get(line_start, "insert lineend")
        if line_text.strip():
            self.text.insert("insert", "\n")

        display_img = pil_image.copy()
        display_img.thumbnail(IMAGE_MAX_SIZE)
        photo = ImageTk.PhotoImage(display_img)

        name = f"img_{file_id}"
        self.text.image_create("insert", image=photo, name=name)
        self._photo_refs[name] = photo
        self._bind_image_click(name, file_id)
        self.text.insert("insert", "\n")
        self._on_text_changed()

    def _on_image_marker(self, file_id):
        name = f"img_{file_id}"
        placeholder = self._make_placeholder_image()
        self.text.image_create("end", image=placeholder, name=name)
        self._photo_refs[name] = placeholder
        self._pending_images.add(name)
        self._bind_image_click(name, file_id)

    def _bind_image_click(self, name, file_id):
        idx = self.text.index(name)
        tag = f"imgtag_{file_id}"
        self.text.tag_add(tag, idx, f"{idx}+1c")
        self.text.tag_bind(tag, "<Button-1>", lambda e, fid=file_id: self._show_image_preview(fid))
        self.text.tag_bind(tag, "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind(tag, "<Leave>", lambda e: self.text.config(cursor="xterm"))

    def _show_image_preview(self, file_id):
        path = os.path.join(store.get_images_dir(), f"{file_id}.png")
        try:
            img = Image.open(path)
        except Exception:
            return

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        max_w, max_h = int(screen_w * 0.85), int(screen_h * 0.85)
        min_edge = 480

        w, h = img.size
        scale = 1.0
        if max(w, h) < min_edge:
            scale = min_edge / max(w, h)
        if w * scale > max_w or h * scale > max_h:
            scale = min(max_w / w, max_h / h)
        if scale != 1.0:
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)

        preview = tk.Toplevel(self)
        preview.overrideredirect(True)
        preview.attributes("-topmost", True)
        preview.configure(bg=BORDER)

        photo = ImageTk.PhotoImage(img)
        preview._photo_ref = photo

        lbl = tk.Label(preview, image=photo, bg=BG, bd=0)
        lbl.pack(padx=1, pady=1)

        pw, ph = img.size
        x = (screen_w - pw) // 2
        y = (screen_h - ph) // 2
        preview.geometry(f"{pw + 2}x{ph + 2}+{x}+{y}")

        preview.bind("<Escape>", lambda e: preview.destroy())
        preview.bind("<Button-1>", lambda e: preview.destroy())
        lbl.bind("<Button-1>", lambda e: preview.destroy())
        preview.focus_force()
        self.after(10, lambda: round_window(preview, 10))

    def _make_placeholder_image(self):
        img = Image.new("RGB", (220, 60), (35, 35, 38))
        draw = ImageDraw.Draw(img)
        draw.text((10, 22), "Dang tai anh...", fill=(140, 140, 145))
        return ImageTk.PhotoImage(img)

    def _check_lazy_images(self):
        if not self._pending_images:
            return
        for name in list(self._pending_images):
            try:
                idx = self.text.index(name)
            except tk.TclError:
                self._pending_images.discard(name)
                continue
            if self.text.bbox(idx) is not None:
                self._load_real_image(name)
                self._pending_images.discard(name)
        if self._pending_images:
            self.after(IMAGE_CHECK_INTERVAL_MS, self._check_lazy_images)

    def _load_real_image(self, name):
        file_id = name[len("img_"):]
        path = os.path.join(store.get_images_dir(), f"{file_id}.png")
        try:
            pil_img = Image.open(path)
            pil_img.thumbnail(IMAGE_MAX_SIZE)
            photo = ImageTk.PhotoImage(pil_img)
        except Exception:
            return
        self.text.image_configure(name, image=photo)
        self._photo_refs[name] = photo

    def _show_entry_menu(self, event):
        menu = ContextMenu(self, [
            ("Cắt", lambda: self.search_entry.event_generate("<<Cut>>")),
            ("Sao chép", lambda: self.search_entry.event_generate("<<Copy>>")),
            ("Dán", lambda: self.search_entry.event_generate("<<Paste>>")),
        ])
        menu.popup(event.x_root, event.y_root)

    # ---------- find in page ----------
    def _on_search_key(self, event):
        if event.keysym in ("Return", "Escape"):
            return
        self._do_search()

    def _do_search(self):
        self.text.tag_remove("match", "1.0", "end")
        self.text.tag_remove("match_current", "1.0", "end")
        self._matches = []
        self._match_idx = -1

        query = self.search_var.get().strip()
        if not query:
            return

        start = "1.0"
        while True:
            pos = self.text.search(query, start, stopindex="end", nocase=True)
            if not pos:
                break
            end = f"{pos}+{len(query)}c"
            self.text.tag_add("match", pos, end)
            self._matches.append(pos)
            start = end

        if self._matches:
            self._goto_match(0)

    def _goto_match(self, idx):
        self._match_idx = idx % len(self._matches)
        pos = self._matches[self._match_idx]
        query_len = len(self.search_var.get().strip())
        self.text.tag_remove("match_current", "1.0", "end")
        self.text.tag_add("match_current", pos, f"{pos}+{query_len}c")
        self.text.see(pos)

    def _next_match(self, _event=None):
        if self._matches:
            self._goto_match(self._match_idx + 1)
        return "break"

    def _clear_search(self):
        self.search_var.set("")
        self.text.tag_remove("match", "1.0", "end")
        self.text.tag_remove("match_current", "1.0", "end")
        self._matches = []
        self._match_idx = -1

    # ---------- visibility ----------
    def show(self):
        self.deiconify()
        self.lift()
        self._always_on_top = True
        self.attributes("-topmost", True)
        self.pin_btn.config(fg=FG_ACCENT)
        self.search_entry.focus_force()

    def toggle_always_on_top(self):
        self._always_on_top = not self._always_on_top
        self.attributes("-topmost", self._always_on_top)
        self.pin_btn.config(fg=FG_ACCENT if self._always_on_top else FG_MUTED)
        cfg = load_config()
        cfg["always_on_top"] = self._always_on_top
        save_config(cfg)

    def hide(self):
        self.flush_save()
        self.withdraw()

    def toggle(self):
        if self.winfo_viewable():
            self.hide()
        else:
            self.show()

    def flush_and_close(self):
        self.flush_save()
