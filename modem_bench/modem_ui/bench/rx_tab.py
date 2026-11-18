"""RX tab (spec RX §6): connection, arm/disarm, gain, acquisition, live plots, measurements, saving.

Talks to rx_firmware.ino through a frame-aware link (bench.rx_link). Text replies drive the
state; binary frames feed a 2 s NumPy ring (stream) or are assembled per capture id (capture).
Plots are redrawn from a 25 fps QTimer, never from the serial callback.
"""
from __future__ import annotations

import json
import math
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QGraphicsItem
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
                               QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QRadioButton, QScrollArea, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from . import rx_analysis as A
from . import rx_io
from . import rx_protocol as P
from .links import LineLink
from .rx_link import SIM_RX_PORT_NAME, RxSerialLink
from .rx_model import (BAND_HI_HZ, BAND_LO_HZ, CLIP_HI, CLIP_LO, FS_HZ, G_LNA_NOM, GAIN_STAGE_WARN_VPP,
                       LNA_GAIN_MAX, LNA_GAIN_MIN, LSB_V, Calibration, RxGain, VgaTable,
                       gain_stage_clip_estimate, vga_code_for_db, vga_gain_dc)
from .widgets import PortBar, SpinSlider

RED, ORANGE, GREEN, GREY = "#c0392b", "#b9770e", "#1e8449", "#7f8c8d"
RING_SAMPLES = 800_000                # 2 s at 400 kSPS
SPEC_NFFT, SPEC_HOP = 1024, 512
SPEC_COLS = 1 + (FS_HZ - SPEC_NFFT) // SPEC_HOP     # rolling 1 s
FFT_N = 8192
CAP_PRE_MAX = 32768

POWER_PROCEDURE = """\
<h3>RX board power procedure</h3>
<ol>
<li><b>Power up:</b> bench supply 12 V (limit ≈ 0.2 A), output ON. Check that the green LED
<b>D6</b> is on. <b>Then</b> plug in the RX Teensy's USB (or connect if it is already enumerated).</li>
<li>Connect in this tab, then <b>Arm</b>.</li>
<li><b>Power down:</b> Stop → <b>Disarm</b> → <b>unplug the RX Teensy's USB</b> → switch the 12 V off.</li>
<li>Do not leave the RX Teensy on USB with the RX board's 12 V off for long: the I2C pull-ups go to
the Teensy's 3.3 V and feed the unpowered AD5245 (not destructive, but outside its power-up order).</li>
<li>Rule of thumb: 12 V before USB; USB out before 12 V off.</li>
</ol>
<h3>Bench notes</h3>
<ul>
<li>Inputs to J2 must stay small: feed <b>≤ 50 mVpp</b> through a divider (10 kΩ : 100 Ω = 100:1).
<b>Never</b> connect the TX power-amp outputs (AMP_A/AMP_B, up to 76 Vpp) to the RX board.</li>
<li>Close-range water tests (≤ 1 m): start with the TX at a few Vpp, RX in <b>LOW</b> coarse gain,
VGA code 0. Raise the gain until the ADC signal is 1–3 Vpp.</li>
<li>The front-end setting must match jumper <b>P2</b> (JFET LNA 1–2, PA-4 2–3). The PA-4 is powered
from VIN through P1.</li>
<li>Scope ground is board GND. Probe TP5/TP7/TP8 with a ground spring and 20 MHz bandwidth limit.</li>
<li>Sync wire (delay measurements): TX Teensy pin 2 → 2.2 kΩ (at the RX end) → RX Teensy pin 2,
twisted with a GND wire. Enable "SYNC output" in the TX tab.</li>
</ul>
"""


@dataclass
class Capture:
    cap_id: int
    n: int
    pre: int
    trig_index: int
    words: np.ndarray                     # uint16 sample words (code | flags), length n
    valid: np.ndarray                     # bool, False where frames were missing
    incomplete: bool
    settings_changed: bool
    gains: list[int]
    pots: list[int]
    clip: bool
    overflow: bool
    trigger_source: str
    level_counts: int
    mid: int
    sync_ok: bool
    meta: dict = field(default_factory=dict)

    @property
    def codes(self) -> np.ndarray:
        return (self.words & P.CODE_MASK).astype(np.uint16)

    @property
    def first_index(self) -> int:
        return self.trig_index - self.pre


