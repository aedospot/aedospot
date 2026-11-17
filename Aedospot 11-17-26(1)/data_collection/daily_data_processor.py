import math
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# AEDOSPOT - DAILY ENVIRONMENTAL DATA PROCESSOR
# ============================================================
# Reads raw_sensor_readings.csv and creates:
#   1. daily_environmental_data.csv
#   2. daily_data_diagnostics.csv
#   3. invalid_sensor_readings.csv
#
# Required raw CSV columns:
# timestamp, temperature, humidity, rainfall_pulses,
# wind_pulses, interval_ms
#
# Optional Arduino fields (rainfall_mm, wind_frequency_hz,
# wind_speed) are not used for recalculation. This script
# recalculates from raw pulse counts using the settings below.
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

RAW_FILE = BASE_DIR / "raw_sensor_readings.csv"
DAILY_FILE = BASE_DIR / "daily_environmental_data.csv"
DIAGNOSTICS_FILE = BASE_DIR / "daily_data_diagnostics.csv"
INVALID_FILE = BASE_DIR / "invalid_sensor_readings.csv"

# Time and reporting configuration
TIMEZONE = "Asia/Manila"
EXPECTED_INTERVAL_SECONDS = 2.0
MIN_DAILY_COVERAGE = 90.0
MIN_SENSOR_COVERAGE = 90.0
MAX_INTERVAL_FOR_COVERAGE_SECONDS = 10.0

# DHT22 operating limits
TEMP_MIN_C = -40.0
TEMP_MAX_C = 80.0
RH_MIN = 0.0
RH_MAX = 100.0

# Anemometer plausible limits
WIND_MIN_MPS = 0.0
WIND_MAX_MPS = 70.0

# Interval validity limits
MIN_INTERVAL_MS = 500.0
MAX_INTERVAL_MS = 10000.0

# MISOL WH-SP-RG: 0.3 mm per physical bucket tip.
# Set to 1 if the final debounced Arduino code produces
# one accepted pulse per physical tip.
# Change to 2 ONLY if a controlled test confirms two
# accepted pulses per physical tip.
RAIN_MM_PER_TIP = 0.3
RAIN_PULSES_PER_TIP = 1

# Provisional factor based on the supplied anemometer article.
# Validate against a reference instrument before finalizing.
WIND_MPS_PER_HZ = 0.0875
WIND_SPEED_OFFSET_MPS = 0.0

# Set True only after confirming the sensor is installed,
# connected, and operational for the period being processed.
RAIN_GAUGE_OPERATIONAL = False
ANEMOMETER_OPERATIONAL = False


def numeric_series(series):
    """Convert values to finite numeric values; invalid entries become NaN."""
    result = pd.to_numeric(series, errors="coerce")
    return result.replace([np.inf, -np.inf], np.nan)


def load_raw_data():
    if not RAW_FILE.exists():
        raise FileNotFoundError(
            f"Raw sensor file not found: {RAW_FILE}\n"
            "Create raw_sensor_readings.csv with the required columns."
        )

    df = pd.read_csv(RAW_FILE)

    required = [
        "timestamp", "temperature", "humidity",
        "rainfall_pulses", "wind_pulses", "interval_ms"
    ]
    missing = [column for column in required if column not in df.columns]
    if missing:
        raise ValueError(f"Missing required CSV columns: {missing}")

    # Receiver timestamps should include a timezone/offset.
    # Naive timestamps are interpreted as UTC by this conversion.
    df["timestamp"] = pd.to_datetime(
        df["timestamp"], errors="coerce", utc=True
    ).dt.tz_convert(TIMEZONE)

    for column in [
        "temperature", "humidity", "rainfall_pulses",
        "wind_pulses", "interval_ms"
    ]:
        df[column] = numeric_series(df[column])

    rows_without_timestamp = int(df["timestamp"].isna().sum())
    df = df.dropna(subset=["timestamp"]).copy()
    df = df.sort_values("timestamp")

    duplicate_count = int(df.duplicated(subset=["timestamp"]).sum())
    df = df.drop_duplicates(subset=["timestamp"], keep="last")
    df = df.reset_index(drop=True)

    print(f"Rows removed for invalid timestamps: {rows_without_timestamp}")
    print(f"Duplicate timestamps removed: {duplicate_count}")
    return df


