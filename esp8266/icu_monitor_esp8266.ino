/* ============================================================================
 * icu_monitor_esp8266.ino
 * ICU Sentinel / MedicAssist — ESP8266 NodeMCU + MAX30102 + DHT11 + buzzer + pot
 *
 * Behaviour
 *   - Connects to configured Wi-Fi and serves HTTP on port 80.
 *   - GET /data returns JSON (no auth, local network only by design). The browser
 *     cannot always read this directly (HTTPS dashboard + no CORS), so this
 *     server fetches it via /api/v1/devices/{id}/fetch-ingest OR the browser can
 *     call it if possible — the UI supports both flows.
 *   - Potentiometer on A0 produces SIMULATED systolic/diastolic BP. The JSON
 *     marks bp_simulated=true and bp_pot_raw; these are NEVER clinical evidence.
 *   - Buzzer can be driven by simple alerts (hr_alert/spo2_alert/bp_alert/buzzer_alert).
 *
 * Libraries: SparkFun MAX3010x, Adafruit DHT, ESP8266WebServer, ESP8266WiFi
 * ========================================================================== */

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <Wire.h>
#include <math.h>
#include <ArduinoJson.h>

#include "MAX30105.h"
#include "spo2_algorithm.h"
#include "DHT.h"

#include "config.h"

ESP8266WebServer server(80);
MAX30105 particleSensor;
DHT dht(DHT_PIN, DHT_TYPE);

#define IR_BUF_LEN 250
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

uint32_t uptimeStartMs = 0;

struct VitalEstimate {
  bool fingerPresent;
  bool usable;
  float heartRate;
  float spo2;
  const char* quality;
  bool hrValid;
  bool spo2Valid;
};

void connectWifi() {
  Serial.printf("[wifi] connecting to '%s'\n", WIFI_SSID);
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
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("[wifi] connect failed — will retry");
  }
}

void maintainWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  uint32_t now = millis();
  if (now - lastWifiAttemptMs < 10000) return;
  lastWifiAttemptMs = now;
  WiFi.disconnect();
  connectWifi();
}

bool initSensors() {
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);

  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) {
    Serial.println("[max30102] NOT FOUND");
    return false;
  }

  byte ledBrightness = 0x1F;
  byte sampleAverage = 4;
  byte ledMode = 2;
  int sampleRate = 100;
  int pulseWidth = 411;
  int adcRange = 4096;
  particleSensor.setup(ledBrightness, sampleAverage, ledMode, sampleRate, pulseWidth, adcRange);

  dht.begin();
#ifdef BUZZER_PIN
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW);
#endif
#if ENABLE_BP_SIM
  pinMode(BP_POT_PIN, INPUT);
#endif
  Serial.println("[sensors] initialized");
  return true;
}

void sampleMAX30102() {
  if (!sensorInitOk) return;
  while (particleSensor.available() == false) {
    particleSensor.check();
  }
  irBuffer[irIdx] = particleSensor.getIR();
  redBuffer[irIdx] = particleSensor.getRed();
  irIdx = (irIdx + 1) % IR_BUF_LEN;
  particleSensor.nextSample();
}

void sampleDht() {
  uint32_t now = millis();
  if (now - lastDhtMs < 2500) return;
  lastDhtMs = now;
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  if (isnan(t) || isnan(h)) {
    return;
  }
  if (t >= -20 && t <= 60 && h >= 0 && h <= 100) {
    lastAmbTemp = t;
    lastAmbHum = h;
  }
}

