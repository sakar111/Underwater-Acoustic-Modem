// Host-side unit tests for tx_firmware.ino (spec §9.1).
// Build + run:  python tests/run_all.py     (or by hand:)
//   g++ -std=c++17 -O1 -Wall -Itests/host_stubs -o build/sim tests/sim_main.cpp && build/sim
#include <algorithm>
#include "host_globals.h"

static int g_fail = 0, g_pass = 0;
#define CHECK(cond, ...)                                                   \
  do {                                                                     \
    if (cond) { g_pass++; }                                                \
    else { g_fail++; printf("FAIL line %d: %s  -- ", __LINE__, #cond);     \
           printf(__VA_ARGS__); printf("\n"); }                            \
  } while (0)

static std::string cmd(const char* c) {
  Serial.out.clear();
  for (const char* p = c; *p; p++) Serial.in.push_back(*p);
  Serial.in.push_back('\r');
  Serial.in.push_back('\n');
  loop();
  std::string r;
  for (size_t i = 0; i < Serial.out.size(); i++) { if (i) r += " | "; r += Serial.out[i]; }
  return r;
}
static bool starts(const std::string& s, const char* p) { return s.rfind(p, 0) == 0; }
static bool has(const std::string& s, const char* p) { return s.find(p) != std::string::npos; }
static void ticks(int n) { for (int i = 0; i < n; i++) dacTick(); }
static void advanceMs(unsigned long ms) {   // run the main loop through time
  for (unsigned long i = 0; i < ms; i += 10) { g_ms += 10; g_us += 10000; loop(); }
}
static bool boardPinsReleased() {
  for (int p = 24; p <= 28; p++) if (g_pinmode[p] != INPUT) return false;
  return true;
}

int main() {
  setup();
  CHECK(boardPinsReleased(), "pins 24-28 must be INPUT after setup");
  CHECK(g_dac.empty() && g_pot.empty(), "nothing written at boot");
  CHECK(g_pinmode[3] == OUTPUT && g_pinmode[8] == OUTPUT, "LEDs are outputs");

  // ---- 1. ARM with 12 V off is refused, pins stay high-Z ----
  g_power = false;
  std::string r = cmd("ARM");
  CHECK(starts(r, "ERR NOPOWER"), "%s", r.c_str());
  CHECK(boardPinsReleased(), "pins released after NOPOWER");
  CHECK(g_dac.empty() && g_pot.empty(), "no DAC/pot traffic without power");
  CHECK(!dacTimer.on, "timer not started");
  r = cmd("STATUS");
  CHECK(has(r, "armed=0") && has(r, "power=0"), "%s", r.c_str());
  r = cmd("START");
  CHECK(starts(r, "ERR NOTARMED"), "%s", r.c_str());
  CHECK(g_backpower == 0, "back-power events: %d", g_backpower);

  // ---- 2. PING / ARM with power ----
  g_power = true;
  r = cmd("PING");
  CHECK(r == "PONG TXFW " TXFW_VERSION, "%s", r.c_str());
  r = cmd("STATUS");
  CHECK(has(r, "power=1") && has(r, "armed=0"), "%s", r.c_str());
  r = cmd("arm");
  CHECK(r == "OK ARMED", "%s", r.c_str());
  CHECK(g_pot.size() >= 2 && g_pot[0] == 255, "first pot write is min gain");
  CHECK(g_potAtDac[0] == 0, "pot at min gain before any DAC word");
  CHECK(!g_dac.empty() && g_dac.front() == 0, "first DAC word is 0");
  CHECK(g_dac.back() == 32768, "ramp ends at mid-scale, got %u", g_dac.back());
  bool mono = true;
  for (size_t i = 1; i < g_dac.size(); i++) if (g_dac[i] < g_dac[i - 1]) mono = false;
  CHECK(mono && g_dac.size() > 500, "ramp monotonic, %zu words (~50 ms)", g_dac.size());
  CHECK(dacTimer.on && dacTimer.period == 2.0f, "500 kHz timer running");
  CHECK(g_pinmode[28] == OUTPUT && g_pins[28] == HIGH, "CS idle high");

  // ---- 3. settings + limits ----
  r = cmd("FREQ 25000");  CHECK(r == "OK FREQ 25000", "%s", r.c_str());
  r = cmd("AMP 200");     CHECK(r == "OK AMP 200", "%s", r.c_str());
  r = cmd("POT 200");     CHECK(r == "OK POT 200", "%s", r.c_str());
  CHECK(g_potReg == 200, "pot written while armed");
  r = cmd("LIMIT 60");    CHECK(starts(r, "ERR LIMIT out of range"), "%s", r.c_str());
  r = cmd("FREQ 50");     CHECK(starts(r, "ERR FREQ"), "%s", r.c_str());
  r = cmd("POT 256");     CHECK(starts(r, "ERR POT out of range"), "%s", r.c_str());
  r = cmd("MODE BURST 5 100"); CHECK(starts(r, "ERR MODE BURST"), "%s", r.c_str());
  r = cmd("MODE BURST 1000 1000"); CHECK(starts(r, "ERR MODE BURST"), "%s", r.c_str());
  r = cmd("STATUS");
  CHECK(has(r, "armed=1 run=0 freq=25000 amp=200 pot=200 mode=CONT") && has(r, "est=2.50") &&
        has(r, "limit=20.0") && has(r, "power=1") && has(r, "cal=1.000"), "%s", r.c_str());

  // ---- 4. idle output stays at mid-scale ----
  g_dac.clear(); ticks(100);
  CHECK(*std::min_element(g_dac.begin(), g_dac.end()) == 32768 &&
        *std::max_element(g_dac.begin(), g_dac.end()) == 32768, "idle at mid-scale");

  // ---- 5. continuous 25 kHz, 200 mV ----
  r = cmd("START");
  CHECK(r == "OK START", "%s", r.c_str());
  CHECK(g_pins[3] == HIGH, "LED_TX on");
  g_dac.clear(); ticks(5000);
  int mn = 65535, mx = 0;
  for (size_t i = 1000; i < g_dac.size(); i++) { mn = std::min<int>(mn, g_dac[i]); mx = std::max<int>(mx, g_dac[i]); }
  int cr = 0;
  for (size_t i = 1001; i < g_dac.size(); i++) if (g_dac[i - 1] < 32768 && g_dac[i] >= 32768) cr++;
  CHECK(mn == 28399 && mx == 37136, "min/max %d/%d expect 28399/37136", mn, mx);
  CHECK(cr == 200, "crossings %d expect 200", cr);
  // amplitude ramp: first 50 samples grow gradually (no click)
  int firstPk = 0;
  for (int i = 0; i < 10; i++) firstPk = std::max(firstPk, abs((int)g_dac[i] - 32768));
  CHECK(firstPk < 4369 / 3, "start is ramped, first peak %d", firstPk);

  // ---- 6. limit rejections leave the running output unchanged ----
  r = cmd("POT 0");      CHECK(r == "OK POT 0", "%s", r.c_str());          // 11.07 Vpp
  r = cmd("AMP 1400");   CHECK(starts(r, "ERR LIMIT"), "%s", r.c_str());   // 77 Vpp
  r = cmd("POT 200");    CHECK(r == "OK POT 200", "%s", r.c_str());
  r = cmd("AMP 1400");   CHECK(r == "OK AMP 1400", "%s", r.c_str());       // 17.5 Vpp
  r = cmd("POT 100");    CHECK(starts(r, "ERR LIMIT"), "%s", r.c_str());   // 47.6 Vpp
  r = cmd("LIMIT 10");   CHECK(starts(r, "ERR LIMIT current"), "%s", r.c_str());
  r = cmd("CAL 1.2");    CHECK(starts(r, "ERR LIMIT"), "%s", r.c_str());   // 21 Vpp
  r = cmd("STATUS");
  CHECK(has(r, "amp=1400 pot=200") && has(r, "limit=20.0") && has(r, "cal=1.000") && has(r, "run=1"), "%s", r.c_str());
  CHECK(g_potReg == 200, "pot unchanged by rejected command");
  r = cmd("AMP 200");    CHECK(r == "OK AMP 200", "%s", r.c_str());

  // ---- 7. bursts: 1 ms every 10 ms, repeatable start phase ----
  r = cmd("MODE BURST 1000 10000");
  CHECK(r == "OK MODE BURST 1000 10000", "%s", r.c_str());
  // Switched while transmitting: starts in the off part (ramp down), first burst at 4500.
  g_dac.clear(); ticks(20000);   // 40 ms -> bursts at 4500, 9500, 14500, 19500
  int active = 0;
  for (auto v : g_dac) if (abs((int)v - 32768) > 50) active++;
  CHECK(active > 1800 && active < 2300, "burst active samples %d expect ~2000", active);
  int firstMove = 0;
  for (int i = 0; i < 100; i++) firstMove = std::max(firstMove, abs((int)g_dac[i] - 32768));
  CHECK(firstMove <= 4369, "switch to burst mid-run ramps down (no jump), %d", firstMove);
  bool same = true;
  for (int k = 1; k < 3; k++)
    for (int i = 0; i < 600; i++) if (g_dac[4500 + 5000 * k + i] != g_dac[4500 + i]) same = false;
  CHECK(same, "every burst starts identically (phase 0 + same ramp)");
  CHECK(abs((int)g_dac[4500] - 32768) < 200, "burst starts near zero amplitude");
  bool quiet = true;
  for (int i = 100; i < 4500; i++) if (g_dac[i] != 32768) quiet = false;
  for (int i = 5100; i < 9500; i++) if (g_dac[i] != 32768) quiet = false;
  CHECK(quiet, "silent between bursts");

  // ---- 8. thermal warning at 90 kHz / ~50 Vpp continuous ----
  r = cmd("MODE CONT");  CHECK(r == "OK MODE CONT", "%s", r.c_str());
  r = cmd("FREQ 90000"); CHECK(r == "OK FREQ 90000", "%s", r.c_str());
  r = cmd("LIMIT 50");   CHECK(r == "OK LIMIT 50.0", "%s", r.c_str());
  r = cmd("POT 0");      CHECK(r == "OK POT 0", "%s", r.c_str());
  r = cmd("AMP 900");    // 49.8 Vpp * 90 kHz -> warning, while running
  CHECK(starts(r, "OK AMP 900 | WARN THERMAL"), "%s", r.c_str());
  r = cmd("STATUS");     CHECK(!has(r, "WARN"), "warning only once: %s", r.c_str());
  r = cmd("STOP");       CHECK(r == "OK STOP", "%s", r.c_str());
  r = cmd("START");      CHECK(starts(r, "OK START | WARN THERMAL"), "%s", r.c_str());

  // ---- 9. thermal auto-stop ----
  r = cmd("THERMSTOP 2"); CHECK(r == "OK THERMSTOP 2", "%s", r.c_str());
  Serial.out.clear();
  advanceMs(2100);
  bool autoStop = false;
  for (auto& s : Serial.out) if (starts(s, "WARN THERMAL auto-stop")) autoStop = true;
  CHECK(autoStop && !running, "thermal auto-stop fired");
  CHECK(g_pins[3] == LOW, "LED_TX off after auto-stop");
  g_dac.clear(); ticks(200);
  CHECK(g_dac.back() == 32768, "after stop at mid-scale, got %u", g_dac.back());

  // ---- 10. misc commands ----
  r = cmd("AMP 1500");   CHECK(starts(r, "ERR AMP out of range"), "%s", r.c_str());
  r = cmd("BOGUS");      CHECK(starts(r, "ERR unknown command"), "%s", r.c_str());
  r = cmd("AMP");        CHECK(starts(r, "ERR AMP needs"), "%s", r.c_str());
  r = cmd("AMP NAN");    CHECK(starts(r, "ERR AMP out of range"), "NaN rejected: %s", r.c_str());
  r = cmd("FREQ nan");   CHECK(starts(r, "ERR FREQ out of range"), "NaN rejected: %s", r.c_str());
  r = cmd("LIMIT NAN");  CHECK(starts(r, "ERR LIMIT out of range"), "NaN rejected: %s", r.c_str());
  r = cmd("CAL NAN");    CHECK(starts(r, "ERR CAL out of range"), "NaN rejected: %s", r.c_str());
  {
    std::string longLine(150, 'A');
    r = cmd(longLine.c_str());
    CHECK(r == "ERR line too long", "%s", r.c_str());
    r = cmd("PING");     CHECK(starts(r, "PONG"), "recovers after long line: %s", r.c_str());
  }
  r = cmd("AMP 200");    CHECK(r == "OK AMP 200", "%s", r.c_str());   // 11.03 Vpp (G(0) = 1.01)
  r = cmd("CAL 1.2");    CHECK(r == "OK CAL 1.200", "%s", r.c_str());
  r = cmd("STATUS");     CHECK(has(r, "est=13.24") && has(r, "cal=1.200"), "%s", r.c_str());
  r = cmd("CAL 1");      CHECK(r == "OK CAL 1.000", "%s", r.c_str());
  r = cmd("POTREAD");    CHECK(r == "OK POTREAD 0", "%s", r.c_str());
  r = cmd("VERSION");    CHECK(starts(r, "OK VERSION TXFW"), "%s", r.c_str());

  // ---- 11. sweep ----
  r = cmd("POT 200");    CHECK(r == "OK POT 200", "%s", r.c_str());
  r = cmd("SWEEP 20000 40000 10"); CHECK(r == "OK SWEEP 20000 40000 10", "%s", r.c_str());
  r = cmd("START");      CHECK(r == "OK START", "%s", r.c_str());
  g_us += 5000; loop();
  double fNow = (double)phaseStep / 4294967296.0 * 500000.0;
  CHECK(fabs(fNow - 30000.0) < 50.0, "sweep mid-point freq %.1f expect 30000", fNow);
  r = cmd("SWEEP OFF");  CHECK(r == "OK SWEEP OFF", "%s", r.c_str());
  fNow = (double)phaseStep / 4294967296.0 * 500000.0;
  CHECK(fabs(fNow - 90000.0) < 1.0, "back to FREQ after sweep off: %.1f", fNow);
  r = cmd("STOP");

  // ---- 12. supply glitch: pot reset to mid-scale is detected and rewritten ----
  g_potReg = 128;
  Serial.out.clear();
  advanceMs(600);
  bool potReset = false;
  for (auto& s : Serial.out) if (starts(s, "WARN POTRESET")) potReset = true;
  CHECK(potReset && g_potReg == 200, "pot reset detected (reg=%d)", g_potReg);

  // ---- 13. DISARM: pot min gain BEFORE the DC ramp, ends at code 0, pins released ----
  size_t dacBefore = g_dac.size();
  size_t potBefore = g_pot.size();
  r = cmd("DISARM");
  CHECK(r == "OK DISARMED", "%s", r.c_str());
  CHECK(g_pot.size() > potBefore && g_pot.back() == 255, "pot at 255");
  CHECK(g_potAtDac[potBefore] == dacBefore, "pot set to 255 before the ramp-down");
  CHECK(g_dac.back() == 0, "last DAC word %u", g_dac.back());
  CHECK(g_dac.size() - dacBefore > 500, "ramp-down ~50 ms");
  CHECK(boardPinsReleased(), "pins released after DISARM");
  CHECK(!dacTimer.on, "timer stopped");
  r = cmd("STATUS");     CHECK(has(r, "armed=0"), "%s", r.c_str());
  CHECK(boardPinsReleased(), "STATUS power check leaves pins released");
  r = cmd("START");      CHECK(starts(r, "ERR NOTARMED"), "%s", r.c_str());
  r = cmd("DISARM");     CHECK(r == "OK DISARMED", "DISARM is idempotent: %s", r.c_str());

  // ---- 14. NOPOT: power present but pot does not answer ----
  // (simulate by power check passing but I2C failing: flip power between check and write is not
  //  possible with this stub, so check the POT command path instead)
  r = cmd("POT 200");    CHECK(r == "OK POT 200", "stored while disarmed: %s", r.c_str());

  // ---- 15. 12 V switched off while armed and transmitting -> watchdog releases pins ----
  r = cmd("MODE CONT");  r = cmd("FREQ 25000");
  r = cmd("ARM");        CHECK(r == "OK ARMED", "%s", r.c_str());
  r = cmd("START");      CHECK(r == "OK START", "%s", r.c_str());
  g_power = false;
  int bpBefore = g_backpower;
  Serial.out.clear();
  advanceMs(1200);
  bool noPower = false;
  for (auto& s : Serial.out) if (starts(s, "WARN NOPOWER")) noPower = true;
  CHECK(noPower && !armed && !running, "watchdog disarmed after power loss");
  CHECK(boardPinsReleased() && !dacTimer.on, "pins released, timer stopped");
  CHECK(g_backpower == bpBefore, "emergency release drives nothing");
  r = cmd("ARM");        CHECK(starts(r, "ERR NOPOWER"), "%s", r.c_str());

  // ---- 16. SYNC output (RX spec Appendix C): pin 2 pulses 8 ticks at every burst start ----
  g_power = true;
  CHECK(g_pinmode[2] == OUTPUT && g_pins[2] == LOW, "SYNC pin is an output, low");
  r = cmd("ARM");        CHECK(r == "OK ARMED", "%s", r.c_str());
  r = cmd("SYNC ON");    CHECK(r == "OK SYNC ON", "%s", r.c_str());
  r = cmd("STATUS");     CHECK(has(r, " sync=1"), "%s", r.c_str());
  r = cmd("MODE BURST 1000 10000");
  r = cmd("START");
  {
    std::vector<int> rise, high;
    int prev = g_pins[2], run = 0;
    g_dac.clear();
    for (int i = 0; i < 20000; i++) {
      dacTick();
      int p = g_pins[2];
      if (p && !prev) rise.push_back(i);
      if (p) run++;
      else if (run) { high.push_back(run); run = 0; }
      prev = p;
    }
    CHECK(rise.size() == 4, "4 sync pulses in 40 ms, got %zu", rise.size());
    bool spacing = rise.size() == 4;
    for (size_t k = 1; k < rise.size(); k++) if (rise[k] - rise[k - 1] != 5000) spacing = false;
    CHECK(spacing && rise[0] == 0, "pulses every 5000 ticks starting at the burst start");
    bool width = !high.empty();
    for (int w : high) if (w != 8) width = false;
    CHECK(width, "pulse width 8 ticks (16 us)");
    CHECK(abs((int)g_dac[0] - 32768) < 200, "pulse coincides with the burst start (phase 0, ramp)");
  }
  r = cmd("STOP");
  r = cmd("MODE CONT");
  r = cmd("START");
  {
    int pulses = 0, prev = g_pins[2];
    for (int i = 0; i < 5000; i++) { dacTick(); if (g_pins[2] && !prev) pulses++; prev = g_pins[2]; }
    CHECK(pulses == 1, "CONT: one pulse at START, got %d", pulses);
  }
  r = cmd("SYNC OFF");   CHECK(r == "OK SYNC OFF", "%s", r.c_str());
  r = cmd("STOP");
  r = cmd("START");
  {
    int pulses = 0;
    for (int i = 0; i < 2000; i++) { dacTick(); if (g_pins[2]) pulses++; }
    CHECK(pulses == 0, "no pulse with SYNC OFF");
  }
  r = cmd("SYNC 1");     CHECK(starts(r, "ERR SYNC"), "%s", r.c_str());
  r = cmd("DISARM");

  printf("\nfirmware host tests: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
