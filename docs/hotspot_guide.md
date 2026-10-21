# ESP32 ↔ laptop connection guide (mobile hotspot)

The ESP32 has no screen. If it cannot reach the backend, readings never arrive and the
Devices page shows `offline` / `no data`. Work through the steps in order.

## 1. Radio compatibility

- The ESP32 (ESP32-WROOM) supports **2.4 GHz only**.
- Many phone hotspots are 5 GHz-only or "5 GHz preferred" — switch the hotspot to
  **2.4 GHz** (check the hotspot's advanced settings, band/speed preference).

## 2. Same network for laptop and ESP32

1. Enable the phone's mobile hotspot (SSID + password).
2. Connect the **laptop** to that hotspot (Wi-Fi).
3. Power the ESP32 — it joins the same SSID using the credentials in `esp32/config.h`.

## 3. Find the laptop's address on the hotspot network

The ESP32 posts to the laptop's IP — `localhost` means *the ESP32 itself*, so it never works.

```bat
ipconfig
```

Look under the Wi-Fi adapter → **IPv4 Address**, e.g. `192.168.43.12`.

In `esp32/config.h`:

```c
#define BACKEND_URL "http://192.168.43.12:8080"   // http://LAPTOP_IP:8080
```

Restart the backend on **0.0.0.0** (not 127.0.0.1) so it accepts LAN connections —
`scripts\run.bat` already binds `0.0.0.0:8080`.

## 4. Device credentials

1. On the dashboard open **Devices** → register the device UID printed on Serial at boot.
2. Copy the **device token** (shown once) into `config.h` as `DEVICE_TOKEN`.
3. Re-flash, open Serial Monitor at **115200 baud** — you should see Wi-Fi connect,
   obtain an IP, then `POST /api/v1/ingest` 200 responses.
4. The device row turns `online` after the first accepted reading.

## 5. If the ESP32 still cannot reach the laptop

In order of likelihood:

| Symptom | Fix |
|---|---|
| HTTP connection timeout | Hotspot **AP/client isolation** ("block local network", "client isolation") — disable it in hotspot settings. |
| Works from phone, not from ESP32 | Some hotspots only route the connected phone; use a cheap Wi-Fi router where laptop + ESP32 share one LAN. |
| Connection refused | **Windows Firewall** — allow Python on *Private* networks for TCP 8080 (run `scripts\stop.bat`, allow, restart). |
| 401 on ingest | Wrong/rotated `DEVICE_TOKEN` — copy the current token from Devices. |
| 404 device not found | `DEVICE_UID` mismatch — must match registration exactly. |
| 500 / rejected | Check the `last error` column on the Devices page — the ingest response body explains it. |

## 6. Without any hotspot (offline demo)

Run `scripts\run.bat`, open the dashboard, and use the **test scenarios**
(admin: Admin & Audit → test scenarios) to generate simulated readings and alerts.
No hardware is required for the software demo.

## Reference

- Wiring and firmware details: `esp32/wiring_guide.md`
- Full API list: `docs/api.md`
