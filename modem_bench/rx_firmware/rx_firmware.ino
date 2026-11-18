// =====================================================================
//  RX board bench-test firmware  —  Teensy 4.1
//  Underwater acoustic modem, receiver board  (spec: RX_Software_Spec.md)
//
//  Samples the ADC121S101 at 400 kSPS from a top-priority IntervalTimer
//  ISR (one 16-bit SPI1 transfer per tick, software CS on pin 28) into a
//  65 536-sample ring, and sends the samples to the PC as CRC-protected
//  binary frames (continuous STREAM, or triggered CAPTURE). Sets the
//  coarse gain (TS5A3166 on pin 10) and the VGA (AD5245 on I2C2).
//
//  Build: Arduino IDE 2 + Teensyduino. Board: Teensy 4.1, USB Type: Serial,
//         CPU Speed: 600 MHz.
//
//  SAFETY (spec §4): every board pin (10, 24, 25, 26, 27, 28, 39) is an
//  INPUT until ARM. ARM first reads the AD5245 over I2C: it can only ACK
//  when the board's 5 V rail is up (the I2C pull-ups go to the TEENSY's
//  3.3 V on this board, so SDA/SCL levels say nothing about board power).
//  Then the ADC self-test proves 5 V, the 2V5 bias, the SPI wiring, the
//  MISO pin and the bit alignment. While armed the pot is read every
//  500 ms; two missed ACKs release every pin (WARN POWERLOST).
//
//  Output stream: ASCII lines (printable, '\n'-terminated) and binary
//  frames (start with 0xA5, which never appears in a line). Everything is
//  written from loop() only, one complete record at a time, so a line can
//  never land inside a frame.
//
//  Commands (one per line, case-insensitive):
//    PING                      -> PONG RXFW <version>
//    VERSION | HELP
//    ARM | DISARM | STATUS
//    GAIN HIGH | GAIN LOW      coarse gain (LOW = pin 10 HIGH)
//    POT <0..255>              VGA code, 0 = minimum gain
//    POTREAD                   read the wiper back (armed)
//    TRIG NONE | TRIG LEVEL <1..2047> | TRIG SYNC
//    CAPTURE <n> [pre]         n = 16..4000000, pre = 0..min(32768, n-1)
//    ABORT
//    STREAM ON | STREAM OFF
//    RAW [1..16]               raw SPI words in hex (bring-up)
//    STATS                     counters + ISR timing since the last STATS
// =====================================================================
#include <SPI.h>
#include <Wire.h>

#define RXFW_VERSION "1.0"

// ---------------- pins (RX schematic, spec §2.3) ----------------
const int PIN_ADC_CS   = 28;   // SPI1_CS: ADC CS and MISO-buffer /OE (plain GPIO)
const int PIN_MOSI1    = 26;   // not connected on the RX board (SPI1 clocks it anyway)
const int PIN_SCK1     = 27;   // SPI1_SCK
const int PIN_MISO1    = 39;   // SPI1 MISO, ALTERNATE pin (default is 1): setMISO(39)!
const int PIN_SCL2     = 24;   // I2C2 SCL (4.7k pull-up to the TEENSY's 3.3 V)
const int PIN_SDA2     = 25;   // I2C2 SDA (4.7k pull-up to the TEENSY's 3.3 V)
const int PIN_GAIN_SEL = 10;   // RX_GAIN_SEL: HIGH = LOW gain, LOW/undriven = HIGH gain
const int PIN_LED_TX   = 3;    // "capturing / streaming"
const int PIN_LED_HB   = 8;    // heartbeat
const int PIN_SYNC     = 2;    // optional TX->RX sync wire (Teensy pin only)

// ---------------- hardware / protocol constants ----------------
const int      ADC_SHIFT   = 1;                                   // VERIFY AT BRING-UP (RAW, §3.2)
const uint16_t ADC_ZMASK   = (uint16_t)(0xFFFFu << (12 + ADC_SHIFT));   // = 0xE000
const uint8_t  POT_ADDR    = 0x2C;                                // AD5245, AD0 = GND
const uint32_t RING_N      = 65536;                               // 164 ms at 400 kSPS
const uint32_t RING_MASK   = RING_N - 1;
const uint32_t BLOCK       = 1024;                                // samples per frame
const uint32_t FRAMES_PER_LOOP = 8;                               // keep commands responsive
const uint16_t CLIP_LO     = 8;
const uint16_t CLIP_HI     = 4087;
const uint16_t MID_MIN     = 1648;                                // self-test window (2.5 V +- 0.49 V)
const uint16_t MID_MAX     = 2448;
const uint32_t CAP_N_MIN   = 16;
const uint32_t CAP_N_MAX   = 4000000;
const uint32_t CAP_PRE_MAX = 32768;
const uint32_t BOARD_CHECK_MS = 500;
const unsigned MAX_LINE    = 90;

