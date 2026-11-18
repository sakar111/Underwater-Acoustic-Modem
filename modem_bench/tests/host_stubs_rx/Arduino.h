// Teensy/Arduino API stubs that model the RX board, so rx_firmware.ino compiles and runs on a PC
// (spec Appendix B.1). Separate from host_stubs/ (TX), whose SPI/I2C model is a DAC + level pot.
//
//   g_power        board 12 V: when false the AD5245 NACKs and the ADC reads 0
//   g_sig(k)       ADC code of sample k        g_sync(k)  SYNC pin level of sample k
//   g_k            sample index of the conversion in progress (set by the test driver = head)
//   g_wrongAlign   ADC returns code << 0 instead of the real mode-0 alignment code << 1
//   Serial         collects every byte written (lines and frames); dtr() is controllable
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cmath>
#include <deque>
#include <functional>
#include <string>
#include <vector>

#define HIGH 1
#define LOW 0
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLDOWN 3
#define MSBFIRST 1
#define SPI_MODE0 0
#define F_CPU_ACTUAL 600000000u

extern int  g_pins[64];
extern int  g_pinmode[64];
extern bool g_power;
extern bool g_wrongAlign;
extern int  g_potReg;
extern std::vector<int> g_potWrites;
extern long long g_k;
extern std::function<int(long long)> g_sig;
extern std::function<int(long long)> g_sync;
extern unsigned long g_ticks;            // simulated 2.5 us sample periods since start
extern unsigned long g_extraMs;          // time advanced without sampling
extern uint32_t g_cyc;
extern int g_spiErrors;

extern uint32_t g_demcr, g_dwtctrl;
#define ARM_DEMCR g_demcr
#define ARM_DEMCR_TRCENA (1u << 24)
#define ARM_DWT_CTRL g_dwtctrl
#define ARM_DWT_CTRL_CYCCNTENA 1u
#define ARM_DWT_CYCCNT (g_cyc += 750u)    // 2 reads per ISR -> 1500 cycles = 2500 ns per tick

inline void pinMode(int p, int m) { g_pinmode[p] = m; }
inline void digitalWriteFast(int p, int v) { g_pins[p] = v; }
inline void digitalToggleFast(int p) { g_pins[p] ^= 1; }
inline int  digitalReadFast(int p) { return (p == 2 && g_sync) ? (g_sync(g_k) ? 1 : 0) : 0; }
inline int  digitalRead(int p) { return digitalReadFast(p); }
inline void delay(unsigned ms) { g_extraMs += ms; }
inline void delayMicroseconds(unsigned) {}
inline unsigned long millis() { return g_ticks / 400 + g_extraMs; }
inline unsigned long micros() { return g_ticks * 5 / 2 + g_extraMs * 1000; }
inline void noInterrupts() {}
inline void interrupts() {}

struct SPISettings {
  uint32_t clk; int order, mode;
  SPISettings(uint32_t c, int o, int m) : clk(c), order(o), mode(m) {}
};
struct SPIClass {
  bool begun = false, open = false;
  int misoPin = 1;
  bool misoSetBeforeBegin = false;
  uint32_t clk = 0;
  int mode = -1;
  unsigned conv = 0;
  void setMISO(int p) { misoPin = p; }
  void begin() {
    begun = true;
    misoSetBeforeBegin = misoPin == 39;
    g_pinmode[26] = OUTPUT; g_pinmode[27] = OUTPUT; g_pinmode[misoPin] = INPUT;
  }
  void end() { begun = false; open = false; }
  void beginTransaction(SPISettings s) { open = true; clk = s.clk; mode = s.mode; }
  void endTransaction() { open = false; }
  uint16_t transfer16(uint16_t) {
    if (!begun || !open || g_pins[28] != LOW || g_pinmode[28] != OUTPUT || misoPin != 39) {
      g_spiErrors++;
      return 0xFFFF;                     // garbage (wrong MISO pin / no CS)
    }
    if (!g_power) return 0;              // ADC unpowered: SDATA floats low through the buffer
    int code = g_sig ? g_sig(g_k) : 2048;
    if (code < 0) code = 0;
    if (code > 4095) code = 4095;
    conv++;
    return g_wrongAlign ? (uint16_t)code : (uint16_t)((code << 1) | (conv & 1));
  }
};
extern SPIClass SPI1;

struct TwoWire {
  std::vector<uint8_t> b;
  int rxAvail = 0;
  bool begun = false;
  uint32_t clock = 0;
  void begin() { begun = true; }
  void end() { begun = false; }
  void setClock(uint32_t c) { clock = c; }
  void beginTransmission(uint8_t a) { b.clear(); b.push_back(a); }
  void write(uint8_t v) { b.push_back(v); }
  int endTransmission() {
    if (!begun || !g_power || b.empty() || b[0] != 0x2C) return 2;   // address NACK
    if (b.size() == 3) {
      if (b[1] & 0x20) printf("FAIL: AD5245 shutdown bit set\n");
      g_potReg = b[2];
      g_potWrites.push_back(b[2]);
    }
    return 0;
  }
  uint8_t requestFrom(uint8_t a, uint8_t n) {
    if (!begun || !g_power || a != 0x2C) { rxAvail = 0; return 0; }
    rxAvail = n;
    return n;
  }
  int available() { return rxAvail; }
  int read() { if (rxAvail <= 0) return -1; rxAvail--; return g_potReg; }
};
extern TwoWire Wire2;

struct IntervalTimer {
  void (*f)() = nullptr;
  bool on = false;
  float period = 0;
  int prio = 128;
  bool begin(void (*fn)(), float us) { f = fn; on = true; period = us; return true; }
  void end() { on = false; }
  void priority(int p) { prio = p; }
};

struct SerialC {
  std::deque<char> in;
  std::vector<uint8_t> out;
  bool dtrState = true;
  void begin(int) {}
  int available() { return (int)in.size(); }
  int read() { int c = in.front(); in.pop_front(); return c; }
  bool dtr() { return dtrState; }
  size_t write(const uint8_t* p, size_t n) { out.insert(out.end(), p, p + n); return n; }
  void println(const char* s) { size_t n = strlen(s); out.insert(out.end(), s, s + n); out.push_back('\r'); out.push_back('\n'); }
};
extern SerialC Serial;
