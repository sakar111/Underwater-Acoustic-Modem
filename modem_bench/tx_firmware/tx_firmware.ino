// =====================================================================
//  TX board bench-test firmware  —  Teensy 4.1
//  Underwater acoustic modem, transmitter board
//
//  Generates a sine wave on the DAC8830 (500 kHz update rate) and sets
//  the TX level through the AD5245 digital pot. Controlled over USB
//  serial with simple text commands (one per line), e.g. from the
//  Python UI (modem_ui/modem_ui.py) or the Arduino Serial Monitor.
//
//  Build: Arduino IDE 2 + Teensyduino. Board: Teensy 4.1, USB Type: Serial,
//         CPU Speed: 600 MHz.
//
//  SAFETY (spec §4): the Teensy does not drive any board pin until ARM
//  succeeds. ARM first checks that the board's +3V3 rail is up (the I2C
//  pull-ups go to the BOARD's 3V3 and read high), so a Teensy left on USB
//  with the 12 V supply off never back-powers the board through the
//  DAC/pot input pins. While armed, the pot is read back every 500 ms;
//  if it stops answering (12 V switched off) the pins are released at
//  once, and if it has reset to mid-scale (supply glitch) it is rewritten.
//  The watchdog is best effort: back-feed through the driven SPI pins may
//  keep the pot alive after 12 V is removed, so the user procedure
//  (Stop -> Disarm -> 12 V off) still applies.
//
//  Commands (case-insensitive; replies start with OK / ERR / WARN /
//  STATUS / PONG; one reply line per command plus optional WARN lines):
//    PING                       -> PONG TXFW <version>
//    VERSION                    -> OK VERSION TXFW <version> ...
//    HELP                       -> OK commands: ...
//    ARM                        -> check board power, init DAC + pot
//    DISARM                     -> stop, park DAC at 0 V, release all pins
//    FREQ <Hz>                  -> 100 .. 100000
//    AMP <mV peak at DAC>       -> 0 .. 1400 (around the 1.5 V mid-scale)
//    POT <code>                 -> 0 .. 255   (255 = minimum gain)
//    LIMIT <Vpp>                -> 1 .. 50, max allowed estimated output
//    CAL <factor>               -> 0.5 .. 2.0, scales the estimate (scope cal)
//    MODE CONT
//    MODE BURST <on_us> <period_us>
//    SWEEP <f1> <f2> <ms> | SWEEP OFF   (repeating linear sweep while running)
//    THERMSTOP <s>              -> 0 .. 3600, auto-stop after s seconds above
//                                  the continuous thermal guideline (0 = off)
//    POTREAD                    -> OK POTREAD <code> (reads the wiper over I2C)
//    START / STOP
//    STATUS
// =====================================================================
#include <SPI.h>
#include <Wire.h>
#include <math.h>

#define TXFW_VERSION "1.1"

// ---------------- pins (TX schematic, spec §2.3) ----------------
const int PIN_DAC_CS = 28;   // SPI1_CS (plain GPIO, R18 10k pull-up to board 3V3)
const int PIN_MOSI1  = 26;   // SPI1_MOSI
const int PIN_SCK1   = 27;   // SPI1_SCK
const int PIN_SCL2   = 24;   // I2C2_SCL (4.7k pull-up to board +3V3)
const int PIN_SDA2   = 25;   // I2C2_SDA (4.7k pull-up to board +3V3)
const int PIN_LED_TX = 3;    // LED_TX
const int PIN_LED_HB = 8;    // LED_HB (heartbeat)

// ---------------- hardware constants ----------------
const float    FS_HZ          = 500000.0f;  // DAC update rate
const float    TICK_US        = 2.0f;       // 1 / FS
const float    VREF_V         = 3.0f;       // REF2030 output
const uint16_t DAC_MID        = 32768;      // 1.5 V
const float    MAX_DAC_MVPK   = 1400.0f;    // keep buffer away from 0 V / 3 V rails
const float    PA_GAIN        = 27.31f;     // power-amp differential gain
const float    POT_RAB        = 10000.0f;   // AD5245 end-to-end
const float    POT_RW         = 50.0f;      // wiper resistance (typ, 120 max); R_WA uses 2*R_W
const float    R21_OHM        = 10000.0f;   // level-control input resistor
const uint8_t  POT_ADDR       = 0x2C;       // AD5245, AD0 = GND
const uint8_t  POT_MIN_GAIN   = 255;
const float    ABS_MAX_VPP    = 50.0f;      // S1n continuous rating
const float    THERMAL_FVPP   = 2.3e6f;     // continuous f*Vpp guideline (OPA55x SOIC)
const int32_t  RAMP_SAMPLES   = 50;         // 100 us amplitude ramp (burst edges)
const float    CAL_MIN        = 0.5f;
const float    CAL_MAX        = 2.0f;
const float    LIMIT_EPS      = 1e-3f;
const uint32_t BOARD_CHECK_MS = 500;        // pot read-back watchdog while armed
const unsigned MAX_LINE       = 90;
const float    K_TWO_PI       = 6.283185307f;