const uint8_t  FT_STREAM = 1, FT_CAPTURE = 2;
const uint16_t FLAG_CLIP = 0x0001, FLAG_OVERFLOW = 0x0002, FLAG_CAP_FIRST = 0x0004,
               FLAG_CAP_LAST = 0x0008, FLAG_FRAME_ERR = 0x0010, FLAG_SYNC = 0x0020;
const uint16_t NO_OFFSET = 0xFFFF;

enum { TRIG_OFF = 0, TRIG_WAIT = 1, TRIG_HIT = 2 };
enum { SRC_NONE = 0, SRC_LEVEL = 1, SRC_SYNC = 2 };
enum { CAP_IDLE = 0, CAP_WAIT = 1, CAP_RUN = 2 };

// ---------------- state shared with the ISR ----------------
static uint16_t ring[RING_N];
volatile uint32_t head = 0;              // samples written since ARM (wraps after ~2.98 h)
volatile uint32_t ferrCount = 0;         // ADC framing errors since ARM
volatile uint8_t  trigState = TRIG_OFF;
volatile uint8_t  trigSrc = SRC_NONE;
volatile uint32_t trigEarliest = 0;
volatile uint16_t trigMid = 2048;
volatile uint16_t trigLevel = 200;
volatile uint32_t trigIndex = 0;
volatile bool     lastSync = false;
volatile bool     statsPrimed = false;
volatile uint32_t lastCyc = 0, perMin = 0xFFFFFFFFu, perMax = 0, isrMax = 0;

// ---------------- settings and main-loop state ----------------
bool     armed = false;
bool     gainLow = true;                 // ARM default: LOW gain (safe for strong signals)
int      potCode = 0;                    // ARM default: minimum VGA gain
uint8_t  trigSrcSet = SRC_NONE;
uint16_t trigLevelSet = 200;
uint16_t midCode = 0;
bool     spiOn = false, wireOn = false;

bool     streaming = false;
uint8_t  capState = CAP_IDLE;
uint16_t capId = 0;
uint32_t capN = 0, capPre = 0;
uint32_t capTrig = 0, capEnd = 0;        // 32-bit sample indices
bool     capFirst = false;
uint32_t sendPos = 0;                    // next sample index to send
bool     pendingOverflow = false;
uint32_t seq = 0;

uint64_t epoch = 0;                      // 64-bit extension of `head`
uint32_t lastHead32 = 0;

uint32_t framesTotal = 0, ovfTotal = 0;
uint32_t statFrames = 0, statOvf = 0, statFerrBase = 0;

uint32_t lastBoardChk = 0;
int      potFailCount = 0;
int      potHw = -1;

IntervalTimer adcTimer;
SPISettings   adcSpi(12000000, MSBFIRST, SPI_MODE0);   // §3.2: mode 0, 10..16 MHz

static uint8_t  fbuf[32 + 2 * BLOCK + 4];
static uint32_t crcTab[256];

// =====================================================================
//  Small output helpers (no String, no floats)
// =====================================================================
static char lineBuf[200];

static void out(const char *s) { Serial.println(s); }

static char *u64str(uint64_t v, char *b) {      // printf("%llu") is not guaranteed on newlib-nano
  char t[24];
  int n = 0;
  do { t[n++] = (char)('0' + (int)(v % 10)); v /= 10; } while (v);
  for (int i = 0; i < n; i++) b[i] = t[n - 1 - i];
  b[n] = 0;
  return b;
}

