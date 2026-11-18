"""RX device->host byte stream: ASCII lines interleaved with CRC-protected binary frames.

Normative format: RX_Software_Spec.md §5.3. Pure Python + NumPy (no Qt).

    parser = FrameParser()
    for kind, item in parser.feed(data):      # kind is "line" (str) or "frame" (Frame)
        ...
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field

import numpy as np

MAGIC = b"\xA5\x5ARX"
VERSION = 1
HEADER = struct.Struct("<4sBBHIQHBBHHHH")      # 32 bytes
assert HEADER.size == 32
CRC = struct.Struct("<I")
MAX_N = 4096
MAX_LINE_BYTES = 1024

TYPE_STREAM, TYPE_CAPTURE = 1, 2
FLAG_CLIP = 0x0001
FLAG_OVERFLOW = 0x0002
FLAG_CAP_FIRST = 0x0004
FLAG_CAP_LAST = 0x0008
FLAG_FRAME_ERR = 0x0010
FLAG_SYNC = 0x0020
NO_OFFSET = 0xFFFF

CODE_MASK = 0x0FFF
FERR_BIT = 0x4000
SYNC_BIT = 0x8000


@dataclass
class Frame:
    version: int
    type: int
    flags: int
    seq: int
    first_index: int
    n: int
    gain: int                     # 0 = HIGH, 1 = LOW coarse gain
    pot: int
    cap_id: int
    trig_off: int
    sync_off: int
    fs_div100: int
    raw: np.ndarray = field(repr=False)   # uint16 sample words

    @property
    def codes(self) -> np.ndarray:
        return self.raw & CODE_MASK

    @property
    def sync(self) -> np.ndarray:
        return (self.raw & SYNC_BIT) != 0

    @property
    def fs_hz(self) -> int:
        return self.fs_div100 * 100

    @property
    def end_index(self) -> int:
        return self.first_index + self.n

    @property
    def gain_name(self) -> str:
        return "LOW" if self.gain else "HIGH"

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)


def encode_frame(raw, *, type: int = TYPE_STREAM, flags: int = 0, seq: int = 0, first_index: int = 0,
                 gain: int = 1, pot: int = 0, cap_id: int = 0, trig_off: int = NO_OFFSET,
                 sync_off: int = NO_OFFSET, fs_div100: int = 4000, version: int = VERSION) -> bytes:
    """Build one frame exactly as the firmware does (used by the fake device and the tests)."""
    raw = np.asarray(raw, dtype="<u2")
    n = int(raw.size)
    hdr = HEADER.pack(MAGIC, version, type, flags & 0xFFFF, seq & 0xFFFFFFFF, first_index, n, gain, pot,
                      cap_id, trig_off, sync_off, fs_div100)
    body = hdr + raw.tobytes()
    return body + CRC.pack(zlib.crc32(body))


def frame_flags(raw: np.ndarray, prev_sync: bool = False) -> tuple[int, int]:
    """(CLIP | FRAME_ERR | SYNC flags, sync_off) for a block of sample words, as the firmware computes."""
    codes = raw & CODE_MASK
    flags = 0
    if codes.size and (codes.min() <= 8 or codes.max() >= 4087):
        flags |= FLAG_CLIP
    if np.any(raw & FERR_BIT):
        flags |= FLAG_FRAME_ERR
    s = (raw & SYNC_BIT) != 0
    prev = np.concatenate(([prev_sync], s[:-1]))
    rising = np.flatnonzero(s & ~prev)
    sync_off = NO_OFFSET
    if rising.size:
        sync_off = int(rising[0])
        flags |= FLAG_SYNC
    return flags, sync_off


def _printable(b: bytes) -> bool:
    return all(0x20 <= c <= 0x7E for c in b)


# Every device line starts with one of these (spec §5.2).
RECORD_PREFIXES = (b"OK", b"ERR", b"WARN", b"STATUS", b"PONG", b"EVT")


def _salvage(raw_line: bytes) -> bytes | None:
    """After a corrupted frame, payload remnants can precede a real line before its '\\n'.
    Return the longest printable tail that starts with a record prefix, if any."""
    best = -1
    for p in RECORD_PREFIXES:
        i = raw_line.rfind(p)
        while i >= 0:
            if _printable(raw_line[i:]):
                if best < 0 or i < best:
                    best = i
                break
            i = raw_line.rfind(p, 0, i)
    return raw_line[best:] if best >= 0 else None


class FrameParser:
    """Incremental parser for the RX stream (normative behaviour in spec §5.3).

    Counters: frames, crc_errors, resync_bytes (bytes dropped while looking for a record start),
    garbage_lines (non-printable "lines", e.g. payload remnants after a CRC error).

    One addition to the normative algorithm: after a CRC or header error the parser is
    "resyncing" until the next valid frame. Meanwhile the bytes it walks through are the rest of
    the damaged frame, so a "line" is only emitted if it starts with a record prefix (OK, ERR,
    ...); a real line glued to payload remnants is salvaged, single printable payload bytes
    between 0x0A bytes are not reported as lines.
    """

    def __init__(self):
        self._buf = bytearray()
        self.frames = 0
        self.crc_errors = 0
        self.resync_bytes = 0
        self.garbage_lines = 0
        self._resyncing = False

    def reset(self):
        self._buf.clear()

    def feed(self, data: bytes) -> list[tuple[str, object]]:
        self._buf += data
        buf = self._buf
        out: list[tuple[str, object]] = []
        pos = 0
        end = len(buf)
        while pos < end:
            if buf[pos] == 0xA5:
                if end - pos < 4:
                    break
                if buf[pos:pos + 4] != MAGIC:
                    pos += 1
                    self.resync_bytes += 1
                    continue
                if end - pos < 32:
                    break
                (_, ver, typ, flags, seq, first, n, gain, pot, cap_id, trig_off, sync_off,
                 fs_div) = HEADER.unpack_from(buf, pos)
                if ver != VERSION or typ not in (TYPE_STREAM, TYPE_CAPTURE) or not 1 <= n <= MAX_N:
                    pos += 1
                    self.resync_bytes += 1
                    self._resyncing = True
                    continue
                total = 32 + 2 * n + 4
                if end - pos < total:
                    break
                body_end = pos + 32 + 2 * n
                crc = CRC.unpack_from(buf, body_end)[0]
                if zlib.crc32(memoryview(buf)[pos:body_end]) != crc:
                    self.crc_errors += 1
                    self.resync_bytes += 1
                    self._resyncing = True
                    pos += 1
                    continue
                self._resyncing = False
                raw = np.frombuffer(bytes(buf[pos + 32:body_end]), dtype="<u2").astype(np.uint16)
                out.append(("frame", Frame(ver, typ, flags, seq, first, n, gain, pot, cap_id, trig_off,
                                           sync_off, fs_div, raw)))
                self.frames += 1
                pos += total
            else:
                k = buf.find(b"\n", pos)
                j = buf.find(b"\xA5", pos)
                if k >= 0 and (j < 0 or k < j):
                    raw_line = bytes(buf[pos:k]).rstrip(b"\r")
                    pos = k + 1
                    if not raw_line:
                        continue
                    if self._resyncing or not _printable(raw_line):
                        good = _salvage(raw_line)
                        if good is not None:
                            self.resync_bytes += len(raw_line) - len(good)
                            self._resyncing = False
                            out.append(("line", good.decode("ascii")))
                        else:
                            self.garbage_lines += 1
                            self.resync_bytes += len(raw_line) + 1
                    else:
                        out.append(("line", raw_line.decode("ascii")))
                elif j >= 0:
                    self.resync_bytes += j - pos
                    pos = j
                else:
                    if end - pos > MAX_LINE_BYTES:
                        self.resync_bytes += end - pos
                        pos = end
                    break
        del buf[:pos]
        return out


def parse_kv(line: str) -> dict[str, str]:
    """'STATUS a=1 b=X' / 'EVT CAPDONE id=1 n=5' -> {'a': '1', 'b': 'X'}."""
    out = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out
