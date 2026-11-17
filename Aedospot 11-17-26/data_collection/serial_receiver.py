import csv
import json
import math
import os
import time
from datetime import datetime

import serial


# ============================================================
# AEDOSPOT - ARDUINO SERIAL DATA COLLECTOR
# ============================================================
# Reads JSON lines from Arduino, validates sensor values,
# calculates calibrated rainfall and wind speed, displays them,
# and saves BOTH raw and converted values to CSV.
#
# Required Arduino JSON fields:
# temperature, humidity, rainfall_pulses, wind_pulses, interval_ms
#
# The daily processor can continue using the raw pulse columns.
# It recalculates the daily features using its own calibration
# settings, so keep those settings synchronized.
# ============================================================


# ----------------------------
# 1. SERIAL CONFIGURATION
# ----------------------------

SERIAL_PORT = "COM5"       # Change if your Arduino uses another COM port
BAUD_RATE = 9600
SERIAL_TIMEOUT_SECONDS = 2

OUTPUT_FILE = "raw_sensor_readings.csv"


# ----------------------------
# 2. CALIBRATION SETTINGS
# ----------------------------

# MISOL WH-SP-RG: 0.3 mm per physical bucket tip.
# The final Arduino sketch uses debounce and counts accepted
# falling-edge pulses. Use 1 unless a controlled test confirms
# 2 accepted pulses for every physical tip.
RAIN_MM_PER_TIP = 0.3
RAIN_PULSES_PER_TIP = 1

# Provisional factor from the supplied anemometer article.
# Validate against a reference anemometer before finalizing.
WIND_MPS_PER_HZ = 0.0875
WIND_SPEED_OFFSET_MPS = 0.0


# ----------------------------
# 3. VALIDATION LIMITS
# ----------------------------

TEMP_MIN_C = -40.0
TEMP_MAX_C = 80.0
RH_MIN = 0.0
RH_MAX = 100.0

WIND_MIN_MPS = 0.0
WIND_MAX_MPS = 70.0

MIN_INTERVAL_MS = 500
MAX_INTERVAL_MS = 10000


# ----------------------------
# 4. CSV CONFIGURATION
# ----------------------------

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


def create_csv_if_needed():
    """Create the CSV file and headers if it does not exist."""
    file_exists = os.path.exists(OUTPUT_FILE)

    if not file_exists or os.path.getsize(OUTPUT_FILE) == 0:
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
            writer.writeheader()


