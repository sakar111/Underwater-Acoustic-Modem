"""rx_model check values from the spec (§3.4-§3.6, §9.1.2, §9.2)."""
import math
import unittest

import _paths  # noqa: F401
from bench.rx_model import (Calibration, RxGain, VgaTable, g_frontend, gain_stage_clip_estimate,
                            gain_stage_dc, h_bandpass, vga_code_for_db, vga_gain_dc)


def db(x):
    return 20 * math.log10(abs(x))


class TestRxModel(unittest.TestCase):
    def test_bandpass_table(self):
        table = {20: -3.70, 30: -0.24, 40: 0.16, 50: 0.36, 60: 0.41, 70: 0.08, 80: -0.94, 90: -2.79,
                 100: -5.22, 150: -17.9, 200: -27.7, 310: -42.8}
        for fk, want in table.items():
            self.assertAlmostEqual(db(h_bandpass(fk * 1e3)), want, delta=0.051 if fk >= 150 else 0.02, msg=fk)

    def test_vga_and_gain_stage(self):
        for d, want in ((0, 1.471), (128, 2.891), (255, 67.77)):
            self.assertAlmostEqual(vga_gain_dc(d), want, places=2)
        self.assertAlmostEqual(vga_gain_dc(255, rw=120), 42.3, delta=0.1)
        self.assertAlmostEqual(gain_stage_dc("HIGH"), 58.317, places=3)
        self.assertAlmostEqual(gain_stage_dc("LOW"), 7.102, places=3)
        self.assertAlmostEqual(20 * math.log10(gain_stage_dc("HIGH") / gain_stage_dc("LOW")), 18.3, delta=0.05)

    def test_total_gain(self):
        g = RxGain(coarse="HIGH", pot=0)
        self.assertAlmostEqual(g.total_db(40e3), 57.8, delta=0.05)
        self.assertAlmostEqual(g.total_mag(40e3), 778, delta=2)
        self.assertAlmostEqual(g.adc_fullscale_input_vpp(40e3) * 1e3, 6.43, delta=0.01)
        g = RxGain(coarse="LOW", pot=255)
        self.assertAlmostEqual(g.total_db(40e3), 72.5, delta=0.05)
        self.assertAlmostEqual(g.total_mag(40e3), 4232, delta=5)
        rows = {("HIGH", 128): (59.8, 63.6, 63.7, 60.0), ("LOW", 0): (35.6, 39.5, 39.7, 36.3),
                ("LOW", 224): (52.6, 56.3, 56.3, 52.3)}
        for (c, d), want in rows.items():
            got = [RxGain(coarse=c, pot=d).total_db(f) for f in (20e3, 40e3, 60e3, 90e3)]
            for a, b in zip(got, want):
                self.assertAlmostEqual(a, b, delta=0.06, msg=(c, d))

    def test_pa4_and_cal(self):
        r = abs(g_frontend(40e3, "PA4")) / abs(g_frontend(40e3, "LNA"))
        self.assertAlmostEqual(r, 9.67 / 8.98, delta=0.002)
        a, b = RxGain(cal=1.0).total_mag(40e3), RxGain(cal=1.25).total_mag(40e3)
        self.assertAlmostEqual(b / a, 1.25)

    def test_bench_frequency_response(self):
        g = RxGain(coarse="LOW", pot=0)
        ref = g.total_mag(60e3)
        for fk, want in {20: -4.05, 30: -0.55, 40: -0.16, 50: 0.0, 70: -0.39, 80: -1.49, 90: -3.42}.items():
            self.assertAlmostEqual(db(g.total_mag(fk * 1e3) / ref), want, delta=0.02, msg=fk)

    def test_bench_coarse_step(self):     # §9.2 step 3: 1 mVpp at J2, 40 kHz, POT 0
        self.assertAlmostEqual(1e-3 * RxGain(coarse="HIGH").total_mag(40e3), 0.778, delta=0.002)
        self.assertAlmostEqual(1e-3 * RxGain(coarse="LOW").total_mag(40e3) * 1e3, 94.6, delta=0.3)

    def test_gain_stage_clip_estimate(self):  # §9.2 step 7: HIGH, POT 0, 10 kHz, 9 mVpp at J2
        g = RxGain(coarse="HIGH", pot=0)
        adc_vpp = 9e-3 * g.total_mag(10e3)
        self.assertAlmostEqual(adc_vpp, 0.37, delta=0.02)
        self.assertAlmostEqual(gain_stage_clip_estimate(adc_vpp, 10e3, g), 4.6, delta=0.05)

    def test_vga_db_to_code(self):
        self.assertEqual(vga_code_for_db(14.9), 192)
        self.assertEqual(vga_code_for_db(0), 0)
        self.assertEqual(vga_code_for_db(99), 255)

    def test_measured_table_and_calibration_json(self):
        t = VgaTable([0, 128, 255], [1.471, 2.8, 40.0])
        self.assertAlmostEqual(t.gain(255), 40.0)
        self.assertAlmostEqual(t.gain(128), 2.8)
        g = RxGain(pot=255, vga_table=t)
        self.assertAlmostEqual(g.vga_gain(), 40.0)
        self.assertAlmostEqual(g.total_mag(40e3) / RxGain(pot=255).total_mag(40e3),
                               40.0 / abs(complex(__import__("bench.rx_model", fromlist=["g_vga_model"]).g_vga_model(40e3, 255))),
                               places=6)
        cal = Calibration(9.4, 1.05, t, 12.5, 1475)
        back = Calibration.from_json(cal.to_json())
        self.assertEqual((back.lna_gain, back.cal_factor, back.system_delay_us, back.sound_speed), (9.4, 1.05, 12.5, 1475))
        self.assertEqual(back.vga_table.codes, [0, 128, 255])


if __name__ == "__main__":
    unittest.main()