VitalEstimate estimateVitals() {
  VitalEstimate v = {false, false, NAN, NAN, "invalid", false, false};
  if (!sensorInitOk) return v;

  uint64_t irSum = 0, redSum = 0;
  uint32_t irMin = 0xFFFFFFFF, irMax = 0;
  for (int i = 0; i < IR_BUF_LEN; i++) {
    irSum += irBuffer[i];
    redSum += redBuffer[i];
    if (irBuffer[i] < irMin) irMin = irBuffer[i];
    if (irBuffer[i] > irMax) irMax = irBuffer[i];
  }
  float irMean = (float)irSum / IR_BUF_LEN;

  if (irMean < FINGER_DETECT_IR) {
    v.fingerPresent = false;
    v.quality = "no_finger";
    return v;
  }
  v.fingerPresent = true;

  uint32_t irPkPk = irMax - irMin;
  if (irPkPk < 500 || irMax > 2000000) {
    v.quality = "poor";
    return v;
  }

  float thr = irMean + (float)irPkPk * 0.25f;
  uint32_t lastPeakIdx = 0;
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
      if (lastPeakIdx == 0 || (uint32_t)(curPeakIdx - lastPeakIdx) >= 20) {
        if (intervalCount < 32 && lastPeakIdx != 0) {
          intervals[intervalCount++] = (int32_t)(curPeakIdx - lastPeakIdx);
        }
        lastPeakIdx = curPeakIdx;
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

  const float effectiveHz = 100.0f / 4.0f;
  float hr = 60.0f * effectiveHz / meanInterval;

  if (hr < 30.0f || hr > 220.0f) {
    v.quality = "poor";
    return v;
  }

  if (cv >= 0.35f) {
    v.quality = "poor";
    return v;
  }

  float spo2 = NAN;
  if (acIR > 1.0f && acRed > 0.0f && irMean > 0) {
    float R = (acRed / redMean) / (acIR / irMean);
    if (R > 0.4f && R < 3.0f) {
      spo2 = 110.0f - 25.0f * R;
      if (spo2 < 50.0f) spo2 = 50.0f;
      if (spo2 > 100.0f) spo2 = 100.0f;
    }
  }

  v.heartRate = hr;
  v.spo2 = spo2;
  v.hrValid = !isnan(hr);
  v.spo2Valid = !isnan(spo2);
  float snrProxy = acIR / (float)irPkPk;
  if (snrProxy > 0.35f && irPkPk > 2000) v.quality = "good";
  else v.quality = "fair";
  v.usable = true;
  return v;
}

void readSimulatedBP(float &sys, float &dia, int &raw) {
  sys = NAN; dia = NAN; raw = 0;
#if ENABLE_BP_SIM
  int val = analogRead(BP_POT_PIN);
  raw = val;
  float t = val / 1023.0f;
  if (t < 0.0f) t = 0.0f;
  if (t > 1.0f) t = 1.0f;
  sys = BP_SYS_MIN + t * (BP_SYS_MAX - BP_SYS_MIN);
  dia = BP_DIA_MIN + t * (BP_DIA_MAX - BP_DIA_MIN);
  sys = roundf(sys);
  dia = roundf(dia);
#endif
}

void handleData() {
  VitalEstimate v = estimateVitals();
  float sbp = NAN, dbp = NAN;
  int bpRaw = 0;
  readSimulatedBP(sbp, dbp, bpRaw);

  bool hrAlert = v.hrValid && (v.heartRate < 50.0f || v.heartRate > 120.0f);
  bool spo2Alert = v.spo2Valid && (v.spo2 < 94.0f);
  bool bpAlert = !isnan(sbp) && (sbp >= BP_SYS_MAX - 1.0f || sbp <= BP_SYS_MIN + 1.0f);

  bool buzzerAlert = hrAlert || spo2Alert || bpAlert;
#ifdef BUZZER_PIN
  if (buzzerAlert) {
    digitalWrite(BUZZER_PIN, HIGH);
  } else {
    digitalWrite(BUZZER_PIN, LOW);
  }
#endif

  StaticJsonDocument<512> doc;
  doc["device"] = DEVICE_ID;
  doc["wifi_connected"] = (WiFi.status() == WL_CONNECTED);
  doc["ip"] = WiFi.localIP().toString();
  doc["uptime_ms"] = (millis() - uptimeStartMs);

  if (v.hrValid && !isnan(v.heartRate)) {
    doc["heart_rate"] = v.heartRate;
  } else {
    doc["heart_rate"] = nullptr;
  }
  if (v.spo2Valid && !isnan(v.spo2)) {
    doc["spo2"] = v.spo2;
  } else {
    doc["spo2"] = nullptr;
  }

  if (!isnan(lastAmbTemp)) {
    doc["ambient_temperature_c"] = lastAmbTemp;
  } else {
    doc["ambient_temperature_c"] = nullptr;
  }
  if (!isnan(lastAmbHum)) {
    doc["humidity_percent"] = lastAmbHum;
  } else {
    doc["humidity_percent"] = nullptr;
  }

  doc["finger_present"] = v.fingerPresent;
  doc["hr_valid"] = v.hrValid;
  doc["spo2_valid"] = v.spo2Valid;

#if ENABLE_BP_SIM
  if (!isnan(sbp)) doc["systolic_bp"] = (int)sbp;
  if (!isnan(dbp)) doc["diastolic_bp"] = (int)dbp;
  doc["bp_unit"] = "mmHg";
  doc["bp_simulated"] = true;
  doc["bp_pot_raw"] = bpRaw;
#else
  doc["bp_simulated"] = false;
#endif

  doc["bp_alert"] = bpAlert;
  doc["hr_alert"] = hrAlert;
  doc["spo2_alert"] = spo2Alert;
  doc["buzzer_alert"] = buzzerAlert;
  doc["raw_ir"] = irBuffer[irIdx == 0 ? IR_BUF_LEN - 1 : irIdx - 1];
  doc["raw_red"] = redBuffer[irIdx == 0 ? IR_BUF_LEN - 1 : irIdx - 1];

  String out;
  serializeJson(doc, out);
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  server.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  server.send(200, "application/json", out);
}

void handleOptions() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  server.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  server.send(204);
}

void handleRoot() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  String html = "<html><head><title>ICU Sentinel ESP8266</title></head><body>";
  html += "<h1>ICU Sentinel ESP8266</h1>";
  html += "<p>Device: " + String(DEVICE_ID) + "</p>";
  html += "<p>IP: " + WiFi.localIP().toString() + "</p>";
  html += "<p><a href='/data'>GET /data</a></p>";
  html += "<p>bp_simulated=true when potentiometer fitted — not clinical.</p>";
  html += "</body></html>";
  server.send(200, "text/html", html);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  uptimeStartMs = millis();

  sensorInitOk = initSensors();

  if (sensorInitOk) {
    Serial.print("[max30102] pre-filling...");
    uint32_t start = millis();
    while (millis() - start < 4000) {
      sampleMAX30102();
      delay(5);
    }
    Serial.println(" done");
    buffersFilled = true;
  }

  connectWifi();
  lastReportMs = millis();

  server.on("/", HTTP_GET, handleRoot);
  server.on("/data", HTTP_GET, handleData);
  server.on("/data", HTTP_OPTIONS, handleOptions);
  server.onNotFound(handleData); // be permissive for local testing
  server.begin();
  Serial.println("[http] server started on port 80");
}

void loop() {
  maintainWifi();
  uint32_t now = millis();
  if (now - lastSampleMs >= SAMPLE_WINDOW_MS) {
    lastSampleMs = now;
    sampleMAX30102();
  }
  sampleDht();
  server.handleClient();
}
