"""Cross-platform file security, process locking and SQLite regressions."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# These checks can run with the standard library even before dependencies are installed.
MODULE_PATH = Path(__file__).resolve().parents[1] / "app" / "platform_files.py"
spec = importlib.util.spec_from_file_location("platform_files_under_test", MODULE_PATH)
platform_files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform_files)


def windows_dacl(path):
    """Read the actual DACL from Windows, independently of the creation code."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    result = advapi.GetNamedSecurityInfoW(str(path), 1, 4, None, None, None, None,
                                          ctypes.byref(descriptor))
    if result:
        raise ctypes.WinError(result)
    try:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                descriptor, 1, 4, ctypes.byref(text), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.LocalFree(descriptor)


class PlatformFileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def assert_private(self, path):
        if os.name == "nt":
            dacl = windows_dacl(path)
            self.assertTrue(dacl.startswith("D:P"), dacl)
            entries = re.findall(r"\(([^)]+)\)", dacl)
            self.assertEqual(len(entries), 2, dacl)
            self.assertEqual({entry.split(";")[-1] for entry in entries},
                             {platform_files._current_user_sid(), "SY"}, dacl)
            self.assertTrue(all(entry.startswith("A;;FA;;;") for entry in entries), dacl)
        else:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_private_file_create_reopen_and_exclusive_create(self):
        path = self.directory / "secret"
        fd = platform_files.open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"existing-key")
        self.assert_private(path)
        with self.assertRaises(FileExistsError):
            platform_files.open_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        fd = platform_files.open_private(path, os.O_RDONLY, fix_permissions=False)
        with os.fdopen(fd, "rb") as stream:
            self.assertEqual(stream.read(), b"existing-key")

    def test_final_symlink_is_rejected(self):
        target = self.directory / "target"
        target.write_bytes(b"unchanged")
        link = self.directory / "link"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"Cannot create a symlink on this system: {exc}")
        with self.assertRaises(OSError):
            platform_files.open_private(link, os.O_RDONLY)
        self.assertEqual(target.read_bytes(), b"unchanged")

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_existing_file_is_protected_or_safely_rejected(self):
        path = self.directory / "existing-key"
        path.write_bytes(b"existing-master-key")
        try:
            fd = platform_files.open_private(path, os.O_RDONLY, fix_permissions=False)
        except PermissionError as exc:
            self.assertIn("private file ACL", str(exc))
            self.assertEqual(path.read_bytes(), b"existing-master-key")
            return
        with os.fdopen(fd, "rb") as stream:
            self.assertEqual(stream.read(), b"existing-master-key")
        self.assert_private(path)

    def test_lock_conflicts_across_processes_and_is_released(self):
        path = self.directory / "instance.lock"
        fd = platform_files.open_private(path, os.O_RDWR | os.O_CREAT)
        child = """
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location('platform_files_child', sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
fd = m.open_private(sys.argv[2], os.O_RDWR)
try:
    try:
        m.lock_exclusive(fd)
    except BlockingIOError:
        print('busy')
    else:
        print('locked')
finally:
    os.close(fd)
"""
        def try_child():
            return subprocess.run([sys.executable, "-c", child, str(MODULE_PATH), str(path)],
                                  check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        try:
            platform_files.lock_exclusive(fd)
            self.assertEqual(try_child(), "busy")
        finally:
            os.close(fd)
        self.assertEqual(try_child(), "locked")

    def test_sqlite_wal_is_private_and_reopens_concurrently(self):
        path = self.directory / "data.sqlite3"
        fd = platform_files.open_private(path, os.O_RDWR | os.O_CREAT)
        os.close(fd)
        with platform_files.database_connection(path) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("CREATE TABLE messages (value TEXT)")
            con.execute("INSERT INTO messages VALUES ('first')")
            con.commit()
            with platform_files.database_connection(path) as observer:
                self.assertEqual(observer.execute("SELECT value FROM messages").fetchone()[0], "first")
                for suffix in ("", "-wal", "-shm"):
                    self.assert_private(Path(str(path) + suffix))
        with self.assertRaises(RuntimeError):
            with platform_files.database_connection(path) as con:
                con.execute("INSERT INTO messages VALUES ('rollback')")
                raise RuntimeError("rollback")

        def add(index):
            with platform_files.database_connection(path) as con:
                con.execute("INSERT INTO messages VALUES (?)", (str(index),))

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(20)))
        with platform_files.database_connection(path) as con:
            values = [row[0] for row in con.execute("SELECT value FROM messages")]
        self.assertEqual(len(values), 21)
        self.assertNotIn("rollback", values)

    def test_existing_database_recovers_a_committed_wal(self):
        path = self.directory / "existing.sqlite3"
        # Some restricted Windows environments cannot change ACLs on an old
        # file, so create the fixtures privately before the simulated crash.
        for suffix in ("", "-wal", "-shm"):
            fd = platform_files.open_private(str(path) + suffix, os.O_RDWR | os.O_CREAT)
            os.close(fd)
        child = """
import os, sqlite3, sys
con = sqlite3.connect(sys.argv[1])
con.execute('PRAGMA journal_mode=WAL')
con.execute('CREATE TABLE messages (value TEXT)')
con.execute('INSERT INTO messages VALUES (?)', ('before restart',))
con.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", child, str(path)], check=True, timeout=10)
        self.assertTrue(Path(str(path) + "-wal").exists())
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            if candidate.exists():
                fd = platform_files.open_private(candidate, os.O_RDWR)
                os.close(fd)
        with platform_files.database_connection(path) as con:
            self.assertEqual(con.execute("SELECT value FROM messages").fetchone()[0], "before restart")


if __name__ == "__main__":
    unittest.main()
