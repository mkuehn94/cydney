#pragma once

#include <stdint.h>

struct TouchGesture {
  enum Result { Pending, Tap, Drag };
  static constexpr int DRAG_PIXELS = 6;
  static constexpr uint32_t RELEASE_MS = 80;
  static constexpr uint32_t MIN_TAP_MS = 20;

  bool active = false, dragged = false;
  int startX = 0, startY = 0, lastX = 0, lastY = 0;
  uint32_t lastSeen = 0, started = 0;
  unsigned samples = 0;

  int dx() const { return lastX - startX; }
  int dy() const { return lastY - startY; }

  Result update(bool down, int x, int y, uint32_t now, int dragPixels = DRAG_PIXELS) {
    if (down) {
      if (!active) {
        active = true;
        dragged = false;
        startX = x;
        startY = y;
        started = now;
        samples = 0;
      }
      ++samples;
      lastX = x;
      lastY = y;
      lastSeen = now;
      // moved once = drag, even if it comes back to the start
      dragged = dragged || dx() * dx() + dy() * dy() >= dragPixels * dragPixels;
      return Pending;
    }
    // resistive touch looses pressure for a moment sometimes
    if (!active || uint32_t(now - lastSeen) < RELEASE_MS) return Pending;
    active = false;
    if (dragged) return Drag;
    // single sample is probably noise
    return samples >= 3 && uint32_t(lastSeen - started) >= MIN_TAP_MS ? Tap : Pending;
  }
};
