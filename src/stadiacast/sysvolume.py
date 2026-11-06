"""The volume of Windows' default output device (``IAudioEndpointVolume``), read for the headset stream.

WASAPI loopback hands over the mix before the endpoint's own attenuation, so the Windows volume
slider and the media keys would not reach the headset. The sender reads the endpoint volume here
and scales the PCM itself. The COM methods are called through their vtable slots rather than
through a COM wrapper package.

Both functions return ``None`` when COM is unavailable, and the caller then streams at full
volume. On other platforms both are stubs that return ``None``.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    import threading
    import time
    from collections.abc import Callable
    from ctypes import POINTER, byref, c_float, c_uint32, c_void_p
    from typing import Any

    CLSCTX_ALL = 0x17
    COINIT_APARTMENTTHREADED = 0x2
    E_RENDER = 0
    E_CONSOLE = 0

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def parse(cls, text: str) -> GUID:
            parts = text.strip().strip("{}").split("-")
            g = cls()
            g.Data1 = int(parts[0], 16)
            g.Data2 = int(parts[1], 16)
            g.Data3 = int(parts[2], 16)
            tail = parts[3] + parts[4]
            for i in range(8):
                g.Data4[i] = int(tail[i * 2 : i * 2 + 2], 16)
            return g

    CLSID_MMDeviceEnumerator = GUID.parse("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    IID_IMMDeviceEnumerator = GUID.parse("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    IID_IAudioEndpointVolume = GUID.parse("{5CDF2C82-841E-4546-9722-0CF74078229A}")

    # vtable slots (the three IUnknown methods come first)
    _VT_RELEASE = 2
    _VT_ENUM_GET_DEFAULT = 4  # IMMDeviceEnumerator::GetDefaultAudioEndpoint
    _VT_DEVICE_ACTIVATE = 3  # IMMDevice::Activate
    _VT_GET_MASTER_SCALAR = 9  # IAudioEndpointVolume::GetMasterVolumeLevelScalar
    _VT_GET_MUTE = 15  # IAudioEndpointVolume::GetMute

    _ole32 = ctypes.WinDLL("ole32")

    def _method(ptr: c_void_p, index: int, restype: type[Any], *argtypes: type[Any]) -> Callable[..., Any]:
        """The COM method in a vtable slot; the interface pointer is its first argument."""
        vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
        return proto(vtable[index])

    def _release(ptr: c_void_p) -> None:
        try:
            _method(ptr, _VT_RELEASE, ctypes.c_ulong)(ptr)
        except Exception:
            pass

    class _Endpoint:
        """The default output's ``IAudioEndpointVolume``; ``close()`` releases the three pointers."""

        def __init__(self) -> None:
            self._enum: c_void_p | None = None
            self._dev: c_void_p | None = None
            self.volume: c_void_p | None = None

        def open(self) -> _Endpoint:
            # Re-initialising on the same thread only bumps a count; S_FALSE is fine. Not paired
            # with CoUninitialize on purpose: this is a resident process.
            _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            p_enum = c_void_p()
            hr = _ole32.CoCreateInstance(
                byref(CLSID_MMDeviceEnumerator), None, CLSCTX_ALL, byref(IID_IMMDeviceEnumerator), byref(p_enum)
            )
            if hr != 0 or not p_enum:
                raise OSError(f"CoCreateInstance(MMDeviceEnumerator) hr=0x{hr & 0xFFFFFFFF:08x}")
            self._enum = p_enum
            p_dev = c_void_p()
            get_default = _method(
                p_enum, _VT_ENUM_GET_DEFAULT, ctypes.c_long, ctypes.c_int, ctypes.c_int, POINTER(c_void_p)
            )
            hr = get_default(p_enum, E_RENDER, E_CONSOLE, byref(p_dev))
            if hr != 0 or not p_dev:
                raise OSError(f"GetDefaultAudioEndpoint hr=0x{hr & 0xFFFFFFFF:08x}")
            self._dev = p_dev
            p_vol = c_void_p()
            activate = _method(
                p_dev, _VT_DEVICE_ACTIVATE, ctypes.c_long, POINTER(GUID), c_uint32, c_void_p, POINTER(c_void_p)
            )
            hr = activate(p_dev, byref(IID_IAudioEndpointVolume), CLSCTX_ALL, None, byref(p_vol))
            if hr != 0 or not p_vol:
                raise OSError(f"IMMDevice::Activate(IAudioEndpointVolume) hr=0x{hr & 0xFFFFFFFF:08x}")
            self.volume = p_vol
            return self

        def close(self) -> None:
            for attr in ("volume", "_dev", "_enum"):
                ptr = getattr(self, attr, None)
                if ptr:
                    _release(ptr)
                    setattr(self, attr, None)

    #: Creating the COM object costs ~12 ms, and the sender reads the volume several times a
    #: second: the pointers are cached and rebuilt on failure or after the TTL (the default
    #: output may have been switched).
    _CACHE_TTL = 15.0
    _lock = threading.Lock()
    _cached: _Endpoint | None = None
    _cached_at = 0.0

    def _invalidate() -> None:
        global _cached, _cached_at
        with _lock:
            ep, _cached, _cached_at = _cached, None, 0.0
        if ep is not None:
            ep.close()

    def _endpoint() -> _Endpoint | None:
        global _cached, _cached_at
        with _lock:
            now = time.monotonic()
            if _cached is not None and now - _cached_at <= _CACHE_TTL:
                return _cached
            old, _cached = _cached, None
            if old is not None:
                old.close()
            try:
                ep = _Endpoint().open()
            except Exception:
                return None
            _cached, _cached_at = ep, now
            return ep

    def master_scalar() -> float | None:
        """The default output's volume, 0.0 to 1.0; None when it cannot be read."""
        ep = _endpoint()
        if ep is None or ep.volume is None:
            return None
        try:
            value = c_float(1.0)
            hr = _method(ep.volume, _VT_GET_MASTER_SCALAR, ctypes.c_long, POINTER(c_float))(ep.volume, byref(value))
            if hr != 0:
                _invalidate()
                return None
            return max(0.0, min(1.0, float(value.value)))
        except Exception:
            _invalidate()
            return None

    def muted() -> bool | None:
        """Whether the default output is muted: a switch of its own, not a volume of zero."""
        ep = _endpoint()
        if ep is None or ep.volume is None:
            return None
        try:
            flag = ctypes.c_int(0)
            hr = _method(ep.volume, _VT_GET_MUTE, ctypes.c_long, POINTER(ctypes.c_int))(ep.volume, byref(flag))
            if hr != 0:
                return None
            return bool(flag.value)
        except Exception:
            _invalidate()
            return None

else:

    def master_scalar() -> float | None:
        return None

    def muted() -> bool | None:
        return None


__all__ = ["master_scalar", "muted"]
