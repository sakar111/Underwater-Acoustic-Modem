"""rx_protocol: frame encode/parse, CRC, interleaving, byte-at-a-time, corruption (spec §9.1.2)."""
import unittest
import zlib

import numpy as np

import _paths  # noqa: F401
from bench import rx_protocol as P
from bench.sim_rx import BurstConfig, SimRxDevice


def make(seq, first, n=1024, **kw):
    raw = (np.arange(n, dtype=np.uint16) * 7 + seq) & 0x0FFF
    return raw, P.encode_frame(raw, seq=seq, first_index=first, **kw)


class TestFrames(unittest.TestCase):
    def test_header_layout(self):
        raw, b = make(5, 123456789012, n=3, type=P.TYPE_CAPTURE, flags=P.FLAG_CAP_FIRST, gain=0, pot=200,
                      cap_id=9, trig_off=1, sync_off=2)
        self.assertEqual(len(b), 32 + 6 + 4)
        self.assertEqual(b[:4], b"\xA5\x5ARX")
        self.assertEqual((b[4], b[5]), (1, 2))
        self.assertEqual(int.from_bytes(b[12:20], "little"), 123456789012)
        self.assertEqual(int.from_bytes(b[30:32], "little"), 4000)
        self.assertEqual(int.from_bytes(b[-4:], "little"), zlib.crc32(b[:-4]))

    def test_roundtrip(self):
        raw, b = make(7, 2048, gain=0, pot=12, type=P.TYPE_CAPTURE, cap_id=3, trig_off=17,
                      flags=P.FLAG_CAP_LAST)
        ev = P.FrameParser().feed(b)
        self.assertEqual(len(ev), 1)
        kind, f = ev[0]
        self.assertEqual(kind, "frame")
        self.assertEqual((f.seq, f.first_index, f.n, f.gain, f.pot, f.cap_id, f.trig_off, f.fs_hz),
                         (7, 2048, 1024, 0, 12, 3, 17, 400000))
        self.assertTrue(f.has(P.FLAG_CAP_LAST))
        self.assertTrue(np.array_equal(f.raw, raw))
        self.assertEqual(f.gain_name, "HIGH")

    def test_sync_and_codes(self):
        raw = np.array([2048, 2048 | 0x8000, 2049 | 0x8000, 0x4000 | 5], dtype=np.uint16)
        flags, off = P.frame_flags(raw)
        self.assertEqual(off, 1)
        self.assertTrue(flags & P.FLAG_SYNC and flags & P.FLAG_FRAME_ERR and flags & P.FLAG_CLIP)
        _, b = (None, P.encode_frame(raw, flags=flags, sync_off=off))
        f = P.FrameParser().feed(b)[0][1]
        self.assertEqual(f.codes.tolist(), [2048, 2048, 2049, 5])
        self.assertEqual(f.sync.tolist(), [False, True, True, False])
        self.assertEqual(P.frame_flags(raw, prev_sync=True)[1], 1)

    def stream(self):
        parts, expect = [], []
        for i in range(6):
            parts.append(f"OK LINE {i}\r\n".encode())
            expect.append(("line", f"OK LINE {i}"))
            raw, b = make(i, i * 1024)
            parts.append(b)
            expect.append(("frame", i))
        parts.append(b"EVT CAPDONE id=1 n=5\r\n")
        expect.append(("line", "EVT CAPDONE id=1 n=5"))
        return b"".join(parts), expect

    @staticmethod
    def norm(ev):
        return [(k, x.seq if k == "frame" else x) for k, x in ev]

    def test_interleaved_and_byte_at_a_time(self):
        data, expect = self.stream()
        self.assertEqual(self.norm(P.FrameParser().feed(data)), expect)
        p = P.FrameParser()
        ev = []
        for i in range(len(data)):
            ev += p.feed(data[i:i + 1])
        self.assertEqual(self.norm(ev), expect)
        self.assertEqual((p.crc_errors, p.resync_bytes, p.frames), (0, 0, 6))
        # random chunking gives the same result
        rng = np.random.default_rng(3)
        p = P.FrameParser()
        ev, i = [], 0
        while i < len(data):
            k = int(rng.integers(1, 3000))
            ev += p.feed(data[i:i + k])
            i += k
        self.assertEqual(self.norm(ev), expect)

    def test_flipped_payload_byte(self):
        data, expect = self.stream()
        b = bytearray(data)
        # corrupt the payload of frame seq=2
        start = data.index(make(2, 2048)[1])
        b[start + 32 + 100] ^= 0x10
        p = P.FrameParser()
        ev = self.norm(p.feed(bytes(b)))
        self.assertEqual(p.crc_errors, 1)
        self.assertNotIn(("frame", 2), ev)
        self.assertIn(("frame", 3), ev)                 # next frame still received
        self.assertIn(("line", "OK LINE 3"), ev)        # next text line still received
        self.assertIn(("line", "EVT CAPDONE id=1 n=5"), ev)
        self.assertEqual(sum(1 for k, _ in ev if k == "frame"), 5)

    def test_garbage_before_frame(self):
        raw, fr = make(0, 0)
        junk = bytes([0x00, 0xFF, 0xA5, 0x01, 0xA5, 0x5A, 0x52, 0x00, 0x13]) + b"\xA5\x5ARX\x07"
        ev = P.FrameParser().feed(junk + fr + b"OK X\n")
        self.assertEqual(self.norm(ev)[-2:], [("frame", 0), ("line", "OK X")])

    def test_garbled_text_and_long_garbage(self):
        p = P.FrameParser()
        ev = p.feed(b"\x01\x02\x03\n" + b"OK A\n")
        self.assertEqual(ev, [("line", "OK A")])
        self.assertEqual(p.garbage_lines, 1)
        ev = p.feed(b"x" * 2000)
        self.assertEqual(ev, [])
        self.assertEqual(p.feed(b"\nOK B\n"), [("line", "OK B")])

    def test_partial_frame_waits(self):
        raw, b = make(1, 1024)
        p = P.FrameParser()
        self.assertEqual(p.feed(b[:40]), [])
        self.assertEqual(p.feed(b[40:-1]), [])
        self.assertEqual(self.norm(p.feed(b[-1:])), [("frame", 1)])

    def test_bad_header_resync(self):
        raw, b = make(1, 1024)
        bad = bytearray(b)
        bad[4] = 9                                       # unknown version
        ev = P.FrameParser().feed(bytes(bad) + b + b"OK\n")
        self.assertEqual(self.norm(ev)[-2:], [("frame", 1), ("line", "OK")])

    def test_parse_kv(self):
        self.assertEqual(P.parse_kv("EVT TRIG id=3 index=60002"), {"id": "3", "index": "60002"})


