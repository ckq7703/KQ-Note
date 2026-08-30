import os
import threading

from PIL import Image, ImageDraw
import pystray
from pynput import keyboard

LOGO_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "logo-kqnote.png")


def _make_icon_image():
    if os.path.exists(LOGO_PATH):
        return Image.open(LOGO_PATH).convert("RGBA")

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=12, fill=(45, 110, 165, 255))
    draw.text((20, 14), "N", fill=(255, 255, 255, 255))
    return img


def _format_hotkey(hk):
    if not hk:
        return ""
    return (
        hk.replace("<ctrl>", "Ctrl")
        .replace("<alt>", "Alt")
        .replace("<shift>", "Shift")
        .replace("<space>", "Space")
        .replace("+", " + ")
    )


class TrayApp:
    def __init__(self, on_toggle, on_quit, hotkey_str, on_toggle_pin=None, hotkey_pin_str=None):
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.hotkey_str = hotkey_str

        hk1 = _format_hotkey(hotkey_str)
        hk2 = _format_hotkey(hotkey_pin_str) if hotkey_pin_str else ""

        menu_items = []
        if hk1:
            menu_items.append(pystray.MenuItem(f"👁️  Show / Hide KQ Note  ({hk1})", lambda: self.on_toggle()))
        else:
            menu_items.append(pystray.MenuItem("👁️  Show / Hide KQ Note", lambda: self.on_toggle()))

        if on_toggle_pin and hk2:
            menu_items.append(pystray.MenuItem(f"📌  Show & Pin KQ Note  ({hk2})", lambda: self.on_toggle_pin()))

        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(pystray.MenuItem("❌  Exit KQ Note", lambda: self.on_quit()))

        self.icon = pystray.Icon(
            "KQNote", _make_icon_image(), "KQ Note",
            menu=pystray.Menu(*menu_items),
        )

        hotkeys = {hotkey_str: self.on_toggle}
        if on_toggle_pin and hotkey_pin_str:
            self.on_toggle_pin = on_toggle_pin
            hotkeys[hotkey_pin_str] = self.on_toggle_pin
        self.hotkey_listener = keyboard.GlobalHotKeys(hotkeys)

    def start(self):
        self.hotkey_listener.start()
        threading.Thread(target=self.icon.run, daemon=True).start()

    def stop(self):
        try:
            self.hotkey_listener.stop()
        except Exception:
            pass
        try:
            self.icon.stop()
        except Exception:
            pass
