"""Build the downloadable Windows version: dist/Mi-Asistente-Windows.zip.

    .venv\\Scripts\\python.exe packaging\\build.py

The build has no API key: it runs in demo mode with session tokens from token-server/ (gemini.token_url in
settings.yaml). It refuses to build if a .env would end up inside it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_DIR = DIST / "Mi Asistente"
ZIP_PATH = DIST / "Mi-Asistente-Windows.zip"
FORBIDDEN = (".env", "memory.db", "preferences.json")  # personal files that must never ship

README = """Mi Asistente
============

1. Unzip this folder anywhere (for example, Documents).
2. Open "Mi Asistente.exe". Use earbuds or headphones for the best experience.
3. If Windows shows "Windows protected your PC", click "More info" and then "Run anyway".
   (The app isn't signed with a paid certificate; it's a free demo.)

Your memories, preferences and logs are saved in %APPDATA%\\Mi Asistente.
This is a demo: it uses a shared free Gemini quota, so it may stop working for a while if many people use it.

Mi Asistente - joseabarca.com
"""


def main() -> int:
    from voicecompanion.config import load_settings

    if not load_settings().gemini.token_url:
        sys.exit("settings.yaml has no gemini.token_url: the build would have no way to connect.")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(DIST), "--workpath", str(ROOT / "build"),
                    str(ROOT / "packaging" / "MiAsistente.spec")], check=True)
    leaked = [p for p in APP_DIR.rglob("*") if p.name in FORBIDDEN]
    if leaked:
        sys.exit(f"Personal files ended up in the build, not zipping: {leaked}")
    (APP_DIR / "LEEME - README.txt").write_text(README, encoding="utf-8")
    ZIP_PATH.unlink(missing_ok=True)
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(APP_DIR.rglob("*")):
            zf.write(path, Path("Mi Asistente") / path.relative_to(APP_DIR))
    print(f"\nBuilt {ZIP_PATH} ({ZIP_PATH.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    sys.exit(main())
