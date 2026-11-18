"""Line-oriented device links.

Every board (TX now, RX later) gets its own link object. Widgets only use this
interface, so the same tab works against a real Teensy (SerialLink) or the
in-process simulator (SimTxLink):

    signals:  line_received(str), line_sent(str), state_changed(bool), error(str)
    methods:  open(name) -> bool, close(), is_open(), send(text),
              request(text, predicate, timeout_ms) -> str | None   (blocking, for shutdown)
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QElapsedTimer, QIODevice, QObject, QTimer, Signal
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo

from .tx_model import TEENSY_VID

SIM_PORT_NAME = "SIM-TX"


class LineLink(QObject):
    line_received = Signal(str)
    line_sent = Signal(str)
    state_changed = Signal(bool)
    error = Signal(str)

    def is_open(self) -> bool:            # pragma: no cover - interface
        raise NotImplementedError

    def open(self, name: str) -> bool:    # pragma: no cover - interface
        raise NotImplementedError

    def close(self):                      # pragma: no cover - interface
        raise NotImplementedError

    def send(self, text: str):            # pragma: no cover - interface
        raise NotImplementedError

    def error_string(self) -> str:
        return ""

    def request(self, text: str, pred: Callable[[str], bool], timeout_ms: int = 1000) -> str | None:
        """Send `text` and block (processing replies) until a line matches `pred`."""
        raise NotImplementedError         # pragma: no cover - interface


def list_ports(vid: int = TEENSY_VID) -> list[tuple[str, str, bool, str]]:
    """(port name, label, is_teensy, serial number) for every serial port.

    Both Teensys report VID 0x16C0, so the label shows the USB serial number to tell the TX and
    RX boards apart (spec RX §6.1)."""
    out = []
    for p in QSerialPortInfo.availablePorts():
        is_teensy = p.hasVendorIdentifier() and p.vendorIdentifier() == vid
        sn = p.serialNumber() or ""
        label = f"{p.portName()}  —  {p.description() or 'serial port'}"
        if is_teensy:
            label += "  (Teensy" + (f" SN {sn}" if sn else "") + ")"
        out.append((p.portName(), label, is_teensy, sn))
    return out


class SerialLink(LineLink):
    """Teensy USB serial via QSerialPort (event driven, no polling threads)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.port = QSerialPort(self)
        self.port.readyRead.connect(self._on_ready_read)
        self.port.errorOccurred.connect(self._on_error)
        self._buf = b""

    def is_open(self) -> bool:
        return self.port.isOpen()

    def error_string(self) -> str:
        return self.port.errorString()

    def open(self, name: str) -> bool:
        self.port.setPortName(name)
        self.port.setBaudRate(115200)        # ignored by Teensy USB serial, harmless
        if not self.port.open(QIODevice.OpenModeFlag.ReadWrite):
            return False
        self.port.setDataTerminalReady(True)
        self._buf = b""
        self.state_changed.emit(True)
        return True

    def close(self):
        was_open = self.port.isOpen()
        if was_open:
            self.port.close()
        self._buf = b""
        if was_open:
            self.state_changed.emit(False)

    def send(self, text: str):
        if not self.port.isOpen():
            return
        self.port.write((text + "\n").encode())
        self.line_sent.emit(text)

    def request(self, text, pred, timeout_ms=1000):
        if not self.port.isOpen():
            return None
        got: list[str] = []

        def cb(line):
            if not got and pred(line):
                got.append(line)

        self.line_received.connect(cb)
        try:
            self.send(text)
            self.port.waitForBytesWritten(timeout_ms)
            t = QElapsedTimer()
            t.start()
            while not got and self.port.isOpen() and t.elapsed() < timeout_ms:
                self.port.waitForReadyRead(50)   # emits readyRead -> _on_ready_read
        finally:
            self.line_received.disconnect(cb)
        return got[0] if got else None

    def _on_ready_read(self):
        self._buf += bytes(self.port.readAll())
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode(errors="replace").strip()
            if line:
                self.line_received.emit(line)

    def _on_error(self, err):
        E = QSerialPort.SerialPortError
        if err in (E.ResourceError, E.PermissionError, E.DeviceNotFoundError) and self.port.isOpen():
            # e.g. USB unplugged: report and close (deferred: we're inside a QSerialPort callback)
            self.error.emit(f"Serial port lost: {self.port.errorString()}")
            QTimer.singleShot(0, self.close)


class SimTxLink(LineLink):
    """In-process simulated TX Teensy (bench.sim_tx) behind the LineLink interface."""

    def __init__(self, sim=None, parent=None, realtime: bool = True):
        super().__init__(parent)
        from .sim_tx import SimTxTeensy
        self.sim = sim if sim is not None else SimTxTeensy()
        self._open = False
        self._pending: list[str] = []
        self._clock = QTimer(self)                # simulated main-loop housekeeping
        self._clock.setInterval(100)
        self._clock.timeout.connect(lambda: self._deliver(self.sim.advance(100)))
        self._realtime = realtime

    def is_open(self) -> bool:
        return self._open

    def error_string(self) -> str:
        return "" if self._open else "simulator closed"

    def open(self, name: str) -> bool:
        self._open = True
        if self._realtime:
            self._clock.start()
        self.state_changed.emit(True)
        return True

    def close(self):
        if not self._open:
            return
        self._open = False
        self._clock.stop()
        self._pending.clear()
        self.state_changed.emit(False)

    def unplug(self):
        """Test helper: behave like a USB unplug."""
        self.error.emit("Serial port lost: simulated unplug")
        self.close()

    def send(self, text: str):
        if not self._open:
            return
        self.line_sent.emit(text)
        replies = self.sim.feed(text + "\n")
        if replies:
            self._pending += replies
            QTimer.singleShot(0, self._flush)

    def _flush(self):
        lines, self._pending = self._pending, []
        self._deliver(lines)

    def _deliver(self, lines):
        for ln in lines:
            if self._open:
                self.line_received.emit(ln)

    def request(self, text, pred, timeout_ms=1000):
        if not self._open:
            return None
        self.line_sent.emit(text)
        replies = self.sim.feed(text + "\n")
        self._flush()
        self._deliver(replies)
        return next((r for r in replies if pred(r)), None)
