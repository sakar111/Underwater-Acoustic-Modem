"""Reusable widgets shared by the TX and RX tabs."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
                               QSlider, QSpinBox, QWidget)

from .links import SIM_PORT_NAME, LineLink, SerialLink, SimTxLink, list_ports


class SpinSlider(QWidget):
    """Spinbox + slider kept in sync. The slider range may be narrower than the spinbox."""
    valueChanged = Signal(int)

    def __init__(self, lo, hi, step, value, suffix="", slider_lo=None, slider_hi=None, parent=None):
        super().__init__(parent)
        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(step)
        self.spin.setSuffix(suffix)
        self.spin.setValue(value)
        self.spin.setMinimumWidth(120)
        self.spin.setKeyboardTracking(False)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo if slider_lo is None else slider_lo,
                             hi if slider_hi is None else slider_hi)
        self.slider.setSingleStep(step)
        self.slider.setPageStep(step * 10)
        self.slider.setValue(value)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def _from_slider(self, v):
        if self.spin.value() != v:
            self.spin.setValue(v)

    def _from_spin(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(max(self.slider.minimum(), min(self.slider.maximum(), v)))
        self.slider.blockSignals(False)
        self.valueChanged.emit(v)

    def value(self) -> int:
        return self.spin.value()

    def setValue(self, v: int):
        self.spin.setValue(int(v))


class PortBar(QWidget):
    """Port selector + Refresh + Connect for one board. Each tab has its own, because
    the TX and RX boards each have their own Teensy and COM port.

    The bar owns a real link (`serial_link`, default a line-based SerialLink) and optionally a
    simulator link; `link` always points at the one in use. Tabs connect to the bar's re-emitted
    signals. Frame-aware links (RX) also have their frame/stats signals re-emitted.

    `peer` is the other tab's bar: a port it has open is greyed out here. `preferred_serial`
    is the USB serial number last identified as this board (pre-selected on refresh).
    """
    line_received = Signal(str)
    line_sent = Signal(str)
    state_changed = Signal(bool)
    error = Signal(str)
    frame_received = Signal(object)
    stats_changed = Signal(int, int, int)

    def __init__(self, title: str, sim_link: LineLink | None = None, serial_link: LineLink | None = None,
                 sim_name: str = SIM_PORT_NAME, parent=None):
        super().__init__(parent)
        self.title = title
        self.serial = serial_link if serial_link is not None else SerialLink(self)
        if self.serial.parent() is None:
            self.serial.setParent(self)
        self.sim = sim_link
        self.sim_name = sim_name
        self.link: LineLink = self.serial
        self.before_close = None          # callable run before the port closes (e.g. DISARM)
        self.peer: PortBar | None = None
        self.preferred_serial = ""
        self._serials: dict[str, str] = {}
        for lk in filter(None, (self.serial, self.sim)):
            lk.line_received.connect(lambda s, lk=lk: lk is self.link and self.line_received.emit(s))
            lk.line_sent.connect(lambda s, lk=lk: lk is self.link and self.line_sent.emit(s))
            lk.state_changed.connect(lambda b, lk=lk: lk is self.link and self._on_state(b))
            lk.error.connect(lambda s, lk=lk: lk is self.link and self.error.emit(s))
            if hasattr(lk, "frame_received"):
                lk.frame_received.connect(lambda f, lk=lk: lk is self.link and self.frame_received.emit(f))
                lk.stats_changed.connect(lambda a, b, c, lk=lk: lk is self.link and self.stats_changed.emit(a, b, c))

        self.cb_port = QComboBox()
        self.cb_port.setMinimumWidth(320)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_connect = QPushButton("Connect")
        self.lbl_board = QLabel("")
        self.lbl_board.setTextFormat(Qt.TextFormat.RichText)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(f"{title} Teensy port"))
        lay.addWidget(self.cb_port, 1)
        lay.addWidget(self.btn_refresh)
        lay.addWidget(self.btn_connect)
        lay.addWidget(self.lbl_board)
        self.btn_refresh.clicked.connect(lambda: self.refresh_ports())
        self.btn_connect.clicked.connect(self.toggle_connect)
        self.refresh_ports()

    # ---- ports ----------------------------------------------------------
    def set_peer(self, other: "PortBar"):
        self.peer = other
        other.state_changed.connect(lambda _b: self.update_disabled())

    def open_port_name(self) -> str | None:
        return self._open_name if self.link.is_open() else None

    _open_name: str | None = None

    def refresh_ports(self, prefer: str | None = None):
        current = prefer or self.cb_port.currentData()
        self.cb_port.clear()
        self._serials.clear()
        best = -1
        pref_idx = -1
        for name, label, is_teensy, sn in list_ports():
            self.cb_port.addItem(label, name)
            self._serials[name] = sn
            if is_teensy and best < 0:
                best = self.cb_port.count() - 1
            if sn and sn == self.preferred_serial:
                pref_idx = self.cb_port.count() - 1
        if self.sim is not None:
            self.cb_port.addItem(f"{self.sim_name}  —  simulated {self.title} Teensy (no hardware)", self.sim_name)
        idx = self.cb_port.findData(current) if current else -1
        if idx < 0:
            idx = pref_idx
        if idx >= 0:
            self.cb_port.setCurrentIndex(idx)
        elif best >= 0:
            self.cb_port.setCurrentIndex(best)
        self.update_disabled()

    def update_disabled(self):
        """Grey out the port the other tab has open (it cannot be opened twice)."""
        taken = self.peer.open_port_name() if self.peer is not None else None
        model = self.cb_port.model()
        if not isinstance(model, QStandardItemModel):
            return
        for i in range(self.cb_port.count()):
            item = model.item(i)
            if item is not None:
                item.setEnabled(self.cb_port.itemData(i) != taken)
        if taken and self.cb_port.currentData() == taken and not self.link.is_open():
            for i in range(self.cb_port.count()):
                if self.cb_port.itemData(i) != taken:
                    self.cb_port.setCurrentIndex(i)
                    break

    def port_name(self) -> str | None:
        return self.cb_port.currentData()

    def serial_number(self, name: str | None = None) -> str:
        return self._serials.get(name or self.port_name() or "", "")

    def select_port(self, name: str) -> bool:
        idx = self.cb_port.findData(name)
        if idx >= 0:
            self.cb_port.setCurrentIndex(idx)
        return idx >= 0

    def set_board_text(self, html: str):
        self.lbl_board.setText(html)

    # ---- connect --------------------------------------------------------
    def is_open(self) -> bool:
        return self.link.is_open()

    def send(self, text: str):
        self.link.send(text)

    def connect_port(self, name: str | None = None) -> bool:
        name = name or self.port_name()
        if not name:
            QMessageBox.warning(self, "No port", "No serial port selected.")
            return False
        if self.peer is not None and self.peer.open_port_name() == name:
            QMessageBox.warning(self, "Port in use", f"{name} is already open in the {self.peer.title} tab.")
            return False
        self.link = self.sim if (name == self.sim_name and self.sim is not None) else self.serial
        self._open_name = name
        if not self.link.open(name):
            QMessageBox.warning(self, "Could not open port",
                                f"{name}: {self.link.error_string()}\n\n"
                                "Only one program can use a COM port: close the Arduino Serial "
                                "Monitor (or any other terminal) if it is open.")
            return False
        return True

    def disconnect_port(self):
        if self.link.is_open():
            if self.before_close:
                self.before_close()
            self.link.close()

    def toggle_connect(self):
        if self.link.is_open():
            self.disconnect_port()
        else:
            self.connect_port()

    def _on_state(self, is_open: bool):
        self.btn_connect.setText("Disconnect" if is_open else "Connect")
        self.cb_port.setEnabled(not is_open)
        self.btn_refresh.setEnabled(not is_open)
        if not is_open:
            self.lbl_board.setText("")
        self.state_changed.emit(is_open)


def make_sim_tx_link(parent=None) -> SimTxLink:
    return SimTxLink(parent=parent)
