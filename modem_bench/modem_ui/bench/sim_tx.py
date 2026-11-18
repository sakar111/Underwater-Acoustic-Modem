"""Pure-Python simulated TX Teensy.

Implements the same ASCII protocol and safety logic as tx_firmware.ino (minus the
sample engine), so the app can be exercised without hardware. tests/
test_fake_vs_firmware.py runs the same command script through this class and
through a host build of the real firmware and requires identical replies.
"""
from __future__ import annotations

import math

from .tx_model import (ABS_MAX_VPP, CAL_MAX, CAL_MIN, MAX_DAC_MVPK, THERMAL_FVPP,
                       est_vpp)

VERSION = "1.2"
_EPS = 1e-3
_THERMAL_WARN = ("WARN THERMAL continuous f*Vpp above ~2.3e6: power amps may overheat; "
                 "use BURST or lower level")


def _lround(x: float) -> int:
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


def _atof(s: str) -> float:
    """C atof(): parse the longest numeric prefix, 0.0 if none."""
    best = 0.0
    for i in range(len(s), 0, -1):
        try:
            return float(s[:i])
        except ValueError:
            continue
    return best


def _atol(s: str) -> int:
    return int(_atof(s)) if s else 0


class SimTxTeensy:
    MAX_LINE = 90

    def __init__(self):
        self.board_powered = True     # 12 V supply on (I2C pull-ups high)
        self.pot_reg = 128            # AD5245 power-on default
        self.ms = 0
        self.armed = False
        self.running = False
        self.burst = False
        self.freq = 25000.0
        self.amp = 200.0
        self.pot = 255
        self.limit = 20.0
        self.cal = 1.0
        self.on_us = 1000
        self.per_us = 100000
        self.sweep_on = False
        self.sweep = (20000.0, 90000.0, 100)
        self.thermal_stop_s = 60
        self.sync_on = False
        self._thermal_active = False
        self._thermal_t0 = 0
        self._last_chk = 0
        self._pot_hw = -1
        self._pot_fail = 0
        self._rx = ""
        self._rx_overflow = False

    # ------------------------------------------------------------------ helpers
    def est(self, amp=None, pot=None, cal=None) -> float:
        return est_vpp(self.amp if amp is None else amp, self.pot if pot is None else pot,
                       self.cal if cal is None else cal)

    def _max_freq(self) -> float:
        return max(self.sweep[0], self.sweep[1]) if self.sweep_on else self.freq

    def _thermal_risk(self) -> bool:
        return (not self.burst) and self._max_freq() * self.est() > THERMAL_FVPP

    def _pot_write(self, d: int) -> bool:
        if not self.board_powered:
            return False
        self.pot_reg = d
        self._pot_hw = d
        return True

    def _set_running(self, r: bool):
        self.running = r
        if not r:
            self._thermal_active = False

    # ------------------------------------------------------------------ I/O
    def feed(self, data: str) -> list[str]:
        """Feed raw serial characters; return reply lines."""
        out: list[str] = []
        for ch in data:
            if ch in "\r\n":
                if self._rx_overflow:
                    out.append("ERR line too long")
                elif self._rx:
                    out += self.handle(self._rx)
                self._rx = ""
                self._rx_overflow = False
            elif len(self._rx) < self.MAX_LINE:
                self._rx += ch
            else:
                self._rx_overflow = True
        return out

    def advance(self, ms: int) -> list[str]:
        """Run the main-loop housekeeping for `ms` milliseconds (10 ms steps)."""
        out: list[str] = []
        for _ in range(0, ms, 10):
            self.ms += 10
            out += self._loop_tick()
        return out

    def _loop_tick(self) -> list[str]:
        out: list[str] = []
        now = self.ms
        if self.armed and now - self._last_chk >= 500:
            self._last_chk = now
            out += self._check_board()
        if self.running and self._thermal_risk():
            if not self._thermal_active:
                self._thermal_active = True
                self._thermal_t0 = now
            elif self.thermal_stop_s > 0 and now - self._thermal_t0 >= self.thermal_stop_s * 1000:
                self._set_running(False)
                out.append(f"WARN THERMAL auto-stop after {self.thermal_stop_s} s of continuous "
                           "output above the guideline")
        else:
            self._thermal_active = False
        return out

    def _check_board(self) -> list[str]:
        if not self.board_powered:
            self._pot_fail += 1
            if self._pot_fail >= 2:
                self._set_running(False)
                self.armed = False
                self._pot_hw = -1
                return ["WARN NOPOWER board stopped answering on I2C (12 V off?): disarmed, pins released"]
            return []
        self._pot_fail = 0
        if self._pot_hw >= 0 and self.pot_reg != self._pot_hw:
            d, want = self.pot_reg, self._pot_hw
            self._pot_write(want)
            return [f"WARN POTRESET wiper read {d} expected {want} (board supply glitch?): rewritten"]
        return []

    # ------------------------------------------------------------------ commands
    def status_line(self) -> str:
        power = 1 if (self.armed or self.board_powered) else 0
        return (f"STATUS armed={int(self.armed)} run={int(self.running)} freq={_lround(self.freq)} "
                f"amp={_lround(self.amp)} pot={self.pot} mode={'BURST' if self.burst else 'CONT'} "
                f"on_us={self.on_us} per_us={self.per_us} est={self.est():.2f} limit={self.limit:.1f} "
                f"power={power} cal={self.cal:.3f} sweep={int(self.sweep_on)} tstop={self.thermal_stop_s} "
                f"sync={int(self.sync_on)}")

    def _arm(self) -> str:
        if self.armed:
            return "OK ARMED"
        if not self.board_powered:
            return "ERR NOPOWER (turn the 12 V supply on, then ARM)"
        if not self._pot_write(255):
            return "ERR NOPOT (AD5245 did not answer on I2C)"
        self._pot_write(self.pot)
        self._set_running(False)
        self.armed = True
        self._pot_fail = 0
        self._last_chk = self.ms
        return "OK ARMED"

    def _disarm(self) -> str:
        if not self.armed:
            return "OK DISARMED"
        self._set_running(False)
        self._pot_write(255)
        self.armed = False
        return "OK DISARMED"

    def handle(self, line: str) -> list[str]:
        line = line.strip()
        if not line:
            return []
        toks = line.upper()[:95].split()
        c = toks[0] if toks else ""
        a1, a2, a3 = (toks[1:4] + [None, None, None])[:3]
        out: list[str] = []
        risk_before = self.running and self._thermal_risk()

        def err(msg):
            out.append(msg)
            return out

        if c == "PING":
            out.append(f"PONG TXFW {VERSION}")
        elif c == "VERSION":
            out.append(f"OK VERSION TXFW {VERSION} fs=500000 dac=DAC8830 pot=AD5245")
        elif c == "HELP":
            out.append("OK commands: PING VERSION ARM DISARM FREQ AMP POT LIMIT CAL MODE SWEEP "
                       "THERMSTOP POTREAD SYNC START STOP STATUS")
        elif c == "STATUS":
            out.append(self.status_line())
        elif c == "ARM":
            out.append(self._arm())
        elif c == "DISARM":
            out.append(self._disarm())
        elif c == "FREQ":
            if a1 is None:
                return err("ERR FREQ needs a value")
            f = _atof(a1)
            if not (100 <= f <= 100000):
                return err("ERR FREQ out of range 100..100000")
            self.freq = f
            out.append(f"OK FREQ {_lround(f)}")
        elif c == "AMP":
            if a1 is None:
                return err("ERR AMP needs a value")
            m = _atof(a1)
            if not (0 <= m <= MAX_DAC_MVPK):
                return err("ERR AMP out of range 0..1400")
            if self.est(amp=m) > self.limit + _EPS:
                return err("ERR LIMIT estimated output would exceed limit")
            self.amp = m
            out.append(f"OK AMP {_lround(m)}")
        elif c == "POT":
            if a1 is None:
                return err("ERR POT needs a value")
            d = int(_atof(a1))
            if d < 0 or d > 255:
                return err("ERR POT out of range 0..255")
            if self.est(pot=d) > self.limit + _EPS:
                return err("ERR LIMIT estimated output would exceed limit")
            if self.armed and not self._pot_write(d):
                return err("ERR NOPOT (I2C write failed)")
            self.pot = d
            out.append(f"OK POT {d}")
        elif c == "LIMIT":
            if a1 is None:
                return err("ERR LIMIT needs a value")
            v = _atof(a1)
            if not (1 <= v <= ABS_MAX_VPP):
                return err("ERR LIMIT out of range 1..50")
            if self.est() > v + _EPS:
                return err("ERR LIMIT current settings already exceed it")
            self.limit = v
            out.append(f"OK LIMIT {v:.1f}")
        elif c == "CAL":
            if a1 is None:
                return err("ERR CAL needs a value")
            k = _atof(a1)
            if not (CAL_MIN <= k <= CAL_MAX):
                return err("ERR CAL out of range 0.5..2.0")
            if self.est(cal=k) > self.limit + _EPS:
                return err("ERR LIMIT estimated output would exceed limit")
            self.cal = k
            out.append(f"OK CAL {k:.3f}")
        elif c == "MODE":
            if a1 == "CONT":
                self.burst = False
                out.append("OK MODE CONT")
            elif a1 == "BURST":
                if a2 is None or a3 is None:
                    return err("ERR MODE BURST needs <on_us> <period_us>")
                on, per = _atol(a2), _atol(a3)
                if on < 10 or per <= on or per > 10_000_000:
                    return err("ERR MODE BURST needs 10 <= on_us < period_us <= 10000000")
                self.on_us, self.per_us, self.burst = on, per, True
                out.append(f"OK MODE BURST {on} {per}")
            else:
                return err("ERR MODE needs CONT or BURST")
        elif c == "SWEEP":
            if a1 == "OFF":
                self.sweep_on = False
                out.append("OK SWEEP OFF")
            else:
                if a1 is None or a2 is None or a3 is None:
                    return err("ERR SWEEP needs <f1> <f2> <ms> or OFF")
                f1, f2, ms = _atof(a1), _atof(a2), _atol(a3)
                if not (100 <= f1 <= 100000 and 100 <= f2 <= 100000):
                    return err("ERR SWEEP frequencies out of range 100..100000")
                if ms < 1 or ms > 600000:
                    return err("ERR SWEEP time out of range 1..600000 ms")
                self.sweep, self.sweep_on = (f1, f2, ms), True
                out.append(f"OK SWEEP {_lround(f1)} {_lround(f2)} {ms}")
        elif c == "THERMSTOP":
            if a1 is None:
                return err("ERR THERMSTOP needs a value")
            s = _atol(a1)
            if s < 0 or s > 3600:
                return err("ERR THERMSTOP out of range 0..3600")
            self.thermal_stop_s = s
            out.append(f"OK THERMSTOP {s}")
        elif c == "SYNC":
            if a1 == "ON":
                self.sync_on = True
                out.append("OK SYNC ON")
            elif a1 == "OFF":
                self.sync_on = False
                out.append("OK SYNC OFF")
            else:
                return err("ERR SYNC needs ON or OFF")
        elif c == "POTREAD":
            if not self.armed:
                return err("ERR NOTARMED")
            if not self.board_powered:
                return err("ERR NOPOT (I2C read failed)")
            out.append(f"OK POTREAD {self.pot_reg}")
        elif c == "START":
            if not self.armed:
                return err("ERR NOTARMED")
            if self.est() > self.limit + _EPS:
                return err("ERR LIMIT")
            self._set_running(True)
            out.append("OK START")
        elif c == "STOP":
            self._set_running(False)
            out.append("OK STOP")
        else:
            return err(f"ERR unknown command: {c}")

        if self.running and self._thermal_risk() and not risk_before:
            out.append(_THERMAL_WARN)
        return out
