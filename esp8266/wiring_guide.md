# ESP8266 Wiring Guide (MedicAssist)

## Hardware
- NodeMCU ESP-12E (ESP8266)
- MAX30102 pulse oximeter
- DHT11 temperature/humidity sensor
- 10k potentiometer (for SIMULATED BP on A0)
- Active buzzer (optional) on D5
- Breadboard, jumper wires, USB cable

## Connections (from config.example.h)
| Component | ESP8266 Pin | Notes |
|---|---|---|
| MAX30102 VCC | 3.3V | 3.3V only |
| MAX30102 GND | GND | |
| MAX30102 SDA | D2 (GPIO4) | I2C data |
| MAX30102 SCL | D1 (GPIO5) | I2C clock |
| DHT11 VCC | 3.3V | |
| DHT11 GND | GND | |
| DHT11 DATA | D4 (GPIO2) | 10k pull-up recommended |
| Buzzer (+) | D5 (GPIO14) | Use active buzzer; set BUZZER_ACTIVE_HIGH |
| Buzzer (-) | GND | |
| Potentiometer VCC | 3.3V | SIMULATED BP only |
| Potentiometer GND | GND | |
| Potentiometer WIPER | A0 | ADC input |

## Firmware setup
1. Copy config.example.h to config.h and edit Wi-Fi SSID/password and DEVICE_ID.
2. Open icu_monitor_esp8266.ino in Arduino IDE with ESP8266 board package installed.
3. Install libraries: SparkFun MAX3010x Sensor Library, DHT sensor library, ArduinoJson.
4. Upload to NodeMCU. Open Serial Monitor at 115200 to see IP.
5. The device serves GET http://<ESP_IP>/data on port 80.

## JSON (/data) contract
Fields returned (see firmware): device, wifi_connected, ip, heart_rate, spo2, ambient_temperature_c, humidity_percent, finger_present, hr_valid, spo2_valid, systolic_bp, diastolic_bp, bp_unit, bp_simulated, bp_pot_raw, bp_alert, hr_alert, spo2_alert, buzzer_alert, raw_ir, raw_red, uptime_ms.  
**SIMULATED BP** comes from the potentiometer and is not a clinical measurement.

## Using with the website
1. On the website Devices page, enter the ESP8266 IP (from Serial Monitor / hotspot) and click **Save & Enable**.
2. Click **Connect** to poll every ~1.5s (direct browser fetch with server proxy fallback).
3. The dashboard labels simulated BP clearly and never treats it as evidence.
