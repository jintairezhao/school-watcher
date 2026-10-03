"""Keep frameless Windows windows inside the current monitor's work area."""


def enable_taskbar_controls(hwnd):
    """Restore shell minimize/restore for a WinForms FormBorderStyle.None window."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE is always a 32-bit value.
    # Retain the frameless appearance; only restore native shell capabilities.
    required = 0x00020000 | 0x00010000 | 0x00080000  # MINIMIZEBOX, MAXIMIZEBOX, SYSMENU
    if style & required == required:
        return
    ctypes.set_last_error(0)
    previous = user32.SetWindowLongW(hwnd, -16, style | required)
    if not previous and ctypes.get_last_error():
        raise ctypes.WinError(ctypes.get_last_error())
    # Apply the changed style without moving, resizing or activating the window.
    if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0010 | 0x0004 | 0x0002 | 0x0001):
        raise ctypes.WinError(ctypes.get_last_error())


def fit_size(width, height, work_width, work_height, scale=1):
    available_width = max(1, int(work_width / scale))
    available_height = max(1, int(work_height / scale))
    return (min(width, available_width), min(height, available_height),
            (min(760, available_width), min(520, available_height)))


def install_work_area(window):
    from System import Action
    from System.Drawing import Rectangle, Size, Point
    from System.Windows.Forms import Screen, FormWindowState
    def shell_controls(*_):
        enable_taskbar_controls(window.native.Handle.ToInt64())

    def update(*_):
        form = window.native
        screen = Screen.FromHandle(form.Handle)
        work, bounds = screen.WorkingArea, screen.Bounds
        form.MaximizedBounds = Rectangle(work.X - bounds.X, work.Y - bounds.Y, work.Width, work.Height)
        form.MinimumSize = Size(min(form.MinimumSize.Width, work.Width), min(form.MinimumSize.Height, work.Height))
        if form.WindowState == FormWindowState.Normal:
            form.Size = Size(min(form.Width, work.Width), min(form.Height, work.Height))
            form.Location = Point(
                max(work.Left, min(form.Left, work.Right - form.Width)),
                max(work.Top, min(form.Top, work.Bottom - form.Height)))
    def attach():
        shell_controls()
        update()
        # WinForms can recreate its handle or style after display/appearance changes.
        window.native.HandleCreated += shell_controls
        window.native.StyleChanged += shell_controls
        # Updating the bounds when dragged across monitors also handles different DPI/taskbar edges.
        window.native.LocationChanged += update
        window.native.DpiChanged += update
    window.native.Invoke(Action(attach))
