import ctypes

_SINGLE_INSTANCE_MUTEX_NAME = "Local\\KQNoteSingleInstanceMutex"
ERROR_ALREADY_EXISTS = 183

_mutex_handle = None


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
