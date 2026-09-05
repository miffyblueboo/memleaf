"""Non-destructive native Windows process liveness checks."""
from __future__ import annotations


def windows_pid_status(pid: int) -> bool | None:
    """Probe a process handle without sending a signal or requesting termination.

    Access denial is conservative evidence of a live owner. Unexpected OS
    errors remain unknown so the caller retains its bounded legacy lease.
    """
    import ctypes
    from ctypes import wintypes

    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    if pid <= 0 or pid > 0xffffffff:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such process
            return False
        if error == 5:  # ERROR_ACCESS_DENIED: never steal an inaccessible owner
            return True
        return None
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0:  # WAIT_OBJECT_0: terminated, including exit code 259
            return False
        if result == 258:  # WAIT_TIMEOUT: still executing
            return True
        return None
    finally:
        kernel.CloseHandle(handle)
