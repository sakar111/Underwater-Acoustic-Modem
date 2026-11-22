# RX Board Bench Software — Build Specification

| | |
|---|---|
| **Project** | Underwater Acoustic Modem (Teensy 4.1-based; inspired by TUHH "ahoi") |
| **Author** | Sakar Pathak (UNR, advisor Prof. Hao Xu) |
| **Purpose** | Complete, self-contained hand-off to an AI coding agent (Claude Code in VS Code) to build the **RX bench firmware** and the **RX tab** of the existing Windows desktop app (`modem_ui.py`, built from the TX spec). |
| **Companion document** | *TX Board Bench Software — Build Specification* v1.0 (16 Nov 2026), project file `claude/tx-software-build-spec.md`. This RX spec uses the same structure, conventions and protocol style. |
| **Version** | 1.0 — 18 Nov 2026 |
| **Hardware status** | RX PCB assembled and powered (12 V only). Rails and bias points verified. The 13–15 MHz oscillation of the gain stage U4 was fixed with a 1 nF capacitor stacked on R15 (bring-up log, 13 Nov 2026). Teensy not yet soldered/flashed. Aquarian AS-1 hydrophone available. |
| **Verification basis** | Pins and nets: the final RX schematic (Altium `RX_Board.SchDoc`) as validated on 13 Oct 2026 (designator list and pin map recorded in project file `claude/pcb-schematic-capture-reference.md`, section "RX FINAL SCHEMATIC VALIDATION"). Chip facts: TI ADC121S101 (SNAS304, Rev G text), ADI AD5245 Rev C, TI TS5A3166, TI SN74LVC1G125, PJRC Teensy 4.1 SPI library source. Items that could not be fully confirmed from a datasheet are marked **VERIFY AT BRING-UP**, with a test that settles them. |

---

## 0. Read this first (instructions for the coding agent)

1. **Everything you need about the RX hardware is in this document.** Pin numbers, nets, chip protocols, gains and safety rules were checked against the final RX schematic and the datasheets. Do not invent pins, addresses or gains. If something you need is missing, stop and ask the user.
2. **You already have the TX side** (TX firmware + `modem_ui.py` with a TX tab and an RX placeholder). Keep the TX protocol and safety logic unchanged, except for the small, optional SYNC addition in §7 / Appendix C.
3. **Deliverables:**
    - **D1 — RX firmware** for the RX board's Teensy 4.1 (`rx_firmware/rx_firmware.ino`, Teensyduino).
    - **D2 — App changes:**
        - the RX tab replaces the placeholder;
        - the app holds **two independent serial connections** (TX board and RX board, each its own Teensy and COM port) and can use both at the same time.
    - **D3 — Tests:**
        - a host (PC) build of the RX firmware logic with stubbed Teensy APIs;
        - a **fake RX device** that speaks the RX protocol, so the app can be tested without hardware (§9.1, Appendix B).
    - **D4 (optional)** — TX firmware v1.1: a SYNC output pulse for the TX→RX delay measurement (§7, Appendix C).
4. **Safety rules in §4 are hard requirements.** Implement them in the firmware (the final authority) and mirror them in the app.
5. **No reference implementation is attached.** The code snippets in this document are normative where they say so (frame format, CRC, ISR core, gain equations). The protocol, frame format and ISR design were checked by compiling a firmware prototype against host stubs and parsing its output in Python (see Appendix B for the scenarios and expected results).
6. **Where this document and the TX spec's §7 ("RX board — summary") differ, this document wins.** That summary was written before the RX details were settled. Corrections:
    - **SPI mode 0** (not mode 2/3, §3.2);
    - VGA range **×1.47–×68 typ** (not ×1.9–×180, §3.5);
    - RX_GAIN_SEL = **pin 10**;
    - RX power detection via the **AD5245 ACK + ADC self-test** (§4.3).
7. **Target environment:** same as TX — Windows 10/11, Python 3.10+, PySide6 ≥ 6.5, **pyqtgraph** (now required), NumPy; Teensyduino; Teensy 4.1 at 600 MHz.

---

## 1. Project goal and context

- Two separate boards, each with its own Teensy 4.1 on its own USB port. **No electrical link between the boards** in normal use; they talk acoustically through water. An optional sync wire is allowed for bench measurements (§7).
- **RX chain:** AS-1 hydrophone → JFET LNA → switchable-gain stage → 20–90 kHz band-pass → variable-gain amplifier (digital pot) → 12-bit ADC at **400 kSPS** → Teensy → USB → PC.
- **This milestone is bench bring-up and water tests:**
    - look at the received signal live (time, FFT, spectrogram);
    - set the gain safely;
    - capture and save data;
    - measure received level, frequency and the TX→RX acoustic delay.
- **Later milestones** (design for them, do not build now): demodulation (BPSK; ≥ 10 kbit/s requirement, 25 kbit/s goal), automatic gain control from a packet preamble, RX digital equalisation.

---

## 2. RX hardware overview

### 2.1 Signal chain (short — analog details only where software needs them)

```
AS-1 hydrophone ─ J2 ─ R14 100Ω ─ Q1 LSK170A JFET LNA (≈ ×9) ─ LNA_OUT (TP6)
                                                                  │
PA-4 preamp (optional, P1) ─ R37 330Ω ─ PA4_OUT ─────────────┐    │
                                                   P2 jumper (front-end select, manual) → PRE_OUT
                                                                  ▼
            C19 6.8 nF / R12 10k → U4 ADA4807-1 gain stage ─ GAIN_OUT (TP5)
                       HIGH ×58.3 / LOW ×7.10, selected by U6 TS5A3166 ← RX_GAIN_SEL (Teensy pin 10)
                                                                  ▼
            U5 OPA4322: 4th-order Butterworth HP 20 kHz + 4th-order LP 90 kHz ─ BP_OUT (TP7)
                                                                  ▼
            C39 6.8 nF / R34 10k → U8 OPA365 VGA, feedback = U7 AD5245 (I2C2) ─ AMP_OUT (TP8)
                       ×1.47 … ×68 (typ)
                                                                  ▼
            R32 100Ω / C43 3.3 nF → U10 ADC121S101 (VA = 5.0 V) ─ SDATA → U9 SN74LVC1G125 → Teensy pin 39
```

- **Every stage is biased at 2.5 V** (net `2V5`, AD8031 buffer U3). With no signal, AMP_OUT ≈ 2.50 V, which is ADC mid-scale (code ≈ 2048).
- The front-end jumper **P2 is manual**: the firmware cannot tell whether the JFET LNA or the PA-4 is in use. The app has a setting for it (§6.4).

### 2.2 Power

| Rail / net | Source | Value | Test point | Notes |
|---|---|---|---|---|
| VIN | J1 12 V → D1 SS34 → D2 SMAJ15A | ≈ 11.6 V | TP3 | Also feeds the PA-4 through P1 |
| 5V25 | U1 TPS62120 buck | 5.28 V | TP1 | |
| 5V | U2 TPS73150 LDO (via R38 0 Ω link) | 5.00 V | TP2 | Analog rail **and ADC reference** (VA) |
| 2V5 | U3 AD8031 buffered divider | 2.50 V | TP4 | Bias for every stage |
| Teensy_3V3 | **Teensy's own 3.3 V pin** (MD1) | 3.3 V | — | Powers only U9 (MISO buffer) and the I2C pull-ups R6/R8 |

- RX board current ≈ 25–30 mA at 12 V (op-amps ≈ 1 mA each, JFET up to 6.5 mA). A bench-supply limit of 0.2 A is plenty.
- The Teensy is **powered from USB only**. Teensy VIN (pin 42) is not connected; only GND is shared. Green LED D6 is on whenever the board's 5 V rail is up.

### 2.3 Teensy 4.1 pin map (RX board, MD1)

| Teensy pin | Net | Connected to | Dir | Notes |
|---|---|---|---|---|
| 27 (SCK1) | SPI1_SCK | U10 ADC121S101 SCLK (pin 4) | out | SPI1 default SCK |
| 28 | SPI1_CS | U10 CS (pin 6) **and** U9 SN74LVC1G125 /OE (pin 1) | out (GPIO) | Not a hardware CS of LPSPI3 (that is pin 0/38). Drive with `digitalWriteFast`. CS high also tristates the MISO buffer. |
| **39 (MISO1 alt)** | SPI1_MISO | U9 output Y (pin 4) ← A (pin 2) ← U10 SDATA (pin 5) | in | **SPI1's default MISO is pin 1.** PJRC `SPI.cpp` lists LPSPI3 MISO = {1, 39}, so firmware **must** call `SPI1.setMISO(39);` before `SPI1.begin();`, or every read returns garbage. |
| 26 (MOSI1) | — | not connected | — | The ADC has no data input; SPI1 still clocks MOSI, which is harmless. |
| 24 (SCL2) | I2C2_SCL | U7 AD5245 SCL (pin 4); **R8 4.7 kΩ pull-up to Teensy_3V3** | I2C | `Wire2` |
| 25 (SDA2) | I2C2_SDA | U7 AD5245 SDA (pin 5); **R6 4.7 kΩ pull-up to Teensy_3V3** | I2C | `Wire2` |
| 10 | RX_GAIN_SEL | R27 3.3 kΩ → U6 TS5A3166 IN (pin 4); R28 100 kΩ pull-down at IN | out | HIGH = **LOW gain**, LOW/undriven = **HIGH gain** (§3.4) |
| 3 | LED_TX | D4 red LED + 1 kΩ to GND | out | On RX: "capturing / streaming" indicator |
| 8 | LED_HB | D5 orange LED + 1 kΩ to GND | out | Heartbeat |
| 47 (3.3 V pad) | Teensy_3V3 | U9 VCC, R6, R8 | — | Teensy supplies this net |
| 42 (VIN) | — | **not connected** | — | Never connect |
| 43/45/46 | GND | board ground | — | Common ground |
| **2** | SYNC (optional) | **not on the RX PCB** — Teensy pin only | in | Optional TX→RX sync wire (§7). Free on both boards. |

**Not usable for power detection:** the I2C pull-ups go to the Teensy's 3.3 V, so SDA/SCL read HIGH whenever the Teensy is powered, whether the RX board is on or off. This is unlike the TX board (§4).

### 2.4 Test points (board powered, idle, no input)

