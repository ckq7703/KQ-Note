import io
import os
import queue
import re
import tkinter as tk
import tkinter.simpledialog as simpledialog
import uuid
import webbrowser

from PIL import Image, ImageDraw, ImageGrab, ImageTk

from app import markup, store
from app.config import load_config, save_config
from app.sync import state as sync_state
from app.sync.engine import SyncEngine, content_hash
from app import winfx
from app.winfx import get_work_area, round_window

DOCK_EDGE_THRESHOLD = 24
DOCK_UNDOCK_DISTANCE = 40

SYNC_POLL_INTERVAL_MS = 45_000
SYNC_EVENT_DRAIN_MS = 500

URL_RE = re.compile(r"(https?://[^\s]+|www\.[^\s]+)")
IMAGE_MAX_SIZE = (300, 400)
IMAGE_CHECK_INTERVAL_MS = 300
ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
LOGO_PATH = os.path.join(ASSETS_DIR, "logo-kqnote.png")
GOOGLE_ICON_PATH = os.path.join(ASSETS_DIR, "google-auth-icon.png")
SCREENSHOT_ICON_PATH = os.path.join(ASSETS_DIR, "screenshot-30.png")
CLOUD_ICON_SIZE = 18
AVATAR_SIZE = 20
SCREENSHOT_ICON_SIZE = 18

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
        self._drag_undocked_this_gesture = False
        self._resize = {"x": 0, "y": 0, "w": 0, "h": 0}
        self._docked_side = None
        self._pre_dock_geometry = None
        self._appbar_registered = False
        self._save_after_id = None
        self._matches = []
        self._match_idx = -1
        self._photo_refs = {}
        self._pending_images = set()

        cfg = load_config()
        self._always_on_top = cfg.get("always_on_top", True)
        self.sync_engine = SyncEngine(cfg.get("sync_server_url"))

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
        self._pre_dock_geometry = cfg.get("pre_dock_geometry")
        docked_side = cfg.get("docked_side")
        if docked_side in ("left", "right"):
            # Set before scheduling so _apply_dock treats this as "already docked,
            # just re-applying geometry" rather than a fresh dock (which would
            # overwrite _pre_dock_geometry with the current, already-docked size).
            self._docked_side = docked_side
            self.after(10, lambda: self._apply_dock(docked_side))

        self._build()
        if self.sync_engine.is_logged_in():
            # Resuming a session from a previous run: notes.cloud.txt already
            # mirrors this account, refresh it with a normal (guarded) pull.
            self._load_content_into_editor(store.load_cloud_cache())
            self.after(1000, self.sync_engine.pull_async)
            self.after(SYNC_POLL_INTERVAL_MS, self._poll_sync)
        else:
            self._load_content_into_editor(store.load_content())

        self._update_cloud_icon()
        self.after(SYNC_EVENT_DRAIN_MS, self._drain_sync_events)

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

        self._cloud_icon_photo = self._load_square_photo(GOOGLE_ICON_PATH, CLOUD_ICON_SIZE)
        self._avatar_photo = None
        self.cloud_btn = tk.Label(header, bg=BG_HEADER, fg=FG_MUTED,
                                   font=("Segoe UI", 10), padx=10, cursor="hand2")
        self.cloud_btn.pack(side="right")
        self.cloud_btn.bind("<Button-1>", self._on_cloud_click)

        self._screenshot_icon_photo = self._load_square_photo(SCREENSHOT_ICON_PATH, SCREENSHOT_ICON_SIZE)
        self.screenshot_btn = tk.Label(header, bg=BG_HEADER, padx=10, cursor="hand2")
        if self._screenshot_icon_photo is not None:
            self.screenshot_btn.config(image=self._screenshot_icon_photo)
        else:
            self.screenshot_btn.config(text="📷", fg=FG_MUTED, font=("Segoe UI", 10))
        self.screenshot_btn.pack(side="right")
        self.screenshot_btn.bind("<Button-1>", lambda e: self._start_screenshot())

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

        def _tb_btn(text, command, italic=False):
            btn = tk.Label(toolbar, text=text, bg=BG, fg=FG_MUTED,
                            font=("Segoe UI", 9, "bold", "italic") if italic else ("Segoe UI", 9, "bold"),
                            cursor="hand2", padx=6)
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e: command())
            return btn

        _tb_btn("H1", self._toggle_heading)
        _tb_btn("B", lambda: self._toggle_inline("bold"))
        _tb_btn("i", lambda: self._toggle_inline("italic"), italic=True)
        _tb_btn("</>", lambda: self._toggle_inline("code"))
        _tb_btn("❝", self._toggle_blockquote)
        _tb_btn("1.", lambda: self._toggle_list("numbered"))
        _tb_btn("—", lambda: self._toggle_list("dash"))
        _tb_btn("+", lambda: self._toggle_list("plus"))
        _tb_btn("☑", lambda: self._toggle_list("checkbox"))
        _tb_btn("{ }", self._toggle_codeblock)
        _tb_btn("🔗", self._insert_link)

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True, padx=(10, 10), pady=(0, 8))

        self.text = tk.Text(
            body, bg=BG, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", wrap="word", font=("Segoe UI", 10), padx=4, pady=4,
            undo=True, borderwidth=0, highlightthickness=0,
            selectbackground=SELECT_BG, selectforeground=FG_TEXT,
        )
        self.text.pack(side="left", fill="both", expand=True)

        self.text.tag_configure("h1", font=("Segoe UI", 14, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h2", font=("Segoe UI", 13, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h3", font=("Segoe UI", 12, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h4", font=("Segoe UI", 11, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h5", font=("Segoe UI", 10, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h6", font=("Segoe UI", 10, "bold"), foreground=FG_MUTED)
        self.text.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        self.text.tag_configure("bolditalic", font=("Segoe UI", 10, "bold", "italic"))
        self.text.tag_configure("code", font=("Cascadia Mono", 9), background=BG_MENU, foreground=FG_TITLE_TAG)
        self.text.tag_configure("codeblock", font=("Cascadia Mono", 9), background=BG_MENU,
                                 lmargin1=8, lmargin2=8, spacing1=1, spacing3=1)
        self.text.tag_configure("blockquote", lmargin1=16, lmargin2=16, foreground=FG_MUTED,
                                 font=("Segoe UI", 10, "italic"))
        self.text.tag_configure("numbered", lmargin1=20, lmargin2=36)
        self.text.tag_configure("bullet1", lmargin1=40, lmargin2=56)
        self.text.tag_configure("bullet2", lmargin1=60, lmargin2=76)
        self.text.tag_configure("checkbox_off", lmargin1=40, lmargin2=56)
        self.text.tag_configure("checkbox_on", lmargin1=40, lmargin2=56,
                                 foreground=FG_MUTED, overstrike=True)
        self.text.tag_configure("hr", foreground=FG_MUTED, font=("Segoe UI", 8))
        self.text.tag_configure("table", font=("Cascadia Mono", 9), foreground=FG_TEXT)
        self.text.tag_configure("tableborder", font=("Cascadia Mono", 9), foreground=FG_MUTED)
        self.text.tag_configure("tableheader", font=("Cascadia Mono", 9, "bold"),
                                 background=BG_MENU, foreground="#ffffff")
        self.text.tag_raise("tableborder")
        self.text.tag_raise("tableheader")
        self.text.tag_configure("match", background=MATCH_BG)
        self.text.tag_configure("match_current", background=MATCH_CURRENT_BG)
        self.text.tag_configure("url", foreground=FG_ACCENT, underline=True)
        self.text.tag_raise("url")
        self.text.tag_bind("url", "<Button-1>", self._on_url_click)
        self.text.tag_bind("url", "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind("url", "<Leave>", lambda e: self.text.config(cursor="xterm"))
        self.text.tag_bind("checkbox_off", "<Button-1>", self._on_checkbox_click)
        self.text.tag_bind("checkbox_on", "<Button-1>", self._on_checkbox_click)

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
        self._drag_undocked_this_gesture = False

    def _drag_move(self, event):
        if self._docked_side is not None and not self._drag_undocked_this_gesture:
            if abs(event.x - self._drag["x"]) < DOCK_UNDOCK_DISTANCE and \
               abs(event.y - self._drag["y"]) < DOCK_UNDOCK_DISTANCE:
                return  # still docked — ignore small jitters until a real drag away
            self._undock(event)

        x = self.winfo_x() + (event.x - self._drag["x"])
        y = self.winfo_y() + (event.y - self._drag["y"])
        self.geometry(f"+{x}+{y}")

    def _undock(self, event):
        self._drag_undocked_this_gesture = True
        self._unregister_appbar_if_needed()
        self._docked_side = None
        if self._pre_dock_geometry:
            self.geometry(self._pre_dock_geometry)
            # winfo_x()/winfo_width() below need the new geometry applied first —
            # without this, _drag_move's position calc reads stale (docked) values.
            self.update_idletasks()
        # Re-anchor the drag so movement keeps following the cursor smoothly
        # after the size/position jump back to the floating geometry.
        self._drag["x"] = event.x
        self._drag["y"] = event.y

    def _unregister_appbar_if_needed(self):
        if self._appbar_registered:
            winfx.unregister_appbar(self.winfo_id())
            self._appbar_registered = False

    def _drag_end(self, _event):
        self._maybe_dock_or_save()

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
        self._unregister_appbar_if_needed()
        self._docked_side = None
        self._save_geometry()

    def _maybe_dock_or_save(self):
        # The last drag-move's geometry() set hasn't necessarily been flushed
        # yet — winfo_x()/winfo_width() below would otherwise risk reading the
        # position from before that final move.
        self.update_idletasks()

        work = get_work_area()
        if work is None:
            self._save_geometry()
            return

        left, top, right, bottom = work
        x = self.winfo_x()
        w = self.winfo_width()
        if x <= left + DOCK_EDGE_THRESHOLD:
            self._apply_dock("left")
        elif (x + w) >= right - DOCK_EDGE_THRESHOLD:
            self._apply_dock("right")
        else:
            self._unregister_appbar_if_needed()
            self._docked_side = None
            self._save_geometry()

    def _apply_dock(self, side):
        if self._docked_side is None:
            self._pre_dock_geometry = self.geometry()
        self._docked_side = side

        w = self._dock_width()
        hwnd = self.winfo_id()
        if not self._appbar_registered:
            winfx.register_appbar(hwnd)
            self._appbar_registered = True

        # Registers this window as an AppBar (same mechanism the Windows
        # taskbar uses) so maximized windows shrink to avoid it, instead of
        # just floating on top of everything at that screen position.
        rect = winfx.set_appbar_edge_pos(hwnd, side, w)
        if rect is None:
            self._unregister_appbar_if_needed()
            return
        x, y, width, height = rect
        self.geometry(f"{width}x{height}+{x}+{y}")
        # _save_geometry() reads self.geometry() right back — without flushing
        # here first, that read can still see the pre-dock size (same class of
        # stale-read issue as in _undock).
        self.update_idletasks()
        self._save_geometry()

    def _dock_width(self):
        # winfo_width() can still report Tk's "not yet mapped" placeholder (1px)
        # this soon after construction — fall back to the width baked into a
        # saved geometry string instead of trusting it blindly.
        live_w = self.winfo_width()
        if live_w > 1:
            return live_w
        for geom in (self._pre_dock_geometry, self.geometry()):
            if geom:
                try:
                    return int(geom.split("x")[0])
                except (ValueError, IndexError):
                    pass
        return MIN_W

    def _save_geometry(self):
        cfg = load_config()
        cfg["widget_geometry"] = self.geometry()
        cfg["docked_side"] = self._docked_side
        cfg["pre_dock_geometry"] = self._pre_dock_geometry
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
        if self.sync_engine.is_logged_in():
            # Logged-in content lives in its own file, kept separate from
            # notes.txt so logging out always reveals the local-only note untouched.
            store.save_cloud_cache(content)
            self.sync_engine.push_async(content)
        else:
            store.save_content(content)

    def _load_content_into_editor(self, content):
        self._photo_refs.clear()
        self._pending_images = set()
        markup.render_into_text(self.text, content,
                                 on_image=self._on_image_marker, on_hr=self._on_hr_marker)
        for tagname in self.text._kq_links:
            self._bind_link_tag(tagname)
        self._highlight_urls()
        if self._pending_images:
            self.after(IMAGE_CHECK_INTERVAL_MS, self._check_lazy_images)

    # ---------- cloud sync ----------
    def _load_square_photo(self, path, size):
        try:
            img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _make_avatar_photo(self, image_bytes, size):
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            img.putalpha(mask)
            return ImageTk.PhotoImage(img)
        except Exception:
            return None

    def _update_cloud_icon(self):
        if self.sync_engine.is_logged_in():
            avatar_bytes = store.load_avatar_bytes()
            photo = self._make_avatar_photo(avatar_bytes, AVATAR_SIZE) if avatar_bytes else None
            if photo is not None:
                self._avatar_photo = photo  # keep a reference so Tk doesn't garbage-collect it
                self.cloud_btn.config(image=photo, text="")
                return
        if self._cloud_icon_photo is not None:
            self.cloud_btn.config(image=self._cloud_icon_photo, text="")
        else:
            self.cloud_btn.config(image="", text="☁", fg=FG_MUTED)

    def _on_cloud_click(self, event):
        if not self.sync_engine.is_logged_in():
            self.sync_engine.login_with_google_async()
            return

        email = self.sync_engine.account_email() or "?"
        menu = ContextMenu(self, [
            (f"Đã đăng nhập: {email}", None),
            ("Đồng bộ ngay", self._sync_now),
            None,
            ("Đăng xuất", self._logout),
        ])
        menu.popup(event.x_root, event.y_root)

    def _on_login_success(self):
        self._update_cloud_icon()
        # Always fetch this account's cloud content on login rather than pushing
        # anything first — the local-only note and the account's note are
        # separate documents, so login must never push local content into it.
        self.sync_engine.pull_async(initial=True)
        self.after(SYNC_POLL_INTERVAL_MS, self._poll_sync)

    def _sync_now(self):
        self.sync_engine.pull_async()
        self.sync_engine.push_async(markup.serialize_from_text(self.text))

    def _logout(self):
        self.sync_engine.logout()
        # Local-only note and cloud-account note live in separate files, so
        # logging out just switches the editor back to notes.txt untouched —
        # nothing to merge or lose.
        self._load_content_into_editor(store.load_content())
        self._update_cloud_icon()

    def _poll_sync(self):
        if not self.sync_engine.is_logged_in():
            return
        self.sync_engine.pull_async()
        self.after(SYNC_POLL_INTERVAL_MS, self._poll_sync)

    def _apply_remote_update(self, result):
        local_content = markup.serialize_from_text(self.text)
        st = sync_state.load_state()
        if content_hash(local_content) != st.get("last_synced_hash"):
            return  # local has unsynced edits; let the next push reconcile instead of clobbering
        self._load_content_into_editor(result["content"])
        store.save_cloud_cache(result["content"])
        sync_state.update_after_sync(result["version"], content_hash(result["content"]))
        self._update_cloud_icon()

    def _apply_initial_pull(self, result):
        # Logging in always shows this account's cloud content, even if that's
        # empty for a brand-new account — it's a separate "document" from the
        # local-only note, never auto-merged or auto-pushed into.
        self._load_content_into_editor(result["content"])
        store.save_cloud_cache(result["content"])
        sync_state.update_after_sync(result["version"], content_hash(result["content"]))
        self._update_cloud_icon()

    def _drain_sync_events(self):
        while True:
            try:
                kind, payload = self.sync_engine.events.get_nowait()
            except queue.Empty:
                break
            if kind == "remote_update":
                self._apply_remote_update(payload)
            elif kind == "initial_pull":
                self._apply_initial_pull(payload)
            elif kind == "google_login_success":
                self._on_login_success()
            elif kind == "google_login_error":
                self._update_cloud_icon()
            elif kind == "synced":
                self._update_cloud_icon()
            elif kind == "conflict_resolved":
                self._update_cloud_icon()
            elif kind == "auth_required":
                # Refresh also failed — the session is unrecoverable, so fall
                # back to a clean logged-out state instead of a stuck icon.
                self._logout()
            elif kind == "error":
                self._update_cloud_icon()
        self.after(SYNC_EVENT_DRAIN_MS, self._drain_sync_events)

    # ---------- inline formatting (bold / italic / code) ----------
    _INLINE_TAGS = ("bold", "italic", "bolditalic", "code")

    def _toggle_inline(self, tagname):
        try:
            start = self.text.index("sel.first")
            end = self.text.index("sel.last")
        except tk.TclError:
            return  # inline formatting needs a selection

        current = set(self.text.tag_names(start))

        if tagname == "code":
            is_code = "code" in current
            for t in self._INLINE_TAGS:
                self.text.tag_remove(t, start, end)
            if not is_code:
                self.text.tag_add("code", start, end)
            self._on_text_changed()
            return

        has_bold = "bold" in current or "bolditalic" in current
        has_italic = "italic" in current or "bolditalic" in current
        for t in self._INLINE_TAGS:
            self.text.tag_remove(t, start, end)

        if tagname == "bold":
            has_bold = not has_bold
        else:
            has_italic = not has_italic

        if has_bold and has_italic:
            self.text.tag_add("bolditalic", start, end)
        elif has_bold:
            self.text.tag_add("bold", start, end)
        elif has_italic:
            self.text.tag_add("italic", start, end)

        self._on_text_changed()

    # ---------- block formatting (heading / blockquote / code block) ----------
    _BLOCK_LEVEL_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
                         "numbered", "bullet1", "bullet2", "checkbox_off",
                         "checkbox_on", "hr", "codeblock")

    def _selected_line_range(self):
        try:
            start_line = int(self.text.index("sel.first").split(".")[0])
            end_line = int(self.text.index("sel.last").split(".")[0])
        except tk.TclError:
            start_line = end_line = int(self.text.index("insert").split(".")[0])
        return start_line, end_line

    def _strip_block_level(self, start, end):
        for t in self._BLOCK_LEVEL_TAGS:
            self.text.tag_remove(t, start, end)

    def _strip_line_tags(self, start, end):
        for t in self.text.tag_names():
            if t == "sel":
                continue
            self.text.tag_remove(t, start, end)

    def _toggle_heading(self):
        start_line, end_line = self._selected_line_range()
        for lineno in range(start_line, end_line + 1):
            ls, le = f"{lineno}.0", f"{lineno}.end"
            tags = self.text.tag_names(ls)
            current_level = next((i for i in range(1, 7) if f"h{i}" in tags), 0)
            self._strip_block_level(ls, le)
            if current_level == 0:
                self.text.tag_add("h1", ls, le)
            elif current_level < 3:
                self.text.tag_add(f"h{current_level + 1}", ls, le)
        self._on_text_changed()

    def _toggle_blockquote(self):
        start_line, end_line = self._selected_line_range()
        turning_on = "blockquote" not in self.text.tag_names(f"{start_line}.0")
        for lineno in range(start_line, end_line + 1):
            ls, le = f"{lineno}.0", f"{lineno}.end"
            self._strip_block_level(ls, le)
            if turning_on:
                self.text.tag_add("blockquote", ls, le)
        self._on_text_changed()

    def _toggle_codeblock(self):
        start_line, end_line = self._selected_line_range()
        turning_on = "codeblock" not in self.text.tag_names(f"{start_line}.0")
        for lineno in range(start_line, end_line + 1):
            ls, le = f"{lineno}.0", f"{lineno}.end"
            self._strip_line_tags(ls, le)
            if turning_on:
                self.text.tag_add("codeblock", ls, le)
        self._on_text_changed()

    # ---------- links ----------
    def _bind_link_tag(self, tagname):
        self.text.tag_configure(tagname, foreground=FG_ACCENT, underline=True)
        self.text.tag_raise(tagname)
        self.text.tag_bind(tagname, "<Button-1>", lambda e, t=tagname: self._open_link(t))
        self.text.tag_bind(tagname, "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind(tagname, "<Leave>", lambda e: self.text.config(cursor="xterm"))

    def _open_link(self, tagname):
        url = self.text._kq_links.get(tagname, "")
        if not url:
            return
        if url.startswith("www."):
            url = "http://" + url
        webbrowser.open(url)

    def _insert_link(self):
        try:
            start = self.text.index("sel.first")
            end = self.text.index("sel.last")
        except tk.TclError:
            return
        url = simpledialog.askstring("Chèn liên kết", "URL:", parent=self)
        if not url:
            return
        tagname = f"link_{len(self.text._kq_links)}"
        self.text._kq_links[tagname] = url
        self.text.tag_add(tagname, start, end)
        self._bind_link_tag(tagname)
        self._on_text_changed()

    # ---------- checkbox lists ----------
    def _on_checkbox_click(self, event):
        index = self.text.index(f"@{event.x},{event.y}")
        col = int(index.split(".")[1])
        if col > 5:
            return None  # click landed past the "- [ ] " marker; let normal editing happen
        lineno = int(index.split(".")[0])
        ls, le = f"{lineno}.0", f"{lineno}.end"
        was_checked = "checkbox_on" in self.text.tag_names(ls)
        new_prefix = "- [ ] " if was_checked else "- [x] "
        self.text.delete(ls, f"{ls}+6c")
        self.text.insert(ls, new_prefix)
        old_tag, new_tag = ("checkbox_on", "checkbox_off") if was_checked else ("checkbox_off", "checkbox_on")
        self.text.tag_remove(old_tag, ls, le)
        self.text.tag_add(new_tag, ls, le)
        self._on_text_changed()
        return "break"

    # ---------- ordered / bullet / checkbox lists ----------
    _LIST_TAGS = {"numbered": "numbered", "dash": "bullet1", "plus": "bullet2", "checkbox": "checkbox_off"}
    _LIST_ALL_TAGS = {"numbered", "bullet1", "bullet2", "checkbox_off", "checkbox_on"}
    _LIST_PREFIX_RE = re.compile(r"^(\d+\. |- \[[ xX]\] |- |\+ )")

    def _toggle_list(self, kind):
        tagname = self._LIST_TAGS[kind]

        start_line, end_line = self._selected_line_range()

        turning_on = not (self._LIST_ALL_TAGS & set(self.text.tag_names(f"{start_line}.0")))

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

            self._strip_block_level(line_start, line_end)

            m = self._LIST_PREFIX_RE.match(line_text)
            if m:
                self.text.delete(line_start, f"{line_start}+{m.end()}c")

            if turning_on:
                if kind == "numbered":
                    prefix = f"{next_num}. "
                    next_num += 1
                elif kind == "dash":
                    prefix = "- "
                elif kind == "plus":
                    prefix = "+ "
                else:
                    prefix = "- [ ] "
                self.text.insert(line_start, prefix)
                self.text.tag_add(tagname, line_start, f"{lineno}.end")

        self._on_text_changed()

    def _on_return_key(self, _event):
        lineno = int(self.text.index("insert").split(".")[0])
        line_start = f"{lineno}.0"
        line_end = f"{lineno}.end"
        line_text = self.text.get(line_start, line_end)
        tags = self.text.tag_names(line_start)

        if "checkbox_off" in tags or "checkbox_on" in tags:
            m = re.match(r"^- \[[ xX]\] (.*)$", line_text)
            if m and not m.group(1).strip():
                self.text.delete(line_start, line_end)
                self._on_text_changed()
                return "break"
            tagname, prefix = "checkbox_off", "- [ ] "
        elif "numbered" in tags:
            m = re.match(r"^(\d+)\. (.*)$", line_text)
            if m and not m.group(2).strip():
                self.text.delete(line_start, line_end)
                self._on_text_changed()
                return "break"
            next_num = int(m.group(1)) + 1 if m else 1
            tagname, prefix = "numbered", f"{next_num}. "
        elif "bullet1" in tags or "bullet2" in tags:
            tagname = "bullet1" if "bullet1" in tags else "bullet2"
            prefix = "- " if tagname == "bullet1" else "+ "
            body = line_text[len(prefix):] if line_text.startswith(prefix) else line_text
            if not body.strip():
                self.text.delete(line_start, line_end)
                self._on_text_changed()
                return "break"
        elif "codeblock" in tags:
            self.text.insert("insert", "\n")
            new_line = int(self.text.index("insert").split(".")[0])
            self.text.tag_add("codeblock", f"{new_line}.0", f"{new_line}.end")
            self._on_text_changed()
            return "break"
        else:
            self.text.insert("insert", "\n")
            new_line = int(self.text.index("insert").split(".")[0])
            self._strip_line_tags(f"{new_line}.0", f"{new_line}.end")
            self._on_text_changed()
            return "break"

        self.text.insert("insert", f"\n{prefix}")
        new_line = int(self.text.index("insert").split(".")[0])

        for ln in (lineno, new_line):
            ln_start, ln_end = f"{ln}.0", f"{ln}.end"
            self._strip_block_level(ln_start, ln_end)
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
            None,
            ("Tiêu đề (H1)", self._toggle_heading),
            ("In đậm", lambda: self._toggle_inline("bold")),
            ("In nghiêng", lambda: self._toggle_inline("italic")),
            ("Code", lambda: self._toggle_inline("code")),
            ("Trích dẫn", self._toggle_blockquote),
            ("Danh sách số", lambda: self._toggle_list("numbered")),
            ("Gạch đầu dòng —", lambda: self._toggle_list("dash")),
            ("Gạch đầu dòng +", lambda: self._toggle_list("plus")),
            ("Checkbox", lambda: self._toggle_list("checkbox")),
            ("Khối code", self._toggle_codeblock),
            ("Chèn liên kết", self._insert_link),
            ("Chụp màn hình", self._start_screenshot),
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
            return

        try:
            clip_text = self.text.clipboard_get()
        except tk.TclError:
            clip_text = None

        if clip_text is None:
            self.text.event_generate("<<Paste>>")
            self._on_text_changed()
            return

        try:
            self.text.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

        before_links = set(self.text._kq_links)
        markup.insert_markdown_at_cursor(self.text, clip_text, on_image=self._on_image_marker,
                                          on_hr=self._on_hr_marker)
        for tagname in set(self.text._kq_links) - before_links:
            self._bind_link_tag(tagname)

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

    # ---------- screenshot capture ----------
    def _start_screenshot(self):
        # Hide the note itself first so it isn't part of the capture and
        # doesn't block the view of whatever's behind it.
        self.withdraw()
        self.after(150, self._open_screenshot_overlay)

    def _open_screenshot_overlay(self):
        overlay = tk.Toplevel(self)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.35)
        sw, sh = overlay.winfo_screenwidth(), overlay.winfo_screenheight()
        overlay.geometry(f"{sw}x{sh}+0+0")
        overlay.configure(bg="#000000")

        canvas = tk.Canvas(overlay, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        drag = {"start": None, "rect": None}

        def on_press(event):
            drag["start"] = (event.x_root, event.y_root)
            drag["rect"] = canvas.create_rectangle(
                event.x_root, event.y_root, event.x_root, event.y_root,
                outline=FG_ACCENT, width=2)

        def on_drag(event):
            if drag["rect"] is None:
                return
            x0, y0 = drag["start"]
            canvas.coords(drag["rect"], x0, y0, event.x_root, event.y_root)

        def on_release(event):
            overlay.destroy()
            if drag["start"] is None:
                self._cancel_screenshot()
                return
            x0, y0 = drag["start"]
            x1, y1 = event.x_root, event.y_root
            left, right = sorted((x0, x1))
            top, bottom = sorted((y0, y1))
            if right - left < 4 or bottom - top < 4:
                self._cancel_screenshot()
                return
            # Give the overlay a moment to actually disappear from the screen
            # before grabbing — otherwise the capture can include it.
            self.after(120, lambda: self._capture_and_insert(left, top, right, bottom))

        def on_cancel(_event=None):
            overlay.destroy()
            self._cancel_screenshot()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", on_cancel)
        overlay.focus_force()

    def _cancel_screenshot(self):
        self.deiconify()
        self.lift()

    def _capture_and_insert(self, left, top, right, bottom):
        try:
            img = ImageGrab.grab(bbox=(left, top, right, bottom))
        except Exception:
            img = None
        self.deiconify()
        self.lift()
        if img is not None:
            self._insert_image_now(img)

    def _on_image_marker(self, file_id):
        name = f"img_{file_id}"
        placeholder = self._make_placeholder_image()
        self.text.image_create("end", image=placeholder, name=name)
        self._photo_refs[name] = placeholder
        self._pending_images.add(name)
        self._bind_image_click(name, file_id)

    def _on_hr_marker(self):
        frame = tk.Frame(self.text, bg=BORDER, height=2, width=280)
        frame._kq_is_hr = True
        self.text.window_create("end", window=frame)

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
        self._unregister_appbar_if_needed()
        self.flush_save()
