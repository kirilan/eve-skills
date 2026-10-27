# PyInstaller spec for the Windows release: one self-contained eve-skills.exe, no Python needed.
#
#   pyinstaller --clean --noconfirm --distpath dist --workpath build/pyinstaller packaging/windows/eve-skills.spec
#
# Run from the repository root. It also builds on Linux (a Linux binary), which is how the spec is
# checked without a Windows machine; the shipped .exe comes from .github/workflows/release-windows.yml.
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parents[1]   # SPECPATH: the directory holding this file
VERSION = re.search(r'^__version__ = "([^"]+)"', (ROOT / "eve_skills" / "__init__.py").read_text(), re.M)[1]

# Version resource: what Explorer shows under Properties > Details. Windows-only metadata.
version_file = None
if sys.platform == "win32":
    from PyInstaller.utils.win32 import versioninfo as vi

    numbers = tuple(int(n) for n in re.findall(r"\d+", VERSION)[:3]) + (0,)
    strings = [vi.StringStruct(key, value) for key, value in (
        ("CompanyName", "kirilan"),
        ("FileDescription", "eve-skills - EVE Online command line tool"),
        ("FileVersion", VERSION),
        ("InternalName", "eve-skills"),
        ("LegalCopyright", "GPL-3.0-only"),
        ("OriginalFilename", "eve-skills.exe"),
        ("ProductName", "eve-skills"),
        ("ProductVersion", VERSION),
    )]
    version_file = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
              vi.VarFileInfo([vi.VarStruct("Translation", [0x0409, 1200])])],
    )

a = Analysis(
    [str(Path(SPECPATH) / "eve-skills.py")],
    pathex=[str(ROOT)],
    # The bundled SDE snapshot and the dashboard page - the same files pyproject's package-data ships.
    datas=[(str(ROOT / "eve_skills" / "data" / pattern), "eve_skills/data") for pattern in ("*.json", "*.html")],
    # Standard library only at runtime; nothing GUI or test-related is ever imported.
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="eve-skills",
    console=True,
    # UPX-packed executables are a classic antivirus false positive; the size saving is not worth it.
    upx=False,
    strip=False,
    debug=False,
    version=version_file,
)
