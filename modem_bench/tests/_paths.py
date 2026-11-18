"""Make modem_ui/ importable from the tests."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UI_DIR = os.path.join(ROOT, "modem_ui")
BUILD_DIR = os.path.join(ROOT, "build")
if UI_DIR not in sys.path:
    sys.path.insert(0, UI_DIR)