// ---------------- sine table ----------------
static int16_t sineTab[1024];               // Q15

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

// ---------------- settings (main loop only) ----------------
bool     armed        = false;
float    freqHz       = 25000.0f;
float    ampMvpk      = 200.0f;
int      potCode      = 255;        // user setting (written to the pot while armed)
float    limitVpp     = 20.0f;
float    calFactor    = 1.0f;
uint32_t burstOnUs    = 1000;
uint32_t burstPerUs   = 100000;
bool     sweepOn      = false;
float    sweepF1      = 20000.0f;
float    sweepF2      = 90000.0f;
uint32_t sweepMs      = 100;
uint32_t sweepT0      = 0;
uint32_t thermalStopS = 60;

// ---------------- runtime bookkeeping ----------------
bool     thermalActive = false;
uint32_t thermalT0     = 0;
uint32_t lastBoardChk  = 0;
int      potHw         = -1;        // value last written to the AD5245 (-1 = unknown)
int      potFailCount  = 0;

IntervalTimer dacTimer;
SPISettings   dacSpi(20000000, MSBFIRST, SPI_MODE0);  // DAC8830: 16-bit word, latched on SCLK rising edge

// =====================================================================
//  Output helper
// =====================================================================
static void out(const String &s) { Serial.println(s); }

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
  Wire2.write((uint8_t)0x00);            // instruction byte: normal write (never SD on this board)
  Wire2.write(d);
  bool ok = Wire2.endTransmission() == 0;
  if (ok) potHw = d;
  return ok;
}

bool potRead(uint8_t &d) {
  if (Wire2.requestFrom(POT_ADDR, (uint8_t)1) != 1) return false;
  if (!Wire2.available()) return false;
  d = (uint8_t)Wire2.read();
  return true;
}

float potGain(int d) {                   // |gain| of the level-control stage
  float rwa = (256.0f - d) / 256.0f * POT_RAB + 2.0f * POT_RW;   // AD5245 eq. 2
  return rwa / R21_OHM;
}

float estVppCal(float mvpk, int pot, float cal) {  // estimated differential Vpp across the transducer
  return 2.0f * (mvpk / 1000.0f) * potGain(pot) * PA_GAIN * cal;
}

float estVpp(float mvpk, int pot) { return estVppCal(mvpk, pot, calFactor); }

bool overLimit(float est, float lim) { return est > lim + LIMIT_EPS; }

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

float maxFreq() {
  if (!sweepOn) return freqHz;
  return sweepF1 > sweepF2 ? sweepF1 : sweepF2;
}

bool thermalRisk() {
  return !burstMode && maxFreq() * estVpp(ampMvpk, potCode) > THERMAL_FVPP;
}

String thermalWarning() {
  return "WARN THERMAL continuous f*Vpp above ~2.3e6: power amps may overheat; use BURST or lower level";
}

// =====================================================================
//  500 kHz sample ISR  (no floats, no Serial, no I2C)
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

void setPhaseStep(float f) {
  phaseStep = (uint32_t)llround((double)f / FS_HZ * 4294967296.0);
}

void applyFreq() { setPhaseStep(sweepOn ? sweepF1 : freqHz); }

void applyAmp() {
  int32_t c = mvToCodes(ampMvpk);
  int32_t r = c / RAMP_SAMPLES;
  rampStep  = r < 1 ? 1 : r;
  ampSet    = c;
}

