"""Frozen-executable entry point (PyInstaller runs this as a plain script)."""
import sys

from sls.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
