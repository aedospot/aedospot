import csv
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

import serial


# ============================================================
# AEDOSPOT - ARDUINO SERIAL RECEIVER + WEBSITE SENDER
# Arduino UNO -> Mini PC -> Flask API -> Supabase
# ============================================================

# 1. SERIAL / WEBSITE SETTINGS
SERIAL_PORT = "COM5"       # Check Arduino IDE > Tools > Port
BAUD_RATE = 9600
SERIAL_TIMEOUT_SECONDS = 2

OUTPUT_FILE = "raw_sensor_readings.csv"

# Website and registered physical device
API_URL = "http://127.0.0.1:5000/api/sensor_readings"
DEVICE_ID = 7
API_KEY_ENV_VAR = "AEDOSPOT_DEVICE_API_KEY"
HTTP_TIMEOUT_SECONDS = 8
UPLOAD_INTERVAL_SECONDS = 60  # Upload one aggregated reading per minute

# 2. SENSOR CALIBRATION
# Provisional: verify 0.3 mm/tip and accepted pulses per tip with a controlled test.
RAIN_MM_PER_TIP = 0.3
RAIN_PULSES_PER_TIP = 1

# Provisional factor; verify against a reference anemometer.
WIND_MPS_PER_HZ = 0.0875
WIND_SPEED_OFFSET_MPS = 0.0

# 3. VALIDATION LIMITS
TEMP_MIN_C, TEMP_MAX_C = -40.0, 80.0
RH_MIN, RH_MAX = 0.0, 100.0
WIND_MIN_MPS, WIND_MAX_MPS = 0.0, 70.0
MIN_INTERVAL_MS, MAX_INTERVAL_MS = 500, 10000

CSV_COLUMNS = [
    "timestamp",
    "temperature",
    "humidity",
    "rainfall_pulses",
    "rainfall_mm",
    "wind_pulses",
    "wind_frequency_hz",
    "wind_speed",
    "interval_ms",
]


def is_finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_pulses(value, field_name):
    if not is_finite_number(value) or value < 0 or int(value) != value:
        raise ValueError(f"{field_name} must be a nonnegative whole number.")
    return int(value)


def optional_sensor_value(value, low, high, field_name):
    if not is_finite_number(value):
        print(f"WARNING: {field_name} missing/invalid; sending null.")
        return None
    value = float(value)
    if not low <= value <= high:
        print(f"WARNING: {field_name} outside its valid range; sending null.")
        return None
    return value


def validate_and_convert(data):
    required = (
        "temperature", "humidity", "rainfall_pulses",
        "wind_pulses", "interval_ms"
    )
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError("Missing JSON fields: " + ", ".join(missing))

    interval = data["interval_ms"]
    if not is_finite_number(interval) or not MIN_INTERVAL_MS <= interval <= MAX_INTERVAL_MS:
        raise ValueError(
            f"interval_ms must be between {MIN_INTERVAL_MS} and {MAX_INTERVAL_MS}."
        )
    interval = int(interval)

    rain_pulses = validate_pulses(data["rainfall_pulses"], "rainfall_pulses")
    wind_pulses = validate_pulses(data["wind_pulses"], "wind_pulses")

    temperature = optional_sensor_value(
        data["temperature"], TEMP_MIN_C, TEMP_MAX_C, "Temperature"
    )
    humidity = optional_sensor_value(
        data["humidity"], RH_MIN, RH_MAX, "Humidity"
    )

    if RAIN_PULSES_PER_TIP <= 0:
        raise ValueError("RAIN_PULSES_PER_TIP must be greater than zero.")

    rainfall_mm = (rain_pulses / RAIN_PULSES_PER_TIP) * RAIN_MM_PER_TIP
    frequency_hz = wind_pulses / (interval / 1000.0)
    wind_speed = frequency_hz * WIND_MPS_PER_HZ + WIND_SPEED_OFFSET_MPS

    if not WIND_MIN_MPS <= wind_speed <= WIND_MAX_MPS:
        raise ValueError(f"Calculated wind speed {wind_speed:.3f} m/s is out of range.")

    return {
        "temperature": temperature,
        "humidity": humidity,
        "rainfall_pulses": rain_pulses,
        "rainfall_mm": round(rainfall_mm, 4),
        "wind_pulses": wind_pulses,
        "wind_frequency_hz": round(frequency_hz, 4),
        "wind_speed": round(wind_speed, 4),
        "interval_ms": interval,
    }