// startInOff: used when bursts are (re)configured while already transmitting.
// Starting in the off part lets the amplitude ramp to zero before the next
// burst resets the phase, so there is no click at the switch-over.
void applyBurst(bool startInOff) {
  uint32_t on  = burstOnUs / 2;
  uint32_t per = burstPerUs / 2;
  if (on < 1) on = 1;
  if (per <= on) per = on + 1;
  noInterrupts();
  burstOnN = on; burstPerN = per; burstCnt = startInOff ? on : 0;
  interrupts();
}

void setRunning(bool r) {
  running = r;
  digitalWriteFast(PIN_LED_TX, r ? HIGH : LOW);
  if (!r) thermalActive = false;
}

// =====================================================================
//  ARM / DISARM
// =====================================================================
String doArm() {
  if (armed) return "OK ARMED";
  if (!boardPowered()) { releasePins(); return "ERR NOPOWER (turn the 12 V supply on, then ARM)"; }

  Wire2.begin();
  Wire2.setClock(400000);
  if (!potWrite(POT_MIN_GAIN)) {
    Wire2.end();
    releasePins();
    potHw = -1;
    return "ERR NOPOT (AD5245 did not answer on I2C)";
  }

  pinMode(PIN_DAC_CS, OUTPUT);
  digitalWriteFast(PIN_DAC_CS, HIGH);
  SPI1.begin();
  SPI1.beginTransaction(dacSpi);         // SPI1 is used only by the DAC: keep it open
  delayMicroseconds(20);

  // Ramp 0 V -> 1.5 V with the pot at minimum gain (spec §4.3), ~51 ms.
  dacWrite(0);                           // shift register is not cleared at power-up
  for (uint32_t c = 0; c <= DAC_MID; c += 64) { dacWrite((uint16_t)c); delayMicroseconds(100); }

  potWrite((uint8_t)potCode);
  setRunning(false);
  ampCur = 0; phase = 0;
  applyFreq(); applyAmp(); applyBurst(false);
  dacTimer.begin(dacTick, TICK_US);
  dacTimer.priority(32);
  armed = true;
  potFailCount = 0;
  lastBoardChk = millis();
  return "OK ARMED";
}

String doDisarm() {
  if (!armed) { releasePins(); return "OK DISARMED"; }
  setRunning(false);
  delay(5);                              // ISR ramps the amplitude to 0 (100 us)
  dacTimer.end();
  potWrite(POT_MIN_GAIN);                // minimum gain before the DC ramp (spec §4.3)
  for (int32_t c = DAC_MID; c >= 0; c -= 64) { dacWrite((uint16_t)c); delayMicroseconds(100); }
  dacWrite(0);
  SPI1.endTransaction();
  SPI1.end();
  Wire2.end();
  releasePins();
  armed = false;
  return "OK DISARMED";
}

// Board stopped answering while armed (12 V switched off): stop driving
// the pins immediately. No ramp — there is nothing powered to ramp.
void emergencyRelease() {
  setRunning(false);
  dacTimer.end();
  SPI1.endTransaction();
  SPI1.end();
  Wire2.end();
  releasePins();
  armed = false;
  potHw = -1;
}

void checkBoard() {
  uint8_t d = 0;
  if (!potRead(d)) {
    if (++potFailCount >= 2) {
      emergencyRelease();
      out("WARN NOPOWER board stopped answering on I2C (12 V off?): disarmed, pins released");
    }
    return;
  }
  potFailCount = 0;
  if (potHw >= 0 && d != (uint8_t)potHw) {
    int want = potHw;
    potWrite((uint8_t)want);
    out(String("WARN POTRESET wiper read ") + String((int)d) + " expected " + String(want) +
        " (board supply glitch?): rewritten");
  }
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
  s += " on_us=";    s += (unsigned long)burstOnUs;
  s += " per_us=";   s += (unsigned long)burstPerUs;
  s += " est=";      s += String(estVpp(ampMvpk, potCode), 2);
  s += " limit=";    s += String(limitVpp, 1);
  s += " power=";    s += armed ? 1 : (boardPowered() ? 1 : 0);
  if (!armed) releasePins();
  // extensions (appended, backward compatible)
  s += " cal=";      s += String(calFactor, 3);
  s += " sweep=";    s += sweepOn ? 1 : 0;
  s += " tstop=";    s += (unsigned long)thermalStopS;
  return s;
}

