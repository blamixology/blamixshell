"""Remove Qt modules BlamixShell never uses from a PyInstaller build (saves ~250 MB).

Usage: python packaging/prune_qt.py <dist folder or .app>
Works for Windows (Qt6*.dll), Linux (libQt6*.so*) and macOS (Qt*.framework).
"""
import re
import shutil
import sys
from pathlib import Path

UNUSED = ("3D|Charts|ChartsQml|DataVisualization|Graphs|Labs|Location|Multimedia|Pdf|Quick3D|"
          "QuickControls2|QuickDialogs2|QuickTemplates2|QuickTimeline|QuickParticles|QuickShapes|"
          "QuickEffects|QuickVectorImage|ShaderTools|Sensors|SerialPort|SerialBus|Scxml|StateMachine|"
          "TextToSpeech|VirtualKeyboard|Bluetooth|Nfc|RemoteObjects|Designer|Help|UiTools|Sql|Test|"
          "SpatialAudio|HttpServer|Protobuf|Grpc|WebView|WebEngineQuick|WebSockets|NetworkAuth|"
          "Concurrent|Xml|SvgWidgets|WaylandCompositor|QuickTest")
PAT = re.compile(rf"^(lib)?Qt6?({UNUSED})[A-Za-z0-9]*(\.|$)", re.I)


def size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size


def main(root: Path) -> None:
    freed = 0
    for p in sorted(root.rglob("*"), key=lambda x: -len(x.parts)):
        if not p.exists():
            continue
        name = p.name
        drop = bool(PAT.match(name))
        # QML runtime files and non-WebEngine translations aren't used
        if p.is_dir() and name == "qml" and "PySide6" in str(p):
            drop = True
        if p.suffix == ".qm" and "qtwebengine_locales" not in str(p):
            drop = True
        # Chromium UI locales: English is enough (web content itself is unaffected)
        if p.suffix == ".pak" and p.parent.name == "qtwebengine_locales" and name != "en-US.pak":
            drop = True
        if name == "qtwebengine_devtools_resources.pak":
            drop = True
        # PySide6 python bindings for unused modules
        if p.suffix in (".pyd", ".so") and re.match(rf"^Qt({UNUSED})\w*\.", name):
            drop = True
        if drop:
            freed += size(p)
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    print(f"prune_qt: freed {freed / 1e6:.0f} MB")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
