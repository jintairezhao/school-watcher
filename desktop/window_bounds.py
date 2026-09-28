"""Keep frameless Windows windows inside the current monitor's work area."""


def fit_size(width, height, work_width, work_height, scale=1):
    available_width = max(1, int(work_width / scale))
    available_height = max(1, int(work_height / scale))
    return (min(width, available_width), min(height, available_height),
            (min(760, available_width), min(520, available_height)))


def install_work_area(window):
    from System import Action
    from System.Drawing import Rectangle, Size, Point
    from System.Windows.Forms import Screen, FormWindowState
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
        update()
        # Updating the bounds when dragged across monitors also handles different DPI/taskbar edges.
        window.native.LocationChanged += update
        window.native.DpiChanged += update
    window.native.Invoke(Action(attach))
