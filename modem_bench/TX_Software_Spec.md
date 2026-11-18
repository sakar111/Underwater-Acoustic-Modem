# TX Board Bench Software — Build Specification

| | |
|---|---|
| **Project** | Underwater Acoustic Modem (Teensy 4.1-based; inspired by TUHH "ahoi") |
| **Author** | Sakar Pathak (UNR, advisor Prof. Hao Xu) |
| **Purpose** | Complete, self-contained hand-off to an AI coding agent (Claude Code in VS Code) to build the **TX bench-test firmware** and a **Windows desktop control app**, designed so an **RX tab** can be added later from a separate RX specification. |
| **Version** | 1.0 — 16 Nov 2026 |
| **Hardware status** | TX PCB assembled and powered up. All DC test points verified (see §9.2). Hydrophone (Aquarian S1n) available. Teensy not yet running signal firmware. |

---

## 0. Read this first (instructions for the coding agent)

1. **Everything you need about the hardware is in this document.** Pin numbers, nets, chip protocols, gains and safety limits were verified against the real schematic netlist and the chip datasheets. Do not invent pins, addresses or gains. If something you need is missing, stop and ask the user instead of guessing.
2. **Deliverables** (details in §5–§8):
    - **D1 — TX firmware** for Teensy 4.1 (Arduino/Teensyduino, `.ino` + optional `.h/.cpp`).
    - **D2 — Python desktop app** for Windows: PySide6 (Qt 6) + QtSerialPort. One window, a **TX tab** now and an **RX tab** placeholder. The TX and RX boards each have **their own Teensy and their own COM port**; the app must be able to talk to both at once later.
    - **D3 — Tests**: a host-side (PC) build of the firmware logic with stubbed Teensy APIs, plus a fake serial device so the app can be tested without hardware (§9.1).
3. **Safety rules in §4 are hard requirements.** They protect a 12 V / ±22 V analog board, two power op-amps and a hydrophone. Implement them in the **firmware** (the final authority), and mirror them in the app for a good user experience.
4. A **working reference implementation** of D1 and D2 is in Appendix A. It was checked by compiling the firmware against host stubs and driving the app against a simulated Teensy. **It has not yet been run on the real hardware.** You may use it as the starting point, refactor it, or rewrite it, but keep the behaviour, protocol and safety logic.
5. Target environment: **Windows 10/11**, Python 3.10+, Teensyduino (Arduino IDE 2.x or PlatformIO), Teensy 4.1 at the default 600 MHz.

---

## 1. Project goal and context

- Two separate boards, each with its own Teensy 4.1:
    - **TX board** — Teensy → DAC → reconstruction filter → digitally set level → bridge power amplifier → **transmit hydrophone (Aquarian S1n)**.
    - **RX board** — receive hydrophone (Aquarian AS-1) → JFET LNA → switchable-gain stage → 20–90 kHz band-pass → AGC (digital pot) → 12-bit ADC → Teensy.
- There is **no electrical link** between the boards; they communicate acoustically through water.
- **Operating band: 20–90 kHz.** Use case: short range (0.5 m aquarium tests now; a few to tens of metres later, e.g. near-shore lake).
- Final data-rate target ≥ 10 kbit/s. **Modulation is not decided yet** (ahoi uses BFSK). This milestone is **bench bring-up**: generate clean test tones and bursts, set the output level safely, and verify every stage with an oscilloscope.
- Later milestones (design the code so they fit; do not implement now unless asked): arbitrary waveform/packet playback, chirps, BFSK/OFDM packets, **TX pre-emphasis** (the transducer transmits about 26–28 dB more efficiently at 90 kHz than at 20 kHz, so the drive should eventually be equalised across the band), synchronised TX/RX experiments.

---

## 2. TX hardware overview

### 2.1 Signal chain

```
Teensy 4.1 ──SPI1──► DAC8830 (16-bit, VREF = 3.000 V, 500 kHz updates)
   │                    │ DAC_OUT (TP7), 0..3 V, unbuffered (6.25 kΩ output)
   │                    ▼
   │              R20 0 Ω ─► BUF_IN ── 47 pF to GND (anti-slew RC with the DAC's 6.25 kΩ)
   │                    ▼
   │              U9A  OPA4322 unity buffer ─► BUF_OUT (TP10)
   │                    ▼
   │              U9B + U9C  4th-order Bessel low-pass (2 × unity Sallen-Key) ─► LPF_OUT (TP8)
   │                    ▼
   │              C34 10 nF + R21 10 kΩ (AC coupling, HP ≈ 1.6 kHz)
   │                    ▼
   └──I2C2 (Wire2)─► U9D inverting level stage, feedback = AD5245 rheostat ─► TXLVL_OUT (TP9), biased at 1.5 V
                        ▼
                  Bridge (BTL) power amp: U7 OPA552 + U6 OPA551, ±22 V rails, differential gain 27.31
                        ▼
                  AMP_A / AMP_B ─► 1 µF film DC-block caps (C18, C25) ─► J2 ─► S1n hydrophone
```

### 2.2 Power

| Rail / net | Source | Value | Test point | Measured at bring-up |
|---|---|---|---|---|
| VIN_Supply | J1 (screw terminal) | 12 V in | — | — |
| VIN | after SS34 diode + SMAJ15A TVS | ≈ 11.6–11.7 V | TP6 | OK |
| +3V3 | TLV70433 LDO via R1 0 Ω cut-link | 3.30 V | TP2 | OK |
| REF_3V | REF2030 VREF | 3.000 V (DAC reference **and** OPA4322 supply) | TP3 | OK |
| REF_1V5 | REF2030 VBIAS | 1.500 V (bias for level stage and power amp) | TP4 | OK |
| VDD | LMR64010 boost | +22.2 V | TP1 | OK |
| VSS | LMR16006 inverting buck-boost | −22.2 V | TP5 | OK |

- **Board idle current ≈ 0.08 A at 12 V** (measured).
- **Bench supply current limit must be ≥ 0.5 A.** At 0.3 A the switching converters' start-up surge trips the limit and the board latches at ≈ 2 V (observed, harmless). Set 12 V with the output off, then switch the output on (do not ramp slowly).
- **Do not exceed 14 V input**: the LMR64010 boost is rated 14.5 V absolute maximum, and the input TVS only clamps from ≈ 16.7 V.
- The Teensy is **powered from USB only**. Its VIN and 3.3 V pins are **not connected** to the board. Only GND is shared.

### 2.3 Teensy 4.1 pin map (TX board) — verified from the schematic netlist

| Teensy pin | Net | Connected to | Direction | Notes |
|---|---|---|---|---|
| 26 (MOSI1) | SPI1_MOSI | DAC8830 SDI (pin 6) | out | SPI1 default MOSI |
| 27 (SCK1) | SPI1_SCK | DAC8830 SCLK (pin 5) | out | SPI1 default SCK |
| 28 | SPI1_CS | DAC8830 CS̄ (pin 4) + **R18 10 kΩ pull-up to +3V3** | out (GPIO) | **Not** the SPI1 hardware CS (that is pin 38). Drive with `digitalWriteFast`. |
| 24 (SCL2) | I2C2_SCL | AD5245 SCL + **R7 4.7 kΩ pull-up to board +3V3** | I2C | `Wire2` |
| 25 (SDA2) | I2C2_SDA | AD5245 SDA + **R8 4.7 kΩ pull-up to board +3V3** | I2C | `Wire2` |
| 3 | LED_TX | LED D3 via 1 kΩ to GND | out | "transmitting" indicator |
| 8 | LED_HB | LED D4 via 1 kΩ to GND | out | heartbeat |
| GND (×3) | GND | board ground | — | common ground |
| VIN, 3.3 V | — | **not connected** | — | never connect |

No MISO is used on the TX board (the DAC is write-only). Pin 39 (MISO1) is unconnected.

**Important property for safety logic:** the I2C pull-ups go to the **board's** +3V3 rail, not the Teensy's. With the I2C pins configured as plain inputs, SCL/SDA read **HIGH only when the TX board is powered**. The firmware uses this to detect board power (§4.1).

### 2.4 Test points

| TP | Net | Idle DC (board on, Teensy off or DAC at 0 V) | With DAC at mid-scale (armed, not transmitting) |
|---|---|---|---|
| TP1 | VDD | +22.2 V | same |
| TP2 | +3V3 | 3.30 V | same |
| TP3 | REF_3V | 3.000 V | same |
| TP4 | REF_1V5 | 1.500 V | same |
| TP5 | VSS | −22.2 V | same |
| TP6 | VIN | ≈ 11.6 V | same |
| TP7 | DAC_OUT | 0 V | 1.50 V |
| TP10 | BUF_OUT | ≈ 0 V | 1.50 V |
| TP8 | LPF_OUT | ≈ 0 V | 1.50 V |
| TP9 | TXLVL_OUT | 1.50 V | 1.50 V |
| R9 / R16 pads | AMP_A_OUT / AMP_B_OUT | ≈ 0 V (± tens of mV) | ≈ 0 V |

