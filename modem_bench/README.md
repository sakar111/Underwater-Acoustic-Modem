# modem_bench — TX + RX bench firmware and desktop app

Implements [TX_Software_Spec.md](TX_Software_Spec.md) and [RX_Software_Spec.md](RX_Software_Spec.md)
(D1 firmware, D2 app, D3 tests, plus the optional RX D4 TX SYNC output).

```
modem_bench/
├─ tx_firmware/tx_firmware.ino   TX D1  Teensy 4.1 firmware (TXFW 1.2: + SYNC output on pin 2)
├─ rx_firmware/rx_firmware.ino   RX D1  Teensy 4.1 firmware (RXFW 1.0: 400 kSPS ADC, frames, triggers)
├─ modem_ui/                     D2  PySide6 app, TX tab + RX tab, two independent connections
│  ├─ modem_ui.py                    entry point  (--sim adds simulated TX and RX Teensys)
│  ├─ requirements.txt               PySide6, pyqtgraph, numpy
│  └─ bench/                         tx_model · sim_tx · links · widgets · tx_tab · main_window
│                                    rx_protocol (frames, CRC, parser) · rx_model (§3.6 gain model)
│                                    rx_analysis (FFT, delay, measurements) · rx_io (save/load)
│                                    sim_rx (fake RX Teensy) · rx_link (serial / sim) · rx_tab
└─ tests/                        D3
   ├─ host_stubs/                    TX Arduino/SPI/Wire stubs (DAC words, pot writes, back-powering)
   ├─ host_stubs_rx/                 RX stubs: model the board (ADC alignment, AD5245 ACK, pins, DTR)
   ├─ sim_main.cpp                   TX firmware tests (waveform, bursts, limits, ARM/DISARM, watchdog, SYNC)
   ├─ rx_sim_main.cpp                RX firmware tests (Appendix B.1 scenarios 1-9 + extras)
   ├─ fw_stdio.cpp · rx_fw_stdio.cpp firmware over stdin/stdout (for the cross-checks)
   ├─ test_fake_vs_firmware.py       TX Python fake must reply exactly like the firmware
   ├─ test_rx_fake_vs_firmware.py    RX Python fake must reply exactly like the firmware (text + frame counts)
   ├─ test_rx_firmware_stream.py     RX firmware byte streams parsed by the app's Python parser
   ├─ test_rx_protocol.py · test_rx_model.py · test_rx_analysis.py   RX unit tests (spec §9.1.2 values)
   ├─ test_tx_model.py               level model + safe command ordering (4000 random transitions)
   ├─ test_ui.py · test_rx_ui.py     drive the app offscreen against the fakes (TX and RX at once)
   ├─ fake_teensy.py · fake_rx.py    fakes on a com0com virtual COM port or a pty
   └─ run_all.py                     runs everything above
```

## Firmware upload

Arduino IDE 2 + Teensyduino → Tools → Board **Teensy 4.1**, USB Type **Serial**, CPU **600 MHz**.
Open `tx_firmware/tx_firmware.ino` (TX board) or `rx_firmware/rx_firmware.ino` (RX board) and
upload. After reset every board pin is high-Z until **ARM**. You can type commands in the Serial
Monitor (`HELP`, `STATUS`; on RX also `RAW 8`, `STATS`); close it before using the app.

## App

```
cd modem_ui
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python modem_ui.py          # real hardware
python modem_ui.py --sim    # adds SIM-TX / SIM-RX to the port lists (the fake RX hears the fake TX)
```

Each tab has its own port bar; the port the other tab holds is greyed out, the USB serial number
is shown and remembered, and connecting a tab to the other board's Teensy is detected (PING) and
refused. **Esc** stops both boards (TX STOP, RX STREAM OFF + ABORT).

**TX** procedure: 12 V on (≥ 0.5 A limit, ≤ 14 V) → Connect → **Arm** → set level → **Start**.
To power down: **Stop (Esc)** → **Disarm** → 12 V off. Closing the app or disconnecting always
sends DISARM first. Help → *Bench rules (TX)* has the scope/probing rules.

**RX** procedure (RX spec §4.2): 12 V on (≈ 0.2 A limit), LED D6 lit → **then** plug in the RX
USB → Connect → **Arm** → Start. Power down: Stop → **Disarm** → **unplug the RX USB** → 12 V off.
Closing the app sends STREAM OFF, ABORT, DISARM. Help → *RX power procedure* has the bench notes.

## Tests (no hardware)

```
python tests/run_all.py     # needs g++ (MSYS2/MinGW) for the firmware part, PySide6 + pyqtgraph + numpy for the app part
```

Current result: TX firmware 116/116, RX firmware 105/105, Python 79/79 (incl. 10 s of streaming
with 0 gaps / 0 CRC errors, LEVEL and SYNC captures exact to the sample, save → reload identical).

## RX: deviations from RX_Software_Spec.md and additions

- **TX firmware is 1.2, not 1.1.** Version 1.1 was already taken by the TX fixes below, so the
  SYNC output (Appendix C) ships as `PONG TXFW 1.2`. The TX tab shows its *SYNC output* checkbox
  when STATUS contains `sync=`, instead of checking a version number.
- **SPL formula.** §6.6 writes `20·log10(V_rms / 1 µV) − RVS_dB` with RVS in dB re 1 V/µPa; that
  mixes units and reads 120 dB high. The app uses `SPL = 20·log10(V_rms / 1 V) − RVS` (dB re 1 µPa).
