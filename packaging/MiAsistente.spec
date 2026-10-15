# PyInstaller recipe for the downloadable Windows build. Run packaging/build.py rather than this directly.
# One folder (not one file): starts faster and antivirus programs flag it far less often.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "settings.yaml"), "."),
    (str(ROOT / "profiles"), "profiles"),
    (str(ROOT / "voicecompanion" / "logo.png"), "voicecompanion"),
    (str(ROOT / "voicecompanion" / "logo.ico"), "voicecompanion"),
]
datas += collect_data_files("customtkinter")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    excludes=["pytest", "_pytest", "matplotlib", "IPython", "notebook", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mi Asistente",
    icon=str(ROOT / "voicecompanion" / "logo.ico"),
    console=False,  # a window app; errors go to %APPDATA%\Mi Asistente\logs
    upx=False,  # UPX-packed exes get flagged by antivirus programs
)
coll = COLLECT(exe, a.binaries, a.datas, name="Mi Asistente", upx=False)
