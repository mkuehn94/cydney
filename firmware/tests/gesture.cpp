#include <assert.h>
#include <initializer_list>
#include "gesture.h"
#include "map_border.h"

int main() {
  using G = TouchGesture;

  G tap;
  assert(tap.update(true, 100, 100, 0) == G::Pending);
  assert(tap.update(true, 102, 101, 10) == G::Pending);
  assert(tap.update(true, 101, 102, 20) == G::Pending);
  assert(tap.update(false, 0, 0, 30) == G::Pending);
  assert(tap.active);
  assert(tap.update(false, 0, 0, 100) == G::Tap);
  assert(!tap.active);
  assert(tap.update(false, 0, 0, 110) == G::Pending);

  // low pressure at first contact
  G swipe;
  swipe.update(true, 100, 100, 0);
  assert(swipe.update(false, 0, 0, 10) == G::Pending);
  assert(swipe.active);
  swipe.update(true, 107, 100, 30);
  assert(swipe.update(false, 0, 0, 40) == G::Pending);
  swipe.update(true, 160, 120, 60);
  assert(swipe.update(false, 0, 0, 139) == G::Pending);
  assert(swipe.update(false, 0, 0, 140) == G::Drag);
  assert(swipe.dx() == 60 && swipe.dy() == 20);
  assert(swipe.update(false, 0, 0, 150) == G::Pending);

  // small swipes are still drags
  G small;
  small.update(true, 100, 100, 0);
  small.update(true, 106, 100, 10);
  assert(small.update(false, 0, 0, 90) == G::Drag);
  G back;
  back.update(true, 100, 100, 0);
  back.update(true, 140, 100, 10);
  back.update(true, 101, 100, 20);
  assert(back.update(false, 0, 0, 100) == G::Drag);
  back.update(true, 220, 20, 110);
  back.update(true, 221, 20, 120);
  back.update(true, 220, 21, 130);
  assert(back.update(false, 0, 0, 210) == G::Tap);

  G spike;
  spike.update(true, 260, 20, 0);
  assert(spike.update(false, 0, 0, 80) == G::Pending);
  assert(!spike.active);
  G shortContact;
  shortContact.update(true, 260, 20, 0);
  shortContact.update(true, 261, 20, 10);
  assert(shortContact.update(false, 0, 0, 90) == G::Pending);
  assert(!shortContact.active);

  // swipe over the zoom buttons = pan
  G controls;
  controls.update(true, 220, 20, 0);
  controls.update(true, 240, 20, 10);
  controls.update(true, 260, 20, 20);
  assert(controls.update(false, 0, 0, 100) == G::Drag);
  assert(controls.dx() == 40);

  // some jitter on the buttons is ok
  assert(mapZoomButton(264, 20) == 1);
  assert(mapZoomButton(228, 20) == -1);
  assert(mapZoomButton(200, 20) == 0);
  assert(mapZoomButton(264, 36) == 0);
  G button;
  button.update(true, 264, 20, 0, 12);
  button.update(true, 272, 22, 10, 12);
  button.update(true, 267, 20, 20, 12);
  assert(button.update(false, 0, 0, 100, 12) == G::Tap);
  G buttonSwipe;
  buttonSwipe.update(true, 264, 20, 0, 12);
  buttonSwipe.update(true, 240, 20, 10, 12);
  buttonSwipe.update(true, 264, 20, 20, 12);
  assert(buttonSwipe.update(false, 0, 0, 100, 12) == G::Drag);

  // no border cache, drag still works
  MapBorder border;
  border.valid = true;
  border.x = border.y = 200000;
  border.zoom = 15;
  for (int offsetX : {-120, 0, 120}) {
    for (int offsetY : {-120, 0, 120}) {
      int cx = border.x + offsetX, cy = border.y + offsetY;
      for (int direction : {-500, 0, 500}) {
        int dx = direction, dy = -direction;
        bool covered = border.coversDrag(cx, cy, 15, dx, dy);
        assert(dx == direction && dy == -direction);
        bool expected = offsetX - dx >= -MapBorder::Margin && offsetX - dx <= MapBorder::Margin &&
                        offsetY - dy >= -MapBorder::Margin && offsetY - dy <= MapBorder::Margin;
        assert(covered == expected);
        if (!covered) continue;
        for (int x = 0; x < 320; ++x) {
          int source = x + MapBorder::Margin + offsetX - dx;
          assert(source >= 0 && source < MapBorder::Width * MapBorder::Scale);
          assert(border.sourceX(x, cx, dx) >= 0 && border.sourceX(x, cx, dx) < MapBorder::Width);
        }
        for (int y = 0; y < 240; ++y) {
          int source = y + MapBorder::Margin + offsetY - dy;
          assert(source >= 0 && source < MapBorder::Height * MapBorder::Scale);
          assert(border.sourceY(y, cy, dy) >= 0 && border.sourceY(y, cy, dy) < MapBorder::Height);
        }
      }
    }
  }
  int dx = 50, dy = 50;
  assert(!border.coversDrag(border.x, border.y, 16, dx, dy));
  assert(dx == 50 && dy == 50);
  border.valid = false;
  assert(!border.coversDrag(border.x, border.y, 15, dx, dy));
  assert(dx == 50 && dy == 50);
  border.valid = true;
  border.x = 2;
  assert(border.covers((256 << 15) - 2, border.y, 15));

  G wrap;
  wrap.update(true, 100, 100, UINT32_MAX - 20);
  wrap.update(true, 100, 101, UINT32_MAX - 10);
  wrap.update(true, 101, 100, UINT32_MAX);
  assert(wrap.update(false, 0, 0, 10) == G::Pending);
  assert(wrap.update(false, 0, 0, 80) == G::Tap);
}
