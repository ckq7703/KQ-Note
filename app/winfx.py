import ctypes
from ctypes import wintypes

_SINGLE_INSTANCE_MUTEX_NAME = "Local\\KQNoteSingleInstanceMutex"
ERROR_ALREADY_EXISTS = 183
SPI_GETWORKAREA = 0x0030

ABM_NEW = 0x00000000
ABM_REMOVE = 0x00000001
ABM_QUERYPOS = 0x00000002
ABM_SETPOS = 0x00000003
ABE_LEFT = 0
ABE_RIGHT = 2
SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
_APPBAR_CALLBACK_MSG = 0x8000 + 1  # WM_APP + 1; arbitrary but must be >= WM_APP

_mutex_handle = None


class _APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", wintypes.RECT),
        ("lParam", wintypes.LPARAM),
    ]


def get_work_area():
    """Primary monitor's work area (left, top, right, bottom), excluding the taskbar.
    Returns None if the Win32 call fails for any reason."""
    try:
        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        if not ok:
            return None
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return None


def register_appbar(hwnd):
    """Register this window with the shell as an AppBar (the same mechanism the
    taskbar itself uses) so maximized windows shrink to avoid overlapping it,
    instead of just floating on top of everything."""
    try:
        abd = _APPBARDATA()
        abd.cbSize = ctypes.sizeof(_APPBARDATA)
        abd.hWnd = hwnd
        abd.uCallbackMessage = _APPBAR_CALLBACK_MSG
        ctypes.windll.shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))
    except Exception:
        pass


def unregister_appbar(hwnd):
    try:
        abd = _APPBARDATA()
        abd.cbSize = ctypes.sizeof(_APPBARDATA)
        abd.hWnd = hwnd
        ctypes.windll.shell32.SHAppBarMessage(ABM_REMOVE, ctypes.byref(abd))
    except Exception:
        pass


def set_appbar_edge_pos(hwnd, side, width):
    """Reserve a `width`-px strip along the left/right edge of the primary
    monitor for `hwnd`. Must call register_appbar() first. Returns the
    (x, y, w, h) rect the shell actually granted (it may differ slightly,
    e.g. to avoid the taskbar), or None on failure."""
    try:
        screen_w = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
        screen_h = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)

        abd = _APPBARDATA()
        abd.cbSize = ctypes.sizeof(_APPBARDATA)
        abd.hWnd = hwnd
        abd.uEdge = ABE_LEFT if side == "left" else ABE_RIGHT
        abd.rc.top = 0
        abd.rc.bottom = screen_h
        if side == "left":
            abd.rc.left = 0
            abd.rc.right = width
        else:
            abd.rc.right = screen_w
            abd.rc.left = screen_w - width

        ctypes.windll.shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))

        # ABM_QUERYPOS may shift the edge coordinate to dodge another appbar
        # (usually the taskbar) — recompute the far edge from our fixed width.
        if side == "left":
            abd.rc.right = abd.rc.left + width
        else:
            abd.rc.left = abd.rc.right - width

        ctypes.windll.shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))

        return abd.rc.left, abd.rc.top, abd.rc.right - abd.rc.left, abd.rc.bottom - abd.rc.top
    except Exception:
        return None


def get_virtual_screen_rect():
    """Bounding rect (x, y, width, height) spanning ALL monitors, not just the
    primary one — winfo_screenwidth()/height() only ever report the primary
    monitor, which would leave the screenshot overlay not covering (or
    misaligned on) a secondary monitor. Returns None if the Win32 call fails."""
    try:
        x = ctypes.windll.user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = ctypes.windll.user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = ctypes.windll.user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = ctypes.windll.user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w <= 0 or h <= 0:
            return None
        return x, y, w, h
    except Exception:
        return None


def acquire_single_instance_lock():
    """Returns True if this is the only running instance, False if another is already running."""
    global _mutex_handle
    try:
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS
    except Exception:
        return True


def round_window(widget, radius=14):
    try:
        widget.update_idletasks()
        hwnd = widget.winfo_id()
        w = max(1, widget.winfo_width())
        h = max(1, widget.winfo_height())
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius, radius)
        ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass
