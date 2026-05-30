#include <Arduino.h>
#include <atomic>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <TJpg_Decoder.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <WiFiManager.h>
#include <XPT2046_Touchscreen.h>
#include <time.h>

#include "gesture.h"
#include "map_border.h"
#include "roots.h"

constexpr int WIDTH = 320, HEIGHT = 240;
constexpr size_t JPEG_CAPACITY = 48 * 1024;
constexpr uint32_t FRAME_INTERVAL = 250, RETRY_INTERVAL = 1000;

TFT_eSPI screen;
SPIClass touchSPI(VSPI);
XPT2046_Touchscreen touchscreen(33);
Preferences settings;
WiFiClientSecure tls;
HTTPClient http;
// Network writes one buffer while UI retains the other for local drag feedback.
uint8_t* jpeg = nullptr;
uint8_t* displayedJPEG = nullptr;
size_t displayedLength = 0;
uint16_t* borderPixels = nullptr;
MapBorder border;
bool decodingBorder = false;
String shownMapKey, networkMapKey;
int shownMapX = 0, shownMapY = 0, shownMapZoom = 0;
std::atomic<bool> borderReady{false};
std::atomic<uint32_t> lastContact{0};
String serverURL, serverUser, serverPassword, sessionID, etag, page;
int revision = -1;
uint32_t nextFrame = 0, nextBorder = 0;
std::atomic<bool> frameReady{false}, inputReady{false};

struct TouchEvent {
  int x, y, dx, dy, revision, frameID, zoom;
  uint32_t sampledAt;
  bool dragged, map;
};
struct DisplayEvent {
  enum Kind { Image, Unchanged, Status, Border, DiscardBorder } kind;
  int length = 0, revision = -1, frameID = -1;
  int mapX = 0, mapY = 0, mapZoom = 0;
  char page[20] = {}, status[64] = {}, mapKey[24] = {};
};
bool publishDisplay(const DisplayEvent& event);
QueueHandle_t touchEvents = nullptr, displayEvents = nullptr;
TaskHandle_t networkTaskHandle = nullptr;
DisplayEvent pendingDisplay;
bool hasPendingDisplay = false, previewDrawing = false;
int previewY = 0;
std::atomic<bool> fingerDown{false}, pauseSampling{false}, samplingPaused{false};
std::atomic<bool> pauseNetwork{false}, networkPaused{false};
std::atomic<int> displayedRevision{-1}, displayedFrameID{-1};
std::atomic<int> dragX{0}, dragY{0}, dragFrameID{-1};
std::atomic<bool> displayedMap{false}, displayedMenu{false};

struct Calibration {
  uint32_t version;
  float x[3], y[3];
} calibration;

void pauseTouchSampling() {
  if (!touchEvents) return;
  if (networkTaskHandle) {
    pauseNetwork = true;
    while (!networkPaused.load()) delay(1);
    xQueueReset(displayEvents);
    hasPendingDisplay = false;
  }
  pauseSampling = true;
  while (!samplingPaused.load()) delay(1);
  xQueueReset(touchEvents);
  fingerDown = false;
  displayedRevision = -1;
  dragFrameID = -1;
}

void message(const String& title, const String& detail = "") {
  frameReady = false;
  inputReady = false;
  screen.fillScreen(TFT_BLACK);
  screen.setTextColor(TFT_WHITE, TFT_BLACK);
  screen.setTextFont(2);
  screen.setCursor(12, 60);
  screen.println(title);
  screen.setCursor(12, 94);
  screen.println(detail);
}

