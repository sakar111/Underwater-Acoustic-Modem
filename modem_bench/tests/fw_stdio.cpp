// Host build of the real firmware that talks over stdin/stdout.
// Used by test_fake_vs_firmware.py to prove the Python fake Teensy replies exactly
// like the firmware. One input line -> one output line (all replies joined by " | ").
// Directives:  #POWER 0|1  (board 12 V)   #MS <n>  (run the main loop for n ms)
#include <iostream>
#include "host_globals.h"

int main() {
  setup();
  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    Serial.out.clear();
    if (line.rfind("#POWER", 0) == 0) {
      g_power = atoi(line.c_str() + 6) != 0;
    } else if (line.rfind("#MS", 0) == 0) {
      long n = atol(line.c_str() + 3);
      for (long i = 0; i < n; i += 10) { g_ms += 10; g_us += 10000; loop(); }
    } else {
      for (char c : line) Serial.in.push_back(c);
      Serial.in.push_back('\n');
      loop();
    }
    std::string r;
    for (size_t i = 0; i < Serial.out.size(); i++) { if (i) r += " | "; r += Serial.out[i]; }
    std::cout << r << std::endl;
  }
  return 0;
}
