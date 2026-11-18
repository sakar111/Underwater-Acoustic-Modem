"""Main window: one tab per board + a shared serial log."""
from __future__ import annotations

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QTabWidget,
                               QVBoxLayout, QWidget)

from .links import SimTxLink
from .rx_link import SimRxLink
from .rx_tab import POWER_PROCEDURE, RxTab
from .sim_rx import BurstConfig, SimRxDevice
from .tx_tab import TxTab

BENCH_RULES = """\
<h3>Bench rules (TX board)</h3>
<ul>
<li><b>Power-up:</b> bench supply 12 V with a current limit of <b>≥ 0.5 A</b> (at 0.3 A the
converters' start-up surge latches the board at ≈ 2 V). Set 12 V with the output off, then switch
the output on. <b>Never exceed 14 V</b> input.</li>
<li><b>Order:</b> turn on 12 V first, then <b>Arm</b>. To power down: <b>Stop → Disarm</b>, then
switch off 12 V. USB may stay plugged in: while disarmed the Teensy drives nothing into the
board.</li>
<li><b>Scope:</b> never clip a scope ground lead to AMP_A / AMP_B or to the hydrophone wires —
scope ground is board GND, so this shorts a power-amp output. Probe each leg to board GND and use
the scope's <b>A − B</b> math.</li>
<li>Keep the hydrophone's cable end and the J2 terminal <b>dry</b>.</li>
<li><b>Drive levels (S1n):</b> ≤ 50 Vpp continuous; 80 Vpp only in bursts with &lt; 10 % duty.
In air keep ≤ 10–20 Vpp, mostly at 20–60 kHz, short runs or bursts. Close-range aquarium tests:
a few Vpp is enough.</li>
<li><b>Thermal:</b> continuous operation guideline f × Vpp ≲ 2.3 × 10⁶ (≈ 25 Vpp at 90 kHz,
46 Vpp at 50 kHz). Prefer <b>Burst</b> for high levels.</li>
<li>The Teensy is powered from USB only; its VIN / 3.3 V pins are not connected to the board.</li>
<li>Only one program can hold a COM port: close the Arduino Serial Monitor before connecting.</li>
</ul>
<p>Estimate: Vpp = 2 × A_dac × G_lvl(D) × 27.31 × cal, with
G_lvl(D) = ((256 − D)/256 × 10 kΩ + 100 Ω) / 10 kΩ. Accuracy ≈ ±5 %; measure with the scope and set
the calibration factor.</p>
"""


class MainWindow(QMainWindow):
    def __init__(self, sim: bool = False, settings: QSettings | None = None, sim_realtime: bool = True):
        super().__init__()
        self.setWindowTitle("Acoustic Modem Bench")
        self.resize(1500, 960)
        self.settings = settings if settings is not None else QSettings("UNR-AcousticModem", "ModemBench")

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        self.tabs = QTabWidget()
        self.sim_tx = SimTxLink(parent=self) if sim else None
        self.sim_rx = SimRxLink(SimRxDevice(burst=BurstConfig()), parent=self, realtime=sim_realtime) if sim else None
        self.tx = TxTab(sim_link=self.sim_tx)
        self.rx = RxTab(sim_link=self.sim_rx, tx_state=self.tx.tx_state)
        self.tx.port.set_peer(self.rx.port)
        self.rx.port.set_peer(self.tx.port)
        self.tabs.addTab(self.tx, "TX")
        self.tabs.addTab(self.rx, "RX")
        root.addWidget(self.tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(170)
        self.log.setFont(QFont("Consolas", 9))
        root.addWidget(QLabel("Serial log"))
        root.addWidget(self.log)

        self._last_status: dict[str, str] = {}
        for tag, bar in (("TX", self.tx.port), ("RX", self.rx.port)):
            bar.line_received.connect(lambda s, t=tag: self.on_received(t, s))
            bar.line_sent.connect(lambda s, t=tag: self.on_sent(t, s))
            bar.state_changed.connect(lambda b, t=tag: self.log.appendPlainText(
                f"[{t}] ── {'connected' if b else 'disconnected'} ──"))
            bar.error.connect(lambda s, t=tag: self.log.appendPlainText(f"[{t}] !! {s}"))

        # menu + shortcuts
        m_help = self.menuBar().addMenu("&Help")
        act = QAction("Bench rules (TX)…", self)
        act.triggered.connect(self.show_help)
        m_help.addAction(act)
        act = QAction("RX power procedure…", self)
        act.triggered.connect(lambda: QMessageBox.information(self, "RX power procedure", POWER_PROCEDURE))
        m_help.addAction(act)
        self.sc_stop = QShortcut(QKeySequence("Esc"), self)
        self.sc_stop.activated.connect(self.stop_all)

        if sim:                      # the simulated RX hears what the simulated TX sends
            self._sim_follow = QTimer(self)
            self._sim_follow.setInterval(250)
            self._sim_follow.timeout.connect(self._follow_tx_sim)
            self._sim_follow.start()

        self.tx.load_settings(self.settings)
        self.rx.load_settings(self.settings)

    def stop_all(self):
        """Esc: TX STOP + RX STREAM OFF / ABORT."""
        self.tx.on_stop()
        self.rx.on_stop()

    def _follow_tx_sim(self):
        """--sim: drive the fake RX signal from the simulated TX settings (0.5 m of water)."""
        st = self.tx.tx_state()
        if not (st.get("connected") and self.tx.port.link is self.sim_tx):
            return
        dev = self.sim_rx.dev
        b = dev.burst or BurstConfig()
        b.freq_hz = float(st["freq"])
        b.continuous = st["mode"] != "BURST"
        b.on_us, b.period_us = float(st["on_us"]), float(st["per_us"])
        b.amp_v = 2e-4 * float(st["est_vpp"]) if st.get("running") else 0.0   # ~0.5 mVpp per 2.5 Vpp
        b.sync_wire = bool(st.get("sync"))
        dev.burst = b

    def show_help(self):
        QMessageBox.information(self, "Bench rules", BENCH_RULES)

    def on_received(self, tag: str, s: str):
        if s.startswith("STATUS"):          # only log status lines when something changed
            if self._last_status.get(tag) == s:
                return
            self._last_status[tag] = s
        self.log.appendPlainText(f"[{tag}] ← {s}")

    def on_sent(self, tag: str, s: str):
        if s != "STATUS":                  # keep the log readable (polled every second)
            self.log.appendPlainText(f"[{tag}] → {s}")

    def shutdown(self):
        """DISARM + close every board connection."""
        self.tx.port.disconnect_port()
        self.rx.port.disconnect_port()

    def closeEvent(self, ev):
        try:
            self.tx.save_settings(self.settings)
            self.rx.save_settings(self.settings)
            self.settings.sync()
        finally:
            self.shutdown()
        super().closeEvent(ev)
