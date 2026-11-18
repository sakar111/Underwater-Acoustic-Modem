// Definitions of the recorded stub state + the firmware itself, for host builds.
// Include exactly once, from the test main.
#pragma once
#include "Arduino.h"

std::vector<uint16_t> g_dac;
int  g_pins[64];
int  g_pinmode[64];
bool g_power = true;
std::vector<int> g_pot;
std::vector<size_t> g_potAtDac;
int  g_potReg = 128;          // AD5245 power-on default: mid-scale
int  g_backpower = 0;
unsigned long g_ms = 0, g_us = 0;

SPIClass SPI1;
TwoWire  Wire2;
SerialC  Serial;

#include "../../tx_firmware/tx_firmware.ino"
