/* ============================================================================
 * config.example.h — copy to config.h and fill in YOUR values.
 *
 * config.h is git-ignored. NEVER commit real Wi-Fi passwords or device tokens.
 * ========================================================================== */
#pragma once

// ---------------------------------------------------------------------------
// 1) Wi-Fi — your phone's mobile hotspot (ESP32 is 2.4 GHz ONLY)
// ---------------------------------------------------------------------------
#define WIFI_SSID       "MyPhoneHotspot"
#define WIFI_PASSWORD   "hotspot-password-here"

// ---------------------------------------------------------------------------
// 2) Backend address — your LAPTOP's IPv4 address ON THE HOTSPOT NETWORK.
//    Find it on the laptop:  cmd > ipconfig   (look for IPv4 under the Wi-Fi adapter)
//    Example: http://192.168.43.12:8080
//    NEVER use localhost — that would mean the ESP32 itself.
// ---------------------------------------------------------------------------
#define BACKEND_URL     "http://192.168.43.12:8080"

// Ingest path appended to BACKEND_URL
#define INGEST_PATH     "/api/v1/ingest"

// ---------------------------------------------------------------------------
// 3) Device identity — register the device in the web UI (Devices page) and
//    paste the values shown ONCE at registration.
// ---------------------------------------------------------------------------
#define DEVICE_ID       "ESP32-ICU-001"        // must match the registered device_uid
#define DEVICE_TOKEN    "paste-device-token-here"

// ---------------------------------------------------------------------------
// 4) Sampling / reporting
// ---------------------------------------------------------------------------
#define REPORT_INTERVAL_MS   5000   // how often to POST a reading (5 s)
#define SAMPLE_WINDOW_MS     200    // MAX30102 sample cadence for the estimator
#define FINGER_DETECT_IR     50000 // raw IR level above this = finger present (tune per sensor)

// Firmware version reported to the backend (shown on the Devices page)
#define FW_VERSION           "1.0.0"
