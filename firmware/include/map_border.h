#pragma once
#include <stdint.h>

struct MapBorder {
  static constexpr int Margin = 128, Scale = 8;
  static constexpr int Width = (320 + 2 * Margin) / Scale;
  static constexpr int Height = (240 + 2 * Margin) / Scale;
  bool valid = false;
  int x = 0, y = 0, zoom = 0;

  static int clampTo(int value, int low, int high) {
    return value < low ? low : value > high ? high : value;
  }
  int deltaX(int centerX) const {
    int size = 256 << zoom, dx = centerX - x;
    if (dx > size / 2) dx -= size;
    if (dx < -size / 2) dx += size;
    return dx;
  }
  bool covers(int centerX, int centerY, int currentZoom) const {
    if (!valid || zoom != currentZoom) return false;
    int dx = deltaX(centerX), dy = centerY - y;
    return dx >= -Margin && dx <= Margin && dy >= -Margin && dy <= Margin;
  }
  bool coversDrag(int centerX, int centerY, int currentZoom, int dx, int dy) const {
    if (!valid || zoom != currentZoom) return false;
    int cx = deltaX(centerX) - dx, cy = centerY - y - dy;
    return cx >= -Margin && cx <= Margin && cy >= -Margin && cy <= Margin;
  }
  int sourceX(int screenX, int centerX, int dx) const {
    return clampTo((screenX + Margin + deltaX(centerX) - dx) / Scale, 0, Width - 1);
  }
  int sourceY(int screenY, int centerY, int dy) const {
    return clampTo((screenY + Margin + centerY - y - dy) / Scale, 0, Height - 1);
  }
};

// same positions as the buttons in the frame
// a bit more tolerance on the buttons because the touch jitters
inline int mapZoomButton(int x, int y) {
  if (y < 4 || y >= 36) return 0;
  if (x >= 212 && x < 244) return -1;
  if (x >= 248 && x < 280) return 1;
  return 0;
}

inline int menuZoomButton(int x, int y) {
  if (y < 96 || y >= 128) return 0;
  if (x >= 4 && x < 156) return -1;
  if (x >= 164 && x < 316) return 1;
  return 0;
}
