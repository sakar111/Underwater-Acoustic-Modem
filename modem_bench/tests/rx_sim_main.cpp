// Host-side tests for rx_firmware.ino (spec §9.1, Appendix B.1 scenarios 1-9 plus extras).
// Build + run:  python tests/run_all.py     (or by hand:)
//   g++ -std=c++17 -O1 -Wall -Itests/host_stubs_rx -o build/rx_sim tests/rx_sim_main.cpp && build/rx_sim build
// The byte streams of scenarios 4-7 are written to <outdir>/rx_out_*.bin and re-parsed by
// tests/test_rx_firmware_stream.py with the app's Python parser.
#include <algorithm>
#include <map>
#include "rx_host_globals.h"

static int g_fail = 0, g_pass = 0;
#define CHECK(cond, ...)                                                   \
  do {                                                                     \
    if (cond) { g_pass++; }                                                \
    else { g_fail++; printf("FAIL line %d: %s  -- ", __LINE__, #cond);     \
           printf(__VA_ARGS__); printf("\n"); }                            \
  } while (0)

#include "rx_stream_parse.h"

// ---------------------------------------------------------------- helpers
static std::string cmd(const char* c) {             // one command, replies joined by " | "
  size_t from = Serial.out.size();
  for (const char* p = c; *p; p++) Serial.in.push_back(*p);
  Serial.in.push_back('\r');
  Serial.in.push_back('\n');
  loop();
  std::vector<uint8_t> part(Serial.out.begin() + from, Serial.out.end());
  Parsed p = parse(part);
  std::string r;
  for (size_t i = 0; i < p.lines.size(); i++) { if (i) r += " | "; r += p.lines[i]; }
  return r;
}
static bool starts(const std::string& s, const char* p) { return s.rfind(p, 0) == 0; }
static bool has(const std::string& s, const char* p) { return s.find(p) != std::string::npos; }
static bool pinsReleased() {
  for (int p : {10, 24, 25, 26, 27, 28, 39}) if (g_pinmode[p] != INPUT) return false;
  return true;
}
static bool hasLine(const Parsed& p, const char* prefix) {
  for (auto& s : p.lines) if (starts(s, prefix)) return true;
  return false;
}
static std::string lineWith(const Parsed& p, const char* prefix) {
  for (auto& s : p.lines) if (starts(s, prefix)) return s;
  return "";
}
static std::string g_outDir = "build";
static void dump(const char* name, size_t from) {
  std::string path = g_outDir + "/" + name;
  FILE* f = fopen(path.c_str(), "wb");
  if (!f) { printf("FAIL: cannot write %s\n", path.c_str()); g_fail++; return; }
  fwrite(Serial.out.data() + from, 1, Serial.out.size() - from, f);
  fclose(f);
}
static int sigBurst(long long k) {               // DC 2050 + 40 kHz, 400-count burst at 60000..60799
  if (k >= 60000 && k < 60800) return 2050 + (int)lround(400.0 * sin(6.283185307179586 * 40000.0 * (double)(k - 60000) / 400000.0));
  return 2050;
}

int main(int argc, char** argv) {
  if (argc > 1) g_outDir = argv[1];
  g_sig = sigBurst;
  g_sync = [](long long k) { return (k >= 80000 && k < 80008) ? 1 : 0; };

  setup();
  CHECK(pinsReleased(), "board pins INPUT after setup");
  CHECK(g_pinmode[2] == INPUT_PULLDOWN, "SYNC pin pulled down");
  CHECK(g_pinmode[3] == OUTPUT && g_pinmode[8] == OUTPUT, "LEDs are outputs");
  CHECK(!SPI1.begun && !Wire2.begun && g_potWrites.empty(), "no SPI/I2C at boot");

  // ---- 1. ARM with the board unpowered ----
  g_power = false;
  std::string r = cmd("ARM");
  CHECK(starts(r, "ERR NOPOWER"), "%s", r.c_str());
  CHECK(pinsReleased() && !adcTimer.on && !SPI1.begun, "pins released, nothing started");
  CHECK(g_potWrites.empty(), "no pot write without power");
  r = cmd("STATUS");
  CHECK(has(r, "armed=0") && has(r, "power=0"), "%s", r.c_str());
  CHECK(pinsReleased() && !Wire2.begun, "STATUS power check releases the pins again");
  r = cmd("CAPTURE 100 0");  CHECK(r == "ERR NOTARMED", "%s", r.c_str());
  r = cmd("STREAM ON");      CHECK(r == "ERR NOTARMED", "%s", r.c_str());

  // ---- 2. wrong bit alignment caught by the self-test ----
  g_power = true;
  g_wrongAlign = true;
  r = cmd("ARM");
  CHECK(starts(r, "ERR ADC self-test failed: mean=5 frame_errors=64"), "%s", r.c_str());
  CHECK(pinsReleased() && !adcTimer.on && !SPI1.begun && !Wire2.begun, "released after self-test failure");
  g_wrongAlign = false;

  // ---- 3. normal ARM, gain, pot, RAW ----
  r = cmd("PING");     CHECK(r == "PONG RXFW " RXFW_VERSION, "%s", r.c_str());
  g_potWrites.clear();
  r = cmd("arm");
  CHECK(r == "OK ARMED mid=2050", "%s", r.c_str());
  CHECK(SPI1.misoSetBeforeBegin, "SPI1.setMISO(39) before begin()");
  CHECK(SPI1.clk == 12000000 && SPI1.mode == SPI_MODE0, "SPI 12 MHz mode 0");
  CHECK(!g_potWrites.empty() && g_potWrites[0] == 0, "pot written to 0 at ARM");
  CHECK(Wire2.clock == 400000, "I2C at 400 kHz after the power check");
  CHECK(g_pinmode[10] == OUTPUT && g_pins[10] == HIGH, "pin 10 HIGH = LOW gain by default");
  CHECK(g_pinmode[28] == OUTPUT && g_pins[28] == HIGH, "CS idle high");
  CHECK(adcTimer.on && adcTimer.period == 2.5f && adcTimer.prio == 0, "2.5 us timer, priority 0");
  r = cmd("GAIN HIGH");  CHECK(r == "OK GAIN HIGH" && g_pins[10] == LOW, "%s", r.c_str());
  r = cmd("GAIN LOW");   CHECK(r == "OK GAIN LOW" && g_pins[10] == HIGH, "%s", r.c_str());
  r = cmd("GAIN MID");   CHECK(starts(r, "ERR GAIN"), "%s", r.c_str());
  r = cmd("POT 200");    CHECK(r == "OK POT 200" && g_potReg == 200, "%s", r.c_str());
  r = cmd("POTREAD");    CHECK(r == "OK POTREAD 200", "%s", r.c_str());
  r = cmd("POT 256");    CHECK(starts(r, "ERR POT out of range"), "%s", r.c_str());
  r = cmd("POT -1");     CHECK(starts(r, "ERR POT out of range"), "%s", r.c_str());
  r = cmd("POT");        CHECK(starts(r, "ERR POT needs"), "%s", r.c_str());
  r = cmd("RAW 4");
  CHECK(starts(r, "OK RAW 0802"), "%s", r.c_str());
  {
    bool ok = true;
    unsigned v;
    const char* p = r.c_str() + 6;
    int cnt = 0;
    while (sscanf(p, " %4x", &v) == 1) { if (v != 0x0802) ok = false; cnt++; p += 5; }
    CHECK(ok && cnt == 4, "RAW words 0x0802: %s", r.c_str());
  }
  CHECK(adcTimer.on, "timer restarted after RAW");
  r = cmd("RAW 17");     CHECK(starts(r, "ERR RAW"), "%s", r.c_str());
  r = cmd("POT 0");      CHECK(r == "OK POT 0", "%s", r.c_str());
  r = cmd("STATUS");
  CHECK(r == "STATUS armed=1 power=1 gain=LOW pot=0 fs=400000 stream=0 cap=IDLE trig=NONE level=200 mid=2050 sync=0 ovf=0 ferr=0",
        "%s", r.c_str());

  // ---- 4. streaming ----
  runSamples(1000);
  r = cmd("STATS");      // reset the timing statistics
  size_t from = Serial.out.size();
  r = cmd("STREAM ON");  CHECK(r == "OK STREAM ON", "%s", r.c_str());
  long long startHead = head;
  runSamples(20480);
  r = cmd("STREAM OFF");
  dump("rx_out_stream.bin", from);
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(p.crcErr == 0 && p.junk == 0, "crc %d junk %d", p.crcErr, p.junk);
    CHECK(hasLine(p, "OK STREAM OFF"), "OK STREAM OFF parsed between frames");
    CHECK(p.frames.size() == 20, "20 frames, got %zu", p.frames.size());
    bool contiguous = true, all2050 = true, types = true;
    for (size_t i = 0; i < p.frames.size(); i++) {
      const Frame& f = p.frames[i];
      if (f.n != 1024 || f.type != 1 || f.capId != 0 || f.fsDiv != 4000 || f.gain != 1 || f.version != 1) types = false;
      if (i && (f.first != p.frames[i - 1].first + 1024 || f.seq != p.frames[i - 1].seq + 1)) contiguous = false;
      for (auto w : f.w) if (w != 2050) all2050 = false;
    }
    CHECK(types, "STREAM headers");
    CHECK(contiguous, "first_index / seq contiguous");
    CHECK(all2050, "payload = 2050");
    CHECK(!p.frames.empty() && p.frames[0].first % 1024 == 0 && (long long)p.frames[0].first <= startHead,
          "block-aligned start %llu", (unsigned long long)p.frames[0].first);
  }
  r = cmd("STATS");
  CHECK(has(r, "frames=20") && has(r, "ovf=0") && has(r, "ferr=0") && has(r, "per_min_ns=2500") &&
        has(r, "per_max_ns=2500") && has(r, "isr_max_ns=1250"), "%s", r.c_str());

  // ---- 5. LEVEL-triggered capture ----
  r = cmd("TRIG LEVEL 300");     CHECK(r == "OK TRIG LEVEL 300", "%s", r.c_str());
  r = cmd("TRIG LEVEL 0");       CHECK(starts(r, "ERR TRIG LEVEL out of range"), "%s", r.c_str());
  r = cmd("TRIG LEVEL 2048");    CHECK(starts(r, "ERR TRIG LEVEL out of range"), "%s", r.c_str());
  r = cmd("CAPTURE 10 0");       CHECK(starts(r, "ERR CAPTURE n out of range"), "%s", r.c_str());
  r = cmd("CAPTURE 100 100");    CHECK(r == "ERR CAPTURE pre out of range 0..99", "%s", r.c_str());
  from = Serial.out.size();
  r = cmd("CAPTURE 5000 1000");  CHECK(r == "OK CAPTURE id=1", "%s", r.c_str());
  r = cmd("STREAM ON");          CHECK(starts(r, "ERR BUSY"), "%s", r.c_str());
  r = cmd("TRIG NONE");          CHECK(starts(r, "ERR BUSY"), "%s", r.c_str());
  r = cmd("STATUS");             CHECK(has(r, "cap=WAIT") && has(r, "trig=LEVEL level=300"), "%s", r.c_str());
  runSamples(70000 - (long)head);
  dump("rx_out_level.bin", from);
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(p.crcErr == 0, "crc %d", p.crcErr);
    CHECK(lineWith(p, "EVT TRIG") == "EVT TRIG id=1 index=60002", "%s", lineWith(p, "EVT TRIG").c_str());
    CHECK(lineWith(p, "EVT CAPDONE") == "EVT CAPDONE id=1 n=5000 pre=1000 trig=60002", "%s",
          lineWith(p, "EVT CAPDONE").c_str());
    std::vector<uint16_t> all;
    bool ok = !p.frames.empty();
    for (size_t i = 0; i < p.frames.size(); i++) {
      const Frame& f = p.frames[i];
      if (f.type != 2 || f.capId != 1) ok = false;
      if (i && f.first != p.frames[i - 1].first + p.frames[i - 1].n) ok = false;
      all.insert(all.end(), f.w.begin(), f.w.end());
    }
    CHECK(ok, "CAPTURE frames contiguous, id 1");
    CHECK(!p.frames.empty() && p.frames[0].first == 59002, "first frame at 59002, got %llu",
          p.frames.empty() ? 0ull : (unsigned long long)p.frames[0].first);
    CHECK(all.size() == 5000, "5000 samples, got %zu", all.size());
    bool same = all.size() == 5000;
    for (size_t i = 0; same && i < all.size(); i++) if (all[i] != sigBurst(59002 + (long long)i)) same = false;
    CHECK(same, "samples identical to g_sig");
    CHECK(!p.frames.empty() && (p.frames.front().flags & 0x0004) && (p.frames.back().flags & 0x0008),
          "CAP_FIRST / CAP_LAST");
    CHECK(!p.frames.empty() && p.frames[0].trigOff == 1000, "trig_off 1000");
    int trigFrames = 0;
    for (auto& f : p.frames) if (f.trigOff != 0xFFFF) trigFrames++;
    CHECK(trigFrames == 1, "exactly one frame carries trig_off");
  }
  r = cmd("STATUS");  CHECK(has(r, "cap=IDLE"), "%s", r.c_str());

  // ---- 6. SYNC-triggered capture ----
  r = cmd("TRIG SYNC");          CHECK(r == "OK TRIG SYNC", "%s", r.c_str());
  from = Serial.out.size();
  r = cmd("CAPTURE 3000 100");   CHECK(r == "OK CAPTURE id=2", "%s", r.c_str());
  runSamples(85000 - (long)head);
  dump("rx_out_sync.bin", from);
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(lineWith(p, "EVT TRIG") == "EVT TRIG id=2 index=80000", "%s", lineWith(p, "EVT TRIG").c_str());
    CHECK(!p.frames.empty() && p.frames[0].first == 79900, "capture starts at 79900");
    CHECK(!p.frames.empty() && p.frames[0].syncOff == 100 && (p.frames[0].flags & 0x0020), "sync_off 100");
    CHECK(!p.frames.empty() && p.frames[0].trigOff == 100, "trig_off 100");
    int nsync = 0;
    size_t total = 0;
    for (auto& f : p.frames) { total += f.n; for (auto w : f.w) if (w & 0x8000) nsync++; }
    CHECK(nsync == 8, "8 words with bit 15, got %d", nsync);
    CHECK(total == 3000, "3000 samples, got %zu", total);
  }

  // ---- extra: immediate capture, ABORT ----
  r = cmd("TRIG NONE");          CHECK(r == "OK TRIG NONE", "%s", r.c_str());
  from = Serial.out.size();
  r = cmd("CAPTURE 2000 0");     CHECK(r == "OK CAPTURE id=3", "%s", r.c_str());
  runSamples(3000);
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(p.frames.size() == 2 && p.frames[0].n == 1024 && p.frames[1].n == 976, "2 frames 1024 + 976");
    CHECK(hasLine(p, "EVT CAPDONE id=3 n=2000 pre=0"), "CAPDONE 3");
  }
  r = cmd("TRIG LEVEL 2000");    // never reached
  r = cmd("CAPTURE 1000 10");    CHECK(r == "OK CAPTURE id=4", "%s", r.c_str());
  runSamples(2000);
  r = cmd("ABORT");              CHECK(r == "OK ABORT", "%s", r.c_str());
  r = cmd("STATUS");             CHECK(has(r, "cap=IDLE") && has(r, "stream=0"), "%s", r.c_str());

  // ---- 7. overflow: the PC stops reading ----
  from = Serial.out.size();
  r = cmd("STREAM ON");          CHECK(r == "OK STREAM ON", "%s", r.c_str());
  runSamples(100000, 0);         // loop() never runs: 100 000 samples > ring
  runSamples(8192);
  r = cmd("STREAM OFF");
  dump("rx_out_overflow.bin", from);
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(!p.frames.empty() && (p.frames[0].flags & 0x0002), "first frame after the stall has OVERFLOW");
    bool contiguous = true;
    int ovfFlags = 0;
    for (size_t i = 0; i < p.frames.size(); i++) {
      if (p.frames[i].flags & 0x0002) ovfFlags++;
      if (i && p.frames[i].first != p.frames[i - 1].first + 1024) contiguous = false;
    }
    CHECK(contiguous && ovfFlags == 1, "later frames contiguous (ovf flags %d)", ovfFlags);
  }
  r = cmd("STATS");
  {
    unsigned long ovf = 0;
    const char* q = strstr(r.c_str(), "ovf=");
    if (q) ovf = strtoul(q + 4, nullptr, 10);
    CHECK(ovf > 0, "STATS reports ovf > 0: %s", r.c_str());
  }
  r = cmd("STATUS");  CHECK(!has(r, "ovf=0 "), "STATUS ovf counts: %s", r.c_str());

  // ---- extra: host gone (DTR dropped) stops streaming, stays armed ----
  r = cmd("STREAM ON");
  runSamples(2048);
  Serial.dtrState = false;
  loop();
  CHECK(!streaming && armed, "DTR low -> streaming stopped, still armed");
  Serial.dtrState = true;

  // ---- extra: supply glitch resets the pot -> rewritten ----
  r = cmd("POT 77");
  g_potReg = 128;
  from = Serial.out.size();
  runSamples(240000);            // 600 ms
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(hasLine(p, "WARN POTRESET wiper read 128 expected 77"), "pot reset detected");
    CHECK(g_potReg == 77, "pot rewritten");
  }

  // ---- extra: line too long ----
  {
    std::string longLine(150, 'A');
    r = cmd(longLine.c_str());   CHECK(r == "ERR line too long", "%s", r.c_str());
    r = cmd("PING");             CHECK(starts(r, "PONG RXFW"), "%s", r.c_str());
  }

  // ---- 8. power removed while armed ----
  r = cmd("STREAM ON");
  g_power = false;
  from = Serial.out.size();
  runSamples(480000);            // 1.2 s
  {
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    CHECK(hasLine(p, "WARN POWERLOST"), "WARN POWERLOST printed");
  }
  CHECK(!armed && !streaming && !adcTimer.on && pinsReleased() && !SPI1.begun && !Wire2.begun,
        "disarmed, pins released");
  CHECK(g_pins[3] == LOW, "LED_TX off");

  // ---- 9. DISARM, then CAPTURE, then an unknown command ----
  g_power = true;
  r = cmd("ARM");               CHECK(starts(r, "OK ARMED mid=2050"), "%s", r.c_str());
  CHECK(g_pins[10] == HIGH && g_potReg == 77, "ARM reapplies stored gain and pot");
  r = cmd("ARM");               CHECK(r == "OK ARMED mid=2050", "ARM is idempotent: %s", r.c_str());
  r = cmd("DISARM");            CHECK(r == "OK DISARMED", "%s", r.c_str());
  CHECK(pinsReleased() && !adcTimer.on && !SPI1.begun && !Wire2.begun, "pins released");
  r = cmd("CAPTURE");           CHECK(r == "ERR NOTARMED", "%s", r.c_str());
  r = cmd("POTREAD");           CHECK(r == "ERR NOTARMED", "%s", r.c_str());
  r = cmd("BOGUS 1 2");         CHECK(r == "ERR unknown command: BOGUS", "%s", r.c_str());
  r = cmd("DISARM");            CHECK(r == "OK DISARMED", "DISARM is idempotent: %s", r.c_str());
  r = cmd("GAIN HIGH");         CHECK(r == "OK GAIN HIGH" && g_pinmode[10] == INPUT, "stored, pin not driven: %s", r.c_str());
  r = cmd("POT 12");            CHECK(r == "OK POT 12" && g_potReg == 77, "stored while disarmed: %s", r.c_str());

  CHECK(g_spiErrors == 0, "SPI transfers with CS high / wrong MISO / no transaction: %d", g_spiErrors);
  printf("\nrx firmware host tests: %d passed, %d failed\n", g_pass, g_fail);
  return g_fail ? 1 : 0;
}
