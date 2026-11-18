"""Frame-aware links for the RX board (spec §6.2).

Both classes are LineLinks (so PortBar, the serial log and the shutdown `request()` work as for
TX) plus two extra signals:

    frame_received(object)                 one rx_protocol.Frame
    stats_changed(int, int, int)           crc_errors, resync_bytes, frames

RxSerialLink  - real Teensy via QSerialPort (event driven, readyRead -> FrameParser)
SimRxLink     - in-process fake RX Teensy (bench.sim_rx), paced by the wall clock at 400 kSPS
"""
from __future__ import annotations

from PySide6.QtCore import QElapsedTimer, QTimer, Signal

from .links import LineLink, SerialLink
from .rx_protocol import FrameParser
from .sim_rx import SimRxDevice

SIM_RX_PORT_NAME = "SIM-RX"


class _ParsingMixin:
    """Feeds device bytes through a FrameParser and emits lines / frames / stats."""

    def _init_parser(self):
        self.parser = FrameParser()
        self._stats_last = (0, 0, 0)

    def _consume(self, data: bytes):
        if not data:
            return
        for kind, item in self.parser.feed(data):
            if kind == "line":
                self.line_received.emit(item)
            else:
                self.frame_received.emit(item)
            if not self.is_open():
                return
        st = (self.parser.crc_errors, self.parser.resync_bytes, self.parser.frames)
        if st != self._stats_last:
            self._stats_last = st
            self.stats_changed.emit(*st)


class RxSerialLink(_ParsingMixin, SerialLink):
    frame_received = Signal(object)
    stats_changed = Signal(int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_parser()
        self.port.setReadBufferSize(0)       # unlimited: never drop USB data on a busy UI

    def open(self, name: str) -> bool:
        self.parser = FrameParser()
        self._stats_last = (0, 0, 0)
        return super().open(name)

    def _on_ready_read(self):
        self._consume(bytes(self.port.readAll()))


class SimRxLink(_ParsingMixin, LineLink):
    """Fake RX Teensy behind the link interface.

    realtime=True: a 10 ms QTimer advances the device by the elapsed wall-clock time.
    realtime=False (tests): call `run_samples(n)` / `run_ms(ms)` to advance simulated time.
    """
    frame_received = Signal(object)
    stats_changed = Signal(int, int, int)

    def __init__(self, dev: SimRxDevice | None = None, parent=None, realtime: bool = True):
        super().__init__(parent)
        self._init_parser()
        self.dev = dev if dev is not None else SimRxDevice()
        self._open = False
        self._realtime = realtime
        self._pending = bytearray()
        self._clock = QTimer(self)
        self._clock.setInterval(10)
        self._clock.timeout.connect(self._on_clock)
        self._elapsed = QElapsedTimer()
        self._done_us = 0

    def is_open(self) -> bool:
        return self._open

    def error_string(self) -> str:
        return "" if self._open else "simulator closed"

    def open(self, name: str) -> bool:
        self._open = True
        self.parser = FrameParser()
        self._stats_last = (0, 0, 0)
        self.dev.dtr = True
        if self._realtime:
            self._elapsed.start()
            self._done_us = 0
            self._clock.start()
        self.state_changed.emit(True)
        return True

    def close(self):
        if not self._open:
            return
        self._open = False
        self.dev.dtr = False                 # host-gone watchdog in the device
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
        out = self.dev.feed(text + "\n")
        if out:
            self._pending += out
            QTimer.singleShot(0, self._flush)

    def _flush(self):
        data, self._pending = bytes(self._pending), bytearray()
        if self._open:
            self._consume(data)

    def _on_clock(self):
        now_us = self._elapsed.nsecsElapsed() // 1000
        n = int((now_us - self._done_us) * 0.4)
        n = min(n, 200_000)                  # after a long UI stall: catch up at most 0.5 s per tick
        if n <= 0:
            return
        self._done_us += int(n / 0.4)
        if now_us - self._done_us > 1_000_000:
            self._done_us = now_us - 1_000_000
        self.run_samples(n)

    def run_samples(self, n: int):
        if self._open:
            self._flush()
            self._consume(self.dev.advance_samples(n))

    def run_ms(self, ms: float, step_ms: float = 5.0):
        """Tests: advance simulated time in steps, delivering bytes after each step."""
        done = 0.0
        while done < ms and self._open:
            s = min(step_ms, ms - done)
            self.run_samples(int(round(s * 400)))
            done += s

    def request(self, text, pred, timeout_ms=1000):
        if not self._open:
            return None
        self._flush()
        got: list[str] = []

        def cb(line):
            if not got and pred(line):
                got.append(line)

        self.line_received.connect(cb)
        try:
            self.line_sent.emit(text)
            self._consume(self.dev.feed(text + "\n"))
        finally:
            self.line_received.disconnect(cb)
        return got[0] if got else None
