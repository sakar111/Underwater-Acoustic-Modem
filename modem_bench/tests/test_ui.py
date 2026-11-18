"""Drive the app offscreen against the simulated TX Teensy (spec §6.4, §9.1)."""
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import _paths  # noqa: F401,E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from bench.links import SIM_PORT_NAME  # noqa: E402
from bench.main_window import MainWindow  # noqa: E402

app = QApplication.instance() or QApplication([])


class AppHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(os.path.join(self.tmp.name, "t.ini"), QSettings.Format.IniFormat)
        self.w = MainWindow(sim=True, settings=self.settings)
        self.tx = self.w.tx
        self.sim = self.w.sim_tx.sim
        self.sent = []
        self.tx.port.line_sent.connect(self.sent.append)
        self.tx.confirm_arm = lambda: True
        self.w.show()

    def tearDown(self):
        self.w.close()
        self.tmp.cleanup()

    def pump(self, ms=300):
        QTest.qWait(ms)

    def connect_sim(self):
        self.assertTrue(self.tx.port.select_port(SIM_PORT_NAME))
        self.tx.port.toggle_connect()
        self.pump(300)
        self.assertTrue(self.tx.identified)

    def arm(self):
        self.tx.btn_arm.click()
        self.pump(300)
        self.assertTrue(self.tx.armed)

    def log(self):
        return self.w.log.toPlainText()