bool drawBlock(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t* pixels) {
  if (decodingBorder) {
    if (x < 0 || y < 0 || x + w > MapBorder::Width || y + h > MapBorder::Height) return false;
    for (int row = 0; row < h; ++row)
      memcpy(borderPixels + (y + row) * MapBorder::Width + x, pixels + row * w, w * sizeof(uint16_t));
    return true;
  }
  if (y >= HEIGHT) return false;
  if (previewDrawing) {
    // only the map moves, buttons stay
    int sourceY = y - previewY;
    if (sourceY + h <= 36 || sourceY >= HEIGHT - 12) return true;
    if (sourceY < 36) {
      int rows = 36 - sourceY;
      pixels += rows * w;
      y += rows;
      h -= rows;
      sourceY = 36;
    }
    if (sourceY + h > HEIGHT - 12) h = HEIGHT - 12 - sourceY;
  }
  screen.pushImage(x, y, w, h, pixels);
  return true;
}

bool rawTouch(TS_Point& point) {
  if (!touchscreen.touched()) return false;
  point = touchscreen.getPoint();
  return point.z >= 200;
}

bool solve(float matrix[3][4], float output[3]) {
  for (int column = 0; column < 3; ++column) {
    int pivot = column;
    for (int row = column + 1; row < 3; ++row)
      if (fabsf(matrix[row][column]) > fabsf(matrix[pivot][column])) pivot = row;
    if (fabsf(matrix[pivot][column]) < 0.00001f) return false;
    for (int j = 0; j < 4; ++j) {
      float saved = matrix[column][j];
      matrix[column][j] = matrix[pivot][j];
      matrix[pivot][j] = saved;
    }
    float divisor = matrix[column][column];
    for (int j = column; j < 4; ++j) matrix[column][j] /= divisor;
    for (int row = 0; row < 3; ++row) {
      if (row == column) continue;
      float factor = matrix[row][column];
      for (int j = column; j < 4; ++j) matrix[row][j] -= factor * matrix[column][j];
    }
  }
  for (int row = 0; row < 3; ++row) output[row] = matrix[row][3];
  return true;
}

void calibrateTouch() {
  pauseTouchSampling();
  const int targets[4][2] = {{20, 20}, {299, 20}, {299, 219}, {20, 219}};
  float raw[4][3];
  for (;;) {
    for (int corner = 0; corner < 4; ++corner) {
      message("Touch calibration", "Hold each cross until it moves");
      screen.drawFastHLine(targets[corner][0] - 9, targets[corner][1], 19, TFT_YELLOW);
      screen.drawFastVLine(targets[corner][0], targets[corner][1] - 9, 19, TFT_YELLOW);
      TS_Point point;
      while (rawTouch(point)) delay(10);
      float sumX = 0, sumY = 0;
      int count = 0;
      while (count < 24) {
        if (rawTouch(point)) {
          sumX += point.x / 4095.0f;
          sumY += point.y / 4095.0f;
          ++count;
        } else {
          sumX = sumY = 0;
          count = 0;
        }
        delay(10);
      }
      raw[corner][0] = sumX / count;
      raw[corner][1] = sumY / count;
      raw[corner][2] = 1;
    }
    bool valid = true;
    for (int axis = 0; axis < 2; ++axis) {
      float matrix[3][4] = {};
      for (int sample = 0; sample < 4; ++sample) {
        for (int row = 0; row < 3; ++row) {
          for (int column = 0; column < 3; ++column)
            matrix[row][column] += raw[sample][row] * raw[sample][column];
          matrix[row][3] += raw[sample][row] * targets[sample][axis];
        }
      }
      valid &= solve(matrix, axis == 0 ? calibration.x : calibration.y);
    }
    for (int sample = 0; sample < 4 && valid; ++sample) {
      float x = 0, y = 0;
      for (int j = 0; j < 3; ++j) {
        x += raw[sample][j] * calibration.x[j];
        y += raw[sample][j] * calibration.y[j];
      }
      valid = hypotf(x - targets[sample][0], y - targets[sample][1]) < 15;
    }
    if (valid) break;
    message("Calibration missed", "Try holding each cross again");
    delay(1500);
  }
  calibration.version = 1;
  settings.putBytes("touch", &calibration, sizeof(calibration));
  TS_Point point;
  while (rawTouch(point)) delay(10);
  etag = "";
  nextFrame = 0;
  pauseSampling = false;
  pauseNetwork = false;
}

void configureWiFi(bool forcePortal) {
  pauseTouchSampling();
  tls.stop();
  char url[192], user[64], password[128];
  serverURL.toCharArray(url, sizeof(url));
  serverUser.toCharArray(user, sizeof(user));
  serverPassword.toCharArray(password, sizeof(password));
  WiFiManager manager;
  WiFiManagerParameter serverField("server", "Server URL (https://cydney.example.com)", url, sizeof(url) - 1);
  WiFiManagerParameter userField("user", "Server username", user, sizeof(user) - 1);
  WiFiManagerParameter passwordField("password", "Server password", password, sizeof(password) - 1,
                                    "type=\"password\"");
  manager.addParameter(&serverField);
  manager.addParameter(&userField);
  manager.addParameter(&passwordField);
  manager.setConfigPortalTimeout(300);
  manager.setConnectTimeout(20);
  manager.setSaveParamsCallback([&]() {
    serverURL = serverField.getValue();
    serverURL.trim();
    while (serverURL.endsWith("/")) serverURL.remove(serverURL.length() - 1);
    serverUser = userField.getValue();
    serverPassword = passwordField.getValue();
    settings.putString("server", serverURL);
    settings.putString("user", serverUser);
    settings.putString("password", serverPassword);
  });
  char portalPassword[16];
  snprintf(portalPassword, sizeof(portalPassword), "%08lx", (unsigned long)esp_random());
  manager.setAPCallback([&](WiFiManager*) {
    message("Wi-Fi: Cydney-Setup", String("Password: ") + portalPassword);
    screen.setCursor(12, 125);
    screen.print("Open http://192.168.4.1");
  });
  message("Connecting to Wi-Fi...");
  WiFi.mode(WIFI_STA);
  bool connected = forcePortal ? manager.startConfigPortal("Cydney-Setup", portalPassword)
                               : manager.autoConnect("Cydney-Setup", portalPassword);
  if (!connected) {
    message("Wi-Fi setup timed out", "Restarting...");
    delay(1000);
    ESP.restart();
  }
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  sessionID = "";
  etag = "";
  revision = -1;
  frameReady = false;
  nextFrame = 0;
  pauseSampling = false;
  pauseNetwork = false;
}

void connectionStatus(const String& detail) {
  inputReady = false;
  etag = "";
  DisplayEvent event{};
  event.kind = DisplayEvent::Status;
  detail.toCharArray(event.status, sizeof(event.status));
  xQueueSend(displayEvents, &event, 0);
}

bool openRequest(const String& path) {
  if (!serverURL.startsWith("https://")) {
    connectionStatus("No https server url, hold BOOT");
    return false;
  }
  if (time(nullptr) < 1700000000) {
    connectionStatus("Waiting for network time...");
    return false;
  }
  // TLS handshake needs a LOT of heap, drop the border bitmap first
  if (!tls.connected() && borderPixels) {
    DisplayEvent discard{};
    discard.kind = DisplayEvent::DiscardBorder;
    if (!publishDisplay(discard)) return false;
  }
  http.setReuse(true);
  http.setConnectTimeout(4000);
  http.setTimeout(6000);
  // Serial.println(serverURL + path);
  if (!http.begin(tls, serverURL + path)) return false;
  http.setAuthorization(serverUser.c_str(), serverPassword.c_str());
  http.setUserAgent("Cydney-ESP32/1.1");
  return true;
}

void requestFailed(int status) {
  char tlsError[128] = {};
  int tlsCode = tls.lastError(tlsError, sizeof(tlsError));
  Serial.printf("TLS result: %d %s; largest heap block %u\n", tlsCode, tlsError, ESP.getMaxAllocHeap());
  tls.stop();
  if (status == 404) {
    sessionID = "";
    revision = -1;
  }
  connectionStatus(status == 404 ? "Restoring session..." : "Connection retry...");
  Serial.printf("Server request: %d %s; heap %u, RSSI %d\n", status,
                status < 0 ? HTTPClient::errorToString(status).c_str() : "", ESP.getFreeHeap(), WiFi.RSSI());
}