- **Parser resync.** After a CRC/header error the normative §5.3 algorithm can glue the next text
  line to the damaged frame's payload bytes (losing the line) and report single payload bytes as
  lines. While resyncing, the parser only emits lines that start with a record prefix (OK, ERR,
  WARN, STATUS, PONG, EVT) and salvages such a line from the junk. Otherwise as specified.
- **Cross-correlation delay** is refined from the envelope peak (flat-topped for a ramped burst)
  to the nearest carrier peak of the correlation; ±1 sample on synthetic data in noise.
- **Firmware extras:** `VERSION`, `HELP`, `ERR line too long`, `CAPTURE <n>` with `pre` optional
  (default 0), idempotent `ARM`, `WARN POTRESET` (wiper reset to mid-scale by a supply glitch is
  detected by the watchdog and rewritten, as on TX). Pin 26 (MOSI1, unconnected) is released
  together with the other SPI pins. `STATUS` while armed and idle reads the pot for `power=`.
- **Layout:** RX modules live in `modem_ui/bench/` beside the existing TX ones (not flat in
  `modem_ui/` as §8 sketches); RX host stubs are in `tests/host_stubs_rx/` because the RX board
  model differs from the TX one.
- **Captures with missing frames** are kept at full length, with missing samples marked
  (`incomplete` in the metadata, red markers in the plot) and never spliced.
- **CSV:** `v_adc` is the AC value `(code − mid) × LSB`; `time_s` is 0 at the trigger.
- **`--sim`:** the simulated RX follows the simulated TX (frequency, burst timing, level, SYNC),
  0.5 m of water (135-sample delay).

## TX: changes relative to the reference implementation (TX spec Appendix A)

Protocol, safety logic and behaviour are kept; these were added or fixed:

**Firmware (TXFW 1.1)**
- DISARM sets the pot to minimum gain *before* the 1.5 V → 0 V ramp (§4.3), not after it.
- Range checks reject `NaN` (`AMP NAN` used to pass and was passed to `lroundf`).
- Switching to BURST while transmitting starts in the off-period, so the amplitude ramps to zero
  before the first burst resets the phase (no click).
- Lines over 90 characters are rejected (`ERR line too long`) instead of running a truncated command.
- `WARN THERMAL` also fires when a change *while running* enters the thermal region; auto-stop after
  `THERMSTOP` seconds (default 60, 0 = off).
- Board watchdog while armed: the pot is read back every 500 ms. If it stops answering, the pins are
  released (`WARN NOPOWER`). If it has reset to mid-scale, it is rewritten (`WARN POTRESET`).
  Best effort only (see the note in the firmware header).
- Extensions from §5.2: `CAL`, `POTREAD`, `SWEEP f1 f2 ms | OFF`, `VERSION`, plus `HELP`, `THERMSTOP`.
  STATUS appends `cal= sweep= tstop=` (backward compatible).
- `Wire2.end()` on disarm or failure; the I2C/SPI pins return to INPUT.

**App**
- Command ordering: every combination of LIMIT/CAL/POT/AMP is checked so that no intermediate state
  exceeds the firmware limit. When the firmware state is unknown (after connect or an ERR), AMP 0 is
  sent first. The reference compared against the new limit only, which could pick a rejected order.
- Nothing except `PING` is sent until the device answers `PONG TXFW …`. Connecting the TX tab to the
  RX Teensy by mistake therefore sends it no TX commands.
- DISARM is always sent on disconnect or close, and the app waits for `OK DISARMED`. The reference
  sent it only if the last STATUS said armed.
- Each tab has its own port bar and link, so RX gets an independent connection. The serial log is
  shared and tagged `[TX]`/`[RX]`.
- Calibration factor, sweep helper, QSettings, Help → Bench rules, Esc = Stop, firmware-estimate readback.

## Not yet verified

- Neither firmware has been compiled with Teensyduino here (no Teensy toolchain on this PC). Both
  are checked only against host stubs. Compile them in the Arduino IDE first.
- Nothing has run on real boards yet. TX: follow TX spec §9.2; watch the ISR timing at 500 kHz
  with SPI at 20 MHz (TP7 staircase period 2 µs) and that the I2C watchdog gives no false disarms.
- RX: follow RX spec §9.2. The items marked **VERIFY AT BRING-UP** are not settled by the stubs:
  - ~~`ADC_SHIFT`~~ settled on the board (2026-11-18): the first ARM gave `mean=1024` with TP8 = 2.506 V, so the
    shift is **0**, not 1 as the spec assumed. Idle `RAW 8` words ≈ `0x0800`, bits 15..12 = 0;
  - SCLK actually lands in 10–16 MHz for the requested 12 MHz;
  - real ISR jitter (`STATS` after 10 min: per_min/per_max within 2500 ± 25 ns, isr_max < 2000 ns;
    the stub reports exactly 2500 / 1250 ns);
  - SINAD ≥ 55 dB at 400 kSPS;
  - Teensy pin 2 unconnected on the TX PCB before using SYNC (Appendix C).
- The real-port RX path (`RxSerialLink` over QSerialPort at 800 kB/s) has not been run; the app
  tests use the in-process fake. `tests/fake_rx.py --port` over a com0com pair exercises it.
- CPU while streaming, measured offscreen with `--sim`: ≈ 51 % of one core in total, of which the
  in-process fake device is ≈ 15 %, so ≈ 35 % for the app itself; plots ≈ 21 fps.
