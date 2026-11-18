"""
Acoustic modem bench UI — TX tab (RX tab to be added later).

Talks to the TX board's Teensy 4.1 (tx_firmware.ino) over USB serial.

Run:   py -m venv .venv
       .venv\\Scripts\\activate
       pip install -r requirements.txt
       python modem_ui.py            # real hardware
       python modem_ui.py --sim      # adds a simulated TX Teensy to the port list
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication  # noqa: E402

from bench.main_window import MainWindow  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Acoustic modem bench UI")
    ap.add_argument("--sim", action="store_true",
                    help="add a simulated TX Teensy (SIM-TX) to the port list, for trying the app "
                         "without hardware")
    args, qt_args = ap.parse_known_args(argv)
    app = QApplication([sys.argv[0]] + qt_args)
    w = MainWindow(sim=args.sim)
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
