# modem_bench — TX bench firmware + desktop app

Implements [TX_Software_Spec.md](TX_Software_Spec.md) (D1 firmware, D2 app, D3 tests).

```
modem_bench/
├─ tx_firmware/tx_firmware.ino   D1  Teensy 4.1 firmware (Teensyduino)
├─ modem_ui/                     D2  PySide6 app
│  ├─ modem_ui.py                    entry point  (--sim adds a simulated TX Teensy)
│  ├─ requirements.txt
│  └─ bench/                         tx_model (math, safe ordering) · sim_tx (fake Teensy)
│                                    links (serial / sim) · widgets · tx_tab · rx_tab · main_window
└─ tests/                        D3
   ├─ host_stubs/                    Arduino/SPI/Wire stubs (record DAC words, pot writes, back-powering)
   ├─ sim_main.cpp                   firmware unit tests (waveform, bursts, limits, ARM/DISARM, watchdog)
   ├─ fw_stdio.cpp                   firmware over stdin/stdout (for the cross-check)
   ├─ test_fake_vs_firmware.py       Python fake must reply exactly like the firmware
   ├─ test_tx_model.py               level model + safe command ordering (4000 random transitions)
   ├─ test_ui.py                     drives the app offscreen against the fake
   ├─ fake_teensy.py                 fake Teensy on a com0com virtual COM port or a pty
   └─ run_all.py                     runs everything above
```

## Firmware upload

Arduino IDE 2 + Teensyduino → Tools → Board **Teensy 4.1**, USB Type **Serial**, CPU **600 MHz**.
Open `tx_firmware/tx_firmware.ino` and upload. The Teensy can stay on USB: after reset all board
pins are high-Z until **ARM**. You can also type commands in the Serial Monitor (`HELP`, `STATUS`);
close it before using the app.

## App

```
cd modem_ui
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python modem_ui.py          # real hardware
python modem_ui.py --sim    # adds "SIM-TX" to the port list to try the app without hardware
```

Procedure: 12 V on (≥ 0.5 A limit, ≤ 14 V) → Connect → **Arm** → set level → **Start**.
To power down: **Stop (Esc)** → **Disarm** → 12 V off. Closing the app or disconnecting always
sends DISARM first. Help → *Bench rules* has the scope/probing rules.

## Tests (no hardware)

```
python tests/run_all.py     # needs g++ (MSYS2/MinGW) for the firmware part, PySide6 for the app part
```

## Changes relative to the reference implementation (Appendix A)

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

- Not compiled with Teensyduino here (no Teensy toolchain on this PC). It is checked only against host
  stubs. Compile it in the Arduino IDE first.
- Nothing has run on the real board yet. Follow spec §9.2. Things to watch for: ISR timing at 500 kHz
  with SPI at 20 MHz (check the TP7 staircase has a 2 µs period), and that the I2C watchdog does not
  give false NOPOWER disarms.
