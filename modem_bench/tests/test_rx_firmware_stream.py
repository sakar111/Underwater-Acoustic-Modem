"""Parse the byte streams written by the RX firmware host build (rx_sim_main.cpp) with the app's
Python parser: proves firmware frames and the app parser agree (CRC = zlib.crc32, layout, flags).

Run tests/run_all.py (or build/rx_sim build) first; skipped if the streams are missing.
"""
import os
import unittest

import numpy as np

import _paths
from bench import rx_protocol as P


def load(name):
    path = os.path.join(_paths.BUILD_DIR, name)
    if not os.path.exists(path):
        raise unittest.SkipTest(f"{path} missing: run the RX firmware host build first")
    with open(path, "rb") as f:
        data = f.read()
    p = P.FrameParser()
    ev = p.feed(data)
    return p, [x for k, x in ev if k == "line"], [x for k, x in ev if k == "frame"]


def sig(k):
    k = np.asarray(k)
    burst = (k >= 60000) & (k < 60800)
    return np.where(burst, 2050 + np.rint(400 * np.sin(2 * np.pi * 40e3 * (k - 60000) / 400e3)), 2050).astype(int)


class TestFirmwareStreams(unittest.TestCase):
    def test_stream(self):
        p, lines, frames = load("rx_out_stream.bin")
        self.assertEqual((p.crc_errors, p.resync_bytes), (0, 0))
        self.assertEqual(len(frames), 20)
        self.assertIn("OK STREAM OFF", lines)
        for a, b in zip(frames, frames[1:]):
            self.assertEqual(b.first_index, a.end_index)
            self.assertEqual(b.seq, a.seq + 1)
        self.assertTrue(all((f.codes == 2050).all() and f.type == P.TYPE_STREAM and f.gain_name == "LOW" for f in frames))

    def test_level_capture(self):
        p, lines, frames = load("rx_out_level.bin")
        self.assertEqual(p.crc_errors, 0)
        self.assertIn("EVT TRIG id=1 index=60002", lines)
        self.assertIn("EVT CAPDONE id=1 n=5000 pre=1000 trig=60002", lines)
        codes = np.concatenate([f.codes for f in frames])
        self.assertEqual(codes.size, 5000)
        self.assertTrue(np.array_equal(codes, sig(np.arange(59002, 64002))))
        self.assertTrue(frames[0].has(P.FLAG_CAP_FIRST) and frames[-1].has(P.FLAG_CAP_LAST))
        self.assertEqual(frames[0].trig_off, 1000)

    def test_sync_capture(self):
        p, lines, frames = load("rx_out_sync.bin")
        self.assertIn("EVT TRIG id=2 index=80000", lines)
        self.assertEqual(frames[0].first_index, 79900)
        self.assertEqual(frames[0].sync_off, 100)
        self.assertEqual(int(sum(f.sync.sum() for f in frames)), 8)

    def test_overflow(self):
        p, lines, frames = load("rx_out_overflow.bin")
        self.assertTrue(frames[0].has(P.FLAG_OVERFLOW))
        self.assertEqual(sum(f.has(P.FLAG_OVERFLOW) for f in frames), 1)
        for a, b in zip(frames, frames[1:]):
            self.assertEqual(b.first_index, a.end_index)


if __name__ == "__main__":
    unittest.main()