class TestTxTab(AppHarness):
    def test_connect_sequence(self):
        self.connect_sim()
        self.assertEqual(self.sent[0], "PING")
        self.assertEqual(self.sent[1], "POT 255")
        self.assertEqual(self.sent[2], "AMP 0")          # unknown state: silence first
        self.assertIn("STATUS", self.sent)
        self.assertIn("Board powered — not armed", self.tx.lbl_power.text())
        self.assertFalse(self.tx.btn_start.isEnabled())
        self.assertTrue(self.tx.btn_arm.isEnabled())
        self.assertIn("[TX] → PING", self.log())
        self.assertIn("[TX] ← PONG TXFW", self.log())

    def test_arm_set_level_and_start(self):
        self.connect_sim()
        self.arm()
        self.assertIn("ARMED", self.tx.lbl_power.text())
        self.tx.freq.setValue(25000)
        self.tx.amp.setValue(200)
        self.tx.gain.setValue(55)                        # pot 200
        self.pump(400)
        self.assertEqual((self.sim.amp, self.sim.pot, self.sim.freq), (200, 200, 25000))
        self.assertIn("2.5 Vpp", self.tx.lbl_est.text())
        self.assertTrue(self.tx.btn_start.isEnabled())
        self.tx.btn_start.click()
        self.pump(300)
        self.assertTrue(self.sim.running)
        self.assertIn("TRANSMITTING", self.tx.lbl_run.text())
        self.assertNotIn("ERR", self.log())

    def test_cannot_exceed_limit(self):
        self.connect_sim()
        self.arm()
        self.tx.amp.setValue(200)
        self.tx.gain.setValue(55)
        self.pump(400)
        self.tx.btn_start.click()
        self.pump(200)
        self.tx.amp.setValue(1400)
        self.tx.gain.setValue(255)                       # 77 Vpp > 20
        self.pump(400)
        self.assertFalse(self.tx.level_ok())
        self.assertFalse(self.tx.btn_start.isEnabled())
        self.assertIn("Above the safety limit", self.tx.lbl_warn.text())
        self.assertIn("#c0392b", self.tx.lbl_est.styleSheet())
        self.assertEqual((self.sim.amp, self.sim.pot), (200, 200))   # nothing sent
        self.assertLessEqual(self.sim.est(), self.sim.limit)
        self.assertNotIn("ERR", self.log())

    def test_safe_ordering_when_lowering_limit(self):
        self.connect_sim()
        self.arm()
        self.tx.limit.setValue(50)
        self.tx.amp.setValue(900)
        self.tx.gain.setValue(255)                       # pot 0 -> 49.8 Vpp
        self.pump(400)
        self.assertEqual((self.sim.amp, self.sim.pot, self.sim.limit), (900, 0, 50.0))
        # one edit batch: lower the amplitude AND the limit
        self.tx.amp.setValue(100)
        self.tx.limit.setValue(10)
        self.pump(400)
        self.assertEqual((self.sim.amp, self.sim.limit), (100, 10.0))
        self.assertNotIn("ERR", self.log())
        # and back up
        self.tx.limit.setValue(50)
        self.tx.amp.setValue(900)
        self.pump(400)
        self.assertEqual((self.sim.amp, self.sim.limit), (900, 50.0))
        self.assertNotIn("ERR", self.log())

    def test_thermal_warning(self):
        self.connect_sim()
        self.arm()
        self.tx.freq.setValue(90000)
        self.tx.limit.setValue(50)
        self.tx.amp.setValue(900)
        self.tx.gain.setValue(255)
        self.pump(400)
        self.assertIn("overheat", self.tx.lbl_warn.text())
        self.tx.btn_start.click()
        self.pump(300)
        self.assertIn("WARN THERMAL", self.tx.lbl_fwmsg.text())
        self.assertIn("#b9770e", self.tx.lbl_fwmsg.text())
        # burst mode clears the continuous thermal warning
        self.tx.rb_burst.setChecked(True)
        self.pump(400)
        self.assertNotIn("overheat", self.tx.lbl_warn.text())
        self.assertTrue(self.sim.burst)

    def test_burst_settings(self):
        self.connect_sim()
        self.tx.rb_burst.setChecked(True)
        self.tx.burst_on.setValue(1.0)
        self.tx.burst_per.setValue(100.0)
        self.pump(400)
        self.assertEqual((self.sim.burst, self.sim.on_us, self.sim.per_us), (True, 1000, 100000))
        self.tx.burst_per.setValue(0.5)                  # invalid: period <= on-time
        self.pump(400)
        self.assertIn("period must be longer", self.tx.lbl_warn.text())
        self.assertEqual(self.sim.per_us, 100000)

    def test_cal_and_sweep(self):
        self.connect_sim()
        self.tx.cal.setValue(1.1)
        self.tx.cb_sweep.setChecked(True)
        self.tx.sweep_f1.setValue(20000)
        self.tx.sweep_f2.setValue(60000)
        self.tx.sweep_ms.setValue(50)
        self.pump(400)
        self.assertAlmostEqual(self.sim.cal, 1.1)
        self.assertTrue(self.sim.sweep_on)
        self.assertEqual(self.sim.sweep, (20000, 60000, 50))
        self.tx.cb_sweep.setChecked(False)
        self.pump(400)
        self.assertFalse(self.sim.sweep_on)

    def test_arm_refused_without_power(self):
        self.sim.board_powered = False
        self.connect_sim()
        self.assertIn("12 V not detected", self.tx.lbl_power.text())
        self.tx.btn_arm.click()
        self.pump(300)
        self.assertFalse(self.sim.armed)
        self.assertFalse(self.tx.armed)
        self.assertIn("ERR NOPOWER", self.tx.lbl_fwmsg.text())
        self.assertFalse(self.tx.btn_start.isEnabled())

    def test_power_lost_while_armed(self):
        self.connect_sim()
        self.arm()
        self.tx.btn_start.click()
        self.pump(200)
        self.sim.board_powered = False
        self.pump(1500)                                   # sim watchdog: 2 failed reads
        self.assertFalse(self.sim.armed)
        self.assertFalse(self.tx.armed)
        self.assertIn("WARN NOPOWER", self.tx.lbl_fwmsg.text())

    def test_disconnect_disarms(self):
        self.connect_sim()
        self.arm()
        self.tx.btn_start.click()
        self.pump(200)
        self.assertTrue(self.sim.running)
        self.tx.port.toggle_connect()                     # Disconnect
        self.pump(100)
        self.assertFalse(self.sim.armed)
        self.assertFalse(self.sim.running)
        self.assertFalse(self.tx.port.is_open())
        self.assertEqual(self.tx.lbl_power.text(), "Not connected")

    def test_close_while_transmitting_disarms(self):
        self.connect_sim()
        self.arm()
        self.tx.btn_start.click()
        self.pump(200)
        self.w.close()
        self.assertFalse(self.sim.armed)
        self.assertFalse(self.sim.running)

    def test_esc_stops(self):
        self.connect_sim()
        self.arm()
        self.tx.btn_start.click()
        self.pump(200)
        self.w.sc_stop.activated.emit()
        self.pump(200)
        self.assertFalse(self.sim.running)
        self.assertTrue(self.sim.armed)

    def test_unplug(self):
        self.connect_sim()
        self.arm()
        self.w.sim_tx.unplug()
        self.pump(100)
        self.assertFalse(self.tx.port.is_open())
        self.assertEqual(self.tx.lbl_power.text(), "Not connected")
        self.assertIn("Serial port lost", self.log())
        self.assertFalse(self.tx.btn_arm.isEnabled())

    def test_err_resyncs(self):
        self.connect_sim()
        self.arm()
        self.tx.send("AMP 5000")                         # provoke an ERR
        self.pump(300)
        self.assertIn("ERR AMP", self.tx.lbl_fwmsg.text())
        self.assertIsNotNone(self.tx.fw_level)           # re-learned from STATUS
        self.tx.amp.setValue(300)
        self.pump(400)
        self.assertEqual(self.sim.amp, 300)

    def test_wrong_device_gets_nothing(self):
        # A device that answers PING with something else must not receive TX commands.
        self.sim.handle = lambda line: ["PONG RXFW 1.0"] if line.strip().upper() == "PING" else ["?"]
        self.tx.port.select_port(SIM_PORT_NAME)
        self.tx.port.toggle_connect()
        self.pump(300)
        self.assertFalse(self.tx.identified)
        self.assertEqual(set(self.sent), {"PING"})
        self.assertIn("not the TX firmware", self.tx.lbl_fwmsg.text())
        self.tx.port.toggle_connect()
        self.assertEqual(set(self.sent), {"PING"})       # no DISARM to a foreign device

    def test_settings_roundtrip(self):
        self.tx.freq.setValue(40000)
        self.tx.amp.setValue(350)
        self.tx.limit.setValue(12.5)
        self.tx.rb_burst.setChecked(True)
        self.w.close()
        w2 = MainWindow(sim=True, settings=self.settings)
        try:
            self.assertEqual(w2.tx.freq.value(), 40000)
            self.assertEqual(w2.tx.amp.value(), 350)
            self.assertAlmostEqual(w2.tx.limit.value(), 12.5)
            self.assertTrue(w2.tx.rb_burst.isChecked())
        finally:
            w2.close()


if __name__ == "__main__":
    unittest.main()
