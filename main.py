import os
import sys
import time
import tkinter as tk
import tkinter.messagebox as messagebox
import traceback

from app import store
from app.config import load_config
from app.notes_widget import NotesWidget
from app.tray_app import TrayApp
from app.winfx import acquire_single_instance_lock, enable_dpi_awareness


def _report_storage_failure(root, error):
    """The note database couldn't be opened/imported. The old note files are never
    modified by the import, so say where they are and log the details."""
    data_dir = store.get_data_dir()
    try:
        with open(os.path.join(data_dir, "startup_error.log"), "a", encoding="utf-8") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{traceback.format_exc()}\n")
    except OSError:
        pass
    messagebox.showerror(
        "KQ Note không thể mở dữ liệu ghi chú",
        f"{error}\n\nGhi chú của bạn chưa bị thay đổi và vẫn nằm trong:\n{data_dir}\n"
        "Chi tiết lỗi được ghi ở startup_error.log trong thư mục đó.",
        parent=root,
    )


def main():
    enable_dpi_awareness()
    if not acquire_single_instance_lock():
        return

    cfg = load_config()

    root = tk.Tk()
    root.withdraw()

    try:
        store.initialize()
    except Exception as e:
        _report_storage_failure(root, e)
        return

    widget = NotesWidget(root)
    widget.show()

    def toggle():
        root.after(0, widget.toggle)

    def do_quit():
        widget.flush_and_close()
        tray.stop()
        root.after(0, root.destroy)

    tray = TrayApp(
        on_toggle=toggle, on_quit=do_quit, hotkey_str=cfg["hotkey"],
        on_toggle_pin=toggle, hotkey_pin_str=cfg["hotkey_pin"],
    )
    tray.start()

    root.mainloop()
    sys.exit(0)


if __name__ == "__main__":
    main()