def prepare_csv():
    """Create CSV or safely expand the old six-column receiver CSV."""
    if not os.path.exists(OUTPUT_FILE) or os.path.getsize(OUTPUT_FILE) == 0:
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_COLUMNS).writeheader()
        return

    with open(OUTPUT_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        old_columns = reader.fieldnames or []
        if old_columns == CSV_COLUMNS:
            return
        old_rows = list(reader)

    # Preserve previous rows while adding the newer derived-value columns.
    temp_file = OUTPUT_FILE + ".migrating"
    with open(temp_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in old_rows:
            writer.writerow({column: row.get(column, "") for column in CSV_COLUMNS})

    os.replace(temp_file, OUTPUT_FILE)
    print("CSV header updated; previous records were preserved.")


def save_csv_reading(timestamp, values):
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writerow({"timestamp": timestamp, **values})
        f.flush()


def send_to_website(values, api_key):
    """Send converted sensor values to the existing Flask API."""
    payload = {
        "device_id": DEVICE_ID,
        "temperature": values["temperature"],
        "humidity": values["humidity"],
        "rainfall_mm": values["rainfall_mm"],
        "wind_speed": values["wind_speed"],
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            response_text = response.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(response_text)
            except json.JSONDecodeError:
                result = {"message": response_text}
            if 200 <= response.status < 300 and result.get("success", True):
                return True, result
            return False, result

    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            pass
        return False, {"http_status": error.code, "message": detail}

    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, {"message": str(error)}


def new_upload_batch():
    """Accumulators for one or more upload windows."""
    return {
        "temperature_weighted_sum": 0.0,
        "temperature_weight": 0,
        "humidity_weighted_sum": 0.0,
        "humidity_weight": 0,
        "rainfall_mm": 0.0,
        "wind_weighted_sum": 0.0,
        "wind_weight": 0,
        "records": 0,
    }


def add_to_upload_batch(batch, values):
    """Keep raw readings locally; aggregate values for the website."""
    weight = values["interval_ms"]

    if values["temperature"] is not None:
        batch["temperature_weighted_sum"] += values["temperature"] * weight
        batch["temperature_weight"] += weight

    if values["humidity"] is not None:
        batch["humidity_weighted_sum"] += values["humidity"] * weight
        batch["humidity_weight"] += weight

    # Rainfall is an accumulated quantity: SUM interval rainfall, never average it.
    batch["rainfall_mm"] += values["rainfall_mm"]

    # Wind speed is a rate: use an interval-duration-weighted average.
    batch["wind_weighted_sum"] += values["wind_speed"] * weight
    batch["wind_weight"] += weight
    batch["records"] += 1


def build_upload_values(batch):
    """Convert accumulated readings into the payload expected by Flask."""
    temperature = (
        batch["temperature_weighted_sum"] / batch["temperature_weight"]
        if batch["temperature_weight"] else None
    )
    humidity = (
        batch["humidity_weighted_sum"] / batch["humidity_weight"]
        if batch["humidity_weight"] else None
    )
    wind_speed = (
        batch["wind_weighted_sum"] / batch["wind_weight"]
        if batch["wind_weight"] else 0.0
    )

    return {
        "temperature": round(temperature, 2) if temperature is not None else None,
        "humidity": round(humidity, 2) if humidity is not None else None,
        "rainfall_mm": round(batch["rainfall_mm"], 4),
        "wind_speed": round(wind_speed, 4),
    }


def main():
    api_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not api_key:
        print(f"ERROR: API key not found. Set the {API_KEY_ENV_VAR} environment variable first.")
        print("Do not paste the key into this script or share it in screenshots.")
        return

    if RAIN_PULSES_PER_TIP <= 0:
        print("ERROR: RAIN_PULSES_PER_TIP must be greater than zero.")
        return

    prepare_csv()

    print("=" * 68)
    print("AEDOSPOT SENSOR RECEIVER + WEBSITE CONNECTION")
    print("=" * 68)
    print(f"Serial port: {SERIAL_PORT} at {BAUD_RATE} baud")
    print(f"CSV: {os.path.abspath(OUTPUT_FILE)}")
    print(f"Website API: {API_URL}")
    print(f"Registered device ID: {DEVICE_ID}")
    print("API key: loaded from environment variable (hidden)")
    print("Press CTRL+C to stop.\n")

    try:
        with serial.Serial(
            SERIAL_PORT, BAUD_RATE, timeout=SERIAL_TIMEOUT_SECONDS
        ) as arduino:
            time.sleep(2)  # Arduino may reset when serial opens
            arduino.reset_input_buffer()
            print("Arduino connected. Waiting for sensor JSON...\n")

            while True:
                raw_line = arduino.readline()
                if not raw_line:
                    continue

                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    print("Arduino:", line)
                    continue

                try:
                    raw_data = json.loads(line)
                    if not isinstance(raw_data, dict):
                        print("Skipped: JSON message is not an object.")
                        continue

                    values = validate_and_convert(raw_data)
                    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

                    # Save every valid raw/interval reading locally first.
                    # Website uploads happen separately, once per minute.
                    save_csv_reading(timestamp, values)
                    add_to_upload_batch(upload_batch, values)

                    print(f"[{timestamp}] LOCAL SAVE OK")
                    print(
                        f"Temperature: {values['temperature']} °C | "
                        f"Humidity: {values['humidity']}%"
                    )
                    print(
                        f"Rain: {values['rainfall_pulses']} pulse(s) = "
                        f"{values['rainfall_mm']:.3f} mm"
                    )
                    print(
                        f"Wind: {values['wind_pulses']} pulse(s), "
                        f"{values['wind_frequency_hz']:.3f} Hz = "
                        f"{values['wind_speed']:.3f} m/s"
                    )

                    now = time.monotonic()
                    if now - last_upload_attempt >= UPLOAD_INTERVAL_SECONDS:
                        last_upload_attempt = now

                        if upload_batch["records"] == 0:
                            continue

                        upload_values = build_upload_values(upload_batch)
                        print(
                            f"\\nWEBSITE UPLOAD: {upload_batch['records']} local readings "
                            f"aggregated into one request."
                        )
                        print(
                            "One-minute values -> "
                            f"Temperature: {upload_values['temperature']} °C | "
                            f"Humidity: {upload_values['humidity']}% | "
                            f"Rainfall total: {upload_values['rainfall_mm']:.3f} mm | "
                            f"Average wind: {upload_values['wind_speed']:.3f} m/s"
                        )

                        ok, result = send_to_website(upload_values, api_key)
                        if ok:
                            print(f"WEBSITE: SENT successfully to Device #{DEVICE_ID}.")
                            if result.get("message"):
                                print("Server:", result["message"])
                            # Clear only after the server confirms success.
                            upload_batch = new_upload_batch()
                        else:
                            print(
                                "WEBSITE: UPLOAD FAILED. Keeping this batch in memory "
                                "and in the CSV; it will be retried with the next upload."
                            )
                            print("Server response:", result)

                        print("-" * 68)

                except json.JSONDecodeError:
                    print("Skipped: invalid JSON received from Arduino.")
                except (ValueError, TypeError) as error:
                    print(f"Skipped: invalid sensor data: {error}")

    except serial.SerialException as error:
        print("\nSerial connection error:", error)
        print("Check the COM port, USB cable, and close Arduino Serial Monitor.")

    except KeyboardInterrupt:
        print("\nReceiver stopped by user.")

    finally:
        print("Aedospot receiver closed.")


if __name__ == "__main__":
    main()
