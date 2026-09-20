import datetime
import math
import time
import tkinter as tk
import tkinter.messagebox as messagebox

from app import store
from app.theme import BG, BG_HEADER, BG_MENU, BORDER, FG_ACCENT, FG_DANGER, FG_MUTED, FG_TEXT


def _days_left(deleted_at, now=None):
    expires = deleted_at + store.TRASH_RETENTION_DAYS * 86400
    return max(0, math.ceil((expires - (now if now is not None else time.time())) / 86400))


class TrashDialog(tk.Toplevel):
    """Lists trashed notes with restore / delete-forever. `on_change` runs after any change
    so the caller can refresh its own list."""

    def __init__(self, parent, on_change=None):
        super().__init__(parent)
        self._on_change = on_change
        self.title("Thùng rác")
        self.configure(bg=BG)
        self.geometry("360x420")
        self.minsize(300, 260)
        self.transient(parent)
        try:
            self.attributes("-topmost", True)  # the main widget is usually pinned on top
        except tk.TclError:
            pass

        tk.Label(self, text="Thùng rác", bg=BG, fg=FG_TEXT, font=("Segoe UI", 12, "bold"),
                 anchor="w", padx=12, pady=8).pack(fill="x")
        self._hint = tk.Label(
            self, text=f"Ghi chú tự bị xoá vĩnh viễn sau {store.TRASH_RETENTION_DAYS} ngày.",
            bg=BG, fg=FG_MUTED, font=("Segoe UI", 9), anchor="w", padx=12)
        self._hint.pack(fill="x")

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        self._canvas = tk.Canvas(body, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(body, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._rows = tk.Frame(self._canvas, bg=BG)
        window = self._canvas.create_window((0, 0), window=self._rows, anchor="nw")
        self._rows.bind("<Configure>", lambda _e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfigure(window, width=e.width))
        self._canvas.bind("<MouseWheel>", lambda e: self._canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        footer = tk.Frame(self, bg=BG_HEADER)
        footer.pack(fill="x")
        self._empty_btn = tk.Button(
            footer, text="Dọn sạch thùng rác", command=self._empty, bg=BG_MENU, fg=FG_DANGER,
            activebackground=BORDER, activeforeground=FG_DANGER, relief="flat", padx=10, pady=4)
        self._empty_btn.pack(side="right", padx=8, pady=6)

        self.refresh()

    def refresh(self):
        for child in self._rows.winfo_children():
            child.destroy()
        trashed = store.list_trash()
        self._empty_btn.config(state="normal" if trashed else "disabled")
        if not trashed:
            tk.Label(self._rows, text="Thùng rác trống.", bg=BG, fg=FG_MUTED,
                     font=("Segoe UI", 10), pady=30).pack(fill="x")
            return
        for note in trashed:
            self._add_row(note)

    def _add_row(self, note):
        card = tk.Frame(self._rows, bg=BG_MENU, padx=8, pady=8,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=4, padx=2)
        tk.Label(card, text=note["title"], bg=BG_MENU, fg=FG_TEXT, font=("Segoe UI", 10, "bold"),
                 anchor="w").pack(fill="x")
        if note["snippet"]:
            tk.Label(card, text=note["snippet"][:80], bg=BG_MENU, fg=FG_MUTED, font=("Segoe UI", 9),
                     anchor="w", justify="left").pack(fill="x")
        deleted = datetime.datetime.fromtimestamp(note["deleted_at"]).strftime("%d/%m/%Y %H:%M")
        tk.Label(card, text=f"Đã xoá {deleted} · còn {_days_left(note['deleted_at'])} ngày",
                 bg=BG_MENU, fg=FG_MUTED, font=("Segoe UI", 8), anchor="w").pack(fill="x", pady=(2, 6))
        buttons = tk.Frame(card, bg=BG_MENU)
        buttons.pack(fill="x")
        tk.Button(buttons, text="Khôi phục", command=lambda i=note["id"]: self._restore(i),
                  bg=BG_HEADER, fg=FG_ACCENT, activebackground=BORDER, activeforeground=FG_ACCENT,
                  relief="flat", padx=8).pack(side="left")
        tk.Button(buttons, text="Xoá vĩnh viễn", command=lambda i=note["id"], t=note["title"]: self._purge(i, t),
                  bg=BG_HEADER, fg=FG_DANGER, activebackground=BORDER, activeforeground=FG_DANGER,
                  relief="flat", padx=8).pack(side="left", padx=(6, 0))

    def _changed(self):
        self.refresh()
        if self._on_change:
            self._on_change()

    def _restore(self, note_id):
        store.restore_note(note_id)
        self._changed()

    def _purge(self, note_id, title):
        if messagebox.askyesno("Xoá vĩnh viễn", f"Xoá vĩnh viễn ghi chú:\n\"{title}\"?\nKhông thể hoàn tác.", parent=self):
            store.purge_note(note_id)
            self._changed()

    def _empty(self):
        if messagebox.askyesno("Dọn sạch thùng rác", "Xoá vĩnh viễn tất cả ghi chú trong thùng rác?\nKhông thể hoàn tác.", parent=self):
            store.empty_trash()
            self._changed()
