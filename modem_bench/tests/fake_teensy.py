"""Fake TX Teensy on a real (virtual) serial port.

Serves the simulated TX firmware (modem_ui/bench/sim_tx.py, verified against the real
firmware by test_fake_vs_firmware.py) so you can test the unmodified app end-to-end,
including the QSerialPort path:

  Windows: install com0com, create a pair (e.g. COM20 <-> COM21), then
           python tests/fake_teensy.py --port COM21      (needs: pip install pyserial)
           python modem_ui/modem_ui.py                   -> connect to COM20
  Linux/WSL:
           python tests/fake_teensy.py --pty             (prints the /dev/pts/N to open)

For quick tests without any of this, run the app with --sim instead.

Interactive keys on stdin while running: 'p' toggles board 12 V, 'g' simulates a
supply glitch (pot resets to mid-scale), 'q' quits.
"""
import argparse
import os
import sys
import threading
import time

import _paths  # noqa: F401
from bench.sim_tx import SimTxTeensy


def serve(read, write, sim: SimTxTeensy, stop: threading.Event):
    last = time.monotonic()
    while not stop.is_set():
        data = read()
        if data:
            for line in sim.feed(data.decode(errors="replace")):
                write((line + "\r\n").encode())
        now = time.monotonic()
        ms = int((now - last) * 1000) // 10 * 10
        if ms:
            last += ms / 1000
            for line in sim.advance(ms):
                write((line + "\r\n").encode())
        if not data:
            time.sleep(0.005)


def keyboard(sim: SimTxTeensy, stop: threading.Event):
    for line in sys.stdin:
        k = line.strip().lower()
        if k == "p":
            sim.board_powered = not sim.board_powered
            print(f"board 12 V {'ON' if sim.board_powered else 'OFF'}")
        elif k == "g":
            sim.pot_reg = 128
            print("supply glitch: pot reset to 128")
        elif k == "q":
            break
    stop.set()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="serial port to serve on (other end of a com0com pair)")
    g.add_argument("--pty", action="store_true", help="create a pseudo-terminal (Linux/WSL/macOS)")
    ap.add_argument("--no-power", action="store_true", help="start with the board 12 V off")
    args = ap.parse_args()

    sim = SimTxTeensy()
    sim.board_powered = not args.no_power
    stop = threading.Event()

    if args.pty:
        import pty
        import tty
        master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        print(f"fake TX Teensy on {os.ttyname(slave)}  (keys: p=power, g=glitch, q=quit)")

        def read():
            try:
                return os.read(master, 1024)
            except (BlockingIOError, OSError):
                return b""
        write = lambda b: os.write(master, b)  # noqa: E731
    else:
        try:
            import serial
        except ImportError:
            sys.exit("pyserial is required for --port:  pip install pyserial")
        ser = serial.Serial(args.port, 115200, timeout=0)
        print(f"fake TX Teensy on {args.port}  (keys: p=power, g=glitch, q=quit)")
        read = lambda: ser.read(1024)  # noqa: E731
        write = ser.write

    threading.Thread(target=keyboard, args=(sim, stop), daemon=True).start()
    try:
        serve(read, write, sim, stop)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
