/* ============================================================================
 * icu_monitor_esp32.ino
 * ICU Sentinel — ESP32 + MAX30102 + DHT11 bedside acquisition node
 *
 * Hardware
 *   - ESP32 development board (2.4 GHz Wi-Fi only)
 *   - MAX30102 pulse oximeter / heart-rate sensor (I2C, 3.3 V)
 *   - DHT11 ambient temperature + humidity sensor (digital, 3.3/5 V)
 *
 * Libraries (install via Arduino Library Manager)
 *   - "SparkFun MAX3010x Sensor Library"  by SparkFun  (v1.1.2+)
 *   - "DHT sensor library"                by Adafruit (v1.4.x+)  [pulls in Adafruit Unified Sensor]
 *
 * Board package: "esp32 by Espressif Systems" (v2.0.x or v3.x)
 * Baud: 115200
 *
 * Behaviour
 *   - Connects to the configured Wi-Fi hotspot and reconnects automatically.
 *   - Reads MAX30102; estimates heart rate and SpO2 ONLY when a finger is
 *     present and the signal quality is sufficient. Invalid measurements are
 *     reported as null (never zero-filled).
 *   - Reads DHT11 ambient temperature and humidity (ROOM AIR — not body temp).
 *   - POSTs JSON to the laptop backend with a per-device token. The server
 *     resolves the patient from its own device-to-patient assignment; any
 *     patient id in a payload would be ignored by design.
 *   - Never prints the Wi-Fi password or device token to the Serial Monitor.
 *
 * This firmware was written against the library versions listed above. If you
 * change library majors, verify the API still matches before flashing.
 * ========================================================================== */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <math.h>

#include "MAX30105.h"
#include "spo2_algorithm.h"      // ships with the SparkFun MAX3010x library
#include "DHT.h"

#include "config.h"              // copy config.example.h -> config.h and edit

// ---------------------------------------------------------------------------
// Pins (ESP32 DevKit defaults; change if your wiring differs)
// ---------------------------------------------------------------------------
#define I2C_SDA 21
#define I2C_SCL 22
#define DHT_PIN 4
#define DHT_TYPE DHT11

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
MAX30105 particleSensor;
DHT dht(DHT_PIN, DHT_TYPE);

// Ring buffer for the IR waveform used by the heart-rate estimator
#define IR_BUF_LEN 250          // ~5 s of samples at ~50 Hz effective capture
uint32_t irBuffer[IR_BUF_LEN];
uint32_t redBuffer[IR_BUF_LEN];
int irIdx = 0;
bool buffersFilled = false;

uint32_t lastSampleMs = 0;
uint32_t lastReportMs = 0;
uint32_t lastWifiAttemptMs = 0;
uint32_t lastDhtMs = 0;

float lastAmbTemp = NAN;
float lastAmbHum = NAN;
bool sensorInitOk = false;

struct VitalEstimate {
  bool fingerPresent;
  bool usable;            // quality sufficient to trust the numbers
  float heartRate;        // bpm, only valid when usable==true
  float spo2;             // %,   only valid when usable==true
  const char* quality;    // good | fair | poor | no_finger | invalid
};

// ---------------------------------------------------------------------------
// Wi-Fi
// ---------------------------------------------------------------------------
void connectWifi() {
  Serial.printf("[wifi] connecting to '%s'", WIFI_SSID);   // SSID only — never the password
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
    delay(250);
    Serial.print(".");
  }
  Serial.println();
  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] connected. IP: ");
    Serial.print(WiFi.localIP());
    Serial.print("  RSSI: ");
    Serial.println(WiFi.RSSI());
  } else {
    Serial.println("[wifi] connect failed — will retry");
  }
}