// =====================================================================
//  CRC-32/IEEE (normative, §5.3) — identical to Python zlib.crc32
// =====================================================================
void crcInit() {
  for (uint32_t i = 0; i < 256; i++) {
    uint32_t c = i;
    for (int k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
    crcTab[i] = c;
  }
}
uint32_t crc32(const uint8_t *p, size_t n) {
  uint32_t c = 0xFFFFFFFFu;
  while (n--) c = crcTab[(c ^ *p++) & 0xFF] ^ (c >> 8);
  return c ^ 0xFFFFFFFFu;
}

// =====================================================================
//  400 kHz sample ISR (normative core, Appendix A.1)
//  No Serial, I2C, floats or String.
// =====================================================================
void adcTick() {
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
    bool hit = (trigSrc == SRC_LEVEL) ? (abs((int)code - (int)trigMid) >= (int)trigLevel)
             : (trigSrc == SRC_SYNC)  ? (s && !lastSync) : true;
    if (hit) { trigIndex = h; trigState = TRIG_HIT; }
  }
  lastSync = s;
  head = h + 1;
  if (statsPrimed) {                                         // timing statistics (STATS)
    uint32_t per = c0 - lastCyc;
    if (per < perMin) perMin = per;
    if (per > perMax) perMax = per;
  }
  statsPrimed = true;
  lastCyc = c0;
  uint32_t dur = ARM_DWT_CYCCNT - c0;
  if (dur > isrMax) isrMax = dur;
}

// =====================================================================
//  Board helpers
// =====================================================================
static inline uint16_t adcConvert() {           // one framed conversion from the main loop
  digitalWriteFast(PIN_ADC_CS, LOW);
  uint16_t raw = SPI1.transfer16(0);
  digitalWriteFast(PIN_ADC_CS, HIGH);
  return raw;
}

bool potWrite(uint8_t d) {
  Wire2.beginTransmission(POT_ADDR);
  Wire2.write((uint8_t)0x00);                   // instruction: normal write. NEVER set SD (bit 5) here.
  Wire2.write(d);
  bool ok = Wire2.endTransmission() == 0;
  if (ok) potHw = d;
  return ok;
}

bool potRead(uint8_t &d) {                      // no instruction byte for a read
  if (Wire2.requestFrom(POT_ADDR, (uint8_t)1) != 1) return false;
  if (!Wire2.available()) return false;
  d = (uint8_t)Wire2.read();
  return true;
}

void releasePins() {                            // nothing driven into the board
  pinMode(PIN_ADC_CS,   INPUT);
  pinMode(PIN_MOSI1,    INPUT);
  pinMode(PIN_SCK1,     INPUT);
  pinMode(PIN_MISO1,    INPUT);
  pinMode(PIN_SCL2,     INPUT);
  pinMode(PIN_SDA2,     INPUT);
  pinMode(PIN_GAIN_SEL, INPUT);                 // R28 pull-down -> hardware default HIGH gain
}

// Not armed: does the AD5245 answer? (needs the board's 5 V). Pins released again after.
bool boardPowered() {
  Wire2.begin();
  Wire2.setClock(100000);
  uint8_t d;
  bool ok = potRead(d);
  Wire2.end();
  releasePins();
  return ok;
}

static void setLedTx() {
  digitalWriteFast(PIN_LED_TX, (streaming || capState != CAP_IDLE) ? HIGH : LOW);
}

static void stopAcquisition() {
  streaming = false;
  capState = CAP_IDLE;
  trigState = TRIG_OFF;
  setLedTx();
}

// Stop everything and release every board pin (DISARM and all failure paths).
void releaseAll() {
  adcTimer.end();
  stopAcquisition();
  if (spiOn) { SPI1.endTransaction(); SPI1.end(); spiOn = false; }
  if (wireOn) { Wire2.end(); wireOn = false; }
  releasePins();
  armed = false;
  potHw = -1;
  digitalWriteFast(PIN_LED_TX, LOW);
}

static void startTimer() {
  noInterrupts();
  statsPrimed = false;
  lastSync = digitalReadFast(PIN_SYNC);
  interrupts();
  adcTimer.priority(0);                         // highest: sampling jitter = ISR entry jitter
  adcTimer.begin(adcTick, 2.5f);                // 60 ticks of the 24 MHz PIT clock, exact
}

static void resetStats() {
  noInterrupts();
  perMin = 0xFFFFFFFFu; perMax = 0; isrMax = 0;
  statFerrBase = ferrCount;
  interrupts();
  statFrames = 0;
  statOvf = 0;
}

