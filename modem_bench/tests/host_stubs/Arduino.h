// Minimal Arduino/Teensy API stubs so tx_firmware.ino compiles and runs on a PC.
// Records every DAC word, every pot write and every pin change so tests can check
// the waveform, the limits, the ARM/DISARM sequence and back-powering.
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
  String(const char* c) : s(c ? c : "") {}
  String(const std::string& x) : s(x) {}
  String(char c) : s(1, c) {}
  String(int v) { s = std::to_string(v); }
  String(long v) { s = std::to_string(v); }
  String(unsigned long v) { s = std::to_string(v); }
  String(unsigned int v) { s = std::to_string(v); }
  String(double v, unsigned char d = 2) { char b[64]; snprintf(b, 64, "%.*f", d, v); s = b; }
  String(float v, unsigned char d = 2) { char b[64]; snprintf(b, 64, "%.*f", d, (double)v); s = b; }
  unsigned int length() const { return (unsigned)s.size(); }
  void trim() {
    size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) { s = ""; return; }
    size_t b = s.find_last_not_of(" \t\r\n");
    s = s.substr(a, b - a + 1);
  }
  void toUpperCase() { for (auto& c : s) c = (char)toupper((unsigned char)c); }
  void toCharArray(char* b, unsigned n) const { strncpy(b, s.c_str(), n - 1); b[n - 1] = 0; }
  String& operator+=(const String& o) { s += o.s; return *this; }
  String& operator+=(const char* o) { s += o; return *this; }
  String& operator+=(char c) { s += c; return *this; }
  String& operator+=(int v) { s += std::to_string(v); return *this; }
  String& operator+=(long v) { s += std::to_string(v); return *this; }
  String& operator+=(unsigned long v) { s += std::to_string(v); return *this; }
  String& operator+=(unsigned int v) { s += std::to_string(v); return *this; }
  bool operator==(const char* o) const { return s == o; }
  bool operator==(const String& o) const { return s == o.s; }
  const char* c_str() const { return s.c_str(); }
};
inline String operator+(const String& a, const String& b) { return String(a.s + b.s); }
inline String operator+(const String& a, const char* b) { return String(a.s + b); }

// ---- recorded hardware state (defined in the test main) ----
extern std::vector<uint16_t> g_dac;      // every DAC word written
extern int  g_pins[64];                  // last digital value written
extern int  g_pinmode[64];               // last pinMode
extern bool g_power;                     // board 12 V on?  (I2C pull-ups high)
extern std::vector<int> g_pot;           // every successful pot write (data byte)
extern std::vector<size_t> g_potAtDac;   // g_dac.size() at the time of each pot write
extern int  g_potReg;                    // AD5245 wiper register (as the chip sees it)
extern int  g_backpower;                 // count of pins driven HIGH into an unpowered board
extern unsigned long g_ms, g_us;

inline bool isBoardPin(int p) { return p == 24 || p == 25 || p == 26 || p == 27 || p == 28; }
inline void pinMode(int p, int m) {
  g_pinmode[p] = m;
  if (m == OUTPUT && !g_power && isBoardPin(p) && g_pins[p] == HIGH) g_backpower++;
}
inline void digitalWriteFast(int p, int v) {
  g_pins[p] = v;
  if (v == HIGH && !g_power && isBoardPin(p) && g_pinmode[p] == OUTPUT) g_backpower++;
}
inline void digitalToggleFast(int p) { g_pins[p] ^= 1; }
inline int digitalRead(int p) { (void)p; return g_power ? HIGH : LOW; }
inline void delay(unsigned) {}
inline void delayMicroseconds(unsigned) {}
inline unsigned long millis() { return g_ms; }
inline unsigned long micros() { return g_us; }
inline void noInterrupts() {}
inline void interrupts() {}

struct SPISettings { SPISettings(uint32_t, int, int) {} };
struct SPIClass {
  bool begun = false, open = false;
  void begin() {
    begun = true;
    g_pinmode[26] = OUTPUT; g_pinmode[27] = OUTPUT;   // MOSI1 / SCK1 now driven
    if (!g_power) g_backpower++;
  }
  void end() { begun = false; }
  void beginTransaction(SPISettings) { open = true; }
  void endTransaction() { open = false; }
  uint16_t transfer16(uint16_t v) {
    if (g_pins[28] != LOW) printf("FAIL: SPI transfer with CS not low\n");
    if (!g_power) g_backpower++;
    g_dac.push_back(v);
    return 0;
  }
};
extern SPIClass SPI1;

struct TwoWire {
  std::vector<uint8_t> b;
  int rxAvail = 0;
  bool begun = false;
  void begin() { begun = true; }
  void end() { begun = false; }
  void setClock(uint32_t) {}
  void beginTransmission(uint8_t a) { b.clear(); b.push_back(a); }
  void write(uint8_t v) { b.push_back(v); }
  int endTransmission() {
    if (!g_power || b.empty() || b[0] != 0x2C) return 2;   // address NACK
    if (b.size() == 3 && b[1] == 0x00) {
      g_potReg = b[2];
      g_pot.push_back(b[2]);
      g_potAtDac.push_back(g_dac.size());
    }
    return 0;
  }
  uint8_t requestFrom(uint8_t a, uint8_t n) {
    if (!g_power || a != 0x2C) { rxAvail = 0; return 0; }
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
  bool begin(void (*fn)(), float us) { f = fn; on = true; period = us; return true; }
  void end() { on = false; }
  void priority(int) {}
};

struct SerialC {
  std::deque<char> in;
  std::vector<std::string> out;
  void begin(int) {}
  int available() { return (int)in.size(); }
  int read() { int c = in.front(); in.pop_front(); return c; }
  void println(const String& s) { out.push_back(s.s); }
};
extern SerialC Serial;
