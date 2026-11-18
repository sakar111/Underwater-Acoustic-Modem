"""The Python fake Teensy must answer exactly like the real firmware logic.

Builds tests/fw_stdio.cpp (tx_firmware.ino + host stubs) with g++, runs one command
script through it and through bench.sim_tx.SimTxTeensy, and compares line by line.
Skipped if g++ is not available.
"""
import os
import shutil
import subprocess
import unittest

import _paths
from bench.sim_tx import SimTxTeensy

SCRIPT = """\
PING
VERSION
#POWER 0
STATUS
ARM
START
#POWER 1
STATUS
arm
ARM
FREQ 25000
AMP 200
POT 200
STATUS
LIMIT 60
LIMIT 0.5
FREQ 50
FREQ 100001
FREQ nan
AMP -1
AMP 1500
AMP nan
POT 256
POT -1
AMP
MODE
MODE FOO
MODE BURST 5 100
MODE BURST 1000 1000
MODE BURST 1000 20000000
SWEEP
SWEEP 20000 40000 0
SWEEP 50 40000 10
CAL 3
CAL 0.4
THERMSTOP 4000
POTREAD
BOGUS 1 2 3
START
POT 0
AMP 1400
POT 200
AMP 1400
POT 100
LIMIT 10
CAL 1.2
STATUS
AMP 200
MODE BURST 1000 10000
STATUS
MODE CONT
FREQ 90000
LIMIT 50
POT 0
AMP 900
STATUS
STOP
START
THERMSTOP 2
#MS 2100
STATUS
AMP 200
CAL 1.2
STATUS
CAL 1
SWEEP 20000 40000 10
STATUS
SWEEP OFF
#MS 600
STOP
DISARM
STATUS
START
DISARM
POT 200
ARM
START
#POWER 0
#MS 400
#MS 600
STATUS
ARM
#POWER 1
ARM
STATUS
help
  ping
"""


def build_fw_stdio() -> str | None:
    gxx = shutil.which("g++")
    if not gxx:
        return None
    os.makedirs(_paths.BUILD_DIR, exist_ok=True)
    exe = os.path.join(_paths.BUILD_DIR, "fw_stdio.exe" if os.name == "nt" else "fw_stdio")
    src = os.path.join(_paths.HERE, "fw_stdio.cpp")
    subprocess.run([gxx, "-std=c++17", "-O1", "-I", os.path.join(_paths.HERE, "host_stubs"),
                    "-o", exe, src], check=True)
    return exe


def run_sim(script: str) -> list[str]:
    sim = SimTxTeensy()
    out = []
    for line in script.splitlines():
        if line.startswith("#POWER"):
            sim.board_powered = line.split()[1] != "0"
            r = []
        elif line.startswith("#MS"):
            r = sim.advance(int(line.split()[1]))
        else:
            r = sim.feed(line + "\n")
        out.append(" | ".join(r))
    return out


class TestFakeMatchesFirmware(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_fw_stdio()

    def test_same_replies(self):
        if not self.exe:
            self.skipTest("g++ not found")
        p = subprocess.run([self.exe], input=SCRIPT, capture_output=True, text=True, check=True)
        fw = p.stdout.splitlines()
        sim = run_sim(SCRIPT)
        lines = SCRIPT.splitlines()
        self.assertEqual(len(fw), len(lines))
        diffs = [f"{cmd!r}:\n   fw : {a}\n   sim: {b}" for cmd, a, b in zip(lines, fw, sim) if a != b]
        self.assertFalse(diffs, "fake differs from firmware:\n" + "\n".join(diffs))
        # sanity: the script exercised the interesting paths
        joined = "\n".join(fw)
        for needle in ("ERR NOPOWER", "OK ARMED", "WARN THERMAL continuous", "WARN THERMAL auto-stop",
                       "WARN NOPOWER", "ERR LIMIT", "OK DISARMED", "est=13.24"):
            self.assertIn(needle, joined)


if __name__ == "__main__":
    unittest.main()
