/* ============================================================================
 * config.example.h — copy to config.h and fill in YOUR values.
 *
 * config.h is git-ignored. NEVER commit real Wi-Fi passwords.
 * ========================================================================= */
#pragma once

// ---------------------------------------------------------------------------
// 1) Wi-Fi (ESP8266 is 2.4 GHz only)
// ---------------------------------------------------------------------------
#define WIFI_SSID       "MyPhoneHotspot"
#define WIFI_PASSWORD   "hotspot-password-here"

// ---------------------------------------------------------------------------
// 2) Device identity (reported in /data and shown in the UI)
// ---------------------------------------------------------------------------
#define DEVICE_ID       "ESP8266-ICU-001"
#define FW_VERSION      "1.0.0"

// ---------------------------------------------------------------------------
// 3) I2C pins for MAX30102 (NodeMCU ESP-12E common wiring)
// ---------------------------------------------------------------------------
#define I2C_SDA  D2
#define I2C_SCL  D1

// ---------------------------------------------------------------------------
// 4) DHT11 pin
// ---------------------------------------------------------------------------
#define DHT_PIN   D4
#define DHT_TYPE  DHT11

// ---------------------------------------------------------------------------
// 5) Buzzer pin (optional)
// ---------------------------------------------------------------------------
#define BUZZER_PIN D5
#define BUZZER_ACTIVE_HIGH true

// ---------------------------------------------------------------------------
// 6) Potentiometer for SIMULATED blood pressure (on A0) — DEMO ONLY
//    These values are never clinical measurements. bp_simulated=true.
// ---------------------------------------------------------------------------
#define ENABLE_BP_SIM  1
#define BP_POT_PIN     A0

// Map the potentiometer to a plausible demo range [min,max] mmHg.
// Tune to your hardware; values outside sanity bounds are clamped by firmware.
#define BP_SYS_MIN     90.0
#define BP_SYS_MAX     170.0
#define BP_DIA_MIN     60.0
#define BP_DIA_MAX     110.0

// ---------------------------------------------------------------------------
// 7) Sampling / reporting
// ---------------------------------------------------------------------------
#define REPORT_INTERVAL_MS   1000   // /data can be polled every 1s
#define SAMPLE_WINDOW_MS     200
#define FINGER_DETECT_IR     50000
