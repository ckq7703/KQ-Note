import sys
import tkinter as tk

from app.config import load_config
from app.notes_widget import NotesWidget
from app.tray_app import TrayApp
from app.winfx import acquire_single_instance_lock


def main():
    if not acquire_single_instance_lock():
        return

    cfg = load_config()

    root = tk.Tk()
    root.withdraw()

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
