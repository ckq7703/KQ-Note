import datetime
import io
import os
import queue
import re
import tkinter as tk
from tkinter import ttk
import tkinter.messagebox as messagebox
import tkinter.simpledialog as simpledialog
import uuid
import webbrowser

from PIL import Image, ImageDraw, ImageGrab, ImageTk

from app import ai_helper, markup, store
from app.config import load_config, save_config
from app.sync.engine import SyncEngine
from app.theme import BG, BG_HEADER, BG_MENU, BORDER, FG_TEXT, FG_MUTED, FG_ACCENT, FG_TITLE_TAG, MATCH_BG, MATCH_CURRENT_BG, SELECT_BG
from app.trash_dialog import TrashDialog
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

MIN_W, MIN_H = 300, 260
WINDOW_RADIUS = 16
MENU_RADIUS = 8


class ContextMenu(tk.Toplevel):
    """Popup menu with optional categories: pass (label, [subitems...]) for a
    row that opens a flyout of those subitems on hover, closing again once
    the pointer leaves both the row and the flyout.

    The flyout is a Frame placed inside this SAME Toplevel (not a second
    popup window) and the window itself grows/shrinks to fit it. That's
    deliberate: Tk's local grab_set() restricts input to a single toplevel,
    so a real second popup window opened alongside this one — while this one
    still holds the grab — wouldn't be clickable. Keeping everything in one
    window sidesteps that entirely."""

    _ASSUMED_FLYOUT_WIDTH = 200
    _FLYOUT_CLOSE_DELAY_MS = 220

    def __init__(self, parent, items):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=BG_MENU)

        self._items = items
        self._anchor = None
        self._main_size = (0, 0)
        self._flyout_open_key = None
        self._flyout_close_job = None

        self.frame = tk.Frame(self, bg=BG_MENU)
        self.frame.place(x=0, y=0)
        self.frame.bind("<Button-1>", self._dismiss)

        self.flyout = tk.Frame(self, bg=BG_MENU)
        self.flyout.bind("<Enter>", lambda e: self._cancel_flyout_close())
        self.flyout.bind("<Leave>", lambda e: self._schedule_flyout_close())
        self.flyout.bind("<Button-1>", self._dismiss)

        self.bind("<Escape>", self._dismiss)
        self.bind("<FocusOut>", self._dismiss)
        # grab_set() routes every app click here while open; catch clicks that
        # land on empty space (not on an item row) and dismiss instead of no-op.
        self.bind("<Button-1>", self._dismiss)

        for item in items:
            self._add_main_row(item)

    def _add_main_row(self, item):
        if item is None:
            sep = tk.Frame(self.frame, bg=BORDER, height=1)
            sep.pack(fill="x", padx=6, pady=4)
            return

        label, action = item
        if isinstance(action, list):
            row = self._make_row(self.frame, f"{label}   ▸")
            row.bind("<Enter>", lambda e, r=row, sub=action: self._on_category_enter(r, sub), add="+")
            row.bind("<Leave>", lambda e: self._schedule_flyout_close(), add="+")
            row.bind("<Button-1>", lambda e: "break")
        else:
            row = self._make_row(self.frame, label)
            row.bind("<Enter>", lambda e: self._schedule_flyout_close(), add="+")
            row.bind("<Button-1>", self._make_handler(action))

    def _make_row(self, master, text):
        row = tk.Label(master, text=text, bg=BG_MENU, fg=FG_TEXT, anchor="w",
                        font=("Segoe UI", 9), padx=16, pady=7, cursor="hand2")
        row.pack(fill="x")
        row.bind("<Enter>", lambda e, r=row: r.config(bg=FG_ACCENT, fg="#ffffff"))
        row.bind("<Leave>", lambda e, r=row: r.config(bg=BG_MENU, fg=FG_TEXT))
        return row

    def _make_handler(self, command):
        def handle(_event, cb=command):
            self._invoke(cb)
            return "break"  # stop the click from also bubbling up to the
            # Toplevel-level dismiss binding (Tk bindtags propagate a
            # widget's events through its ancestors), which would otherwise
            # both destroy the menu AND run the command's own side effects
            # against a half-torn-down widget.
        return handle

    # ---- hover-opened flyout ----
    def _on_category_enter(self, row, sub_items):
        self._cancel_flyout_close()
        key = id(sub_items)
        if self._flyout_open_key == key:
            return  # already showing this exact category
        self._flyout_open_key = key

        for child in self.flyout.winfo_children():
            child.destroy()
        for item in sub_items:
            self._add_flyout_row(item)

        self.flyout.update_idletasks()
        sub_w = self.flyout.winfo_reqwidth()
        sub_h = self.flyout.winfo_reqheight()
        main_w, main_h = self._main_size
        row_y = row.winfo_rooty() - self.winfo_rooty()

        total_w = main_w + sub_w
        total_h = max(main_h, row_y + sub_h)
        x, y = self._anchor
        self.geometry(f"{total_w}x{total_h}+{x}+{y}")
        self.flyout.place(x=main_w, y=row_y)
        round_window(self, MENU_RADIUS)

    def _add_flyout_row(self, item):
        if item is None:
            sep = tk.Frame(self.flyout, bg=BORDER, height=1)
            sep.pack(fill="x", padx=6, pady=4)
            return
        label, action = item
        row = self._make_row(self.flyout, label)
        row.bind("<Button-1>", self._make_handler(action))

    def _schedule_flyout_close(self):
        self._cancel_flyout_close()
        self._flyout_close_job = self.after(self._FLYOUT_CLOSE_DELAY_MS, self._close_flyout)

    def _cancel_flyout_close(self):
        if self._flyout_close_job is not None:
            self.after_cancel(self._flyout_close_job)
            self._flyout_close_job = None

    def _close_flyout(self):
        self._flyout_close_job = None
        if self._flyout_open_key is None:
            return
        self._flyout_open_key = None
        self.flyout.place_forget()
        main_w, main_h = self._main_size
        x, y = self._anchor
        self.geometry(f"{main_w}x{main_h}+{x}+{y}")
        round_window(self, MENU_RADIUS)

    def _invoke(self, command):
        owner = self.master
        self.destroy()
        if command:
            # Deferred rather than called inline: running it in the same call
            # stack as destroy() (which releases this menu's grab_set()) can
            # race the grab teardown on Windows — actions that themselves
            # grab focus/attention right away (like the screenshot overlay)
            # could then misbehave. A tick later, the menu and its grab are
            # fully gone.
            owner.after(10, command)

    def _dismiss(self, _event=None):
        if self.winfo_exists():
            self.destroy()

    def popup(self, x, y, direction="down"):
        self.update_idletasks()
        main_w = self.frame.winfo_reqwidth()
        main_h = self.frame.winfo_reqheight()
        self._main_size = (main_w, main_h)

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()

        if x + main_w > screen_w:
            x = max(0, screen_w - main_w - 8)

        if direction == "up":
            y = max(0, y - main_h)
        else:
            if y + main_h > screen_h:
                y = max(0, screen_h - main_h - 8)

        self._anchor = (x, y)
        self.geometry(f"{main_w}x{main_h}+{x}+{y}")
        round_window(self, MENU_RADIUS)
        self.deiconify()
        self.focus_force()