void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  uint32_t now = millis();
  if (now - lastWifiAttemptMs < 10000) return;   // back off 10 s between attempts
  lastWifiAttemptMs = now;
  Serial.println("[wifi] connection lost — reconnecting");
  WiFi.disconnect();
  connectWifi();
}

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------
bool initSensors() {
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);   // MAX30102 supports 400 kHz

  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("[max30102] NOT FOUND — check wiring (SDA=21, SCL=22, 3.3 V, GND)");
    return false;
  }

  // Configure for pulse-oximetry readings.
  // See the SparkFun example "Example1_BasicReading" for the meaning of these.
  byte ledBrightness = 0x1F;   // 0..255 (higher = stronger LEDs, more heat)
  byte sampleAverage = 4;      // 1,2,4,8,16,32
  byte ledMode = 2;            // 2 = Red + IR (required for SpO2)
  int sampleRate = 100;        // samples per second
  int pulseWidth = 411;        // ADC integration width
  int adcRange = 4096;         // max ADC count
  particleSensor.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);

  dht.begin();
  Serial.println("[max30102] initialized (Red+IR mode, 100 Hz)");
  return true;
}

void sampleMAX30102() {
  if (!sensorInitOk) return;
  while (particleSensor.available() == false) {
    particleSensor.check();    // advance to the next sample
  }
  irBuffer[irIdx] = particleSensor.getIR();
  redBuffer[irIdx] = particleSensor.getRed();
  irIdx = (irIdx + 1) % IR_BUF_LEN;
  particleSensor.nextSample();
}

void sampleDht() {
  uint32_t now = millis();
  if (now - lastDhtMs < 2500) return;   // DHT11 should not be read more often than ~1 Hz
  lastDhtMs = now;
  float t = dht.readTemperature();      // ambient air temperature, °C
  float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) {
    Serial.println("[dht11] read failed (NaN) — keeping last good value");
    return;
  }
  if (t < -20 || t > 60 || h < 0 || h > 100) {
    Serial.printf("[dht11] implausible reading t=%.1f h=%.1f — discarded\n", t, h);
    return;
  }
  lastAmbTemp = t;
  lastAmbHum = h;
}