void handle(String line) {
  line.trim();
  if (line.length() == 0) return;
  String up = line; up.toUpperCase();

  char buf[96];
  up.toCharArray(buf, sizeof(buf));
  char *cmd = strtok(buf, " \t");
  char *a1  = strtok(nullptr, " \t");
  char *a2  = strtok(nullptr, " \t");
  char *a3  = strtok(nullptr, " \t");
  String c(cmd ? cmd : "");

  bool riskBefore = running && thermalRisk();

  if (c == "PING")         out("PONG TXFW " TXFW_VERSION);
  else if (c == "VERSION") out("OK VERSION TXFW " TXFW_VERSION " fs=500000 dac=DAC8830 pot=AD5245");
  else if (c == "HELP")    out("OK commands: PING VERSION ARM DISARM FREQ AMP POT LIMIT CAL MODE SWEEP THERMSTOP POTREAD START STOP STATUS");
  else if (c == "STATUS")  out(statusLine());
  else if (c == "ARM")     out(doArm());
  else if (c == "DISARM")  out(doDisarm());
  else if (c == "FREQ") {
    if (!a1) { out("ERR FREQ needs a value"); return; }
    float f = atof(a1);
    // Range checks are written as !(in range) so that NaN (atof("NAN")) is rejected too.
    if (!(f >= 100 && f <= 100000)) { out("ERR FREQ out of range 100..100000"); return; }
    freqHz = f;
    if (!sweepOn) applyFreq();
    out(String("OK FREQ ") + String((long)lroundf(f)));
  }
  else if (c == "AMP") {
    if (!a1) { out("ERR AMP needs a value"); return; }
    float m = atof(a1);
    if (!(m >= 0 && m <= MAX_DAC_MVPK)) { out("ERR AMP out of range 0..1400"); return; }
    if (overLimit(estVpp(m, potCode), limitVpp)) { out("ERR LIMIT estimated output would exceed limit"); return; }
    ampMvpk = m; applyAmp();
    out(String("OK AMP ") + String((long)lroundf(m)));
  }
  else if (c == "POT") {
    if (!a1) { out("ERR POT needs a value"); return; }
    int d = atoi(a1);
    if (d < 0 || d > 255) { out("ERR POT out of range 0..255"); return; }
    if (overLimit(estVpp(ampMvpk, d), limitVpp)) { out("ERR LIMIT estimated output would exceed limit"); return; }
    if (armed && !potWrite((uint8_t)d)) { out("ERR NOPOT (I2C write failed)"); return; }
    potCode = d;
    out(String("OK POT ") + String(d));
  }
  else if (c == "LIMIT") {
    if (!a1) { out("ERR LIMIT needs a value"); return; }
    float v = atof(a1);
    if (!(v >= 1 && v <= ABS_MAX_VPP)) { out("ERR LIMIT out of range 1..50"); return; }
    if (overLimit(estVpp(ampMvpk, potCode), v)) { out("ERR LIMIT current settings already exceed it"); return; }
    limitVpp = v;
    out(String("OK LIMIT ") + String(v, 1));
  }
  else if (c == "CAL") {
    if (!a1) { out("ERR CAL needs a value"); return; }
    float k = atof(a1);
    if (!(k >= CAL_MIN && k <= CAL_MAX)) { out("ERR CAL out of range 0.5..2.0"); return; }
    if (overLimit(estVppCal(ampMvpk, potCode, k), limitVpp)) { out("ERR LIMIT estimated output would exceed limit"); return; }
    calFactor = k;
    out(String("OK CAL ") + String(k, 3));
  }
  else if (c == "MODE") {
    String m(a1 ? a1 : "");
    if (m == "CONT") { burstMode = false; out("OK MODE CONT"); }
    else if (m == "BURST") {
      if (!a2 || !a3) { out("ERR MODE BURST needs <on_us> <period_us>"); return; }
      long on = atol(a2), per = atol(a3);
      if (on < 10 || per <= on || per > 10000000) { out("ERR MODE BURST needs 10 <= on_us < period_us <= 10000000"); return; }
      burstOnUs = on; burstPerUs = per; applyBurst(running); burstMode = true;
      out(String("OK MODE BURST ") + String(on) + " " + String(per));
    }
    else { out("ERR MODE needs CONT or BURST"); return; }
  }
  else if (c == "SWEEP") {
    String m(a1 ? a1 : "");
    if (m == "OFF") { sweepOn = false; applyFreq(); out("OK SWEEP OFF"); }
    else {
      if (!a1 || !a2 || !a3) { out("ERR SWEEP needs <f1> <f2> <ms> or OFF"); return; }
      float f1 = atof(a1), f2 = atof(a2);
      long ms = atol(a3);
      if (!(f1 >= 100 && f1 <= 100000 && f2 >= 100 && f2 <= 100000)) { out("ERR SWEEP frequencies out of range 100..100000"); return; }
      if (ms < 1 || ms > 600000) { out("ERR SWEEP time out of range 1..600000 ms"); return; }
      sweepF1 = f1; sweepF2 = f2; sweepMs = (uint32_t)ms; sweepOn = true;
      sweepT0 = micros();
      applyFreq();
      out(String("OK SWEEP ") + String((long)lroundf(f1)) + " " + String((long)lroundf(f2)) + " " + String(ms));
    }
  }
  else if (c == "THERMSTOP") {
    if (!a1) { out("ERR THERMSTOP needs a value"); return; }
    long sec = atol(a1);
    if (sec < 0 || sec > 3600) { out("ERR THERMSTOP out of range 0..3600"); return; }
    thermalStopS = (uint32_t)sec;
    out(String("OK THERMSTOP ") + String(sec));
  }
  else if (c == "POTREAD") {
    if (!armed) { out("ERR NOTARMED"); return; }
    uint8_t d = 0;
    if (!potRead(d)) { out("ERR NOPOT (I2C read failed)"); return; }
    out(String("OK POTREAD ") + String((int)d));
  }
  else if (c == "START") {
    if (!armed) { out("ERR NOTARMED"); return; }
    if (overLimit(estVpp(ampMvpk, potCode), limitVpp)) { out("ERR LIMIT"); return; }
    if (!running) {
      if (burstMode) applyBurst(false);
      if (sweepOn) { sweepT0 = micros(); applyFreq(); }
    }
    setRunning(true);
    out("OK START");
  }
  else if (c == "STOP") {
    setRunning(false);
    if (sweepOn) applyFreq();
    out("OK STOP");
  }
  else { out(String("ERR unknown command: ") + c); return; }

  // Thermal guideline (spec §4.2): warn once when continuous output enters the risky region.
  if (running && thermalRisk() && !riskBefore) out(thermalWarning());
}

