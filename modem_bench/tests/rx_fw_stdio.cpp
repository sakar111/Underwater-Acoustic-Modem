// Host build of rx_firmware.ino over stdin/stdout, for test_rx_fake_vs_firmware.py.
// One input line -> one output line: the text lines the device sent (joined by " | "), plus
// " [frames=N]" when binary frames were sent. Signal: DC 2050, no noise, SYNC low.
// Directives:  #POWER 0|1   #MS <n> (sample + run the main loop for n ms)   #DTR 0|1
//              #POTRESET (AD5245 back to mid-scale, as after a supply glitch)
#include <iostream>
#include "rx_host_globals.h"
#include "rx_stream_parse.h"

int main() {
  g_sig = [](long long) { return 2050; };
  setup();
  std::string line;
  while (std::getline(std::cin, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    size_t from = Serial.out.size();
    if (line.rfind("#POWER", 0) == 0) {
      g_power = atoi(line.c_str() + 6) != 0;
    } else if (line.rfind("#MS", 0) == 0) {
      runSamples(atol(line.c_str() + 3) * 400L);
    } else if (line.rfind("#DTR", 0) == 0) {
      Serial.dtrState = atoi(line.c_str() + 4) != 0;
    } else if (line == "#POTRESET") {
      g_potReg = 128;
    } else {
      for (char c : line) Serial.in.push_back(c);
      Serial.in.push_back('\n');
      loop();
    }
    Parsed p = parse(std::vector<uint8_t>(Serial.out.begin() + from, Serial.out.end()));
    std::string r;
    for (size_t i = 0; i < p.lines.size(); i++) { if (i) r += " | "; r += p.lines[i]; }
    if (!p.frames.empty()) r += " [frames=" + std::to_string(p.frames.size()) + "]";
    if (p.crcErr || p.junk) r += " [BAD STREAM]";
    std::cout << r << std::endl;
  }
  return 0;
}
