"""Drive the RX tab offscreen against the fake RX device (and the fake TX at the same time).
Spec RX §6.9 / §9.1.3. The fake RX runs in simulated time (link.run_ms), not the wall clock.
"""
import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

import _paths  # noqa: F401,E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bench import rx_io  # noqa: E402
from bench.links import SIM_PORT_NAME  # noqa: E402
from bench.main_window import MainWindow  # noqa: E402
from bench.rx_link import SIM_RX_PORT_NAME  # noqa: E402
from bench.rx_model import LSB_V, RxGain  # noqa: E402
from bench.sim_rx import BurstConfig  # noqa: E402

app = QApplication.instance() or QApplication([])


class RxHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(os.path.join(self.tmp.name, "t.ini"), QSettings.Format.IniFormat)
        self.w = MainWindow(sim=True, settings=self.settings, sim_realtime=False)
        self.rx = self.w.rx
        self.tx = self.w.tx
        self.link = self.w.sim_rx
        self.dev = self.link.dev
        self.dev.burst = None                      # plain noise unless a test sets a signal
        self.sent = []
        self.rx.port.line_sent.connect(self.sent.append)
        self.rx.confirm_arm = lambda: True
        self.tx.confirm_arm = lambda: True
        self.rx.ask_save_path = lambda default, flt: os.path.join(self.tmp.name, default)
        self.w.show()
        self.w.tabs.setCurrentWidget(self.rx)        # plots only redraw while visible

    def tearDown(self):
        self.w.close()
        self.tmp.cleanup()

    def pump(self, ms=50):
        QTest.qWait(ms)

    def run_sim(self, ms):
        self.link.run_ms(ms)
        self.pump(1)

    def connect_rx(self):
        self.assertTrue(self.rx.port.select_port(SIM_RX_PORT_NAME))
        self.rx.port.toggle_connect()
        self.pump(80)
        self.assertTrue(self.rx.identified)

    def arm(self):
        self.rx.btn_arm.click()
        self.pump(50)
        self.assertTrue(self.rx.armed)

    def start(self):
        self.assertTrue(self.rx.btn_start.isEnabled())
        self.rx.btn_start.click()
        self.pump(50)

    def capture_mode(self, trig="LEVEL", pre_ms=0.5, len_ms=4.0, auto=False):
        self.rx.rb_capture.setChecked(True)
        if trig == "SYNC":
            self.rx.cb_syncwire.setChecked(True)
        self.rx.cb_trig.setCurrentIndex(self.rx.cb_trig.findData(trig))
        self.rx.sp_pre.setValue(pre_ms)
        self.rx.sp_len.setValue(len_ms)
        (self.rx.rb_auto if auto else self.rx.rb_single).setChecked(True)
        self.pump(200)

    def log(self):
        return self.w.log.toPlainText()


