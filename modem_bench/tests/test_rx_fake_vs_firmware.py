"""The Python fake RX Teensy must answer exactly like the real firmware logic.

Builds tests/rx_fw_stdio.cpp (rx_firmware.ino + RX host stubs) with g++, runs one command script
through it and through bench.sim_rx.SimRxDevice (noise-free DC 2050, like the stub), and compares
the text replies and the number of frames line by line. Skipped if g++ is not available.
"""
import os
import shutil
import subprocess
import unittest

import _paths
from bench.rx_protocol import FrameParser
from bench.sim_rx import SimRxDevice

SCRIPT = """\
PING
VERSION
HELP
#POWER 0
STATUS
ARM
STREAM ON
CAPTURE 100 0
POTREAD
#POWER 1
STATUS
POT 300
POT 12x
POT
POT 200
POTREAD
GAIN HIGH
GAIN
TRIG LEVEL 300
TRIG LEVEL 0
TRIG LEVEL 99999999999
TRIG FOO
RAW 2
arm
ARM
STATUS
POTREAD
RAW 4
RAW
RAW 0
RAW 17
CAPTURE 10
CAPTURE 100 100
CAPTURE 2000 10
STREAM ON
TRIG NONE
CAPTURE 50 0
STATUS
#MS 16
ABORT
TRIG NONE
CAPTURE 5000 100
#MS 16
STATUS
STREAM ON
#MS 32
STATS
STREAM OFF
STATS
STATS
#DTR 0
STREAM ON
#MS 16
STATUS
#DTR 1
POT 77
#POTRESET
#MS 512
STREAM ON
#POWER 0
#MS 400
#MS 608
STATUS
ARM
#POWER 1
ARM
DISARM
DISARM
STREAM OFF
BOGUS x y
  ping
""" + "A" * 120 + "\nPING\n"


def build_rx_fw_stdio() -> str | None:
    gxx = shutil.which("g++")
    if not gxx:
        return None
    os.makedirs(_paths.BUILD_DIR, exist_ok=True)
    exe = os.path.join(_paths.BUILD_DIR, "rx_fw_stdio.exe" if os.name == "nt" else "rx_fw_stdio")
    subprocess.run([gxx, "-std=c++17", "-O1", "-I", os.path.join(_paths.HERE, "host_stubs_rx"), "-o", exe,
                    os.path.join(_paths.HERE, "rx_fw_stdio.cpp")], check=True)
    return exe


def run_fake(script: str) -> list[str]:
    dev = SimRxDevice(noise=0.0, dc=2050.0, version="1.0")
    out = []
    for line in script.splitlines():
        if line.startswith("#POWER"):
            dev.board_powered = line.split()[1] != "0"
            data = b""
        elif line.startswith("#MS"):
            data = dev.advance_ms(int(line.split()[1]))
        elif line.startswith("#DTR"):
            dev.dtr = line.split()[1] != "0"
            data = b""
        elif line == "#POTRESET":
            dev.pot_reg = 128
            data = b""
        else:
            data = dev.feed(line + "\n") + dev._loop()      # the firmware runs loop() once per command
        p = FrameParser()
        ev = p.feed(data)
        lines = [x for k, x in ev if k == "line"]
        nfr = sum(1 for k, _ in ev if k == "frame")
        r = " | ".join(lines)
        if nfr:
            r += f" [frames={nfr}]"
        if p.crc_errors or p.resync_bytes:
            r += " [BAD STREAM]"
        out.append(r)
    return out


class TestRxFakeMatchesFirmware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_rx_fw_stdio()

    def test_same_replies(self):
        if not self.exe:
            self.skipTest("g++ not found")
        p = subprocess.run([self.exe], input=SCRIPT, capture_output=True, text=True, check=True)
        fw = p.stdout.splitlines()
        fake = run_fake(SCRIPT)
        lines = SCRIPT.splitlines()
        self.assertEqual(len(fw), len(lines))
        diffs = [f"{cmd[:30]!r}:\n   fw  : {a}\n   fake: {b}" for cmd, a, b in zip(lines, fw, fake) if a != b]
        self.assertFalse(diffs, "fake differs from firmware:\n" + "\n".join(diffs))
        joined = "\n".join(fw)
        for needle in ("ERR NOPOWER", "OK ARMED mid=2050", "OK RAW 1005 1004 1005 1004", "EVT TRIG id=2",
                       "EVT CAPDONE id=2 n=5000 pre=100", "[frames=", "WARN POTRESET", "WARN POWERLOST",
                       "ERR BUSY", "ERR line too long", "per_min_ns=2500", "OK DISARMED"):
            self.assertIn(needle, joined)


if __name__ == "__main__":
    unittest.main()