class TestFakeDeviceStream(unittest.TestCase):
    """10 s of streaming from the fake device parses with no gaps and no CRC errors."""

    def test_ten_seconds(self):
        dev = SimRxDevice(burst=BurstConfig(amp_v=1e-3))
        p = P.FrameParser()
        p.feed(dev.feed("ARM\nSTREAM ON\n"))
        last = None
        n = 0
        for _ in range(200):
            for k, f in p.feed(dev.advance_ms(50)):
                if k != "frame":
                    continue
                if last is not None:
                    self.assertEqual(f.first_index, last.first_index + 1024)
                    self.assertEqual(f.seq, last.seq + 1)
                last = f
                n += 1
        self.assertEqual(p.crc_errors, 0)
        self.assertGreaterEqual(n, 3900)

    def test_faults(self):
        dev = SimRxDevice()
        dev.crc_every, dev.garbage_every, dev.drop_every = 5, 7, 11
        p = P.FrameParser()
        p.feed(dev.feed("ARM\nSTREAM ON\n"))
        ev = p.feed(dev.advance_ms(200))
        ev += p.feed(dev.feed("STATUS\n"))
        self.assertGreater(p.crc_errors, 0)
        self.assertGreater(p.resync_bytes, 0)
        self.assertTrue(any(k == "line" and x.startswith("STATUS armed=1") for k, x in ev))
        firsts = [x.first_index for k, x in ev if k == "frame"]
        self.assertEqual(firsts, sorted(firsts))

    def test_stall_sets_overflow(self):
        dev = SimRxDevice()
        p = P.FrameParser()
        p.feed(dev.feed("ARM\nSTREAM ON\n"))
        p.feed(dev.advance_ms(10))
        dev.stall(300)
        frames = [x for k, x in p.feed(dev.advance_ms(20)) if k == "frame"]
        self.assertTrue(frames[0].has(P.FLAG_OVERFLOW))
        self.assertFalse(any(f.has(P.FLAG_OVERFLOW) for f in frames[1:]))


if __name__ == "__main__":
    unittest.main()
