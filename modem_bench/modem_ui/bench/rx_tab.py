"""RX tab placeholder.

The RX board has its own Teensy and COM port, so this tab gets its own PortBar
(independent connection). The RX firmware protocol, power-check rule and binary
sample-block format come from the separate RX specification (spec §7). Until then
this tab only connects and logs whatever the device prints; it sends nothing.

Planned contents: Arm/Disarm, gain-state toggle (TS5A3166), AGC pot slider with gain
readout, capture controls (block length, free-run / level trigger), live time-domain
plot + FFT (pyqtgraph, decimated, ~20-30 fps), CSV/NumPy save.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget

from .widgets import PortBar


class RxTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        self.port = PortBar("RX")
        lay.addWidget(self.port)
        g = QGroupBox("RX board")
        gl = QVBoxLayout(g)
        lbl = QLabel("RX controls (gain switch, AGC pot), capture and live plotting will go here "
                     "once the RX specification is available.\n\nConnecting here only opens the "
                     "port and logs what the RX Teensy prints — no commands are sent.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        gl.addWidget(lbl)
        lay.addWidget(g, 1)

    def save_settings(self, s):
        s.setValue("rx/port", self.port.port_name() or "")

    def load_settings(self, s):
        port = s.value("rx/port", "")
        if port:
            self.port.select_port(str(port))