// 64-bit sample index of the current head (main loop only).
static uint64_t head64() {
  uint32_t h = head;
  if (h < lastHead32) epoch += 0x100000000ull;
  lastHead32 = h;
  return epoch | h;
}
static uint64_t index64(uint32_t idx) {
  uint32_t h = head;
  uint64_t h64 = head64();
  return h64 - (uint32_t)(h - idx);
}

// =====================================================================
//  ARM / DISARM (spec §4.3, normative order)
// =====================================================================
void doArm() {
  if (armed) { snprintf(lineBuf, sizeof lineBuf, "OK ARMED mid=%u", (unsigned)midCode); out(lineBuf); return; }

  // 1-2. Board power: the AD5245 can only ACK with the board's 5 V up.
  Wire2.begin();
  wireOn = true;
  Wire2.setClock(100000);
  uint8_t d;
  if (!potRead(d)) {
    releaseAll();
    out("ERR NOPOWER (AD5245 not answering: turn the RX board 12 V on, check LED D6, then ARM)");
    return;
  }
  // 3. Pot to the stored code (default 0 = minimum gain).
  if (!potWrite((uint8_t)potCode)) {
    releaseAll();
    out("ERR NOPOT (AD5245 did not acknowledge the write)");
    return;
  }
  Wire2.setClock(400000);

  // 4. Coarse gain, CS idle high, SPI1 on the alternate MISO pin.
  pinMode(PIN_GAIN_SEL, OUTPUT);
  digitalWriteFast(PIN_GAIN_SEL, gainLow ? HIGH : LOW);       // HIGH = LOW gain
  pinMode(PIN_ADC_CS, OUTPUT);
  digitalWriteFast(PIN_ADC_CS, HIGH);
  SPI1.setMISO(PIN_MISO1);                                   // before begin()!
  SPI1.begin();
  SPI1.beginTransaction(adcSpi);                             // kept open: SPI1 serves only the ADC
  spiOn = true;

  // 5. ADC self-test.
  adcConvert();                                              // first two results after power-up
  adcConvert();                                              // are unusable (datasheet)
  uint32_t sum = 0, fe = 0;
  for (int i = 0; i < 64; i++) {
    delayMicroseconds(3);
    uint16_t raw = adcConvert();
    if (raw & ADC_ZMASK) fe++;
    sum += (raw >> ADC_SHIFT) & 0x0FFF;
  }
  uint32_t mean = (sum + 32) / 64;
  if (fe > 2 || mean < MID_MIN || mean > MID_MAX) {
    releaseAll();
    snprintf(lineBuf, sizeof lineBuf,
             "ERR ADC self-test failed: mean=%lu frame_errors=%lu (expect %u..%u, 0; check 5 V, 2V5, SPI wiring, MISO pin 39, ADC_SHIFT)",
             (unsigned long)mean, (unsigned long)fe, (unsigned)MID_MIN, (unsigned)MID_MAX);
    out(lineBuf);
    return;
  }
  midCode = (uint16_t)mean;

  // 6. Counters, then the sampling timer.
  noInterrupts();
  head = 0; ferrCount = 0; trigState = TRIG_OFF;
  interrupts();
  epoch = 0; lastHead32 = 0; seq = 0;
  framesTotal = 0; ovfTotal = 0;
  resetStats();
  streaming = false; capState = CAP_IDLE; pendingOverflow = false;
  armed = true;
  potFailCount = 0;
  lastBoardChk = millis();
  startTimer();
  snprintf(lineBuf, sizeof lineBuf, "OK ARMED mid=%u", (unsigned)midCode);
  out(lineBuf);
}

void doDisarm() {
  releaseAll();
  out("OK DISARMED");
}

// =====================================================================
//  Frames (spec §5.3)
// =====================================================================
static inline void put16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static inline void put32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8 * i)); }
static inline void put64(uint8_t *p, uint64_t v) { for (int i = 0; i < 8; i++) p[i] = (uint8_t)(v >> (8 * i)); }