class RxTab(QWidget):
    def __init__(self, sim_link: LineLink | None = None, tx_state=None, parent=None):
        super().__init__(parent)
        pg.setConfigOptions(antialias=False)
        self.tx_state = tx_state or (lambda: None)       # read-only TX info (spec §6.1)
        self.confirm_arm = self._confirm_arm_dialog      # replaced in tests
        self.ask_save_path = self._ask_save_path         # replaced in tests
        self._reset_state()
        self.vga_table: VgaTable | None = None
        self.calibration_path = ""

        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 6, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(left)
        scroll.setMinimumWidth(560)
        split.addWidget(scroll)

        # ---- 1. connection ------------------------------------------------
        self.port = PortBar("RX", sim_link=sim_link, serial_link=RxSerialLink(), sim_name=SIM_RX_PORT_NAME)
        self.port.cb_port.setMinimumWidth(150)
        self.port.before_close = self._shutdown_before_close
        lv.addWidget(self.port)
        self.lbl_banner = QLabel("")
        self.lbl_banner.setWordWrap(True)
        self.lbl_banner.setTextFormat(Qt.TextFormat.RichText)
        lv.addWidget(self.lbl_banner)

        # ---- 2. board -------------------------------------------------------
        g = QGroupBox("Board")
        gl = QGridLayout(g)
        self.lbl_board = QLabel("Not connected")
        self.lbl_board.setWordWrap(True)
        self.btn_arm = QPushButton("Arm (12 V is ON)")
        self.btn_disarm = QPushButton("Disarm")
        self.btn_help = QPushButton("Power procedure…")
        gl.addWidget(self.lbl_board, 0, 0, 1, 3)
        gl.addWidget(self.btn_arm, 1, 0)
        gl.addWidget(self.btn_disarm, 1, 1)
        gl.addWidget(self.btn_help, 1, 2)
        self.lbl_fwmsg = QLabel("")
        self.lbl_fwmsg.setWordWrap(True)
        self.lbl_fwmsg.setTextFormat(Qt.TextFormat.RichText)
        gl.addWidget(self.lbl_fwmsg, 2, 0, 1, 3)
        lv.addWidget(g)

        # ---- 3. gain --------------------------------------------------------
        g = QGroupBox("Gain")
        gl = QGridLayout(g)
        r = 0
        self.cb_frontend = QComboBox()
        self.cb_frontend.addItem("JFET LNA (P2 = 1–2)", "LNA")
        self.cb_frontend.addItem("PA-4 +20 dB (P2 = 2–3)", "PA4")
        gl.addWidget(QLabel("Front end"), r, 0)
        gl.addWidget(self.cb_frontend, r, 1)
        lbl = QLabel("must match jumper P2")
        lbl.setStyleSheet(f"color:{GREY}")
        gl.addWidget(lbl, r, 2)
        r += 1
        self.sp_lna = QDoubleSpinBox()
        self.sp_lna.setRange(LNA_GAIN_MIN, LNA_GAIN_MAX)
        self.sp_lna.setDecimals(2)
        self.sp_lna.setSingleStep(0.1)
        self.sp_lna.setValue(G_LNA_NOM)
        self.sp_lna.setPrefix("×")
        gl.addWidget(QLabel("LNA gain"), r, 0)
        gl.addWidget(self.sp_lna, r, 1)
        r += 1
        self.rb_high = QRadioButton("HIGH (×58)")
        self.rb_low = QRadioButton("LOW (×7.1)")
        self.rb_low.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self.rb_high)
        bg.addButton(self.rb_low)
        h = QHBoxLayout()
        h.addWidget(self.rb_low)
        h.addWidget(self.rb_high)
        h.addStretch(1)
        gl.addWidget(QLabel("Coarse gain"), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.vga = SpinSlider(0, 255, 1, 0, "")
        self.vga.spin.setPrefix("code ")
        gl.addWidget(QLabel("VGA (AD5245)"), r, 0)
        gl.addWidget(self.vga, r, 1, 1, 2)
        r += 1
        self.sp_vga_db = QDoubleSpinBox()
        self.sp_vga_db.setRange(0.0, 45.0)
        self.sp_vga_db.setDecimals(1)
        self.sp_vga_db.setSingleStep(0.5)
        self.sp_vga_db.setSuffix(" dB")
        self.sp_vga_db.setKeyboardTracking(False)
        self.lbl_vga = QLabel()
        gl.addWidget(QLabel("VGA gain"), r, 0)
        gl.addWidget(self.sp_vga_db, r, 1)
        gl.addWidget(self.lbl_vga, r, 2)
        r += 1
        self.sp_fa = QDoubleSpinBox()
        self.sp_fa.setRange(1.0, 199.0)
        self.sp_fa.setDecimals(1)
        self.sp_fa.setValue(40.0)
        self.sp_fa.setSuffix(" kHz")
        self.cb_fa_auto = QCheckBox("auto (detected tone)")
        h = QHBoxLayout()
        h.addWidget(self.sp_fa)
        h.addWidget(self.cb_fa_auto)
        h.addStretch(1)
        gl.addWidget(QLabel("Analysis freq."), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.sp_cal = QDoubleSpinBox()
        self.sp_cal.setRange(0.1, 10.0)
        self.sp_cal.setDecimals(3)
        self.sp_cal.setSingleStep(0.01)
        self.sp_cal.setValue(1.0)
        self.btn_cal_load = QPushButton("Load cal…")
        self.btn_cal_save = QPushButton("Save cal…")
        h = QHBoxLayout()
        h.addWidget(self.sp_cal)
        h.addWidget(self.btn_cal_load)
        h.addWidget(self.btn_cal_save)
        h.addStretch(1)
        gl.addWidget(QLabel("Cal factor"), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.lbl_total = QLabel()
        self.lbl_total.setWordWrap(True)
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        self.lbl_total.setFont(f)
        gl.addWidget(self.lbl_total, r, 0, 1, 3)
        lv.addWidget(g)

        # ---- 4. acquisition -------------------------------------------------
        g = QGroupBox("Acquisition")
        gl = QGridLayout(g)
        r = 0
        self.rb_stream = QRadioButton("Stream")
        self.rb_capture = QRadioButton("Triggered capture")
        self.rb_stream.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self.rb_stream)
        bg.addButton(self.rb_capture)
        h = QHBoxLayout()
        h.addWidget(self.rb_stream)
        h.addWidget(self.rb_capture)
        h.addStretch(1)
        gl.addWidget(QLabel("Mode"), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.cb_trig = QComboBox()
        self.cb_trig.addItem("Immediate", "NONE")
        self.cb_trig.addItem("Level", "LEVEL")
        self.cb_trig.addItem("Sync wire", "SYNC")
        self.cb_syncwire = QCheckBox("sync wire connected")
        h = QHBoxLayout()
        h.addWidget(self.cb_trig)
        h.addWidget(self.cb_syncwire)
        h.addStretch(1)
        gl.addWidget(QLabel("Trigger"), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.sp_level = QDoubleSpinBox()
        self.sp_level.setRange(1.3, 2499.0)
        self.sp_level.setDecimals(1)
        self.sp_level.setValue(244.1)
        self.sp_level.setSuffix(" mV at ADC")
        self.sp_level.setKeyboardTracking(False)
        self.lbl_level = QLabel()
        gl.addWidget(QLabel("Level"), r, 0)
        gl.addWidget(self.sp_level, r, 1)
        gl.addWidget(self.lbl_level, r, 2)
        r += 1
        self.sp_pre = QDoubleSpinBox()
        self.sp_pre.setRange(0.0, CAP_PRE_MAX / 400)
        self.sp_pre.setDecimals(2)
        self.sp_pre.setValue(0.5)
        self.sp_pre.setSuffix(" ms pre")
        self.sp_len = QDoubleSpinBox()
        self.sp_len.setRange(0.1, 10000.0)
        self.sp_len.setDecimals(1)
        self.sp_len.setValue(8.0)
        self.sp_len.setSuffix(" ms total")
        h = QHBoxLayout()
        h.addWidget(self.sp_pre)
        h.addWidget(self.sp_len)
        h.addStretch(1)
        gl.addWidget(QLabel("Window"), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.rb_single = QRadioButton("Single")
        self.rb_auto = QRadioButton("Auto-repeat")
        self.rb_single.setChecked(True)
        bg = QButtonGroup(self)
        bg.addButton(self.rb_single)
        bg.addButton(self.rb_auto)
        self.sp_timeout = QDoubleSpinBox()
        self.sp_timeout.setRange(0.0, 600.0)
        self.sp_timeout.setDecimals(1)
        self.sp_timeout.setValue(0.0)
        self.sp_timeout.setSpecialValueText("no timeout")
        self.sp_timeout.setSuffix(" s timeout")
        h = QHBoxLayout()
        h.addWidget(self.rb_single)
        h.addWidget(self.rb_auto)
        h.addWidget(self.sp_timeout)
        h.addStretch(1)
        gl.addWidget(QLabel("Repeat"), r, 0)
        gl.addLayout(h, r, 1, 1, 2)
        r += 1
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop  (Esc)")
        self.lbl_acq = QLabel("Idle")
        h = QHBoxLayout()
        h.addWidget(self.btn_start)
        h.addWidget(self.btn_stop)
        h.addWidget(self.lbl_acq, 1)
        gl.addLayout(h, r, 0, 1, 3)
        lv.addWidget(g)

        # ---- 5. measurements ------------------------------------------------
        g = QGroupBox("Measurements")
        gl = QGridLayout(g)
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["Quantity", "Value"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.setMinimumHeight(200)
        gl.addWidget(self.tbl, 0, 0, 1, 4)
        self.sp_c = QDoubleSpinBox()
        self.sp_c.setRange(1300, 1700)
        self.sp_c.setValue(1480)
        self.sp_c.setSuffix(" m/s")
        self.sp_sysdelay = QDoubleSpinBox()
        self.sp_sysdelay.setRange(-10000, 10000)
        self.sp_sysdelay.setDecimals(1)
        self.sp_sysdelay.setSuffix(" µs sys. delay")
        self.sp_rvs = QDoubleSpinBox()
        self.sp_rvs.setRange(-260, -100)
        self.sp_rvs.setDecimals(1)
        self.sp_rvs.setValue(-206.0)
        self.sp_rvs.setSuffix(" dB re 1 V/µPa")
        self.sp_rvs.setToolTip("Hydrophone receive sensitivity (AS-1 data sheet; −206 dB in the project notes: verify)")
        self.cb_spl = QCheckBox("SPL")
        gl.addWidget(QLabel("c"), 1, 0)
        gl.addWidget(self.sp_c, 1, 1)
        gl.addWidget(self.sp_sysdelay, 1, 2, 1, 2)
        gl.addWidget(self.cb_spl, 2, 0)
        gl.addWidget(self.sp_rvs, 2, 1)
        self.btn_copy = QPushButton("Copy")
        gl.addWidget(self.btn_copy, 2, 3)
        self.lbl_repeat = QLabel("")
        self.lbl_repeat.setWordWrap(True)
        gl.addWidget(self.lbl_repeat, 3, 0, 1, 4)
        lv.addWidget(g)

        # ---- 6. status ------------------------------------------------------
        g = QGroupBox("Status")
        gl = QVBoxLayout(g)
        self.lbl_clip = QLabel("CLIP")
        self.lbl_clip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_clip.setMinimumWidth(60)
        self.lbl_clip_n = QLabel("clips: 0")
        h = QHBoxLayout()
        h.addWidget(self.lbl_clip)
        h.addWidget(self.lbl_clip_n)
        h.addStretch(1)
        gl.addLayout(h)
        self.lbl_gs = QLabel("")
        self.lbl_gs.setWordWrap(True)
        self.lbl_gs.setTextFormat(Qt.TextFormat.RichText)
        gl.addWidget(self.lbl_gs)
        self.lbl_counters = QLabel("")
        self.lbl_counters.setStyleSheet(f"color:{GREY}")
        gl.addWidget(self.lbl_counters)
        lv.addWidget(g)

        # ---- 7. save --------------------------------------------------------
        g = QGroupBox("Save")
        gl = QGridLayout(g)
        self.ed_notes = QLineEdit()
        self.ed_notes.setPlaceholderText("notes (saved in the metadata)")
        gl.addWidget(self.ed_notes, 0, 0, 1, 3)
        self.btn_save_npy = QPushButton("Save capture .npy")
        self.btn_save_csv = QPushButton("Save capture .csv")
        self.btn_save_snap = QPushButton("Save stream snapshot")
        self.sp_snap = QDoubleSpinBox()
        self.sp_snap.setRange(0.01, RING_SAMPLES / FS_HZ)
        self.sp_snap.setValue(1.0)
        self.sp_snap.setSuffix(" s")
        gl.addWidget(self.btn_save_npy, 1, 0)
        gl.addWidget(self.btn_save_csv, 1, 1)
        gl.addWidget(self.btn_save_snap, 2, 0)
        gl.addWidget(self.sp_snap, 2, 1)
        self.lbl_saved = QLabel("")
        self.lbl_saved.setWordWrap(True)
        self.lbl_saved.setStyleSheet(f"color:{GREY}")
        gl.addWidget(self.lbl_saved, 3, 0, 1, 3)
        lv.addWidget(g)
        lv.addStretch(1)

        # ---- plots ----------------------------------------------------------
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        h = QHBoxLayout()
        self.cb_span = QComboBox()
        for ms in (1, 2, 5, 10, 20, 50, 100):
            self.cb_span.addItem(f"{ms} ms", ms)
        self.cb_span.setCurrentIndex(3)
        self.cb_yunit = QComboBox()
        self.cb_yunit.addItem("V at ADC (AC)", "adc")
        self.cb_yunit.addItem("mV at hydrophone", "input")
        self.cb_fft_unit = QComboBox()
        self.cb_fft_unit.addItem("dBV at ADC", "adc")
        self.cb_fft_unit.addItem("dBV re input", "input")
        self.cb_avg = QComboBox()
        for n in (1, 4, 16):
            self.cb_avg.addItem(f"avg {n}", n)
        self.sp_spec_lo = QSpinBox()
        self.sp_spec_hi = QSpinBox()
        for sb, v in ((self.sp_spec_lo, -110), (self.sp_spec_hi, -10)):
            sb.setRange(-200, 40)
            sb.setValue(v)
            sb.setSuffix(" dB")
        for w_, t in ((self.cb_span, "Span"), (self.cb_yunit, "Time y"), (self.cb_fft_unit, "FFT y"),
                      (self.cb_avg, None), (self.sp_spec_lo, "Spectrogram"), (self.sp_spec_hi, "to")):
            if t:
                h.addWidget(QLabel(t))
            h.addWidget(w_)
        h.addStretch(1)
        rv.addLayout(h)
        psplit = QSplitter(Qt.Orientation.Vertical)
        rv.addWidget(psplit, 1)
        self.p_time = pg.PlotWidget()
        self.p_time.setLabel("bottom", "time", "ms")
        self.p_time.showGrid(x=True, y=True, alpha=0.3)
        self.c_time = self.p_time.plot(pen=pg.mkPen("#4fc3f7", width=1))
        self.l_clip = [pg.InfiniteLine(angle=0, pen=pg.mkPen(RED, style=Qt.PenStyle.DashLine)) for _ in range(2)]
        for ln in self.l_clip:
            self.p_time.addItem(ln)
        self.region = pg.LinearRegionItem(brush=pg.mkBrush(255, 255, 0, 30))
        self.region.setZValue(-10)
        self.region.hide()
        self.p_time.addItem(self.region)
        self.gap_lines: list[pg.InfiniteLine] = []
        self.p_fft = pg.PlotWidget()
        self.p_fft.setLabel("bottom", "frequency", "kHz")
        self.p_fft.showGrid(x=True, y=True, alpha=0.3)
        self.p_fft.setXRange(0, 200)
        band = pg.LinearRegionItem(values=(BAND_LO_HZ / 1e3, BAND_HI_HZ / 1e3), movable=False,
                                   brush=pg.mkBrush(100, 200, 100, 30))
        band.setZValue(-10)
        self.p_fft.addItem(band)
        self.c_fft = self.p_fft.plot(pen=pg.mkPen("#ffb74d", width=1))
        self.pk_marker = pg.ScatterPlotItem(size=8, brush=pg.mkBrush(RED))
        self.p_fft.addItem(self.pk_marker)
        self.pk_text = pg.TextItem(anchor=(0, 1), color="w")
        self.p_fft.addItem(self.pk_text)
        self.p_spec = pg.PlotWidget()
        self.p_spec.setLabel("bottom", "time", "s")
        self.p_spec.setLabel("left", "frequency", "kHz")
        self.img = pg.ImageItem()
        self.img.setColorMap(pg.colormap.get("viridis"))
        self.p_spec.addItem(self.img)
        for p_ in (self.p_time, self.p_fft, self.p_spec):
            psplit.addWidget(p_)
            for ax in ("left", "bottom"):        # axis text is costly to repaint at 25 fps: cache it
                p_.getAxis(ax).setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)
        for p_ in (self.p_time, self.p_fft):
            p_.disableAutoRange(axis="y")         # _smart_yrange: rescale only when needed
        psplit.setSizes([300, 250, 250])
        split.addWidget(right)
        split.setStretchFactor(1, 1)

        # ---- wiring ---------------------------------------------------------
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(120)
        self.debounce.timeout.connect(self.push_settings)
        self.poll = QTimer(self)
        self.poll.setInterval(1000)
        self.poll.timeout.connect(self._poll)
        self.refresh = QTimer(self)
        self.refresh.setInterval(40)
        self.refresh.timeout.connect(self.redraw)
        self.refresh.start()
        self.trig_timer = QTimer(self)
        self.trig_timer.setSingleShot(True)
        self.trig_timer.timeout.connect(self._on_trigger_timeout)
        self.repeat_timer = QTimer(self)
        self.repeat_timer.setSingleShot(True)
        self.repeat_timer.setInterval(100)
        self.repeat_timer.timeout.connect(self._issue_capture)

        self.btn_arm.clicked.connect(self.on_arm)
        self.btn_disarm.clicked.connect(self.on_disarm)
        self.btn_help.clicked.connect(lambda: QMessageBox.information(self, "RX power procedure", POWER_PROCEDURE))
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(self.on_stop)
        self.rb_high.toggled.connect(self.on_gain_edit)
        self.vga.valueChanged.connect(self.on_gain_edit)
        self.sp_vga_db.valueChanged.connect(self._on_vga_db)
        for w_ in (self.cb_frontend,):
            w_.currentIndexChanged.connect(self.refresh_labels)
        for w_ in (self.sp_lna, self.sp_cal, self.sp_fa, self.sp_level):
            w_.valueChanged.connect(self.on_ui_edit)
        self.cb_trig.currentIndexChanged.connect(self.on_ui_edit)
        self.cb_syncwire.toggled.connect(self.on_ui_edit)
        self.rb_stream.toggled.connect(self._on_mode_changed)
        self.region.sigRegionChangeFinished.connect(self._on_region)
        self.btn_copy.clicked.connect(self.copy_measurements)
        self.btn_cal_load.clicked.connect(lambda: self.load_calibration())
        self.btn_cal_save.clicked.connect(lambda: self.save_calibration())
        self.btn_save_npy.clicked.connect(lambda: self.save_capture("npy"))
        self.btn_save_csv.clicked.connect(lambda: self.save_capture("csv"))
        self.btn_save_snap.clicked.connect(lambda: self.save_snapshot())
        for w_ in (self.cb_span, self.cb_yunit, self.cb_fft_unit, self.cb_avg):
            w_.currentIndexChanged.connect(self._mark_dirty)
        for w_ in (self.sp_spec_lo, self.sp_spec_hi):
            w_.valueChanged.connect(self._mark_dirty)
        self.cb_avg.currentIndexChanged.connect(lambda _i: setattr(self, "_fft_avg", None))
        self.port.line_received.connect(self.on_line)
        self.port.frame_received.connect(self.on_frame)
        self.port.stats_changed.connect(self.on_link_stats)
        self.port.state_changed.connect(self.on_link_state)
        self.port.error.connect(lambda s: self.show_fw_msg(s, RED))

        self._sync_vga_db()
        self.refresh_labels()

    # ==================================================================== state
    def _reset_state(self):
        self.identified = False
        self.fw_version = ""
        self.armed = False
        self.power: bool | None = None
        self.mid = 2048
        self.fw_stream = False
        self.fw_cap = "IDLE"
        self.sent: dict[str, str] = {}
        self.acq_running = False
        self.acq_mode = "stream"
        self._expect_start: str | None = None
        self._ping_tries = 0
        self._closing = False               # shutdown sequence running: send nothing else
        # link / frame counters
        self.crc_errors = self.resync_bytes = self.frames = 0
        self.gaps = self.overflows = self.ferr_frames = self.clip_frames = 0
        self._clip_until = 0.0
        self._last_seq: int | None = None
        self._next_stream_index: int | None = None
        # PC stream ring
        self.ring = np.zeros(RING_SAMPLES, dtype=np.uint16)
        self.ring_total = 0                 # samples ever written to the ring
        self.gap_marks: deque[int] = deque(maxlen=200)   # ring_total positions of gaps
        self._spec_cols = np.zeros((0, SPEC_NFFT // 2 + 1), dtype=np.float32)   # dB
        self._spec_next = 0
        self._spec_frame = 0
        self._fft_avg = None
        self._fft_freqs = None
        # captures
        self._cap_frames: dict[int, list[P.Frame]] = {}
        self._cap_waiting: int | None = None
        self._cap_triggered = False
        self.capture: Capture | None = None
        self.captures_done = 0
        self.measurements: dict = {}
        self.history: deque[dict] = deque(maxlen=20)
        self._dirty = True
        self._gs_warn = False
        self._time_unit = self._fft_unit = ""

    def gain(self) -> RxGain:
        return RxGain(coarse="HIGH" if self.rb_high.isChecked() else "LOW", pot=self.vga.value(),
                      frontend=self.cb_frontend.currentData(), g_lna=self.sp_lna.value(),
                      cal=self.sp_cal.value(), vga_table=self.vga_table)

    def f_analysis(self) -> float:
        return self.sp_fa.value() * 1e3

    def level_counts(self) -> int:
        return max(1, min(2047, int(round(self.sp_level.value() * 1e-3 / LSB_V))))

    def capture_samples(self) -> tuple[int, int]:
        n = max(16, min(4_000_000, int(round(self.sp_len.value() * 400))))
        pre = max(0, min(min(CAP_PRE_MAX, n - 1), int(round(self.sp_pre.value() * 400))))
        return n, pre

    def trig_cmd(self) -> str:
        src = self.cb_trig.currentData()
        if src == "LEVEL":
            return f"TRIG LEVEL {self.level_counts()}"
        return f"TRIG {src}"

    def connected(self) -> bool:
        return self.port.is_open() and self.identified

    # ==================================================================== labels
    def refresh_labels(self, *_):
        g = self.gain()
        fa = self.f_analysis()
        d = self.vga.value()
        gv = g.vga_gain()
        src = "measured" if self.vga_table is not None else "model"
        self.lbl_vga.setText(f"code {d} → ×{gv:.2f} ({20 * math.log10(gv):.1f} dB, {src})")
        tot = g.total_mag(fa)
        self.lbl_total.setText(f"Total gain @ {fa / 1e3:.1f} kHz: {20 * math.log10(tot):.1f} dB (×{tot:.0f}) — "
                               f"ADC full scale = {_fmt_v(5.0 / tot)}pp at the hydrophone")
        lv = self.level_counts() * LSB_V
        self.lbl_level.setText(f"= {self.level_counts()} counts ≈ {_fmt_v(lv / tot)} at the hydrophone")
        sync_ok = self.cb_syncwire.isChecked()
        item = self.cb_trig.model().item(2)
        if item is not None:
            item.setEnabled(sync_ok)
        if not sync_ok and self.cb_trig.currentData() == "SYNC":
            self.cb_trig.setCurrentIndex(1)
        for w_ in (self.cb_trig, self.sp_pre, self.sp_len, self.rb_single, self.rb_auto, self.sp_timeout):
            w_.setEnabled(self.rb_capture.isChecked())
        self.sp_level.setEnabled(self.rb_capture.isChecked() and self.cb_trig.currentData() == "LEVEL")
        self.update_buttons()
        self._mark_dirty()

    def update_buttons(self):
        conn = self.connected()
        self.btn_arm.setEnabled(conn and not self.armed)
        self.btn_disarm.setEnabled(conn and self.armed)
        self.btn_start.setEnabled(conn and self.armed and not self.acq_running)
        self.btn_stop.setEnabled(conn and (self.acq_running or self.fw_stream or self.fw_cap != "IDLE"))
        self.btn_save_npy.setEnabled(self.capture is not None)
        self.btn_save_csv.setEnabled(self.capture is not None)
        self.btn_save_snap.setEnabled(self.ring_total > 0)

    def show_fw_msg(self, text: str, color: str = GREY):
        self.lbl_fwmsg.setText(f"<b style='color:{color}'>{text}</b>" if text else "")

    def _set_acq_label(self, text: str, color: str = GREY):
        self.lbl_acq.setText(f"<b style='color:{color}'>{text}</b>")

    def _mark_dirty(self, *_):
        self._dirty = True

    # ==================================================================== gain edits
    def _sync_vga_db(self):
        self.sp_vga_db.blockSignals(True)
        self.sp_vga_db.setValue(20 * math.log10(self.gain().vga_gain()))
        self.sp_vga_db.blockSignals(False)

    def _on_vga_db(self, db: float):
        code = vga_code_for_db(db, self.vga_table)
        if code != self.vga.value():
            self.vga.setValue(code)
        else:
            self._sync_vga_db()

    def on_gain_edit(self, *_):
        self._sync_vga_db()
        self.on_ui_edit()

    def on_ui_edit(self, *_):
        self.refresh_labels()
        self.debounce.start()

    # ==================================================================== talking
    def send(self, text: str):
        self.port.send(text)

    def _send_cached(self, key: str, cmd: str):
        if self.sent.get(key) != cmd:
            self.send(cmd)
            self.sent[key] = cmd

    def push_settings(self):
        """Send GAIN / POT / TRIG if they changed (stored by the firmware, applied at ARM)."""
        if not self.connected():
            return
        self._send_cached("GAIN", "GAIN HIGH" if self.rb_high.isChecked() else "GAIN LOW")
        self._send_cached("POT", f"POT {self.vga.value()}")
        if self.fw_cap == "IDLE" and self._cap_waiting is None:
            self._send_cached("TRIG", self.trig_cmd())

    def request_status(self):
        if self.connected() and not self._closing:
            self.send("STATUS")

    def _poll(self):
        if not self.port.is_open():
            return
        if self.identified:
            self.request_status()
            return
        self._ping_tries += 1
        if self._ping_tries == 4:
            self.show_fw_msg("No PONG RXFW reply. Is rx_firmware uploaded (USB Type: Serial), and is this "
                             "the RX board's Teensy?", RED)
        self.send("PING")

    def on_link_state(self, is_open: bool):
        keep_cap, keep_hist = self.capture, self.history
        self.poll.stop()
        self.trig_timer.stop()
        self.repeat_timer.stop()
        self.debounce.stop()
        self._reset_state()
        self.capture, self.history = keep_cap, keep_hist
        self.lbl_banner.setText("")
        self.show_fw_msg("")
        if is_open:
            self.lbl_board.setText("Connected — waiting for the RX firmware to answer PING…")
            self._poll()
            self.poll.start()
        else:
            self.lbl_board.setText("Not connected")
            self._set_acq_label("Idle")
        self.update_counters()
        self.update_buttons()

    def _on_identified(self):
        self.identified = True
        self.show_fw_msg("")
        sn = self.port.serial_number(self.port.open_port_name())
        if sn:
            self.port.preferred_serial = sn
        self.port.set_board_text(f"<b style='color:{GREEN}'>RX board</b> {self.fw_version}")
        self.lbl_board.setText("Connected — checking board…")
        self.send("STREAM OFF")          # in case a previous session left it running
        self.send("ABORT")
        self.push_settings()
        self.request_status()
        self.update_buttons()

    def _confirm_arm_dialog(self) -> bool:
        r = QMessageBox.question(
            self, "Arm the RX board",
            "Is the RX board's 12 V ON and the green LED D6 lit?\n\n"
            "Procedure: 12 V on (limit ≈ 0.2 A) → USB → Connect → Arm.\n"
            "Power down: Stop → Disarm → unplug the RX USB → 12 V off.\n\n"
            "Arming an unpowered board is refused by the firmware (AD5245 check).")
        return r == QMessageBox.StandardButton.Yes

    def on_arm(self):
        if self.confirm_arm():
            self.push_settings()
            self.send("ARM")

    def on_disarm(self):
        self.on_stop()
        self.send("DISARM")

    def on_start(self):
        if not (self.connected() and self.armed):
            return
        self.debounce.stop()
        self.push_settings()
        self.acq_running = True
        self._fft_avg = None
        if self.rb_stream.isChecked():
            self.acq_mode = "stream"
            self._expect_start = "STREAM"
            self.send("STREAM ON")
            self._set_acq_label("Streaming", GREEN)
        else:
            self.acq_mode = "capture"
            self.history.clear()
            self._issue_capture()
        self.update_buttons()

    def _issue_capture(self):
        if not (self.acq_running and self.acq_mode == "capture" and self.connected() and self.armed):
            return
        n, pre = self.capture_samples()
        self._send_cached("TRIG", self.trig_cmd())
        self._expect_start = "CAPTURE"
        self.send(f"CAPTURE {n} {pre}")
        self._set_acq_label("Waiting for trigger…", ORANGE)

    def on_stop(self):
        was = self.acq_running
        self.acq_running = False
        self.trig_timer.stop()
        self.repeat_timer.stop()
        self._expect_start = None
        if self.port.is_open() and self.identified:
            self.send("STREAM OFF")
            self.send("ABORT")
        self._cap_waiting = None
        self._cap_frames.clear()
        if was:
            self._set_acq_label("Stopped")
        self.update_buttons()

    def _on_trigger_timeout(self):
        if self._cap_waiting is not None and not self._cap_triggered:
            self.send("ABORT")
            self._cap_waiting = None
            self.acq_running = False
            self._set_acq_label(f"No trigger within {self.sp_timeout.value():.1f} s — aborted", RED)
            self.update_buttons()

    def _shutdown_before_close(self):
        """STREAM OFF, ABORT, DISARM before the port closes (spec §4.3), waiting for each reply."""
        self.poll.stop()
        self.trig_timer.stop()
        self.repeat_timer.stop()
        self.acq_running = False
        self._closing = True
        if not self.identified:
            pong = self.port.link.request("PING", lambda s: s.startswith("PONG"), 400)
            if not (pong and pong.startswith("PONG RXFW")):
                return
        lk = self.port.link
        lk.request("STREAM OFF", lambda s: s.startswith(("OK STREAM", "ERR")), 800)
        lk.request("ABORT", lambda s: s.startswith(("OK ABORT", "ERR")), 800)
        lk.request("DISARM", lambda s: s.startswith(("OK DISARMED", "ERR")), 1500)

    # ==================================================================== device lines
    def on_line(self, line: str):
        if not self.identified and not line.startswith("PONG"):
            return
        if line.startswith("PONG"):
            self.fw_version = line.split(None, 1)[1] if " " in line else line
            if not self.identified:
                if line.startswith("PONG RXFW"):
                    self._on_identified()
                else:
                    who = "the TX board's Teensy" if line.startswith("PONG TXFW") else "not the RX firmware"
                    msg = f"This is {who} ({line}), wrong port. Port closed; nothing was sent."
                    QTimer.singleShot(0, lambda m=msg: (self.port.disconnect_port(), self.show_fw_msg(m, RED)))
            return
        if line.startswith("STATUS"):
            self._on_status(P.parse_kv(line))
        elif line.startswith("OK ARMED"):
            self.armed = True
            self.mid = int(P.parse_kv(line).get("mid", self.mid))
            self.lbl_banner.setText("")
            self.show_fw_msg("")
            self.request_status()
        elif line.startswith("OK DISARMED"):
            self.armed = False
            self.acq_running = False
            self._cap_waiting = None
            self.request_status()
        elif line.startswith("OK STREAM ON"):
            self._expect_start = None
            self.fw_stream = True
        elif line.startswith("OK STREAM OFF"):
            self.fw_stream = False
            self._next_stream_index = None
        elif line.startswith("OK CAPTURE"):
            self._expect_start = None
            self._cap_waiting = int(P.parse_kv(line).get("id", 0))
            self._cap_triggered = False
            self._cap_frames[self._cap_waiting] = []
            self.fw_cap = "WAIT"
            if self.sp_timeout.value() > 0:
                self.trig_timer.start(int(self.sp_timeout.value() * 1000))
        elif line.startswith("OK ABORT"):
            self.fw_cap = "IDLE"
        elif line.startswith("EVT TRIG"):
            kv = P.parse_kv(line)
            if int(kv.get("id", -1)) == self._cap_waiting:
                self._cap_triggered = True
                self.trig_timer.stop()
                self.fw_cap = "RUN"
                self._set_acq_label("Triggered — receiving…", GREEN)
        elif line.startswith("EVT CAPDONE"):
            self._on_capdone(P.parse_kv(line))
        elif line.startswith("ERR"):
            self.show_fw_msg(f"Teensy: {line}", RED)
            self.sent = {}
            if self._expect_start is not None:
                self._expect_start = None
                self.acq_running = False
                self._cap_waiting = None
                self._set_acq_label("Not started", RED)
            self.request_status()
        elif line.startswith("WARN POWERLOST"):
            self.armed = False
            self.acq_running = False
            self._cap_waiting = None
            self.lbl_banner.setText(f"<div style='background:{RED};color:white;padding:6px'><b>RX board power "
                                    "lost — disarmed. Unplug the RX USB before working on the board.</b></div>")
            self.show_fw_msg(f"Teensy: {line}", RED)
            self._set_acq_label("Stopped (power lost)", RED)
            self.request_status()
        elif line.startswith("WARN"):
            self.show_fw_msg(f"Teensy: {line}", ORANGE)
        self.update_buttons()

    def _on_status(self, st: dict[str, str]):
        self.armed = st.get("armed") == "1"
        self.power = st.get("power") == "1"
        self.fw_stream = st.get("stream") == "1"
        self.fw_cap = st.get("cap", "IDLE")
        if self.armed:
            self.mid = int(st.get("mid", self.mid))
            self.lbl_board.setText(f"<b style='color:{GREEN}'>ARMED</b> (mid = {self.mid}) — ADC sampling at 400 kSPS")
        elif self.power:
            self.lbl_board.setText("Board powered — not armed (press Arm)")
        else:
            self.lbl_board.setText(f"<span style='color:{GREY}'>RX board 12 V not detected — Teensy pins released</span>")
        if not self.armed and self.acq_running:
            self.acq_running = False
            self._set_acq_label("Stopped")
        self.update_buttons()

    # ==================================================================== frames
    def on_link_stats(self, crc: int, resync: int, frames: int):
        self.crc_errors, self.resync_bytes, self.frames = crc, resync, frames
        self.update_counters()

    def update_counters(self):
        self.lbl_counters.setText(f"frames: {self.frames}   CRC errors: {self.crc_errors}   gaps: {self.gaps}   "
                                  f"overflow: {self.overflows}   frame errors: {self.ferr_frames}   "
                                  f"resync bytes: {self.resync_bytes}")
        self.lbl_clip_n.setText(f"clips: {self.clip_frames}")

    def on_frame(self, f: P.Frame):
        if f.has(P.FLAG_CLIP):
            self.clip_frames += 1
            self._clip_until = time.monotonic() + 1.0
        if f.has(P.FLAG_FRAME_ERR):
            self.ferr_frames += 1
        gap = f.has(P.FLAG_OVERFLOW)
        if f.has(P.FLAG_OVERFLOW):
            self.overflows += 1
        if self._last_seq is not None and f.seq != (self._last_seq + 1) & 0xFFFFFFFF:
            gap = True
        self._last_seq = f.seq
        if f.type == P.TYPE_STREAM:
            if self._next_stream_index is not None and f.first_index != self._next_stream_index:
                gap = True
            self._next_stream_index = f.end_index
            if gap:
                self.gaps += 1
                self.gap_marks.append(self.ring_total)
            self._ring_write(f.raw)
        else:
            frames = self._cap_frames.get(f.cap_id)
            if frames is None:
                frames = self._cap_frames.setdefault(f.cap_id, [])
            if gap:
                self.gaps += 1
            frames.append(f)
        self._dirty = True
        self.update_counters()

    def _ring_write(self, raw: np.ndarray):
        n = raw.size
        if n >= RING_SAMPLES:
            raw = raw[-RING_SAMPLES:]
            n = RING_SAMPLES
        p = self.ring_total % RING_SAMPLES
        k = min(n, RING_SAMPLES - p)
        self.ring[p:p + k] = raw[:k]
        if k < n:
            self.ring[:n - k] = raw[k:]
        self.ring_total += n

    def ring_get(self, count: int) -> np.ndarray:
        """Last `count` samples of the PC ring (sample words)."""
        count = min(count, self.ring_total, RING_SAMPLES)
        if count <= 0:
            return np.zeros(0, dtype=np.uint16)
        end = self.ring_total % RING_SAMPLES
        start = end - count
        if start >= 0:
            return self.ring[start:end].copy()
        return np.concatenate((self.ring[start:], self.ring[:end]))

    def _on_capdone(self, kv: dict[str, str]):
        cid = int(kv.get("id", 0))
        n, pre, trig = int(kv.get("n", 0)), int(kv.get("pre", 0)), int(kv.get("trig", 0))
        frames = sorted(self._cap_frames.pop(cid, []), key=lambda fr: fr.first_index)
        self.fw_cap = "IDLE"
        if cid != self._cap_waiting:
            return
        self._cap_waiting = None
        start = trig - pre
        words = np.full(n, self.mid & P.CODE_MASK, dtype=np.uint16)
        valid = np.zeros(n, dtype=bool)
        for fr in frames:
            a = fr.first_index - start
            b = a + fr.n
            lo, hi = max(a, 0), min(b, n)
            if hi > lo:
                words[lo:hi] = fr.raw[lo - a:hi - a]
                valid[lo:hi] = True
        incomplete = not bool(valid.all())
        gains = sorted({fr.gain for fr in frames})
        pots = sorted({fr.pot for fr in frames})
        src = self.sent.get("TRIG", "TRIG NONE").split()[1] if self.sent.get("TRIG") else "NONE"
        cap = Capture(cap_id=cid, n=n, pre=pre, trig_index=trig, words=words, valid=valid,
                      incomplete=incomplete, settings_changed=len(gains) > 1 or len(pots) > 1,
                      gains=gains, pots=pots, clip=any(fr.has(P.FLAG_CLIP) for fr in frames),
                      overflow=any(fr.has(P.FLAG_OVERFLOW) for fr in frames), trigger_source=src,
                      level_counts=self.level_counts(), mid=self.mid, sync_ok=src == "SYNC")
        self.capture = cap
        self.captures_done += 1
        self._cap_spec = None
        # default measurement window: trigger -> end
        t0, t1 = 0.0, (n - pre) / FS_HZ * 1e3
        self.region.blockSignals(True)
        self.region.setRegion((t0, t1))
        self.region.blockSignals(False)
        self._measure_capture()
        if self.measurements:
            self.history.append(dict(self.measurements))
        self._update_repeat_stats()
        msg = f"Capture {cid}: {n} samples"
        if incomplete:
            msg += f" — <span style='color:{RED}'>INCOMPLETE ({int((~valid).sum())} samples missing)</span>"
        if cap.settings_changed:
            msg += f" — <span style='color:{ORANGE}'>settings changed during capture</span>"
        self._set_acq_label(msg, GREEN if not incomplete else RED)
        if self.acq_running and self.rb_auto.isChecked():
            self.repeat_timer.start()
        elif self.acq_running:
            self.acq_running = False
        self._dirty = True
        self.update_buttons()

    # ==================================================================== measurements
    def _template(self):
        tx = self.tx_state() or {}
        if tx.get("connected") and tx.get("freq"):
            on_us = tx.get("on_us") if tx.get("mode") == "BURST" else None
            if on_us is None:
                on_us = 1000.0
            return A.burst_template(float(tx["freq"]), float(on_us))
        return None

    def _measure_capture(self):
        cap = self.capture
        if cap is None:
            self.measurements = {}
            return
        r0, r1 = self.region.getRegion()
        a = int(round(cap.pre + r0 * 1e-3 * FS_HZ))
        b = int(round(cap.pre + r1 * 1e-3 * FS_HZ))
        self.measurements = A.measure(cap.codes, cap.mid, self.gain(), trig=cap.pre, win=(a, b),
                                      rvs_db=self.sp_rvs.value() if self.cb_spl.isChecked() else None,
                                      template=self._template(), sync_ok=cap.sync_ok,
                                      system_delay_us=self.sp_sysdelay.value(), sound_speed=self.sp_c.value(),
                                      f_hint=None if self.cb_fa_auto.isChecked() else self.f_analysis())
        if not cap.sync_ok:
            self.measurements["delay"] = "no sync"
        self._auto_freq(self.measurements.get("freq_hz"), cap.codes[a:b])
        self._fill_table()

    def _auto_freq(self, f: float | None, codes):
        if not self.cb_fa_auto.isChecked() or not f or not (5e3 <= f <= 195e3):
            return
        if codes is None or np.asarray(codes).size < 16 or A.adc_vpp(codes) < 10 * LSB_V:
            return
        if abs(self.sp_fa.value() - f / 1e3) > 0.05:
            self.sp_fa.blockSignals(True)
            self.sp_fa.setValue(round(f / 100) / 10)
            self.sp_fa.blockSignals(False)
            self.refresh_labels()

    def _on_region(self, *_):
        if self.capture is not None and self.acq_mode_view() == "capture":
            self._measure_capture()
            self._dirty = True

    def _fill_table(self):
        rows = []
        for key, label, fmt in A.MEAS_LABELS:
            if key in self.measurements and self.measurements[key] is not None:
                v = self.measurements[key]
                try:
                    rows.append((label, fmt(v)))
                except (TypeError, ValueError):
                    rows.append((label, str(v)))
        if self.measurements.get("delay") == "no sync":
            rows.append(("Delay", "no sync"))
        self.tbl.setRowCount(len(rows))
        for i, (k, v) in enumerate(rows):
            self.tbl.setItem(i, 0, QTableWidgetItem(k))
            self.tbl.setItem(i, 1, QTableWidgetItem(v))

    def _update_repeat_stats(self):
        if not self.rb_auto.isChecked() or len(self.history) < 2:
            self.lbl_repeat.setText("")
            return
        parts = [f"last {len(self.history)} captures:"]
        for key, name, scale, unit in (("delay_xc_us", "delay (x-corr)", 1, "µs"), ("delay_thr_us", "delay (thr)", 1, "µs"),
                                       ("adc_vpp_v", "ADC Vpp", 1e3, "mV")):
            vals = [h[key] for h in self.history if isinstance(h.get(key), (int, float))]
            if len(vals) >= 2:
                parts.append(f"{name} {np.mean(vals) * scale:.2f} ± {np.std(vals, ddof=1) * scale:.2f} {unit}")
        self.lbl_repeat.setText("   ".join(parts))

    def copy_measurements(self):
        lines = [f"{self.tbl.item(i, 0).text()}\t{self.tbl.item(i, 1).text()}" for i in range(self.tbl.rowCount())]
        QGuiApplication.clipboard().setText("\n".join(lines))

    # ==================================================================== plots
    def acq_mode_view(self) -> str:
        return "stream" if self.rb_stream.isChecked() else "capture"

    def _on_mode_changed(self, *_):
        if self.acq_running:
            self.on_stop()
        self.refresh_labels()

    def redraw(self):
        now = time.monotonic()
        clip = now < self._clip_until
        self.lbl_clip.setStyleSheet(f"background:{RED};color:white;font-weight:bold;padding:2px" if clip
                                    else f"background:#555;color:#aaa;padding:2px")
        if not self._dirty or not self.isVisible():
            return
        self._dirty = False
        if self.acq_mode_view() == "stream":
            self._draw_stream()
        else:
            self._draw_capture()

    def _y_scale(self) -> tuple[float, str]:
        if self.cb_yunit.currentData() == "input":
            return 1e3 / self.gain().total_mag(self.f_analysis()), "mV at hydrophone"
        return 1.0, "V at ADC"

    def _draw_clip_lines(self, scale: float):
        lo = (CLIP_LO - self.mid) * LSB_V * scale
        hi = (CLIP_HI - self.mid) * LSB_V * scale
        self.l_clip[0].setValue(lo)
        self.l_clip[1].setValue(hi)

    def _set_gap_lines(self, xs: list[float]):
        for ln in self.gap_lines:
            self.p_time.removeItem(ln)
        self.gap_lines = []
        for x in xs[-50:]:
            ln = pg.InfiniteLine(pos=x, angle=90, pen=pg.mkPen(RED, width=1))
            self.p_time.addItem(ln)
            self.gap_lines.append(ln)

    def _draw_time(self, codes: np.ndarray, t0_ms: float):
        scale, unit = self._y_scale()
        y = (codes.astype(float) - self.mid) * LSB_V * scale
        idx, yy = A.minmax_decimate(y, 4000)
        self.c_time.setData(t0_ms + idx / FS_HZ * 1e3, yy)
        if yy.size:
            self._smart_yrange(self.p_time, float(yy.min()), float(yy.max()))
        if self._time_unit != unit:
            self._time_unit = unit
            self.p_time.setLabel("left", unit)
        self._draw_clip_lines(scale)

    def _draw_fft(self, codes: np.ndarray, smooth: bool):
        x = (codes.astype(float) - self.mid) * LSB_V
        f, mag = A.spectrum(x, FS_HZ, FFT_N)
        p = mag ** 2
        navg = self.cb_avg.currentData() or 1
        if smooth and navg > 1 and self._fft_avg is not None and self._fft_avg.shape == p.shape:
            p = self._fft_avg + (p - self._fft_avg) / navg
        self._fft_avg = p
        db = 10 * np.log10(np.maximum(p, 1e-24))
        if self.cb_fft_unit.currentData() == "input":
            g = self.gain().total_mag(np.maximum(f, 1.0))
            db = db - 20 * np.log10(np.maximum(g, 1e-12))
            unit = "dBV re input"
        else:
            unit = "dBV at ADC"
        if self._fft_unit != unit:
            self._fft_unit = unit
            self.p_fft.setLabel("left", unit)
        fk = f / 1e3
        self.c_fft.setData(fk, db)
        self._smart_yrange(self.p_fft, float(np.percentile(db[1:], 5)) - 10, float(db[1:].max()) + 5)
        lo = int(np.searchsorted(f, 1e3))
        k = lo + int(np.argmax(db[lo:]))
        self.pk_marker.setData([fk[k]], [db[k]])
        self.pk_text.setText(f"{fk[k]:.2f} kHz\n{db[k]:.1f} dB")
        self.pk_text.setPos(fk[k], db[k])
        self._gain_stage_check(x, f[k] if f[k] > 0 else self.f_analysis())
        if self.acq_mode_view() == "stream":
            fpk, amp = A.peak_frequency(x, FS_HZ, FFT_N, fmin=5e3, fmax=195e3)
            self._auto_freq(fpk, codes)

    def _gain_stage_check(self, x_ac: np.ndarray, f_dom: float):
        """§3.7: estimate the U4 output swing from the ADC signal at the full-band dominant frequency."""
        if x_ac.size < 16:
            return
        vpp = float(x_ac.max() - x_ac.min())
        est = gain_stage_clip_estimate(vpp, max(f_dom, 1e3), self.gain())
        warn = est > GAIN_STAGE_WARN_VPP and vpp > 20 * LSB_V
        self._gs_warn = warn
        self.lbl_gs.setText(f"<b style='color:{ORANGE}'>Gain stage may be clipping (≈ {est:.1f} Vpp estimated at "
                            f"GAIN_OUT, {f_dom / 1e3:.1f} kHz) — switch to LOW coarse gain</b>" if warn else "")

    @staticmethod
    def _smart_yrange(plot, lo: float, hi: float):
        """Rescale y only when the data leaves the view or uses < 30 % of it (keeps the axes
        from being regenerated every frame)."""
        if not (np.isfinite(lo) and np.isfinite(hi)):
            return
        if hi - lo < 1e-9:
            lo, hi = lo - 1e-3, hi + 1e-3
        a, b = plot.viewRange()[1]
        if lo < a or hi > b or (hi - lo) < 0.3 * (b - a):
            pad = 0.15 * (hi - lo)
            plot.setYRange(lo - pad, hi + pad, padding=0)

    def _draw_spec(self, db: np.ndarray, t0: float):
        """db: spectrogram columns already in dB, shape (n_columns, n_freq)."""
        if db.shape[0] == 0:
            self.img.clear()
            return
        cols = db
        self.img.setImage(db, autoLevels=False, levels=(self.sp_spec_lo.value(), self.sp_spec_hi.value()))
        dt = SPEC_HOP / FS_HZ
        self.img.setRect(pg.QtCore.QRectF(t0, 0, cols.shape[0] * dt, FS_HZ / 2e3))

    def _draw_stream(self):
        self.region.hide()
        if self.ring_total == 0:
            return
        span = int((self.cb_span.currentData() or 10) * 1e-3 * FS_HZ)
        words = self.ring_get(max(span, FFT_N))
        codes = (words & P.CODE_MASK).astype(np.uint16)
        tcodes = codes[-span:]
        self._draw_time(tcodes, -tcodes.size / FS_HZ * 1e3)
        first_total = self.ring_total - tcodes.size
        self._set_gap_lines([(g - self.ring_total) / FS_HZ * 1e3 for g in self.gap_marks if g >= first_total])
        self._draw_fft(codes[-FFT_N:], smooth=True)
        self._update_stream_spec()
        self._spec_frame += 1
        if self._spec_frame % 3 == 0:                   # spectrogram image at ~8 fps is plenty
            cols = self._spec_cols
            self._draw_spec(cols, -cols.shape[0] * SPEC_HOP / FS_HZ)
        if self.measurements_due():
            self.measurements = A.measure(codes[-FFT_N:], self.mid, self.gain(),
                                          rvs_db=self.sp_rvs.value() if self.cb_spl.isChecked() else None,
                                          f_hint=None if self.cb_fa_auto.isChecked() else self.f_analysis())
            self._fill_table()

    _last_meas = 0.0

    def measurements_due(self) -> bool:
        now = time.monotonic()
        if now - self._last_meas > 0.25:
            self._last_meas = now
            return True
        return False

    def _update_stream_spec(self):
        avail = self.ring_total - self._spec_next
        if avail > RING_SAMPLES - SPEC_NFFT:            # fell behind: restart from the latest second
            self._spec_next = max(0, self.ring_total - FS_HZ)
            avail = self.ring_total - self._spec_next
        if avail < SPEC_NFFT:
            return
        words = self.ring_get(avail)
        x = ((words & P.CODE_MASK).astype(float) - self.mid) * LSB_V
        cols = A.spectrogram(x, SPEC_NFFT, SPEC_HOP)
        self._spec_next += cols.shape[0] * SPEC_HOP
        db = (20 * np.log10(np.maximum(cols, 1e-12))).astype(np.float32)
        self._spec_cols = np.concatenate((self._spec_cols, db))[-SPEC_COLS:]

    def _draw_capture(self):
        cap = self.capture
        if cap is None:
            self.region.hide()
            return
        self.region.show()
        codes = cap.codes
        self._draw_time(codes, -cap.pre / FS_HZ * 1e3)
        miss = np.flatnonzero(np.diff(cap.valid.astype(np.int8)) != 0)
        self._set_gap_lines([(i + 1 - cap.pre) / FS_HZ * 1e3 for i in miss])
        r0, r1 = self.region.getRegion()
        a = max(0, int(round(cap.pre + r0 * 1e-3 * FS_HZ)))
        b = min(cap.n, int(round(cap.pre + r1 * 1e-3 * FS_HZ)))
        seg = codes[a:b] if b - a >= 16 else codes
        self._draw_fft(seg[:FFT_N], smooth=False)
        if getattr(self, "_cap_spec", None) is None:
            x = (codes.astype(float) - cap.mid) * LSB_V
            cols = A.spectrogram(x[: 20 * FS_HZ], SPEC_NFFT, SPEC_HOP)
            self._cap_spec = (20 * np.log10(np.maximum(cols, 1e-12))).astype(np.float32)
        self._draw_spec(self._cap_spec, -cap.pre / FS_HZ)

    # ==================================================================== calibration
    def calibration(self) -> Calibration:
        return Calibration(self.sp_lna.value(), self.sp_cal.value(), self.vga_table, self.sp_sysdelay.value(),
                           self.sp_c.value())

    def apply_calibration(self, cal: Calibration):
        self.vga_table = cal.vga_table
        self.sp_lna.setValue(cal.lna_gain)
        self.sp_cal.setValue(cal.cal_factor)
        self.sp_sysdelay.setValue(cal.system_delay_us)
        self.sp_c.setValue(cal.sound_speed)
        self._sync_vga_db()
        self.refresh_labels()

    def load_calibration(self, path: str | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Load RX calibration", self.calibration_path,
                                                  "Calibration (*.json)")
            if not path:
                return False
        try:
            with open(path, encoding="utf-8") as f:
                cal = Calibration.from_json(f.read())
        except (OSError, ValueError, KeyError) as e:
            QMessageBox.warning(self, "Calibration", f"Could not load {path}:\n{e}")
            return False
        self.calibration_path = path
        self.apply_calibration(cal)
        return True

    def save_calibration(self, path: str | None = None) -> bool:
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Save RX calibration", self.calibration_path or "rx_calibration.json",
                                                  "Calibration (*.json)")
            if not path:
                return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.calibration().to_json())
        self.calibration_path = path
        return True

    # ==================================================================== saving
    def _ask_save_path(self, default: str, flt: str) -> str:
        path, _ = QFileDialog.getSaveFileName(self, "Save RX data", default, flt)
        return path

    def metadata(self, cap: Capture | None) -> dict:
        g = self.gain()
        fa = self.f_analysis()
        m = rx_io.base_metadata()
        m.update({
            "firmware": self.fw_version, "mid_code": self.mid,
            "coarse_gain": g.coarse, "pot_code": g.pot, "frontend": g.frontend, "lna_gain": g.g_lna,
            "cal_factor": g.cal, "vga_gain": g.vga_gain(), "vga_table": self.vga_table.to_dict() if self.vga_table else None,
            "total_gain_db_at_f": g.total_db(fa), "analysis_freq_hz": fa,
            "crc_errors": self.crc_errors, "notes": self.ed_notes.text(),
            "tx_state": self.tx_state(),
        })
        if cap is not None:
            m.update({"n": cap.n, "pre": cap.pre, "trigger_source": cap.trigger_source.lower(),
                      "trigger_level_counts": cap.level_counts if cap.trigger_source == "LEVEL" else None,
                      "trigger_index": cap.trig_index, "first_index": cap.first_index, "cap_id": cap.cap_id,
                      "settings_changed_during_capture": cap.settings_changed, "incomplete": cap.incomplete,
                      "frame_gains": ["LOW" if x else "HIGH" for x in cap.gains], "frame_pots": cap.pots,
                      "clip_count": int(np.count_nonzero(((cap.codes <= CLIP_LO) | (cap.codes >= CLIP_HI)))),
                      "overflow": cap.overflow, "mid_code": cap.mid, "measurements": dict(self.measurements)})
        return m

    def save_capture(self, kind: str = "npy", path: str | None = None) -> str | None:
        cap = self.capture
        if cap is None:
            return None
        if path is None:
            name = rx_io.default_name(self.measurements.get("freq_hz") or self.f_analysis())
            path = self.ask_save_path(name + (".npy" if kind == "npy" else ".csv"),
                                      "NumPy (*.npy)" if kind == "npy" else "CSV (*.csv)")
            if not path:
                return None
        meta = self.metadata(cap)
        if kind == "npy":
            out = rx_io.save_npy(path, cap.words, meta)[0]
        else:
            out = rx_io.save_csv(path, cap.words, meta, self.gain().total_mag(self.f_analysis()))
        self.lbl_saved.setText(f"Saved {out}")
        return out

    def save_snapshot(self, path: str | None = None, seconds: float | None = None) -> str | None:
        if self.ring_total == 0:
            return None
        n = int((seconds or self.sp_snap.value()) * FS_HZ)
        words = self.ring_get(n)
        if path is None:
            path = self.ask_save_path(rx_io.default_name(self.f_analysis()) + "_stream.npy", "NumPy (*.npy)")
            if not path:
                return None
        meta = self.metadata(None)
        meta.update({"n": int(words.size), "pre": 0, "trigger_source": "none", "trigger_index": None,
                     "first_index": None, "cap_id": 0, "settings_changed_during_capture": None, "overflow": self.overflows > 0,
                     "gaps": self.gaps, "measurements": dict(self.measurements)})
        out = rx_io.save_npy(path, words, meta)[0]
        self.lbl_saved.setText(f"Saved {out}")
        return out

    # ==================================================================== settings
    def save_settings(self, s: QSettings):
        s.beginGroup("rx")
        s.setValue("port", self.port.port_name() or "")
        s.setValue("serial", self.port.preferred_serial)
        s.setValue("frontend", self.cb_frontend.currentData())
        s.setValue("lna", self.sp_lna.value())
        s.setValue("coarse_high", self.rb_high.isChecked())
        s.setValue("pot", self.vga.value())
        s.setValue("fa_khz", self.sp_fa.value())
        s.setValue("fa_auto", self.cb_fa_auto.isChecked())
        s.setValue("cal", self.sp_cal.value())
        s.setValue("vga_table", json.dumps(self.vga_table.to_dict()) if self.vga_table else "")
        s.setValue("capture_mode", self.rb_capture.isChecked())
        s.setValue("trig", self.cb_trig.currentData())
        s.setValue("syncwire", self.cb_syncwire.isChecked())
        s.setValue("level_mv", self.sp_level.value())
        s.setValue("pre_ms", self.sp_pre.value())
        s.setValue("len_ms", self.sp_len.value())
        s.setValue("auto", self.rb_auto.isChecked())
        s.setValue("timeout", self.sp_timeout.value())
        s.setValue("c", self.sp_c.value())
        s.setValue("sysdelay", self.sp_sysdelay.value())
        s.setValue("rvs", self.sp_rvs.value())
        s.setValue("spl", self.cb_spl.isChecked())
        s.setValue("span", self.cb_span.currentIndex())
        s.setValue("spec_lo", self.sp_spec_lo.value())
        s.setValue("spec_hi", self.sp_spec_hi.value())
        s.setValue("cal_path", self.calibration_path)
        s.endGroup()

    def load_settings(self, s: QSettings):
        s.beginGroup("rx")
        try:
            def get(key, default, typ):
                v = s.value(key, default)
                try:
                    return typ(v) if typ is not bool else (str(v).lower() in ("1", "true"))
                except (TypeError, ValueError):
                    return default
            self.port.preferred_serial = get("serial", "", str)
            i = self.cb_frontend.findData(get("frontend", "LNA", str))
            self.cb_frontend.setCurrentIndex(max(i, 0))
            self.sp_lna.setValue(get("lna", G_LNA_NOM, float))
            (self.rb_high if get("coarse_high", False, bool) else self.rb_low).setChecked(True)
            tbl = get("vga_table", "", str)
            self.vga_table = VgaTable.from_dict(json.loads(tbl)) if tbl else None
            self.vga.setValue(get("pot", 0, int))
            self.sp_fa.setValue(get("fa_khz", 40.0, float))
            self.cb_fa_auto.setChecked(get("fa_auto", False, bool))
            self.sp_cal.setValue(get("cal", 1.0, float))
            (self.rb_capture if get("capture_mode", False, bool) else self.rb_stream).setChecked(True)
            self.cb_syncwire.setChecked(get("syncwire", False, bool))
            i = self.cb_trig.findData(get("trig", "NONE", str))
            self.cb_trig.setCurrentIndex(max(i, 0))
            self.sp_level.setValue(get("level_mv", 244.1, float))
            self.sp_pre.setValue(get("pre_ms", 0.5, float))
            self.sp_len.setValue(get("len_ms", 8.0, float))
            (self.rb_auto if get("auto", False, bool) else self.rb_single).setChecked(True)
            self.sp_timeout.setValue(get("timeout", 0.0, float))
            self.sp_c.setValue(get("c", 1480.0, float))
            self.sp_sysdelay.setValue(get("sysdelay", 0.0, float))
            self.sp_rvs.setValue(get("rvs", -206.0, float))
            self.cb_spl.setChecked(get("spl", False, bool))
            self.cb_span.setCurrentIndex(get("span", 3, int))
            self.sp_spec_lo.setValue(get("spec_lo", -110, int))
            self.sp_spec_hi.setValue(get("spec_hi", -10, int))
            self.calibration_path = get("cal_path", "", str)
            self.port.refresh_ports()
            port = s.value("port", "")
            if port and not self.port.serial_number():
                self.port.select_port(str(port))
        finally:
            s.endGroup()
        self.debounce.stop()
        self._sync_vga_db()
        self.refresh_labels()


def _fmt_v(v: float) -> str:
    if not math.isfinite(v):
        return "—"
    if abs(v) >= 1:
        return f"{v:.2f} V"
    if abs(v) >= 1e-3:
        return f"{v * 1e3:.2f} mV"
    return f"{v * 1e6:.1f} µV"