// ---------------------------------------------------------------------------
// Heart-rate / SpO2 estimation (documented, deterministic, quality-gated)
//
// Method:
//   1) Finger detection: mean IR over the window must exceed FINGER_DETECT_IR.
//   2) Peak detection on the IR waveform using an adaptive threshold
//      (midpoint between window min/max). Peak intervals must be physiologically
//      plausible (30..220 bpm) and reasonably consistent (coefficient of
//      variation < 0.35) for the estimate to be accepted.
//   3) SpO2 via the classic ratio-of-ratios:
//        R = (ACred/DCred) / (ACir/DCir)
//        SpO2 = A - B*R        (A=110, B=25 — common fingertip calibration)
//      This is an ESTIMATE, not a clinical measurement.
//
// If any check fails, usable=false and the backend stores an invalid reading
// instead of a fabricated number.
// ---------------------------------------------------------------------------
VitalEstimate estimateVitals() {
  VitalEstimate v = {false, false, NAN, NAN, "invalid"};

  if (!sensorInitOk) return v;

  // Mean levels
  uint64_t irSum = 0, redSum = 0;
  uint32_t irMin = 0xFFFFFFFF, irMax = 0;
  for (int i = 0; i < IR_BUF_LEN; i++) {
    irSum += irBuffer[i];
    redSum += redBuffer[i];
    if (irBuffer[i] < irMin) irMin = irBuffer[i];
    if (irBuffer[i] > irMax) irMax = irBuffer[i];
  }
  float irMean = (float)irSum / IR_BUF_LEN;
  float redMean = (float)redSum / IR_BUF_LEN;

  if (irMean < FINGER_DETECT_IR) {
    v.fingerPresent = false;
    v.quality = "no_finger";
    return v;
  }
  v.fingerPresent = true;

  // Reject flat-line / saturated signals early
  uint32_t irPkPk = irMax - irMin;
  if (irPkPk < 500) {           // essentially no pulsatile component
    v.quality = "poor";
    return v;
  }
  if (irMax > 2000000) {        // saturated against the ADC ceiling
    v.quality = "poor";
    return v;
  }

  // Adaptive-threshold peak detection on IR
  float thr = irMean + (float)irPkPk * 0.25f;
  uint32_t lastPeakIdx = 0;
  uint32_t lastPeakVal = 0;
  int32_t intervals[32];
  int intervalCount = 0;
  float acIR = 0, acRed = 0;

  bool inPeak = false;
  uint32_t curPeakVal = 0;
  int curPeakIdx = 0;

  for (int i = 1; i < IR_BUF_LEN; i++) {
    bool above = irBuffer[i] > thr;
    if (above && !inPeak) {
      inPeak = true;
      curPeakVal = irBuffer[i];
      curPeakIdx = i;
    } else if (above && inPeak) {
      if (irBuffer[i] > curPeakVal) { curPeakVal = irBuffer[i]; curPeakIdx = i; }
    } else if (!above && inPeak) {
      inPeak = false;
      // Ignore peaks too close together (< 20 samples ~ 200 ms at 100 Hz / avg4)
      if (lastPeakIdx == 0 || (uint32_t)(curPeakIdx - lastPeakIdx) >= 20) {
        if (intervalCount < 32 && lastPeakIdx != 0) {
          intervals[intervalCount++] = (int32_t)(curPeakIdx - lastPeakIdx);
        }
        lastPeakIdx = curPeakIdx;
        lastPeakVal = curPeakVal;
        // local AC amplitude around the peak
        float pkIR = curPeakVal;
        float pkRed = redBuffer[curPeakIdx];
        if (pkIR - irMean > acIR) { acIR = pkIR - irMean; acRed = pkRed - redMean; }
      }
    }
  }

  if (intervalCount < 4) {
    v.quality = "poor";
    return v;
  }

  // Mean interval + variability check
  float meanInterval = 0;
  for (int i = 0; i < intervalCount; i++) meanInterval += intervals[i];
  meanInterval /= intervalCount;
  float varSum = 0;
  for (int i = 0; i < intervalCount; i++) {
    float d = intervals[i] - meanInterval;
    varSum += d * d;
  }
  float sd = sqrtf(varSum / intervalCount);
  float cv = (meanInterval > 0) ? sd / meanInterval : 1.0f;

  // The SparkFun example samples at 100 Hz with sampleAverage=4 -> effective
  // ~25 Hz per displayed sample. We captured at the configured rate, so use
  // the actual effective sample rate to convert intervals -> bpm.
  const float effectiveHz = 100.0f / 4.0f;   // 25 Hz
  float hr = 60.0f * effectiveHz / meanInterval;

  if (hr < 30.0f || hr > 220.0f) {
    v.quality = "poor";
    return v;
  }

  bool consistent = cv < 0.35f;

  // SpO2 ratio-of-ratios (estimate only)
  float dcIR = irMean - (irMean * 0.5f);   // DC component approximation
  float dcRed = redMean;
  float spo2 = NAN;
  if (acIR > 1.0f && acRed > 0.0f && dcIR > 0.0f && dcRed > 0.0f && irMean > 0) {
    float R = (acRed / redMean) / (acIR / irMean);
    if (R > 0.4f && R < 3.0f) {            // reject absurd ratios
      spo2 = 110.0f - 25.0f * R;
      if (spo2 < 50.0f) spo2 = 50.0f;
      if (spo2 > 100.0f) spo2 = 100.0f;
    }
  }

  if (!consistent) {
    v.quality = "poor";                    // motion artefact / irregular intervals
    return v;
  }

  v.heartRate = hr;
  v.spo2 = spo2;                            // may still be NAN if ratio rejected
  // Amplitude-based quality grade
  float snrProxy = acIR / (float)irPkPk;
  if (snrProxy > 0.35f && irPkPk > 2000) v.quality = "good";
  else v.quality = "fair";
  v.usable = true;
  return v;
}

