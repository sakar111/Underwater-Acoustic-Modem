"""Pure-Python fake RX Teensy (spec Appendix B.2).

Speaks the rx_firmware.ino protocol: same commands, same reply texts, same binary frames, same
overflow logic and the same 256-sample main-loop granularity as the host build, so
tests/test_rx_fake_vs_firmware.py can compare it with the real firmware line by line.

    dev = SimRxDevice()
    out_bytes = dev.feed(b"ARM\\n")           # command bytes in -> reply bytes out
    out_bytes = dev.advance_samples(4000)    # let 10 ms of sampling pass -> lines + frames

The signal generator models the board: noise around mid-scale plus optional tone bursts given
in volts at the hydrophone, converted to ADC codes with the §3.6 gain model at the current
GAIN/POT setting, and a SYNC pulse at each burst start with the tone arriving `delay_samples` later.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import rx_protocol as P
from .rx_model import FS_HZ, LSB_V, RxGain

RING_N = 65536
BLOCK = 1024
FRAMES_PER_LOOP = 8
LOOP_EVERY = 256                  # samples between main-loop passes (as in the host build)
MID_MIN, MID_MAX = 1648, 2448
CAP_N_MIN, CAP_N_MAX, CAP_PRE_MAX = 16, 4_000_000, 32768
SYNC_PULSE_SAMPLES = 7            # TX pulse is 16 us = 6.4 samples
RAMP_SAMPLES = 40                 # TX 100 us amplitude ramp

MSG_NOPOWER = "ERR NOPOWER (AD5245 not answering: turn the RX board 12 V on, check LED D6, then ARM)"
MSG_POWERLOST = "WARN POWERLOST RX board stopped answering: disarmed, pins released"


@dataclass
class BurstConfig:
    freq_hz: float = 40e3
    on_us: float = 1000.0
    period_us: float = 100_000.0
    offset_us: float = 1000.0
    amp_v: float = 1e-3               # peak volts at the hydrophone terminals
    delay_samples: int = 135          # acoustic delay after the sync edge (0.5 m)
    sync_wire: bool = True
    continuous: bool = False          # True: a continuous tone, no bursts


def _parse_uint(s: str | None) -> int | None:
    if not s or not s.isdigit() or not s.isascii():
        return None
    v = int(s)
    return v if v <= 0xFFFFFFFF else None


class SimRxDevice:
    MAX_LINE = 90

    def __init__(self, seed: int = 1, noise: float = 2.0, dc: float = 2048.0,
                 burst: BurstConfig | None = None, version: str = "1.0-fake"):
        # board / environment
        self.board_powered = True
        self.pot_reg = 128                 # AD5245 power-on default
        self.dtr = True
        self.version = version
        self.seed = seed
        self.noise = noise
        self.dc = dc
        self.burst = burst
        self.signal_fn = None              # optional override: f(start, n) -> codes (int array)
        # fault injection (Appendix B.2)
        self.crc_every = 0                 # flip a payload byte in every N-th frame
        self.garbage_every = 0             # insert garbage bytes before every N-th frame
        self.drop_every = 0                # drop every N-th frame (gap)
        self.pong_as_tx = False            # answer PING like the TX board (wrong-port test)
        self.commands: list[str] = []      # every command received
        self._rng_fault = np.random.default_rng(seed + 1000)
        self._noise_cache: dict[int, np.ndarray] = {}
        # firmware state
        self.armed = False
        self.gain_low = True
        self.pot = 0
        self.trig_src = "NONE"
        self.trig_level = 200
        self.mid = 0
        self.streaming = False
        self.cap_state = "IDLE"
        self.cap_id = 0
        self.cap_n = self.cap_pre = 0
        self.cap_trig = self.cap_end = 0
        self._cap_first = False
        self._trig_wait = False
        self._trig_hit: int | None = None
        self._trig_earliest = 0
        self._trig_mid = 0
        self._trig_level_active = 200
        self._trig_src_active = "NONE"
        self._last_sync = False
        self.head = 0
        self.send_pos = 0
        self._pending_overflow = False
        self.seq = 0
        self.frames_total = 0
        self.ovf_total = 0
        self._stat_frames = 0
        self._stat_ovf = 0
        self._timer_on = False
        self._primed = False
        self._per_seen = False
        self._isr_seen = False
        self._pot_fail = 0
        self._pot_hw = -1
        self._last_chk = 0
        self._last_hb = 0
        self.ticks = 0                     # simulated sample periods (whether or not sampling)
        self._phase = 0                    # samples since the last loop() pass
        self._conv = 0                     # ADC conversion counter (RAW low bit, as the host stub)
        self._rx = ""
        self._rx_overflow = False
        self._frame_counter = 0            # for fault injection

    # ================================================================ signal model
    @property
    def ms(self) -> int:
        return self.ticks // 400

    def gain_model(self) -> RxGain:
        return RxGain(coarse="LOW" if self.gain_low else "HIGH", pot=self.pot)

    def _gain_at(self, f: float) -> float:
        key = (self.gain_low, self.pot, f)
        if getattr(self, "_gain_key", None) != key:
            self._gain_key, self._gain_val = key, self.gain_model().total_mag(f)
        return self._gain_val

    def _noise(self, start: int, n: int) -> np.ndarray:
        if self.noise <= 0:
            return np.zeros(n)
        out = np.empty(n)
        i = 0
        while i < n:
            idx = start + i
            b, off = divmod(idx, BLOCK)
            blk = self._noise_cache.get(b)
            if blk is None:
                if len(self._noise_cache) > 512:
                    self._noise_cache.clear()
                blk = np.random.default_rng((self.seed, b)).normal(0, self.noise, BLOCK)
                self._noise_cache[b] = blk
            k = min(BLOCK - off, n - i)
            out[i:i + k] = blk[off:off + k]
            i += k
        return out

    def words(self, start: int, n: int) -> np.ndarray:
        """Sample words (code | sync bit 15) for sample indices [start, start + n)."""
        if n <= 0:
            return np.zeros(0, dtype=np.uint16)
        if not self.board_powered:
            return np.zeros(n, dtype=np.uint16)          # ADC unpowered
        if self.signal_fn is not None:
            codes = np.asarray(self.signal_fn(start, n), dtype=float)
            sync = np.zeros(n, dtype=bool)
        else:
            codes = self.dc + self._noise(start, n)
            sync = np.zeros(n, dtype=bool)
            bc = self.burst
            if bc is not None and bc.amp_v > 0:
                k = np.arange(start, start + n)
                amp_counts = bc.amp_v * self._gain_at(bc.freq_hz) / LSB_V
                if bc.continuous:
                    codes = codes + amp_counts * np.sin(2 * np.pi * bc.freq_hz * k / FS_HZ)
                else:
                    per = max(int(round(bc.period_us * 0.4)), 1)
                    off = int(round(bc.offset_us * 0.4))
                    on = max(int(round(bc.on_us * 0.4)), 1)
                    pos = (k - off) % per
                    started = k >= off
                    if bc.sync_wire:
                        sync = started & (pos < SYNC_PULSE_SAMPLES)
                    m = pos - bc.delay_samples
                    active = started & (m >= 0) & (m < on + RAMP_SAMPLES)
                    env = np.clip(np.minimum((m + 1) / RAMP_SAMPLES, (on + RAMP_SAMPLES - m) / RAMP_SAMPLES), 0, 1)
                    tone = np.sin(2 * np.pi * bc.freq_hz * m / FS_HZ) * env
                    codes = codes + np.where(active, amp_counts * tone, 0.0)
        codes = np.clip(np.rint(codes), 0, 4095).astype(np.uint16)
        return codes | np.where(sync, P.SYNC_BIT, 0).astype(np.uint16)

    # ================================================================ byte I/O
    def feed(self, data: bytes | str) -> bytes:
        """Command bytes from the host -> reply bytes (lines, possibly frames)."""
        if isinstance(data, bytes):
            data = data.decode("latin-1")
        out = bytearray()
        for ch in data:
            if ch in "\r\n":
                if self._rx_overflow:
                    out += self._line("ERR line too long")
                elif self._rx:
                    out += self.handle(self._rx)
                self._rx = ""
                self._rx_overflow = False
            elif len(self._rx) < self.MAX_LINE:
                self._rx += ch
            else:
                self._rx_overflow = True
        return bytes(out)

    def advance_samples(self, n: int) -> bytes:
        """Let n sample periods pass; loop() runs every 256 samples as in the host build."""
        out = bytearray()
        n = int(n)
        while n > 0:
            k = min(n, LOOP_EVERY - self._phase)
            self._tick(k)
            self._phase += k
            n -= k
            if self._phase >= LOOP_EVERY:
                self._phase = 0
                out += self._loop()
        return bytes(out)

    def advance_ms(self, ms: float) -> bytes:
        return self.advance_samples(int(round(ms * 400)))

    def stall(self, ms: float):
        """The PC stops reading for `ms`: samples keep coming, nothing is sent (overflow test)."""
        n = int(round(ms * 400))
        self._tick(n)

    def power_off(self):
        self.board_powered = False

    def power_on(self):
        self.board_powered = True

    @staticmethod
    def _line(s: str) -> bytes:
        return (s + "\r\n").encode("ascii")

    # ================================================================ "ISR"
    def _tick(self, k: int):
        self.ticks += k
        if not self._timer_on or k <= 0:
            return
        h0 = self.head
        if self._trig_wait and self._trig_hit is None:
            a = max(h0, self._trig_earliest)
            if a < h0 + k:
                w = self.words(a, h0 + k - a)
                codes = (w & P.CODE_MASK).astype(int)
                if self._trig_src_active == "LEVEL":
                    hits = np.flatnonzero(np.abs(codes - self._trig_mid) >= self._trig_level_active)
                elif self._trig_src_active == "SYNC":
                    s = (w & P.SYNC_BIT) != 0
                    prev_first = self._sync_at(a - 1) if a > h0 else self._last_sync
                    prev = np.concatenate(([prev_first], s[:-1]))
                    hits = np.flatnonzero(s & ~prev)
                else:
                    hits = np.array([0])
                if hits.size:
                    self._trig_hit = a + int(hits[0])
        self.head = h0 + k
        self._last_sync = self._sync_at(self.head - 1)
        if self.board_powered:
            self._conv += k
        if self._primed or k >= 2:
            self._per_seen = True
        self._primed = True
        self._isr_seen = True

    def _sync_at(self, idx: int) -> bool:
        bc = self.burst
        if (idx < 0 or self.signal_fn is not None or bc is None or not bc.sync_wire or bc.continuous
                or bc.amp_v <= 0 or not self.board_powered):
            return False
        return bool(self.words(idx, 1)[0] & P.SYNC_BIT)

    def _start_timer(self):
        self._primed = False
        self._last_sync = False
        self._timer_on = True

    def _reset_stats(self):
        self._per_seen = False
        self._isr_seen = False
        self._stat_frames = 0
        self._stat_ovf = 0

    # ================================================================ main loop
    def _busy(self) -> bool:
        return self.streaming or self.cap_state != "IDLE"

    def _loop(self) -> bytes:
        out = bytearray()
        if self.armed:
            if self.streaming:
                out += self._service_stream()
            elif self.cap_state != "IDLE":
                out += self._service_capture()
        if self._busy() and not self.dtr:
            self._stop_acq()
        now = self.ms
        if self.armed and now - self._last_chk >= 500:
            self._last_chk = now
            out += self._check_board()
        return bytes(out)

    def _stop_acq(self):
        self.streaming = False
        self.cap_state = "IDLE"
        self._trig_wait = False
        self._trig_hit = None

    def _release_all(self):
        self._timer_on = False
        self._stop_acq()
        self.armed = False
        self._pot_hw = -1

    def _pot_write(self, d: int) -> bool:
        if not self.board_powered:
            return False
        self.pot_reg = d
        self._pot_hw = d
        return True

    def _check_board(self) -> bytes:
        if not self.board_powered:
            self._pot_fail += 1
            if self._pot_fail >= 2:
                self._release_all()
                return self._line(MSG_POWERLOST)
            return b""
        self._pot_fail = 0
        if self._pot_hw >= 0 and self.pot_reg != self._pot_hw:
            d, want = self.pot_reg, self._pot_hw
            self._pot_write(want)
            return self._line(f"WARN POTRESET wiper read {d} expected {want} (board supply glitch?): rewritten")
        return b""

    def _check_overflow(self) -> bool:
        if self.head - self.send_pos > RING_N - 2 * BLOCK:
            to = self.head - BLOCK
            lost = to - self.send_pos
            self.ovf_total += lost
            self._stat_ovf += lost
            self.send_pos = to
            self._pending_overflow = True
            return True
        return False

    def _frame(self, typ: int, start: int, n: int, flags: int) -> bytes:
        w = self.words(start, n)
        prev = start > 0 and self._sync_at(start - 1)
        f2, sync_off = P.frame_flags(w, prev)
        flags |= f2
        trig_off = P.NO_OFFSET
        if typ == P.TYPE_CAPTURE and 0 <= self.cap_trig - start < n:
            trig_off = self.cap_trig - start
        if self._pending_overflow:
            flags |= P.FLAG_OVERFLOW
            self._pending_overflow = False
        data = P.encode_frame(w, type=typ, flags=flags, seq=self.seq, first_index=start,
                              gain=1 if self.gain_low else 0, pot=self.pot,
                              cap_id=self.cap_id if typ == P.TYPE_CAPTURE else 0,
                              trig_off=trig_off, sync_off=sync_off)
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        self.frames_total += 1
        self._stat_frames += 1
        self._frame_counter += 1
        c = self._frame_counter
        if self.drop_every and c % self.drop_every == 0:
            return b""
        if self.crc_every and c % self.crc_every == 0:
            b = bytearray(data)
            i = 32 + int(self._rng_fault.integers(0, 2 * n))
            b[i] ^= 0x5A
            data = bytes(b)
        if self.garbage_every and c % self.garbage_every == 0:
            junk = bytes(self._rng_fault.integers(0, 256, 23, dtype=np.uint8)) + b"\xA5\x00\xA5\x5A"
            data = junk + data
        return data

    def _service_stream(self) -> bytes:
        out = bytearray()
        for _ in range(FRAMES_PER_LOOP):
            if not self.streaming:
                break
            self._check_overflow()
            if self.head - self.send_pos < BLOCK:
                break
            out += self._frame(P.TYPE_STREAM, self.send_pos, BLOCK, 0)
            self.send_pos += BLOCK
        return bytes(out)

    def _service_capture(self) -> bytes:
        out = bytearray()
        if self.cap_state == "WAIT":
            if self._trig_hit is None:
                return b""
            self.cap_trig = self._trig_hit
            self.send_pos = self.cap_trig - self.cap_pre
            self.cap_end = self.send_pos + self.cap_n
            self._cap_first = True
            self._pending_overflow = False
            self.cap_state = "RUN"
            out += self._line(f"EVT TRIG id={self.cap_id} index={self.cap_trig}")
        for _ in range(FRAMES_PER_LOOP):
            if self.cap_state != "RUN":
                break
            if self._check_overflow():
                if self.cap_end - self.send_pos < 0:
                    self.send_pos = self.cap_end
                out += self._line(f"WARN OVERFLOW capture id={self.cap_id} lost samples (PC not reading fast enough)")
            remaining = self.cap_end - self.send_pos
            n = min(remaining, BLOCK)
            if n > 0:
                if self.head - self.send_pos < n:
                    break
                fl = 0
                if self._cap_first:
                    fl |= P.FLAG_CAP_FIRST
                    self._cap_first = False
                if n == remaining:
                    fl |= P.FLAG_CAP_LAST
                out += self._frame(P.TYPE_CAPTURE, self.send_pos, n, fl)
                self.send_pos += n
            if self.send_pos == self.cap_end:
                out += self._line(f"EVT CAPDONE id={self.cap_id} n={self.cap_n} pre={self.cap_pre} trig={self.cap_trig}")
                self.cap_state = "IDLE"
                self._trig_wait = False
                self._trig_hit = None
        return bytes(out)

    # ================================================================ commands
    def _arm(self) -> str:
        if self.armed:
            return f"OK ARMED mid={self.mid}"
        if not self.board_powered:
            self._release_all()
            return MSG_NOPOWER
        if not self._pot_write(self.pot):
            self._release_all()
            return "ERR NOPOT (AD5245 did not acknowledge the write)"
        w = self.words(self.head, 64)
        self._conv += 66
        mean = (int((w & P.CODE_MASK).astype(int).sum()) + 32) // 64
        if not (MID_MIN <= mean <= MID_MAX):
            self._release_all()
            return (f"ERR ADC self-test failed: mean={mean} frame_errors=0 (expect {MID_MIN}..{MID_MAX}, 0; "
                    "check 5 V, 2V5, SPI wiring, MISO pin 39, ADC_SHIFT)")
        self.mid = mean
        self.head = 0
        self.seq = 0
        self.frames_total = 0
        self.ovf_total = 0
        self._reset_stats()
        self._stop_acq()
        self._pending_overflow = False
        self.armed = True
        self._pot_fail = 0
        self._last_chk = self.ms
        self._start_timer()
        return f"OK ARMED mid={self.mid}"

    def status_line(self) -> str:
        if self.armed:
            power = 1 if self._busy() else int(self.board_powered)
        else:
            power = int(self.board_powered)
        sync = int(self._sync_at(self.head)) if self.armed else 0
        return (f"STATUS armed={int(self.armed)} power={power} gain={'LOW' if self.gain_low else 'HIGH'} "
                f"pot={self.pot} fs=400000 stream={int(self.streaming)} cap={self.cap_state} "
                f"trig={self.trig_src} level={self.trig_level} mid={self.mid} sync={sync} "
                f"ovf={self.ovf_total} ferr=0")

    def handle(self, line: str) -> bytes:
        line = line.strip()
        toks = line.upper().split()
        if not toks:
            return b""
        self.commands.append(line)
        c = toks[0]
        a1 = toks[1] if len(toks) > 1 else None
        a2 = toks[2] if len(toks) > 2 else None
        L = self._line
        busy = self._busy()

        if c == "PING":
            return L(f"PONG TXFW 1.1" if self.pong_as_tx else f"PONG RXFW {self.version}")
        if c == "VERSION":
            return L(f"OK VERSION RXFW {self.version} fs=400000 adc=ADC121S101 pot=AD5245 shift=1")
        if c == "HELP":
            return L("OK commands: PING VERSION ARM DISARM STATUS GAIN POT POTREAD TRIG CAPTURE ABORT "
                     "STREAM RAW STATS")
        if c == "STATUS":
            return L(self.status_line())
        if c == "ARM":
            return L(self._arm())
        if c == "DISARM":
            self._release_all()
            return L("OK DISARMED")
        if c == "GAIN":
            if a1 == "HIGH":
                self.gain_low = False
            elif a1 == "LOW":
                self.gain_low = True
            else:
                return L("ERR GAIN needs HIGH or LOW")
            return L("OK GAIN LOW" if self.gain_low else "OK GAIN HIGH")
        if c == "POT":
            if a1 is None:
                return L("ERR POT needs a value")
            d = _parse_uint(a1)
            if d is None or d > 255:
                return L("ERR POT out of range 0..255")
            if self.armed and not self._pot_write(d):
                return L("ERR NOPOT (I2C write failed)")
            self.pot = d
            return L(f"OK POT {d}")
        if c == "POTREAD":
            if not self.armed:
                return L("ERR NOTARMED")
            if not self.board_powered:
                return L("ERR NOPOT (I2C read failed)")
            return L(f"OK POTREAD {self.pot_reg}")
        if c == "TRIG":
            if self.cap_state != "IDLE":
                return L("ERR BUSY capture in progress (ABORT first)")
            if a1 == "NONE":
                self.trig_src = "NONE"
                return L("OK TRIG NONE")
            if a1 == "SYNC":
                self.trig_src = "SYNC"
                return L("OK TRIG SYNC")
            if a1 == "LEVEL":
                if a2 is None:
                    return L("ERR TRIG LEVEL needs <counts>")
                v = _parse_uint(a2)
                if v is None or not 1 <= v <= 2047:
                    return L("ERR TRIG LEVEL out of range 1..2047")
                self.trig_src, self.trig_level = "LEVEL", v
                return L(f"OK TRIG LEVEL {v}")
            return L("ERR TRIG needs NONE, LEVEL <counts> or SYNC")
        if c == "CAPTURE":
            if not self.armed:
                return L("ERR NOTARMED")
            if self.streaming:
                return L("ERR BUSY streaming (STREAM OFF first)")
            if self.cap_state != "IDLE":
                return L("ERR BUSY capture in progress (ABORT first)")
            if a1 is None:
                return L("ERR CAPTURE needs <n> [pre]")
            n = _parse_uint(a1)
            if n is None or not CAP_N_MIN <= n <= CAP_N_MAX:
                return L("ERR CAPTURE n out of range 16..4000000")
            pre_max = min(n - 1, CAP_PRE_MAX)
            pre = 0
            if a2 is not None:
                pre = _parse_uint(a2)
                if pre is None or pre > pre_max:
                    return L(f"ERR CAPTURE pre out of range 0..{pre_max}")
            self.cap_id = (self.cap_id + 1) & 0xFFFF or 1
            self.cap_n, self.cap_pre = n, pre
            self._trig_src_active = self.trig_src
            self._trig_level_active = self.trig_level
            self._trig_mid = self.mid
            self._trig_earliest = self.head + pre + 1
            self._trig_wait = True
            self._trig_hit = None
            self.cap_state = "WAIT"
            return L(f"OK CAPTURE id={self.cap_id}")
        if c == "ABORT":
            self.cap_state = "IDLE"
            self._trig_wait = False
            self._trig_hit = None
            return L("OK ABORT")
        if c == "STREAM":
            if a1 == "OFF":
                self.streaming = False
                return L("OK STREAM OFF")
            if a1 == "ON":
                if not self.armed:
                    return L("ERR NOTARMED")
                if self.cap_state != "IDLE":
                    return L("ERR BUSY capture in progress (ABORT first)")
                if not self.streaming:
                    self.send_pos = self.head & ~(BLOCK - 1)
                    self._pending_overflow = False
                    self.streaming = True
                return L("OK STREAM ON")
            return L("ERR STREAM needs ON or OFF")
        if c == "RAW":
            if not self.armed:
                return L("ERR NOTARMED")
            if busy:
                return L("ERR BUSY (STREAM OFF / ABORT first)")
            k = 8
            if a1 is not None:
                k = _parse_uint(a1)
                if k is None or not 1 <= k <= 16:
                    return L("ERR RAW out of range 1..16")
            w = self.words(self.head, k) & P.CODE_MASK
            parts = []
            for i in range(k):
                if self.board_powered:
                    self._conv += 1
                    parts.append(f"{(int(w[i]) << 1) | (self._conv & 1):04X}")
                else:
                    parts.append("0000")
            self._start_timer()
            return L("OK RAW " + " ".join(parts))
        if c == "STATS":
            per = 2500 if self._per_seen else 0
            isr = 1250 if self._isr_seen else 0
            s = (f"OK STATS frames={self._stat_frames} ovf={self._stat_ovf} ferr=0 per_min_ns={per} "
                 f"per_max_ns={per} isr_max_ns={isr}")
            self._reset_stats()
            return L(s)
        return L(f"ERR unknown command: {c[:40]}")


def tone_burst_volts_for_adc_vpp(adc_vpp: float, freq_hz: float, gain: RxGain) -> float:
    """Hydrophone peak volts that give `adc_vpp` at the ADC (helper for demos/tests)."""
    return adc_vpp / 2 / gain.total_mag(freq_hz)


__all__ = ["SimRxDevice", "BurstConfig", "tone_burst_volts_for_adc_vpp", "RING_N", "BLOCK"]
