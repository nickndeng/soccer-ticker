"""Entry point: launch the front-end matching the current platform.

    python3 -m soccer_ticker
"""
import sys


def main():
    if sys.platform == "darwin":
        from .frontend_macos import main as run
    else:
        from .frontend_linux import main as run
    run()


if __name__ == "__main__":
    main()
