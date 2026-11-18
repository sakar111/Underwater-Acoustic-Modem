// Definitions of the RX stub state + the firmware itself, for host builds.
// Include exactly once, from the test main.
#pragma once
#include "Arduino.h"

int  g_pins[64];
int  g_pinmode[64];
bool g_power = true;
bool g_wrongAlign = false;
int  g_potReg = 128;                      // AD5245 power-on default: mid-scale
std::vector<int> g_potWrites;
long long g_k = 0;
std::function<int(long long)> g_sig;
std::function<int(long long)> g_sync;
unsigned long g_ticks = 0, g_extraMs = 0;
uint32_t g_cyc = 0;
int g_spiErrors = 0;
uint32_t g_demcr = 0, g_dwtctrl = 0;

SPIClass SPI1;
TwoWire  Wire2;
SerialC  Serial;

#include "../../rx_firmware/rx_firmware.ino"

// ---- test-driver helpers ----
// Run n sample periods: the ISR fires while the timer is on, loop() runs every `loopEvery`
// samples (0 = never, to force an overflow).
inline void runSamples(long n, int loopEvery = 256) {
  for (long i = 0; i < n; i++) {
    if (adcTimer.on) { g_k = (long long)head; adcTick(); }
    g_ticks++;
    if (loopEvery > 0 && (i % loopEvery) == loopEvery - 1) loop();
  }
}
