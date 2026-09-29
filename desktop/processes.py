"""Keep desktop services (including their browser children) owned by the app."""
import os
import sys
import threading


class ServiceJob:
    """A non-inherited Windows job handle, held only by the desktop parent."""
    def __init__(self):
        self.handle = None
        if os.name != 'nt':
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t),
                ('active', w.DWORD), ('affinity', c.c_size_t),
                ('priority', w.DWORD), ('scheduling', w.DWORD)]

        class ExtendedLimits(c.Structure):
            _fields_ = [('basic', Limits), ('io', c.c_ulonglong * 6),
                ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]

        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.SetInformationJobObject.restype = w.BOOL
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.AssignProcessToJobObject.restype = w.BOOL
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            error = c.WinError(c.get_last_error())
            self.close()
            raise error

    def add(self, process):
        if self.handle:
            import ctypes
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def guard_service():
    """Wait for ownership before doing work; EOF also covers non-Windows hosts."""
    if os.environ.pop('WATCHER_SERVICE_GUARD', None) != '1':
        return
    if sys.stdin is not None:
        descriptor = os.dup(sys.stdin.fileno())
    elif os.name == 'nt':
        # A frozen windowed executable has sys.stdin=None, even with a pipe.
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(-10 & 0xffffffff)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise SystemExit('Desktop parent pipe is unavailable')
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    else:
        raise SystemExit('Desktop parent pipe is unavailable')
    # Never hold a BufferedReader lock in a daemon thread: short-lived migrate
    # and probe services must be able to finalize Python while the parent lives.
    marker = b''
    while len(marker) < 6:
        piece = os.read(descriptor, 6 - len(marker))
        if not piece: break
        marker += piece
    if marker != b'start\n':
        os.close(descriptor)
        raise SystemExit('Desktop parent exited before service startup')

    def parent_exited():
        try:
            os.read(descriptor, 1)  # The sole writing handle belongs to the desktop app.
        finally:
            try:
                os.close(descriptor)
                import psutil
                children = psutil.Process().children(recursive=True)
                for child in children:
                    try: child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied): pass
                _, alive = psutil.wait_procs(children, timeout=3)
                for child in alive:
                    try: child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied): pass
                psutil.wait_procs(alive, timeout=3)
            finally:
                os._exit(0)

    threading.Thread(target=parent_exited, name='desktop-parent-guard', daemon=True).start()