bool createSession() {
  if (!openRequest("/api/device/sessions")) return false;
  http.addHeader("Content-Type", "application/json");
  int status = http.POST("{\"profile\":\"landscape\",\"shared\":true}");
  bool ready = false;
  if (status == 201) {
    JsonDocument filter, document;
    filter["id"] = true;
    filter["width"] = true;
    filter["height"] = true;
    auto error = deserializeJson(document, http.getStream(), DeserializationOption::Filter(filter));
    if (!error && document["width"] == WIDTH && document["height"] == HEIGHT) {
      sessionID = document["id"].as<String>();
      ready = sessionID.length() == 32;
      etag = "";
    }
  }
  if (!ready) tls.stop();
  http.end();
  if (!ready) requestFailed(status);
  return ready;
}

bool publishDisplay(const DisplayEvent& event) {
  if (pauseNetwork.load()) return false;
  if (xQueueSend(displayEvents, &event, pdMS_TO_TICKS(50)) != pdTRUE) return false;
  for (;;) {
    uint32_t result = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(50));
    if (result) return result == 1;
    if (pauseNetwork.load()) {
      xQueueReset(displayEvents);
      return false;
    }
  }
}

bool receiveFrame(int status) {
  uint32_t started = millis();
  DisplayEvent event{};
  event.revision = http.header("X-Frame-Revision").toInt();
  event.frameID = http.header("X-Frame-ID").toInt();
  http.header("X-Frame-Page").toCharArray(event.page, sizeof(event.page));
  http.header("X-Map-Key").toCharArray(event.mapKey, sizeof(event.mapKey));
  event.mapX = http.header("X-Map-X").toInt();
  event.mapY = http.header("X-Map-Y").toInt();
  event.mapZoom = http.header("X-Map-Zoom").toInt();
  String newEtag = http.header("ETag");
  if (status == 304 && frameReady.load()) {
    event.kind = DisplayEvent::Unchanged;
    http.end();
  } else {
    int length = http.getSize();
    if (status != 200) {
      if (status > 0) http.getString();
      http.end();
      requestFailed(status);
      return false;
    }
    if (length <= 0 || length > (int)JPEG_CAPACITY || http.header("X-Frame-Revision").isEmpty()) {
      tls.stop();
      http.end();
      connectionStatus("bad frame, retry...");
      return false;
    }
    size_t received = http.getStream().readBytes(jpeg, length);
    http.end();
    if (received != (size_t)length) {
      tls.stop();
      connectionStatus("Frame download retry...");
      return false;
    }
    event.kind = DisplayEvent::Image;
    event.length = length;
    Serial.printf("JPEG download: %u bytes, %lu ms\n", (unsigned)received,
                  (unsigned long)(millis() - started));
  }
  if (!publishDisplay(event)) {
    etag = "";
    return false;
  }
  revision = event.revision;
  page = event.page;
  networkMapKey = event.mapKey;
  etag = newEtag;
  return true;
}

bool fetchFrame() {
  if (!openRequest("/api/device/" + sessionID + "/frame.jpg")) return false;
  if (frameReady.load() && etag.length()) http.addHeader("If-None-Match", etag);
  uint32_t started = millis();
  int status = http.GET();
  String serverTiming = http.header("Server-Timing");
  bool ready = receiveFrame(status);
  Serial.printf("Frame: %lu ms; server %s\n", (unsigned long)(millis() - started), serverTiming.c_str());
  return ready;
}

