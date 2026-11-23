"""Private file access and a process-lifetime lock on Windows and POSIX."""
import errno
import os
import re
import sqlite3
import stat
import threading
from contextlib import contextmanager


_database_open_lock = threading.RLock()


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                    ("inherit", wintypes.BOOL)]

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p
    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(_SecurityAttributes), wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
    _kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    _kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.restype = wintypes.DWORD
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL),
                                                    ctypes.POINTER(ctypes.c_void_p),
                                                    ctypes.POINTER(wintypes.BOOL)]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p]
    _advapi32.SetSecurityInfo.restype = wintypes.DWORD
    _advapi32.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    _advapi32.GetSecurityInfo.restype = wintypes.DWORD
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL

    def _current_user_sid():
        token = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            length = wintypes.DWORD()
            _advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
            buffer = ctypes.create_string_buffer(length.value)
            if not _advapi32.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
                raise ctypes.WinError(ctypes.get_last_error())
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_text = wintypes.LPWSTR()
            if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return sid_text.value
            finally:
                _kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
        finally:
            _kernel32.CloseHandle(token)

    def _dacl_text(descriptor):
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            _kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def _verify_private_dacl(handle, expected, path):
        actual = ctypes.c_void_p()
        result = _advapi32.GetSecurityInfo(handle, 1, 4, None, None, None, None,
                                           ctypes.byref(actual))
        if result:
            raise ctypes.WinError(result)
        try:
            wanted, found = _dacl_text(expected), _dacl_text(actual)
            prefix = found.split("(", 1)[0]
            entries = lambda value: sorted(re.findall(r"\([^()]*\)", value))
            if not prefix.startswith("D:") or "P" not in prefix[2:] or entries(found) != entries(wanted):
                raise PermissionError(errno.EACCES, "Windows did not apply the private file ACL", os.fspath(path))
        finally:
            _kernel32.LocalFree(actual)

    def _windows_open(path, flags):
        """Open the final path component itself and apply a user-only DACL."""
        # A descriptor at creation time prevents a key from ever being created
        # with inherited, potentially permissive access rights.
        descriptor = ctypes.c_void_p()
        sddl = f"D:P(A;;FA;;;{_current_user_sid()})(A;;FA;;;SY)"
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attrs = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            access = 0x00040000 | 0x00020000  # WRITE_DAC | READ_CONTROL
            if flags & os.O_RDWR:
                access |= 0x80000000 | 0x40000000  # GENERIC_READ | GENERIC_WRITE
            elif flags & os.O_WRONLY:
                access |= 0x40000000
            else:
                access |= 0x80000000
            if flags & os.O_CREAT:
                disposition = 1 if flags & os.O_EXCL else 4  # CREATE_NEW / OPEN_ALWAYS
            else:
                disposition = 3  # OPEN_EXISTING
            handle = _kernel32.CreateFileW(os.fsdecode(path), access, 0x7, ctypes.byref(attrs),
                                           disposition, 0x00200000 | 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                info = _FileAttributeTagInfo()
                if not _kernel32.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if _kernel32.GetFileType(handle) != 1 or info.attributes & (0x400 | 0x10):
                    raise OSError(errno.ELOOP, "Expected a regular file, not a reparse point", os.fspath(path))
                present, defaulted = wintypes.BOOL(), wintypes.BOOL()
                dacl = ctypes.c_void_p()
                if not _advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
                                                           ctypes.byref(dacl), ctypes.byref(defaulted)):
                    raise ctypes.WinError(ctypes.get_last_error())
                if not present.value or not dacl.value:
                    raise OSError("Private Windows file ACL is missing")
                result = _advapi32.SetSecurityInfo(handle, 1, 0x80000004, None, None, dacl, None)
                if result:
                    raise ctypes.WinError(result)
                # Some filesystems or restricted environments report success
                # without applying the ACL. Never silently expose an old key.
                _verify_private_dacl(handle, descriptor, path)
                fd = msvcrt.open_osfhandle(handle, (flags & (os.O_RDWR | os.O_WRONLY)) | os.O_BINARY)
                handle = None  # The file descriptor owns the handle now.
                return fd
            finally:
                if handle is not None:
                    _kernel32.CloseHandle(handle)
        finally:
            _kernel32.LocalFree(descriptor)


def open_private(path, flags, *, fix_permissions=True):
    """Open a private regular file without following the final symlink.

    POSIX callers may leave existing permissions unchanged to validate them.
    On Windows an explicit user-only DACL is installed for both new and old files.
    """
    if os.name == "nt":
        return _windows_open(path, flags)
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Expected a regular file", os.fspath(path))
        if fix_permissions:
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def lock_exclusive(fd):
    """Acquire a non-blocking exclusive lock, released when fd is closed."""
    if os.name == "nt":
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(errno.EAGAIN, "File is locked") from exc
            raise
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def database_connection(path, timeout=5):
    """Open SQLite with private Windows WAL files and serialize open/close."""
    with _database_open_lock:
        if os.name == "nt":
            # SQLite otherwise gives new WAL/SHM files the directory's ACL.
            for suffix in ("-wal", "-shm"):
                fd = open_private(str(path) + suffix, os.O_RDWR | os.O_CREAT)
                os.close(fd)
        con = sqlite3.connect(path, timeout=timeout)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            if os.name == "nt":
                # Open the WAL before another connection is allowed to close
                # and remove it. The lock covers both ends of this transition.
                con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        except BaseException:
            con.close()
            raise
    try:
        with con:
            yield con
    finally:
        with _database_open_lock:
            con.close()