// Build and send one frame of n samples starting at sample index `from`.
static void sendFrame(uint8_t type, uint32_t from, uint32_t n, uint16_t flags) {
  uint8_t *pl = fbuf + 32;
  uint16_t trigOff = NO_OFFSET, syncOff = NO_OFFSET;
  bool prev = (from != 0) && (ring[(from - 1) & RING_MASK] & 0x8000);
  for (uint32_t i = 0; i < n; i++) {
    uint16_t w = ring[(from + i) & RING_MASK];
    put16(pl + 2 * i, w);
    uint16_t c = w & 0x0FFF;
    if (c <= CLIP_LO || c >= CLIP_HI) flags |= FLAG_CLIP;
    if (w & 0x4000) flags |= FLAG_FRAME_ERR;
    bool s = (w & 0x8000) != 0;
    if (s && !prev && syncOff == NO_OFFSET) { syncOff = (uint16_t)i; flags |= FLAG_SYNC; }
    prev = s;
  }
  if (type == FT_CAPTURE && (uint32_t)(capTrig - from) < n) trigOff = (uint16_t)(capTrig - from);
  if (pendingOverflow) { flags |= FLAG_OVERFLOW; pendingOverflow = false; }

  fbuf[0] = 0xA5; fbuf[1] = 0x5A; fbuf[2] = 'R'; fbuf[3] = 'X';
  fbuf[4] = 1;
  fbuf[5] = type;
  put16(fbuf + 6, flags);
  put32(fbuf + 8, seq++);
  put64(fbuf + 12, index64(from));
  put16(fbuf + 20, (uint16_t)n);
  fbuf[22] = gainLow ? 1 : 0;
  fbuf[23] = (uint8_t)potCode;
  put16(fbuf + 24, type == FT_CAPTURE ? capId : 0);
  put16(fbuf + 26, trigOff);
  put16(fbuf + 28, syncOff);
  put16(fbuf + 30, 4000);                       // fs / 100
  size_t len = 32 + 2 * n;
  put32(fbuf + len, crc32(fbuf, len));
  Serial.write(fbuf, len + 4);
  framesTotal++;
  statFrames++;
}

// The PC fell behind and the next unsent sample is about to be overwritten: skip ahead.
static bool checkOverflow() {
  uint32_t h = head;
  if (h - sendPos > RING_N - 2 * BLOCK) {
    uint32_t to = h - BLOCK;
    uint32_t lost = to - sendPos;
    ovfTotal += lost;
    statOvf += lost;
    sendPos = to;
    pendingOverflow = true;
    return true;
  }
  return false;
}

static void serviceStream() {
  for (uint32_t k = 0; k < FRAMES_PER_LOOP && streaming; k++) {
    checkOverflow();
    if (head - sendPos < BLOCK) return;
    sendFrame(FT_STREAM, sendPos, BLOCK, 0);
    sendPos += BLOCK;
  }
}

static void serviceCapture() {
  if (capState == CAP_WAIT) {
    if (trigState != TRIG_HIT) return;
    capTrig = trigIndex;
    sendPos = capTrig - capPre;
    capEnd = sendPos + capN;
    capFirst = true;
    pendingOverflow = false;
    capState = CAP_RUN;
    char b[24];
    snprintf(lineBuf, sizeof lineBuf, "EVT TRIG id=%u index=%s", (unsigned)capId, u64str(index64(capTrig), b));
    out(lineBuf);
  }
  for (uint32_t k = 0; k < FRAMES_PER_LOOP && capState == CAP_RUN; k++) {
    if (checkOverflow()) {
      if ((int32_t)(capEnd - sendPos) < 0) sendPos = capEnd;   // whole rest lost
      snprintf(lineBuf, sizeof lineBuf,
               "WARN OVERFLOW capture id=%u lost samples (PC not reading fast enough)", (unsigned)capId);
      out(lineBuf);
    }
    uint32_t remaining = capEnd - sendPos;
    uint32_t n = remaining < BLOCK ? remaining : BLOCK;
    if (n > 0) {
      if (head - sendPos < n) return;                          // not sampled yet
      uint16_t fl = 0;
      if (capFirst) { fl |= FLAG_CAP_FIRST; capFirst = false; }
      if (n == remaining) fl |= FLAG_CAP_LAST;
      sendFrame(FT_CAPTURE, sendPos, n, fl);
      sendPos += n;
    }
    if (sendPos == capEnd) {
      char b[24];
      snprintf(lineBuf, sizeof lineBuf, "EVT CAPDONE id=%u n=%lu pre=%lu trig=%s", (unsigned)capId,
               (unsigned long)capN, (unsigned long)capPre, u64str(index64(capTrig), b));
      out(lineBuf);
      capState = CAP_IDLE;
      trigState = TRIG_OFF;
      setLedTx();
    }
  }
}

