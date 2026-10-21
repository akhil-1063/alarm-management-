# ESP32 Wiring Guide — ICU Sentinel Bedside Node

**ESP32 DevKit (30-pin or 38-pin) + MAX30102 + DHT11**

> **Power note:** never power the ESP32 from the laptop USB while also feeding
> 5 V into its 5V/VIN pin from another supply. Pick one power source.
> The MAX30102 must be fed **3.3 V only** (its LED driver is not 5 V tolerant
> on most breakout boards).

## 1. MAX30102 (I2C — Oxygen & pulse sensor)

| MAX30102 pin | ESP32 pin | Notes |
|---|---|---|
| VIN / 3.3V | **3V3** | 3.3 V only |
| GND | **GND** | common ground required |
| SDA | **GPIO 21** | I2C data |
| SCL | **GPIO 22** | I2C clock |
| INT | *not connected* | firmware polls; interrupt unused |

- Use the short 4-wire jumper that ships with the breakout.
- I2C bus runs at 400 kHz.
- If your breakout has an on-board pull-up disable jumper, leave the pull-ups
  **enabled** (typical 4.7 kΩ) unless you added external ones.

## 2. DHT11 (ambient temperature & humidity — ROOM AIR)

| DHT11 pin | ESP32 pin | Notes |
|---|---|---|
| VCC / + | **3V3** (or VIN/5 V on 3-pin modules with regulator) | check your module |
| DATA | **GPIO 4** | needs a **10 kΩ pull-up** to VCC on bare sensors |
| GND | **GND** | common ground |

- Most DHT11 modules with 3 pins already include the 10 kΩ pull-up; bare
  4-pin sensors need it added externally (pin 3 = NC on bare sensors).
- DHT11 range: 0–50 °C, ±2 °C — **ambient room air only**, never body temp.

## 3. Common ground diagram

```
          +---------------------+
  3V3 ----| VIN  MAX30102   GND |----+
  3V3 ----| VCC  DHT11     GND |----+---+--- GND (ESP32)
  GPIO21--| SDA             SCL |----|--- GPIO21
          +---------------------+    |--- GPIO22
                                     |
 ESP32 GPIO4 ------------------------|--- DATA (DHT11)
```

All three boards must share **one common GND** — separate grounds cause
I2C lockups and garbage readings.

## 4. First flash checklist

1. Arduino IDE → **Tools → Board → esp32 by Espressif Systems** (v2.x/3.x).
2. Library Manager → install:
   - `SparkFun MAX3010x Sensor Library` (v1.1.2+)
   - `DHT sensor library` by Adafruit (v1.4.x, pulls in *Adafruit Unified Sensor*)
3. Copy `esp32/config.example.h` → `esp32/config.h` and fill in:
   - `WIFI_SSID` / `WIFI_PASSWORD` (your phone hotspot, 2.4 GHz)
   - `BACKEND_URL` = laptop IPv4 on that hotspot (`cmd > ipconfig`), e.g.
     `http://192.168.43.12:8080` — **never** `localhost`
   - `DEVICE_ID` + `DEVICE_TOKEN` (from **Devices → Register device** in the
     web UI — the token is shown only once)
4. Upload at **115200 baud**, open Serial Monitor.

## 5. Expected Serial output

```
=================================================
 ICU Sentinel ESP32 node  (fw 1.0.0)
 Device UID: ESP32-ICU-001
=================================================
[max30102] initialized (Red+IR mode, 100 Hz)
[max30102] pre-filling waveform buffer done
[wifi] connecting to 'MyPhoneHotspot'....
[wifi] connected. IP: 192.168.43.77  RSSI: -48
[http] 200 OK  {"status":"ok","patient_id":2,"risk":"low",...}
```

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `[max30102] NOT FOUND` | Wrong SDA/SCL pins, no common GND, or VIN not 3.3 V |
| `[http] connection error -11` | `BACKEND_URL` IP wrong, or backend not running, or laptop firewall blocking port 8080 |
| `[http] 401 — device token invalid` | Token mismatch → re-register device, update `config.h` |
| `quality: "no_finger"` everywhere | Place finger flat over the sensor; clean the lens |
| `quality: "poor"` constantly | Motion artefacts — rest the finger; check LED brightness vs. skin perfusion |
| DHT11 prints `read failed (NaN)` | Missing 10 kΩ pull-up on DATA, or wrong pin |

## 7. Security reminders

- `config.h` is **git-ignored** — never commit it, never share the device token.
- Rotate the token in the web UI (**Devices → Rotate token**) if it is exposed.
- The device token is the only credential; the backend resolves the patient
  from its own assignment table, so a cloned firmware cannot target another
  patient by changing a payload field.
