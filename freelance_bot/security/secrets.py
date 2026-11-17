"""Windows DPAPI-backed secret storage.

Secrets are bound to the current Windows user and cannot be read as plain text
from the SQLite database. A clear error is raised on unsupported systems so a
caller does not silently downgrade to unsafe storage.
"""

import base64
import ctypes
import ctypes.wintypes as wintypes
import sys


class SecretStore:
    def encrypt(self, value: str) -> str:
        if sys.platform != "win32":
            raise RuntimeError("SecretStore requires Windows DPAPI")
        raw = value.encode("utf-8")
        blob = DATA_BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_byte)))
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(output)):
            raise OSError("CryptProtectData failed")
        try:
            return base64.b64encode(ctypes.string_at(output.pbData, output.cbData)).decode("ascii")
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    def decrypt(self, value: str) -> str:
        if sys.platform != "win32":
            raise RuntimeError("SecretStore requires Windows DPAPI")
        raw = base64.b64decode(value)
        blob = DATA_BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_byte)))
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob), None, None, None, None, 0, ctypes.byref(output)):
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