// =====================================================================
void setup() {
  releasePins();                         // never drive the board before ARM
  pinMode(PIN_LED_TX, OUTPUT); digitalWriteFast(PIN_LED_TX, LOW);
  pinMode(PIN_LED_HB, OUTPUT); digitalWriteFast(PIN_LED_HB, LOW);
  for (int i = 0; i < 1024; i++)
    sineTab[i] = (int16_t)lroundf(32767.0f * sinf(K_TWO_PI * i / 1024.0f));
  applyFreq();
  applyAmp();
  Serial.begin(115200);
}

void loop() {
  static String   rx;
  static bool     rxOverflow = false;
  static uint32_t lastHb = 0;

  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (rxOverflow) out("ERR line too long");
      else if (rx.length()) handle(rx);
      rx = "";
      rxOverflow = false;
    } else if (rx.length() < MAX_LINE) {
      rx += ch;
    } else {
      rxOverflow = true;
    }
  }

  uint32_t now = millis();

  // Board watchdog: the pot must keep answering while armed.
  if (armed && now - lastBoardChk >= BOARD_CHECK_MS) {
    lastBoardChk = now;
    checkBoard();
  }

  // Repeating linear sweep (phase step updated from the main loop; ISR stays minimal).
  if (armed && running && sweepOn) {
    uint32_t perUs = sweepMs * 1000UL;
    uint32_t t = (micros() - sweepT0) % perUs;
    setPhaseStep(sweepF1 + (sweepF2 - sweepF1) * ((float)t / (float)perUs));
  }

  // Thermal auto-stop for continuous operation above the guideline.
  if (running && thermalRisk()) {
    if (!thermalActive) { thermalActive = true; thermalT0 = now; }
    else if (thermalStopS > 0 && now - thermalT0 >= thermalStopS * 1000UL) {
      setRunning(false);
      out(String("WARN THERMAL auto-stop after ") + String((unsigned long)thermalStopS) +
          " s of continuous output above the guideline");
    }
  } else {
    thermalActive = false;
  }

  if (now - lastHb >= (armed ? 125u : 500u)) {   // heartbeat: slow when idle, fast when armed
    lastHb = now;
    digitalToggleFast(PIN_LED_HB);
  }
}
