"""TX board hardware model and protocol helpers (no Qt).

Keep the constants in sync with tx_firmware.ino (spec §3.6).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import permutations

TEENSY_VID = 0x16C0

FS_HZ = 500_000
PA_GAIN = 27.31          # power-amp differential gain
POT_RAB = 10_000.0       # AD5245 end-to-end resistance
POT_RW = 50.0            # wiper resistance (typ, 120 max); R_WA uses 2*R_W
R21 = 10_000.0           # level-control input resistor
THERMAL_FVPP = 2.3e6     # continuous f*Vpp guideline for the OPA55x (SOIC)
MAX_DAC_MVPK = 1400
ABS_MAX_VPP = 50.0
DEFAULT_LIMIT_VPP = 20.0
FREQ_MIN, FREQ_MAX = 100, 100_000
BAND_MIN, BAND_MAX = 20_000, 90_000
CAL_MIN, CAL_MAX = 0.5, 2.0
LIMIT_MIN = 1.0
FW_EPS = 1e-3            # firmware tolerance: rejects est > limit + 1e-3
BURST_ON_MIN_US = 10
BURST_PER_MAX_US = 10_000_000


def pot_gain(code: int) -> float:
    """Level-control stage gain magnitude for an AD5245 code (255 = minimum)."""
    rwa = (256 - code) / 256 * POT_RAB + 2 * POT_RW   # AD5245 eq. 2
    return rwa / R21


def est_vpp(dac_mvpk: float, pot_code: int, cal: float = 1.0) -> float:
    """Estimated differential Vpp across the transducer (spec §3.6)."""
    return 2.0 * (dac_mvpk / 1000.0) * pot_gain(pot_code) * PA_GAIN * cal


def thermal_risk(freq_hz: float, est: float, continuous: bool) -> bool:
    return continuous and freq_hz * est > THERMAL_FVPP


def parse_status(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in line.split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Level settings and safe ordering
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Level:
    """The four settings that determine the estimated output."""
    amp: int        # DAC mV peak
    pot: int        # AD5245 code
    limit: float    # Vpp
    cal: float = 1.0

    def est(self) -> float:
        return est_vpp(self.amp, self.pot, self.cal)

    def within_limit(self) -> bool:
        return self.est() <= self.limit + 1e-9

    @staticmethod
    def from_status(st: dict[str, str]) -> "Level | None":
        try:
            return Level(int(round(float(st["amp"]))), int(st["pot"]),
                         round(float(st["limit"]), 1), round(float(st.get("cal", "1")), 3))
        except (KeyError, ValueError):
            return None


def fmt_cmd(key: str, lv: Level) -> str:
    if key == "AMP":
        return f"AMP {lv.amp}"
    if key == "POT":
        return f"POT {lv.pot}"
    if key == "LIMIT":
        return f"LIMIT {lv.limit:.1f}"
    if key == "CAL":
        return f"CAL {lv.cal:.3f}"
    raise KeyError(key)


def _fw_accepts(state: Level, key: str, new: Level) -> Level | None:
    """Mirror of the firmware's limit check for one command. Returns the new state or None."""
    nxt = {
        "AMP": replace(state, amp=new.amp),
        "POT": replace(state, pot=new.pot),
        "LIMIT": replace(state, limit=new.limit),
        "CAL": replace(state, cal=new.cal),
    }[key]
    # Use a margin on our side so float32 rounding in the firmware can never bite.
    return nxt if nxt.est() <= nxt.limit - 1e-6 or nxt.est() == 0 else None


LEVEL_ORDER = ("LIMIT", "CAL", "POT", "AMP")


def plan_level_commands(current: Level | None, target: Level) -> list[str]:
    """Commands that take the firmware from `current` to `target` such that every
    intermediate state is within the firmware's limit (spec §6.3).

    `current` = None means the firmware state is unknown: the amplitude is first
    set to 0 (always accepted, output silent), then everything else, amplitude last.
    `target` must itself be within its limit (the caller checks that).
    """
    if current is not None:
        changed = [k for k in LEVEL_ORDER if fmt_cmd(k, current) != fmt_cmd(k, target)]
        for order in permutations(changed):
            st = current
            for key in order:
                st = _fw_accepts(st, key, target)
                if st is None:
                    break
            else:
                return [fmt_cmd(k, target) for k in order]
    # Unknown (or inconsistent) state: silence first.
    cmds = ["AMP 0"]
    cmds += [fmt_cmd(k, target) for k in ("LIMIT", "CAL", "POT")]
    if target.amp != 0:
        cmds.append(fmt_cmd("AMP", target))
    return cmds
