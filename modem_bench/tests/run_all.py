"""Run every test that does not need hardware.

    python tests/run_all.py

1. builds tx_firmware.ino against the host stubs (g++) and runs sim_main (waveform,
   bursts, limits, ARM/DISARM, back-power, watchdog checks)
2. Python unit tests: level model + safe ordering, fake-vs-firmware cross-check,
   offscreen app tests against the simulated Teensy (needs PySide6)
"""
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _paths  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def firmware_tests() -> bool:
    gxx = shutil.which("g++")
    if not gxx:
        print("!! g++ not found: skipping the firmware host build (install MSYS2 / MinGW-w64)")
        return True
    os.makedirs(_paths.BUILD_DIR, exist_ok=True)
    exe = os.path.join(_paths.BUILD_DIR, "sim.exe" if os.name == "nt" else "sim")
    subprocess.run([gxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-I",
                    os.path.join(HERE, "host_stubs"), "-o", exe,
                    os.path.join(HERE, "sim_main.cpp")], check=True)
    return subprocess.run([exe]).returncode == 0


def python_tests() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromNames(["test_tx_model", "test_fake_vs_firmware"])
    try:
        import PySide6  # noqa: F401
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName("test_ui"))
    except ImportError:
        print("!! PySide6 not installed: skipping the app tests")
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


if __name__ == "__main__":
    print("== firmware (host build) ==", flush=True)
    ok = firmware_tests()
    print("\n== python ==")
    ok = python_tests() and ok
    print("\nALL PASSED" if ok else "\nFAILURES")
    sys.exit(0 if ok else 1)
