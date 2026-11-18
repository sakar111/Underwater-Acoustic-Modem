"""RX board gain model (spec §3.4-§3.7), hydrophone terminals -> ADC input. No Qt.

All component values are the final RX schematic designators. Functions take frequency in Hz
(scalar or NumPy array) and return complex responses unless the name says otherwise.

    G_total(f) = cal * G_FE(f) * G_GS(f, coarse) * H_BP(f) * G_VGA(f, D) * H_RC(f)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

# ---- ADC (§3.1, §3.7) --------------------------------------------------------
FS_HZ = 400_000
VA = 5.0
LSB_V = VA / 4096                 # 1.2207 mV
MID_CODE = 2048
CLIP_LO, CLIP_HI = 8, 4087
GAIN_STAGE_WARN_VPP = 4.5         # U4 output swing warning (§3.7)

# ---- front end ---------------------------------------------------------------
R12, C19 = 10e3, 6.8e-9           # gain-stage input coupling
R37 = 330.0                       # PA-4 series resistor
G_LNA_NOM = 9.0                   # JFET LNA (7.1 .. 11.2 with IDSS spread)
G_PA4 = 10.0                      # PA-4 with J2 open (+20 dB)
LNA_GAIN_MIN, LNA_GAIN_MAX = 7.0, 11.5
FRONTEND_LNA, FRONTEND_PA4 = "LNA", "PA4"

# ---- gain stage U4 (§3.4) ----------------------------------------------------
R13, C24, R23 = 47e3, 6.8e-12, 820.0
R26, C35 = 5.6e3, 68e-12
COARSE_HIGH, COARSE_LOW = "HIGH", "LOW"

# ---- band-pass U5 (§3.6) -----------------------------------------------------
HP_SECTIONS = ((7.5e3, 8.2e3, 1e-9), (3.0e3, 20e3, 1e-9))           # (R to output, R to 2V5, C)
LP_SECTIONS = ((1.5e3, 1.2e-9, 1e-9), (1.5e3, 3.3e-9, 470e-12))     # (R, C feedback, C shunt)

# ---- VGA U8 + AD5245 (§3.5) --------------------------------------------------
R34, C39 = 10e3, 6.8e-9
R31, R36, R35, C42 = 4.7e3, 82.0, 10e3, 68e-12
POT_RAB = 10e3
POT_RW = 50.0                     # typ (120 max); the AD5245 equations use 2 * R_W
VGA_REF_HZ = 40e3                 # frequency of the bench VGA calibration (§9.2 step 6)

# ---- anti-alias RC (§3.6) ----------------------------------------------------
R32, C43 = 100.0, 3.3e-9

BAND_LO_HZ, BAND_HI_HZ = 20e3, 90e3


def _jw(f):
    return 2j * np.pi * np.asarray(f, dtype=float)


def g_frontend(f, frontend: str = FRONTEND_LNA, g_lna: float = G_LNA_NOM):
    s = _jw(f)
    with np.errstate(divide="ignore", invalid="ignore"):
        if frontend == FRONTEND_PA4:
            return G_PA4 * R12 / (R12 + R37 + 1 / (s * C19))
        return g_lna * R12 / (R12 + 1 / (s * C19))


def g_gain_stage(f, coarse: str = COARSE_LOW):
    s = _jw(f)
    y = 1 / R13 + s * C24
    if coarse == COARSE_LOW:
        y = y + 1 / R26 + s * C35
    return 1 + (1 / y) / R23


def gain_stage_dc(coarse: str) -> float:
    zf = R13 if coarse == COARSE_HIGH else 1 / (1 / R13 + 1 / R26)
    return 1 + zf / R23


def h_bandpass(f):
    s = _jw(f)
    h = np.ones_like(s)
    for r1, r2, c in HP_SECTIONS:
        a = r1 * r2 * c * c
        h = h * (a * s * s) / (a * s * s + r1 * 2 * c * s + 1)
    for r, c1, c2 in LP_SECTIONS:
        h = h / (r * r * c1 * c2 * s * s + 2 * r * c2 * s + 1)
    return h


def pot_resistances(code: int, rw: float = POT_RW, rab: float = POT_RAB) -> tuple[float, float]:
    rwb = code / 256 * rab + 2 * rw
    rwa = (256 - code) / 256 * rab + 2 * rw
    return rwb, rwa


def vga_gain_dc(code: int, rw: float = POT_RW, rab: float = POT_RAB) -> float:
    """Mid-band VGA gain without the coupling and C42 poles (the §3.5 table)."""
    rwb, rwa = pot_resistances(code, rw, rab)
    return 1 + (R31 + rwb) / (R36 + rwa)


def g_vga_model(f, code: int, rw: float = POT_RW, rab: float = POT_RAB):
    s = _jw(f)
    rwb, rwa = pot_resistances(code, rw, rab)
    with np.errstate(divide="ignore", invalid="ignore"):
        zb = 1 / (1 / (R31 + rwb) + s * C42)
        return R34 / (R34 + 1 / (s * C39)) * (1 + zb / (R36 + rwa))


def h_rc(f):
    return 1 / (1 + _jw(f) * R32 * C43)


@dataclass
class VgaTable:
    """Measured VGA gain vs code at 40 kHz (§9.2 step 6), interpolated in dB between points."""
    codes: list[int]
    gains: list[float]

    def __post_init__(self):
        pts = sorted(zip(self.codes, self.gains))
        self.codes = [int(c) for c, _ in pts]
        self.gains = [float(g) for _, g in pts]

    def gain(self, code: int) -> float:
        db = np.interp(code, self.codes, [20 * math.log10(g) for g in self.gains])
        return float(10 ** (db / 20))

    def to_dict(self) -> dict:
        return {str(c): g for c, g in zip(self.codes, self.gains)}

    @staticmethod
    def from_dict(d: dict) -> "VgaTable":
        return VgaTable([int(k) for k in d], [float(v) for v in d.values()])


def g_vga(f, code: int, table: VgaTable | None = None):
    """VGA response; with a measured table the model is scaled to match it at 40 kHz."""
    g = g_vga_model(f, code)
    if table is not None and len(table.codes) >= 2:
        g = g * (table.gain(code) / abs(complex(g_vga_model(VGA_REF_HZ, code))))
    return g


@dataclass
class RxGain:
    """Everything the gain depends on. `cal` is the user's bench calibration factor."""
    coarse: str = COARSE_LOW
    pot: int = 0
    frontend: str = FRONTEND_LNA
    g_lna: float = G_LNA_NOM
    cal: float = 1.0
    vga_table: VgaTable | None = field(default=None, repr=False)

    def total(self, f):
        """Complex G_total(f), hydrophone terminals -> ADC input."""
        return (self.cal * g_frontend(f, self.frontend, self.g_lna) * g_gain_stage(f, self.coarse) *
                h_bandpass(f) * g_vga(f, self.pot, self.vga_table) * h_rc(f))

    def total_mag(self, f) -> float | np.ndarray:
        m = np.abs(self.total(f))
        return float(m) if np.ndim(m) == 0 else m

    def total_db(self, f) -> float:
        return 20 * math.log10(self.total_mag(f))

    def after_gain_stage_mag(self, f) -> float:
        """|H_BP * G_VGA * H_RC|: GAIN_OUT -> ADC (to estimate gain-stage clipping, §3.7)."""
        return float(abs(complex(h_bandpass(f) * g_vga(f, self.pot, self.vga_table) * h_rc(f))))

    def vga_gain(self) -> float:
        if self.vga_table is not None and len(self.vga_table.codes) >= 2:
            return self.vga_table.gain(self.pot)
        return vga_gain_dc(self.pot)

    def adc_fullscale_input_vpp(self, f) -> float:
        return VA / self.total_mag(f)