def validate_data(df):
    df = df.copy()

    df["temperature_valid"] = df["temperature"].between(
        TEMP_MIN_C, TEMP_MAX_C, inclusive="both"
    )
    df["humidity_valid"] = df["humidity"].between(
        RH_MIN, RH_MAX, inclusive="both"
    )

    df["rain_pulses_valid"] = (
        df["rainfall_pulses"].notna()
        & (df["rainfall_pulses"] >= 0)
        & (df["rainfall_pulses"] % 1 == 0)
    )
    df["wind_pulses_valid"] = (
        df["wind_pulses"].notna()
        & (df["wind_pulses"] >= 0)
        & (df["wind_pulses"] % 1 == 0)
    )

    df["interval_valid"] = df["interval_ms"].between(
        MIN_INTERVAL_MS, MAX_INTERVAL_MS, inclusive="both"
    )
    df["interval_seconds"] = df["interval_ms"] / 1000.0

    # Treat DHT22 temperature and humidity as one sensor pair.
    df["dht_valid"] = df["temperature_valid"] & df["humidity_valid"]

    # Rainfall per reporting interval, calculated from pulse counts.
    df["rainfall_interval_mm"] = np.nan
    if RAIN_PULSES_PER_TIP <= 0:
        raise ValueError("RAIN_PULSES_PER_TIP must be greater than zero.")
    if RAIN_MM_PER_TIP < 0:
        raise ValueError("RAIN_MM_PER_TIP cannot be negative.")

    rain_mask = df["rain_pulses_valid"] & df["interval_valid"]
    df.loc[rain_mask, "rainfall_interval_mm"] = (
        df.loc[rain_mask, "rainfall_pulses"]
        / RAIN_PULSES_PER_TIP
        * RAIN_MM_PER_TIP
    )

    # Wind speed per reporting interval.
    df["wind_frequency_hz"] = np.nan
    df["wind_speed_mps"] = np.nan

    wind_input_mask = df["wind_pulses_valid"] & df["interval_valid"]
    df.loc[wind_input_mask, "wind_frequency_hz"] = (
        df.loc[wind_input_mask, "wind_pulses"]
        / df.loc[wind_input_mask, "interval_seconds"]
    )
    df.loc[wind_input_mask, "wind_speed_mps"] = (
        df.loc[wind_input_mask, "wind_frequency_hz"] * WIND_MPS_PER_HZ
        + WIND_SPEED_OFFSET_MPS
    )

    df["wind_speed_valid"] = df["wind_speed_mps"].between(
        WIND_MIN_MPS, WIND_MAX_MPS, inclusive="both"
    )
    df.loc[~df["wind_speed_valid"], "wind_speed_mps"] = np.nan

    def issue_text(row):
        issues = []
        if not row["interval_valid"]:
            issues.append("invalid_interval")
        if not row["dht_valid"]:
            issues.append("invalid_or_missing_dht22")
        if not row["rain_pulses_valid"]:
            issues.append("invalid_or_missing_rain_pulses")
        if not row["wind_pulses_valid"]:
            issues.append("invalid_or_missing_wind_pulses")
        elif row["interval_valid"] and not row["wind_speed_valid"]:
            issues.append("wind_speed_out_of_range")
        return ";".join(issues)

    df["validation_issues"] = df.apply(issue_text, axis=1)
    df["has_validation_issue"] = df["validation_issues"].ne("")
    return df


def covered_seconds(group, valid_mask):
    """
    Sum valid observation intervals, excluding long gaps.
    This prevents one long interval from falsely filling a gap.
    """
    intervals = group.loc[valid_mask, "interval_seconds"].dropna()
    intervals = intervals[
        (intervals > 0)
        & (intervals <= MAX_INTERVAL_FOR_COVERAGE_SECONDS)
    ]
    return float(intervals.sum())


def coverage_percent(seconds):
    return min(100.0, seconds / 86400.0 * 100.0)