class TestRxTab(RxHarness):
    def test_connect_sequence(self):
        self.connect_rx()
        self.pump(200)
        self.assertEqual(self.sent[:6], ["PING", "STREAM OFF", "ABORT", "GAIN LOW", "POT 0", "TRIG NONE"])
        self.assertIn("STATUS", self.sent)
        self.assertIn("Board powered — not armed", self.rx.lbl_board.text())
        self.assertFalse(self.rx.btn_start.isEnabled())
        self.assertTrue(self.rx.btn_arm.isEnabled())
        self.assertIn("[RX] → PING", self.log())
        self.assertIn("[RX] ← PONG RXFW", self.log())
        self.assertIn("RX board", self.rx.port.lbl_board.text())

    def test_stream_ten_seconds_then_close(self):
        self.dev.burst = BurstConfig(amp_v=2e-3)
        self.connect_rx()
        self.arm()
        self.assertTrue(2040 <= self.rx.mid <= 2056, self.rx.mid)
        self.assertIn("ARMED", self.rx.lbl_board.text())
        self.start()
        t0 = time.perf_counter()
        for _ in range(20):
            self.run_sim(500)
            self.rx._dirty = True
            self.rx.redraw()
        wall = time.perf_counter() - t0
        self.assertEqual((self.rx.gaps, self.rx.crc_errors, self.rx.overflows), (0, 0, 0))
        self.assertGreaterEqual(self.rx.frames, 3900)
        self.assertEqual(self.rx.ring_total, self.rx.frames * 1024)
        x, y = self.rx.c_time.getData()
        self.assertGreater(len(x), 100)
        self.assertGreater(self.rx._spec_cols.shape[0], 700)
        self.assertNotIn("ERR", self.log())
        self.assertLess(wall, 60, "10 s of simulated streaming took too long")
        # saving a stream snapshot
        out = self.rx.save_snapshot(os.path.join(self.tmp.name, "snap"), seconds=0.5)
        codes, meta = rx_io.load_npy(out)
        self.assertEqual(codes.size, 200_000)
        self.assertEqual(meta["trigger_source"], "none")
        # closing the app: STREAM OFF, ABORT, DISARM
        self.dev.commands.clear()
        self.w.close()
        self.assertEqual(self.dev.commands[-3:], ["STREAM OFF", "ABORT", "DISARM"])
        self.assertFalse(self.dev.armed)
        self.assertFalse(self.dev.streaming)

    def test_level_capture_measure_save_reload(self):
        g = RxGain(coarse="LOW", pot=0)
        self.dev.burst = BurstConfig(freq_hz=40e3, amp_v=0.5 / g.total_mag(40e3), sync_wire=False,
                                     period_us=20_000, offset_us=3000)
        self.connect_rx()
        self.arm()
        self.capture_mode("LEVEL", pre_ms=0.5, len_ms=4.0)
        self.rx.sp_level.setValue(200.0)
        self.pump(200)
        self.start()
        self.assertIn("TRIG LEVEL 164", self.sent)
        self.assertIn("CAPTURE 1600 200", self.sent)
        self.run_sim(40)
        cap = self.rx.capture
        self.assertIsNotNone(cap)
        self.assertEqual((cap.n, cap.pre, cap.incomplete, cap.settings_changed), (1600, 200, False, False))
        codes = cap.codes.astype(int)
        self.assertGreaterEqual(abs(codes[200] - cap.mid), 164)
        self.assertLess(np.abs(codes[:200] - cap.mid).max(), 164)
        m = self.rx.measurements
        self.assertAlmostEqual(m["freq_hz"], 40e3, delta=100)
        self.assertAlmostEqual(m["adc_vpp_v"], 1.0, delta=0.05)
        self.assertAlmostEqual(m["input_vpp_v"] * 1e3, 1.0 / g.total_mag(40e3) * 1e3, delta=0.6)
        self.assertGreater(self.rx.tbl.rowCount(), 4)
        self.assertFalse(self.rx.acq_running)                # single shot
        self.assertTrue(self.rx.btn_start.isEnabled())
        # save .npy/.json and .csv, re-load identical samples and metadata
        npy = self.rx.save_capture("npy")
        csv = self.rx.save_capture("csv")
        c1, m1 = rx_io.load_npy(npy)
        c2, m2 = rx_io.load_csv(csv)
        self.assertTrue(np.array_equal(c1, cap.codes))
        self.assertTrue(np.array_equal(c2, cap.codes))
        for k in ("n", "pre", "trigger_source", "trigger_index", "coarse_gain", "pot_code", "cap_id",
                  "trigger_level_counts", "mid_code"):
            self.assertEqual(m1[k], m2[k], k)
        self.assertEqual((m1["n"], m1["pre"], m1["trigger_source"], m1["coarse_gain"]), (1600, 200, "level", "LOW"))
        self.assertEqual(m1["trigger_level_counts"], 164)
        self.assertTrue(os.path.basename(npy).startswith("rx_"))

    def test_sync_capture_delay_with_tx_connected(self):
        # TX and RX connected at the same time, each to its own (simulated) port
        self.assertTrue(self.tx.port.select_port(SIM_PORT_NAME))
        self.tx.port.toggle_connect()
        self.pump(300)
        self.assertTrue(self.tx.identified)
        self.tx.btn_arm.click()
        self.pump(300)
        self.assertFalse(self.tx.cb_sync.isHidden())       # TX firmware 1.2 reports sync=
        self.tx.freq.setValue(40000)
        self.tx.amp.setValue(200)
        self.tx.gain.setValue(55)                            # pot 200 -> 2.5 Vpp
        self.tx.rb_burst.setChecked(True)
        self.tx.burst_on.setValue(1.0)
        self.tx.burst_per.setValue(20.0)
        self.tx.cb_sync.setChecked(True)
        self.pump(300)
        self.tx.btn_start.click()
        self.pump(300)
        self.assertTrue(self.w.sim_tx.sim.sync_on and self.w.sim_tx.sim.running)
        self.w._follow_tx_sim()
        self.assertTrue(self.dev.burst.sync_wire)
        self.assertAlmostEqual(self.dev.burst.amp_v, 5e-4, delta=1e-5)

        self.connect_rx()
        self.rx.rb_high.setChecked(True)
        self.arm()
        self.capture_mode("SYNC", pre_ms=0.5, len_ms=8.0, auto=True)
        self.start()
        self.assertIn("TRIG SYNC", self.sent)
        for _ in range(10):
            self.run_sim(20)
            self.pump(120)                                   # auto-repeat re-issues after 100 ms
        self.assertGreaterEqual(self.rx.captures_done, 3)
        m = self.rx.measurements
        self.assertLessEqual(abs(m["delay_xc_samples"] - 135), 2, m)
        self.assertGreater(m["delay_thr_samples"], 130)
        self.assertAlmostEqual(m["distance_m"], 1480 * 135 / 400e3, delta=0.01)
        self.assertIn("delay (x-corr)", self.rx.lbl_repeat.text())
        cap = self.rx.capture
        self.assertTrue(cap.sync_ok)
        self.assertTrue(cap.words[cap.pre] & 0x8000)          # sync edge at the trigger sample
        meta = self.rx.metadata(cap)
        self.assertEqual(meta["tx_state"]["freq"], 40000)
        self.rx.btn_stop.click()
        self.pump(50)
        self.assertFalse(self.rx.acq_running)
        # both ports open at once; the RX tab's combobox greys out the TX tab's port
        self.assertTrue(self.tx.port.is_open() and self.rx.port.is_open())

    def test_crc_fault_injection_recovers(self):
        self.connect_rx()
        self.arm()
        self.dev.crc_every, self.dev.garbage_every = 4, 9
        self.start()
        self.run_sim(500)
        f1 = self.rx.frames
        self.assertGreater(self.rx.crc_errors, 0)
        self.assertGreater(self.rx.gaps, 0)                   # the dropped frames show as gaps
        self.assertGreater(f1, 100)
        self.rx.request_status()
        self.pump(50)
        self.assertIn("ARMED", self.rx.lbl_board.text())     # text replies still arrive
        self.dev.crc_every = self.dev.garbage_every = 0
        crc, gaps = self.rx.crc_errors, self.rx.gaps
        self.run_sim(200)
        self.assertEqual((self.rx.crc_errors, self.rx.gaps), (crc, gaps))
        self.assertGreater(self.rx.frames, f1)

    def test_incomplete_capture_is_marked(self):
        self.connect_rx()
        self.arm()
        self.capture_mode("NONE", pre_ms=0.0, len_ms=20.0)
        self.dev.drop_every = 3
        self.start()
        self.run_sim(40)
        cap = self.rx.capture
        self.assertIsNotNone(cap)
        self.assertTrue(cap.incomplete)
        self.assertEqual(cap.words.size, cap.n)               # never spliced
        self.assertIn("INCOMPLETE", self.rx.lbl_acq.text())

    def test_power_lost(self):
        self.connect_rx()
        self.arm()
        self.start()
        self.run_sim(100)
        self.dev.power_off()
        self.run_sim(1200)
        self.pump(50)
        self.assertFalse(self.dev.armed)
        self.assertFalse(self.rx.armed)
        self.assertIn("power lost", self.rx.lbl_banner.text())
        self.assertIn("Unplug the RX USB", self.rx.lbl_banner.text())
        self.assertFalse(self.rx.btn_start.isEnabled())

    def test_arm_refused_without_power(self):
        self.dev.power_off()
        self.connect_rx()
        self.pump(100)
        self.assertIn("12 V not detected", self.rx.lbl_board.text())
        self.rx.btn_arm.click()
        self.pump(50)
        self.assertFalse(self.rx.armed)
        self.assertIn("ERR NOPOWER", self.rx.lbl_fwmsg.text())

    def test_wrong_port(self):
        self.dev.pong_as_tx = True
        self.rx.port.select_port(SIM_RX_PORT_NAME)
        self.rx.port.toggle_connect()
        self.pump(100)
        self.assertFalse(self.rx.identified)
        self.assertFalse(self.rx.port.is_open())
        self.assertIn("TX board's Teensy", self.rx.lbl_fwmsg.text())
        self.assertEqual(set(self.sent), {"PING"})

    def test_tx_tab_refuses_rx_board(self):
        self.w.sim_tx.sim.handle = lambda line: (["PONG RXFW 1.0"] if line.strip().upper() == "PING" else ["?"])
        self.tx.port.select_port(SIM_PORT_NAME)
        self.tx.port.toggle_connect()
        self.pump(300)
        self.assertFalse(self.tx.port.is_open())
        self.assertIn("RX board's Teensy (wrong port)", self.tx.lbl_fwmsg.text())

    def test_esc_stops_rx(self):
        self.connect_rx()
        self.arm()
        self.start()
        self.run_sim(50)
        self.assertTrue(self.dev.streaming)
        self.w.sc_stop.activated.emit()
        self.pump(50)
        self.assertFalse(self.dev.streaming)
        self.assertTrue(self.dev.armed)
        self.assertFalse(self.rx.acq_running)

    def test_gain_controls(self):
        self.connect_rx()
        self.rx.rb_high.setChecked(True)
        self.rx.sp_vga_db.setValue(14.9)
        self.pump(250)
        self.assertEqual(self.rx.vga.value(), 192)
        self.assertEqual((self.dev.gain_low, self.dev.pot), (False, 192))
        self.assertIn("×5.59", self.rx.lbl_vga.text())
        self.rx.sp_fa.setValue(40.0)
        self.assertIn("Total gain @ 40.0 kHz", self.rx.lbl_total.text())
        self.rx.vga.setValue(0)
        self.pump(250)
        self.assertIn("57.8 dB (×778)", self.rx.lbl_total.text())
        self.assertIn("6.43 mVpp", self.rx.lbl_total.text())

    def test_gain_stage_warning(self):
        self.dev.burst = BurstConfig(freq_hz=10e3, amp_v=4.5e-3, continuous=True, sync_wire=False)
        self.connect_rx()
        self.rx.rb_high.setChecked(True)
        self.arm()
        self.start()
        self.run_sim(100)
        self.rx._dirty = True
        self.rx.redraw()
        self.assertIn("Gain stage may be clipping", self.rx.lbl_gs.text())
        self.rx.rb_low.setChecked(True)
        self.pump(250)
        self.run_sim(100)
        self.rx._dirty = True
        self.rx.redraw()
        self.assertEqual(self.rx.lbl_gs.text(), "")

    def test_clip_indicator(self):
        self.dev.burst = BurstConfig(freq_hz=40e3, amp_v=0.1, continuous=True, sync_wire=False)
        self.connect_rx()
        self.arm()
        self.start()
        self.run_sim(50)
        self.assertGreater(self.rx.clip_frames, 0)
        self.rx.redraw()
        self.assertIn("#c0392b", self.rx.lbl_clip.styleSheet())

    def test_port_used_by_other_tab_is_greyed(self):
        self.tx.port.open_port_name = lambda: SIM_RX_PORT_NAME
        self.rx.port.update_disabled()
        idx = self.rx.port.cb_port.findData(SIM_RX_PORT_NAME)
        self.assertFalse(self.rx.port.cb_port.model().item(idx).isEnabled())

    def test_unplug(self):
        self.connect_rx()
        self.arm()
        self.link.unplug()
        self.pump(50)
        self.assertFalse(self.rx.port.is_open())
        self.assertEqual(self.rx.lbl_board.text(), "Not connected")
        self.assertIn("Serial port lost", self.log())

    def test_calibration_file(self):
        self.rx.sp_lna.setValue(9.4)
        self.rx.sp_cal.setValue(1.1)
        self.rx.sp_sysdelay.setValue(12.5)
        path = os.path.join(self.tmp.name, "cal.json")
        self.assertTrue(self.rx.save_calibration(path))
        self.rx.sp_lna.setValue(8.0)
        self.rx.sp_cal.setValue(1.0)
        self.assertTrue(self.rx.load_calibration(path))
        self.assertAlmostEqual(self.rx.sp_lna.value(), 9.4)
        self.assertAlmostEqual(self.rx.sp_cal.value(), 1.1)
        self.assertAlmostEqual(self.rx.sp_sysdelay.value(), 12.5)

    def test_settings_roundtrip(self):
        self.rx.rb_high.setChecked(True)
        self.rx.vga.setValue(77)
        self.rx.rb_capture.setChecked(True)
        self.rx.cb_syncwire.setChecked(True)
        self.rx.cb_trig.setCurrentIndex(2)
        self.rx.sp_len.setValue(12.0)
        self.w.close()
        w2 = MainWindow(sim=True, settings=self.settings, sim_realtime=False)
        try:
            self.assertTrue(w2.rx.rb_high.isChecked())
            self.assertEqual(w2.rx.vga.value(), 77)
            self.assertTrue(w2.rx.rb_capture.isChecked())
            self.assertEqual(w2.rx.cb_trig.currentData(), "SYNC")
            self.assertAlmostEqual(w2.rx.sp_len.value(), 12.0)
        finally:
            w2.close()

    def test_level_counts_conversion(self):
        self.rx.sp_level.setValue(244.1)
        self.assertEqual(self.rx.level_counts(), round(0.2441 / LSB_V))


if __name__ == "__main__":
    unittest.main()
