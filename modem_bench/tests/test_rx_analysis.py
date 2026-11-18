"""rx_analysis + rx_io (spec §9.1.2 check values, §6.7 round trip)."""
import os
import tempfile
import unittest

import numpy as np

import _paths  # noqa: F401
from bench import rx_analysis as A
from bench import rx_io
from bench.rx_model import FS_HZ, LSB_V, RxGain


def tone(f, n, amp_counts, mid=2048, phase=0.3, noise=0.0, seed=0):
    k = np.arange(n)
    x = mid + amp_counts * np.sin(2 * np.pi * f * k / FS_HZ + phase)
    if noise:
        x = x + np.random.default_rng(seed).normal(0, noise, n)
    return np.clip(np.rint(x), 0, 4095).astype(np.uint16)


class TestAnalysis(unittest.TestCase):
    def test_peak_frequency(self):
        for f in (21_345.0, 40_000.0, 40_123.0, 87_900.0):
            c = tone(f, 8192, 600, noise=2)
            fp, amp = A.peak_frequency((c - 2048.0) * LSB_V)
            self.assertAlmostEqual(fp, f, delta=10, msg=f)
            self.assertAlmostEqual(amp / (600 * LSB_V), 1.0, delta=0.02)

    def test_vpp_within_one_lsb(self):
        c = tone(40e3, 4000, 500.0, phase=np.pi / 2)   # samples hit both peaks
        self.assertLessEqual(abs(A.adc_vpp(c) - 1000 * LSB_V), LSB_V)

    def burst_signal(self, delay=135, pre=200, n=4000, f=40e3, on_us=1000, amp=400, noise=3, seed=1):
        tpl = A.burst_template(f, on_us)
        x = np.random.default_rng(seed).normal(0, noise, n)
        a = pre + delay
        m = min(tpl.size, n - a)
        x[a:a + m] += amp * tpl[:m]
        return np.clip(np.rint(2048 + x), 0, 4095).astype(np.uint16), tpl

    def test_delay_threshold_and_xcorr(self):
        for seed in range(5):
            codes, tpl = self.burst_signal(seed=seed)
            x = (codes - 2048.0) * LSB_V
            d_xc = A.delay_xcorr(x, 200, tpl)
            self.assertLessEqual(abs(d_xc - 135), 1, msg=seed)
        # threshold method on an abrupt burst (no ramp)
        n, pre = 3000, 200
        x = np.random.default_rng(7).normal(0, 2, n)
        k = np.arange(n - pre - 135)
        x[pre + 135:] += 400 * np.sin(2 * np.pi * 40e3 * k / FS_HZ + np.pi / 2)
        d_thr = A.delay_threshold(x * LSB_V, pre)
        self.assertLessEqual(abs(d_thr - 135), 1)

    def test_measure(self):
        codes, tpl = self.burst_signal()
        g = RxGain(coarse="HIGH")
        m = A.measure(codes, 2048, g, trig=200, win=(200 + 135, 200 + 135 + 400), template=tpl, sync_ok=True,
                      rvs_db=-206, system_delay_us=0, sound_speed=1480)
        self.assertAlmostEqual(m["freq_hz"], 40e3, delta=60)
        self.assertLessEqual(abs(m["delay_xc_samples"] - 135), 1)
        self.assertAlmostEqual(m["distance_m"], 1480 * 135 / FS_HZ, delta=0.004)
        self.assertGreater(m["snr_db"], 30)
        self.assertAlmostEqual(m["input_vpp_v"], m["adc_vpp_v"] / g.total_mag(m["freq_hz"]), places=9)
        self.assertIn("spl_db_re_1upa", m)
        self.assertNotIn("delay_xc_us", A.measure(codes, 2048, g, trig=200, template=tpl, sync_ok=False))

    def test_minmax_decimate(self):
        y = np.sin(np.arange(100_000) * 0.37)
        idx, out = A.minmax_decimate(y, 4000)
        self.assertLessEqual(out.size, 4002)
        self.assertAlmostEqual(out.max(), y.max())
        self.assertAlmostEqual(out.min(), y.min())
        self.assertEqual(A.minmax_decimate(y[:100], 4000)[1].size, 100)

    def test_spectrogram(self):
        x = (tone(60e3, 10_240, 300) - 2048.0) * LSB_V
        s = A.spectrogram(x)
        self.assertEqual(s.shape, (19, 513))
        self.assertEqual(int(np.argmax(s[5])), round(60e3 / (FS_HZ / 1024)))


class TestSaveLoad(unittest.TestCase):
    def test_roundtrip_npy_and_csv(self):
        words = tone(40e3, 3000, 700, noise=2)
        words[[100, 101, 102]] |= 0x8000
        meta = rx_io.base_metadata()
        meta.update({"n": 3000, "pre": 200, "trigger_source": "sync", "mid_code": 2048, "coarse_gain": "LOW",
                     "pot_code": 17, "tx_state": {"freq": 40000, "mode": "BURST"}, "measurements": {"freq_hz": 40000.5},
                     "notes": "tank A, 0.5 m", "nan_value": float("nan")})
        with tempfile.TemporaryDirectory() as d:
            npy, js = rx_io.save_npy(os.path.join(d, "cap1"), words, meta)
            codes, m2 = rx_io.load_npy(os.path.join(d, "cap1"))
            self.assertTrue(np.array_equal(codes, words & 0x0FFF))
            self.assertEqual(codes.dtype, np.uint16)
            self.assertEqual(m2["sync_indices"], [100, 101, 102])
            self.assertEqual(m2["tx_state"], meta["tx_state"])
            self.assertIsNone(m2["nan_value"])
            csv = rx_io.save_csv(os.path.join(d, "cap1.csv"), words, meta, input_gain=95.0)
            codes2, m3 = rx_io.load_csv(csv)
            self.assertTrue(np.array_equal(codes2, codes))
            m3.pop("n"), m2.pop("n")
            self.assertEqual(m3, m2)
            with open(csv) as f:
                body = [ln for ln in f if not ln.startswith("#")]
            self.assertEqual(body[0].strip(), rx_io.CSV_COLUMNS)
            s, t, c, va, vi, sy = body[1 + 100].strip().split(",")
            self.assertEqual((int(s), int(sy)), (100, 1))
            self.assertAlmostEqual(float(t), (100 - 200) / FS_HZ)
            self.assertAlmostEqual(float(va), (int(c) - 2048) * LSB_V, places=6)
            self.assertAlmostEqual(float(vi), float(va) / 95.0, places=8)

    def test_default_name(self):
        from datetime import datetime
        self.assertEqual(rx_io.default_name(40e3, datetime(2026, 11, 18, 9, 5, 7)), "rx_20261118_090507_40kHz")


if __name__ == "__main__":
    unittest.main()
