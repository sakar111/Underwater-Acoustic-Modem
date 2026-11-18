"""Run every test that does not need hardware.

    python tests/run_all.py

1. builds tx_firmware.ino and rx_firmware.ino against their host stubs (g++) and runs the
   scenario checks (TX: waveform, bursts, limits, ARM/DISARM, watchdog, SYNC output;
   RX: power check, self-test, streaming, triggered captures, overflow, watchdogs). The RX run
   writes its byte streams to build/rx_out_*.bin for test_rx_firmware_stream.
2. Python unit tests: TX level model + safe ordering, fake-vs-firmware cross-checks (TX and RX),
   RX protocol / gain model / analysis / save-load, RX firmware streams parsed by the app parser,
   and offscreen app tests against the simulated Teensys (needs PySide6, pyqtgraph, numpy).
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


def build_and_run(name: str, stub_dir: str, src: str, *args) -> bool:
    gxx = shutil.which("g++")
    exe = os.path.join(_paths.BUILD_DIR, name + (".exe" if os.name == "nt" else ""))
    subprocess.run([gxx, "-std=c++17", "-O1", "-Wall", "-Wextra", "-I", os.path.join(HERE, stub_dir),
                    "-o", exe, os.path.join(HERE, src)], check=True)
    return subprocess.run([exe, *args]).returncode == 0


def firmware_tests() -> bool:
    if not shutil.which("g++"):
        print("!! g++ not found: skipping the firmware host builds (install MSYS2 / MinGW-w64)")
        return True
    os.makedirs(_paths.BUILD_DIR, exist_ok=True)
    ok = build_and_run("sim", "host_stubs", "sim_main.cpp")
    ok = build_and_run("rx_sim", "host_stubs_rx", "rx_sim_main.cpp", _paths.BUILD_DIR) and ok
    return ok


def python_tests() -> bool:
    names = ["test_tx_model", "test_fake_vs_firmware", "test_rx_fake_vs_firmware", "test_rx_firmware_stream"]
    try:
        import numpy  # noqa: F401
        names += ["test_rx_protocol", "test_rx_model", "test_rx_analysis"]
    except ImportError:
        print("!! numpy not installed: skipping the RX Python tests")
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    try:
        import PySide6  # noqa: F401
        import pyqtgraph  # noqa: F401
        suite.addTests(unittest.defaultTestLoader.loadTestsFromNames(["test_ui", "test_rx_ui"]))
    except ImportError:
        print("!! PySide6 / pyqtgraph not installed: skipping the app tests")
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


if __name__ == "__main__":
    print("== firmware (host builds) ==", flush=True)
    ok = firmware_tests()
    print("\n== python ==")
    ok = python_tests() and ok
    print("\nALL PASSED" if ok else "\nFAILURES")
    sys.exit(0 if ok else 1)
