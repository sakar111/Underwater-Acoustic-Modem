// Minimal parser for the RX device->host byte stream (lines + frames), test side.
// Include after rx_host_globals.h (uses the firmware's crc32()).
#pragma once
#include <string>
#include <vector>

struct Frame {
  uint8_t version, type; uint16_t flags; uint32_t seq; uint64_t first; uint16_t n;
  uint8_t gain, pot; uint16_t capId, trigOff, syncOff, fsDiv;
  std::vector<uint16_t> w;
};
inline uint16_t g16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }
inline uint32_t g32(const uint8_t* p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }
inline uint64_t g64(const uint8_t* p) { return (uint64_t)g32(p) | ((uint64_t)g32(p + 4) << 32); }

struct Parsed { std::vector<std::string> lines; std::vector<Frame> frames; int crcErr = 0, junk = 0; };
inline Parsed parse(const std::vector<uint8_t>& b) {
  Parsed r;
  size_t i = 0;
  while (i < b.size()) {
    if (b[i] == 0xA5) {
      if (b.size() - i < 32 || memcmp(&b[i], "\xA5\x5ARX", 4) != 0) { r.junk++; i++; continue; }
      Frame f;
      const uint8_t* h = &b[i];
      f.version = h[4]; f.type = h[5]; f.flags = g16(h + 6); f.seq = g32(h + 8); f.first = g64(h + 12);
      f.n = g16(h + 20); f.gain = h[22]; f.pot = h[23]; f.capId = g16(h + 24); f.trigOff = g16(h + 26);
      f.syncOff = g16(h + 28); f.fsDiv = g16(h + 30);
      size_t len = 32 + 2 * (size_t)f.n;
      if (f.n < 1 || f.n > 4096 || b.size() - i < len + 4) { r.junk++; i++; continue; }
      if (crc32(h, len) != g32(h + len)) { r.crcErr++; i++; continue; }
      for (unsigned k = 0; k < f.n; k++) f.w.push_back(g16(h + 32 + 2 * k));
      r.frames.push_back(f);
      i += len + 4;
    } else {
      size_t k = i;
      while (k < b.size() && b[k] != '\n') k++;
      std::string s(b.begin() + i, b.begin() + k);
      while (!s.empty() && s.back() == '\r') s.pop_back();
      r.lines.push_back(s);
      i = k + 1;
    }
  }
  return r;
}