// =====================================================================
//  Commands (spec §5.2)
// =====================================================================
static const char *trigName(uint8_t s) { return s == SRC_LEVEL ? "LEVEL" : s == SRC_SYNC ? "SYNC" : "NONE"; }
static const char *capName(uint8_t s)  { return s == CAP_WAIT ? "WAIT" : s == CAP_RUN ? "RUN" : "IDLE"; }

void statusLine() {
  int power;
  if (armed) {
    if (streaming || capState != CAP_IDLE) power = 1;          // the watchdog covers this case
    else { uint8_t d; power = potRead(d) ? 1 : 0; }
  } else {
    power = boardPowered() ? 1 : 0;                            // releases the pins again
  }
  snprintf(lineBuf, sizeof lineBuf,
           "STATUS armed=%d power=%d gain=%s pot=%d fs=400000 stream=%d cap=%s trig=%s level=%u mid=%u sync=%d ovf=%lu ferr=%lu",
           armed ? 1 : 0, power, gainLow ? "LOW" : "HIGH", potCode, streaming ? 1 : 0,
           capName(capState), trigName(trigSrcSet), (unsigned)trigLevelSet, (unsigned)midCode,
           digitalReadFast(PIN_SYNC) ? 1 : 0, (unsigned long)ovfTotal, (unsigned long)ferrCount);
  out(lineBuf);
}

static bool parseUInt(const char *s, uint32_t &v) {          // decimal digits only
  if (!s || !*s) return false;
  uint64_t x = 0;
  for (const char *p = s; *p; p++) {
    if (*p < '0' || *p > '9') return false;
    x = x * 10 + (uint64_t)(*p - '0');
    if (x > 0xFFFFFFFFull) return false;
  }
  v = (uint32_t)x;
  return true;
}

static uint32_t cycToNs(uint32_t c) { return (uint32_t)((uint64_t)c * 1000ull / (F_CPU_ACTUAL / 1000000ull)); }