def process_daily_data(df):
    df = df.copy()
    df["date"] = df["timestamp"].dt.date

    daily_records = []
    diagnostic_records = []
    today = pd.Timestamp.now(tz=TIMEZONE).date()

    for date, group in df.groupby("date", sort=True):
        dht_mask = group["dht_valid"] & group["interval_valid"]
        rain_mask = group["rain_pulses_valid"] & group["interval_valid"]
        wind_mask = (
            group["wind_pulses_valid"]
            & group["interval_valid"]
            & group["wind_speed_valid"]
        )
        interval_mask = group["interval_valid"]

        overall_coverage = coverage_percent(
            covered_seconds(group, interval_mask)
        )
        dht_coverage = coverage_percent(
            covered_seconds(group, dht_mask)
        )
        rain_coverage = coverage_percent(
            covered_seconds(group, rain_mask)
        )
        wind_coverage = coverage_percent(
            covered_seconds(group, wind_mask)
        )

        valid_temp = group.loc[dht_mask, "temperature"]
        valid_rh = group.loc[dht_mask, "humidity"]

        tmean = float(valid_temp.mean()) if not valid_temp.empty else np.nan
        rh = float(valid_rh.mean()) if not valid_rh.empty else np.nan

        # Rain gauge daily total is the sum of interval increments.
        rainfall = np.nan
        if (
            RAIN_GAUGE_OPERATIONAL
            and rain_coverage >= MIN_SENSOR_COVERAGE
            and rain_mask.any()
        ):
            rainfall = float(
                group.loc[rain_mask, "rainfall_interval_mm"].sum()
            )

        # Time-weighted daily mean wind speed.
        wind_speed = np.nan
        if (
            ANEMOMETER_OPERATIONAL
            and wind_coverage >= MIN_SENSOR_COVERAGE
            and wind_mask.any()
        ):
            wind_rows = group.loc[wind_mask]
            wind_speed = float(
                np.average(
                    wind_rows["wind_speed_mps"],
                    weights=wind_rows["interval_ms"]
                )
            )

        accepted = (
            overall_coverage >= MIN_DAILY_COVERAGE
            and dht_coverage >= MIN_SENSOR_COVERAGE
            and rain_coverage >= MIN_SENSOR_COVERAGE
            and wind_coverage >= MIN_SENSOR_COVERAGE
            and RAIN_GAUGE_OPERATIONAL
            and ANEMOMETER_OPERATIONAL
            and not pd.isna(tmean)
            and not pd.isna(rh)
            and not pd.isna(rainfall)
            and not pd.isna(wind_speed)
        )

        # Do not treat the current unfinished day as a completed day.
        if date == today:
            accepted = False
            rejection_reason = "current_day_incomplete"
        elif accepted:
            rejection_reason = ""
        else:
            rejection_reason = "coverage_or_sensor_requirement_not_met"

        diagnostic_records.append({
            "date": str(date),
            "total_records": len(group),
            "expected_records_at_2s": int(86400 / EXPECTED_INTERVAL_SECONDS),
            "overall_coverage_percent": round(overall_coverage, 2),
            "dht_coverage_percent": round(dht_coverage, 2),
            "rain_coverage_percent": round(rain_coverage, 2),
            "wind_coverage_percent": round(wind_coverage, 2),
            "valid_dht_readings": int(dht_mask.sum()),
            "valid_rain_readings": int(rain_mask.sum()),
            "valid_wind_readings": int(wind_mask.sum()),
            "rain_gauge_operational": RAIN_GAUGE_OPERATIONAL,
            "anemometer_operational": ANEMOMETER_OPERATIONAL,
            "rain_mm_per_tip": RAIN_MM_PER_TIP,
            "rain_pulses_per_tip": RAIN_PULSES_PER_TIP,
            "wind_mps_per_hz": WIND_MPS_PER_HZ,
            "wind_offset_mps": WIND_SPEED_OFFSET_MPS,
            "accepted_for_ai": bool(accepted),
            "rejection_reason": rejection_reason
        })

        if accepted:
            daily_records.append({
                "date": str(date),
                "RAINFALL": round(float(rainfall), 4),
                "TMEAN": round(float(tmean), 4),
                "RH": round(float(rh), 4),
                "WIND_SPEED": round(float(wind_speed), 4)
            })

    return daily_records, diagnostic_records


def save_invalid_readings(df):
    columns = [
        "timestamp", "temperature", "humidity",
        "rainfall_pulses", "wind_pulses", "interval_ms",
        "validation_issues"
    ]
    invalid_df = df.loc[df["has_validation_issue"], columns].copy()
    invalid_df.to_csv(INVALID_FILE, index=False)
    return len(invalid_df)


def main():
    print("=" * 58)
    print(" AEDOSPOT - DAILY ENVIRONMENTAL DATA PROCESSOR")
    print("=" * 58)

    print("\nCalibration and status:")
    print(f"Rain resolution: {RAIN_MM_PER_TIP} mm per physical tip")
    print(f"Accepted rain pulses per tip: {RAIN_PULSES_PER_TIP}")
    print(f"Wind factor: {WIND_MPS_PER_HZ} m/s per Hz")
    print(f"Wind offset: {WIND_SPEED_OFFSET_MPS} m/s")
    print(f"Rain gauge operational: {RAIN_GAUGE_OPERATIONAL}")
    print(f"Anemometer operational: {ANEMOMETER_OPERATIONAL}")

    raw_df = load_raw_data()
    print(f"\nTimestamped raw records loaded: {len(raw_df)}")

    if raw_df.empty:
        print("No usable timestamped readings found.")
        return

    validated_df = validate_data(raw_df)
    invalid_count = save_invalid_readings(validated_df)

    daily_records, diagnostic_records = process_daily_data(validated_df)

    model_columns = ["date", "RAINFALL", "TMEAN", "RH", "WIND_SPEED"]
    daily_df = pd.DataFrame(daily_records, columns=model_columns)
    diagnostics_df = pd.DataFrame(diagnostic_records)

    daily_df.to_csv(DAILY_FILE, index=False)
    diagnostics_df.to_csv(DIAGNOSTICS_FILE, index=False)

    print("\nProcessing completed.")
    print(f"Rows with validation issues: {invalid_count}")
    print(f"Daily dates diagnosed: {len(diagnostics_df)}")
    print(f"Daily records accepted for AI: {len(daily_df)}")

    print("\nGenerated files:")
    print(f"1. {DAILY_FILE}")
    print(f"2. {DIAGNOSTICS_FILE}")
    print(f"3. {INVALID_FILE}")

    if not RAIN_GAUGE_OPERATIONAL:
        print("\nWARNING: Rain gauge is marked NOT operational.")
    if not ANEMOMETER_OPERATIONAL:
        print("WARNING: Anemometer is marked NOT operational.")

    if daily_df.empty:
        print(
            "\nNo complete daily records are ready for AI. "
            "Check calibration, operational flags, and coverage."
        )
    else:
        print("\nModel-ready data preview:")
        print(daily_df.tail())

    print("\nFinished.")


if __name__ == "__main__":
    main()
