# PyInstaller spec: the standalone executables attached to a release (make exe, .github/workflows/release.yml).
#
# Two one-file console programs: ``stadiacast`` (the receiver, with the audio and gamepad extras
# when the platform has them) and ``stadiacast-provision`` (Wi-Fi provisioning over Bluetooth).
# ffmpeg is not bundled: the audio sources find it on PATH.

import importlib.util
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

HERE = Path(SPECPATH)
SRC = str(HERE.parent / "src")

# The receiver's own data: the protobuf schemas and the built-in credential.
datas = collect_data_files("stadiacast", includes=["schemas/*.descriptor", "schemas/*.proto", "data/*.bin"])
hiddenimports = collect_submodules("stadiacast")  # the helpers are referenced by name only

optional_datas, optional_libs = [], []
for package in ("vgamepad", "pyaudiowpatch", "sounddevice"):
    # find_spec, not an import: importing vgamepad connects to the ViGEm bus and fails without the driver
    if importlib.util.find_spec(package) is None:
        continue
    optional_datas += collect_data_files(package)
    optional_libs += collect_dynamic_libs(package)
    hiddenimports.append(package)


def build(name, script, extra_hidden=()):
    analysis = Analysis(
        [str(HERE / script)],
        pathex=[SRC],
        datas=datas + optional_datas,
        binaries=optional_libs,
        hiddenimports=hiddenimports + list(extra_hidden),
        excludes=["tkinter", "test", "unittest"],
        noarchive=False,
    )
    pyz = PYZ(analysis.pure)
    return EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name=name,
        console=True,
        upx=False,
        strip=False,
        target_arch=None,
    )


build("stadiacast", "launcher.py")
build("stadiacast-provision", "provision_launcher.py", extra_hidden=["bleak"])
