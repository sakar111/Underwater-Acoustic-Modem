"""Fake RX Teensy on a real (virtual) serial port (spec RX Appendix B.2).

Serves the fake RX firmware (modem_ui/bench/sim_rx.py, checked against the real firmware by
test_rx_fake_vs_firmware.py) with real-time pacing at 400 kSPS, so you can test the unmodified app
end-to-end, including the QSerialPort path and the binary frame parser:

  Windows: install com0com, create a pair (e.g. COM22 <-> COM23), then
           python tests/fake_rx.py --port COM23        (needs: pip install pyserial)
           python modem_ui/modem_ui.py                 -> RX tab, connect to COM22
  Linux/WSL:
           python tests/fake_rx.py --pty               (prints the /dev/pts/N to open)

For quick tests without any of this, run the app with --sim instead (SIM-RX in the RX tab).

Interactive keys on stdin (type the letter + Enter):
  p  toggle board 12 V          b  toggle the 40 kHz burst signal     y  toggle the sync wire
  c  flip a payload byte every 50 frames (CRC test)      g  garbage bytes every 37 frames
  d  drop every 40th frame (gap test)                    s  stall 300 ms (overflow test)
  t  answer PING like the TX board (wrong-port test)     0  clear all faults       q  quit
"""
import argparse
import os
import sys
import threading
import time

import _paths  # noqa: F401
from bench.sim_rx import BurstConfig, SimRxDevice


def serve(read, write, dev: SimRxDevice, lock: threading.Lock, stop: threading.Event):
    t_last = time.perf_counter()
    while not stop.is_set():
        data = read()
        with lock:
            if data:
                out = dev.feed(data)
                if out:
                    write(out)
            now = time.perf_counter()
            n = int((now - t_last) * 400_000)
            if n >= 256:
                t_last += n / 400_000
                out = dev.advance_samples(min(n, 200_000))
                if out:
                    write(out)
        if not data:
            time.sleep(0.002)


def keyboard(dev: SimRxDevice, lock: threading.Lock, stop: threading.Event):
    for line in sys.stdin:
        k = line.strip().lower()
        with lock:
            if k == "p":
                dev.board_powered = not dev.board_powered
                print(f"board 12 V {'ON' if dev.board_powered else 'OFF'}")
            elif k == "b":
                dev.burst = None if dev.burst else BurstConfig()
                print(f"burst signal {'ON' if dev.burst else 'OFF'}")
            elif k == "y" and dev.burst:
                dev.burst.sync_wire = not dev.burst.sync_wire
                print(f"sync wire {'connected' if dev.burst.sync_wire else 'disconnected'}")
            elif k == "c":
                dev.crc_every = 50
                print("CRC errors every 50 frames")
            elif k == "g":
                dev.garbage_every = 37
                print("garbage every 37 frames")
            elif k == "d":
                dev.drop_every = 40
                print("dropping every 40th frame")
            elif k == "s":
                dev.stall(300)
                print("stalled 300 ms")
            elif k == "t":
                dev.pong_as_tx = not dev.pong_as_tx
                print(f"PING answers as {'TX' if dev.pong_as_tx else 'RX'}")
            elif k == "0":
                dev.crc_every = dev.garbage_every = dev.drop_every = 0
                print("faults cleared")
            elif k == "q":
                break
    stop.set()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--port", help="serial port to serve on (other end of a com0com pair)")
    g.add_argument("--pty", action="store_true", help="create a pseudo-terminal (Linux/WSL/macOS)")
    ap.add_argument("--no-power", action="store_true", help="start with the board 12 V off")
    ap.add_argument("--no-signal", action="store_true", help="noise only (no 40 kHz bursts)")
    args = ap.parse_args()

    dev = SimRxDevice(burst=None if args.no_signal else BurstConfig())
    dev.board_powered = not args.no_power
    lock = threading.Lock()
    stop = threading.Event()

    if args.pty:
        import pty
        import tty
        master, slave = pty.openpty()
        tty.setraw(slave)
        os.set_blocking(master, False)
        print(f"fake RX Teensy on {os.ttyname(slave)}  (keys: see --help)")

        def read():
            try:
                return os.read(master, 4096)
            except (BlockingIOError, OSError):
                return b""
        write = lambda b: os.write(master, b)  # noqa: E731
    else:
        try:
            import serial
        except ImportError:
            sys.exit("pyserial is required for --port:  pip install pyserial")
        ser = serial.Serial(args.port, 115200, timeout=0, write_timeout=1)
        print(f"fake RX Teensy on {args.port}  (keys: see --help)")
        read = lambda: ser.read(4096)  # noqa: E731
        write = ser.write

    threading.Thread(target=keyboard, args=(dev, lock, stop), daemon=True).start()
    try:
        serve(read, write, dev, lock, stop)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
