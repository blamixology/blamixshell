import sys

if len(sys.argv) > 1 and sys.argv[1] not in ("gui",):
    from .cli import main
else:
    # `python -m shelldeck` opens the desktop app when PySide6 is available, else the CLI/TUI
    try:
        import PySide6  # noqa: F401
        from .main import main
    except ImportError:
        from .cli import main
if len(sys.argv) > 1 and sys.argv[1] == "gui":
    sys.argv.pop(1)
main()