void handle(char *line) {
  for (char *p = line; *p; p++) if (*p >= 'a' && *p <= 'z') *p -= 32;
  char *cmd = strtok(line, " \t");
  if (!cmd) return;
  char *a1 = strtok(nullptr, " \t");
  char *a2 = strtok(nullptr, " \t");
  bool busy = streaming || capState != CAP_IDLE;

  if (!strcmp(cmd, "PING")) out("PONG RXFW " RXFW_VERSION);
  else if (!strcmp(cmd, "VERSION")) {
    snprintf(lineBuf, sizeof lineBuf, "OK VERSION RXFW " RXFW_VERSION " fs=400000 adc=ADC121S101 pot=AD5245 shift=%d", ADC_SHIFT);
    out(lineBuf);
  }
  else if (!strcmp(cmd, "HELP"))
    out("OK commands: PING VERSION ARM DISARM STATUS GAIN POT POTREAD TRIG CAPTURE ABORT STREAM RAW STATS");
  else if (!strcmp(cmd, "STATUS")) statusLine();
  else if (!strcmp(cmd, "ARM")) doArm();
  else if (!strcmp(cmd, "DISARM")) doDisarm();
  else if (!strcmp(cmd, "GAIN")) {
    if (a1 && !strcmp(a1, "HIGH")) gainLow = false;
    else if (a1 && !strcmp(a1, "LOW")) gainLow = true;
    else { out("ERR GAIN needs HIGH or LOW"); return; }
    if (armed) digitalWriteFast(PIN_GAIN_SEL, gainLow ? HIGH : LOW);
    out(gainLow ? "OK GAIN LOW" : "OK GAIN HIGH");
  }
  else if (!strcmp(cmd, "POT")) {
    uint32_t d;
    if (!a1) { out("ERR POT needs a value"); return; }
    if (!parseUInt(a1, d) || d > 255) { out("ERR POT out of range 0..255"); return; }
    if (armed && !potWrite((uint8_t)d)) { out("ERR NOPOT (I2C write failed)"); return; }
    potCode = (int)d;
    snprintf(lineBuf, sizeof lineBuf, "OK POT %d", potCode);
    out(lineBuf);
  }
  else if (!strcmp(cmd, "POTREAD")) {
    if (!armed) { out("ERR NOTARMED"); return; }
    uint8_t d;
    if (!potRead(d)) { out("ERR NOPOT (I2C read failed)"); return; }
    snprintf(lineBuf, sizeof lineBuf, "OK POTREAD %u", (unsigned)d);
    out(lineBuf);
  }
  else if (!strcmp(cmd, "TRIG")) {
    if (capState != CAP_IDLE) { out("ERR BUSY capture in progress (ABORT first)"); return; }
    if (a1 && !strcmp(a1, "NONE")) { trigSrcSet = SRC_NONE; out("OK TRIG NONE"); }
    else if (a1 && !strcmp(a1, "SYNC")) { trigSrcSet = SRC_SYNC; out("OK TRIG SYNC"); }
    else if (a1 && !strcmp(a1, "LEVEL")) {
      uint32_t v;
      if (!a2) { out("ERR TRIG LEVEL needs <counts>"); return; }
      if (!parseUInt(a2, v) || v < 1 || v > 2047) { out("ERR TRIG LEVEL out of range 1..2047"); return; }
      trigSrcSet = SRC_LEVEL;
      trigLevelSet = (uint16_t)v;
      snprintf(lineBuf, sizeof lineBuf, "OK TRIG LEVEL %lu", (unsigned long)v);
      out(lineBuf);
    }
    else out("ERR TRIG needs NONE, LEVEL <counts> or SYNC");
  }
  else if (!strcmp(cmd, "CAPTURE")) {
    if (!armed) { out("ERR NOTARMED"); return; }
    if (streaming) { out("ERR BUSY streaming (STREAM OFF first)"); return; }
    if (capState != CAP_IDLE) { out("ERR BUSY capture in progress (ABORT first)"); return; }
    uint32_t n, pre = 0;
    if (!a1) { out("ERR CAPTURE needs <n> [pre]"); return; }
    if (!parseUInt(a1, n) || n < CAP_N_MIN || n > CAP_N_MAX) { out("ERR CAPTURE n out of range 16..4000000"); return; }
    uint32_t preMax = n - 1 < CAP_PRE_MAX ? n - 1 : CAP_PRE_MAX;
    if (a2 && (!parseUInt(a2, pre) || pre > preMax)) {
      snprintf(lineBuf, sizeof lineBuf, "ERR CAPTURE pre out of range 0..%lu", (unsigned long)preMax);
      out(lineBuf);
      return;
    }
    capId = (uint16_t)(capId + 1);
    if (capId == 0) capId = 1;                                 // 0 means "stream"
    capN = n; capPre = pre;
    noInterrupts();
    trigSrc = trigSrcSet;
    trigLevel = trigLevelSet;
    trigMid = midCode;
    trigEarliest = head + pre + 1;                             // pre-trigger samples are fresh
    trigState = TRIG_WAIT;
    interrupts();
    capState = CAP_WAIT;
    setLedTx();
    snprintf(lineBuf, sizeof lineBuf, "OK CAPTURE id=%u", (unsigned)capId);
    out(lineBuf);
  }
  else if (!strcmp(cmd, "ABORT")) {
    capState = CAP_IDLE;
    trigState = TRIG_OFF;
    setLedTx();
    out("OK ABORT");
  }
  else if (!strcmp(cmd, "STREAM")) {
    if (a1 && !strcmp(a1, "OFF")) { streaming = false; setLedTx(); out("OK STREAM OFF"); }
    else if (a1 && !strcmp(a1, "ON")) {
      if (!armed) { out("ERR NOTARMED"); return; }
      if (capState != CAP_IDLE) { out("ERR BUSY capture in progress (ABORT first)"); return; }
      if (!streaming) {
        sendPos = head & ~(BLOCK - 1);                         // block-aligned start
        pendingOverflow = false;
        streaming = true;
      }
      setLedTx();
      out("OK STREAM ON");
    }
    else out("ERR STREAM needs ON or OFF");
  }
  else if (!strcmp(cmd, "RAW")) {
    if (!armed) { out("ERR NOTARMED"); return; }
    if (busy) { out("ERR BUSY (STREAM OFF / ABORT first)"); return; }
    uint32_t k = 8;
    if (a1 && (!parseUInt(a1, k) || k < 1 || k > 16)) { out("ERR RAW out of range 1..16"); return; }
    adcTimer.end();                                            // the ISR owns SPI1 otherwise
    char *p = lineBuf + snprintf(lineBuf, sizeof lineBuf, "OK RAW");
    for (uint32_t i = 0; i < k; i++) {
      delayMicroseconds(3);
      p += snprintf(p, 8, " %04X", (unsigned)adcConvert());
    }
    startTimer();
    out(lineBuf);
  }
  else if (!strcmp(cmd, "STATS")) {
    noInterrupts();
    uint32_t pmin = perMin, pmax = perMax, imax = isrMax, fe = ferrCount - statFerrBase;
    interrupts();
    snprintf(lineBuf, sizeof lineBuf,
             "OK STATS frames=%lu ovf=%lu ferr=%lu per_min_ns=%lu per_max_ns=%lu isr_max_ns=%lu",
             (unsigned long)statFrames, (unsigned long)statOvf, (unsigned long)fe,
             (unsigned long)(pmin == 0xFFFFFFFFu ? 0 : cycToNs(pmin)), (unsigned long)cycToNs(pmax),
             (unsigned long)cycToNs(imax));
    out(lineBuf);
    resetStats();
  }
  else {
    snprintf(lineBuf, sizeof lineBuf, "ERR unknown command: %.40s", cmd);
    out(lineBuf);
  }
}