// ---------------------------------------------------------------------------
// Reporting
// ---------------------------------------------------------------------------
void jsonFloat(char* out, size_t n, const char* key, float value, bool valid) {
  if (valid && !isnan(value)) {
    snprintf(out, n, "\"%s\":%.1f", key, (double)value);
  } else {
    snprintf(out, n, "\"%s\":null", key);   // never send 0 as a fake measurement
  }
}

void postReading() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[http] skipped — no Wi-Fi");
    return;
  }

  VitalEstimate v = estimateVitals();

  char hrBuf[48], spBuf[48], tBuf[48], hBuf[48];
  jsonFloat(hrBuf, sizeof(hrBuf), "heart_rate", v.heartRate, v.usable && !isnan(v.heartRate));
  jsonFloat(spBuf, sizeof(spBuf), "spo2", v.spo2, v.usable && !isnan(v.spo2));
  jsonFloat(tBuf, sizeof(tBuf), "ambient_temperature", lastAmbTemp, !isnan(lastAmbTemp));
  jsonFloat(hBuf, sizeof(hBuf), "ambient_humidity", lastAmbHum, !isnan(lastAmbHum));

  char body[768];
  snprintf(body, sizeof(body),
    "{%s,%s,%s,%s,"
    "\"signal_quality\":\"%s\","
    "\"finger_detected\":%s,"
    "\"firmware_version\":\"%s\","
    "\"ip_address\":\"%s\"}",
    hrBuf, spBuf, tBuf, hBuf,
    v.quality,
    v.fingerPresent ? "true" : "false",
    FW_VERSION,
    WiFi.localIP().toString().c_str());

  HTTPClient http;
  http.setTimeout(5000);
  String url = String(BACKEND_URL) + INGEST_PATH;
  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", DEVICE_TOKEN);

  int code = http.POST((uint8_t*)body, strlen(body));
  if (code > 0) {
    if (code == 200) {
      String resp = http.getString();
      Serial.print("[http] 200 OK  ");
      Serial.println(resp);
    } else if (code == 401) {
      Serial.println("[http] 401 — device token invalid. Re-register the device in the web UI and update config.h.");
    } else if (code == 403) {
      Serial.println("[http] 403 — device deactivated.");
    } else {
      Serial.printf("[http] POST failed, HTTP %d: %s\n", code, http.getString().c_str());
    }
  } else {
    // Negative codes are HTTPClient transport errors (connection refused, timeout…)
    Serial.printf("[http] connection error %d — is the backend running and BACKEND_URL correct?\n", code);
  }
  http.end();
}

// ---------------------------------------------------------------------------
// Arduino entry points
// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println();
  Serial.println("=================================================");
  Serial.println(" ICU Sentinel ESP32 node  (fw " FW_VERSION ")");
  Serial.println(" Device UID: " DEVICE_ID);       // UID is safe to print
  Serial.println("================================================-");

  sensorInitOk = initSensors();

  // Prefill the waveform buffers so the estimator has data immediately
  if (sensorInitOk) {
    Serial.print("[max30102] pre-filling waveform buffer");
    uint32_t start = millis();
    while (millis() - start < 4000 && WiFi.status() != WL_CONNECTED) {
      sampleMAX30102();
      delay(5);
    }
    // If Wi-Fi connected quickly, still fill the buffer
    start = millis();
    while (millis() - start < 2000) {
      sampleMAX30102();
      delay(5);
    }
    Serial.println(" done");
    buffersFilled = true;
  }

  connectWifi();
  lastReportMs = millis();
}

void loop() {
  maintainWifi();

  uint32_t now = millis();
  if (now - lastSampleMs >= SAMPLE_WINDOW_MS) {
    lastSampleMs = now;
    sampleMAX30102();
  }

  sampleDht();

  if (now - lastReportMs >= REPORT_INTERVAL_MS) {
    lastReportMs = now;
    postReading();
  }
}