def vga_code_for_db(db: float, table: VgaTable | None = None) -> int:
    """Pot code whose mid-band VGA gain is nearest to `db` (the taper is very non-linear)."""
    best, best_err = 0, float("inf")
    for d in range(256):
        g = table.gain(d) if (table is not None and len(table.codes) >= 2) else vga_gain_dc(d)
        err = abs(20 * math.log10(g) - db)
        if err < best_err:
            best, best_err = d, err
    return best


def counts_to_volts(counts):
    return np.asarray(counts, dtype=float) * LSB_V


def gain_stage_clip_estimate(adc_vpp: float, f: float, gain: RxGain) -> float:
    """Estimated Vpp at GAIN_OUT (U4) from the ADC Vpp at the dominant frequency f (§3.7)."""
    m = gain.after_gain_stage_mag(f)
    return adc_vpp / m if m > 0 else float("inf")


# ---- calibration file (§6.4: LNA gain, cal, VGA table, system delay) ----------
@dataclass
class Calibration:
    lna_gain: float = G_LNA_NOM
    cal_factor: float = 1.0
    vga_table: VgaTable | None = None
    system_delay_us: float = 0.0
    sound_speed: float = 1480.0

    def to_json(self) -> str:
        return json.dumps({
            "lna_gain": self.lna_gain, "cal_factor": self.cal_factor,
            "vga_table": self.vga_table.to_dict() if self.vga_table else None,
            "system_delay_us": self.system_delay_us, "sound_speed_m_s": self.sound_speed,
        }, indent=2)

    @staticmethod
    def from_json(text: str) -> "Calibration":
        d = json.loads(text)
        t = d.get("vga_table")
        return Calibration(float(d.get("lna_gain", G_LNA_NOM)), float(d.get("cal_factor", 1.0)),
                           VgaTable.from_dict(t) if t else None, float(d.get("system_delay_us", 0.0)),
                           float(d.get("sound_speed_m_s", 1480.0)))