void fetchBorder() {
  nextBorder = millis() + 500;
  if (!openRequest("/api/device/" + sessionID + "/map-preview.jpg?map_key=" + networkMapKey)) return;
  http.setTimeout(1500);
  int status = http.GET();
  int length = http.getSize();
  DisplayEvent event{};
  event.kind = DisplayEvent::Border;
  event.length = length;
  http.header("X-Map-Key").toCharArray(event.mapKey, sizeof(event.mapKey));
  event.mapX = http.header("X-Map-X").toInt();
  event.mapY = http.header("X-Map-Y").toInt();
  event.mapZoom = http.header("X-Map-Zoom").toInt();
  if (status != 200 || length <= 0 || length > (int)JPEG_CAPACITY ||
      http.header("X-Preview-Margin").toInt() != MapBorder::Margin) {
    if (status > 0) http.getString();
    http.end();
    return;
  }
  size_t received = http.getStream().readBytes(jpeg, length);
  http.end();
  if (received == (size_t)length) publishDisplay(event);
}

void sendGesture(const TouchEvent& event) {
  if (!inputReady.load() || revision < 0) return;
  bool pan = event.dragged, zoom = !pan && event.zoom != 0;
  if (pan && page != "map") return;
  if (zoom && page != "map" && page != "menu") return;
  if (pan && event.dx == 0 && event.dy == 0) return;
  JsonDocument body;
  body["revision"] = (pan || zoom) ? revision : event.revision;
  int frameID = (pan || zoom) ? displayedFrameID.load() : event.frameID;
  if (frameID > 0) body["frame_id"] = frameID;
  if (pan) {
    body["kind"] = "pan";
    body["dx"] = event.dx;
    body["dy"] = event.dy;
  } else if (zoom) {
    body["kind"] = "zoom";
    body["value"] = event.zoom > 0 ? "in" : "out";
  } else {
    body["x"] = event.x;
    body["y"] = event.y;
    body["pointer"] = "touch";
  }
  Serial.printf("Touch %s: x=%d y=%d dx=%d dy=%d\n", pan ? "pan" : zoom ? "zoom" : "tap", event.x, event.y, event.dx, event.dy);
  String payload;
  serializeJson(body, payload);
  if (!openRequest("/api/device/" + sessionID + ((pan || zoom) ? "/action" : "/touch") + "?encoding=jpg")) return;
  http.addHeader("Content-Type", "application/json");
  uint32_t started = millis();
  int status = http.POST(payload);
  inputReady = false;
  etag = "";
  if (status == 409) {
    http.getString();
    http.end();
    nextFrame = 0;
    return;
  }
  bool ready = receiveFrame(status);
  nextFrame = millis() + (ready ? FRAME_INTERVAL : RETRY_INTERVAL);
  Serial.printf("Gesture: %lu ms; release-to-display %lu ms\n", (unsigned long)(millis() - started),
                (unsigned long)(millis() - event.sampledAt));
}