class AIPreviewDialog(tk.Toplevel):
    def __init__(self, parent, content, on_insert, on_replace):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=BG)
        self.transient(parent)

        # Center dialog over parent window
        parent.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()

        w, h = 540, 440
        cx = max(0, px + (pw - w) // 2)
        cy = max(0, py + (ph - h) // 2)
        self.geometry(f"{w}x{h}+{cx}+{cy}")

        hdr = tk.Frame(self, bg=BG_HEADER, padx=10, pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="👁️ Xem trước kết quả AI (Rendered)", bg=BG_HEADER, fg=FG_ACCENT, font=("Segoe UI", 10, "bold")).pack(side="left")

        close_btn = tk.Label(hdr, text="✕", bg=BG_HEADER, fg=FG_MUTED, font=("Segoe UI", 10, "bold"), cursor="hand2", padx=4)
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self.destroy())

        body = tk.Frame(self, bg=BG, padx=10, pady=10)
        body.pack(fill="both", expand=True)

        self.text = tk.Text(
            body, bg=BG, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", wrap="word", font=("Segoe UI", 10), padx=8, pady=8,
            borderwidth=0, highlightthickness=1, highlightbackground=BORDER,
            selectbackground=SELECT_BG, selectforeground=FG_TEXT
        )
        scrollbar = ttk.Scrollbar(body, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.text.tag_configure("h1", font=("Segoe UI", 14, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h2", font=("Segoe UI", 13, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h3", font=("Segoe UI", 12, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h4", font=("Segoe UI", 11, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("h5", font=("Segoe UI", 10, "bold"), foreground=FG_TITLE_TAG)
        self.text.tag_configure("bold", font=("Segoe UI", 10, "bold"))
        self.text.tag_configure("italic", font=("Segoe UI", 10, "italic"))
        self.text.tag_configure("bolditalic", font=("Segoe UI", 10, "bold", "italic"))
        self.text.tag_configure("code", font=("Cascadia Mono", 9), background=BG_MENU, foreground=FG_ACCENT)
        self.text.tag_configure("codeblock", font=("Cascadia Mono", 9), background=BG_MENU, foreground=FG_TEXT)
        self.text.tag_configure("bullet1", font=("Segoe UI", 10), foreground=FG_ACCENT)
        self.text.tag_configure("bullet2", font=("Segoe UI", 10), foreground=FG_MUTED)
        self.text.tag_configure("numbered", font=("Segoe UI", 10, "bold"), foreground=FG_ACCENT)
        self.text.tag_configure("checkbox_off", font=("Segoe UI", 10), foreground=FG_MUTED)
        self.text.tag_configure("checkbox_on", font=("Segoe UI", 10), foreground=FG_ACCENT)

        markup.render_into_text(self.text, content)

        footer = tk.Frame(self, bg=BG_HEADER, padx=10, pady=8)
        footer.pack(fill="x")

        def _make_btn(parent_f, text, cmd, bg_col=BG_MENU, fg_col=FG_TEXT):
            b = tk.Label(parent_f, text=text, bg=bg_col, fg=fg_col, font=("Segoe UI", 9, "bold"), cursor="hand2", padx=10, pady=4)
            b.pack(side="left", padx=4)
            b.bind("<Button-1>", lambda e: [cmd(), self.destroy()])
            return b

        _make_btn(footer, "📥 Chèn vào note", on_insert, FG_ACCENT, "#ffffff")
        _make_btn(footer, "🔄 Thay thế note", on_replace, BG_MENU, FG_TEXT)
        _make_btn(footer, "Đóng", lambda: None, BG_MENU, FG_MUTED)
        self.after(10, lambda: round_window(self, 12))


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
        self._db_content = None  # the stored text the editor was last loaded from / saved to
        self._editor_baseline = None  # what the editor serialised to at that moment (to tell if the user typed)
        self._sync_after_id = None
        self._poll_after_id = None

        cfg = load_config()
        self._always_on_top = cfg.get("always_on_top", True)
        self.sync_engine = SyncEngine(cfg.get("sync_server_url"))
        # Signed in: show the account's notes (already mirrored on disk); otherwise the local-only ones.
        account = self.sync_engine.account_id() if self.sync_engine.is_logged_in() else None
        store.set_scope(account)
        self.active_note_id = store.ensure_note() if account else store.get_active_note_id()

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
        self.wm_minsize(280, 320)

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
        self._load_content_into_editor(store.load_content())
        if self.sync_engine.is_logged_in():
            self.after(1000, self.sync_engine.sync_async)
            self._schedule_poll()

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

        self.list_nav_btn = tk.Label(header, text="☰", bg=BG_HEADER, fg=FG_TEXT,
                                     font=("Segoe UI", 11, "bold"), padx=10, pady=9, cursor="hand2")
        self.list_nav_btn.pack(side="left")
        self.list_nav_btn.bind("<Button-1>", lambda e: self._toggle_view_mode())

        # Header menu dropdown button (...)
        self.menu_more_btn = tk.Label(
            header, text="⋯", bg=BG_HEADER, fg=FG_TEXT,
            font=("Segoe UI", 13, "bold"), padx=12, pady=6, cursor="hand2"
        )
        self.menu_more_btn.pack(side="right")
        self.menu_more_btn.bind("<Button-1>", self._show_header_dropdown_menu)
        self.menu_more_btn.bind("<Enter>", lambda e: self.menu_more_btn.config(bg=BG_MENU))
        self.menu_more_btn.bind("<Leave>", lambda e: self.menu_more_btn.config(bg=BG_HEADER))

        # Sync status (a cloud glyph whose colour says how sync is doing; blank when signed out)
        self.sync_status_lbl = tk.Label(header, text="", bg=BG_HEADER, fg=FG_MUTED,
                                        font=("Segoe UI", 11), padx=0, pady=6, cursor="hand2")
        self.sync_status_lbl.pack(side="right")
        self.sync_status_lbl.bind("<Button-1>", self._show_header_dropdown_menu)

        # Main view containers
        self._setup_custom_scrollbar_style()
        self.detail_view_frame = tk.Frame(outer, bg=BG)
        self.list_view_frame = tk.Frame(outer, bg=BG)

        # Build Detail View (Text Editor)
        self._build_detail_view(self.detail_view_frame)

        # Build List View (Note List Interface)
        self._build_list_view(self.list_view_frame)

        # Show detail view by default
        self.detail_view_frame.pack(fill="both", expand=True)

        grip = tk.Label(outer, text="⋰", bg=BG, fg=FG_MUTED, cursor="size_nw_se",
                         font=("Segoe UI", 10))
        grip.place(relx=1.0, rely=1.0, anchor="se")
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_move)
        grip.bind("<ButtonRelease-1>", self._resize_end)

    def _setup_custom_scrollbar_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(
            "Dark.Vertical.TScrollbar",
            grabbackground="#444448",
            troughcolor=BG,
            background="#2d2d30",
            bordercolor=BG,
            arrowcolor=BG,
            lightcolor=BG,
            darkcolor=BG,
            borderwidth=0,
            arrowsize=0,
            relief="flat",
            width=8
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#606066"), ("pressed", "#007acc")],
            grabbackground=[("active", "#606066"), ("pressed", "#007acc")]
        )

    def _build_detail_view(self, parent):
        search_row = tk.Frame(parent, bg=BG, padx=10, pady=8)
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

        toolbar = tk.Frame(parent, bg=BG)
        toolbar.pack(fill="x", padx=10, pady=(6, 4))

        # Pack AI button FIRST on right so it is ALWAYS visible
        self.ai_btn = tk.Label(
            toolbar, text="✨ AI", bg=BG, fg=FG_ACCENT,
            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=8, pady=2
        )
        self.ai_btn.pack(side="right")
        self.ai_btn.bind("<Button-1>", lambda e: self._toggle_ai_footer())
        self.ai_btn.bind("<Enter>", lambda e: self.ai_btn.config(bg=BG_MENU))
        self.ai_btn.bind("<Leave>", lambda e: self.ai_btn.config(bg=BG if not getattr(self, "ai_footer_visible", False) else BG_MENU))

        # Wide formatting frame (all 11 buttons)
        self.tb_wide_frame = tk.Frame(toolbar, bg=BG)
        self.tb_wide_frame.pack(side="left")

        def _tb_btn(parent_frame, text, command, italic=False):
            btn = tk.Label(parent_frame, text=text, bg=BG, fg=FG_MUTED,
                            font=("Segoe UI", 9, "bold", "italic") if italic else ("Segoe UI", 9, "bold"),
                            cursor="hand2", padx=5)
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda e: command())
            btn.bind("<Enter>", lambda e: btn.config(bg=BG_MENU, fg=FG_TEXT))
            btn.bind("<Leave>", lambda e: btn.config(bg=BG, fg=FG_MUTED))
            return btn

        _tb_btn(self.tb_wide_frame, "H1", self._toggle_heading)
        _tb_btn(self.tb_wide_frame, "B", lambda: self._toggle_inline("bold"))
        _tb_btn(self.tb_wide_frame, "i", lambda: self._toggle_inline("italic"), italic=True)
        _tb_btn(self.tb_wide_frame, "</>", lambda: self._toggle_inline("code"))
        _tb_btn(self.tb_wide_frame, "❝", self._toggle_blockquote)
        _tb_btn(self.tb_wide_frame, "1.", lambda: self._toggle_list("numbered"))
        _tb_btn(self.tb_wide_frame, "—", lambda: self._toggle_list("dash"))
        _tb_btn(self.tb_wide_frame, "+", lambda: self._toggle_list("plus"))
        _tb_btn(self.tb_wide_frame, "☑", lambda: self._toggle_list("checkbox"))
        _tb_btn(self.tb_wide_frame, "{ }", self._toggle_codeblock)
        _tb_btn(self.tb_wide_frame, "🔗", self._insert_link)

        # Compact formatting frame (essential buttons + dropdown menu for narrow screens)
        self.tb_compact_frame = tk.Frame(toolbar, bg=BG)

        _tb_btn(self.tb_compact_frame, "B", lambda: self._toggle_inline("bold"))
        _tb_btn(self.tb_compact_frame, "i", lambda: self._toggle_inline("italic"), italic=True)
        _tb_btn(self.tb_compact_frame, "</>", lambda: self._toggle_inline("code"))

        fmt_dropdown_btn = tk.Label(
            self.tb_compact_frame, text="🎨 Định dạng ▾", bg=BG, fg=FG_MUTED,
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=2
        )
        fmt_dropdown_btn.pack(side="left")
        fmt_dropdown_btn.bind("<Button-1>", self._show_toolbar_format_menu)
        fmt_dropdown_btn.bind("<Enter>", lambda e: fmt_dropdown_btn.config(bg=BG_MENU, fg=FG_TEXT))
        fmt_dropdown_btn.bind("<Leave>", lambda e: fmt_dropdown_btn.config(bg=BG, fg=FG_MUTED))

        # Responsive listener on top toolbar resize
        def _on_toolbar_resize(event):
            if event.width < 420:
                self.tb_wide_frame.pack_forget()
                self.tb_compact_frame.pack(side="left")
            else:
                self.tb_compact_frame.pack_forget()
                self.tb_wide_frame.pack(side="left")

        toolbar.bind("<Configure>", _on_toolbar_resize)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=(10, 10), pady=(0, 8))

        self.text = tk.Text(
            body, bg=BG, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", wrap="word", font=("Segoe UI", 10), padx=4, pady=4,
            undo=True, borderwidth=0, highlightthickness=0,
            selectbackground=SELECT_BG, selectforeground=FG_TEXT,
            inactiveselectbackground=SELECT_BG,
        )
        text_scrollbar = ttk.Scrollbar(body, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.text.yview)
        self.text.configure(yscrollcommand=text_scrollbar.set)
        self.text.pack(side="left", fill="both", expand=True)
        text_scrollbar.pack(side="right", fill="y")

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
        self.text.tag_raise("sel")
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

        self._build_ai_footer(parent)

    def _build_ai_footer(self, parent):
        self.ai_footer_visible = False
        self.ai_chat_history = []
        self.ai_raw_response = ""
        self.ai_view_mode = "rendered"

        self.ai_footer_frame = tk.Frame(
            parent, bg=BG_HEADER, highlightbackground=BORDER, highlightthickness=1, padx=8, pady=4
        )

        # Top Resizer Bar (Draggable to resize response box height)
        resizer = tk.Frame(self.ai_footer_frame, bg=BORDER, height=6, cursor="size_ns")
        resizer.pack(fill="x", side="top", pady=(0, 2))

        def _on_resizer_press(e):
            self._ai_drag_start_y = e.y_root
            try:
                self._ai_init_h = int(self.ai_res_text.cget("height"))
            except Exception:
                self._ai_init_h = 4
            self.ai_response_box.pack(fill="x", pady=(2, 4))

        def _on_resizer_motion(e):
            if hasattr(self, "_ai_drag_start_y") and hasattr(self, "_ai_init_h"):
                dy = self._ai_drag_start_y - e.y_root
                line_delta = int(dy // 14)
                new_h = max(2, min(25, self._ai_init_h + line_delta))
                self.ai_res_text.configure(height=new_h)

        resizer.bind("<ButtonPress-1>", _on_resizer_press)
        resizer.bind("<B1-Motion>", _on_resizer_motion)

        # 1. Header row inside AI footer
        hdr_row = tk.Frame(self.ai_footer_frame, bg=BG_HEADER)
        hdr_row.pack(fill="x", pady=(0, 4))

        self.ai_title_lbl = tk.Label(
            hdr_row, text="✦ Trợ lý AI (Gemini)", bg=BG_HEADER, fg=FG_ACCENT,
            font=("Segoe UI", 9, "bold")
        )
        self.ai_title_lbl.pack(side="left")

        self.ai_model_lbl = tk.Label(
            hdr_row, text="", bg=BG_HEADER, fg=FG_MUTED,
            font=("Segoe UI", 8, "italic")
        )
        self.ai_model_lbl.pack(side="left", padx=(4, 0))

        # Close button on far right
        close_btn = tk.Label(
            hdr_row, text="✕", bg=BG_HEADER, fg=FG_MUTED,
            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=4
        )
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self._toggle_ai_footer())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=FG_TEXT))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=FG_MUTED))

        # Wide header menus frame (horizontal)
        self.hdr_wide_menus = tk.Frame(hdr_row, bg=BG_HEADER)
        self.hdr_wide_menus.pack(side="right")

        settings_menu_btn = tk.Label(
            self.hdr_wide_menus, text="⚙️ Cài đặt ▾", bg=BG_HEADER, fg=FG_MUTED,
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=1
        )
        settings_menu_btn.pack(side="right", padx=3)
        settings_menu_btn.bind("<Button-1>", self._show_ai_settings_menu)
        settings_menu_btn.bind("<Enter>", lambda e: settings_menu_btn.config(bg=BG_MENU, fg=FG_TEXT))
        settings_menu_btn.bind("<Leave>", lambda e: settings_menu_btn.config(bg=BG_HEADER, fg=FG_MUTED))

        actions_menu_btn = tk.Label(
            self.hdr_wide_menus, text="⚡ Thao tác ▾", bg=BG_HEADER, fg=FG_ACCENT,
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=1
        )
        actions_menu_btn.pack(side="right", padx=3)
        actions_menu_btn.bind("<Button-1>", self._show_ai_actions_menu)
        actions_menu_btn.bind("<Enter>", lambda e: actions_menu_btn.config(bg=FG_ACCENT, fg="#ffffff"))
        actions_menu_btn.bind("<Leave>", lambda e: actions_menu_btn.config(bg=BG_HEADER, fg=FG_ACCENT))

        # Compact header menu frame (single dropdown button for narrow header)
        self.hdr_compact_menu = tk.Frame(hdr_row, bg=BG_HEADER)

        compact_hdr_btn = tk.Label(
            self.hdr_compact_menu, text="⚙️ AI ▾", bg=BG_HEADER, fg=FG_ACCENT,
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=1
        )
        compact_hdr_btn.pack(side="right", padx=3)
        compact_hdr_btn.bind("<Button-1>", self._show_ai_combined_header_menu)
        compact_hdr_btn.bind("<Enter>", lambda e: compact_hdr_btn.config(bg=BG_MENU, fg=FG_TEXT))
        compact_hdr_btn.bind("<Leave>", lambda e: compact_hdr_btn.config(bg=BG_HEADER, fg=FG_ACCENT))

        # 2. Input Row (Entry + Send Button)
        input_row = tk.Frame(self.ai_footer_frame, bg=BG_HEADER)
        input_row.pack(fill="x", pady=(2, 4))

        self.ai_entry = tk.Entry(
            input_row, bg=BG, fg=FG_TEXT, insertbackground=FG_TEXT,
            relief="flat", font=("Segoe UI", 9), highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=FG_ACCENT
        )
        self.ai_entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 6))
        self.ai_entry.bind("<Return>", lambda e: self._send_ai_prompt())

        self.ai_send_btn = tk.Label(
            input_row, text="Gửi ➔", bg=FG_ACCENT, fg="#ffffff",
            font=("Segoe UI", 9, "bold"), cursor="hand2", padx=10, pady=3
        )
        self.ai_send_btn.pack(side="right")
        self.ai_send_btn.bind("<Button-1>", lambda e: self._send_ai_prompt())

        # 3. Actions Row (PLACED ABOVE Response Box!)
        self.ai_actions_frame = tk.Frame(self.ai_footer_frame, bg=BG_HEADER)

        # Wide view frame (horizontal action buttons)
        self.ai_actions_wide_frame = tk.Frame(self.ai_actions_frame, bg=BG_HEADER)
        self.ai_actions_wide_frame.pack(side="left")

        def _act_btn(parent_frame, text, command):
            btn = tk.Label(
                parent_frame, text=text, bg=BG_MENU, fg=FG_TEXT,
                font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=2
            )
            btn.pack(side="left", padx=2)
            btn.bind("<Button-1>", lambda e: command())
            btn.bind("<Enter>", lambda e: btn.config(bg=FG_ACCENT, fg="#ffffff"))
            btn.bind("<Leave>", lambda e: btn.config(bg=BG_MENU, fg=FG_TEXT))
            return btn

        self.ai_toggle_view_btn = tk.Label(
            self.ai_actions_wide_frame, text="📝 Xem Raw", bg=BG_MENU, fg=FG_TEXT,
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=2
        )
        self.ai_toggle_view_btn.pack(side="left", padx=2)
        self.ai_toggle_view_btn.bind("<Button-1>", lambda e: self._toggle_ai_view_mode())
        self.ai_toggle_view_btn.bind("<Enter>", lambda e: self.ai_toggle_view_btn.config(bg=FG_ACCENT, fg="#ffffff"))
        self.ai_toggle_view_btn.bind("<Leave>", lambda e: self._update_toggle_btn_style())

        self.ai_popup_btn = _act_btn(self.ai_actions_wide_frame, "👁️ Pop-up", self._ai_open_preview)
        self.ai_insert_btn = _act_btn(self.ai_actions_wide_frame, "📥 Chèn", self._ai_insert_at_cursor)
        self.ai_replace_btn = _act_btn(self.ai_actions_wide_frame, "🔄 Thay thế", self._ai_replace_content)
        self.ai_copy_btn = _act_btn(self.ai_actions_wide_frame, "📋 Sao chép", self._ai_copy_response)

        # Compact view frame (collapsed dropdown button for narrow screens)
        self.ai_actions_compact_frame = tk.Frame(self.ai_actions_frame, bg=BG_HEADER)

        compact_dropdown_btn = tk.Label(
            self.ai_actions_compact_frame, text="⚡ Thao tác kết quả ▾", bg=FG_ACCENT, fg="#ffffff",
            font=("Segoe UI", 8, "bold"), cursor="hand2", padx=8, pady=2
        )
        compact_dropdown_btn.pack(side="left")
        compact_dropdown_btn.bind("<Button-1>", self._show_ai_result_actions_menu)
        compact_dropdown_btn.bind("<Enter>", lambda e: compact_dropdown_btn.config(bg=BG_MENU, fg=FG_TEXT))
        compact_dropdown_btn.bind("<Leave>", lambda e: compact_dropdown_btn.config(bg=FG_ACCENT, fg="#ffffff"))

        # 4. Response display box (placed BELOW Actions Row!)
        self.ai_response_box = tk.Frame(self.ai_footer_frame, bg=BG)

        self.ai_res_text = tk.Text(
            self.ai_response_box, height=4, bg=BG, fg=FG_TEXT, wrap="word",
            font=("Segoe UI", 9), relief="flat", highlightthickness=0,
            selectbackground=SELECT_BG, selectforeground=FG_TEXT
        )
        self.ai_res_text.tag_configure("h1", font=("Segoe UI", 12, "bold"), foreground=FG_TITLE_TAG)
        self.ai_res_text.tag_configure("h2", font=("Segoe UI", 11, "bold"), foreground=FG_TITLE_TAG)
        self.ai_res_text.tag_configure("h3", font=("Segoe UI", 10, "bold"), foreground=FG_TITLE_TAG)
        self.ai_res_text.tag_configure("h4", font=("Segoe UI", 9, "bold"), foreground=FG_TITLE_TAG)
        self.ai_res_text.tag_configure("bold", font=("Segoe UI", 9, "bold"))
        self.ai_res_text.tag_configure("italic", font=("Segoe UI", 9, "italic"))
        self.ai_res_text.tag_configure("bolditalic", font=("Segoe UI", 9, "bold", "italic"))
        self.ai_res_text.tag_configure("code", font=("Cascadia Mono", 9), background=BG_MENU, foreground=FG_ACCENT)
        self.ai_res_text.tag_configure("codeblock", font=("Cascadia Mono", 9), background=BG_MENU, foreground=FG_TEXT)
        self.ai_res_text.tag_configure("bullet1", font=("Segoe UI", 9), foreground=FG_ACCENT)
        self.ai_res_text.tag_configure("bullet2", font=("Segoe UI", 9), foreground=FG_MUTED)
        self.ai_res_text.tag_configure("numbered", font=("Segoe UI", 9, "bold"), foreground=FG_ACCENT)
        self.ai_res_text.tag_configure("checkbox_off", font=("Segoe UI", 9), foreground=FG_MUTED)
        self.ai_res_text.tag_configure("checkbox_on", font=("Segoe UI", 9), foreground=FG_ACCENT)

        ai_res_scroll = ttk.Scrollbar(self.ai_response_box, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.ai_res_text.yview)
        self.ai_res_text.configure(yscrollcommand=ai_res_scroll.set)
        self.ai_res_text.pack(side="left", fill="x", expand=True)
        ai_res_scroll.pack(side="right", fill="y")

        # Responsive listener on ai_footer_frame width resize (Option B multi-tier breakpoints)
        def _on_footer_resize(event):
            w = event.width
            if w < 320:
                self.ai_title_lbl.config(text="✦ AI")
                self.hdr_wide_menus.pack_forget()
                self.hdr_compact_menu.pack(side="right")
                self.ai_actions_wide_frame.pack_forget()
                self.ai_actions_compact_frame.pack(side="left")
            elif w < 460:
                self.ai_title_lbl.config(text="✦ Trợ lý AI")
                self.hdr_compact_menu.pack_forget()
                self.hdr_wide_menus.pack(side="right")
                self.ai_actions_compact_frame.pack_forget()
                self.ai_actions_wide_frame.pack(side="left")
                self.ai_popup_btn.config(text="👁️ Pop-up")
                self.ai_insert_btn.config(text="📥 Chèn")
                self.ai_replace_btn.config(text="🔄 Thay thế")
                self.ai_copy_btn.config(text="📋 Copy")
            else:
                self.ai_title_lbl.config(text="✦ Trợ lý AI (Gemini)")
                self.hdr_compact_menu.pack_forget()
                self.hdr_wide_menus.pack(side="right")
                self.ai_actions_compact_frame.pack_forget()
                self.ai_actions_wide_frame.pack(side="left")
                self.ai_popup_btn.config(text="👁️ Pop-up")
                self.ai_insert_btn.config(text="📥 Chèn vào con trỏ")
                self.ai_replace_btn.config(text="🔄 Thay thế nội dung")
                self.ai_copy_btn.config(text="📋 Sao chép")

        self.ai_footer_frame.bind("<Configure>", _on_footer_resize)

    def _show_ai_actions_menu(self, event):
        items = [
            ("📝 Tóm tắt ghi chú", lambda: self._send_ai_prompt("Hãy tóm tắt ngắn gọn các ý chính của ghi chú này dưới dạng bullet-point Markdown.")),
            ("✨ Sửa & Chuẩn hóa MD", lambda: self._send_ai_prompt("Hãy sửa và chuẩn hóa toàn bộ định dạng Markdown (tiêu đề, danh sách, codeblock) cho ghi chú này.")),
            ("💡 Giải thích Code/Lệnh", lambda: self._send_ai_prompt("Hãy giải thích chi tiết các câu lệnh/code trong ghi chú này.")),
            ("✍️ Viết tiếp nội dung", lambda: self._send_ai_prompt("Hãy viết tiếp đoạn tiếp theo cho nội dung ghi chú này.")),
        ]
        menu = ContextMenu(self, items)
        menu.popup(event.x_root, event.y_root, direction="up")

    def _show_ai_combined_header_menu(self, event):
        current_model = store.get_selected_gemini_model()
        items = [
            ("📝 Tóm tắt ghi chú", lambda: self._send_ai_prompt("Hãy tóm tắt ngắn gọn các ý chính của ghi chú này dưới dạng bullet-point Markdown.")),
            ("✨ Sửa & Chuẩn hóa MD", lambda: self._send_ai_prompt("Hãy sửa và chuẩn hóa toàn bộ định dạng Markdown (tiêu đề, danh sách, codeblock) cho ghi chú này.")),
            ("💡 Giải thích Code/Lệnh", lambda: self._send_ai_prompt("Hãy giải thích chi tiết các câu lệnh/code trong ghi chú này.")),
            ("✍️ Viết tiếp nội dung", lambda: self._send_ai_prompt("Hãy viết tiếp đoạn tiếp theo cho nội dung ghi chú này.")),
            None,
            ("⚙️ Cấu hình Gemini API Key", self._prompt_gemini_key),
            (f"🤖 Chọn Model... [{current_model}]", lambda: self._show_model_select_menu(event)),
            ("🧹 Dọn sạch lịch sử chat", self._clear_ai_chat_history),
        ]
        menu = ContextMenu(self, items)
        menu.popup(event.x_root, event.y_root, direction="up")

    def _show_ai_result_actions_menu(self, event):
        mode = getattr(self, "ai_view_mode", "rendered")
        toggle_label = "📝 Xem mã Raw" if mode == "rendered" else "👁 Xem Rendered"

        items = [
            (toggle_label, self._toggle_ai_view_mode),
            ("👁 Pop-up Xem trước", self._ai_open_preview),
            ("📥 Chèn vào vị trí con trỏ", self._ai_insert_at_cursor),
            ("🔄 Thay thế nội dung note", self._ai_replace_content),
            ("📋 Sao chép câu trả lời", self._ai_copy_response),
        ]
        menu = ContextMenu(self, items)
        menu.popup(event.x_root, event.y_root, direction="up")

    def _show_toolbar_format_menu(self, event):
        items = [
            ("H1  Tiêu đề (Heading)", self._toggle_heading),
            ("❝  Trích dẫn (Blockquote)", self._toggle_blockquote),
            ("1.  Danh sách số", lambda: self._toggle_list("numbered")),
            ("—  Danh sách gạch đầu dòng", lambda: self._toggle_list("dash")),
            ("+  Danh sách dấu cộng", lambda: self._toggle_list("plus")),
            ("☑  Danh sách công việc", lambda: self._toggle_list("checkbox")),
            ("{ } Khối Code", self._toggle_codeblock),
            ("🔗  Chèn liên kết URL", self._insert_link),
        ]
        menu = ContextMenu(self, items)
        menu.popup(event.x_root, event.y_root, direction="down")

    def _show_ai_settings_menu(self, event):
        current_model = store.get_selected_gemini_model()
        items = [
            ("⚙️ Cấu hình Gemini API Key", self._prompt_gemini_key),
            (f"🤖 Chọn Model AI... [{current_model}]", lambda: self._show_model_select_menu(event)),
            ("🧹 Dọn sạch lịch sử chat", self._clear_ai_chat_history),
        ]
        menu = ContextMenu(self, items)
        menu.popup(event.x_root, event.y_root, direction="up")

    def _ai_open_preview(self):
        res = self.ai_res_text.get("1.0", "end-1c").strip()
        if res and "⏳ Trợ lý AI đang suy nghĩ" not in res:
            AIPreviewDialog(
                self,
                res,
                on_insert=self._ai_insert_at_cursor,
                on_replace=self._ai_replace_content
            )
        else:
            messagebox.showinfo("KQ AI Assistant", "Chưa có kết quả AI để xem trước!", parent=self)

    def _show_model_select_menu(self, event):
        key = store.get_gemini_api_key()
        dynamic_models = ai_helper.get_available_models(key)

        default_list = [
            "gemini-1.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-pro",
            "gemini-2.5-pro",
            "gemini-2.0-flash-lite",
        ]

        models_list = []
        for m in dynamic_models + default_list:
            if m not in models_list:
                models_list.append(m)

        menu_items = []
        curr = store.get_selected_gemini_model()
        for m in models_list:
            prefix = "✓ " if m == curr else "   "

            def _make_cmd(selected_model):
                def _cb():
                    store.set_selected_gemini_model(selected_model)
                return _cb

            menu_items.append((f"{prefix}{m}", _make_cmd(m)))

        menu = ContextMenu(self, menu_items)
        menu.popup(event.x_root, event.y_root, direction="up")

    def _clear_ai_chat_history(self):
        self.ai_chat_history = []
        messagebox.showinfo("KQ AI Assistant", "Đã xóa lịch sử hội thoại phiên này!", parent=self)

    def _prompt_gemini_key(self):
        current = store.get_gemini_api_key()
        key = simpledialog.askstring(
            "Cấu hình Gemini API Key",
            "Nhập Gemini API Key từ Google AI Studio (aistudio.google.com):",
            initialvalue=current,
            parent=self
        )
        if key is not None:
            store.set_gemini_api_key(key.strip())
            messagebox.showinfo("KQ AI Assistant", "Đã lưu Gemini API Key thành công!", parent=self)

    def _toggle_ai_footer(self):
        if self.ai_footer_visible:
            self.ai_footer_frame.pack_forget()
            self.ai_footer_visible = False
            self.ai_btn.config(bg=BG, fg=FG_MUTED)
        else:
            self.ai_footer_frame.pack(fill="x", side="bottom", padx=10, pady=(0, 8))
            self.ai_footer_visible = True
            self.ai_btn.config(bg=BG_MENU, fg=FG_ACCENT)
            self.ai_entry.focus_set()

    def _update_ai_response_display(self):
        raw = getattr(self, "ai_raw_response", "")
        mode = getattr(self, "ai_view_mode", "rendered")
        self.ai_res_text.delete("1.0", "end")

        if mode == "rendered":
            markup.render_into_text(self.ai_res_text, raw)
        else:
            self.ai_res_text.insert("end", raw)
        self._update_toggle_btn_style()

    def _update_toggle_btn_style(self):
        if not hasattr(self, "ai_toggle_view_btn"):
            return
        mode = getattr(self, "ai_view_mode", "rendered")
        if mode == "rendered":
            self.ai_toggle_view_btn.config(text="📝 Xem Raw", bg=BG_MENU, fg=FG_TEXT)
        else:
            self.ai_toggle_view_btn.config(text="👁️ Render", bg=FG_ACCENT, fg="#ffffff")

    def _toggle_ai_view_mode(self):
        curr = getattr(self, "ai_view_mode", "rendered")
        self.ai_view_mode = "raw" if curr == "rendered" else "rendered"
        self._update_ai_response_display()

    def _send_ai_prompt(self, custom_prompt=None):
        prompt = custom_prompt or self.ai_entry.get().strip()
        if not prompt:
            return

        if not custom_prompt:
            self.ai_entry.delete(0, "end")

        self.ai_actions_frame.pack_forget()
        self.ai_response_box.pack(fill="x", pady=(2, 4))
        self.ai_res_text.delete("1.0", "end")
        self.ai_res_text.insert("end", "⏳ Trợ lý AI đang suy nghĩ và xử lý...")

        context = self.text.get("1.0", "end-1c")
        api_key = store.get_gemini_api_key()

        def _on_response(success, result_text, model_used):
            def _ui_update():
                self.ai_raw_response = result_text
                self.ai_view_mode = "rendered"
                self._update_ai_response_display()
                if success:
                    self.ai_actions_frame.pack(fill="x", pady=(2, 4), before=self.ai_response_box)
                    try:
                        win_h = self.winfo_height()
                        flex_h = max(3, min(10, win_h // 70))
                        self.ai_res_text.configure(height=flex_h)
                    except Exception:
                        pass
                    if model_used:
                        self.ai_model_lbl.config(text=f"[{model_used}]")
                    self.ai_chat_history.append({"role": "user", "text": prompt})
                    self.ai_chat_history.append({"role": "model", "text": result_text})
                else:
                    self.ai_model_lbl.config(text="")
            self.after(0, _ui_update)

        preferred_model = store.get_selected_gemini_model()
        ai_helper.ask_gemini_async(
            prompt,
            context=context,
            callback=_on_response,
            api_key=api_key,
            preferred_model=preferred_model,
            chat_history=getattr(self, "ai_chat_history", []),
        )

    def _ai_insert_at_cursor(self):
        res = getattr(self, "ai_raw_response", "").strip() or self.ai_res_text.get("1.0", "end-1c").strip()
        if res and "⏳ Trợ lý AI đang suy nghĩ" not in res:
            markup.insert_markdown_at_cursor(self.text, "\n\n" + res, on_image=self._on_image_marker)
            self._on_text_changed()

    def _ai_replace_content(self):
        res = getattr(self, "ai_raw_response", "").strip() or self.ai_res_text.get("1.0", "end-1c").strip()
        if res and "⏳ Trợ lý AI đang suy nghĩ" not in res:
            self._load_content_into_editor(res)
            self._on_text_changed()

    def _ai_copy_response(self):
        res = getattr(self, "ai_raw_response", "").strip() or self.ai_res_text.get("1.0", "end-1c").strip()
        if res and "⏳ Trợ lý AI đang suy nghĩ" not in res:
            self.clipboard_clear()
            self.clipboard_append(res)
            messagebox.showinfo("KQ AI Assistant", "Đã sao chép câu trả lời vào bộ nhớ tạm!", parent=self)

    def _build_list_view(self, parent):
        top_bar = tk.Frame(parent, bg=BG, padx=10, pady=8)
        top_bar.pack(fill="x")

        search_wrap = tk.Frame(top_bar, bg=BG)
        search_wrap.pack(side="left", fill="x", expand=True)

        search_box = tk.Frame(search_wrap, bg=BG)
        search_box.pack(fill="x")

        tk.Label(search_box, text="\U0001F50D", bg=BG, fg=FG_MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(2, 6))
        self.notes_search_var = tk.StringVar()
        self.notes_search_entry = tk.Entry(
            search_box, textvariable=self.notes_search_var, bg=BG, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief="flat", font=("Segoe UI", 10),
            highlightthickness=0, selectbackground=SELECT_BG, selectforeground=FG_TEXT,
        )
        self.notes_search_entry.pack(side="left", fill="x", expand=True, ipady=5)
        self.notes_search_entry.bind("<KeyRelease>", lambda e: self._render_notes_list())

        search_underline = tk.Frame(search_wrap, bg=BORDER, height=1)
        search_underline.pack(fill="x", pady=(2, 0))

        add_btn = tk.Label(
            top_bar, text=" + ", bg=FG_ACCENT, fg="#ffffff",
            font=("Segoe UI", 11, "bold"), padx=8, pady=2, cursor="hand2"
        )
        add_btn.pack(side="right", padx=(8, 0))
        add_btn.bind("<Button-1>", lambda e: self.create_new_note())
        add_btn.bind("<Enter>", lambda e: add_btn.config(bg="#1c92d2"))
        add_btn.bind("<Leave>", lambda e: add_btn.config(bg=FG_ACCENT))

        # Scrollable note list
        container = tk.Frame(parent, bg=BG)
        container.pack(fill="both", expand=True, padx=10, pady=(4, 8))

        self.notes_canvas = tk.Canvas(container, bg=BG, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", style="Dark.Vertical.TScrollbar", command=self.notes_canvas.yview)
        self.notes_scroll_frame = tk.Frame(self.notes_canvas, bg=BG)

        self.notes_scroll_frame.bind(
            "<Configure>",
            lambda e: self.notes_canvas.configure(scrollregion=self.notes_canvas.bbox("all"))
        )

        canvas_win = self.notes_canvas.create_window((0, 0), window=self.notes_scroll_frame, anchor="nw")
        self.notes_canvas.configure(yscrollcommand=scrollbar.set)

        def _on_canvas_resize(event):
            self.notes_canvas.itemconfig(canvas_win, width=event.width)

        self.notes_canvas.bind("<Configure>", _on_canvas_resize)

        self.notes_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_notes_mw(e):
            if hasattr(e, "delta") and e.delta:
                lines = -1 * int(e.delta / 40)
                if lines == 0:
                    lines = -1 if e.delta > 0 else 1
                self.notes_canvas.yview_scroll(lines, "units")
            elif getattr(e, "num", None) == 4:
                self.notes_canvas.yview_scroll(-3, "units")
            elif getattr(e, "num", None) == 5:
                self.notes_canvas.yview_scroll(3, "units")

        self._on_notes_mw_func = _on_notes_mw

        def _bind_mw_recursive(widget):
            if not widget:
                return
            widget.bind("<MouseWheel>", _on_notes_mw, add="+")
            widget.bind("<Button-4>", _on_notes_mw, add="+")
            widget.bind("<Button-5>", _on_notes_mw, add="+")
            for child in widget.winfo_children():
                _bind_mw_recursive(child)

        self._bind_notes_mw_recursive = _bind_mw_recursive

        _bind_mw_recursive(container)
        _bind_mw_recursive(self.notes_canvas)
        _bind_mw_recursive(self.notes_scroll_frame)

    # ---------- Navigation & Note Management ----------
    def _toggle_view_mode(self):
        if self.detail_view_frame.winfo_viewable():
            self.show_list_view()

    def show_list_view(self):
        self.flush_save()
        self.detail_view_frame.pack_forget()
        self.list_view_frame.pack(fill="both", expand=True)
        self.list_nav_btn.config(text="", cursor="arrow")
        self._render_notes_list()

    def show_detail_view(self, note_id=None):
        if note_id:
            if note_id != self.active_note_id:
                self.flush_save()
            self.active_note_id = note_id
            store.set_active_note_id(note_id)
            content = store.load_note_by_id(note_id)
            self._load_content_into_editor(content)

        self.list_view_frame.pack_forget()
        self.detail_view_frame.pack(fill="both", expand=True)
        self.list_nav_btn.config(text="☰", fg=FG_TEXT)

    def switch_to_note(self, note_id):
        self.show_detail_view(note_id)

    def create_new_note(self):
        self.flush_save()
        new_id = store.create_note("# Ghi chú mới\n\nNội dung ghi chú...")
        self.active_note_id = new_id
        store.set_active_note_id(new_id)
        content = store.load_note_by_id(new_id)
        self._load_content_into_editor(content)
        self.show_detail_view()
        self.text.focus_force()

    def _confirm_delete_note(self, note_id, title):
        if messagebox.askyesno(
            "Chuyển vào thùng rác",
            f"Chuyển ghi chú vào thùng rác:\n\"{title}\"?\n"
            f"Bạn có thể khôi phục trong {store.TRASH_RETENTION_DAYS} ngày.",
            parent=self,
        ):
            next_id = store.delete_note_by_id(note_id)
            if self.active_note_id == note_id:
                self.active_note_id = next_id
                store.set_active_note_id(next_id)
                content = store.load_note_by_id(next_id)
                self._load_content_into_editor(content)
            self._render_notes_list()
            self._sync_soon()

    def _bind_card_drag(self, widget, card_info):
        if not widget:
            return

        def _on_drag_start(event):
            self._drag_data = {
                "card_info": card_info,
                "start_y": event.y_root,
                "moved": False,
            }
            card_info["card"].config(highlightbackground=FG_ACCENT, highlightthickness=2)

        def _on_drag_motion(event):
            if not hasattr(self, "_drag_data") or not self._drag_data:
                return
            if abs(event.y_root - self._drag_data["start_y"]) > 6:
                self._drag_data["moved"] = True

            if not self._drag_data["moved"]:
                return

            pointer_y = self.notes_scroll_frame.winfo_pointery() - self.notes_scroll_frame.winfo_rooty()

            target_idx = None
            for idx, item in enumerate(self._rendered_cards):
                c_widget = item["card"]
                c_top = c_widget.winfo_y()
                c_height = c_widget.winfo_height()
                if c_top <= pointer_y <= c_top + c_height:
                    target_idx = idx
                    break

            if target_idx is not None and card_info in self._rendered_cards:
                curr_idx = self._rendered_cards.index(card_info)
                if target_idx != curr_idx:
                    item = self._rendered_cards.pop(curr_idx)
                    self._rendered_cards.insert(target_idx, item)

                    for card_item in self._rendered_cards:
                        card_item["card"].pack_forget()
                        card_item["card"].pack(fill="x", pady=4, padx=2)

        def _on_drag_end(event):
            if hasattr(self, "_drag_data") and self._drag_data:
                is_active = (card_info["id"] == self.active_note_id)
                card_info["card"].config(
                    highlightbackground=FG_ACCENT if is_active else BORDER,
                    highlightthickness=1
                )
                if self._drag_data.get("moved"):
                    ordered_ids = [item["id"] for item in self._rendered_cards]
                    store.reorder_notes(ordered_ids)
                    self._sync_soon()
                self._drag_data = None

        widget.bind("<ButtonPress-1>", _on_drag_start, add="+")
        widget.bind("<B1-Motion>", _on_drag_motion, add="+")
        widget.bind("<ButtonRelease-1>", _on_drag_end, add="+")

    def _render_notes_list(self):
        for widget in self.notes_scroll_frame.winfo_children():
            widget.destroy()

        notes = store.list_notes()
        query = self.notes_search_var.get().strip().lower() if hasattr(self, 'notes_search_var') else ""

        filtered = []
        for n in notes:
            if not query or query in n.get("title", "").lower() or query in n.get("snippet", "").lower():
                filtered.append(n)

        if not filtered:
            lbl = tk.Label(
                self.notes_scroll_frame,
                text="Không tìm thấy ghi chú nào" if query else "Chưa có ghi chú nào.\nBấm + ở trên để tạo mới.",
                bg=BG, fg=FG_MUTED, font=("Segoe UI", 10), pady=30
            )
            lbl.pack(fill="x")
            return

        self._rendered_cards = []

        for n in filtered:
            nid = n["id"]
            is_active = (nid == self.active_note_id)

            card = tk.Frame(
                self.notes_scroll_frame,
                bg=BG_HEADER if is_active else BG_MENU,
                padx=8, pady=8, cursor="hand2",
                highlightbackground=FG_ACCENT if is_active else BORDER,
                highlightthickness=1
            )
            card.pack(fill="x", pady=4, padx=2)

            top_row = tk.Frame(card, bg=card.cget("bg"))
            top_row.pack(fill="x")

            drag_grip = tk.Label(
                top_row, text="⋮⋮", bg=card.cget("bg"), fg=FG_MUTED,
                font=("Segoe UI", 10), padx=4, cursor="fleur"
            )
            drag_grip.pack(side="left")

            title_text = n.get("title") or "Ghi chú không tiêu đề"
            lbl_title = tk.Label(
                top_row, text=title_text, bg=card.cget("bg"),
                fg=FG_TITLE_TAG if is_active else FG_TEXT,
                font=("Segoe UI", 10, "bold"), anchor="w"
            )
            lbl_title.pack(side="left", fill="x", expand=True, padx=(2, 0))

            del_btn = tk.Label(
                top_row, text="🗑", bg=card.cget("bg"), fg=FG_MUTED,
                font=("Segoe UI", 9), padx=4, cursor="hand2"
            )
            del_btn.pack(side="right")
            del_btn.bind("<Button-1>", lambda e, note_id=nid, t=title_text: self._confirm_delete_note(note_id, t))

            snippet_text = n.get("snippet", "")
            lbl_snip = None
            if snippet_text:
                lbl_snip = tk.Label(
                    card, text=snippet_text, bg=card.cget("bg"), fg=FG_MUTED,
                    font=("Segoe UI", 9), anchor="w", justify="left", wraplength=280
                )
                lbl_snip.pack(fill="x", pady=(2, 4), padx=(20, 0))

            updated_ts = n.get("updated_at", 0)
            dt_str = datetime.datetime.fromtimestamp(updated_ts).strftime("%d/%m/%Y %H:%M") if updated_ts else ""
            lbl_date = tk.Label(
                card, text=dt_str, bg=card.cget("bg"), fg=FG_MUTED,
                font=("Segoe UI", 8), anchor="w"
            )
            lbl_date.pack(fill="x", padx=(20, 0))

            card_info = {"id": nid, "card": card, "note": n}
            self._rendered_cards.append(card_info)

            def _bind_click(w, target_id=nid):
                if w:
                    w.bind("<Button-1>", lambda e: self.switch_to_note(target_id))

            for w in (top_row, lbl_title, lbl_snip, lbl_date):
                _bind_click(w)

            self._bind_card_drag(drag_grip, card_info)
            self._bind_card_drag(card, card_info)

            if hasattr(self, "_bind_notes_mw_recursive"):
                self._bind_notes_mw_recursive(card)

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
        radius = 0 if self._docked_side is not None else WINDOW_RADIUS
        round_window(self, radius)

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
        if not hasattr(self, "text"):
            return
        content = markup.serialize_from_text(self.text)
        if content == self._editor_baseline:
            # Nothing typed since the editor was loaded or last saved. (KeyRelease also fires for
            # arrow keys etc., and the stored text may have been updated by a sync meanwhile:
            # writing the editor's older text back would undo that.)
            return
        active_id = getattr(self, "active_note_id", None) or store.get_active_note_id()
        outcome = None
        if active_id:
            outcome = store.save_note_by_id(active_id, content, expected_old=self._db_content)
        else:
            store.save_content(content)
        if outcome is not None and outcome.copy_id:
            self._on_edit_conflicted(outcome.copy_id)
            return
        if outcome is not None and outcome.merged:
            # The stored text had moved on, but the changes were in different places and were
            # combined: show the merged text (cursor stays put) and carry on.
            self._reload_active_note()
            self._sync_soon()
            return
        self._db_content = content
        self._editor_baseline = content
        self._sync_soon()

    def _on_edit_conflicted(self, copy_id):
        """The stored text changed under the editor (a sync applied another device's version):
        the store kept what was typed as a new note instead of overwriting."""
        self._reload_active_note()
        if self.list_view_frame.winfo_viewable():
            self._render_notes_list()
        messagebox.showinfo(
            "Ghi chú vừa được cập nhật từ thiết bị khác",
            "Ghi chú này vừa thay đổi ở nơi khác trong lúc bạn đang gõ.\n\n"
            "Phần bạn vừa gõ không bị mất: nó được lưu thành một ghi chú mới có tiêu đề bắt đầu bằng "
            "[Xung đột]. Hãy gộp phần cần giữ vào ghi chú gốc rồi xoá bản xung đột.",
            parent=self,
        )
        self._sync_soon()

    def _load_content_into_editor(self, content):
        self._photo_refs.clear()
        self._pending_images = set()
        markup.render_into_text(self.text, content, on_image=self._on_image_marker,
                                 on_hr=self._on_hr_marker, on_codeblock=self._on_codeblock_end)
        for tagname in self.text._kq_links:
            self._bind_link_tag(tagname)
        self._highlight_urls()
        if self._pending_images:
            self.after(IMAGE_CHECK_INTERVAL_MS, self._check_lazy_images)
        self._db_content = content
        self._editor_baseline = markup.serialize_from_text(self.text)

    def _editor_is_clean(self):
        return markup.serialize_from_text(self.text) == self._editor_baseline

    def _reload_active_note(self):
        """Show the stored text of the active note again, keeping the cursor and scroll position."""
        # Stay on the note that is open if it still exists; only then fall back to another one.
        # (Recomputing "the active note" would pick whatever is first in the list, and a conflict
        # copy has just been inserted there.)
        note_id = self.active_note_id
        if note_id not in {n["id"] for n in store.list_notes()}:
            note_id = store.get_active_note_id() or store.ensure_note()
        store.set_active_note_id(note_id)
        self.active_note_id = note_id
        try:
            cursor, top = self.text.index("insert"), self.text.yview()[0]
        except tk.TclError:
            cursor, top = None, None
        self._load_content_into_editor(store.load_note_by_id(note_id))
        try:
            if cursor:
                self.text.mark_set("insert", cursor)
            if top is not None:
                self.text.yview_moveto(top)
        except tk.TclError:
            pass

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

    def _show_header_dropdown_menu(self, event=None):
        items = [
            ("📌  Bỏ ghim cửa sổ" if self._always_on_top else "📌  Ghim trên cùng", self.toggle_always_on_top),
        ]

        if hasattr(self, 'detail_view_frame') and self.detail_view_frame.winfo_viewable():
            items.append(("📷  Chụp màn hình", self._start_screenshot))

        items.append(("🗑️  Thùng rác", self._open_trash))
        items.append(None)  # Separator

        if self.sync_engine.is_logged_in():
            email = self.sync_engine.account_email() or "Tài khoản"
            items.append((f"👤  {email}", None))
            items.append((self._sync_status_text(), None))
            items.append(("🔄  Đồng bộ ngầm ngay", self._sync_now))
            items.append(("🚪  Đăng xuất khỏi Cloud", self._logout))
        else:
            items.append(("☁️  Đăng nhập Cloud (Google)", self.sync_engine.login_with_google_async))

        menu = ContextMenu(self, items)
        x = self.menu_more_btn.winfo_rootx()
        y = self.menu_more_btn.winfo_rooty() + self.menu_more_btn.winfo_height() + 2
        menu.popup(x, y)

    def _open_trash(self):
        TrashDialog(self, on_change=self._on_trash_changed)

    def _on_trash_changed(self):
        # Restoring puts a note back in the list; refresh it if the list is what's showing.
        if self.list_view_frame.winfo_viewable():
            self._render_notes_list()
        self._sync_soon()

    _SYNC_COLORS = {"synced": "#6fd0a0", "syncing": FG_ACCENT, "offline": "#d9a441", "error": "#e5706b"}

    def _sync_status_text(self):
        st = self.sync_engine.status()
        pending = st.get("pending") or 0
        tail = f" · còn {pending} thay đổi chưa gửi" if pending else ""
        if st["state"] == "syncing":
            return "⏳  Đang đồng bộ…"
        if st["state"] == "offline":
            return "📴  Ngoại tuyến, sẽ đồng bộ khi có mạng" + tail
        if st["state"] == "error":
            return f"⚠️  Lỗi đồng bộ: {st['message']}"[:90] + tail
        if st.get("last_ok"):
            return "✅  Đã đồng bộ lúc " + datetime.datetime.fromtimestamp(st["last_ok"]).strftime("%H:%M") + tail
        return "☁️  Chưa đồng bộ"

    def _update_cloud_icon(self):
        if not hasattr(self, "sync_status_lbl"):
            return
        if not self.sync_engine.is_logged_in():
            self.sync_status_lbl.config(text="", padx=0)
            return
        st = self.sync_engine.status()
        state = st["state"]
        if state == "synced" and st.get("pending"):
            state = "offline"  # something is still waiting to go up
        self.sync_status_lbl.config(text="☁", padx=6, fg=self._SYNC_COLORS.get(state, FG_MUTED))

    def _on_cloud_click(self, event):
        if not self.sync_engine.is_logged_in():
            self.sync_engine.login_with_google_async()
            return

        email = self.sync_engine.account_email() or "?"
        menu = ContextMenu(self, [
            (f"Đã đăng nhập: {email}", None),
            (self._sync_status_text(), None),
            ("Đồng bộ ngay", self._sync_now),
            None,
            ("Đăng xuất", self._logout),
        ])
        menu.popup(event.x_root, event.y_root)

    # -- when to sync: after local changes (debounced), on a timer, and on demand
    def _sync_soon(self, delay_ms=2000):
        if not self.sync_engine.is_logged_in():
            return
        if self._sync_after_id is not None:
            self.after_cancel(self._sync_after_id)
        self._sync_after_id = self.after(delay_ms, self._run_sync)

    def _run_sync(self):
        self._sync_after_id = None
        self.sync_engine.sync_async()

    def _schedule_poll(self):
        if self._poll_after_id is not None:
            self.after_cancel(self._poll_after_id)
        self._poll_after_id = self.after(SYNC_POLL_INTERVAL_MS, self._poll_sync)

    def _poll_sync(self):
        self._poll_after_id = None
        if not self.sync_engine.is_logged_in():
            return
        self.sync_engine.sync_async()
        self._schedule_poll()

    def _sync_now(self):
        self.flush_save()
        self.sync_engine.sync_async()

    # -- account
    def _on_login_success(self):
        self.flush_save()  # persist anything typed into the local note before the account takes over
        self._update_cloud_icon()
        self.sync_engine.sync_async()  # the account's notes are shown once its first sync has pulled them
        self._schedule_poll()

    def _on_account_ready(self, account):
        """The first pull of this session finished and the local notes were copied into the account."""
        if store.get_scope() == account:
            return  # already showing it (app restarted while signed in)
        self.flush_save()
        store.set_scope(account)
        store.ensure_note()
        self._reload_active_note()
        if self.list_view_frame.winfo_viewable():
            self._render_notes_list()
        self._update_cloud_icon()

    def _logout(self):
        self.flush_save()
        if self._sync_after_id is not None:
            self.after_cancel(self._sync_after_id)
            self._sync_after_id = None
        self.sync_engine.logout()
        # The account's notes stay on disk (hidden) so signing back in is instant;
        # what shows now is the local-only list, untouched by anything the account did.
        store.set_scope(None)
        self._reload_active_note()
        if self.list_view_frame.winfo_viewable():
            self._render_notes_list()
        self._update_cloud_icon()

    # -- reacting to what the sync engine changed in the database
    def _on_notes_changed(self, payload):
        changed = set(payload.get("changed", []))
        if self.list_view_frame.winfo_viewable():
            self._render_notes_list()

        active = self.active_note_id
        if active not in {n["id"] for n in store.list_notes()}:
            # The open note was deleted (or replaced) from another device. Keep whatever was being
            # typed (it lands in that note, which is in the trash), then show a note that still exists.
            self.flush_save()
            self.active_note_id = store.get_active_note_id() or store.ensure_note()
            self._reload_active_note()
            if self.detail_view_frame.winfo_viewable():
                messagebox.showinfo(
                    "Ghi chú đã thay đổi",
                    "Ghi chú đang mở vừa bị xoá hoặc thay thế từ một thiết bị khác.\n"
                    "Nếu bạn vừa gõ dở, phần đó vẫn nằm trong Thùng rác.",
                    parent=self,
                )
        elif active in changed and self._editor_is_clean():
            self._reload_active_note()  # nothing typed here: just show the newer text
        # If the user is mid-typing on the changed note, do nothing now: the next autosave
        # notices the stored text moved and keeps the typing as a conflict copy.

        conflicts = payload.get("conflicts") or []
        if conflicts:
            titles = "\n".join(f"• {c['title']}" for c in conflicts[:5])
            messagebox.showinfo(
                "Ghi chú bị sửa ở hai nơi cùng lúc",
                f"{len(conflicts)} ghi chú được sửa ở nhiều thiết bị cùng lúc:\n{titles}\n\n"
                "Phần bạn sửa ở máy này được giữ thành ghi chú mới có tiêu đề bắt đầu bằng [Xung đột]. "
                "Hãy gộp phần cần giữ vào ghi chú gốc rồi xoá bản xung đột.",
                parent=self,
            )

    def _drain_sync_events(self):
        while True:
            try:
                kind, payload = self.sync_engine.events.get_nowait()
            except queue.Empty:
                break
            if kind == "notes_changed":
                self._on_notes_changed(payload)
            elif kind == "account_ready":
                self._on_account_ready(payload)
            elif kind == "sync_status":
                self._update_cloud_icon()
            elif kind == "google_login_success":
                self._on_login_success()
            elif kind == "google_login_error":
                self._update_cloud_icon()
                messagebox.showerror(
                    "Đăng nhập Cloud thất bại",
                    payload or "Không rõ nguyên nhân. Vui lòng thử lại.",
                    parent=self,
                )
            elif kind == "auth_required":
                # Refresh also failed — the session is unrecoverable, so fall
                # back to a clean logged-out state instead of a stuck icon.
                self._logout()
                messagebox.showinfo("Phiên đăng nhập đã hết hạn",
                                    "Vui lòng đăng nhập lại để tiếp tục đồng bộ. Ghi chú của bạn vẫn được giữ.",
                                    parent=self)
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
        if turning_on:
            code_text = "\n".join(
                self.text.get(f"{ln}.0", f"{ln}.end") for ln in range(start_line, end_line + 1)
            )
            self._on_codeblock_end(code_text, at_index=f"{end_line}.end")
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
        format_items = [
            ("In đậm", lambda: self._toggle_inline("bold")),
            ("In nghiêng", lambda: self._toggle_inline("italic")),
            ("Code", lambda: self._toggle_inline("code")),
        ]
        block_items = [
            ("Tiêu đề (H1)", self._toggle_heading),
            ("Trích dẫn", self._toggle_blockquote),
            ("Khối code", self._toggle_codeblock),
            ("Chèn liên kết", self._insert_link),
        ]
        list_items = [
            ("Danh sách số", lambda: self._toggle_list("numbered")),
            ("Gạch đầu dòng —", lambda: self._toggle_list("dash")),
            ("Gạch đầu dòng +", lambda: self._toggle_list("plus")),
            ("Checkbox", lambda: self._toggle_list("checkbox")),
        ]

        menu = ContextMenu(self, [
            ("Cắt", self._cut_text),
            ("Sao chép", lambda: self.text.event_generate("<<Copy>>")),
            ("Dán", self._paste_text),
            None,
            ("Chọn tất cả", self._select_all_text),
            None,
            ("Định dạng chữ", format_items),
            ("Khối nội dung", block_items),
            ("Danh sách", list_items),
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
                                          on_hr=self._on_hr_marker, on_codeblock=self._on_codeblock_end)
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
        self.update_idletasks()  # make sure the hide is actually applied first
        self.after(150, self._open_screenshot_overlay)

    def _open_screenshot_overlay(self):
        rect = winfx.get_virtual_screen_rect()
        if rect is not None:
            vx, vy, vw, vh = rect
        else:
            vx, vy = 0, 0
            vw, vh = self.winfo_screenwidth(), self.winfo_screenheight()

        overlay = tk.Toplevel(self)
        overlay.overrideredirect(True)
        overlay.attributes("-topmost", True)
        overlay.attributes("-alpha", 0.35)
        # Covers every monitor (not just the primary one winfo_screenwidth()/
        # height() would report), so dragging a selection works consistently
        # regardless of which monitor the note/cursor happens to be on.
        overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
        overlay.configure(bg="#000000")

        canvas = tk.Canvas(overlay, bg="#000000", highlightthickness=0, cursor="crosshair")
        canvas.pack(fill="both", expand=True)

        drag = {"start_root": None, "start_local": None, "rect": None}

        def on_press(event):
            drag["start_root"] = (event.x_root, event.y_root)
            drag["start_local"] = (event.x, event.y)
            drag["rect"] = canvas.create_rectangle(
                event.x, event.y, event.x, event.y, outline=FG_ACCENT, width=2)

        def on_drag(event):
            if drag["rect"] is None:
                return
            x0, y0 = drag["start_local"]
            # event.x/y are canvas-local; using event.x_root/y_root here would
            # be offset by the overlay's own (vx, vy) origin and draw the
            # selection box in the wrong place whenever that isn't (0, 0).
            canvas.coords(drag["rect"], x0, y0, event.x, event.y)

        def on_release(event):
            overlay.grab_release()
            overlay.destroy()
            if drag["start_root"] is None:
                self._cancel_screenshot()
                return
            x0, y0 = drag["start_root"]
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
            overlay.grab_release()
            overlay.destroy()
            self._cancel_screenshot()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        overlay.bind("<Escape>", on_cancel)
        overlay.update_idletasks()
        overlay.deiconify()
        overlay.lift()
        overlay.focus_force()
        # A local grab makes sure every mouse/keyboard event this app sees
        # goes to the overlay specifically — without it, drag-selection was
        # sometimes flaky depending on which window Windows considered
        # "focused" right after the popup menu that triggered this closed.
        overlay.grab_set()

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

    def _on_codeblock_end(self, code_text, at_index=None):
        # Embeds inline right after the block's last code line (same line, to
        # its right) rather than on a line of its own below the block.
        if at_index is not None:
            self.text.mark_set("insert", at_index)
            target = "insert"
        else:
            target = "end"
        # No literal spacer text before the button — its own padx below gives
        # the visual gap, and keeps the button's index exactly at the code
        # content's true end so serialization can truncate there cleanly.
        btn = tk.Label(self.text, text="📋 Copy", bg=BG_MENU, fg=FG_MUTED,
                        font=("Segoe UI", 8), padx=6, pady=1, cursor="hand2")
        btn._kq_is_codecopy = True
        self.text.window_create(target, window=btn)
        btn.bind("<Button-1>", lambda e, code=code_text, b=btn: self._copy_codeblock(code, b))

    def _copy_codeblock(self, code_text, btn):
        self.clipboard_clear()
        self.clipboard_append(code_text)
        btn.config(text="✓ Copied", fg=FG_ACCENT)
        self.after(1200, lambda: btn.winfo_exists() and btn.config(text="📋 Copy", fg=FG_MUTED))

    def _bind_image_click(self, name, file_id):
        idx = self.text.index(name)
        tag = f"imgtag_{file_id}"
        self.text.tag_add(tag, idx, f"{idx}+1c")
        self.text.tag_bind(tag, "<Button-1>", lambda e, fid=file_id: self._show_image_preview(fid))
        self.text.tag_bind(tag, "<Button-3>", lambda e, fid=file_id: self._show_image_menu(e, fid))
        self.text.tag_bind(tag, "<Enter>", lambda e: self.text.config(cursor="hand2"))
        self.text.tag_bind(tag, "<Leave>", lambda e: self.text.config(cursor="xterm"))

    def _show_image_menu(self, event, file_id):
        menu = ContextMenu(self, [
            ("Sao chép ảnh", lambda: self._copy_image_to_clipboard(file_id)),
        ])
        menu.popup(event.x_root, event.y_root)
        return "break"

    def _copy_image_to_clipboard(self, file_id):
        path = os.path.join(store.get_images_dir(), f"{file_id}.png")
        try:
            import win32clipboard

            img = Image.open(path).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "BMP")
            # CF_DIB wants the raw DIB data only, without BMP's 14-byte file header.
            data = buf.getvalue()[14:]
            buf.close()

            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
            win32clipboard.CloseClipboard()
        except Exception:
            pass  # clipboard access can fail transiently (e.g. another app holding it)

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

        copy_btn = tk.Label(preview, text="📋 Copy", bg=BG_MENU, fg=FG_TEXT,
                             font=("Segoe UI", 9), padx=8, pady=4, cursor="hand2")
        copy_btn.place(relx=1.0, rely=0.0, anchor="ne", x=-8, y=8)

        def do_copy(_event=None):
            self._copy_image_to_clipboard(file_id)
            copy_btn.config(text="✓ Copied")
            self.after(1000, lambda: copy_btn.winfo_exists() and copy_btn.config(text="📋 Copy"))
            return "break"  # stop this click from also bubbling up to the
            # preview/lbl bindings below, which close the whole preview.

        copy_btn.bind("<Button-1>", do_copy)

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
        if hasattr(self, 'pin_btn'):
            self.pin_btn.config(fg=FG_ACCENT)
        self.search_entry.focus_force()

    def toggle_always_on_top(self):
        self._always_on_top = not self._always_on_top
        self.attributes("-topmost", self._always_on_top)
        if hasattr(self, 'pin_btn'):
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