// =====================================================================
//  Watchdogs (spec §4.4)
// =====================================================================
void checkBoard() {
  uint8_t d = 0;
  if (!potRead(d)) {
    if (++potFailCount >= 2) {
      releaseAll();
      out("WARN POWERLOST RX board stopped answering: disarmed, pins released");
    }
    return;
  }
  potFailCount = 0;
  if (potHw >= 0 && d != (uint8_t)potHw) {         // supply glitch reset it to mid-scale
    int want = potHw;
    potWrite((uint8_t)want);
    snprintf(lineBuf, sizeof lineBuf, "WARN POTRESET wiper read %u expected %d (board supply glitch?): rewritten",
             (unsigned)d, want);
    out(lineBuf);
  }
}

// =====================================================================
void setup() {
  releasePins();                                   // never drive the board before ARM
  pinMode(PIN_SYNC, INPUT_PULLDOWN);
  pinMode(PIN_LED_TX, OUTPUT); digitalWriteFast(PIN_LED_TX, LOW);
  pinMode(PIN_LED_HB, OUTPUT); digitalWriteFast(PIN_LED_HB, LOW);
  ARM_DEMCR |= ARM_DEMCR_TRCENA;                   // cycle counter for the ISR timing statistics
  ARM_DWT_CTRL |= ARM_DWT_CTRL_CYCCNTENA;
  crcInit();
  Serial.begin(115200);
}

void loop() {
  static char     rx[MAX_LINE + 1];
  static unsigned rxLen = 0;
  static bool     rxOverflow = false;
  static uint32_t lastHb = 0;

  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (rxOverflow) out("ERR line too long");
      else if (rxLen) { rx[rxLen] = 0; handle(rx); }
      rxLen = 0;
      rxOverflow = false;
    } else if (rxLen < MAX_LINE) {
      rx[rxLen++] = ch;
    } else {
      rxOverflow = true;
    }
  }

  if (armed) {
    head64();                                      // keep the 64-bit index extension current
    if (streaming) serviceStream();
    else if (capState != CAP_IDLE) serviceCapture();
  }

  // Host gone (port closed): stop sending. Stay armed (harmless).
  if ((streaming || capState != CAP_IDLE) && !Serial.dtr()) stopAcquisition();

  uint32_t now = millis();
  if (armed && now - lastBoardChk >= BOARD_CHECK_MS) {
    lastBoardChk = now;
    checkBoard();
  }

  if (now - lastHb >= (armed ? 125u : 500u)) {     // heartbeat: slow when idle, fast when armed
    lastHb = now;
    digitalToggleFast(PIN_LED_HB);
  }
}
