"""TCP client for fetching historical power/GPS samples from the robot's
control server (link.power_history on Pi #1, see that module and the new
HIS sentence in link/server.py) for pages/power.html's day/month history
charts.

Pi #1 holds the actual SQLite database (it keeps logging even when the
WiFi link to this Pi #2 is down -- see link/power_history.py's module
docstring for why), so every history request is a live round trip to
Pi #1 rather than a local read. Pi #1 paginates its HIS response (one
month of 5-minute samples is ~8640 rows -- too many for one reasonable
line), so fetch_power_history() below loops, bumping the offset, until
it has the whole requested window; same "one command per connection"
style as robot_link.send_command, just repeated.
"""

from robot_link import send_command

# Must match link/power_history.py's FIELD_ORDER exactly (same
# hand-kept-in-sync convention as nmea.py's docstring describes for the
# two repos' copies of that file) -- this is how a flat row of HIS
# fields gets decoded back into named values.
FIELD_ORDER = (
    "ts", "lat", "lon",
    "pv_voltage", "pv_current", "pv_power",
    "battery_voltage", "battery_charging_current", "battery_charging_power",
    "load_voltage", "load_current", "load_power",
    "battery_soc", "battery_temp", "controller_temp", "cpu_temp",
)
ROW_SIZE = len(FIELD_ORDER)

VALID_PERIODS = ("DAY", "MONTH")

# Safety valve against a malformed/looping response (e.g. Pi #1 always
# echoing offset=0): this is well above any real page count -- a month
# at HIS_CHUNK_ROWS=100 is ~87 pages -- so it never fires in practice,
# it just turns "infinite loop" into a clear error if link/server.py's
# HIS handler and this client's pagination contract ever drift apart.
MAX_PAGES = 1000


class PowerHistoryError(RuntimeError):
    """Raised when Pi #1 can't be reached, or its HIS/SMP response
    doesn't match the contract this client expects."""

# Must match link/power_history.py's solar_map_cells column order
# exactly (same hand-kept-in-sync convention as FIELD_ORDER above) --
# how a flat row of SMP fields gets decoded back into named values.
SOLAR_MAP_FIELD_ORDER = ("lat", "lon", "avg_pv_power", "sample_count", "last_ts")
SOLAR_MAP_ROW_SIZE = len(SOLAR_MAP_FIELD_ORDER)

# Must match link/server.py's STS sentence field order exactly (same
# hand-kept-in-sync convention as above) -- see
# link/power_history.py's fetch_stats_summary() for what each figure
# means. All numeric; obstacle_front_count/obstacle_rear_count are
# integers, the rest are floats.
STATS_SUMMARY_FIELD_ORDER = (
    "total_distance_m", "avg_speed_moving_mps", "best_daily_distance_m",
    "solar_harvested_kwh", "energy_consumed_kwh",
    "best_solar_day_kwh", "worst_solar_day_kwh",
    "obstacle_front_count", "obstacle_rear_count",
)

# Must match link/power_history.py's obstacle_log column order exactly
# (same hand-kept-in-sync convention as FIELD_ORDER above) -- how a
# flat row of OBS fields gets decoded back into named values. Unlike
# the other *_FIELD_ORDER tuples above, `side` is a string ("front"/
# "rear"), not a number -- see fetch_obstacle_points() below.
OBSTACLE_FIELD_ORDER = ("ts", "side", "distance_m", "lat", "lon", "heading_deg")
OBSTACLE_ROW_SIZE = len(OBSTACLE_FIELD_ORDER)


def fetch_power_history(period: str, host: str, port: int, timeout: float = 5.0) -> list:
    """Returns every logged sample in the rolling `period` window
    ("DAY"/"MONTH"), oldest first, as a list of dicts keyed by
    FIELD_ORDER (all numeric, lat/lon 0.0 if there was no GPS fix at log
    time -- same placeholder convention as every other field here, see
    link/power_history.py). Raises PowerHistoryError on any failure
    (connection refused, malformed response, Pi #1 reporting an ERR
    sentence) -- callers (app.py's /api/power_history) turn that into a
    clean JSON error rather than a 500."""
    if period not in VALID_PERIODS:
        raise PowerHistoryError(f"BAD_PERIOD:{period}")

    rows = []
    offset = 0
    for _ in range(MAX_PAGES):
        result = send_command(f"HIS,{period},{offset}", host, port, timeout=timeout)
        if not result["ok"]:
            raise PowerHistoryError(result.get("error", "HIS_FAILED"))

        fields = result.get("fields") or []
        if len(fields) < 4:
            raise PowerHistoryError(f"HIS_MALFORMED_RESPONSE:{fields}")

        try:
            total_count = int(fields[1])
            resp_offset = int(fields[2])
            returned_count = int(fields[3])
        except ValueError:
            raise PowerHistoryError(f"HIS_MALFORMED_RESPONSE:{fields}")

        row_fields = fields[4:]
        if len(row_fields) != returned_count * ROW_SIZE:
            raise PowerHistoryError("HIS_FIELD_COUNT_MISMATCH")

        for i in range(returned_count):
            raw = row_fields[i * ROW_SIZE:(i + 1) * ROW_SIZE]
            row = dict(zip(FIELD_ORDER, (float(value) for value in raw)))
            row["ts"] = int(row["ts"])
            rows.append(row)

        offset = resp_offset + returned_count
        if returned_count == 0 or offset >= total_count:
            return rows

    raise PowerHistoryError("HIS_TOO_MANY_PAGES")