(The level stage is AC-coupled, so the DAC's DC level never reaches the power amp. That is why TP9 and the power-amp outputs stay put when the DAC moves from 0 V to mid-scale.)

---

## 3. Chip-level details the software depends on

### 3.1 DAC8830 (U8) — 16-bit, unbuffered voltage DAC

Source: TI DAC8830 datasheet (SLAS449D).

- **Serial word: 16 bits, MSB first.** "It takes 16 clocks of SCLK to transfer one data word." If more than 16 clocks occur while CS̄ is low, the **last 16 bits** are used.
- **SDI is latched on the rising edge of SCLK** → use **SPI mode 0** (CPOL = 0, CPHA = 0).
- **Output updates on the CS̄ rising edge.** There is no LDAC pin on the 8-pin DAC8830.
- SCLK up to 50 MHz. CS̄ high time between frames ≥ 30 ns. Wait ≥ 10 µs after VDD comes up before the first CS̄ low.
- **Power-on: output = 0 V (zero code).** The input shift register is **not** cleared at power-up, so always send a complete 16-bit word first.
- **Transfer function (straight binary, unipolar):** `VOUT = VREF × code / 65536`, VREF = 3.000 V.
    - code 0 → 0 V; code 0x8000 (32768) → 1.500 V (mid-scale); 0xFFFF → VREF − 1 LSB.
    - 1 LSB = 45.8 µV. 1 mV ≈ 21.85 codes.
- Settling time ≈ 1 µs; output impedance 6.25 kΩ (buffered on the board by U9A).
- **Update rate for this project: 500 kHz** (one sample every 2 µs). With fmax = 90 kHz this gives ≥ 5.5 samples per cycle, and the first image at 410 kHz is attenuated ≈ 20 dB by the Bessel filter. SPI at 20–30 MHz takes ≈ 0.6–0.8 µs per word, which fits.
- **Usable signal window:** keep the waveform within **1.5 V ± 1.4 V peak** (codes ≈ 2200..63300). The OPA4322 runs from 0–3.0 V, and the level stage drives a 2.49 kΩ load, so ±1.4 V keeps it out of the rails.

### 3.2 REF2030 (U1)

3.3 V in → 3.000 V (REF_3V) and 1.500 V (REF_1V5). REF_3V is both the DAC reference and the supply of the quad op-amp U9 (OPA4322). No software interaction.

### 3.3 Buffer and reconstruction filter (U9A/B/C, OPA4322)

- U9A: unity follower. 47 pF C42 at its input forms an RC with the DAC's 6.25 kΩ (fc ≈ 450–540 kHz), so the 10 V/µs buffer never slew-limits on the 500 kHz steps.
- U9B/U9C: 4th-order Bessel low-pass, two unity-gain Sallen-Key stages (R26 820 Ω, R27 27 kΩ, C33 470 pF, C40 47 pF; R22 1.2 kΩ, R23 15 kΩ, C32 470 pF, C39 47 pF). Stage fc/Q: 227.6 kHz/0.535 and 252.4 kHz/0.828.
- **Response: flat in 20–90 kHz** (−0.04 dB at 20 kHz, ≈ −0.9 dB at 90 kHz including the 47 pF RC); −20 dB at 410 kHz (the first DAC image at 500 kHz).
- **DC gain = 1, no reference voltage.** LPF_OUT follows the DAC's DC level.

### 3.4 Level control stage (U9D + AD5245 U5)

- **Topology:** inverting amplifier. Input LPF_OUT → C34 10 nF → R21 10 kΩ → U9D −IN (net POT_W). +IN = REF_1V5 via R28 10 kΩ. **Feedback = AD5245 used as a rheostat between terminal A (= TXLVL_OUT, the output) and wiper W (= POT_W, −IN). Terminal B is open.** C28 10 pF across the feedback.
- **Gain magnitude:** `G_lvl = R_WA / R21 = R_WA / 10 kΩ`. The output is inverted relative to LPF_OUT and centred on 1.5 V.
- **AD5245 rheostat equation (datasheet eq. 2):** `R_WA(D) = (256 − D)/256 × R_AB + 2 × R_W`, with R_AB = 10 kΩ and R_W ≈ 50 Ω typical (120 Ω max). D = 0..255.
    - **D = 0 → maximum gain** (R_WA ≈ 10.1 kΩ, G ≈ 1.01).
    - **D = 255 → minimum gain** (R_WA ≈ 139 Ω, G ≈ 0.014).
    - **Power-on default: mid-scale, D = 128** (G ≈ 0.51).
- C34/R21 high-pass corner ≈ 1.6 kHz (no effect in band). The AD5245 10 kΩ has ≈ 600 kHz bandwidth.

#### AD5245 I2C protocol

Source: ADI AD5245 datasheet, Rev. C.

- 7-bit address **0x2C** (AD0 tied to GND). Write byte 0x58, read byte 0x59.
- **Write:** `START, 0x2C+W, ACK, instruction byte, ACK, data byte (D7..D0), ACK, STOP`.
- **Instruction byte = `X RS SD X X X X X`:**
    - bit 6 **RS** = mid-scale reset (moves the wiper to the centre tap; the register is overwritten).
    - bit 5 **SD** = shutdown (opens terminal A and shorts W to B; register unchanged).
    - **Normal write: instruction byte = 0x00.**
    - Do **not** use SD on this board: opening terminal A breaks U9D's feedback path.
- **Read:** `START, 0x2C+R` → one data byte (current wiper code); the master NACKs, then STOP.
- I2C clock up to **400 kHz**. Terminal voltages must stay within GND..VDD (3.3 V); the circuit guarantees this because U9 runs from 3.0 V.
- Teensy usage: `Wire2.beginTransmission(0x2C); Wire2.write(0x00); Wire2.write(D); Wire2.endTransmission();` (return value 0 = ACK).

### 3.5 Bridge power amplifier (U6 OPA551, U7 OPA552)

- **U7 (OPA552, "AMP_B"):** inverting stage. −IN gets TXLVL_OUT through R24 0 Ω + R25 2.49 kΩ; feedback R19 68 kΩ from AMP_B (after R16) and C26 4.7 pF from the op-amp output pin. +IN = divider R15 68 kΩ (from AMP_A) / R17 2.49 kΩ (to REF_1V5).
- **U6 (OPA551, "AMP_A"):** unity inverter of AMP_B (R14 33 kΩ in, R13 33 kΩ feedback, C19 10 pF), +IN to GND via R11 15 kΩ.
- Result (verified analytically):
    - **Each leg:** `AMP_B = +13.65 × (TXLVL_OUT − REF_1V5)`. Because TXLVL_OUT is the inverted LPF_OUT, AMP_B is in phase with LPF_OUT.
    - `AMP_A = −AMP_B`.
    - **Differential voltage across the transducer:** `V_diff = AMP_A − AMP_B`, with a magnitude gain of **27.31** from TXLVL_OUT's AC swing.
    - **DC:** both legs sit at ≈ 0 V when TXLVL_OUT = REF_1V5. Any offset is blocked by the 1 µF film caps.
- Rails ±22 V; each leg swings to about ±19 V at high current → the maximum differential drive is ≈ 76 Vpp.
- R9/R16 are 0 Ω placeholders (isolation resistors 22–47 Ω can be fitted if ringing appears with the transducer).
- **Thermal (important):**
    - SOIC-8 package, θJA ≈ 97 °C/W, thermal shutdown at ≈ 160 °C (output turns off, recovers at 140 °C; the parts are protected but repeated shutdown degrades reliability).
    - Idle ≈ 0.31 W per amp.
    - With the transducer (≈ 7.1 nF incl. cable): `P_amp ≈ 0.31 W + 14.0 × I_pk` (W, with I_pk in A), where `I_pk ≈ π × f × 7.1 nF × Vpp_diff`.
    - **Continuous-operation guideline: f × Vpp_diff ≲ 2.3 × 10⁶** (e.g. ≈ 25 Vpp at 90 kHz, 46 Vpp at 50 kHz). Short bursts may exceed this.
    - No load (transducer disconnected) → only idle heat.

### 3.6 End-to-end level equation (use this everywhere)

```
A_dac      = DAC peak amplitude around 1.5 V   [V]   (0 .. 1.4 V)
G_lvl(D)   = ((256 − D)/256 × 10000 + 100) / 10000
Vpp_diff   = 2 × A_dac × G_lvl(D) × 27.31             [V peak-to-peak across the hydrophone]
```

Accuracy is about ±5 % (resistor tolerances, wiper resistance, the filter's −0.9 dB at 90 kHz). The app should allow a **user calibration factor** (default 1.00) measured with the scope.

| Pot code D | G_lvl | Vpp_diff per 100 mV pk of DAC amplitude |
|---|---|---|
| 0 | 1.010 | 5.52 V |
| 64 | 0.760 | 4.15 V |
| 128 | 0.510 | 2.79 V |
| 192 | 0.260 | 1.42 V |
| 224 | 0.135 | 0.74 V |
| 240 | 0.0725 | 0.40 V |
| 255 | 0.0139 | 0.08 V |

Example used for first tests: A_dac = 200 mV pk, D = 200 → G = 0.229 → **≈ 2.5 Vpp** across the hydrophone. The scope should show:

| Point | Expected |
|---|---|
| TP7 | Staircase sine, 1.5 V ± 0.2 V |
| TP8 | Smooth sine, 1.5 V ± 0.2 V |
| TP9 | Inverted sine, 1.5 V ± 46 mV |
| AMP_A_OUT / AMP_B_OUT | Each ≈ ±0.63 V, opposite phase |

### 3.7 Transducer (Aquarian S1n)

- Capacitive load ≈ 6.5 nF plus ≈ 0.6 nF of cable (5 m).
- Max input 50 Vpp continuous / 200 Vpp at < 10 % duty cycle.
- TVR 128 dB re 1 µPa/V at 1 m; the response wobbles above ≈ 70–90 kHz.
- **Project drive policy:**
    - **≤ 50 Vpp continuous.**
    - 80 Vpp only in bursts with < 10 % duty cycle.
    - **In air (bench, no water): keep ≤ ~10–20 Vpp**, mostly at 20–60 kHz, using short runs or bursts. In air almost no sound leaves the element, so a higher drive has no benefit.
    - Close-range aquarium tests: a few Vpp is enough, and more can clip the RX front end.

---

## 4. Safety requirements (MUST implement)

### 4.1 Never back-power the board from the Teensy

**Problem:** if the Teensy is on USB while the board's 12 V is off and firmware drives CS̄ (pin 28), MOSI or SCK high, the 3.3 V on those pins forward-biases the DAC8830's input ESD diodes. Current then flows into the board's unpowered +3V3 rail and partially powers the REF2030, OPA4322, AD5245 and LED (≈ 10 mA through one GPIO and one ESD diode). It won't destroy anything instantly, but it is outside ratings and the chips come up in an undefined state.

**Required behaviour:**

1. At boot (`setup()`), configure pins 24, 25, 26, 27, 28 as **INPUT** (high-Z). Do not call `SPI1.begin()` or `Wire2.begin()` yet.
2. Introduce an explicit **ARM** step, triggered by a command from the app. ARM must first **check board power**: set pins 24/25 to INPUT (no internal pull), wait ≈ 2 ms, and require `digitalRead(24) == HIGH && digitalRead(25) == HIGH`. If either is LOW → refuse with an error; leave all pins high-Z.
3. Then start I2C and write the pot to minimum gain (D = 255). If there is no ACK → refuse (board not powered or pot fault) and release the pins.
4. Then set pin 28 to OUTPUT HIGH, start SPI1, write a full word of 0, ramp the DAC from 0 V to mid-scale (1.5 V) over ≈ 50 ms, then write the user's pot setting and start the sample timer.
5. **DISARM** reverses this:
    - stop output (ramp the amplitude to 0)
    - stop the timer
    - ramp the DAC from 1.5 V to 0 V
    - set the pot to 255
    - end SPI
    - return pins 24–28 to INPUT
6. The app must DISARM before disconnecting or closing.
7. The STATUS reply reports `power=1/0` (the same SDA/SCL check, done only while not armed).
8. LED pins 3 and 8 are always safe to drive (LED + resistor to board GND only).

**User procedure (show in the app):** turn on 12 V first, then ARM. To power down: STOP → DISARM, then switch off 12 V. USB may stay plugged in permanently: while disarmed the Teensy drives nothing into the board.

(The RX board is different: its I2C pull-ups go to the Teensy's own 3.3 V, so this power check does **not** work there. See §7.)

### 4.2 Output-level limits

- The firmware keeps a **limit in Vpp** (default **20 Vpp**, settable 1..50, never above 50).
- Every command that changes amplitude, pot code or the limit must compute `Vpp_diff` (§3.6) and **reject** the change if it would exceed the limit.
- The app shows the estimate live and does not send values that exceed the limit.
- DAC amplitude is limited to 0..1400 mV peak around mid-scale; clamp DAC codes to 0..65535 in the ISR as a last resort.
- **Thermal guard:** for continuous mode, if `f × Vpp_diff > 2.3e6`, the firmware replies with a `WARN THERMAL …` line and the app shows an orange warning. Optionally, the firmware may auto-stop continuous operation after N seconds above this threshold (make N configurable, default 60 s).
- **Burst mode** is the preferred way to run high levels.

### 4.3 Start-up / click avoidance

- Ramp the DAC from 0 V to 1.5 V (and back) with the pot at minimum gain.
- Amplitude changes and burst edges are ramped (≈ 100 µs linear envelope) to avoid broadband clicks.
- Each burst starts at phase 0 (repeatable).

### 4.4 Bench notes to display in the app's help text

- Bench supply 12 V with a current limit of ≥ 0.5 A; maximum input 14 V.
- Never clip a scope ground lead to AMP_A/AMP_B or to the hydrophone wires: scope ground is board GND, so this shorts a power-amp output. Probe each leg to board GND and use the scope's A − B math.
- Keep the hydrophone's cable end and the J2 terminal dry.

---

## 5. Firmware requirements (D1)

### 5.1 Structure

- **Sample engine:** `IntervalTimer` at **2.0 µs** (500 kHz), priority high (e.g. 32). ISR work:
    1. update the burst envelope
    2. ramp the current amplitude toward its target
    3. advance a **32-bit phase accumulator**
    4. look up a **1024-entry Q15 sine table** (index = phase >> 22)
    5. `code = 32768 + ((amp_codes × sine) >> 15)`, clamped 0..65535
    6. send the 16-bit word over SPI1 with CS̄ framing

    Keep the ISR minimal: no floats, no Serial, no I2C.

- Phase step: `step = round(f / 500000 × 2³²)`. Frequency range 100 Hz..100 kHz (warn the user above 90 kHz).
- SPI: `SPI1.beginTransaction(SPISettings(20000000, MSBFIRST, SPI_MODE0))` once at ARM, kept open (SPI1 serves only the DAC); `SPI1.transfer16(code)` per sample. 20 MHz is a safe default; it can be raised to 30 MHz.
- I2C: `Wire2`, 400 kHz, only in the main loop (never in the ISR).
- Main loop:
    - read USB Serial lines (handle `\n` or `\r\n`, max ≈ 90 chars)
    - execute commands and print one reply line per command (plus optional WARN lines)
    - blink the heartbeat LED (slow when disarmed, fast when armed)
    - LED_TX on while transmitting
- Shared ISR variables must be `volatile`. Multi-word updates (burst counters) are done with interrupts disabled.

### 5.2 Serial protocol (ASCII, one command per line, case-insensitive)

Replies start with `OK`, `ERR`, `WARN`, `STATUS` or `PONG`. Numbers are decimal.

| Command | Effect | Reply |
|---|---|---|
| `PING` | identify | `PONG TXFW <version>` |
| `ARM` | power check → init (§4.1) | `OK ARMED` / `ERR NOPOWER …` / `ERR NOPOT …` |
| `DISARM` | safe shutdown (§4.1) | `OK DISARMED` |
| `FREQ <Hz>` | 100..100000 | `OK FREQ <Hz>` / `ERR …` |
| `AMP <mV>` | DAC peak amplitude, 0..1400 | `OK AMP <mV>` / `ERR LIMIT …` |
| `POT <D>` | AD5245 code 0..255 (255 = min gain); written immediately if armed, else stored | `OK POT <D>` / `ERR LIMIT …` / `ERR NOPOT …` |
| `LIMIT <Vpp>` | 1..50, must be ≥ current estimate | `OK LIMIT <Vpp>` |
| `MODE CONT` | continuous tone | `OK MODE CONT` |
| `MODE BURST <on_us> <period_us>` | 10 ≤ on < period ≤ 10 000 000 | `OK MODE BURST <on> <per>` |
| `START` | requires ARM and estimate ≤ limit | `OK START` (+ optional `WARN THERMAL …` line) |
| `STOP` | ramp to silence (DAC stays at 1.5 V) | `OK STOP` |
| `STATUS` | report state | see below |

`STATUS` format (single line, space-separated key=value):

```
STATUS armed=1 run=0 freq=25000 amp=200 pot=200 mode=CONT on_us=1000 per_us=100000 est=2.50 limit=20.0 power=1
```

Recommended extensions (backward compatible): `CAL <factor>` (scales the estimate), `POTREAD` (reads the wiper back over I2C), `SWEEP <f1> <f2> <ms>` (linear frequency sweep), `VERSION`.

### 5.3 Acceptance criteria (firmware)

1. With 12 V off: `ARM` → `ERR NOPOWER`, and pins 24–28 remain inputs.
2. With 12 V on: `ARM` → `OK ARMED`. TP7/TP10/TP8 read 1.50 V; TP9 1.50 V; amp outputs ≈ 0 V.
3. `FREQ 25000`, `AMP 200`, `POT 200`, `START` → 25.0 kHz sine; amplitudes match §3.6 within ±10 %; no MHz oscillation on AMP_A/B.
4. `MODE BURST 1000 100000` → 1 ms bursts every 100 ms, clean ramped edges, each burst starts at the same phase.
5. `AMP` / `POT` / `LIMIT` combinations exceeding the limit are rejected and leave the running output unchanged.
6. `DISARM` → DAC ramps to 0 V, pot = 255, pins released; `STATUS` shows `armed=0`.

---

## 6. Desktop app requirements (D2)

### 6.1 Technology

- Python 3.10+, **PySide6** (Qt 6) with **QtSerialPort** (event-driven `readyRead`, no polling threads). `requirements.txt`: `PySide6>=6.5`. Later RX plotting: **pyqtgraph** (add when the RX tab is built).
- Single main script (`modem_ui.py`), or a small package (`app/`, `devices/tx.py`, `devices/rx.py`, `widgets/`). Keep the serial transport and the protocol separate from the widgets so the RX tab can reuse them.
- Teensy USB serial: VID 0x16C0. Pre-select ports with this VID. On Windows the port is `COMx`. Set DTR on open. Baud rate is irrelevant (USB), so use 115200. Only one program can hold a COM port, so tell the user to close the Arduino Serial Monitor.

### 6.2 Window layout

- **Top bar (per tab or global):** port combobox (shows `COM5 — USB Serial Device (Teensy)`), Refresh, Connect/Disconnect. TX and RX need **independent connections**, so put the port selector inside each tab.
- **TX tab:**
    1. *Board* group — status text and buttons:
        - status: "Not connected" / "Board 12 V not detected — Teensy pins released" / "Board powered — not armed" / "ARMED"
        - **Arm (12 V is ON)**: shows a confirmation dialog
        - **Disarm**
    2. *Signal* group:
        - frequency slider (20–90 kHz) + spinbox (100 Hz–100 kHz)
        - preset buttons 20/25/40/60/80/90 kHz
        - Mode: Continuous / Burst, with burst on-time (ms) and period (ms)
    3. *Level* group:
        - DAC amplitude slider + spinbox (0–1400 mV pk)
        - level-gain slider oriented so **right = more gain** (slider value s → pot code D = 255 − s), showing D and the gain
        - safety limit spinbox (default 20 Vpp, max 50) with the hint "≤ 20 Vpp in air; S1n max 50 Vpp continuous"
        - calibration factor
        - **large estimated output label** "≈ X.X Vpp across the transducer" (green when OK, red above the limit)
    4. *Run* group: Start, Stop, state label ("TRANSMITTING" in red).
    5. Warning area: limit exceeded (red), thermal guideline (orange), firmware ERR/WARN lines.
- **RX tab:** placeholder now (see §7).
- **Serial log** at the bottom: `→ command` / `← reply`. Don't log repeated identical STATUS lines.

### 6.3 Behaviour

- Edits are debounced (≈ 120 ms), then only the changed settings are sent.
- **Never send a combination above the limit.** Order the commands so the firmware never sees an intermediate value above its limit:
    - if the new amplitude with the old pot code is within the limit → send AMP then POT, else POT then AMP;
    - raise LIMIT first, lower LIMIT last.
- On connect: `PING`, then `POT 255`, then push all settings, then `STATUS`. Poll `STATUS` every 1 s; it drives the board/run labels and enables or disables the buttons.
- Start is enabled only when armed and estimate ≤ limit. Stop and Disarm are enabled when armed.
- On ERR: show it, forget the "last sent" cache (so the next edit resends everything), request STATUS.
- On disconnect or window close: if armed, send `DISARM` (and wait for the bytes to be written) before closing the port.
- Handle USB unplug (QSerialPort ResourceError) gracefully: mark disconnected.
- Nice to have:
    - save/restore settings (QSettings)
    - a "Help / bench rules" dialog with §4.4
    - keyboard shortcut for Stop (e.g. Esc)
    - a frequency-sweep helper

### 6.4 Acceptance criteria (app)

- Works against the fake device (§9.1) and against the real board.
- Cannot start transmission above the limit; shows the thermal warning when appropriate.
- Arm refused cleanly when the board is unpowered.
- Closing the app while transmitting leaves the board disarmed.

---

## 7. RX board — summary for app architecture only

A separate **RX specification** (from the RX design chat) will give full details. Design the app now so an RX tab drops in without restructuring. Known facts:

- **Signal chain:** AS-1 hydrophone → LSK170A JFET LNA (≈ 9×) → ADA4807-1 switchable-gain stage (HIGH ≈ 58–59× / LOW ≈ 7.2× via TS5A3166 analog switch) → 4th-order Butterworth band-pass 20–90 kHz (OPA4322) → AGC (OPA365 + AD5245, ≈ 1.9×–180×) → ADC121S101.
- **Single 5 V analog supply; all stages biased at 2.5 V** (ADC mid-scale).
- **ADC121S101:** 12-bit, SPI, up to 1 MSPS, **project sample rate 400 kSPS**. 16-clock frame: 3 leading zeros, then 12 data bits MSB first, then trailing zeros. Data is shifted out on SCLK **falling** edges, so read on the rising edge, typically SPI mode 3 or mode 2 (confirm in the RX spec / datasheet). SCLK ≈ 6.4 MHz minimum at 400 kSPS; the ADC allows up to 20 MHz.
- **RX Teensy pins (from the RX validation):**
    - SPI1 SCK = pin 27; CS = pin 28 (GPIO; it also enables the MISO level-shift buffer).
    - **MISO1 = pin 39, so firmware must call `SPI1.setMISO(39);` before `SPI1.begin();`.**
    - I2C2 SCL = 24 / SDA = 25 → AD5245 at 0x2C (AGC gain).
    - A GPIO `RX_GAIN_SEL` drives the TS5A3166: HIGH = LOW-gain state; it has a pull-down, so the default is HIGH gain. The pin number comes from the RX spec.
- **RX power check differs:** RX I2C pull-ups go to the **Teensy's own 3.3 V**, so the SDA/SCL trick does not detect RX board power. The RX spec will define its own rule (manual confirmation, or a sense input).
- **App implications for later:**
    - a separate connection, its own Arm/Disarm
    - gain-state toggle, AGC pot slider with gain readout
    - capture controls (block length, trigger: free-run or level), live time-domain plot and FFT (pyqtgraph)
    - optional CSV/NumPy save
    - streaming should be **binary blocks** over USB (e.g. a header + N × uint16 samples). At 400 kSPS × 2 bytes = 0.8 MB/s, which Teensy 4.1's 480 Mbit/s USB handles; plot decimated data and refresh the plot at ~20–30 fps.
    - keep the TX protocol ASCII; define the RX block format in the RX spec.

---

## 8. Project layout and build

```
modem-bench/
├─ tx_firmware/
│  └─ tx_firmware.ino           # Teensy 4.1, Teensyduino (Board: Teensy 4.1, USB Type: Serial)
├─ modem_ui/
│  ├─ modem_ui.py               # PySide6 app (TX tab + RX placeholder)
│  └─ requirements.txt          # PySide6>=6.5   (+ pyqtgraph later)
└─ tests/
   ├─ host_stubs/               # Arduino.h, SPI.h, Wire.h stubs for compiling firmware on the PC
   ├─ sim_main.cpp              # unit tests: commands, limits, waveform checks
   ├─ fake_teensy.py / ptysim   # fake serial device implementing the protocol (for app tests)
   └─ test_ui.py                # drives the app (offscreen) against the fake device
```

- **Firmware upload:** Arduino IDE 2 with Teensyduino; Tools → Board: Teensy 4.1, USB Type: Serial, CPU 600 MHz. The Teensy may stay connected to USB; uploading does not drive the board pins (they are high-Z after reset).
- **App:**
    - `py -m venv .venv`
    - `.venv\Scripts\activate`
    - `pip install -r requirements.txt`
    - `python modem_ui.py`
- **Windows fake device for app testing:** use a virtual COM pair (e.g. com0com) with a Python script implementing the protocol on the other end, or test in WSL/Linux with a pty.

---

## 9. Verification

### 9.1 Without hardware

1. **Host build of the firmware:** compile `tx_firmware.ino` as C++ with stub headers (String, SPI1, Wire2, IntervalTimer, Serial, pin functions), then:
    - feed commands, call the ISR N times, check the generated codes:
        - 25 kHz at 500 kHz → 20 samples per cycle, 200 rising mid-crossings in 4000 samples
        - min/max = 32768 ∓ round(0.2/3×65536) = 28399/37137 for AMP 200
    - check the burst duty (1 ms on of 10 ms → ≈ 10 % active samples)
    - check limit rejections
    - check ARM refused with "no power" (SDA/SCL stub LOW) and pins left as INPUT
    - check DISARM ends at code 0 with pot 255
2. **Fake device:** a program or pty that runs the same logic, so the app can be exercised end-to-end (Qt offscreen mode `QT_QPA_PLATFORM=offscreen` for automated tests).

### 9.2 On the bench (already done / to do)

Already verified (12 V only, no Teensy): all rails and the TP DC values in §2.4, idle current 0.08 A, nothing warm.

Next:

1. Fit the Teensy (12 V first, then USB). Upload firmware. Connect with the app, ARM.
    - expect TP7/TP10/TP8 = 1.50 V
    - expect TP9 = 1.50 V
    - expect amp outputs ≈ 0 V
2. **No hydrophone:** 25 kHz, AMP 200, POT 200 → check the waveforms in §3.6. Sweep 20–90 kHz: TP8 flat within ≈ 1 dB. Check AMP_A/B for MHz oscillation and burst-edge ringing.
3. **Hydrophone in air:** ≤ 10–20 Vpp, bursts. Waveforms clean, no ringing. U6/U7 only warm.
4. **Hydrophone in water** (with the RX side listening): a few Vpp at 0.5–1 m, tone bursts. The direct arrival comes at distance / 1480 m/s; the received level should rise toward 90 kHz.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| ARM / DISARM | Firmware state in which the Teensy drives the board (SPI/I2C active) / releases all pins |
| BTL | Bridge-tied load: two amps drive the transducer in antiphase, doubling the voltage swing |
| Code / D | DAC code (0..65535) / AD5245 wiper code (0..255) |
| Vpp_diff | Peak-to-peak voltage across the hydrophone terminals (AMP_A − AMP_B) |
| REF_1V5 / REF_3V | 1.5 V bias / 3.0 V reference from the REF2030 |
| TVR | Transmit voltage response of the hydrophone (dB re 1 µPa/V at 1 m) |

---

## Appendix A — Reference implementation

The two files below implement §4–§6. They were compiled against host stubs and driven by automated tests against a simulated Teensy:

- firmware waveform, burst, limit and ARM/DISARM checks passed
- the app ran against a pty-based fake device

**They have not been run on the physical board yet.** Use them as the starting point; keep the protocol and the safety logic.

### A.1 Firmware — tx_firmware.ino

`tx_firmware.ino`

```cpp
// =====================================================================
//  TX board bench-test firmware  —  Teensy 4.1
//  Underwater acoustic modem, transmitter board
//
//  Generates a sine wave on the DAC8830 (500 kHz update rate) and sets
//  the TX level through the AD5245 digital pot. Controlled over USB
//  serial with simple text commands (one per line), e.g. from the
//  Python UI (modem_ui.py) or the Arduino Serial Monitor.
//
//  SAFETY: the Teensy does not drive any board pin until ARM succeeds.
//  ARM first checks that the board's +3V3 rail is up (I2C pull-ups read
//  high), so a Teensy left on USB with the 12 V supply off never
//  back-powers the board through the DAC/pot input pins.
//
//  Commands (case-insensitive, answers start with OK / ERR / WARN / STATUS):
//    PING                       -> PONG TXFW 1.0
//    ARM                        -> check board power, init DAC + pot
//    DISARM                     -> stop, park DAC at 0 V, release all pins
//    FREQ <Hz>                  -> 100 .. 100000
//    AMP <mV peak at DAC>       -> 0 .. 1400 (around the 1.5 V mid-scale)
//    POT <code>                 -> 0 .. 255   (255 = minimum gain)
//    LIMIT <Vpp>                -> 1 .. 50, max allowed estimated output
//    MODE CONT
//    MODE BURST <on_us> <period_us>
//    START / STOP
//    STATUS
// =====================================================================
#include <SPI.h>
#include <Wire.h>
#include <math.h>

// ---------------- pins (TX schematic) ----------------
const int PIN_DAC_CS = 28;   // SPI1_CS (plain GPIO)
const int PIN_MOSI1  = 26;   // SPI1_MOSI
const int PIN_SCK1   = 27;   // SPI1_SCK
const int PIN_SCL2   = 24;   // I2C2_SCL (4.7k pull-up to board +3V3)
const int PIN_SDA2   = 25;   // I2C2_SDA (4.7k pull-up to board +3V3)
const int PIN_LED_TX = 3;    // LED_TX
const int PIN_LED_HB = 8;    // LED_HB (heartbeat)

// ---------------- hardware constants ----------------
const float    FS_HZ        = 500000.0f;  // DAC update rate
const float    TICK_US      = 2.0f;       // 1 / FS
const float    VREF_V       = 3.0f;       // REF2030 output
const uint16_t DAC_MID      = 32768;      // 1.5 V
const float    MAX_DAC_MVPK = 1400.0f;    // keep buffer away from 0 V / 3 V rails
const float    PA_GAIN      = 27.31f;     // power-amp differential gain
const float    POT_RAB      = 10000.0f;   // AD5245 end-to-end
const float    POT_RW       = 50.0f;      // wiper resistance (typ, 120 max); R_WA uses 2*R_W per datasheet
const float    R21_OHM      = 10000.0f;   // level-control input resistor
const uint8_t  POT_ADDR     = 0x2C;       // AD5245, AD0 = GND
const float    ABS_MAX_VPP  = 50.0f;      // S1n continuous rating
const float    THERMAL_FVPP = 2.3e6f;     // continuous f*Vpp guideline (OPA55x SOIC)
const int32_t  RAMP_SAMPLES = 50;         // 100 us amplitude ramp (burst edges)

// ---------------- sine table ----------------
static int16_t sineTab[1024];             // Q15

// ---------------- state shared with ISR ----------------
volatile uint32_t phase      = 0;
volatile uint32_t phaseStep  = 0;
volatile int32_t  ampSet     = 0;   // configured peak amplitude, DAC codes
volatile int32_t  ampCur     = 0;   // current (ramped) amplitude
volatile int32_t  rampStep   = 1;
volatile bool     running    = false;
volatile bool     burstMode  = false;
volatile uint32_t burstOnN   = 0;   // samples
volatile uint32_t burstPerN  = 0;   // samples
volatile uint32_t burstCnt   = 0;

// ---------------- settings (main loop) ----------------
bool     armed      = false;
float    freqHz     = 25000.0f;
float    ampMvpk    = 200.0f;
int      potCode    = 255;
float    limitVpp   = 20.0f;
uint32_t burstOnUs  = 1000;
uint32_t burstPerUs = 100000;

IntervalTimer dacTimer;
SPISettings   dacSpi(20000000, MSBFIRST, SPI_MODE0);  // DAC8830: 16-bit word, latched on SCLK rising edge

// =====================================================================
//  Low-level helpers
// =====================================================================
static inline void dacWrite(uint16_t code) {
  digitalWriteFast(PIN_DAC_CS, LOW);
  SPI1.transfer16(code);
  digitalWriteFast(PIN_DAC_CS, HIGH);    // DAC8830 updates on CS rising edge
}

bool potWrite(uint8_t d) {
  Wire2.beginTransmission(POT_ADDR);
  Wire2.write((uint8_t)0x00);            // instruction byte: normal write
  Wire2.write(d);
  return Wire2.endTransmission() == 0;
}

float potGain(int d) {                   // |gain| of the level-control stage
  float rwa = (256.0f - d) / 256.0f * POT_RAB + 2.0f * POT_RW;   // AD5245 eq. 2
  return rwa / R21_OHM;
}

float estVpp(float mvpk, int pot) {      // estimated differential Vpp across the transducer
  return 2.0f * (mvpk / 1000.0f) * potGain(pot) * PA_GAIN;
}

int32_t mvToCodes(float mvpk) {
  return (int32_t)lroundf((mvpk / 1000.0f) / VREF_V * 65536.0f);
}

void releasePins() {                     // high-impedance: nothing driven into the board
  pinMode(PIN_DAC_CS, INPUT);
  pinMode(PIN_MOSI1,  INPUT);
  pinMode(PIN_SCK1,   INPUT);
  pinMode(PIN_SCL2,   INPUT);
  pinMode(PIN_SDA2,   INPUT);
}

bool boardPowered() {                    // I2C pull-ups go to the board's +3V3
  pinMode(PIN_SCL2, INPUT);
  pinMode(PIN_SDA2, INPUT);
  delay(2);
  return digitalRead(PIN_SCL2) == HIGH && digitalRead(PIN_SDA2) == HIGH;
}

// =====================================================================
//  500 kHz sample ISR
// =====================================================================
void dacTick() {
  int32_t tgt = running ? ampSet : 0;
  if (running && burstMode) {
    if (burstCnt == 0) phase = 0;        // every burst starts at the same phase
    if (burstCnt >= burstOnN) tgt = 0;
    if (++burstCnt >= burstPerN) burstCnt = 0;
  }
  int32_t a = ampCur;
  if (a < tgt)      { a += rampStep; if (a > tgt) a = tgt; }
  else if (a > tgt) { a -= rampStep; if (a < tgt) a = tgt; }
  ampCur = a;

  phase += phaseStep;
  int32_t s    = sineTab[phase >> 22];
  int32_t code = (int32_t)DAC_MID + ((a * s) >> 15);
  if (code < 0) code = 0;
  if (code > 65535) code = 65535;
  dacWrite((uint16_t)code);
}

void applyFreq() {
  phaseStep = (uint32_t)llround((double)freqHz / FS_HZ * 4294967296.0);
}

void applyAmp() {
  int32_t c = mvToCodes(ampMvpk);
  int32_t r = c / RAMP_SAMPLES;
  rampStep  = r < 1 ? 1 : r;
  ampSet    = c;
}

void applyBurst() {
  uint32_t on  = burstOnUs / 2;
  uint32_t per = burstPerUs / 2;
  if (on < 1) on = 1;
  if (per <= on) per = on + 1;
  noInterrupts();
  burstOnN = on; burstPerN = per; burstCnt = 0;
  interrupts();
}

// =====================================================================
//  ARM / DISARM
// =====================================================================
String doArm() {
  if (armed) return "OK ARMED";
  if (!boardPowered()) { releasePins(); return "ERR NOPOWER (turn the 12 V supply on, then ARM)"; }

  Wire2.begin();
  Wire2.setClock(400000);
  if (!potWrite(255)) { releasePins(); return "ERR NOPOT (AD5245 did not answer on I2C)"; }

  pinMode(PIN_DAC_CS, OUTPUT);
  digitalWriteFast(PIN_DAC_CS, HIGH);
  SPI1.begin();
  SPI1.beginTransaction(dacSpi);         // SPI1 is used only by the DAC: keep it open
  delayMicroseconds(20);

  dacWrite(0);                           // shift register is not cleared at power-up
  for (uint32_t c = 0; c <= DAC_MID; c += 64) { dacWrite((uint16_t)c); delayMicroseconds(100); }

  potWrite((uint8_t)potCode);
  running = false; ampCur = 0; phase = 0;
  applyFreq(); applyAmp(); applyBurst();
  dacTimer.begin(dacTick, TICK_US);
  dacTimer.priority(32);
  armed = true;
  return "OK ARMED";
}

String doDisarm() {
  if (!armed) { releasePins(); return "OK DISARMED"; }
  running = false;
  delay(5);                              // let the amplitude ramp down to mid-scale
  dacTimer.end();
  for (int32_t c = DAC_MID; c >= 0; c -= 64) { dacWrite((uint16_t)c); delayMicroseconds(100); }
  dacWrite(0);
  potWrite(255);
  SPI1.endTransaction();
  SPI1.end();
  releasePins();
  digitalWriteFast(PIN_LED_TX, LOW);
  armed = false;
  return "OK DISARMED";
}

// =====================================================================
//  Commands
// =====================================================================
String statusLine() {
  String s = "STATUS armed=";
  s += armed ? 1 : 0;
  s += " run=";      s += running ? 1 : 0;
  s += " freq=";     s += (long)lroundf(freqHz);
  s += " amp=";      s += (long)lroundf(ampMvpk);
  s += " pot=";      s += potCode;
  s += " mode=";     s += burstMode ? "BURST" : "CONT";
  s += " on_us=";    s += burstOnUs;
  s += " per_us=";   s += burstPerUs;
  s += " est=";      s += String(estVpp(ampMvpk, potCode), 2);
  s += " limit=";    s += String(limitVpp, 1);
  s += " power=";    s += armed ? 1 : (boardPowered() ? 1 : 0);
  if (!armed) releasePins();
  return s;
}

String thermalWarning() {
  if (!burstMode && freqHz * estVpp(ampMvpk, potCode) > THERMAL_FVPP)
    return "WARN THERMAL continuous f*Vpp above ~2.3e6: power amps may overheat; use BURST or lower level";
  return "";
}

String handle(String line) {
  line.trim();
  if (line.length() == 0) return "";
  String up = line; up.toUpperCase();

  char buf[96];
  up.toCharArray(buf, sizeof(buf));
  char *cmd = strtok(buf, " \t");
  char *a1  = strtok(nullptr, " \t");
  char *a2  = strtok(nullptr, " \t");
  char *a3  = strtok(nullptr, " \t");
  String c(cmd ? cmd : "");

  if (c == "PING")   return "PONG TXFW 1.0";
  if (c == "STATUS") return statusLine();
  if (c == "ARM")    return doArm();
  if (c == "DISARM") return doDisarm();

  if (c == "FREQ") {
    if (!a1) return "ERR FREQ needs a value";
    float f = atof(a1);
    if (f < 100 || f > 100000) return "ERR FREQ out of range 100..100000";
    freqHz = f; applyFreq();
    return String("OK FREQ ") + String((long)lroundf(f));
  }
  if (c == "AMP") {
    if (!a1) return "ERR AMP needs a value";
    float m = atof(a1);
    if (m < 0 || m > MAX_DAC_MVPK) return "ERR AMP out of range 0..1400";
    if (estVpp(m, potCode) > limitVpp + 1e-3f) return "ERR LIMIT estimated output would exceed limit";
    ampMvpk = m; applyAmp();
    return String("OK AMP ") + String((long)lroundf(m));
  }
  if (c == "POT") {
    if (!a1) return "ERR POT needs a value";
    int d = atoi(a1);
    if (d < 0 || d > 255) return "ERR POT out of range 0..255";
    if (estVpp(ampMvpk, d) > limitVpp + 1e-3f) return "ERR LIMIT estimated output would exceed limit";
    if (armed && !potWrite((uint8_t)d)) return "ERR NOPOT (I2C write failed)";
    potCode = d;
    return String("OK POT ") + String(d);
  }
  if (c == "LIMIT") {
    if (!a1) return "ERR LIMIT needs a value";
    float v = atof(a1);
    if (v < 1 || v > ABS_MAX_VPP) return "ERR LIMIT out of range 1..50";
    if (estVpp(ampMvpk, potCode) > v + 1e-3f) return "ERR LIMIT current settings already exceed it";
    limitVpp = v;
    return String("OK LIMIT ") + String(v, 1);
  }
  if (c == "MODE") {
    String m(a1 ? a1 : "");
    if (m == "CONT") { burstMode = false; return "OK MODE CONT"; }
    if (m == "BURST") {
      if (!a2 || !a3) return "ERR MODE BURST needs <on_us> <period_us>";
      long on = atol(a2), per = atol(a3);
      if (on < 10 || per <= on || per > 10000000) return "ERR MODE BURST needs 10 <= on_us < period_us <= 10000000";
      burstOnUs = on; burstPerUs = per; applyBurst(); burstMode = true;
      return String("OK MODE BURST ") + String(on) + " " + String(per);
    }
    return "ERR MODE needs CONT or BURST";
  }
  if (c == "START") {
    if (!armed) return "ERR NOTARMED";
    if (estVpp(ampMvpk, potCode) > limitVpp + 1e-3f) return "ERR LIMIT";
    if (burstMode) applyBurst();
    running = true;
    digitalWriteFast(PIN_LED_TX, HIGH);
    String w = thermalWarning();
    return w.length() ? String("OK START\n") + w : String("OK START");
  }
  if (c == "STOP") {
    running = false;
    digitalWriteFast(PIN_LED_TX, LOW);
    return "OK STOP";
  }
  return String("ERR unknown command: ") + c;
}

// =====================================================================
void setup() {
  releasePins();                         // never drive the board before ARM
  pinMode(PIN_LED_TX, OUTPUT); digitalWriteFast(PIN_LED_TX, LOW);
  pinMode(PIN_LED_HB, OUTPUT); digitalWriteFast(PIN_LED_HB, LOW);
  for (int i = 0; i < 1024; i++)
    sineTab[i] = (int16_t)lroundf(32767.0f * sinf(2.0f * (float)M_PI * i / 1024.0f));
  Serial.begin(115200);
}

void loop() {
  static String   rx;
  static uint32_t lastHb = 0;

  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (rx.length()) {
        String r = handle(rx);
        if (r.length()) Serial.println(r);
        rx = "";
      }
    } else if (rx.length() < 90) {
      rx += ch;
    }
  }

  uint32_t now = millis();                // heartbeat: slow when idle, fast when armed
  if (now - lastHb >= (armed ? 125u : 500u)) {
    lastHb = now;
    digitalToggleFast(PIN_LED_HB);
  }
}
```

### A.2 Desktop app — modem_ui.py

`modem_ui.py`

```python
"""
Acoustic modem bench UI  —  TX tab (RX tab to be added later)

Talks to the TX board's Teensy 4.1 (tx_firmware.ino) over USB serial.
Run:   pip install -r requirements.txt
       python modem_ui.py
"""
import sys

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QIODevice
from PySide6.QtGui import QFont
from PySide6.QtSerialPort import QSerialPort, QSerialPortInfo
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QGroupBox, QLabel, QPushButton, QComboBox, QSpinBox, QDoubleSpinBox, QSlider,
    QRadioButton, QButtonGroup, QTabWidget, QPlainTextEdit, QMessageBox,
)

# ---------------------------------------------------------------------------
# Hardware model (keep in sync with tx_firmware.ino)
# ---------------------------------------------------------------------------
TEENSY_VID = 0x16C0
PA_GAIN = 27.31          # power-amp differential gain
POT_RAB = 10000.0        # AD5245 end-to-end resistance
POT_RW = 50.0            # wiper resistance (typ, 120 max); R_WA uses 2*R_W
R21 = 10000.0            # level-control input resistor
THERMAL_FVPP = 2.3e6     # continuous f*Vpp guideline for the OPA55x (SOIC)
MAX_DAC_MVPK = 1400


def pot_gain(code: int) -> float:
    """Level-control stage gain magnitude for an AD5245 code (255 = minimum)."""
    rwa = (256 - code) / 256 * POT_RAB + 2 * POT_RW   # AD5245 eq. 2
    return rwa / R21


def est_vpp(dac_mvpk: float, pot_code: int) -> float:
    """Estimated differential Vpp across the transducer."""
    return 2.0 * (dac_mvpk / 1000.0) * pot_gain(pot_code) * PA_GAIN


def parse_status(line: str) -> dict:
    out = {}
    for tok in line.split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Serial link
# ---------------------------------------------------------------------------
class SerialLink(QObject):
    line_received = Signal(str)
    line_sent = Signal(str)
    state_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.port = QSerialPort()
        self.port.readyRead.connect(self._on_ready_read)
        self.port.errorOccurred.connect(self._on_error)
        self._buf = b""

    def is_open(self) -> bool:
        return self.port.isOpen()

    def open(self, name: str) -> bool:
        self.port.setPortName(name)
        self.port.setBaudRate(115200)        # ignored by Teensy USB serial, harmless
        if not self.port.open(QIODevice.ReadWrite):
            return False
        self.port.setDataTerminalReady(True)
        self._buf = b""
        self.state_changed.emit(True)
        return True

    def close(self):
        if self.port.isOpen():
            self.port.close()
        self.state_changed.emit(False)

    def send(self, text: str):
        if not self.port.isOpen():
            return
        self.port.write((text + "\n").encode())
        self.line_sent.emit(text)

    def _on_ready_read(self):
        self._buf += bytes(self.port.readAll())
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            line = raw.decode(errors="replace").strip()
            if line:
                self.line_received.emit(line)

    def _on_error(self, err):
        if err == QSerialPort.SerialPortError.ResourceError:   # e.g. USB unplugged
            self.close()


# ---------------------------------------------------------------------------
# Small helper: spinbox + slider kept in sync
# ---------------------------------------------------------------------------
class SpinSlider(QWidget):
    valueChanged = Signal(int)

    def __init__(self, lo, hi, step, value, suffix="", slider_lo=None, slider_hi=None):
        super().__init__()
        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(step)
        self.spin.setSuffix(suffix)
        self.spin.setValue(value)
        self.spin.setMinimumWidth(110)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(slider_lo if slider_lo is not None else lo,
                             slider_hi if slider_hi is not None else hi)
        self.slider.setSingleStep(step)
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

    def value(self):
        return self.spin.value()

    def setValue(self, v):
        self.spin.setValue(v)


# ---------------------------------------------------------------------------
# TX tab
# ---------------------------------------------------------------------------
class TxTab(QWidget):
    def __init__(self, link: SerialLink):
        super().__init__()
        self.link = link
        self.armed = False
        self.running = False
        self.sent = {}                    # last values sent to the firmware

        root = QVBoxLayout(self)

        # --- board power ------------------------------------------------
        g_pwr = QGroupBox("Board")
        l_pwr = QHBoxLayout(g_pwr)
        self.lbl_power = QLabel("Not connected")
        self.btn_arm = QPushButton("Arm (12 V is ON)")
        self.btn_disarm = QPushButton("Disarm")
        l_pwr.addWidget(self.lbl_power, 1)
        l_pwr.addWidget(self.btn_arm)
        l_pwr.addWidget(self.btn_disarm)
        root.addWidget(g_pwr)

        # --- signal -----------------------------------------------------
        g_sig = QGroupBox("Signal")
        l_sig = QGridLayout(g_sig)
        self.freq = SpinSlider(100, 100000, 500, 25000, " Hz", 20000, 90000)
        l_sig.addWidget(QLabel("Frequency"), 0, 0)
        l_sig.addWidget(self.freq, 0, 1)
        presets = QHBoxLayout()
        for f in (20000, 25000, 40000, 60000, 80000, 90000):
            b = QPushButton(f"{f // 1000} kHz")
            b.clicked.connect(lambda _=False, f=f: self.freq.setValue(f))
            presets.addWidget(b)
        l_sig.addLayout(presets, 1, 1)

        self.rb_cont = QRadioButton("Continuous")
        self.rb_burst = QRadioButton("Burst")
        self.rb_cont.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_cont)
        grp.addButton(self.rb_burst)
        self.burst_on = QDoubleSpinBox()
        self.burst_on.setRange(0.02, 1000.0)
        self.burst_on.setDecimals(2)
        self.burst_on.setValue(1.0)
        self.burst_on.setSuffix(" ms on")
        self.burst_per = QDoubleSpinBox()
        self.burst_per.setRange(0.1, 10000.0)
        self.burst_per.setDecimals(1)
        self.burst_per.setValue(100.0)
        self.burst_per.setSuffix(" ms period")
        mode = QHBoxLayout()
        for w in (self.rb_cont, self.rb_burst, self.burst_on, self.burst_per):
            mode.addWidget(w)
        mode.addStretch(1)
        l_sig.addWidget(QLabel("Mode"), 2, 0)
        l_sig.addLayout(mode, 2, 1)
        root.addWidget(g_sig)

        # --- level ------------------------------------------------------
        g_lvl = QGroupBox("Level")
        l_lvl = QGridLayout(g_lvl)
        self.amp = SpinSlider(0, MAX_DAC_MVPK, 10, 200, " mV pk")
        l_lvl.addWidget(QLabel("DAC amplitude"), 0, 0)
        l_lvl.addWidget(self.amp, 0, 1)
        # slider position 0..255 maps to pot code 255..0 (right = more gain)
        self.gain = SpinSlider(0, 255, 1, 0, "")
        self.gain.spin.setPrefix("step ")
        self.lbl_gain = QLabel()
        l_lvl.addWidget(QLabel("Level gain (AD5245)"), 1, 0)
        l_lvl.addWidget(self.gain, 1, 1)
        l_lvl.addWidget(self.lbl_gain, 1, 2)
        self.limit = QDoubleSpinBox()
        self.limit.setRange(1.0, 50.0)
        self.limit.setDecimals(1)
        self.limit.setValue(20.0)
        self.limit.setSuffix(" Vpp max")
        l_lvl.addWidget(QLabel("Safety limit"), 2, 0)
        hl = QHBoxLayout()
        hl.addWidget(self.limit)
        hl.addWidget(QLabel("(keep ≤ 20 Vpp in air; S1n max 50 Vpp continuous)"))
        hl.addStretch(1)
        l_lvl.addLayout(hl, 2, 1)
        self.lbl_est = QLabel()
        f = QFont()
        f.setPointSize(16)
        f.setBold(True)
        self.lbl_est.setFont(f)
        l_lvl.addWidget(QLabel("Estimated output"), 3, 0)
        l_lvl.addWidget(self.lbl_est, 3, 1)
        root.addWidget(g_lvl)

        # --- run --------------------------------------------------------
        g_run = QGroupBox("Run")
        l_run = QHBoxLayout(g_run)
        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop")
        self.lbl_run = QLabel("Stopped")
        l_run.addWidget(self.btn_start)
        l_run.addWidget(self.btn_stop)
        l_run.addWidget(self.lbl_run, 1)
        root.addWidget(g_run)

        self.lbl_warn = QLabel("")
        self.lbl_warn.setWordWrap(True)
        root.addWidget(self.lbl_warn)
        root.addStretch(1)

        # --- wiring -----------------------------------------------------
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(120)
        self.debounce.timeout.connect(self.push_changes)
        for w in (self.freq, self.amp, self.gain):
            w.valueChanged.connect(self.on_edit)
        for w in (self.limit, self.burst_on, self.burst_per):
            w.valueChanged.connect(self.on_edit)
        self.rb_cont.toggled.connect(self.on_edit)

        self.btn_arm.clicked.connect(self.on_arm)
        self.btn_disarm.clicked.connect(lambda: self.link.send("DISARM"))
        self.btn_start.clicked.connect(self.on_start)
        self.btn_stop.clicked.connect(lambda: self.link.send("STOP"))
        self.link.line_received.connect(self.on_line)
        self.link.state_changed.connect(self.on_link_state)

        self.refresh_labels()
        self.update_buttons()

    # ---- values ---------------------------------------------------------
    def pot_code(self) -> int:
        return 255 - self.gain.value()

    def mode_cmd(self) -> str:
        if self.rb_cont.isChecked():
            return "MODE CONT"
        on_us = int(round(self.burst_on.value() * 1000))
        per_us = int(round(self.burst_per.value() * 1000))
        return f"MODE BURST {on_us} {per_us}"

    def estimate(self) -> float:
        return est_vpp(self.amp.value(), self.pot_code())

    # ---- UI updates -----------------------------------------------------
    def refresh_labels(self):
        g = pot_gain(self.pot_code())
        self.lbl_gain.setText(f"pot code {self.pot_code():3d}  →  ×{g:.3f}")
        est = self.estimate()
        over = est > self.limit.value() + 1e-6
        self.lbl_est.setText(f"≈ {est:.1f} Vpp across the transducer")
        self.lbl_est.setStyleSheet("color:#c0392b;" if over else "color:#1e8449;")
        msgs = []
        if over:
            msgs.append("<b style='color:#c0392b'>Above the safety limit — lower the amplitude or gain "
                        "(or raise the limit). Nothing is sent until it fits.</b>")
        if self.rb_cont.isChecked() and self.freq.value() * est > THERMAL_FVPP:
            msgs.append("<span style='color:#b9770e'>Continuous at this level and frequency may overheat "
                        "the power amps (f×Vpp above ~2.3e6). Use Burst, or keep runs short.</span>")
        if self.rb_burst.isChecked() and self.burst_per.value() <= self.burst_on.value():
            msgs.append("<span style='color:#c0392b'>Burst period must be longer than the on-time.</span>")
        self.lbl_warn.setText("<br>".join(msgs))
        self.update_buttons()

    def update_buttons(self):
        conn = self.link.is_open()
        ok_level = self.estimate() <= self.limit.value() + 1e-6
        self.btn_arm.setEnabled(conn and not self.armed)
        self.btn_disarm.setEnabled(conn and self.armed)
        self.btn_start.setEnabled(conn and self.armed and ok_level)
        self.btn_stop.setEnabled(conn and self.armed)

    # ---- talking to the firmware ---------------------------------------
    def on_edit(self, *_):
        self.refresh_labels()
        self.debounce.start()

    def push_changes(self, force=False):
        if not self.link.is_open():
            return
        if self.estimate() > self.limit.value() + 1e-6:
            return                          # never send a level above the limit
        if self.rb_burst.isChecked() and self.burst_per.value() <= self.burst_on.value():
            return
        want = {
            "FREQ": f"FREQ {self.freq.value()}",
            "AMP": f"AMP {self.amp.value()}",
            "POT": f"POT {self.pot_code()}",
            "LIMIT": f"LIMIT {self.limit.value():.1f}",
            "MODE": self.mode_cmd(),
        }
        # Order so the firmware never sees an intermediate level above its limit:
        # anything that lowers the output goes first, the limit is raised first / lowered last.
        old_pot = self.sent.get("pot", self.pot_code())
        if est_vpp(self.amp.value(), old_pot) <= self.limit.value():
            order = ["LIMIT", "FREQ", "AMP", "POT", "MODE"]
        else:
            order = ["LIMIT", "FREQ", "POT", "AMP", "MODE"]
        if self.limit.value() < self.sent.get("limit", 0):
            order.remove("LIMIT")
            order.append("LIMIT")
        for key in order:
            cmd = want[key]
            if force or self.sent.get(key) != cmd:
                self.link.send(cmd)
                self.sent[key] = cmd
        self.sent["amp"] = self.amp.value()
        self.sent["pot"] = self.pot_code()
        self.sent["limit"] = self.limit.value()

    def on_link_state(self, is_open: bool):
        self.armed = False
        self.running = False
        self.sent = {}
        if is_open:
            self.lbl_power.setText("Connected — checking board…")
            self.link.send("PING")
            self.link.send("POT 255")       # start from minimum gain
            self.sent["pot"] = 255
            self.push_changes(force=True)
            self.link.send("STATUS")
        else:
            self.lbl_power.setText("Not connected")
            self.lbl_run.setText("Stopped")
        self.update_buttons()

    def on_arm(self):
        r = QMessageBox.question(
            self, "Arm the TX board",
            "Is the TX board's 12 V supply switched ON?\n\n"
            "The Teensy only starts driving the DAC and pot after this. "
            "Arming with the board unpowered is refused by the firmware anyway.")
        if r == QMessageBox.Yes:
            self.link.send("ARM")

    def on_start(self):
        self.push_changes()
        self.link.send("START")

    def on_line(self, line: str):
        if line.startswith("STATUS"):
            st = parse_status(line)
            self.armed = st.get("armed") == "1"
            self.running = st.get("run") == "1"
            if self.armed:
                self.lbl_power.setText("<b style='color:#1e8449'>ARMED</b> — board powered, DAC and pot active")
            elif st.get("power") == "1":
                self.lbl_power.setText("Board powered — not armed (press Arm)")
            else:
                self.lbl_power.setText("<span style='color:#7f8c8d'>Board 12 V not detected — "
                                       "Teensy pins are released</span>")
            self.lbl_run.setText("<b style='color:#c0392b'>TRANSMITTING</b>" if self.running else "Stopped")
            self.update_buttons()
        elif line.startswith("ERR"):
            self.lbl_warn.setText(f"<b style='color:#c0392b'>Teensy: {line}</b>")
            self.sent = {}                  # resend everything on the next change
            self.link.send("STATUS")
        elif line.startswith("WARN"):
            self.lbl_warn.setText(f"<span style='color:#b9770e'>Teensy: {line}</span>")
        elif line.startswith(("OK ARMED", "OK DISARMED", "OK START", "OK STOP")):
            self.link.send("STATUS")


# ---------------------------------------------------------------------------
# RX tab placeholder
# ---------------------------------------------------------------------------
class RxTab(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lbl = QLabel("RX controls (gain switch, AGC pot) and live plotting will go here.")
        lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(lbl)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Acoustic Modem Bench")
        self.resize(900, 760)
        self.link = SerialLink()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.cb_port = QComboBox()
        self.cb_port.setMinimumWidth(320)
        self.btn_refresh = QPushButton("Refresh")
        self.btn_connect = QPushButton("Connect")
        top.addWidget(QLabel("Teensy port"))
        top.addWidget(self.cb_port, 1)
        top.addWidget(self.btn_refresh)
        top.addWidget(self.btn_connect)
        root.addLayout(top)

        self.tabs = QTabWidget()
        self.tx = TxTab(self.link)
        self.tabs.addTab(self.tx, "TX")
        self.tabs.addTab(RxTab(), "RX")
        root.addWidget(self.tabs, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1000)
        self.log.setFixedHeight(160)
        self.log.setFont(QFont("Consolas", 9))
        root.addWidget(QLabel("Serial log"))
        root.addWidget(self.log)

        self.btn_refresh.clicked.connect(self.refresh_ports)
        self.btn_connect.clicked.connect(self.toggle_connect)
        self._last_status = ""
        self.link.line_received.connect(self.on_received)
        self.link.line_sent.connect(self.on_sent)
        self.link.state_changed.connect(self.on_state)

        self.poll = QTimer(self)
        self.poll.setInterval(1000)
        self.poll.timeout.connect(lambda: self.link.send("STATUS"))
        self.refresh_ports()

    def on_received(self, s):
        if s.startswith("STATUS"):          # only log status lines when something changed
            if s == self._last_status:
                return
            self._last_status = s
        self.log.appendPlainText("← " + s)

    def on_sent(self, s):
        if s != "STATUS":                  # keep the log readable
            self.log.appendPlainText("→ " + s)

    def refresh_ports(self):
        self.cb_port.clear()
        best = -1
        for i, p in enumerate(QSerialPortInfo.availablePorts()):
            is_teensy = p.hasVendorIdentifier() and p.vendorIdentifier() == TEENSY_VID
            label = f"{p.portName()}  —  {p.description() or 'serial port'}"
            if is_teensy:
                label += "  (Teensy)"
                best = i
            self.cb_port.addItem(label, p.portName())
        if best >= 0:
            self.cb_port.setCurrentIndex(best)

    def toggle_connect(self):
        if self.link.is_open():
            if self.tx.armed:
                self.link.send("DISARM")
            self.link.close()
            return
        name = self.cb_port.currentData()
        if not name:
            QMessageBox.warning(self, "No port", "No serial port selected.")
            return
        if not self.link.open(name):
            QMessageBox.warning(self, "Could not open port",
                                f"{name}: {self.link.port.errorString()}\n"
                                "Close the Arduino Serial Monitor if it is open.")

    def on_state(self, is_open):
        self.btn_connect.setText("Disconnect" if is_open else "Connect")
        self.cb_port.setEnabled(not is_open)
        if is_open:
            self.poll.start()
        else:
            self.poll.stop()

    def closeEvent(self, ev):
        if self.link.is_open():
            if self.tx.armed:
                self.link.send("DISARM")    # park the DAC at 0 V and release the pins
                self.link.port.waitForBytesWritten(200)
            self.link.close()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

### A.3 requirements.txt

```text
PySide6>=6.5
```

---

## Appendix B — Host test harness (firmware logic on a PC)

Compile with `g++ -std=c++17 -I. -o sim sim_main.cpp` after copying `tx_firmware.ino` to `fw.cpp` in the same folder (the stub `SPI.h` and `Wire.h` just `#include "Arduino.h"`). The stub records every DAC word and every pot write so tests can check the waveform, the limits and the ARM/DISARM sequence.

### B.1 Arduino.h (stub)

`Arduino.h`

```cpp
#pragma once
#include <string>
#include <vector>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <cctype>
#include <deque>
#include <functional>
#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define MSBFIRST 1
#define SPI_MODE0 0
class String {
 public:
  std::string s;
  String() {}
  String(const char* c): s(c?c:"") {}
  String(const std::string& x): s(x) {}
  String(int v){ s=std::to_string(v);} String(long v){s=std::to_string(v);} String(unsigned long v){s=std::to_string(v);}
  String(unsigned int v){s=std::to_string(v);}
  String(double v, unsigned char d=2){ char b[64]; snprintf(b,64,"%.*f",d,v); s=b; }
  String(float v, unsigned char d=2){ char b[64]; snprintf(b,64,"%.*f",d,(double)v); s=b; }
  unsigned int length() const { return s.size(); }
  void trim(){ size_t a=s.find_first_not_of(" \t\r\n"); if(a==std::string::npos){s="";return;} size_t b=s.find_last_not_of(" \t\r\n"); s=s.substr(a,b-a+1);}
  void toUpperCase(){ for(auto&c:s) c=toupper(c);}
  void toCharArray(char* b, unsigned n) const { strncpy(b,s.c_str(),n-1); b[n-1]=0; }
  String& operator+=(const String& o){ s+=o.s; return *this;}
  String& operator+=(const char* o){ s+=o; return *this;}
  String& operator+=(char c){ s+=c; return *this;}
  String& operator+=(int v){ s+=std::to_string(v); return *this;}
  String& operator+=(long v){ s+=std::to_string(v); return *this;}
  String& operator+=(unsigned long v){ s+=std::to_string(v); return *this;}
  String& operator+=(unsigned int v){ s+=std::to_string(v); return *this;}
  bool operator==(const char* o) const { return s==o; }
  bool operator==(const String& o) const { return s==o.s; }
  const char* c_str() const { return s.c_str(); }
};
inline String operator+(const String& a, const String& b){ return String(a.s+b.s);}
inline String operator+(const String& a, const char* b){ return String(a.s+b);}
extern std::vector<uint16_t> g_dac; extern int g_pins[64]; extern int g_pinmode[64]; extern bool g_power; extern std::vector<int> g_pot;
inline void pinMode(int p,int m){ g_pinmode[p]=m; }
inline void digitalWriteFast(int p,int v){ g_pins[p]=v; }
inline void digitalToggleFast(int p){ g_pins[p]^=1; }
inline int digitalRead(int p){ return g_power?HIGH:LOW; }
inline void delay(unsigned){} inline void delayMicroseconds(unsigned){}
extern unsigned long g_ms; inline unsigned long millis(){ return g_ms; }
inline void noInterrupts(){} inline void interrupts(){}
struct SPISettings{ SPISettings(uint32_t,int,int){} };
struct SPIClass{ bool open=false; void begin(){} void end(){} void beginTransaction(SPISettings){open=true;} void endTransaction(){open=false;}
  uint16_t transfer16(uint16_t v){ if(g_pins[28]!=LOW){printf("CS not low!\n");} g_dac.push_back(v); return 0;} };
extern SPIClass SPI1;
struct TwoWire{ std::vector<uint8_t> b; void begin(){} void setClock(uint32_t){} void beginTransmission(uint8_t a){b.clear(); b.push_back(a);} void write(uint8_t v){b.push_back(v);} int endTransmission(){ if(!g_power) return 2; if(b.size()==3) g_pot.push_back(b[2]); return 0;} };
extern TwoWire Wire2;
struct IntervalTimer{ std::function<void()> f; bool on=false; void begin(void(*fn)(), float){f=fn; on=true;} void end(){on=false;} void priority(int){} };
struct SerialC{ std::deque<char> in; std::vector<std::string> out; void begin(int){} int available(){return in.size();} int read(){int c=in.front(); in.pop_front(); return c;} void println(const String& s){ out.push_back(s.s);} };
extern SerialC Serial;
```

### B.2 sim_main.cpp (unit-style checks)

`sim_main.cpp`

```cpp
#include <algorithm>
#include "Arduino.h"
std::vector<uint16_t> g_dac; int g_pins[64]; int g_pinmode[64]; bool g_power=true; std::vector<int> g_pot; unsigned long g_ms=0;
SPIClass SPI1; TwoWire Wire2; SerialC Serial;
#include "fw.cpp"
std::string cmd(const char* c){ Serial.out.clear(); for(const char* p=c;*p;p++) Serial.in.push_back(*p); Serial.in.push_back('\n'); loop(); std::string r; for(auto&s:Serial.out){ r+=s; r+=" | ";} return r; }
int main(){
  setup();
  g_power=false; printf("ARM(no power): %s\n", cmd("ARM").c_str());
  printf("pins after nopower: CS mode=%d\n", g_pinmode[28]);
  g_power=true;
  printf("%s\n", cmd("PING").c_str());
  printf("%s\n", cmd("arm").c_str());
  printf("pot writes so far:"); for(int p: g_pot) printf(" %d", p); printf("\n");
  printf("DAC ramp writes: %zu, last=%u\n", g_dac.size(), g_dac.back());
  printf("%s\n", cmd("FREQ 25000").c_str());
  printf("%s\n", cmd("AMP 200").c_str());
  printf("%s\n", cmd("POT 200").c_str());
  printf("%s\n", cmd("POT 0").c_str());
  printf("%s\n", cmd("LIMIT 60").c_str());
  printf("%s\n", cmd("STATUS").c_str());
  // run ISR not running: should stay at mid
  g_dac.clear(); for(int i=0;i<100;i++) dacTick(); printf("idle min/max %u/%u\n", *std::min_element(g_dac.begin(),g_dac.end()), *std::max_element(g_dac.begin(),g_dac.end()));
  printf("%s\n", cmd("START").c_str());
  g_dac.clear(); for(int i=0;i<5000;i++) dacTick();
  // after ramp, analyze last 4000 samples
  int mn=65535,mx=0; for(size_t i=1000;i<g_dac.size();i++){ mn=std::min<int>(mn,g_dac[i]); mx=std::max<int>(mx,g_dac[i]); }
  // count rising crossings of mid in 4000 samples (8ms) -> expect 200 cycles
  int cr=0; for(size_t i=1001;i<g_dac.size();i++) if(g_dac[i-1]<32768 && g_dac[i]>=32768) cr++;
  printf("cont: min=%d max=%d  (expect ~%d..%d) crossings=%d (expect 200)\n", mn, mx, 32768-4369, 32768+4369, cr);
  printf("mV pk measured = %.1f\n", (mx-32768)/65536.0*3000);
  printf("%s\n", cmd("MODE BURST 1000 10000").c_str());
  g_dac.clear(); for(int i=0;i<20000;i++) dacTick(); // 40 ms -> 4 bursts
  int active=0; for(auto v: g_dac) if (abs((int)v-32768)>50) active++;
  printf("burst: active samples=%d of 20000 (expect ~4*500=2000)\n", active);
  printf("%s\n", cmd("FREQ 90000").c_str());
  printf("%s\n", cmd("MODE CONT").c_str());
  printf("%s\n", cmd("LIMIT 50").c_str());
  printf("%s\n", cmd("AMP 900").c_str());
  printf("%s\n", cmd("START").c_str());
  printf("%s\n", cmd("STOP").c_str());
  g_dac.clear(); for(int i=0;i<200;i++) dacTick(); printf("after stop last=%u (mid 32768)\n", g_dac.back());
  printf("%s\n", cmd("AMP 1500").c_str());
  printf("%s\n", cmd("BOGUS").c_str());
  printf("%s\n", cmd("DISARM").c_str());
  printf("after disarm last DAC=%u, CS mode=%d MOSI mode=%d, last pot=%d\n", g_dac.back(), g_pinmode[28], g_pinmode[26], g_pot.back());
  printf("%s\n", cmd("START").c_str());
}
```

Expected output (abridged): `ERR NOPOWER` with no power; `OK ARMED`; continuous 25 kHz → min 28399 / max 37136, 200 crossings; burst → ≈2000 active samples of 20000; `WARN THERMAL` at 90 kHz / ≈49.6 Vpp continuous; `ERR AMP out of range`; `OK DISARMED` with last DAC word 0 and pot 255.