def is_finite_number(value):
    """True only for finite numeric values, excluding booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def validate_nonnegative_integer(value, field_name):
    """Validate pulse counts as nonnegative whole numbers."""
    if not is_finite_number(value):
        raise ValueError(f"{field_name} is missing or not numeric.")

    if value < 0 or int(value) != value:
        raise ValueError(f"{field_name} must be a nonnegative integer.")

    return int(value)


def calculate_rainfall(rain_pulses):
    """Convert accepted rain pulses into interval rainfall in mm."""
    if RAIN_PULSES_PER_TIP <= 0:
        raise ValueError("RAIN_PULSES_PER_TIP must be greater than zero.")

    physical_tips = rain_pulses / RAIN_PULSES_PER_TIP
    return physical_tips * RAIN_MM_PER_TIP


def calculate_wind_speed(wind_pulses, interval_ms):
    """Return pulse frequency (Hz) and wind speed (m/s)."""
    if interval_ms <= 0:
        raise ValueError("Measurement interval must be positive.")

    interval_seconds = interval_ms / 1000.0
    frequency_hz = wind_pulses / interval_seconds

    wind_speed = (
        frequency_hz * WIND_MPS_PER_HZ
        + WIND_SPEED_OFFSET_MPS
    )

    if not WIND_MIN_MPS <= wind_speed <= WIND_MAX_MPS:
        raise ValueError(
            f"Calculated wind speed {wind_speed:.3f} m/s "
            "is outside the configured range."
        )

    return frequency_hz, wind_speed


def validate_and_process(data):
    """
    Validate one Arduino JSON object and calculate calibrated values.

    Missing/invalid DHT22 values are retained as blank CSV values,
    while invalid pulse counts or intervals reject the whole record.
    """
    required_fields = [
        "temperature",
        "humidity",
        "rainfall_pulses",
        "wind_pulses",
        "interval_ms",
    ]

    missing_fields = [key for key in required_fields if key not in data]
    if missing_fields:
        raise ValueError(f"Missing JSON fields: {', '.join(missing_fields)}")

    interval_ms = data["interval_ms"]
    if not is_finite_number(interval_ms):
        raise ValueError("interval_ms is missing or not numeric.")

    if not MIN_INTERVAL_MS <= interval_ms <= MAX_INTERVAL_MS:
        raise ValueError(
            f"interval_ms must be between {MIN_INTERVAL_MS} "
            f"and {MAX_INTERVAL_MS}."
        )

    rain_pulses = validate_nonnegative_integer(
        data["rainfall_pulses"], "rainfall_pulses"
    )
    wind_pulses = validate_nonnegative_integer(
        data["wind_pulses"], "wind_pulses"
    )

    # DHT22 values may be null when the sensor read fails.
    temperature = data["temperature"]
    humidity = data["humidity"]

    if is_finite_number(temperature):
        if not TEMP_MIN_C <= temperature <= TEMP_MAX_C:
            temperature = None
            print("WARNING: Temperature outside DHT22 range; saved as blank.")
    else:
        temperature = None
        print("WARNING: Temperature missing/invalid; saved as blank.")

    if is_finite_number(humidity):
        if not RH_MIN <= humidity <= RH_MAX:
            humidity = None
            print("WARNING: Humidity outside DHT22 range; saved as blank.")
    else:
        humidity = None
        print("WARNING: Humidity missing/invalid; saved as blank.")

    rainfall_mm = calculate_rainfall(rain_pulses)
    wind_frequency_hz, wind_speed = calculate_wind_speed(
        wind_pulses, interval_ms
    )

    return {
        "temperature": temperature,
        "humidity": humidity,
        "rainfall_pulses": rain_pulses,
        "rainfall_mm": round(rainfall_mm, 4),
        "wind_pulses": wind_pulses,
        "wind_frequency_hz": round(wind_frequency_hz, 4),
        "wind_speed": round(wind_speed, 4),
        "interval_ms": int(interval_ms),
    }


def save_reading(processed_data):
    """Save raw counts and calibrated measurements to the CSV."""
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

    record = {
        "timestamp": timestamp,
        **processed_data,
    }

    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writerow(record)
        file.flush()

    return timestamp


def display_reading(timestamp, data):
    """Display both raw sensor counts and calibrated values."""
    temp_text = (
        f"{data['temperature']:.1f} °C"
        if data["temperature"] is not None else "INVALID / MISSING"
    )
    humidity_text = (
        f"{data['humidity']:.1f}%"
        if data["humidity"] is not None else "INVALID / MISSING"
    )

    print(f"[{timestamp}]")
    print(f"Temperature: {temp_text} | Humidity: {humidity_text}")
    print(
        f"Rain pulses: {data['rainfall_pulses']} | "
        f"Rainfall: {data['rainfall_mm']:.3f} mm"
    )
    print(
        f"Wind pulses: {data['wind_pulses']} | "
        f"Frequency: {data['wind_frequency_hz']:.3f} Hz | "
        f"Wind speed: {data['wind_speed']:.3f} m/s"
    )
    print(f"Interval: {data['interval_ms']} ms")
    print("-" * 65)


def main():
    create_csv_if_needed()

    print("=" * 65)
    print("             AEDOSPOT SENSOR DATA COLLECTOR")
    print("=" * 65)
    print(f"Serial port: {SERIAL_PORT}")
    print(f"Baud rate: {BAUD_RATE}")
    print(f"Saving to: {os.path.abspath(OUTPUT_FILE)}")
    print("\nCalibration settings:")
    print(f"Rain: {RAIN_MM_PER_TIP} mm/tip, {RAIN_PULSES_PER_TIP} pulse(s)/tip")
    print(f"Wind: {WIND_MPS_PER_HZ} m/s per Hz")
    print("\nConnecting to Arduino...")

    try:
        with serial.Serial(
            SERIAL_PORT,
            BAUD_RATE,
            timeout=SERIAL_TIMEOUT_SECONDS
        ) as arduino:

            # Opening a serial connection may reset the Arduino.
            time.sleep(2)

            # Clear startup text already buffered by the serial port.
            arduino.reset_input_buffer()

            print("Arduino connected.")
            print("Receiving, validating, and converting sensor readings.")
            print("Press CTRL+C to stop.\n")

            while True:
                raw_line = arduino.readline()

                if not raw_line:
                    continue

                line = raw_line.decode(
                    "utf-8", errors="replace"
                ).strip()

                if not line:
                    continue

                # Ignore Arduino startup messages.
                if not line.startswith("{"):
                    print("Arduino:", line)
                    continue

                try:
                    data = json.loads(line)

                    if not isinstance(data, dict):
                        print("Skipped: JSON message is not an object.")
                        continue

                    processed = validate_and_process(data)
                    timestamp = save_reading(processed)
                    display_reading(timestamp, processed)

                except json.JSONDecodeError:
                    print("Skipped: invalid JSON received from Arduino.")

                except (ValueError, TypeError) as error:
                    print(f"Skipped: invalid sensor data: {error}")

    except serial.SerialException as error:
        print("\nSerial connection error:")
        print(error)
        print("\nCheck the COM port, USB connection, and baud rate.")

    except KeyboardInterrupt:
        print("\nData collection stopped by user.")

    finally:
        print("Aedospot data collector closed.")


if __name__ == "__main__":
    main()