def fetch_solar_map(host: str, port: int, timeout: float = 5.0) -> list:
    """Returns every solar-exposure-map grid cell currently known to
    Pi #1 (see link/server.py's SMP sentence and link/power_history.py's
    solar_map_cells table/recompute_solar_map()), as a list of dicts
    keyed by SOLAR_MAP_FIELD_ORDER. Same pagination-loop shape as
    fetch_power_history() above -- see that function's own docstring --
    just against SMP instead of HIS: SMP only takes an offset (no
    period field), since the grid itself has no time window -- Pi #1's
    own retention/pruning is what keeps it from growing unbounded, not
    this read. Raises PowerHistoryError on any failure (connection
    refused, malformed response, Pi #1 reporting an ERR sentence) --
    callers (robot-webserver's /api/solar_map) turn that into a clean
    JSON error rather than a 500."""
    rows = []
    offset = 0
    for _ in range(MAX_PAGES):
        result = send_command(f"SMP,{offset}", host, port, timeout=timeout)
        if not result["ok"]:
            raise PowerHistoryError(result.get("error", "SMP_FAILED"))

        fields = result.get("fields") or []
        if len(fields) < 3:
            raise PowerHistoryError(f"SMP_MALFORMED_RESPONSE:{fields}")

        try:
            total_count = int(fields[0])
            resp_offset = int(fields[1])
            returned_count = int(fields[2])
        except ValueError:
            raise PowerHistoryError(f"SMP_MALFORMED_RESPONSE:{fields}")

        row_fields = fields[3:]
        if len(row_fields) != returned_count * SOLAR_MAP_ROW_SIZE:
            raise PowerHistoryError("SMP_FIELD_COUNT_MISMATCH")

        for i in range(returned_count):
            raw = row_fields[i * SOLAR_MAP_ROW_SIZE:(i + 1) * SOLAR_MAP_ROW_SIZE]
            row = dict(zip(SOLAR_MAP_FIELD_ORDER, (float(value) for value in raw)))
            row["sample_count"] = int(row["sample_count"])
            row["last_ts"] = int(row["last_ts"])
            rows.append(row)

        offset = resp_offset + returned_count
        if returned_count == 0 or offset >= total_count:
            return rows

    raise PowerHistoryError("SMP_TOO_MANY_PAGES")


def fetch_stats_summary(host: str, port: int, timeout: float = 5.0) -> dict:
    """Returns the one-shot /stats summary dict (see link/server.py's STS
    sentence and link/power_history.py's fetch_stats_summary() for what
    each figure means), keyed by STATS_SUMMARY_FIELD_ORDER. No
    pagination -- this is a handful of numbers, not a row set, unlike
    fetch_power_history()/fetch_solar_map()/fetch_obstacle_points()
    above/below. Raises PowerHistoryError on any failure, same as every
    other fetch_* function here."""
    result = send_command("STS", host, port, timeout=timeout)
    if not result["ok"]:
        raise PowerHistoryError(result.get("error", "STS_FAILED"))

    fields = result.get("fields") or []
    if len(fields) != len(STATS_SUMMARY_FIELD_ORDER):
        raise PowerHistoryError(f"STS_MALFORMED_RESPONSE:{fields}")

    try:
        summary = dict(zip(STATS_SUMMARY_FIELD_ORDER, (float(value) for value in fields)))
    except ValueError:
        raise PowerHistoryError(f"STS_MALFORMED_RESPONSE:{fields}")
    summary["obstacle_front_count"] = int(summary["obstacle_front_count"])
    summary["obstacle_rear_count"] = int(summary["obstacle_rear_count"])
    return summary


def fetch_obstacle_points(host: str, port: int, timeout: float = 5.0) -> list:
    """Returns every logged ultrasonic-obstacle reading currently known
    to Pi #1 (see link/server.py's OBS sentence and
    link/power_history.py's obstacle_log table/fetch_obstacle_chunk()),
    as a list of dicts keyed by OBSTACLE_FIELD_ORDER. Same pagination-
    loop shape as fetch_solar_map() above. Legitimately returns an empty
    list until the ultrasonic sensors exist and start logging -- see
    link/power_history.py's log_obstacle() docstring -- callers should
    treat that as "no data yet", not as an error.

    `side` stays a plain string ("front"/"rear") -- unlike every other
    field here it is NOT run through float(), since it isn't numeric on
    the wire (same free-text-field shape as HIS's own `period`
    argument)."""
    rows = []
    offset = 0
    for _ in range(MAX_PAGES):
        result = send_command(f"OBS,{offset}", host, port, timeout=timeout)
        if not result["ok"]:
            raise PowerHistoryError(result.get("error", "OBS_FAILED"))

        fields = result.get("fields") or []
        if len(fields) < 3:
            raise PowerHistoryError(f"OBS_MALFORMED_RESPONSE:{fields}")

        try:
            total_count = int(fields[0])
            resp_offset = int(fields[1])
            returned_count = int(fields[2])
        except ValueError:
            raise PowerHistoryError(f"OBS_MALFORMED_RESPONSE:{fields}")

        row_fields = fields[3:]
        if len(row_fields) != returned_count * OBSTACLE_ROW_SIZE:
            raise PowerHistoryError("OBS_FIELD_COUNT_MISMATCH")

        for i in range(returned_count):
            raw = row_fields[i * OBSTACLE_ROW_SIZE:(i + 1) * OBSTACLE_ROW_SIZE]
            row = dict(zip(OBSTACLE_FIELD_ORDER, raw))
            row["ts"] = int(row["ts"])
            row["distance_m"] = float(row["distance_m"])
            row["lat"] = float(row["lat"])
            row["lon"] = float(row["lon"])
            row["heading_deg"] = float(row["heading_deg"])
            rows.append(row)

        offset = resp_offset + returned_count
        if returned_count == 0 or offset >= total_count:
            return rows

    raise PowerHistoryError("OBS_TOO_MANY_PAGES")