void sampleTouch(void*) {
  TouchGesture gesture;
  TouchEvent event{};
  for (;;) {
    if (pauseSampling.load()) {
      gesture = TouchGesture{};
      samplingPaused = true;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    samplingPaused = false;
    TS_Point point;
    bool down = rawTouch(point);
    int px = 0, py = 0;
    if (down) {
      lastContact = millis();
      float x = point.x / 4095.0f, y = point.y / 4095.0f;
      px = constrain(lroundf(calibration.x[0] * x + calibration.x[1] * y + calibration.x[2]), 0L, 319L);
      py = constrain(lroundf(calibration.y[0] * x + calibration.y[1] * y + calibration.y[2]), 0L, 239L);
      if (!gesture.active) {
        event.revision = displayedRevision.load();
        event.frameID = displayedFrameID.load();
        event.map = displayedMap.load();
        event.zoom = event.map ? mapZoomButton(px, py) : displayedMenu.load() ? menuZoomButton(px, py) : 0;
        dragFrameID = -1;
      }
    }
    auto result = gesture.update(down, px, py, millis(), event.zoom ? 12 : TouchGesture::DRAG_PIXELS);
    fingerDown = gesture.active;
    if (gesture.active && gesture.dragged && event.map) {
      dragX = gesture.dx();
      dragY = gesture.dy();
      dragFrameID = event.frameID;
    }
    if (result != TouchGesture::Pending && event.revision >= 0) {
      event.x = gesture.startX;
      event.y = gesture.startY;
      event.dx = gesture.dx();
      event.dy = gesture.dy();
      event.dragged = result == TouchGesture::Drag;
      event.sampledAt = millis();
      int releasedZoom = event.map ? mapZoomButton(gesture.lastX, gesture.lastY) : menuZoomButton(gesture.lastX, gesture.lastY);
      bool sameButton = !event.zoom || event.dragged || releasedZoom == event.zoom;
      if (sameButton) xQueueSend(touchEvents, &event, 0);
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

void networkLoop(void*) {
  for (;;) {
    if (pauseNetwork.load()) {
      networkPaused = true;
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    networkPaused = false;
    TouchEvent event;
    if (xQueueReceive(touchEvents, &event, 0) == pdTRUE) {
      if ((event.dragged && event.map && page == "map") || !event.dragged) sendGesture(event);
    }
    if (!fingerDown.load() && !uxQueueMessagesWaiting(touchEvents) && (int32_t)(millis() - nextFrame) >= 0) {
      bool ready = false;
      if (WiFi.status() != WL_CONNECTED) {
        connectionStatus("Wi-Fi reconnecting...");
        WiFi.reconnect();
      } else if (sessionID.length() || createSession()) ready = fetchFrame();
      nextFrame = millis() + (ready ? FRAME_INTERVAL : RETRY_INTERVAL);
    }
    if (borderPixels && frameReady.load() && inputReady.load() && page == "map" &&
        !borderReady.load() && !fingerDown.load() && !uxQueueMessagesWaiting(touchEvents) &&
        millis() - lastContact.load() >= 1000 &&
        (int32_t)(millis() - nextBorder) >= 0) fetchBorder();
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

void paintDisplay() {
  static int lastDX = 0, lastDY = 0;
  if (!hasPendingDisplay) hasPendingDisplay = xQueueReceive(displayEvents, &pendingDisplay, 0) == pdTRUE;
  if (hasPendingDisplay && pendingDisplay.kind == DisplayEvent::DiscardBorder) {
    free(borderPixels);
    borderPixels = nullptr;
    border.valid = false;
    borderReady = false;
    lastDX = lastDY = 1000000;
    hasPendingDisplay = false;
    xTaskNotify(networkTaskHandle, 1, eSetValueWithOverwrite);
  }
  if (hasPendingDisplay && pendingDisplay.kind == DisplayEvent::Status) {
    if (!frameReady.load()) message(pendingDisplay.status, "Reconnecting...");
    else {
      screen.fillRect(3, 3, 190, 16, TFT_BLACK);
      screen.setTextColor(TFT_ORANGE, TFT_BLACK);
      screen.setTextFont(1);
      screen.setCursor(6, 7);
      screen.print(pendingDisplay.status);
    }
    hasPendingDisplay = false;
  }
  if (hasPendingDisplay && pendingDisplay.kind == DisplayEvent::Border && !fingerDown.load()) {
    bool valid = false;
    if (shownMapKey == pendingDisplay.mapKey && shownMapZoom == pendingDisplay.mapZoom) {
      if (!borderPixels)
        borderPixels = (uint16_t*)malloc(MapBorder::Width * MapBorder::Height * sizeof(uint16_t));
      uint16_t width = 0, height = 0;
      valid = borderPixels && TJpgDec.getJpgSize(&width, &height, jpeg, pendingDisplay.length) == JDR_OK &&
              width == WIDTH + 2 * MapBorder::Margin && height == HEIGHT + 2 * MapBorder::Margin;
      if (valid) {
        border.valid = false;
        decodingBorder = true;
        TJpgDec.setJpgScale(MapBorder::Scale);
        valid = TJpgDec.drawJpg(0, 0, jpeg, pendingDisplay.length) == JDR_OK;
        TJpgDec.setJpgScale(1);
        decodingBorder = false;
        if (valid) {
          border.x = pendingDisplay.mapX;
          border.y = pendingDisplay.mapY;
          border.zoom = pendingDisplay.mapZoom;
          border.valid = true;
          lastDX = lastDY = 1000000;  // force repaint
          Serial.printf("Map border ready: %d px, %d bytes cache; heap %u\n", MapBorder::Margin,
                        MapBorder::Width * MapBorder::Height * 2, ESP.getFreeHeap());
        }
      }
    }
    borderReady = valid;
    hasPendingDisplay = false;
    xTaskNotify(networkTaskHandle, valid ? 1 : 2, eSetValueWithOverwrite);
  }
  // dont swap the frame while finger is down
  if (hasPendingDisplay && !fingerDown.load()) {
    bool valid = true;
    uint32_t started = millis();
    if (pendingDisplay.kind == DisplayEvent::Image) {
      uint16_t width = 0, height = 0;
      valid = TJpgDec.getJpgSize(&width, &height, jpeg, pendingDisplay.length) == JDR_OK &&
              width == WIDTH && height == HEIGHT;
      if (valid) {
        screen.startWrite();
        valid = TJpgDec.drawJpg(0, 0, jpeg, pendingDisplay.length) == JDR_OK;
        screen.endWrite();
      }
      if (valid) {
        uint8_t* previous = displayedJPEG;
        displayedJPEG = jpeg;
        jpeg = previous;
        displayedLength = pendingDisplay.length;
        lastDX = lastDY = 0;
      }
      //Serial.printf("heap %u\n", ESP.getFreeHeap());
      Serial.printf("JPEG draw: %lu ms; frame %d; heap %u; RSSI %d\n",
                    (unsigned long)(millis() - started), pendingDisplay.frameID, ESP.getFreeHeap(), WiFi.RSSI());
    }
    if (valid) {
      if (shownMapKey != pendingDisplay.mapKey) borderReady = false;
      shownMapKey = pendingDisplay.mapKey;
      shownMapX = pendingDisplay.mapX;
      shownMapY = pendingDisplay.mapY;
      shownMapZoom = pendingDisplay.mapZoom;
      displayedRevision = pendingDisplay.revision;
      displayedFrameID = pendingDisplay.frameID;
      displayedMap = strcmp(pendingDisplay.page, "map") == 0;
      displayedMenu = strcmp(pendingDisplay.page, "menu") == 0;
      if (displayedMap.load() && !borderPixels)
        borderPixels = (uint16_t*)malloc(MapBorder::Width * MapBorder::Height * sizeof(uint16_t));
      frameReady = true;
      inputReady = true;
    }
    hasPendingDisplay = false;
    xTaskNotify(networkTaskHandle, valid ? 1 : 2, eSetValueWithOverwrite);
  }
  if (frameReady.load() && fingerDown.load() && displayedMap.load() && displayedLength &&
      dragFrameID.load() == displayedFrameID.load()) {
    int dx = dragX.load(), dy = dragY.load();
    // border cache is optional, drag works without it
    bool covered = border.coversDrag(shownMapX, shownMapY, shownMapZoom, dx, dy);
    if (dx != lastDX || dy != lastDY) {
      screen.setViewport(0, 36, WIDTH, HEIGHT - 48, false);
      screen.startWrite();
      if (!covered) screen.fillRect(0, 36, WIDTH, HEIGHT - 48, 0x1945);
      if (covered) {
        uint16_t line[WIDTH];
        int baseX = MapBorder::Margin + border.deltaX(shownMapX) - dx;
        for (int y = 36; y < HEIGHT - 12; ++y) {
          int row = border.sourceY(y, shownMapY, dy) * MapBorder::Width;
          for (int x = 0; x < WIDTH; ++x)
            line[x] = borderPixels[row + MapBorder::clampTo((x + baseX) / MapBorder::Scale, 0, MapBorder::Width - 1)];
          int leftEnd = WIDTH, rightStart = WIDTH;
          if (y >= 36 + dy && y < HEIGHT - 12 + dy) {
            leftEnd = MapBorder::clampTo(dx, 0, WIDTH);
            rightStart = MapBorder::clampTo(WIDTH + dx, 0, WIDTH);
          }
          if (leftEnd) screen.pushImage(0, y, leftEnd, 1, line);
          if (rightStart < WIDTH) screen.pushImage(rightStart, y, WIDTH - rightStart, 1, line + rightStart);
        }
      }
      previewDrawing = true;
      previewY = dy;
      TJpgDec.drawJpg(dx, dy, displayedJPEG, displayedLength);
      previewDrawing = false;
      screen.endWrite();
      screen.resetViewport();
      lastDX = dx;
      lastDY = dy;
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(0, INPUT_PULLUP);
  screen.init();
  screen.setRotation(1);
  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, HIGH);
  screen.setSwapBytes(true);
  touchSPI.begin(25, 39, 32, 33);
  touchscreen.begin(touchSPI);
  touchscreen.setRotation(1);
  TJpgDec.setCallback(drawBlock);
  settings.begin("cydney", false);
  serverURL = settings.getString("server");
  serverUser = settings.getString("user");
  serverPassword = settings.getString("password");
  settings.getBytes("touch", &calibration, sizeof(calibration));
  if (calibration.version != 1) calibrateTouch();
  configureWiFi(serverURL.isEmpty());
  configTime(0, 0, "pool.ntp.org", "time.cloudflare.com");
  tls.setCACert(CYD_ROOTS);
  tls.setTimeout(6);
  tls.setHandshakeTimeout(6);
  const char* headers[] = {"ETag", "X-Frame-Revision", "X-Frame-Page", "X-Frame-ID", "Server-Timing",
                           "X-Map-Key", "X-Map-X", "X-Map-Y", "X-Map-Zoom", "X-Preview-Margin"};
  http.collectHeaders(headers, 10);
  jpeg = (uint8_t*)malloc(JPEG_CAPACITY);
  displayedJPEG = (uint8_t*)malloc(JPEG_CAPACITY);
  if (!jpeg || !displayedJPEG) {
    message("Not enough RAM for JPEG");
    for (;;) delay(1000);
  }
  touchEvents = xQueueCreate(8, sizeof(TouchEvent));
  displayEvents = xQueueCreate(2, sizeof(DisplayEvent));
  // TLS on core 1, otherwise wifi starves
  if (!touchEvents || !displayEvents ||
      xTaskCreatePinnedToCore(sampleTouch, "touch", 4096, nullptr, 2, nullptr, 1) != pdPASS ||
      xTaskCreatePinnedToCore(networkLoop, "network", 12288, nullptr, 1, &networkTaskHandle, 1) != pdPASS) {
    message("task setup failed", "restart pls");
    for (;;) delay(1000);
  }
  // TODO portrait mode
  Serial.printf("ready, heap %u\n", ESP.getFreeHeap());
}

void loop() {
  static uint32_t bootDown = 0;
  if (digitalRead(0) == LOW) {
    if (!bootDown) bootDown = millis();
    if (millis() - bootDown >= 2000) {
      configureWiFi(true);
      bootDown = 0;
    }
  } else bootDown = 0;
  if (Serial.available()) {
    char command = Serial.read();
    if (command == 'c') calibrateTouch();
    if (command == 's') configureWiFi(true);
    if ((command == '+' || command == '-') && inputReady.load() &&
        (displayedMap.load() || displayedMenu.load())) {
      TouchEvent event{};
      event.zoom = command == '+' ? 1 : -1;
      event.map = displayedMap.load();
      event.revision = displayedRevision.load();
      event.frameID = displayedFrameID.load();
      event.sampledAt = millis();
      xQueueSend(touchEvents, &event, 0);
    }
  }
  paintDisplay();
  delay(2);
}