| TP | Net | Expected |
|---|---|---|
| TP3 | VIN | ≈ 11.6 V |
| TP1 | 5V25 | ≈ 5.28 V |
| TP2 | 5V | 5.00 V |
| TP4 | 2V5 | 2.50 V |
| TP6 | LNA_OUT (JFET drain) | DC 1.5–3.6 V (depends on the JFET's IDSS) |
| TP5 | GAIN_OUT | ≈ 2.5 V, no MHz oscillation (fixed by the 1 nF on R15) |
| TP7 | BP_OUT | ≈ 2.5 V |
| TP8 | AMP_OUT = ADC input | ≈ 2.5 V → ADC code ≈ 2048 |

Every stage after the LNA is AC-coupled and re-biased at 2V5, so the ADC idles at mid-scale whatever the gain settings.

---

## 3. Chip-level details the software depends on

### 3.1 ADC121S101 (U10) — 12-bit SAR ADC

Source: TI ADC121S101 datasheet (SNAS304). Numbers below are from the Rev G text; the current revision is H. Re-check the timing table in Rev H if you depend on a value with little margin.

- **Supply/reference:** VA = 5.0 V (`5V` rail). The input range is 0..VA.
    - Output is **straight binary**.
    - 1 LSB = VA/4096 = **1.2207 mV**.
    - The first transition is at ½ LSB.
- **Conversion start:** "On the falling edge of CS, a conversion process begins." The **CS falling edge is the sampling instant**. The part goes from hold back to track on the 13th rising SCLK edge.
- **Frame:** "Sixteen SCLK cycles are required to read a complete sample."
    - Output is 3 leading zeros, then 12 data bits MSB first.
    - SDATA changes on **falling** SCLK edges.
    - SDATA goes back to TRI-STATE after the 16th falling edge or when CS rises.
- **Clock / rate:**

    | Range | SCLK | Sample rate |
    |---|---|---|
    | Guaranteed performance | 10–20 MHz | 500 kSPS–1 MSPS |
    | Functional (Operating Ratings) | 25 kHz–20 MHz | up to 1 MSPS |

    - **400 kSPS is below the guaranteed throughput range but inside the functional range.**
    - The rate is set only by how often CS falls. Running SCLK at 12 MHz keeps the clock inside the guaranteed band. The only change is a longer track time between conversions, which helps acquisition.
    - Conversion quality at 400 kSPS is checked in §9.2 (SINAD test). **VERIFY AT BRING-UP.**
- **Timing (VA = 4.75–5.25 V column):**
    - tSU (CS to SCLK) ≥ 10 ns
    - tEN (CS to SDATA enabled) ≤ 20 ns
    - tACC (SCLK falling to data valid) ≤ 20 ns
    - tH (data hold after SCLK falling) ≥ 5 ns
    - tCH, tCL ≥ 0.4 × tSCLK
    - tQUIET ≥ 50 ns
    - tACQ ≥ 350 ns, from the 13th rising SCLK edge to the next CS falling edge
- **Power-down trap:**
    - If CS goes high between the 2nd and 10th falling SCLK edge, the ADC enters **shutdown**.
    - If CS goes high between the 10th and 16th edges, the current conversion is aborted (the word is truncated).
    - **Always clock all 16 bits.**
- **Power-up:** "the first conversion result [is] unusable. The second conversion … is valid." Discard at least the first two conversions after ARM.
- **Digital inputs:**
    - SCLK and CS are rated −0.3 V to 6.5 V absolute maximum with respect to GND.
    - Operating range is "−0.3 V to 5.25 V (regardless of supply voltage)".
    - So 3.3 V drive is fine: VIH = 2.4 V at VA = 5.25 V.

### 3.2 MISO level shifter (U9, SN74LVC1G125) and SPI mode

- The ADC's SDATA swings to 5 V, so it passes through U9. U9 runs from Teensy_3V3 and has 5.5 V-tolerant inputs, so Teensy pin 39 only ever sees 3.3 V.
- U9's /OE is tied to CS: while CS is high, MISO floats (Teensy input; harmless).
- **SPI mode: use `SPI_MODE0`** (CPOL = 0, CPHA = 0: SCLK idles low; MISO is sampled on the rising edge, half a period after the ADC changes SDATA on the falling edge).

    Timing margin at 12 MHz: half-period 41.7 ns, minus tACC 20 ns, minus U9 propagation ≲ 5 ns, leaves ≈ 15 ns for board delay and the LPSPI input setup. Comfortable.

    Do **not** raise SCLK above ≈ 16 MHz in mode 0. At 16 MHz the half-period is 31 ns and the margin is gone.
- **Bit alignment in mode 0** (inferred from the datasheet's timing figure; **VERIFY AT BRING-UP**):
    - The first rising edge captures the leading zero that is valid from CS falling.
    - The 16-bit word is therefore `0 0 0 D11 … D0 x`.
    - The code is `code = (word >> 1) & 0x0FFF`.
    - Bits 15..13 must be 0; any 1 there is a framing error.
    - Make the shift a constant `ADC_SHIFT = 1`. The ARM self-test (§4.3) and the `RAW` command (§5.3) confirm it. At idle the ADC reads ≈ 2048, so raw words must be ≈ `0x1000`. If they read ≈ `0x0800` the shift is 0; if ≈ `0x2000` it is 2.
- **SPI clock:** request `SPISettings(12000000, MSBFIRST, SPI_MODE0)`.
    - Teensyduino rounds down to an integer divisor of the LPSPI root clock.
    - Check SCLK on the scope: it must be within 10–16 MHz.
    - One conversion (16 clocks) takes ≈ 1.33 µs, plus ≈ 0.1–0.2 µs of software overhead.

### 3.3 Sampling engine — decision: hardware timer ISR at top priority

**Decision for v1:** a hardware `IntervalTimer` (PIT) interrupt at exactly 2.5 µs, set to the highest NVIC priority (0). Each ISR does one 16-bit SPI transfer with software CS and stores the sample in a ring buffer.

- **Timebase:** the PIT runs from the 24 MHz crystal clock, so 2.5 µs = 60 ticks exactly, with no rate error and no drift beyond the crystal tolerance.
- **What jitter means here:**
    - The ADC samples on the CS falling edge, so sampling jitter = variation in ISR entry time. CS is the first statement in the ISR.
    - With priority 0 and the code in ITCM (Teensy 4 default), entry varies by a few CPU cycles (a few ns).
    - Rare longer delays happen only if some code disables interrupts globally.
    - The firmware **measures this** with the Cortex-M7 cycle counter at every ISR entry and reports min/max period and maximum ISR duration (`STATS`, §5.3).
- **How much jitter is acceptable:** jitter-limited SNR is `SNR = −20·log10(2π·f·σ_t)`.

    | RMS jitter σ_t | SNR at 20 kHz | SNR at 40 kHz | SNR at 90 kHz |
    |---|---|---|---|
    | 2 ns | 72 dB | 66 dB | 59 dB |
    | 5 ns | 64 dB | 58 dB | 51 dB |
    | 10 ns | 58 dB | 52 dB | 45 dB |
    | 20 ns | 52 dB | 46 dB | 39 dB |
    | 50 ns | 44 dB | 38 dB | 31 dB |

    A 12-bit ADC is ≈ 72 dB at best. Modem demodulation needs roughly 15–30 dB. Bench FFTs look clean down to the ≈ 5–10 ns level. **Target: period spread (max − min) ≤ 50 ns, and no outliers over a 10 s streaming run.**

- **Why not DMA now:** DMA only removes jitter if the CS edge is also produced by hardware.
    - Pin 28 is not an LPSPI3 hardware chip-select pin.
    - A jitter-free DMA design would need three pieces:
        - a FlexPWM output generating CS on pin 28 (pin 28 is PWM-capable on Teensy 4.1 per PJRC's pin table; verify);
        - a PWM-triggered DMA channel writing LPSPI3's transmit register;
        - a second DMA channel draining the receive FIFO into a ring buffer.
    - That is significantly harder to debug during bring-up.
    - **Keep it as the v2 upgrade path** if `STATS` or the SINAD test (§9.2) shows that jitter limits the measurement.
- **CPU budget:** the ISR takes ≈ 1.5 µs of every 2.5 µs (≈ 60 %). The main loop keeps ≈ 40 % of a 600 MHz core. That is ample for framing, CRC and USB at 0.8 MB/s. The ISR must not use Serial, I2C, floats or `String`.

### 3.4 Coarse gain switch (U6, TS5A3166)

- **Wiring:** R26 5.6 kΩ ∥ C35 68 pF goes from GAIN_OUT to NO (pin 1). COM (pin 2) goes to node G, the feedback summing node of U4. V+ = 5 V. IN (pin 4) ← R27 3.3 kΩ ← Teensy pin 10. R28 100 kΩ pulls IN down to GND.
- **Logic (TI function table):** IN = H → switch ON (NO–COM connected); IN = L → OFF.
    - At V+ = 4.5–5.5 V: VIH ≥ 2.4 V, VIL ≤ 0.8 V.
    - The Teensy's 3.3 V through the R27/R28 divider gives 3.19 V at IN.
- **Result:**

    | Teensy pin 10 (RX_GAIN_SEL) | Switch | Feedback | Gain stage gain (DC) | Name |
    |---|---|---|---|---|
    | LOW, or input / undriven (R28 pull-down) | OFF | R13 47 kΩ | 1 + 47k/820 = **58.32 (35.3 dB)** | **HIGH gain** (hardware default) |
    | HIGH | ON | 47k ∥ 5.6k = 5.004 kΩ | 1 + 5004/820 = **7.10 (17.0 dB)** | **LOW gain** |

    - Step between states: **18.3 dB** (ratio 8.21).
    - In band, C24 (6.8 pF) and C35 (68 pF) lower these slightly: 58.1 / 7.07 at 40 kHz.
- **Firmware default on ARM: LOW gain** (pin 10 driven HIGH). This is the safe state for strong near-field signals, like the TX firmware starting at minimum pot gain. The hardware default when the Teensy is not driving is HIGH gain.
- Switching takes effect within microseconds. The next few hundred µs of data contain a step transient; data frames carry the gain state, so the app can see which blocks are affected.

### 3.5 Variable-gain amplifier (U8 OPA365 + U7 AD5245BRJZ10, 10 kΩ)

#### Topology (final designators; the planning names R401/R402/R403 map as shown)

| Planning name | Actual | Value | Connection |
|---|---|---|---|
| R401 | **R36** | 82 Ω | pot terminal A (pin 8) → `2V5` (C48 bypass) |
| R402 | **R31** | 4.7 kΩ | U8 output (AMP_OUT) → pot terminal B (pin 7) |
| R403 | **R35** | 10 kΩ | pot wiper W (pin 1) → U8 −IN (no DC current; isolates the −IN capacitance) |
| C402 | **C42** | 68 pF | AMP_OUT → wiper W (HF compensation) |
| C404 / R404 | **C39 / R34** | 6.8 nF / 10 kΩ | input AC coupling and bias from BP_OUT (HP ≈ 2.34 kHz) |

Non-inverting amplifier: the feedback divider runs from AMP_OUT through R31 and R_WB to the wiper (= −IN), and from the wiper through R_WA and R36 to 2V5.

#### Gain vs code (normative)

AD5245 datasheet, Rev C, eqs. (1) and (2). The wiper term is **2 × R_W**:

```
R_WB(D) = D/256 · R_AB + 2·R_W
R_WA(D) = (256 − D)/256 · R_AB + 2·R_W          R_AB = 10 kΩ (±30 %),  R_W = 50 Ω typ (120 Ω max)
G_VGA(D) = 1 + (R31 + R_WB(D)) / (R36 + R_WA(D)) = 1 + (4700 + R_WB) / (82 + R_WA)
```

- **Higher code = more gain** (opposite to the TX board's pot). Code 0 is the minimum.
- At high gain, C42 adds a pole. For the frequency-dependent version, replace (R31 + R_WB) with (R31 + R_WB) ∥ (1/(j·2πf·C42)).
- At D = 255 this pole costs ≈ 1.2 dB at 90 kHz.

| D | R_WB [Ω] | R_WA [Ω] | G_VGA (R_W = 50 Ω) | dB | G_VGA (R_W = 120 Ω) | \|G\| at 90 kHz (R_W = 50 Ω) |
|---|---|---|---|---|---|---|
| 0 | 100 | 10 100 | 1.471 | 3.4 | 1.48 | 1.46 |
| 32 | 1 350 | 8 850 | 1.677 | 4.5 | 1.68 | 1.65 |
| 64 | 2 600 | 7 600 | 1.950 | 5.8 | 1.95 | 1.90 |
| 96 | 3 850 | 6 350 | 2.329 | 7.3 | 2.32 | 2.23 |
| 128 | 5 100 | 5 100 | 2.891 | 9.2 | 2.87 | 2.73 |
| 160 | 6 350 | 3 850 | 3.810 | 11.6 | 3.75 | 3.53 |
| 192 | 7 600 | 2 600 | 5.586 | 14.9 | 5.41 | 5.07 |
| 208 | 8 225 | 1 975 | 7.283 | 17.2 | 6.95 | 6.54 |
| 224 | 8 850 | 1 350 | 10.46 | 20.4 | 9.71 | 9.29 |
| 232 | 9 162 | 1 038 | 13.38 | 22.5 | 12.1 | 11.8 |
| 240 | 9 475 | 725 | 18.57 | 25.4 | 16.1 | 16.3 |
| 248 | 9 788 | 412 | 30.30 | 29.6 | 24.1 | 26.5 |
| 252 | 9 944 | 256 | 44.29 | 32.9 | 31.9 | 38.6 |
| 255 | 10 061 | 139 | **67.77** | 36.6 | **42.3** | 58.9 |

- **Range: ×1.47 to ×68 typical (3.4 to 36.6 dB).**
    - The top codes depend strongly on the real wiper resistance, and R_AB has a ±30 % tolerance.
    - The earlier design notes quoted ×1.94–181.7. That figure ignored the wiper resistance (R_W = 0 gives ×122 at D = 255) and should be treated as superseded.
- The pot's taper is very non-linear: most of the range is in the top 40 codes. The app should therefore offer a **"gain in dB" control that maps to the nearest code**, not only a raw code slider (§6.4).
- **Calibrate on the bench** (§9.2 step 6) and store a measured gain-vs-code table. The app uses it in place of the model when it is available.

#### AD5245 I2C protocol

Source: ADI AD5245 Rev C.

- **7-bit address 0x2C** (pattern `0101 10 AD0`, AD0 tied to GND). Write byte 0x58, read byte 0x59.
- **Write:** `START, 0x2C+W, ACK, instruction, ACK, data D7..D0, ACK, STOP`.
- **Instruction byte = `X RS SD X X X X X`:**
    - **RS** (bit 6): mid-scale reset.
    - **SD** (bit 5): shutdown — opens terminal A and shorts W to B.
    - **Normal write = 0x00.**
    - **Never set SD on this board**: it would remove the VGA's feedback path to 2V5 and turn the stage into a unity follower.
- **Read:** `START, 0x2C+R` → one data byte (wiper code); the master NACKs, then STOP. **No instruction byte is sent for a read.**
- SCL ≤ 400 kHz. At VDD = 5 V: VIH ≥ 2.4 V and VIL ≤ 0.8 V, so the 3.3 V pull-ups are valid.
- **Power-up value: mid-scale (D = 128).** The pot is volatile; the firmware writes it at every ARM.
- Teensy:
    - Write: `Wire2.beginTransmission(0x2C); Wire2.write(0x00); Wire2.write(D); Wire2.endTransmission()` returns 0 on ACK.
    - Read: `Wire2.requestFrom(0x2C, 1)` returns 1 if the part answered, then `Wire2.read()`.

### 3.6 End-to-end gain model (hydrophone terminals → ADC input)

Use this everywhere in the app (normative; all values from the RX schematic):

```
G_total(f) = cal · G_FE(f) · G_GS(f, coarse) · H_BP(f) · G_VGA(f, D) · H_RC(f)

G_FE   LNA path : G_LNA · R12/(R12 + 1/(jωC19))               G_LNA = 9.0 nominal (7.1 … 11.2, JFET spread)
       PA-4 path: 10 · R12/(R12 + R37 + 1/(jωC19))            (PA-4 jumper J2 open = +20 dB, R37 = 330 Ω)
G_GS   1 + Zf/R23,  Zf = R13 ∥ C24              (HIGH)         R13 = 47k, C24 = 6.8 pF, R23 = 820 Ω
                    Zf = R13 ∥ C24 ∥ (R26 ∥ C35)  (LOW)        R26 = 5.6k, C35 = 68 pF
H_BP   two unity-gain Sallen-Key high-pass sections × two low-pass sections:
       HP: H = a·s² / (a·s² + R1·2C·s + 1),  a = R1·R2·C²   (R1 = resistor to output, R2 = resistor to 2V5)
           HP-A R1 = 7.5k, R2 = 8.2k, C = 1 nF;   HP-B R1 = 3.0k, R2 = 20k, C = 1 nF
       LP: H = 1 / (R²·C1·C2·s² + 2R·C2·s + 1)          (C1 = feedback cap, C2 = shunt cap)
           LP-A R = 1.5k, C1 = 1.2 nF, C2 = 1 nF;   LP-B R = 1.5k, C1 = 3.3 nF, C2 = 470 pF
G_VGA  R34/(R34 + 1/(jωC39)) · (1 + Zb/(R36 + R_WA)),  Zb = (R31 + R_WB) ∥ C42
H_RC   1 / (1 + jω·R32·C43)                                    R32 = 100 Ω, C43 = 3.3 nF (−0.15 dB at 90 kHz)
cal    user calibration factor (default 1.00), measured on the bench (§9.2)
```

**Band-pass response (check values for unit tests):**

| f [kHz] | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 150 | 200 | 310 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| H_BP [dB] | −3.70 | −0.24 | +0.16 | +0.36 | +0.41 | +0.08 | −0.94 | −2.79 | −5.22 | −17.9 | −27.7 | −42.8 |

At 310 kHz (the alias of 90 kHz at 400 kSPS) the filter gives ≈ −43 dB.

**Total gain, JFET LNA path (G_LNA = 9.0, R_W = 50 Ω, cal = 1):**

| Coarse | D | 20 kHz | 40 kHz | 60 kHz | 90 kHz | ×(40 kHz) | ADC full scale (5 Vpp) referred to the hydrophone at 40 kHz |
|---|---|---|---|---|---|---|---|
| HIGH | 0 | 53.9 dB | 57.8 dB | 58.0 dB | 54.6 dB | 778 | 6.43 mVpp |
| HIGH | 64 | 56.4 | 60.2 | 60.4 | 56.9 | 1 027 | 4.87 mVpp |
| HIGH | 128 | 59.8 | 63.6 | 63.7 | 60.0 | 1 512 | 3.31 mVpp |
| HIGH | 192 | 65.5 | 69.2 | 69.2 | 65.4 | 2 896 | 1.73 mVpp |
| HIGH | 224 | 70.9 | 74.6 | 74.6 | 70.7 | 5 397 | 0.93 mVpp |
| HIGH | 240 | 75.9 | 79.6 | 79.5 | 75.6 | 9 553 | 0.52 mVpp |
| HIGH | 255 | 87.1 | 90.8 | 90.7 | 86.7 | 34 788 | 0.14 mVpp |
| LOW | 0 | 35.6 | 39.5 | 39.7 | 36.3 | 95 | 52.9 mVpp |
| LOW | 64 | 38.1 | 41.9 | 42.1 | 38.5 | 125 | 40.0 mVpp |
| LOW | 128 | 41.5 | 45.3 | 45.3 | 41.7 | 184 | 27.2 mVpp |
| LOW | 192 | 47.2 | 50.9 | 50.9 | 47.1 | 352 | 14.2 mVpp |
| LOW | 224 | 52.6 | 56.3 | 56.3 | 52.3 | 657 | 7.62 mVpp |
| LOW | 240 | 57.6 | 61.3 | 61.2 | 57.2 | 1 162 | 4.30 mVpp |
| LOW | 255 | 68.8 | 72.5 | 72.4 | 68.4 | 4 232 | 1.18 mVpp |

**Uncertainty:**

| Source | Effect |
|---|---|
| JFET gain spread | −2.1 / +1.9 dB |
| Wiper resistance | 0 dB at low codes, up to −4 dB at D = 255 |
| R_AB tolerance | Gain at mid/high codes shifts by up to a few dB |
| Resistors (1 %) | Small |

The **cal factor** and the **measured VGA table** (§9.2) absorb these. **PA-4 path:** multiply by 9.67/8.98 (+0.6 dB) relative to the LNA rows.

### 3.7 Code ↔ volts, clipping

- Absolute ADC input: `V_adc = code × 5.0 / 4096`.
- AC signal at the ADC: `v = (code − mid) × 1.2207 mV`, where `mid` is the idle mean measured at ARM (≈ 2048).
- Input-referred amplitude at frequency f: `V_hyd = V_adc_ac / |G_total(f)|`. For a broadband signal use the dominant frequency from the FFT, and say so in the UI.
- **Clip thresholds: code ≤ 8 or ≥ 4087** (within ≈ 10 mV of the rails). The firmware flags these per frame.
- **Gain-stage clipping the ADC cannot see:**
    - In band, the ADC always clips first: after U4 the band-pass is ≈ unity and the VGA gain is ≥ 1.47, so the CLIP flag covers in-band signals.
    - U4 (GAIN_OUT, 0–5 V swing) can still clip **unseen** on strong **out-of-band** content, because the band-pass removes it before the ADC. Examples: low-frequency handling or pump noise, or energy below ~15 kHz or above ~100 kHz.
    - The app therefore estimates `V_GAIN_OUT = V_adc_pp / |H_BP · G_VGA · H_RC|` at the dominant frequency of the **full-band** FFT (0–200 kHz). If that exceeds **4.5 Vpp**, warn: "gain stage may be clipping — switch to LOW coarse gain".
    - This is only an estimate, since a signal U4 clips may not show at all after the filter. The bench test in §9.2 step 7 verifies it.

---

## 4. Safety requirements (MUST implement)

### 4.1 Back-feed analysis — what the firmware can and cannot control

When the Teensy is on USB and the RX board's 12 V is off, each Teensy-connected pin behaves as follows:

| Pin | Into | Datasheet behaviour when the board is unpowered | Policy |
|---|---|---|---|
| 24/25 SCL/SDA | AD5245 SCL/SDA, **pulled up to Teensy_3V3 by R8/R6 4.7 kΩ** | Digital pins are rated 0–7 V independent of VDD and protected by "series input resistors and parallel Zener ESD structures". The ideal power-up order is GND, VDD, then digital inputs, then A/B/W. | **Firmware cannot prevent this** — the pull-ups are hardware. Current is limited by 4.7 kΩ (< 0.6 mA per line even with a clamp). Handle it with the **power procedure** (§4.2). |
| 27 SCK, 28 CS | ADC SCLK/CS (CS also U9 /OE) | Rated −0.3–6.5 V abs max vs GND, "regardless of supply voltage". The datasheet is silent on VA = 0. U9 is powered by the Teensy, so it is always valid. | High-Z until ARM. |
| 39 MISO | U9 output (powered from Teensy_3V3) | Normal input | Input. |
| 10 RX_GAIN_SEL | 3.3 kΩ → TS5A3166 IN | IN rated −0.5–6.5 V, "control inputs 5.5 V tolerant". The datasheet does not cover V+ = 0. | High-Z until ARM (R28 holds IN low). |
| 3, 8 LEDs | LED + 1 kΩ → GND | No path into any rail | Always safe. |
| Teensy 3.3 V pad | Teensy_3V3 net (U9 + pull-ups only) | Not connected to the board's 5 V | — |

So on the RX board the only unavoidable "back-feed" is the two pull-ups into the AD5245's digital pins. The datasheet ratings suggest it is negligible, but they do not guarantee behaviour with VDD = 0. Everything else is controlled by keeping pins high-Z until ARM.

### 4.2 RX power procedure (show it in the app's help and in the ARM dialog)

1. **Power up:** bench supply 12 V (limit ≈ 0.2 A), output ON. Check that green LED D6 is on. **Then** plug in the RX Teensy's USB (or connect if it is already enumerated).
2. Connect in the app, then **ARM**.
3. **Power down:** STOP streaming/capture → **DISARM** → **unplug the RX Teensy's USB** → switch off the 12 V.
4. Unlike the TX board, **do not leave the RX Teensy on USB with the RX board's 12 V off** for long periods. It is not destructive, but it runs the AD5245 outside its recommended power-up order.
5. **Keep the existing bring-up rule:** 12 V before USB; USB out before 12 V off.

### 4.3 What ARM must check (replaces the TX SDA/SCL trick)

The RX ARM sequence (normative order):

1. At boot (`setup()`): pins 24, 25, 27, 28, 39 and 10 as **INPUT**. Do not call `SPI1.begin()` or `Wire2.begin()`. Configure LEDs, and pin 2 (SYNC) as `INPUT_PULLDOWN`.
2. **Board-power check via the AD5245 ACK:**
    - `Wire2.begin()` at 100 kHz, then read one byte from 0x2C.
    - The AD5245 can only acknowledge when the board's 5 V rail is up.
    - I2C is open-drain, so this adds no drive into the board beyond the pull-ups already there.
    - No ACK → reply `ERR NOPOWER …` and return all pins to INPUT.
3. Write the stored pot code (default 0 = minimum gain); no ACK → `ERR NOPOT`. Set I2C to 400 kHz.
4. Drive pin 10 for the stored coarse state (default **LOW gain** → pin 10 HIGH). Set pin 28 OUTPUT HIGH, call `SPI1.setMISO(39)`, `SPI1.begin()`, then `SPI1.beginTransaction(12 MHz, MSBFIRST, MODE0)` (kept open — SPI1 serves only the ADC).
5. **ADC self-test** (proves 5 V, 2V5 bias, SPI wiring, MISO pin and bit alignment):
    - Discard 2 conversions.
    - Read 64 conversions (CS framed, ≈ 3 µs apart).
    - Require: ≤ 2 framing errors (bits 15..13 must be 0), **and** mean code within **1648–2448** (2.5 V ± 0.49 V).
    - Otherwise reply `ERR ADC self-test failed: mean=… frame_errors=…` and DISARM.
    - Store the mean as `mid` (used for the trigger and in STATUS).
6. Reset counters, start the 400 kHz timer at priority 0, and reply `OK ARMED mid=<code>`.

**DISARM** (and every failure path):

- stop the timer, streaming and capture;
- end the SPI transaction and SPI1;
- return pins 24, 25, 27, 28, 39 and 10 to INPUT (the coarse gain returns to the hardware default HIGH);
- LED_TX off;
- reply `OK DISARMED`.

The app must send `STREAM OFF`, `ABORT` and `DISARM` before it closes the port or quits.

### 4.4 Runtime watchdogs

- **Power-loss watchdog:**
    - Every 500 ms while armed, the main loop reads the AD5245 (≈ 50 µs at 400 kHz; the ISR keeps sampling).
    - **Two consecutive NACKs** → DISARM and print `WARN POWERLOST RX board stopped answering: disarmed, pins released`.
- **Host-gone watchdog:** if `Serial.dtr()` goes false (the app closed the port), stop streaming and cancel any capture. Stay armed; this is harmless, and the app will DISARM on its next connect if needed.
- `STATUS` while not armed does the same AD5245 read to report `power=1/0`, then releases the pins again.

### 4.5 Signal-level safety and bench notes (help text)

- Inputs to J2 must stay small:
    - the JFET gate is clamped by a BAV99 at ±0.7 V behind R14 100 Ω;
    - for bench tests feed **≤ 50 mVpp** through a divider (e.g. 10 kΩ : 100 Ω for 100:1);
    - never connect the TX power-amp outputs (AMP_A/AMP_B, up to 76 Vpp) to the RX board.
- Close-range water tests (≤ 1 m): start with the TX at a few Vpp, RX in **LOW** coarse gain, pot 0. Raise the gain until the ADC signal is 1–3 Vpp.
- The PA-4 (if used) is powered from VIN through P1. The app's front-end setting must match jumper P2.
- Scope ground is board GND. Probe TP5/TP7/TP8 with a ground spring and 20 MHz bandwidth limit.

---

## 5. Firmware requirements (D1)

### 5.1 Structure

- **Sample ISR** (`IntervalTimer`, 2.5 µs, `priority(0)`). Per tick, in this order:
    1. `digitalWriteFast(28, LOW)` — this is the sampling instant;
    2. `raw = SPI1.transfer16(0)`;
    3. `digitalWriteFast(28, HIGH)`;
    4. `code = (raw >> ADC_SHIFT) & 0x0FFF`; set the framing-error bit if `raw & 0xE000`;
    5. read the SYNC pin (`digitalReadFast(2)`, one cycle) into bit 15;
    6. store the 16-bit word in the ring buffer (`ring[head & MASK]`), then `head++`;
    7. if a capture trigger is armed, test it (§5.4) and record the trigger sample index;
    8. update the timing statistics from `ARM_DWT_CYCCNT` (entry-to-entry period min/max, ISR duration max).

    Nothing else: no Serial, I2C, floats or `String`. Variables shared with the main loop are `volatile`; multi-word reads are done with interrupts briefly disabled.

- **Ring buffer:** 65 536 × uint16 in RAM1 (128 KiB) = **164 ms** of samples. `head` is a 32-bit count of samples since ARM (wraps after 2³² samples ≈ 2.98 h). The main loop extends it to 64 bits for the frame header.
- **Stored sample word** (also the payload word in frames):

    | Bits | Meaning |
    |---|---|
    | 0..11 | ADC code |
    | 12..13 | 0 |
    | 14 | ADC framing error (leading bits were not zero) |
    | 15 | SYNC input level at that sample |

- **Main loop:**
    - read USB serial lines (`\n` or `\r\n`, ≤ 90 chars) and execute commands;
    - one reply line per command;
    - service streaming or capture by sending complete frames (§5.3) from the ring;
    - power watchdog (§4.4);
    - LEDs: LED_TX on while streaming or capturing; heartbeat slow when disarmed, fast when armed.
- **Ordering rule (critical for mixing text and binary):** everything sent to USB — reply lines, `WARN`/`EVT` lines and frames — is written **only from the main loop**, one complete record at a time (`Serial.write(buf, len)` for a frame, `Serial.println` for a line). The ISR never prints. A text line can therefore never land inside a frame.
- **Overflow handling:**
    - If the PC falls behind, the next unsent sample index becomes older than the ring can hold (`head − sendPos > RING_N − 2·BLOCK`).
    - The firmware then skips ahead to `head − BLOCK`, adds the skipped count to `ovf`, and sets `FLAG_OVERFLOW` on the next frame.
    - Frame `first_index` values then show the exact gap.

### 5.2 Serial protocol (ASCII, one command per line, case-insensitive)

Same style as TX. Replies start with `OK`, `ERR`, `WARN`, `STATUS` or `PONG`. **Asynchronous notifications start with `EVT`.** Numbers are decimal unless stated.

| Command | Effect | Reply |
|---|---|---|
| `PING` | identify | `PONG RXFW <version>` (the app uses `RXFW` vs `TXFW` to verify the board on each port) |
| `ARM` | power check → init → ADC self-test (§4.3) | `OK ARMED mid=<code>` / `ERR NOPOWER …` / `ERR NOPOT …` / `ERR ADC …` |
| `DISARM` | stop everything, release pins | `OK DISARMED` |
| `STATUS` | report state | see below |
| `GAIN HIGH` \| `GAIN LOW` | coarse gain (stored; applied immediately if armed) | `OK GAIN LOW` |
| `POT <D>` | AD5245 code 0..255 (0 = min gain); written immediately if armed, else stored | `OK POT <D>` / `ERR NOPOT …` |
| `POTREAD` | read back the wiper (armed only) | `OK POTREAD <D>` |
| `TRIG NONE` | capture starts immediately | `OK TRIG NONE` |
| `TRIG LEVEL <counts>` | trigger when \|code − mid\| ≥ counts (1..2047) | `OK TRIG LEVEL <counts>` |
| `TRIG SYNC` | trigger on the SYNC input's rising edge (§7) | `OK TRIG SYNC` |
| `CAPTURE <n> <pre>` | one triggered capture: n samples in total (16..4 000 000), of which `pre` (0..min(32768, n−1)) are before the trigger | `OK CAPTURE id=<id>`, then `EVT TRIG id=<id> index=<sample>`, frames, `EVT CAPDONE id=<id> n=<n> pre=<pre> trig=<sample>` |
| `ABORT` | cancel a pending or running capture | `OK ABORT` |
| `STREAM ON` \| `STREAM OFF` | continuous streaming of every sample (exclusive with CAPTURE) | `OK STREAM ON` / `OK STREAM OFF` |
| `RAW [k]` | k (1..16) raw 16-bit SPI words in hex, for bring-up (not while streaming or capturing; pauses the timer briefly) | `OK RAW 1000 1001 0FFE …` |
| `STATS` | counters and ISR timing since the last `STATS` (then reset) | `OK STATS frames=… ovf=… ferr=… per_min_ns=… per_max_ns=… isr_max_ns=…` |

Error replies: `ERR NOTARMED`, `ERR BUSY …`, `ERR … out of range …`, `ERR unknown command: …`.

**`STATUS`** (single line, key=value):

```
STATUS armed=1 power=1 gain=LOW pot=0 fs=400000 stream=0 cap=IDLE trig=LEVEL level=200 mid=2049 sync=0 ovf=0 ferr=0
```

- `cap` is `IDLE`, `WAIT` (waiting for the trigger) or `RUN`.
- `power` comes from the AD5245 ACK. While streaming it reports `1` without an I2C read; the watchdog covers that case.

**Asynchronous lines:**

- `EVT TRIG …`
- `EVT CAPDONE …`
- `WARN POWERLOST …`
- `WARN OVERFLOW …` (capture data lost)

### 5.3 Binary frame format (normative)

Device→host bytes are a sequence of **records**: either an ASCII line, or a binary frame.

- ASCII lines: printable ASCII 0x20–0x7E, ending in `\n`.
- A binary frame always starts with the magic byte **0xA5**, which can never appear in an ASCII line. The first byte of a record therefore tells the parser what follows.

Frame layout, little-endian:

| Offset | Size | Field | Notes |
|---|---|---|---|
| 0 | 4 | magic | `A5 5A 52 58` ("\xA5\x5A" "RX") |
| 4 | 1 | version | 1 |
| 5 | 1 | type | 1 = STREAM block, 2 = CAPTURE block |
| 6 | 2 | flags | see below |
| 8 | 4 | seq | frame counter since ARM, +1 per frame, wraps |
| 12 | 8 | first_index | absolute sample index of payload[0] (0 = first sample after ARM) |
| 20 | 2 | n | samples in this frame, 1..4096 (firmware uses 1024; the last capture block may be shorter) |
| 22 | 1 | gain | 0 = HIGH, 1 = LOW coarse gain at send time |
| 23 | 1 | pot | AD5245 code at send time |
| 24 | 2 | cap_id | capture id, 0 for STREAM |
| 26 | 2 | trig_off | index of the trigger sample in this frame, 0xFFFF = none |
| 28 | 2 | sync_off | index of the first SYNC rising edge in this frame, 0xFFFF = none |
| 30 | 2 | fs_div100 | sample rate / 100 (4000 = 400 kSPS); reserved for future decimation |
| 32 | 2n | payload | uint16 sample words (bits as in §5.1) |
| 32+2n | 4 | crc32 | CRC-32/IEEE (poly 0xEDB88320 reflected, init and final XOR 0xFFFFFFFF) over bytes [0, 32+2n) — identical to Python `zlib.crc32` |

**Flags:**

| Bit | Value | Name | Meaning |
|---|---|---|---|
| 0 | 0x0001 | CLIP | some code ≤ 8 or ≥ 4087 in this frame |
| 1 | 0x0002 | OVERFLOW | samples were dropped right before this frame |
| 2 | 0x0004 | CAP_FIRST | first frame of a capture |
| 3 | 0x0008 | CAP_LAST | last frame of a capture |
| 4 | 0x0010 | FRAME_ERR | some sample has bit 14 set |
| 5 | 0x0020 | SYNC | `sync_off` is valid |

**Host parser (normative behaviour):**

```
loop while the buffer is not empty:
  if buf[0] == 0xA5:
      need 4 bytes; if buf[0:4] != MAGIC: drop 1 byte (count resync), continue
      need 32 bytes; parse header; if version != 1 or type not in {1,2} or not 1 <= n <= 4096:
          drop 1 byte (resync), continue
      need 32 + 2n + 4 bytes; if CRC mismatch: count crc_error, drop 1 byte, continue
      emit frame, consume it
  else:
      k = index of "\n", j = index of 0xA5
      if k exists and (j missing or k < j): emit line buf[:k] (strip "\r"), consume k+1
      elif j exists: drop buf[:j] (garbled text), continue
      else: wait for more data (if > 1 kB without "\n": drop as garbage)
```

The firmware CRC (normative):

```cpp
static uint32_t crcTab[256];
void crcInit() { for (uint32_t i = 0; i < 256; i++) { uint32_t c = i;
  for (int k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1); crcTab[i] = c; } }
uint32_t crc32(const uint8_t *p, size_t n) { uint32_t c = 0xFFFFFFFFu;
  while (n--) c = crcTab[(c ^ *p++) & 0xFF] ^ (c >> 8); return c ^ 0xFFFFFFFFu; }
```

### 5.4 Capture modes

**(a) Triggered capture — `TRIG …` then `CAPTURE <n> <pre>`:**

- The ISR tests the trigger only from sample `head_at_command + pre + 1` onward, so the pre-trigger samples are always fresh.
- `LEVEL`: `|code − mid| ≥ level` (symmetric — works for bursts of either polarity). `SYNC`: rising edge on pin 2. `NONE`: the first eligible sample.
- On the trigger, the main loop prints `EVT TRIG` and sends CAPTURE frames from `trig − pre` to `trig − pre + n` as the samples arrive. It then prints `EVT CAPDONE` and returns to idle.
- Long captures (up to 4 M samples = 10 s) are **streamed while they happen**. They are not limited by the ring size, only by the PC keeping up.
- Single-shot: the app re-issues `CAPTURE` for auto/repeat mode. `ABORT` cancels.
- The app shows a "waiting for trigger" state and offers a timeout.

**(b) Continuous streaming — `STREAM ON`:**

- Sends every sample in 1024-sample STREAM frames, starting at a block-aligned index.
- Exclusive with CAPTURE: the app turns streaming off before a capture and back on afterwards if wanted.

**(c) Decimation:**

- **Not needed** (§5.5). Firmware v1 always sends 400 kSPS.
- The app decimates for display only, using min/max envelope reduction for the time plot so the display never aliases.
- If a slow link ever requires it, a later firmware can add `DECIM 2` using a proper FIR half-band low-pass filter (pass band to 90 kHz needs a long filter because Nyquist is 100 kHz) and report it in `fs_div100`. Plain averaging must **not** be used: it aliases.

### 5.5 USB throughput

| Quantity | Value |
|---|---|
| Raw data | 400 000 samples/s × 2 B = **800 kB/s** |
| Frame (1024 samples) | 32 + 2048 + 4 = 2084 B every 2.56 ms → **814 kB/s = 6.5 Mbit/s** (1.8 % overhead) |
| Teensy 4.1 USB | 480 Mbit/s high-speed; Teensy USB serial sustains many MB/s → **≈ 1.4 % of the bus** |
| Through a USB 1.1 / full-speed hub (12 Mbit/s) | 54 % — would work but is marginal: plug the Teensy directly into the PC |
| PC side | ≈ 390 frames/s; parsing with `zlib.crc32` + `numpy.frombuffer` costs well under 10 % of one core |
| Buffering | 164 ms ring on the Teensy; Windows USB stalls longer than that cause a flagged gap, not corrupted data |

Both boards on the same PC: TX traffic is tiny (ASCII only), so there is no conflict.

### 5.6 Coarse-gain and pot changes during acquisition

- Allowed at any time. Each frame header carries the gain state and pot code at send time, so the app knows exactly which settings applied to each 2.56 ms block.
- The app marks a capture as "settings changed during capture" if the frames disagree.

### 5.7 Acceptance criteria (firmware)

1. **12 V off, USB on:**
    - `ARM` → `ERR NOPOWER`;
    - `STATUS` → `power=0 armed=0`;
    - pins 10, 24, 25, 27, 28, 39 remain inputs (measure pin 10 and pin 28: no drive).
2. **12 V on:**
    - `ARM` → `OK ARMED mid=` 1950..2150;
    - `RAW 8` → words ≈ `0x0F80..0x10C0` with bits 15..13 = 0;
    - mean code × 1.2207 mV equals the TP8 DMM reading within ±10 mV.
3. **Scope:**
    - CS (pin 28) low for ≈ 1.4–1.6 µs every **2.500 µs**;
    - 16 SCLK pulses at 10–16 MHz inside each CS-low;
    - no CS rising edge before the 16th clock.
4. **`STREAM ON` for 10 min, then `STATS`:**
    - `ovf=0`, `ferr=0`, no CRC errors at the app;
    - `seq` continuous;
    - `per_min_ns`/`per_max_ns` within **2500 ± 25 ns**; `isr_max_ns` < 2000.
5. **Gain commands:**
    - `GAIN HIGH`/`LOW` switch pin 10 (LOW gain = pin HIGH);
    - `POT 200` then `POTREAD` → 200;
    - amplitude steps match §9.2.
6. **Triggered capture:**
    - `TRIG LEVEL`/`SYNC` captures contain exactly `n` samples;
    - the trigger sample sits at offset `pre`;
    - `EVT TRIG` and `EVT CAPDONE` are printed.
7. **Power loss:** switching the 12 V off while armed gives `WARN POWERLOST` within ≈ 1 s, and the pins are released.
8. **Host gone:** closing the app while streaming stops streaming (DTR watchdog).

---

## 6. Desktop app requirements (D2)

### 6.1 Structure changes to the existing app

- **Two connections.** Replace the single global port bar with a **connection bar inside each tab**: port combobox, Refresh, Connect/Disconnect, and the identified board shown next to it.
    - The TX tab owns a `SerialLink` (unchanged, line-based).
    - The RX tab owns an `RxLink` (frame-aware, §6.2).
    - Both can be open at once.
- **Board identification:**
    - After opening a port, send `PING`.
    - The TX tab requires `PONG TXFW …`; the RX tab requires `PONG RXFW …`.
    - On a mismatch: show "This is the TX board's Teensy (wrong port)" and close the port.
    - Grey out a port in one tab's combobox when the other tab already has it open.
- **Port labels:** both Teensys report VID 0x16C0. Show the serial number from `QSerialPortInfo.serialNumber()` next to the COM name. Remember (QSettings) which serial number was TX and which was RX, and pre-select them next time.
- **Global stop:** Esc = TX `STOP` + RX `STREAM OFF` / `ABORT`.
- **Close:** for each open link, send the safe-shutdown commands, flush (`waitForBytesWritten`), then close:
    - TX: `DISARM` if armed (existing behaviour);
    - RX: `STREAM OFF`, `ABORT`, `DISARM`.
- **TX → RX information:**
    - The RX tab gets a read-only callback `tx_state()` from the TX tab: connected, armed, running, freq, mode, on_us, per_us, estimated Vpp.
    - Used for the delay-measurement template (§6.6) and saved in capture metadata.
    - The TX tab gets a "SYNC output" checkbox if TX firmware v1.1 is detected (§7).
- **Dependencies:** `requirements.txt` → `PySide6>=6.5`, `pyqtgraph>=0.13`, `numpy>=1.24`.
- **Suggested modules:**
    - `rx_protocol.py` — frame parser and CRC; pure Python, unit-tested;
    - `rx_model.py` — §3.6 gain model;
    - `rx_analysis.py` — measurements, pure numpy;
    - `rx_link.py`;
    - `rx_tab.py`.
- Keep TX code in its own module.

### 6.2 RxLink (serial transport)

- `QSerialPort`, event-driven `readyRead` (as in TX); DTR set on open.
- `readAll()` → `FrameParser.feed()`, which yields `('line', str)` and `('frame', Frame)` events.
- Qt signals:
    - `line_received(str)`
    - `frame_received(object)`
    - `line_sent(str)`
    - `state_changed(bool)`
    - `stats_changed(crc_errors, resync_bytes, frames)`
- `Frame` exposes:
    - header fields;
    - `codes = raw & 0x0FFF`, `sync = raw & 0x8000 != 0`.
- The serial log shows text lines only, plus a counter "frames: N, CRC errors: N, gaps: N". Never log frames.
- **Continuity check:** track `seq` and `first_index`. Any jump → increment "gaps" and mark the plot (vertical red line) at the gap.
- Handle unplugging the USB (`ResourceError`): mark disconnected.

### 6.3 RX tab layout

Left column (controls, scrollable). Right side: plots (§6.5).

1. **Connection bar** (§6.1).
2. **Board group:**
    - status text: "Not connected" / "RX board 12 V not detected" / "Board powered — not armed" / "ARMED (mid = 2049)";
    - **Arm** button with a confirmation dialog containing the §4.2 procedure ("Is the RX board's 12 V ON and green LED D6 lit?");
    - **Disarm** button;
    - Help button with the power procedure and bench notes (§4.2, §4.5).
3. **Gain group** (§6.4).
4. **Acquisition group:**
    - mode: **Stream** / **Triggered capture**;
    - trigger source: Immediate / Level / Sync (Sync is enabled only when the sync wire is in use);
    - level in **mV at the ADC** (converted to counts = round(mV / 1.2207)), with the equivalent input-referred value displayed;
    - pre-trigger (ms) and length (ms);
    - Single / Auto-repeat;
    - Start / Stop buttons;
    - "waiting for trigger…" indicator and an optional timeout.
5. **Measurements group** (§6.6).
6. **Status indicators:**
    - **CLIP** (red; latched for 1 s after any frame with `FLAG_CLIP`, with a "clips: N" counter);
    - gain-stage clip warning (orange, §3.7);
    - OVERFLOW / gaps counter, CRC errors, frame errors.
7. **Save group** (§6.7).

### 6.4 Gain controls

- **Front end** combobox: "JFET LNA (P2 = 1–2)" / "PA-4 +20 dB (P2 = 2–3)". It must match the hardware jumper; show the reminder text.
- **LNA gain** spinbox: 7.0–11.5, default 9.0. Overwritten by the bench calibration.
- **Coarse gain:** radio buttons HIGH (×58) / LOW (×7.1). Sends `GAIN HIGH|LOW`. App default LOW (matches the firmware ARM default).
- **VGA:**
    - slider 0..255 (right = more gain) plus a code spinbox;
    - **and** a "VGA gain [dB]" spinbox that picks the nearest code from the model or measured table (the taper is very non-linear, §3.5);
    - shows "code 192 → ×5.59 (14.9 dB)";
    - sends `POT <D>`, debounced 120 ms (as TX).
- **Analysis frequency** spinbox (kHz; default 40). Set automatically to the detected tone frequency when "auto" is ticked.
- **Big label:** `Total gain @ 40.0 kHz: 63.6 dB (×1512) — ADC full scale = 3.31 mVpp at the hydrophone`. Computed with the §3.6 model × cal factor, or with the measured VGA table if one is loaded.
- **Cal factor** spinbox (default 1.000), plus "Load/Save calibration" (JSON: LNA gain, cal, VGA table, system delay).

### 6.5 Plots (pyqtgraph)

Refresh at ~25 fps from a `QTimer`, never from the serial callback.

- **Time plot:**
    - x in ms (stream: last *span* ms, selectable 1/2/5/10/20/50/100 ms; capture: relative to the trigger, t = 0 at the trigger);
    - y selectable: **V at ADC (AC, code − mid)**, or **input-referred mV** (÷ |G_total(f_analysis)|);
    - min/max envelope decimation to ≤ 4000 points;
    - dashed lines at the clip levels;
    - a `LinearRegionItem` selects the measurement window.
- **FFT:**
    - Hann window, N = 8192 (stream: latest samples; capture: the measurement window, zero-padded if shorter);
    - y in dBV (sine-peak amplitude at the ADC) or dB re input;
    - 0–200 kHz, shaded 20–90 kHz band;
    - optional exponential averaging (N = 1/4/16);
    - peak marker with frequency and level.
- **Spectrogram:**
    - 1024-point Hann, hop 512, magnitude in dB, 0–200 kHz;
    - stream: rolling last 1 s (≈ 780 columns); capture: whole capture;
    - a colormap with fixed dB limits (spinboxes) so levels can be compared over time.
- **Stream ring on the PC:** keep the last 2 s (800 000 samples) in a NumPy ring. Plots read from it. Capture data is stored separately per capture.

### 6.6 Measurements (water tests)

Computed on the measurement window: capture default = trigger → end; stream = latest FFT window.

| Measurement | Definition |
|---|---|
| ADC Vpp | (max − min) × 1.2207 mV |
| ADC Vrms | standard deviation × 1.2207 mV |
| Frequency | FFT peak with parabolic interpolation, Hann window |
| Input Vpp | ADC Vpp ÷ \|G_total(f_peak)\| |
| SPL (optional) | 20·log10(V_in,rms / 1 µV) − RVS_dB, with the hydrophone sensitivity RVS as a user setting (dB re 1 V/µPa; take it from the AS-1 data sheet; the project notes used −206 dB — verify) |
| SNR | rms in the window vs rms of the pre-trigger samples |
| **Delay** | needs TRIG SYNC (§7). (1) **Threshold:** first sample after the trigger with \|x − mid\| ≥ max(6 × pre-trigger noise rms, 20 % of peak). (2) **Cross-correlation:** with a tone-burst template built from the TX settings (freq, on-time, from `tx_state()`), peak of the correlation envelope. Show both, in µs and samples (1 sample = 2.5 µs). |
| Distance | c × (delay − system_delay), with c = 1480 m/s (user setting; ≈ 1450–1500 m/s with temperature) and system_delay from calibration (§7.3) |

- Show the results in a table with a "Copy" button.
- In Auto-repeat mode, also show the mean ± std of delay and Vpp over the last N captures.

### 6.7 Saving

- **Save .npy:**
    - writes `<name>.npy` = uint16 ADC codes, masked to 12 bits;
    - plus `<name>.json` with the metadata below, and a `sync_indices` list;
    - default name `rx_YYYYMMDD_HHMMSS_<f>kHz`.
- **Save .csv:**
    - header lines starting with `#` holding the same metadata as `# key: value`;
    - then the columns `sample,time_s,code,v_adc,v_input,sync`.
- **Metadata keys** (both formats):
    - `timestamp_utc`, `timestamp_local`, `app_version`, `firmware` (PONG text);
    - `fs_hz`, `n`, `pre`, `trigger_source`, `trigger_level_counts`, `trigger_index`, `first_index`, `cap_id`;
    - `coarse_gain`, `pot_code`, `settings_changed_during_capture`;
    - `frontend`, `lna_gain`, `cal_factor`, `vga_gain`, `total_gain_db_at_f`, `analysis_freq_hz`;
    - `adc_va_v` (5.0), `lsb_v`, `mid_code`, `clip_count`, `overflow`, `crc_errors`;
    - `tx_state` (dict, if the TX tab is connected);
    - `measurements` (dict from §6.6), `notes` (free text field in the UI).
- "Save stream snapshot" saves the last N seconds of the PC ring with the same metadata (`trigger_source = none`).

### 6.8 Behaviour

- **On connect:**
    1. `PING` and check `RXFW`;
    2. `STREAM OFF`, `ABORT` (in case a previous session left it running);
    3. push stored settings: `GAIN`, `POT`, `TRIG …`;
    4. `STATUS`.
- Poll `STATUS` every 1 s; it drives the board label and enables or disables the buttons.
- **Button enabling:**
    - Arm only when connected and not armed;
    - Start only when armed;
    - gain controls always enabled (values are stored and applied at ARM).
- **On `ERR`:** show it, forget the last-sent cache, request `STATUS`.
- **On `WARN POWERLOST`:** red banner "RX board power lost — disarmed. Unplug the RX USB before working on the board."
- **On `EVT CAPDONE`:** finalise the capture (assemble frames by `cap_id`, check `n`), update the plots and measurements, and, if Auto-repeat is on, re-issue `CAPTURE` after 100 ms.
- **Missing frames inside a capture** (gap in `first_index`): mark the capture "incomplete" and keep it; never silently splice.

### 6.9 Acceptance criteria (app)

1. Works against the fake RX device (§9.1) and the real board.
2. TX and RX tabs connected at the same time to two ports. Swapping the ports is detected and refused.
3. 10 min of streaming with no UI freezes:
    - plots update at ≥ 20 fps;
    - CPU < 50 % of one core on a typical laptop;
    - a CRC-error/gap counter that stays 0 against the fake device with no injected faults.
4. Injected corruption (fake device option) is detected:
    - CRC errors counted;
    - the parser recovers and the next frames are fine;
    - text replies still arrive.
5. A capture saved to .npy/.json and .csv re-loads to identical samples and metadata (round-trip test).
6. Closing the app leaves the RX board disarmed and streaming off.

---

## 7. Using both boards together — TX→RX sync (recommendation)

### 7.1 Options compared

| Option | How | Timing accuracy | Verdict |
|---|---|---|---|
| **A. Sync wire** (recommended) | TX Teensy pin 2 pulses HIGH at the first DAC sample of every burst. RX Teensy pin 2 is sampled **inside the 400 kHz ISR** (bit 15 of every sample) and can trigger a capture (`TRIG SYNC`). | **1 sample = 2.5 µs** (≈ 3.7 mm of water). No USB jitter. The two crystals' drift does not matter because every burst is re-synchronised. | Simple, exact, cheap. Use for all delay and distance measurements. |
| B. Timestamps / trigger over the two USB ports | The app sends `START` to TX and records the PC time; it maps RX sample indices to PC time with periodic queries. | Windows USB-serial latency jitter is ≈ 1 ms or more: **±1.5 m of water.** | Not usable for 0.3–1 m tanks. Acceptable only for coarse "capture around the time I pressed Start". |
| C. No link | `TRIG LEVEL` on the received signal | Arrival time unknown in absolute terms. Only relative timings (direct path vs reflections) are possible. | Fallback when the wire can't be run. |

### 7.2 Sync wire — hardware and rules

- **Wiring:** TX Teensy **pin 2** → **2.2 kΩ series resistor (at the RX end)** → RX Teensy **pin 2**, as a twisted pair with a GND wire between the two boards' GND.
    - Pin 2 has no net on either PCB, so connect to the Teensy pin itself.
    - The 2.2 kΩ limits current to ≈ 1.3 mA if the RX Teensy is unpowered while TX pulses.
    - Both Teensys normally share ground through the PC's USB anyway. If the extra GND wire adds hum or noise on the RX, try without it.
- **RX:** pin 2 is `INPUT_PULLDOWN`, always safe. It is read in every sample, so `sync_off` and bit 15 give the edge to the sample.
- **TX (firmware v1.1, Appendix C):**
    - pin 2 OUTPUT LOW from `setup()` — safe at any time because it has no net on the TX board;
    - in BURST mode, HIGH for 8 DAC ticks (16 µs) starting at the tick where the burst restarts at phase 0;
    - in CONT mode, one pulse at `START`;
    - enabled by `SYNC ON` / `SYNC OFF`.
- **Delay measured by the RX tab** = acoustic time of flight + electronics/transducer delay. The burst also starts with the TX's 100 µs amplitude ramp (TX spec §4.3); the cross-correlation template must include this ramp.

### 7.3 System-delay calibration

- **Preferred — two distances in water:**
    - measure the delay at two or more distances (e.g. 0.3 m and 0.8 m);
    - the slope of the fit gives 1/c; the intercept gives `system_delay`;
    - save both in the calibration JSON.
- **Sanity check — electrical loopback** (optional, at the bench):
    - TX TP9 (TXLVL_OUT, a few tens of mV around 1.5 V) → 100 nF → 10 kΩ : 100 Ω divider → RX J2;
    - this measures the electronics delay only (the RX band-pass group delay is ≈ 8 µs mid-band and up to ≈ 33 µs near 20/90 kHz, so it depends on frequency);
    - **never** take this signal from AMP_A/AMP_B.

---

## 8. Project layout and build

```
modem-bench/
├─ tx_firmware/tx_firmware.ino     # existing; v1.1 adds SYNC (Appendix C, optional)
├─ rx_firmware/rx_firmware.ino     # NEW (D1)  Teensy 4.1, USB Type: Serial, 600 MHz
├─ modem_ui/
│  ├─ modem_ui.py                  # main window: TX tab + RX tab, two connections
│  ├─ tx_tab.py                    # existing TX code moved here (unchanged behaviour)
│  ├─ rx_link.py  rx_protocol.py  rx_model.py  rx_analysis.py  rx_tab.py   # NEW
│  └─ requirements.txt             # PySide6>=6.5, pyqtgraph>=0.13, numpy>=1.24
└─ tests/
   ├─ host_stubs/                  # Arduino.h / SPI.h / Wire.h stubs (+ RX board model, Appendix B.1)
   ├─ rx_sim_main.cpp              # host build of rx_firmware.ino + scenario checks
   ├─ test_rx_firmware_stream.py   # parses the byte streams the host build produced
   ├─ test_rx_protocol.py  test_rx_model.py  test_rx_analysis.py
   ├─ fake_rx.py                   # fake RX device (Appendix B.2), pty or com0com
   └─ test_rx_ui.py                # app offscreen against the fake device(s)
```

- **Firmware upload:**
    - Arduino IDE 2 + Teensyduino: Board Teensy 4.1, USB Type **Serial**, CPU 600 MHz.
    - Upload with the RX board's 12 V **on** (preferred, per §4.2). A short upload with the board off is harmless: after reset all pins are high-Z and nothing is driven until ARM.
- **App:**
    - `py -m venv .venv`
    - `.venv\Scripts\activate`
    - `pip install -r requirements.txt`
    - `python modem_ui.py`
- **Two Teensys on one PC:** both appear as "USB Serial Device (COMx)" with VID 0x16C0. Tell them apart by serial number (§6.1). Close the Arduino Serial Monitor before connecting the app.

---

## 9. Verification

### 9.1 Without hardware

1. **Host build of the RX firmware** (Appendix B.1). Compile `rx_firmware.ino` as C++ with stub headers, then run the scenarios with the expected results listed there:
    - power-off ARM refused with pins still inputs;
    - wrong bit alignment caught by the self-test;
    - streaming contiguity and CRC;
    - LEVEL- and SYNC-triggered captures exact to the sample;
    - overflow flagging;
    - power-loss watchdog;
    - DISARM.
2. **Python unit tests:**
    - **`rx_protocol`:**
        - encode → parse round trip;
        - CRC equals `zlib.crc32`;
        - frames and text lines interleaved;
        - feeding the stream **one byte at a time** gives identical results;
        - a flipped payload byte → `crc_errors == 1` and the next frame and next text line are still received;
        - garbage bytes before a frame are skipped.
    - **`rx_model`** check values:

        | Quantity | Expected |
        |---|---|
        | `H_BP` | the §3.6 table (±0.02 dB) |
        | `G_VGA_mid(0/128/255)` | 1.471 / 2.891 / 67.77 |
        | Gain stage DC | 58.317 HIGH / 7.102 LOW |
        | Total, HIGH, D = 0, 40 kHz | 57.8 dB (×778) |
        | Total, LOW, D = 255, 40 kHz | 72.5 dB (×4232) |

    - **`rx_analysis`:**
        - a synthetic 40 kHz tone → peak frequency within ±10 Hz;
        - a synthetic burst delayed by 135 samples in noise → threshold and cross-correlation delay = 135 ± 1;
        - Vpp of a known sine within 1 LSB.
3. **Fake RX device + offscreen UI test** (`QT_QPA_PLATFORM=offscreen`):
    - connect TX (fake TX from the TX spec) and RX (fake RX) at the same time;
    - ARM;
    - stream 10 s → no gaps;
    - run a triggered capture → n samples, trigger at `pre`;
    - save → reload identical;
    - with fault injection: CRC errors counted and recovery;
    - with `power_off()`: WARN POWERLOST handled;
    - close → the fake device received `STREAM OFF`, `ABORT`, `DISARM`.

### 9.2 On the bench (RX board, no hydrophone)

Signal source: function generator → **10 kΩ : 100 Ω divider (100:1)** → J2. Generator ground → RX GND.

1. **Power and ARM.** Follow §4.2, then §5.7 checks 1–3. Confirm `ADC_SHIFT` with `RAW` (idle words ≈ 0x1000).
2. **Timing.** `STREAM ON` for 10 min, then `STATS`; §5.7 check 4. Record `per_min_ns`, `per_max_ns` and `isr_max_ns` in the bring-up log.
3. **Coarse gain step.** 100 mVpp at the generator (→ 1 mVpp at J2), 40 kHz, POT 0:

    | Setting | Expected at the ADC (LNA = 9.0) | Accept |
    |---|---|---|
    | HIGH | 0.778 Vpp (≈ 637 counts p-p) | 0.6–1.0 Vpp (JFET spread) |
    | LOW | 94.6 mVpp | ratio HIGH/LOW = **18.3 dB ± 0.5 dB** |

    **Calibrate:** `LNA gain = 9.0 × measured_HIGH / 0.778`. Save it in the calibration JSON.
4. **Oscillation check, both coarse states** (still open from the bring-up log): scope TP5 and TP8 at HIGH and LOW, POT 0 / 128 / 255, input shorted. There must be no MHz oscillation.

    R35 sits in series with U8's −IN, the same pattern as R15. If TP8 oscillates at high codes, the fix is again a capacitor across R35.
5. **Frequency response.** LOW, POT 0, 1 mVpp. Sweep 20–90 kHz, normalised to 60 kHz:

    | f [kHz] | 20 | 30 | 40 | 50 | 70 | 80 | 90 |
    |---|---|---|---|---|---|---|---|
    | Expected [dB] | −4.05 | −0.55 | −0.16 | 0.00 | −0.39 | −1.49 | −3.42 |

    Accept ±1 dB.
6. **VGA calibration.** LOW, 1 mVpp at 40 kHz. Step POT through 0, 32, 64, 96, 128, 160, 192, 208, 224, 232, 240, 248, 252, 255.
    - Record the ADC Vpp (keep it below 4.5 Vpp; lower the input if needed).
    - Store `G_VGA(D) = Vpp(D) / Vpp(0) × 1.471` as the measured table (interpolate in dB between points).
    - Expected (§3.5): 94.6 mVpp at D = 0 … ≈ 2.7–4.2 Vpp at D = 255. The spread comes from the wiper resistance.
7. **Clipping.**
    - Raise the input until the waveform flattens: CLIP turns on at code ≤ 8 / ≥ 4087.
    - Gain-stage warning test: HIGH gain, POT 0, **10 kHz**, ≈ 9 mVpp at J2.
        - GAIN_OUT (TP5) ≈ 4.6 Vpp on the scope.
        - The ADC shows only ≈ 0.37 Vpp because the band-pass is −25 dB at 10 kHz.
        - The orange warning must appear (estimate ≈ 4.6 Vpp).
8. **ADC quality at 400 kSPS.** Use a clean 40 kHz tone giving ≈ 4 Vpp at the ADC (LOW, POT ≈ 192, ≈ 11 mVpp at J2), N = 65 536 capture.
    - **SNR (excluding DC and the first 5 harmonics) ≥ 55 dB.** Generator harmonics do not count.
    - If it is lower: check `STATS` jitter, the ADC framing errors and the SCLK frequency, and consider the v2 FlexPWM + DMA engine (§3.3).
9. **Noise floor**, J2 shorted, rough expectations:

    | Setting | Expected |
    |---|---|
    | LOW, POT 0 | ≈ ADC-limited (≤ 1 LSB rms) |
    | HIGH, POT 255 | order of 10–20 mVrms at the ADC (LNA ≈ 1.6 nV/√Hz over 70 kHz × 35 000) |

    Record the values. Spikes at the buck's PFM frequency (≈ 200–400 kHz, aliased) are possible.
10. **Triggers.** Burst mode on the generator. `TRIG LEVEL` capture shows the burst at t = 0 with the pre-trigger samples quiet.

### 9.3 In water — AS-1 receiving the TX board's S1n

1. **Setup:**
    - S1n (TX) and AS-1 (RX) facing each other, 0.5 m apart, mid-depth, away from the walls;
    - sync wire connected (§7);
    - both boards powered per their procedures; both tabs connected.
2. **TX:** 40 kHz, BURST 1 ms on / 100 ms period, **≈ 2.5 Vpp** (AMP 200, POT 200), `SYNC ON`.
3. **RX:**
    - LOW gain, POT 0;
    - `TRIG SYNC`, capture 8 ms with 0.5 ms pre-trigger, Auto-repeat;
    - raise the gain (POT first, then HIGH if needed) until the direct arrival is **1–3 Vpp at the ADC with no CLIP**.

    Rough expectation: 0.1–3 mVpp at the hydrophone at 0.5 m for 2.5 Vpp drive, depending on frequency (±10 dB; uses the S1n TVR from the TX notes and RVS ≈ −206 dB re 1 V/µPa, unverified).
4. **Delay:** the direct arrival comes ≈ 338 µs (135 samples) + system delay after the sync edge. Reflections from walls and surface follow. Threshold and cross-correlation delays agree within a few samples.
5. **Repeatability:** standard deviation of the delay over 20 captures ≤ 2.5 µs (1 sample).
6. **Distance:** 0.3 / 0.5 / 0.8 m, 5 captures each. Fit delay vs distance:
    - c = 1440–1520 m/s;
    - intercept → `system_delay` (save it).
7. **Frequency sweep:** 20–90 kHz in 10 kHz steps at a fixed TX level (watch the TX thermal guideline). Record the input-referred Vpp. The level should rise strongly toward 90 kHz (S1n TVR ≈ 26–28 dB higher at 90 kHz than at 20 kHz), adjusting the RX gain as needed.
8. **Save:** save a capture set (.npy + .json, and one .csv) and re-load it in Python to confirm the metadata.
9. **Unsynced fallback:** without the sync wire, `TRIG LEVEL` captures still work; the delay field shows "no sync".

---

## 10. Glossary

| Term | Meaning |
|---|---|
| ARM / DISARM | Firmware state in which the Teensy drives the RX board (SPI, gain pin, I2C writes) / releases all board pins |
| Code / D | ADC code 0..4095 / AD5245 wiper code 0..255 (higher = more VGA gain on RX) |
| Coarse gain HIGH / LOW | Gain stage ×58.3 (switch off, hardware default) / ×7.10 (RX_GAIN_SEL high) |
| VGA | Variable-gain amplifier U8 + AD5245 (×1.47..×68 typ) |
| mid | Idle ADC code (≈ 2048) measured at ARM; AC signal = code − mid |
| Frame | CRC-protected binary block of 1..4096 samples with a 32-byte header (§5.3) |
| Record | One ASCII line or one frame in the device→host stream |
| STREAM / CAPTURE | Continuous sending / one triggered window with pre-trigger samples |
| SYNC | Optional TX→RX wire; TX pin 2 pulses at each burst start, sampled by the RX ISR |
| System delay | Electronics + transducer delay, subtracted from the measured delay to get time of flight |
| Input-referred | Value at the hydrophone terminals = value at ADC ÷ \|G_total(f)\| |
| RVS / TVR | Hydrophone receive sensitivity (dB re 1 V/µPa) / transmit voltage response |
| Jitter | Variation of the sampling instant (CS falling edge) |

---

## Appendix A — Normative code fragments

### A.1 Sample ISR core

Checked in the host build.

```cpp
const int PIN_ADC_CS = 28, PIN_SYNC = 2;
const int ADC_SHIFT = 1;                                     // VERIFY AT BRING-UP (RAW, §3.2)
const uint16_t ADC_ZMASK = (uint16_t)(0xFFFFu << (12 + ADC_SHIFT));   // = 0xE000
const uint32_t RING_N = 65536, RING_MASK = RING_N - 1;
static uint16_t ring[RING_N];
volatile uint32_t head = 0;

void adcTick() {                                             // IntervalTimer, 2.5 us, priority(0)
  uint32_t c0 = ARM_DWT_CYCCNT;
  digitalWriteFast(PIN_ADC_CS, LOW);                         // sampling instant
  uint16_t raw = SPI1.transfer16(0);                         // SPI_MODE0, 12 MHz, transaction kept open
  digitalWriteFast(PIN_ADC_CS, HIGH);                        // also tristates the MISO buffer
  uint16_t code = (raw >> ADC_SHIFT) & 0x0FFF;
  uint16_t w = code;
  if (raw & ADC_ZMASK) { w |= 0x4000; ferrCount++; }         // framing error
  bool s = digitalReadFast(PIN_SYNC);
  if (s) w |= 0x8000;                                        // SYNC level
  uint32_t h = head;
  ring[h & RING_MASK] = w;
  if (trigState == TRIG_WAIT && (int32_t)(h - trigEarliest) >= 0) {
    bool hit = (trigSrc == SRC_LEVEL) ? (abs((int)code - (int)trigMid) >= trigLevel)
             : (trigSrc == SRC_SYNC)  ? (s && !lastSync) : true;
    if (hit) { trigIndex = h; trigState = TRIG_HIT; }
  }
  lastSync = s;
  head = h + 1;
  uint32_t per = c0 - lastCyc; lastCyc = c0;                 // timing statistics (STATS)
  if (per < perMin) perMin = per;
  if (per > perMax) perMax = per;
  uint32_t dur = ARM_DWT_CYCCNT - c0; if (dur > isrMax) isrMax = dur;
}
// setup(): ARM_DEMCR |= ARM_DEMCR_TRCENA; ARM_DWT_CTRL |= ARM_DWT_CTRL_CYCCNTENA;
```

### A.2 SPI1 / I2C bring-up inside ARM

```cpp
Wire2.begin(); Wire2.setClock(100000);
if (Wire2.requestFrom((uint8_t)0x2C, (uint8_t)1) != 1) { releasePins(); return "ERR NOPOWER (...)"; }
(void)Wire2.read();
Wire2.beginTransmission(0x2C); Wire2.write((uint8_t)0x00); Wire2.write((uint8_t)potCode);
if (Wire2.endTransmission() != 0) { releasePins(); return "ERR NOPOT (...)"; }
Wire2.setClock(400000);
pinMode(10, OUTPUT); digitalWriteFast(10, gainLow ? HIGH : LOW);   // HIGH = LOW gain
pinMode(28, OUTPUT); digitalWriteFast(28, HIGH);
SPI1.setMISO(39);                                                  // before begin()!
SPI1.begin();
SPI1.beginTransaction(SPISettings(12000000, MSBFIRST, SPI_MODE0));
// ... discard 2 conversions, self-test 64 conversions (§4.3), then:
adcTimer.begin(adcTick, 2.5f); adcTimer.priority(0);
```

---

## Appendix B — Host test harness and fake RX device

### B.1 Host build of the firmware

Build:

- copy `rx_firmware.ino` to `fw.cpp`;
- `g++ -std=c++17 -I host_stubs -o rx_sim rx_sim_main.cpp`;
- `rx_sim_main.cpp` `#include`s `fw.cpp` after defining the stub globals.

**The stubs model the RX board:**

| Stub | Behaviour |
|---|---|
| `g_power` | When false: the AD5245 NACKs (`requestFrom` returns 0, `endTransmission` returns 2) and SPI reads return 0 (floating ADC). |
| `SPI1.transfer16()` | Returns `code << 1 \| random_bit`: the real mode-0 alignment. A switch sets a wrong alignment (`code << 0`) to test the self-test. It prints an error if pin 28 is not LOW, and records whether `setMISO(39)` was called. |
| `g_sig(k)` | ADC code for sample k (e.g. DC 2050 plus a 40 kHz, 400-count burst at samples 60 000–60 799). |
| `g_sync(k)` | SYNC level for sample k. |
| `Serial` | Collects every byte written (text and frames) into a buffer that the test dumps to `out_*.bin`. `dtr()` is controllable. |
| `ARM_DWT_CYCCNT` | Advances by 1500 cycles per read. |
| `millis()` | Derived from the sample count (400 samples per ms). |

**The test driver:**

- calls `adcTick()` once per simulated sample;
- calls `loop()` every 256 samples (or never, to force an overflow).

**Scenarios and expected results.** A prototype of this firmware passed all of these, with the Python stream checks below.

| # | Scenario | Expected |
|---|---|---|
| 1 | `ARM` with `g_power = false` | `ERR NOPOWER …`; pins 10/24/25/27/28/39 = INPUT; `STATUS` → `power=0 armed=0`, pins released again |
| 2 | `ARM` with wrong alignment | `ERR ADC self-test failed: mean=1025 …`; pins released |
| 3 | `ARM` normal (DC 2050) | `OK ARMED mid=2050`; `setMISO(39)` called; pot written 0; pin 10 OUTPUT HIGH (LOW gain); `GAIN HIGH` → pin 10 LOW; `POT 200` / `POTREAD` → 200; `POT 256` → ERR; `RAW 4` → `OK RAW 1004 1005 …` |
| 4 | `STREAM ON`, 20 480 samples, `STREAM OFF` | 20 STREAM frames × 1024; contiguous `first_index`; `seq` +1 each; payload all 2050; `OK STREAM OFF` parsed from between frames; 0 CRC errors |
| 5 | `TRIG LEVEL 300`, `CAPTURE 5000 1000` | trigger at the first sample with \|x − 2050\| ≥ 300 (sample 60 002); first frame starts at 59 002; 5000 samples identical to `g_sig`; `CAP_FIRST`/`CAP_LAST` flags; `EVT TRIG id=1`, `EVT CAPDONE id=1` |
| 6 | `TRIG SYNC`, `CAPTURE 3000 100`, sync high at samples 80 000–80 007 | trigger index 80 000; capture starts at 79 900; `sync_off` points to 80 000; exactly 8 payload words have bit 15 set |
| 7 | `STREAM ON`, 100 000 samples without calling `loop()` | first frame after the stall has `FLAG_OVERFLOW`; later frames contiguous; `STATS` reports `ovf > 0` |
| 8 | Power removed while armed, 1.2 s simulated | `WARN POWERLOST …`; disarmed; pins released |
| 9 | `DISARM`; then `CAPTURE`; then an unknown command | `OK DISARMED`, pins released; `ERR NOTARMED`; `ERR unknown command: …` |

### B.2 Fake RX device (`tests/fake_rx.py`)

A Python implementation of the same protocol, used by the app tests and for UI development.

- **Transports:**
    - Linux/WSL: a pty (`os.openpty()`; QSerialPort opens the slave path);
    - Windows: one end of a com0com virtual pair;
    - also an in-process `feed(bytes) -> bytes` API for pure unit tests.
- **State machine:**
    - all commands in §5.2, with the same reply texts;
    - `PONG RXFW 1.0-fake`;
    - `power` flag (ARM refused when off; `power_off()` while armed → `WARN POWERLOST` within 1 s).
- **Signal generator** (configurable):
    - noise σ = 2 counts around mid = 2048;
    - tone bursts (freq, on-time, period, start offset);
    - input amplitude in **volts at the hydrophone**, converted to codes with the §3.6 model at the current GAIN/POT, so gain changes visibly change the plot; values clipped to 0..4095;
    - an acoustic delay (default 135 samples) after a simulated SYNC pulse at each burst start.
- **Real-time pacing:** a thread emits 1024-sample frames according to the wall clock (400 kSPS) while streaming or capturing.
- **Fault injection options:**
    - flip a random payload byte every N frames (CRC test);
    - insert garbage bytes between records;
    - drop frames (gap test);
    - stall for 300 ms (overflow-flag test);
    - reply as `PONG TXFW` (wrong-port test).
- **Records** every command it received, so tests can assert the shutdown sequence.

---

## Appendix C — TX firmware v1.1: SYNC output (optional)

This is a small, backward-compatible change to `tx_firmware.ino` from the TX spec. Pin 2 has no net on the TX PCB, so driving it is always safe. **VERIFY:** confirm on the TX schematic that Teensy pin 2 is unconnected; the TX pin map lists only pins 3, 8 and 24–28.

```cpp
const int PIN_SYNC_OUT = 2;
volatile bool    syncEnable = false;
volatile bool    syncPending = false;   // CONT mode: one pulse at START
volatile uint8_t syncCnt = 0;

// setup():   pinMode(PIN_SYNC_OUT, OUTPUT); digitalWriteFast(PIN_SYNC_OUT, LOW);

// dacTick(), at the top:
if (syncCnt && --syncCnt == 0) digitalWriteFast(PIN_SYNC_OUT, LOW);
// ... in the existing burst block:
//   if (burstCnt == 0) { phase = 0;
//                        if (syncEnable) { digitalWriteFast(PIN_SYNC_OUT, HIGH); syncCnt = 8; } }
// ... for continuous mode:
//   if (running && !burstMode && syncPending) { syncPending = false;
//        if (syncEnable) { digitalWriteFast(PIN_SYNC_OUT, HIGH); syncCnt = 8; } }

// commands:  SYNC ON | SYNC OFF   -> OK SYNC ON / OK SYNC OFF
//            START sets syncPending = true;  STATUS adds " sync=0|1";  PONG TXFW 1.1
```

The pulse lasts 8 DAC ticks (16 µs), so the RX (2.5 µs sampling) always sees it. The rising edge coincides with the DAC tick that starts the burst at phase 0. The TX amplitude ramp (100 µs) follows, and the RX delay template includes it.
