from flask import Flask, request, jsonify, send_from_directory
from pathlib import Path
from datetime import datetime
import sqlite3
import threading
import time
import uuid
import json
import urllib.request
import urllib.error
import os
from dotenv import load_dotenv
import cv2
import numpy as np
import board
import adafruit_dht
import smbus2


# ============================================================
# AQUAVISION V7
# Visual Tank Monitoring Platform
#
# Raspberry Pi 5
# DHT11 on GPIO27
# OpenCV anomaly detection
# SQLite
# Offline dashboard
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path("/home/chamo/aquavision")

DATA_DIR = BASE_DIR / "data"
IMAGE_DIR = DATA_DIR / "images"
OVERLAY_DIR = DATA_DIR / "overlays"
REFERENCE_DIR = DATA_DIR / "references"

DATABASE_FILE = BASE_DIR / "aquavision_prod.db"
BASELINE_FILE = REFERENCE_DIR / "active_reference.jpg"

IMAGE_DIR.mkdir(parents=True, exist_ok=True)
OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE_DIR.mkdir(parents=True, exist_ok=True)

TANK_CODE = "TANK-01"
TANK_NAME = "Tanque principal"

SENSOR_INTERVAL = 5
TARGET_WIDTH = 1200

# Calibración experimental obtenida de las 5 pruebas iniciales.
BASELINE_NOISE_PERCENT = 1.96
CALIBRATION_VERSION = "aquavision-v7-visual"

# Clasificación del porcentaje corregido.
CLEAN_LIMIT = 1.50
OBSERVATION_LIMIT = 4.00
DIRTY_LIMIT = 8.00

# Calidad mínima para considerar una captura confiable.
MIN_RELIABLE_QUALITY = 45.0

# ROI por defecto. El sistema exige configurar la ROI manualmente
# después de registrar una nueva referencia.
DEFAULT_ROI = {
    "x": 0.25,
    "y": 0.25,
    "w": 0.50,
    "h": 0.50
}


# ============================================================
# SUPABASE CLOUD SYNC
# ============================================================
# SQLite remains the source of truth on the Raspberry while offline.
# When internet is available, pending rows are copied to Supabase.
# Images remain local for now (no Storage bucket configured).
load_dotenv()
SUPABASE_ENABLED = True
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

ORGANIZATION_ID = os.getenv("ORGANIZATION_ID")
SITE_ID = os.getenv("SITE_ID")
TANK_ID = os.getenv("TANK_ID")
DEVICE_ID = os.getenv("DEVICE_ID")

SYNC_INTERVAL = 15
SYNC_BATCH_SIZE = 25
SYNC_MAX_ERROR_LENGTH = 500

sync_lock = threading.Lock()
sync_state = {
    "enabled": SUPABASE_ENABLED,
    "online": False,
    "last_success": None,
    "last_attempt": None,
    "last_error": None,
    "pending_sensor": 0,
    "pending_uv": 0,
    "pending_inspections": 0,
    "synced_this_session": 0
}


# ============================================================
# UV MODULE - SAFE RAW MODE
# ============================================================
# The module is detected at I2C address 0x55. Its exact register
# map has not been verified, so AquaVision only performs safe
# reads and stores RAW values. They are NOT presented as calibrated
# UVA irradiance.
UV_I2C_BUS = 1
UV_I2C_ADDRESS = 0x55
UV_SAFE_REGISTERS = [0x00, 0x01, 0x02, 0x10, 0x11, 0x3A]

uv_lock = threading.Lock()
uv_state = {
    "connected": False,
    "timestamp": None,
    "status": "STARTING",
    "address": "0x55",
    "raw_registers": {},
    "raw_word_00": None,
    "raw_word_10": None
}


# ============================================================
# DHT11
# ============================================================

dht = adafruit_dht.DHT11(
    board.D27,
    use_pulseio=False
)

sensor_lock = threading.Lock()

sensor_state = {
    "temperature": None,
    "humidity": None,
    "timestamp": None,
    "status": "STARTING"
}


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=15
    )

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    conn.execute(
        "PRAGMA journal_mode = WAL"
    )

    conn.execute(
        "PRAGMA synchronous = NORMAL"
    )

    return conn


def column_exists(conn, table, column):
    rows = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(
        row["name"] == column
        for row in rows
    )


def ensure_column(
    conn,
    table,
    column,
    definition
):
    if not column_exists(
        conn,
        table,
        column
    ):
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def init_database():
    conn = db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS tanks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        location TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sensor_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tank_id INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        temperature_c REAL,
        humidity_pct REAL,
        status TEXT NOT NULL,

        FOREIGN KEY (tank_id)
            REFERENCES tanks(id)
    );

    CREATE TABLE IF NOT EXISTS uv_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tank_id INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        connected INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        i2c_address TEXT NOT NULL,
        raw_registers_json TEXT,
        raw_word_00 INTEGER,
        raw_word_10 INTEGER,

        FOREIGN KEY (tank_id)
            REFERENCES tanks(id)
    );

    CREATE TABLE IF NOT EXISTS reference_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tank_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,

        FOREIGN KEY (tank_id)
            REFERENCES tanks(id)
    );

    CREATE TABLE IF NOT EXISTS inspections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inspection_uid TEXT NOT NULL UNIQUE,
        tank_id INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        image_filename TEXT NOT NULL,
        overlay_filename TEXT NOT NULL,

        affected_percentage REAL NOT NULL,
        raw_percentage REAL,
        baseline_noise REAL,
        corrected_percentage REAL,

        visual_change REAL NOT NULL,
        condition TEXT NOT NULL,
        capture_quality TEXT NOT NULL,
        quality_score REAL NOT NULL,
        alignment_score REAL NOT NULL,

        brightness REAL,
        contrast REAL,
        illumination_delta REAL,
        detection_threshold REAL,
        affected_pixels INTEGER,
        analyzed_pixels INTEGER,
        roi_json TEXT NOT NULL,

        temperature_c REAL,
        humidity_pct REAL,
        sensor_status TEXT,
        reliable INTEGER NOT NULL DEFAULT 1,
        reference_id INTEGER,
        calibration_version TEXT,

        FOREIGN KEY (tank_id)
            REFERENCES tanks(id),

        FOREIGN KEY (reference_id)
            REFERENCES reference_images(id)
    );

    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tank_id INTEGER NOT NULL,
        inspection_id INTEGER,
        created_at TEXT NOT NULL,
        severity TEXT NOT NULL,
        category TEXT NOT NULL,
        message TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0,

        FOREIGN KEY (tank_id)
            REFERENCES tanks(id),

        FOREIGN KEY (inspection_id)
            REFERENCES inspections(id)
    );
    """)

    # Migración automática desde versiones anteriores.
    migrations = [
        ("raw_percentage", "REAL"),
        ("baseline_noise", "REAL"),
        ("corrected_percentage", "REAL"),
        ("temperature_c", "REAL"),
        ("humidity_pct", "REAL"),
        ("sensor_status", "TEXT"),
        ("reliable", "INTEGER NOT NULL DEFAULT 1"),
        ("reference_id", "INTEGER"),
        ("calibration_version", "TEXT")
    ]

    for column, definition in migrations:
        ensure_column(
            conn,
            "inspections",
            column,
            definition
        )

    # Cloud synchronization metadata. Existing local data is preserved
    # and becomes eligible for one-time upload when internet is available.
    sync_columns = [
        ("event_uuid", "TEXT"),
        ("sync_status", "TEXT NOT NULL DEFAULT 'PENDING'"),
        ("synced_at", "TEXT"),
        ("sync_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_sync_error", "TEXT")
    ]

    for table_name in ("sensor_readings", "uv_readings", "inspections"):
        for column, definition in sync_columns:
            ensure_column(conn, table_name, column, definition)

        rows = conn.execute(
            f"SELECT id FROM {table_name} WHERE event_uuid IS NULL OR event_uuid = ''"
        ).fetchall()

        for row in rows:
            conn.execute(
                f"UPDATE {table_name} SET event_uuid = ?, sync_status = COALESCE(sync_status, 'PENDING') WHERE id = ?",
                (str(uuid.uuid4()), row["id"])
            )

    conn.executescript("""
    CREATE INDEX IF NOT EXISTS
        idx_sensor_time
        ON sensor_readings(captured_at);

    CREATE INDEX IF NOT EXISTS
        idx_uv_time
        ON uv_readings(captured_at);

    CREATE INDEX IF NOT EXISTS
        idx_inspection_time
        ON inspections(captured_at);

    CREATE INDEX IF NOT EXISTS
        idx_inspection_condition
        ON inspections(condition);

    CREATE INDEX IF NOT EXISTS
        idx_alert_time
        ON alerts(created_at);

    CREATE INDEX IF NOT EXISTS
        idx_sensor_sync
        ON sensor_readings(sync_status, id);

    CREATE INDEX IF NOT EXISTS
        idx_uv_sync
        ON uv_readings(sync_status, id);

    CREATE INDEX IF NOT EXISTS
        idx_inspection_sync
        ON inspections(sync_status, id);
    """)

    tank = conn.execute(
        """
        SELECT id
        FROM tanks
        WHERE code = ?
        """,
        (TANK_CODE,)
    ).fetchone()

    if tank is None:
        conn.execute(
            """
            INSERT INTO tanks (
                code,
                name,
                location,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                TANK_CODE,
                TANK_NAME,
                "AquaVision",
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )

    conn.commit()
    conn.close()


def tank_id():
    conn = db()

    row = conn.execute(
        """
        SELECT id
        FROM tanks
        WHERE code = ?
        """,
        (TANK_CODE,)
    ).fetchone()

    conn.close()

    return row["id"]


def active_reference_id():
    conn = db()

    row = conn.execute(
        """
        SELECT id
        FROM reference_images
        WHERE tank_id = ?
          AND active = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (tank_id(),)
    ).fetchone()

    conn.close()

    if row is None:
        return None

    return row["id"]


def set_setting(key, value):
    conn = db()

    conn.execute(
        """
        INSERT INTO settings (
            key,
            value,
            updated_at
        )
        VALUES (?, ?, ?)

        ON CONFLICT(key)
        DO UPDATE SET
            value = excluded.value,
            updated_at = excluded.updated_at
        """,
        (
            key,
            json.dumps(value),
            datetime.now().isoformat(
                timespec="seconds"
            )
        )
    )

    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = db()

    row = conn.execute(
        """
        SELECT value
        FROM settings
        WHERE key = ?
        """,
        (key,)
    ).fetchone()

    conn.close()

    if row is None:
        return default

    try:
        return json.loads(
            row["value"]
        )
    except Exception:
        return default


# ============================================================
# SENSOR
# ============================================================

def sensor_loop():
    current_tank = tank_id()

    while True:
        timestamp = datetime.now().isoformat(
            timespec="seconds"
        )

        temperature = None
        humidity = None
        status = "ERROR"

        try:
            temperature = dht.temperature
            humidity = dht.humidity

            if (
                temperature is not None
                and humidity is not None
            ):
                status = "OK"

        except RuntimeError:
            # El DHT11 puede fallar de forma intermitente.
            status = "RETRY"

        except Exception as exc:
            status = "ERROR"
            print(
                "DHT11 error:",
                exc
            )

        with sensor_lock:
            if temperature is not None:
                sensor_state[
                    "temperature"
                ] = float(
                    temperature
                )

            if humidity is not None:
                sensor_state[
                    "humidity"
                ] = float(
                    humidity
                )

            sensor_state[
                "timestamp"
            ] = timestamp

            sensor_state[
                "status"
            ] = status

        if (
            temperature is not None
            and humidity is not None
        ):
            try:
                conn = db()
                event_uuid = str(uuid.uuid4())

                conn.execute(
                    """
                    INSERT INTO sensor_readings (
                        tank_id,
                        captured_at,
                        temperature_c,
                        humidity_pct,
                        status,
                        event_uuid,
                        sync_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'PENDING')
                    """,
                    (
                        current_tank,
                        timestamp,
                        float(temperature),
                        float(humidity),
                        "OK",
                        event_uuid
                    )
                )

                conn.commit()
                conn.close()

            except Exception as exc:
                print(
                    "Sensor DB error:",
                    exc
                )

        time.sleep(
            SENSOR_INTERVAL
        )


# ============================================================
# UV RAW MONITOR
# ============================================================

def read_uv_registers(bus):
    registers = {}

    for register in UV_SAFE_REGISTERS:
        value = bus.read_byte_data(
            UV_I2C_ADDRESS,
            register
        )

        registers[f"0x{register:02X}"] = int(value)

    word_00 = int(
        bus.read_word_data(
            UV_I2C_ADDRESS,
            0x00
        )
    )

    word_10 = int(
        bus.read_word_data(
            UV_I2C_ADDRESS,
            0x10
        )
    )

    return registers, word_00, word_10


def uv_loop():
    current_tank = tank_id()

    while True:
        timestamp = datetime.now().isoformat(
            timespec="seconds"
        )

        connected = False
        status = "READ_ERROR"
        registers = {}
        word_00 = None
        word_10 = None

        try:
            with smbus2.SMBus(UV_I2C_BUS) as bus:
                registers, word_00, word_10 = (
                    read_uv_registers(bus)
                )

            connected = True
            status = "RAW_OK"

        except Exception as exc:
            print("UV sensor error:", exc)

        with uv_lock:
            uv_state["connected"] = connected
            uv_state["timestamp"] = timestamp
            uv_state["status"] = status
            uv_state["raw_registers"] = registers
            uv_state["raw_word_00"] = word_00
            uv_state["raw_word_10"] = word_10

        try:
            conn = db()
            event_uuid = str(uuid.uuid4())

            conn.execute(
                """
                INSERT INTO uv_readings (
                    tank_id,
                    captured_at,
                    connected,
                    status,
                    i2c_address,
                    raw_registers_json,
                    raw_word_00,
                    raw_word_10,
                    event_uuid,
                    sync_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                """,
                (
                    current_tank,
                    timestamp,
                    1 if connected else 0,
                    status,
                    "0x55",
                    json.dumps(registers),
                    word_00,
                    word_10,
                    event_uuid
                )
            )

            conn.commit()
            conn.close()

        except Exception as exc:
            print("UV DB error:", exc)

        time.sleep(SENSOR_INTERVAL)


# ============================================================
# CLOUD SYNCHRONIZATION
# ============================================================

def supabase_post(table_name, payload):
    if not SUPABASE_ENABLED:
        raise RuntimeError("Supabase sync disabled")

    # IMPORTANT: use a plain INSERT. The previous V7 used PostgREST UPSERT
    # (on_conflict + merge-duplicates), which can require UPDATE/SELECT
    # permissions and additional RLS policies. This Raspberry only needs
    # INSERT permission. event_uuid remains UNIQUE in Supabase, so an
    # already-synced local event is detected as a duplicate and treated
    # as successfully synchronized.
    url = f"{SUPABASE_URL}/rest/v1/{table_name}"

    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_PUBLISHABLE_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            status = int(response.status)
            if status not in (200, 201, 204):
                raise RuntimeError(f"Supabase HTTP {status}")

    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""

        # PostgreSQL unique_violation = 23505. If the same event_uuid is
        # already in Supabase, the local row can safely be marked SYNCED.
        if exc.code == 409 and (
            "23505" in detail
            or "duplicate key" in detail.lower()
            or "event_uuid" in detail.lower()
        ):
            return

        raise RuntimeError(
            f"Supabase HTTP {exc.code}: {detail[:SYNC_MAX_ERROR_LENGTH]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network: {exc.reason}") from exc


def pending_counts(conn):
    return {
        "pending_sensor": conn.execute(
            "SELECT COUNT(*) AS n FROM sensor_readings WHERE sync_status != 'SYNCED'"
        ).fetchone()["n"],
        "pending_uv": conn.execute(
            "SELECT COUNT(*) AS n FROM uv_readings WHERE sync_status != 'SYNCED'"
        ).fetchone()["n"],
        "pending_inspections": conn.execute(
            "SELECT COUNT(*) AS n FROM inspections WHERE sync_status != 'SYNCED'"
        ).fetchone()["n"]
    }


def mark_synced(conn, table_name, row_id):
    conn.execute(
        f"""
        UPDATE {table_name}
        SET sync_status = 'SYNCED',
            synced_at = ?,
            sync_attempts = sync_attempts + 1,
            last_sync_error = NULL
        WHERE id = ?
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            row_id
        )
    )


def mark_sync_error(conn, table_name, row_id, error):
    conn.execute(
        f"""
        UPDATE {table_name}
        SET sync_status = 'ERROR',
            sync_attempts = sync_attempts + 1,
            last_sync_error = ?
        WHERE id = ?
        """,
        (
            str(error)[:SYNC_MAX_ERROR_LENGTH],
            row_id
        )
    )


def sensor_payload(row):
    return {
        "event_uuid": row["event_uuid"],
        "device_id": SUPABASE_DEVICE_ID,
        "temperature_c": row["temperature_c"],
        "humidity_pct": row["humidity_pct"],
        "measured_at": row["captured_at"]
    }


def uv_payload(row):
    try:
        registers = json.loads(row["raw_registers_json"] or "{}")
    except Exception:
        registers = {}

    return {
        "event_uuid": row["event_uuid"],
        "device_id": SUPABASE_DEVICE_ID,
        "raw_register_00": registers.get("0x00"),
        "raw_register_01": registers.get("0x01"),
        "raw_register_02": registers.get("0x02"),
        "raw_register_11": registers.get("0x11"),
        "raw_register_3a": registers.get("0x3A"),
        "raw_word_00": row["raw_word_00"],
        "raw_word_10": row["raw_word_10"],
        "calibration_version": "raw-unverified-0x55",
        "measured_at": row["captured_at"]
    }


def inspection_payload(row):
    return {
        "event_uuid": row["event_uuid"],
        "tank_id": SUPABASE_TANK_ID,
        "device_id": SUPABASE_DEVICE_ID,
        "raw_percentage": row["raw_percentage"],
        "baseline_noise": row["baseline_noise"],
        "corrected_percentage": row["corrected_percentage"],
        "condition": row["condition"],
        "capture_quality": row["capture_quality"],
        "quality_score": row["quality_score"],
        "alignment_score": row["alignment_score"],
        "detection_version": row["calibration_version"] or CALIBRATION_VERSION,
        "captured_at": row["captured_at"]
    }


def sync_table(conn, table_name, payload_builder):
    rows = conn.execute(
        f"""
        SELECT *
        FROM {table_name}
        WHERE sync_status != 'SYNCED'
        ORDER BY id ASC
        LIMIT ?
        """,
        (SYNC_BATCH_SIZE,)
    ).fetchall()

    synced = 0

    for row in rows:
        try:
            supabase_post(table_name, payload_builder(row))
            print(f"SYNC OK {table_name} local_id={row['id']}", flush=True)
            mark_synced(conn, table_name, row["id"])
            conn.commit()
            synced += 1

            with sync_lock:
                sync_state["online"] = True
                sync_state["last_success"] = datetime.now().isoformat(
                    timespec="seconds"
                )
                sync_state["last_error"] = None
                sync_state["synced_this_session"] += 1

        except Exception as exc:
            print(f"SYNC ERROR {table_name} local_id={row['id']}: {exc}", flush=True)
            mark_sync_error(conn, table_name, row["id"], exc)
            conn.commit()

            with sync_lock:
                sync_state["online"] = False
                sync_state["last_error"] = str(exc)[:SYNC_MAX_ERROR_LENGTH]

            # If the network/API is unavailable, there is no point hammering
            # every remaining row in the same cycle.
            break

    return synced


def sync_loop():
    while True:
        with sync_lock:
            sync_state["last_attempt"] = datetime.now().isoformat(
                timespec="seconds"
            )

        conn = None

        try:
            conn = db()

            if SUPABASE_ENABLED:
                sync_table(conn, "sensor_readings", sensor_payload)
                sync_table(conn, "uv_readings", uv_payload)
                sync_table(conn, "inspections", inspection_payload)

            counts = pending_counts(conn)

            with sync_lock:
                sync_state.update(counts)

        except Exception as exc:
            with sync_lock:
                sync_state["online"] = False
                sync_state["last_error"] = str(exc)[:SYNC_MAX_ERROR_LENGTH]

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        time.sleep(SYNC_INTERVAL)


# ============================================================
# IMAGE UTILITIES
# ============================================================

def decode_uploaded_image():
    if "image" in request.files:
        raw = request.files[
            "image"
        ].read()

    else:
        raw = request.get_data()

    if not raw:
        raise ValueError(
            "No se recibió ninguna imagen."
        )

    arr = np.frombuffer(
        raw,
        np.uint8
    )

    image = cv2.imdecode(
        arr,
        cv2.IMREAD_COLOR
    )

    if image is None:
        raise ValueError(
            "El archivo recibido no pudo convertirse a imagen."
        )

    return image


def normalize_image(image):
    height, width = image.shape[:2]

    if width == TARGET_WIDTH:
        return image

    scale = TARGET_WIDTH / width

    target_height = int(
        height * scale
    )

    interpolation = (
        cv2.INTER_AREA
        if scale < 1
        else cv2.INTER_LINEAR
    )

    return cv2.resize(
        image,
        (
            TARGET_WIDTH,
            target_height
        ),
        interpolation=interpolation
    )


def save_jpeg(image, path):
    ok, encoded = cv2.imencode(
        ".jpg",
        image,
        [
            int(
                cv2.IMWRITE_JPEG_QUALITY
            ),
            93
        ]
    )

    if not ok:
        raise ValueError(
            "No fue posible guardar JPEG."
        )

    path.write_bytes(
        encoded.tobytes()
    )


def new_filename(prefix):
    return (
        f"{prefix}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex[:8]}.jpg"
    )


# ============================================================
# ROI
# ============================================================

def current_roi():
    roi = get_setting(
        "analysis_roi",
        DEFAULT_ROI
    )

    if not isinstance(
        roi,
        dict
    ):
        return DEFAULT_ROI

    for key in (
        "x",
        "y",
        "w",
        "h"
    ):
        if key not in roi:
            return DEFAULT_ROI

    return roi


def roi_is_configured():
    return bool(
        get_setting(
            "roi_configured",
            False
        )
    )


def build_roi_mask(image, roi):
    height, width = image.shape[:2]

    x1 = int(
        roi["x"] * width
    )

    y1 = int(
        roi["y"] * height
    )

    x2 = int(
        (
            roi["x"]
            + roi["w"]
        )
        * width
    )

    y2 = int(
        (
            roi["y"]
            + roi["h"]
        )
        * height
    )

    x1 = int(
        np.clip(
            x1,
            0,
            width - 1
        )
    )

    x2 = int(
        np.clip(
            x2,
            x1 + 1,
            width
        )
    )

    y1 = int(
        np.clip(
            y1,
            0,
            height - 1
        )
    )

    y2 = int(
        np.clip(
            y2,
            y1 + 1,
            height
        )
    )

    roi_width = x2 - x1
    roi_height = y2 - y1

    # Excluye una franja interior adicional para reducir
    # falsos positivos en paredes, bordes y refracciones.
    margin_x = max(
        5,
        int(
            roi_width * 0.055
        )
    )

    margin_y = max(
        5,
        int(
            roi_height * 0.055
        )
    )

    ix1 = x1 + margin_x
    ix2 = x2 - margin_x

    iy1 = y1 + margin_y
    iy2 = y2 - margin_y

    if ix2 <= ix1:
        ix1 = x1
        ix2 = x2

    if iy2 <= iy1:
        iy1 = y1
        iy2 = y2

    mask = np.zeros(
        (
            height,
            width
        ),
        np.uint8
    )

    mask[
        iy1:iy2,
        ix1:ix2
    ] = 255

    return (
        mask,
        (
            ix1,
            iy1,
            ix2,
            iy2
        )
    )


# ============================================================
# ALIGNMENT
# ============================================================

def homography_is_reasonable(
    homography,
    width,
    height
):
    try:
        corners = np.float32([
            [0, 0],
            [width - 1, 0],
            [width - 1, height - 1],
            [0, height - 1]
        ]).reshape(
            -1,
            1,
            2
        )

        projected = cv2.perspectiveTransform(
            corners,
            homography
        ).reshape(
            4,
            2
        )

        original_area = float(
            width * height
        )

        projected_area = abs(
            cv2.contourArea(
                projected.astype(
                    np.float32
                )
            )
        )

        ratio = (
            projected_area
            / original_area
            if original_area
            else 0
        )

        return (
            0.55 <= ratio <= 1.60
            and cv2.isContourConvex(
                projected.astype(
                    np.float32
                )
            )
        )

    except Exception:
        return False


def align_images(
    current,
    baseline
):
    current = cv2.resize(
        current,
        (
            baseline.shape[1],
            baseline.shape[0]
        )
    )

    small_width = 700

    scale = (
        small_width
        / baseline.shape[1]
    )

    small_height = int(
        baseline.shape[0]
        * scale
    )

    current_small = cv2.resize(
        current,
        (
            small_width,
            small_height
        )
    )

    baseline_small = cv2.resize(
        baseline,
        (
            small_width,
            small_height
        )
    )

    gray_current = cv2.cvtColor(
        current_small,
        cv2.COLOR_BGR2GRAY
    )

    gray_baseline = cv2.cvtColor(
        baseline_small,
        cv2.COLOR_BGR2GRAY
    )

    try:
        orb = cv2.ORB_create(
            nfeatures=3500,
            scaleFactor=1.2,
            nlevels=8
        )

        kp1, des1 = (
            orb.detectAndCompute(
                gray_current,
                None
            )
        )

        kp2, des2 = (
            orb.detectAndCompute(
                gray_baseline,
                None
            )
        )

        if (
            des1 is None
            or des2 is None
        ):
            return (
                current,
                0.0,
                "NONE"
            )

        matcher = cv2.BFMatcher(
            cv2.NORM_HAMMING
        )

        matches = matcher.knnMatch(
            des1,
            des2,
            k=2
        )

        good = []

        for pair in matches:
            if len(pair) < 2:
                continue

            first, second = pair

            if (
                first.distance
                < 0.72
                * second.distance
            ):
                good.append(
                    first
                )

        if len(good) < 12:
            return (
                current,
                0.0,
                "NONE"
            )

        source = np.float32([
            kp1[
                match.queryIdx
            ].pt
            for match in good
        ]).reshape(
            -1,
            1,
            2
        )

        target = np.float32([
            kp2[
                match.trainIdx
            ].pt
            for match in good
        ]).reshape(
            -1,
            1,
            2
        )

        homography_small, inliers = (
            cv2.findHomography(
                source,
                target,
                cv2.RANSAC,
                4.0
            )
        )

        if (
            homography_small is None
            or inliers is None
        ):
            return (
                current,
                0.0,
                "NONE"
            )

        S = np.array([
            [scale, 0, 0],
            [0, scale, 0],
            [0, 0, 1]
        ], dtype=np.float64)

        homography_full = (
            np.linalg.inv(S)
            @ homography_small
            @ S
        )

        if not homography_is_reasonable(
            homography_full,
            baseline.shape[1],
            baseline.shape[0]
        ):
            return (
                current,
                0.0,
                "REJECTED"
            )

        aligned = cv2.warpPerspective(
            current,
            homography_full,
            (
                baseline.shape[1],
                baseline.shape[0]
            ),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT
        )

        inlier_count = int(
            np.count_nonzero(
                inliers
            )
        )

        inlier_ratio = (
            inlier_count
            / max(
                len(good),
                1
            )
        )

        # La puntuación combina la cantidad de coincidencias
        # válidas con la proporción de inliers.
        quantity_score = min(
            1.0,
            len(good) / 80.0
        )

        score = (
            100
            * (
                0.75 * inlier_ratio
                + 0.25 * quantity_score
            )
        )

        return (
            aligned,
            float(
                np.clip(
                    score,
                    0,
                    100
                )
            ),
            "ORB"
        )

    except Exception as exc:
        print(
            "Alignment error:",
            exc
        )

        return (
            current,
            0.0,
            "NONE"
        )


# ============================================================
# VISUAL ANALYSIS
# ============================================================

def robust_median_mad(values):
    median = float(
        np.median(
            values
        )
    )

    mad = float(
        np.median(
            np.abs(
                values
                - median
            )
        )
    )

    return (
        median,
        mad
    )


def calculate_gradient(gray):
    gray_float = gray.astype(
        np.float32
    )

    gx = cv2.Sobel(
        gray_float,
        cv2.CV_32F,
        1,
        0,
        ksize=3
    )

    gy = cv2.Sobel(
        gray_float,
        cv2.CV_32F,
        0,
        1,
        ksize=3
    )

    return cv2.magnitude(
        gx,
        gy
    )


def analyze(
    current,
    baseline
):
    baseline = normalize_image(
        baseline
    )

    current = normalize_image(
        current
    )

    current, alignment_score, alignment_method = (
        align_images(
            current,
            baseline
        )
    )

    roi = current_roi()

    valid_mask, bounds = (
        build_roi_mask(
            baseline,
            roi
        )
    )

    valid = valid_mask > 0

    analyzed_pixels = int(
        np.count_nonzero(
            valid_mask
        )
    )

    if analyzed_pixels < 1000:
        raise ValueError(
            "Área de análisis demasiado pequeña."
        )

    # ========================================================
    # COLOR SPACE
    # ========================================================

    baseline_lab = cv2.cvtColor(
        baseline,
        cv2.COLOR_BGR2LAB
    ).astype(
        np.float32
    )

    current_lab = cv2.cvtColor(
        current,
        cv2.COLOR_BGR2LAB
    ).astype(
        np.float32
    )

    # ========================================================
    # GLOBAL LIGHT NORMALIZATION
    # ========================================================

    baseline_l = baseline_lab[
        :,
        :,
        0
    ]

    current_l = current_lab[
        :,
        :,
        0
    ]

    baseline_median = float(
        np.median(
            baseline_l[
                valid
            ]
        )
    )

    current_median = float(
        np.median(
            current_l[
                valid
            ]
        )
    )

    illumination_shift = (
        baseline_median
        - current_median
    )

    illumination_delta = abs(
        illumination_shift
    )

    current_lab[
        :,
        :,
        0
    ] = np.clip(
        current_lab[
            :,
            :,
            0
        ]
        + illumination_shift,
        0,
        255
    )

    # ========================================================
    # MULTISCALE COLOR DIFFERENCE
    # ========================================================

    baseline_small_blur = cv2.GaussianBlur(
        baseline_lab,
        (5, 5),
        0
    )

    current_small_blur = cv2.GaussianBlur(
        current_lab,
        (5, 5),
        0
    )

    baseline_large_blur = cv2.GaussianBlur(
        baseline_lab,
        (13, 13),
        0
    )

    current_large_blur = cv2.GaussianBlur(
        current_lab,
        (13, 13),
        0
    )

    diff_small = (
        current_small_blur
        - baseline_small_blur
    )

    diff_large = (
        current_large_blur
        - baseline_large_blur
    )

    def lab_distance(diff):
        dl = diff[
            :,
            :,
            0
        ]

        da = diff[
            :,
            :,
            1
        ]

        db_channel = diff[
            :,
            :,
            2
        ]

        return np.sqrt(
            (dl * 0.18) ** 2
            + (da * 1.18) ** 2
            + (db_channel * 1.18) ** 2
        )

    color_small = lab_distance(
        diff_small
    )

    color_large = lab_distance(
        diff_large
    )

    # Da más peso a cambios consistentes a varias escalas.
    color_distance = (
        0.58 * color_small
        + 0.42 * color_large
    )

    # ========================================================
    # TEXTURE CHANGE
    # ========================================================

    gray_current = cv2.cvtColor(
        current,
        cv2.COLOR_BGR2GRAY
    )

    gray_baseline = cv2.cvtColor(
        baseline,
        cv2.COLOR_BGR2GRAY
    )

    gradient_current = calculate_gradient(
        gray_current
    )

    gradient_baseline = calculate_gradient(
        gray_baseline
    )

    texture_difference = np.abs(
        gradient_current
        - gradient_baseline
    )

    texture_difference = cv2.GaussianBlur(
        texture_difference,
        (5, 5),
        0
    )

    texture_scaled = np.clip(
        texture_difference,
        0,
        80
    )

    anomaly_score = (
        color_distance
        + 0.18 * texture_scaled
    )

    # ========================================================
    # REFLECTION / EDGE SUPPRESSION
    # ========================================================

    hsv_current = cv2.cvtColor(
        current,
        cv2.COLOR_BGR2HSV
    )

    hsv_baseline = cv2.cvtColor(
        baseline,
        cv2.COLOR_BGR2HSV
    )

    # Reflexión especular: muy brillante y poca saturación.
    reflection_mask = (
        (
            (hsv_current[:, :, 2] > 238)
            & (hsv_current[:, :, 1] < 70)
        )
        |
        (
            (hsv_baseline[:, :, 2] > 238)
            & (hsv_baseline[:, :, 1] < 70)
        )
    )

    # Bordes fuertes persistentes suelen ser paredes,
    # contornos del tanque o refracciones.
    edge_mask = (
        (gradient_current > 95)
        & (gradient_baseline > 95)
    ).astype(
        np.uint8
    ) * 255

    edge_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (7, 7)
    )

    edge_mask = cv2.dilate(
        edge_mask,
        edge_kernel,
        iterations=1
    )

    # ========================================================
    # ADAPTIVE THRESHOLD
    # ========================================================

    roi_values = anomaly_score[
        valid
    ]

    median, mad = robust_median_mad(
        roi_values
    )

    threshold = (
        median
        + 6.0 * mad
    )

    threshold = float(
        np.clip(
            threshold,
            23,
            58
        )
    )

    detection = np.zeros_like(
        valid_mask
    )

    detection[
        (
            anomaly_score
            > threshold
        )
        &
        valid
    ] = 255

    detection[
        reflection_mask
    ] = 0

    detection[
        edge_mask > 0
    ] = 0

    # ========================================================
    # MORPHOLOGY
    # ========================================================

    kernel_open = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (5, 5)
    )

    kernel_close = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (9, 9)
    )

    detection = cv2.morphologyEx(
        detection,
        cv2.MORPH_OPEN,
        kernel_open
    )

    detection = cv2.morphologyEx(
        detection,
        cv2.MORPH_CLOSE,
        kernel_close
    )

    detection = cv2.bitwise_and(
        detection,
        valid_mask
    )

    # ========================================================
    # CONNECTED COMPONENT FILTER
    # ========================================================

    contours, _ = cv2.findContours(
        detection,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    clean_mask = np.zeros_like(
        detection
    )

    minimum_area = max(
        220,
        int(
            analyzed_pixels
            * 0.0015
        )
    )

    x1, y1, x2, y2 = bounds

    for contour in contours:
        area = cv2.contourArea(
            contour
        )

        if area < minimum_area:
            continue

        bx, by, bw, bh = (
            cv2.boundingRect(
                contour
            )
        )

        if (
            bw < 9
            or bh < 9
        ):
            continue

        aspect = max(
            bw / max(
                bh,
                1
            ),
            bh / max(
                bw,
                1
            )
        )

        # Rechaza líneas finas aisladas.
        if (
            aspect > 10
            and area
            < minimum_area * 6
        ):
            continue

        # Componentes pegados al límite de la ROI suelen venir
        # de encuadre/refracción. Solo los descartamos si son
        # relativamente pequeños.
        touches_border = (
            bx <= x1 + 4
            or by <= y1 + 4
            or bx + bw >= x2 - 4
            or by + bh >= y2 - 4
        )

        if (
            touches_border
            and area
            < analyzed_pixels * 0.01
        ):
            continue

        cv2.drawContours(
            clean_mask,
            [contour],
            -1,
            255,
            cv2.FILLED
        )

    clean_mask = cv2.bitwise_and(
        clean_mask,
        valid_mask
    )

    # ========================================================
    # PERCENTAGES
    # ========================================================

    affected_pixels = int(
        np.count_nonzero(
            clean_mask
        )
    )

    raw_percentage = (
        affected_pixels
        / analyzed_pixels
        * 100
        if analyzed_pixels
        else 0.0
    )

    corrected_percentage = max(
        0.0,
        raw_percentage
        - BASELINE_NOISE_PERCENT
    )

    visual_change = (
        float(
            np.mean(
                roi_values
            )
        )
        / 255
        * 100
    )

    # ========================================================
    # IMAGE QUALITY
    # ========================================================

    gray_roi = gray_current[
        y1:y2,
        x1:x2
    ]

    brightness = float(
        np.mean(
            gray_roi
        )
    )

    contrast = float(
        np.std(
            gray_roi
        )
    )

    sharpness = float(
        cv2.Laplacian(
            gray_roi,
            cv2.CV_64F
        ).var()
    )

    alignment_component = (
        alignment_score
        if alignment_score > 0
        else 28.0
    )

    illumination_component = float(
        np.clip(
            100
            - illumination_delta * 2.2,
            0,
            100
        )
    )

    sharpness_component = float(
        np.clip(
            sharpness / 3.0,
            0,
            100
        )
    )

    quality_score = (
        alignment_component * 0.50
        + illumination_component * 0.30
        + sharpness_component * 0.20
    )

    if quality_score >= 72:
        capture_quality = "BUENA"

    elif quality_score >= MIN_RELIABLE_QUALITY:
        capture_quality = "MEDIA"

    else:
        capture_quality = "BAJA"

    reliable = (
        quality_score
        >= MIN_RELIABLE_QUALITY
    )

    # ========================================================
    # CONDITION
    # ========================================================

    if corrected_percentage < CLEAN_LIMIT:
        condition = "LIMPIO"

    elif corrected_percentage < OBSERVATION_LIMIT:
        condition = "OBSERVACION"

    elif corrected_percentage < DIRTY_LIMIT:
        condition = "SUCIO"

    else:
        condition = "CRITICO"

    return {
        "raw_percentage":
            round(
                raw_percentage,
                2
            ),

        "baseline_noise":
            BASELINE_NOISE_PERCENT,

        "corrected_percentage":
            round(
                corrected_percentage,
                2
            ),

        "visual_change":
            round(
                visual_change,
                2
            ),

        "condition":
            condition,

        "capture_quality":
            capture_quality,

        "quality_score":
            round(
                quality_score,
                1
            ),

        "reliable":
            bool(
                reliable
            ),

        "alignment_score":
            round(
                alignment_score,
                1
            ),

        "alignment_method":
            alignment_method,

        "brightness":
            round(
                brightness,
                1
            ),

        "contrast":
            round(
                contrast,
                1
            ),

        "illumination_delta":
            round(
                illumination_delta,
                1
            ),

        "threshold":
            round(
                threshold,
                1
            ),

        "affected_pixels":
            affected_pixels,

        "analyzed_pixels":
            analyzed_pixels,

        "mask":
            clean_mask,

        "aligned":
            current,

        "bounds":
            bounds,

        "roi":
            roi
    }


# ============================================================
# OVERLAY
# ============================================================

def overlay_image(
    image,
    mask,
    bounds
):
    output = image.copy()

    red = np.zeros_like(
        image
    )

    red[
        :,
        :,
        2
    ] = 255

    detected = mask > 0

    if np.any(
        detected
    ):
        blend = cv2.addWeighted(
            image,
            0.42,
            red,
            0.58,
            0
        )

        output[
            detected
        ] = blend[
            detected
        ]

    contours, _ = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE
    )

    cv2.drawContours(
        output,
        contours,
        -1,
        (
            0,
            210,
            255
        ),
        2
    )

    x1, y1, x2, y2 = bounds

    cv2.rectangle(
        output,
        (
            x1,
            y1
        ),
        (
            x2,
            y2
        ),
        (
            95,
            170,
            205
        ),
        2
    )

    return output


# ============================================================
# BASELINE
# ============================================================

@app.route(
    "/api/baseline",
    methods=["POST"]
)
def baseline_api():
    try:
        image = decode_uploaded_image()

        image = normalize_image(
            image
        )

        save_jpeg(
            image,
            BASELINE_FILE
        )

        name = new_filename(
            "reference"
        )

        save_jpeg(
            image,
            REFERENCE_DIR / name
        )

        current_tank = tank_id()

        conn = db()

        conn.execute(
            """
            UPDATE reference_images
            SET active = 0
            WHERE tank_id = ?
            """,
            (current_tank,)
        )

        cursor = conn.execute(
            """
            INSERT INTO reference_images (
                tank_id,
                filename,
                created_at,
                active
            )
            VALUES (?, ?, ?, 1)
            """,
            (
                current_tank,
                name,
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )
        )

        conn.commit()
        conn.close()

        # Una nueva referencia puede cambiar geometría/encuadre,
        # por lo que obligamos a confirmar nuevamente la ROI.
        set_setting(
            "analysis_roi",
            DEFAULT_ROI
        )

        set_setting(
            "roi_configured",
            False
        )

        return jsonify({
            "ok": True,
            "reference_id":
                cursor.lastrowid,
            "message":
                "Referencia registrada. Defina nuevamente el área de agua."
        })

    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 400


# ============================================================
# ROI
# ============================================================

@app.route(
    "/api/roi",
    methods=[
        "GET",
        "POST"
    ]
)
def roi_api():
    if request.method == "GET":
        return jsonify({
            "roi":
                current_roi(),
            "configured":
                roi_is_configured()
        })

    try:
        data = request.get_json(
            force=True
        )

        roi = {
            "x": float(
                data["x"]
            ),
            "y": float(
                data["y"]
            ),
            "w": float(
                data["w"]
            ),
            "h": float(
                data["h"]
            )
        }

        if (
            roi["x"] < 0
            or roi["y"] < 0
            or roi["w"] <= 0
            or roi["h"] <= 0
            or roi["x"]
            + roi["w"] > 1
            or roi["y"]
            + roi["h"] > 1
        ):
            raise ValueError(
                "Área inválida."
            )

        if (
            roi["w"] < 0.08
            or roi["h"] < 0.08
        ):
            raise ValueError(
                "El área seleccionada es demasiado pequeña."
            )

        set_setting(
            "analysis_roi",
            roi
        )

        set_setting(
            "roi_configured",
            True
        )

        return jsonify({
            "ok": True,
            "roi": roi
        })

    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 400


# ============================================================
# ANALYZE
# ============================================================

@app.route(
    "/foto",
    methods=["POST"]
)
@app.route(
    "/api/analyze",
    methods=["POST"]
)
def analyze_api():
    if not BASELINE_FILE.exists():
        return jsonify({
            "ok": False,
            "error":
                "Primero registre una referencia limpia."
        }), 409

    if not roi_is_configured():
        return jsonify({
            "ok": False,
            "error":
                "Primero defina el área de agua en el dashboard."
        }), 409

    try:
        image = decode_uploaded_image()

        baseline = cv2.imread(
            str(
                BASELINE_FILE
            )
        )

        if baseline is None:
            raise ValueError(
                "No fue posible abrir la referencia activa."
            )

        result = analyze(
            image,
            baseline
        )

        inspection_event_uuid = str(uuid.uuid4())

        inspection_uid = (
            "AV-"
            + datetime.now().strftime(
                "%Y%m%d-%H%M%S"
            )
            + "-"
            + uuid.uuid4().hex[
                :6
            ].upper()
        )

        image_name = new_filename(
            "inspection"
        )

        overlay_name = (
            "overlay_"
            + image_name
        )

        save_jpeg(
            result["aligned"],
            IMAGE_DIR / image_name
        )

        rendered_overlay = overlay_image(
            result["aligned"],
            result["mask"],
            result["bounds"]
        )

        save_jpeg(
            rendered_overlay,
            OVERLAY_DIR / overlay_name
        )

        timestamp = datetime.now().isoformat(
            timespec="seconds"
        )

        current_tank = tank_id()
        reference_id = active_reference_id()

        with sensor_lock:
            inspection_temperature = (
                sensor_state.get(
                    "temperature"
                )
            )

            inspection_humidity = (
                sensor_state.get(
                    "humidity"
                )
            )

            inspection_sensor_status = (
                sensor_state.get(
                    "status"
                )
            )

        reliable = (
            1
            if result["reliable"]
            else 0
        )

        conn = db()

        cursor = conn.execute(
            """
            INSERT INTO inspections (
                inspection_uid,
                tank_id,
                captured_at,
                image_filename,
                overlay_filename,

                affected_percentage,
                raw_percentage,
                baseline_noise,
                corrected_percentage,

                visual_change,
                condition,
                capture_quality,
                quality_score,
                alignment_score,

                brightness,
                contrast,
                illumination_delta,
                detection_threshold,
                affected_pixels,
                analyzed_pixels,
                roi_json,

                temperature_c,
                humidity_pct,
                sensor_status,
                reliable,
                reference_id,
                calibration_version,
                event_uuid,
                sync_status
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, 'PENDING'
            )
            """,
            (
                inspection_uid,
                current_tank,
                timestamp,
                image_name,
                overlay_name,

                result[
                    "corrected_percentage"
                ],

                result[
                    "raw_percentage"
                ],

                result[
                    "baseline_noise"
                ],

                result[
                    "corrected_percentage"
                ],

                result[
                    "visual_change"
                ],

                result[
                    "condition"
                ],

                result[
                    "capture_quality"
                ],

                result[
                    "quality_score"
                ],

                result[
                    "alignment_score"
                ],

                result[
                    "brightness"
                ],

                result[
                    "contrast"
                ],

                result[
                    "illumination_delta"
                ],

                result[
                    "threshold"
                ],

                result[
                    "affected_pixels"
                ],

                result[
                    "analyzed_pixels"
                ],

                json.dumps(
                    result["roi"]
                ),

                inspection_temperature,
                inspection_humidity,
                inspection_sensor_status,
                reliable,
                reference_id,
                CALIBRATION_VERSION,
                inspection_event_uuid
            )
        )

        inspection_id = (
            cursor.lastrowid
        )

        if not reliable:
            conn.execute(
                """
                INSERT INTO alerts (
                    tank_id,
                    inspection_id,
                    created_at,
                    severity,
                    category,
                    message
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    current_tank,
                    inspection_id,
                    timestamp,
                    "WARNING",
                    "CAPTURE_QUALITY",
                    (
                        "Inspección con calidad BAJA. "
                        "Revisar encuadre, enfoque o iluminación."
                    )
                )
            )

        elif result[
            "condition"
        ] in (
            "SUCIO",
            "CRITICO"
        ):
            conn.execute(
                """
                INSERT INTO alerts (
                    tank_id,
                    inspection_id,
                    created_at,
                    severity,
                    category,
                    message
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    current_tank,
                    inspection_id,
                    timestamp,

                    (
                        "CRITICAL"
                        if result[
                            "condition"
                        ] == "CRITICO"
                        else "WARNING"
                    ),

                    "VISUAL_CONDITION",

                    (
                        "Superficie visual afectada corregida: "
                        f"{result['corrected_percentage']}%"
                    )
                )
            )

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,

            "inspection_uid":
                inspection_uid,

            "image":
                image_name,

            "overlay":
                overlay_name,

            "analysis": {
                "raw_percentage":
                    result[
                        "raw_percentage"
                    ],

                "baseline_noise":
                    result[
                        "baseline_noise"
                    ],

                "corrected_percentage":
                    result[
                        "corrected_percentage"
                    ],

                "condition":
                    result[
                        "condition"
                    ],

                "capture_quality":
                    result[
                        "capture_quality"
                    ],

                "quality_score":
                    result[
                        "quality_score"
                    ],

                "alignment_score":
                    result[
                        "alignment_score"
                    ],

                "reliable":
                    bool(
                        reliable
                    ),

                "temperature_c":
                    inspection_temperature,

                "humidity_pct":
                    inspection_humidity
            }
        })

    except Exception as exc:
        return jsonify({
            "ok": False,
            "error": str(exc)
        }), 500


# ============================================================
# APIs
# ============================================================

@app.route("/api/status")
def status_api():
    conn = db()

    latest = conn.execute(
        """
        SELECT *
        FROM inspections
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    count = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM inspections
        """
    ).fetchone()

    local_sync_counts = pending_counts(conn)

    conn.close()

    with sensor_lock:
        sensor = dict(
            sensor_state
        )

    with uv_lock:
        uv = dict(
            uv_state
        )

    with sync_lock:
        cloud = dict(sync_state)
        cloud.update(local_sync_counts)

    return jsonify({
        "tank": {
            "code":
                TANK_CODE,
            "name":
                TANK_NAME
        },

        "sensor":
            sensor,

        "uv":
            uv,

        "cloud":
            cloud,

        "baseline_ready":
            BASELINE_FILE.exists(),

        "roi_configured":
            roi_is_configured(),

        "baseline_noise":
            BASELINE_NOISE_PERCENT,

        "inspection_count":
            count["total"],

        "inspection":
            (
                dict(
                    latest
                )
                if latest
                else None
            )
    })


@app.route("/api/history")
def history_api():
    conn = db()

    sensors = conn.execute(
        """
        SELECT
            captured_at,
            temperature_c,
            humidity_pct
        FROM sensor_readings
        ORDER BY id DESC
        LIMIT 120
        """
    ).fetchall()

    uv_rows = conn.execute(
        """
        SELECT
            captured_at,
            connected,
            status,
            raw_word_00,
            raw_word_10,
            raw_registers_json
        FROM uv_readings
        ORDER BY id DESC
        LIMIT 120
        """
    ).fetchall()

    inspections = conn.execute(
        """
        SELECT *
        FROM inspections
        ORDER BY id DESC
        LIMIT 60
        """
    ).fetchall()

    conn.close()

    return jsonify({
        "sensors": [
            dict(x)
            for x
            in reversed(
                sensors
            )
        ],

        "uv": [
            dict(x)
            for x
            in reversed(
                uv_rows
            )
        ],

        "inspections": [
            dict(x)
            for x
            in reversed(
                inspections
            )
        ]
    })


@app.route("/api/inspections")
def inspections_api():
    conn = db()

    rows = conn.execute(
        """
        SELECT *
        FROM inspections
        ORDER BY id DESC
        LIMIT 40
        """
    ).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


@app.route(
    "/api/inspection/<int:inspection_id>"
)
def inspection_detail_api(
    inspection_id
):
    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM inspections
        WHERE id = ?
        """,
        (inspection_id,)
    ).fetchone()

    conn.close()

    if row is None:
        return jsonify({
            "ok": False,
            "error":
                "Inspección no encontrada."
        }), 404

    return jsonify(
        dict(
            row
        )
    )


@app.route("/api/summary")
def summary_api():
    conn = db()

    stats = conn.execute(
        """
        SELECT
            COUNT(*) AS total,

            SUM(
                CASE
                    WHEN condition = 'LIMPIO'
                    THEN 1
                    ELSE 0
                END
            ) AS clean,

            SUM(
                CASE
                    WHEN condition = 'OBSERVACION'
                    THEN 1
                    ELSE 0
                END
            ) AS observation,

            SUM(
                CASE
                    WHEN condition = 'SUCIO'
                    THEN 1
                    ELSE 0
                END
            ) AS dirty,

            SUM(
                CASE
                    WHEN condition = 'CRITICO'
                    THEN 1
                    ELSE 0
                END
            ) AS critical,

            AVG(
                corrected_percentage
            ) AS avg_corrected,

            MAX(
                corrected_percentage
            ) AS max_corrected

        FROM inspections
        """
    ).fetchone()

    alerts = conn.execute(
        """
        SELECT *
        FROM alerts
        ORDER BY id DESC
        LIMIT 12
        """
    ).fetchall()

    conn.close()

    return jsonify({
        "stats":
            dict(
                stats
            ),

        "alerts": [
            dict(row)
            for row in alerts
        ]
    })


ALLOWED_TABLES = {
    "tanks",
    "settings",
    "sensor_readings",
    "uv_readings",
    "reference_images",
    "inspections",
    "alerts"
}


@app.route("/api/db/tables")
def db_tables_api():
    conn = db()
    rows = conn.execute(
        """
        SELECT name, type, sql
        FROM sqlite_master
        WHERE type IN ('table','index')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY type DESC, name
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/db/schema/<table_name>")
def db_schema_api(table_name):
    if table_name not in ALLOWED_TABLES:
        return jsonify({"ok": False, "error": "Tabla no permitida."}), 400

    conn = db()
    columns = conn.execute(
        f"PRAGMA table_info({table_name})"
    ).fetchall()
    foreign_keys = conn.execute(
        f"PRAGMA foreign_key_list({table_name})"
    ).fetchall()
    indexes = conn.execute(
        f"PRAGMA index_list({table_name})"
    ).fetchall()
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    conn.close()

    return jsonify({
        "table": table_name,
        "columns": [dict(row) for row in columns],
        "foreign_keys": [dict(row) for row in foreign_keys],
        "indexes": [dict(row) for row in indexes],
        "create_sql": create_sql["sql"] if create_sql else None
    })


@app.route("/api/db/rows/<table_name>")
def db_rows_api(table_name):
    if table_name not in ALLOWED_TABLES:
        return jsonify({"ok": False, "error": "Tabla no permitida."}), 400

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    conn = db()
    total = conn.execute(
        f"SELECT COUNT(*) AS total FROM {table_name}"
    ).fetchone()["total"]
    rows = conn.execute(
        f"SELECT * FROM {table_name} ORDER BY rowid DESC LIMIT ? OFFSET ?",
        (limit, offset)
    ).fetchall()
    conn.close()

    return jsonify({
        "table": table_name,
        "total": total,
        "limit": limit,
        "offset": offset,
        "rows": [dict(row) for row in rows]
    })


@app.route("/api/health")
def health():
    return jsonify({
        "service":
            "AquaVision",

        "version":
            "7.1",

        "status":
            "online",

        "baseline_noise":
            BASELINE_NOISE_PERCENT,

        "calibration":
            CALIBRATION_VERSION,

        "uv_mode":
            "raw-register-monitoring",

        "cloud_sync":
            "supabase-offline-first",

        "timestamp":
            datetime.now().isoformat(
                timespec="seconds"
            )
    })


# ============================================================
# FILE ROUTES
# ============================================================

@app.route(
    "/images/<path:name>"
)
def images(name):
    return send_from_directory(
        IMAGE_DIR,
        name
    )


@app.route(
    "/overlays/<path:name>"
)
def overlays(name):
    return send_from_directory(
        OVERLAY_DIR,
        name
    )


@app.route(
    "/reference/current"
)
def reference_current():
    if not BASELINE_FILE.exists():
        return (
            "No reference",
            404
        )

    return send_from_directory(
        REFERENCE_DIR,
        BASELINE_FILE.name
    )


# ============================================================
# DASHBOARD
# ============================================================

@app.route("/")
def dashboard():
    return r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AquaVision V7.1</title>
<style>
:root{--bg:#071018;--panel:#0d1720;--panel2:#111e29;--line:#233542;--text:#e8eef2;--muted:#8295a3;--blue:#64a9c1;--green:#60b18a;--amber:#d4a15c;--red:#d56969}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}header{height:66px;padding:0 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);background:#081119;position:sticky;top:0;z-index:10}.brand{font-weight:650}.brand small{display:block;color:var(--muted);font-size:9px;margin-top:4px;font-weight:400;letter-spacing:.08em}.online{color:var(--green);font-size:9px;letter-spacing:.1em}main{max-width:1580px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;align-items:flex-end;gap:15px;margin-bottom:14px}h1{font-size:21px;margin:0}.sub{font-size:10px;color:var(--muted);margin-top:5px}.actions{display:flex;gap:7px;flex-wrap:wrap}.btn{border:1px solid var(--line);background:var(--panel2);color:var(--text);border-radius:5px;padding:10px 13px;font-size:10px;cursor:pointer}.btn.primary{background:#15313d;border-color:#3b697b}input[type=file]{display:none}.tabs{display:flex;border-bottom:1px solid var(--line);margin-bottom:12px}.tab{padding:11px 14px;color:var(--muted);font-size:9px;letter-spacing:.08em;cursor:pointer;border-bottom:2px solid transparent}.tab.active{color:var(--text);border-bottom-color:var(--blue)}.view{display:none}.view.active{display:block}.metrics{display:grid;grid-template-columns:repeat(9,1fr);gap:9px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:6px}.card{padding:14px;min-height:94px}.label{font-size:8px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.value{font-size:22px;margin-top:10px}.unit{font-size:10px;color:var(--muted)}.green{color:var(--green)}.amber{color:var(--amber)}.red{color:var(--red)}.blue{color:var(--blue)}.workspace{display:grid;grid-template-columns:1.55fr .45fr;gap:12px;margin-top:12px}.panel-title{height:43px;padding:0 13px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);font-size:8px;color:#bac8d0;letter-spacing:.09em;text-transform:uppercase}.compare{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line)}.viewer{height:380px;background:#030608;display:flex;align-items:center;justify-content:center;position:relative}.viewer img{width:100%;height:100%;object-fit:contain}.tag{position:absolute;top:9px;left:9px;background:#071018d9;border:1px solid var(--line);padding:4px 6px;font-size:8px}.diag{padding:14px}.condition{font-size:26px;margin-bottom:13px}.row{min-height:39px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #1a2933;font-size:10px}.row span{color:var(--muted)}.note{margin-top:11px;padding:10px;border:1px solid var(--line);background:#09121a;border-radius:4px;color:var(--muted);font-size:9px;line-height:1.5}.charts{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:12px;margin-top:12px}.chartbox{height:220px;padding:10px}.chart{width:100%;height:165px}.bottom{display:grid;grid-template-columns:1.45fr .55fr;gap:12px;margin-top:12px}.tablewrap{max-height:430px;overflow:auto}table{width:100%;border-collapse:collapse;font-size:9px}th,td{padding:9px 10px;border-bottom:1px solid #1a2933;text-align:left;white-space:nowrap;vertical-align:top}th{position:sticky;top:0;background:var(--panel);color:var(--muted);font-size:8px;text-transform:uppercase}.thumb{width:60px;height:38px;object-fit:cover;border:1px solid var(--line);border-radius:3px}.historyrow{cursor:pointer}.historyrow:hover{background:var(--panel2)}.badge{border:1px solid currentColor;border-radius:3px;padding:3px 5px;font-size:8px}.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:12px}.stat{padding:10px;border:1px solid #1a2933;background:#09121a;border-radius:4px}.stat span{font-size:8px;color:var(--muted)}.stat b{display:block;font-size:18px;margin-top:4px}.events{padding:6px 13px}.event{padding:9px 0;border-bottom:1px solid #1a2933;font-size:9px}.telegrid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.rawgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:12px}.raw{padding:9px;border:1px solid #1a2933;background:#09121a;border-radius:4px}.raw span{display:block;color:var(--muted);font-size:8px}.raw b{display:block;margin-top:4px;font-size:14px}.dbgrid{display:grid;grid-template-columns:230px 1fr;gap:12px}.sidebar{padding:9px}.dbbtn{display:block;width:100%;text-align:left;margin-bottom:5px;padding:9px;border:1px solid #1a2933;background:#09121a;color:var(--text);border-radius:4px;font-size:9px}.dbbtn.active{border-color:#3f7184;background:#122631}.schema{padding:12px}pre{margin:0;padding:10px;background:#050a0e;border:1px solid #1a2933;border-radius:4px;color:#b8c7d0;font-size:9px;white-space:pre-wrap;max-height:250px;overflow:auto}.modal{display:none;position:fixed;inset:0;background:#000d;z-index:100;align-items:center;justify-content:center}.modalbox{width:min(1000px,94vw);max-height:92vh;overflow:auto;padding:15px;background:var(--panel);border:1px solid var(--line);border-radius:6px}#roiCanvas{width:100%;max-height:68vh;background:#000;display:block;touch-action:none}.modalactions{display:flex;justify-content:flex-end;gap:8px;margin-top:10px}.toast{display:none;position:fixed;right:18px;bottom:18px;z-index:120;padding:12px 14px;background:#15222c;border:1px solid var(--line);border-radius:5px;font-size:10px;max-width:390px}@media(max-width:1100px){.metrics{grid-template-columns:repeat(4,1fr)}.workspace,.bottom,.telegrid,.dbgrid,.charts{grid-template-columns:1fr}}@media(max-width:650px){main{padding:11px}.top{flex-direction:column;align-items:flex-start}.metrics{grid-template-columns:1fr 1fr}.compare{grid-template-columns:1fr}.viewer{height:270px}}
</style>
</head>
<body>
<header><div class="brand">AquaVision V7.1<small>VISUAL MONITORING · OFFLINE SQLITE · SUPABASE SYNC</small></div><div class="online">● SYSTEM ONLINE</div></header>
<main>
<div class="top"><div><h1>Tank 01</h1><div class="sub" id="telemetry">Esperando datos</div></div><div class="actions"><label class="btn" for="refInput">Registrar referencia</label><input id="refInput" type="file" accept="image/*" capture="environment"><button class="btn" id="roiBtn">Definir área de agua</button><label class="btn primary" for="inspInput">Nueva inspección</label><input id="inspInput" type="file" accept="image/*" capture="environment"></div></div>
<div class="tabs"><div class="tab active" data-view="monitor">MONITOR</div><div class="tab" data-view="telemetry">TELEMETRÍA</div><div class="tab" data-view="database">BASE DE DATOS</div></div>
<section class="view active" id="view-monitor">
<div class="metrics"><div class="card"><div class="label">Suciedad visual</div><div class="value"><span id="corrected">--</span><span class="unit"> %</span></div></div><div class="card"><div class="label">Bruto</div><div class="value"><span id="raw">--</span><span class="unit"> %</span></div></div><div class="card"><div class="label">Condición</div><div class="value" id="condition">--</div></div><div class="card"><div class="label">Temperatura</div><div class="value"><span id="temp">--</span><span class="unit"> °C</span></div></div><div class="card"><div class="label">Humedad</div><div class="value"><span id="hum">--</span><span class="unit"> %</span></div></div><div class="card"><div class="label">UV módulo</div><div class="value" id="uvstatus">--</div></div><div class="card"><div class="label">Calidad</div><div class="value" id="quality">--</div></div><div class="card"><div class="label">Alineación</div><div class="value"><span id="alignmetric">--</span><span class="unit"> %</span></div></div><div class="card"><div class="label">Cloud sync</div><div class="value" id="cloudstatus">--</div><div class="label" id="cloudpending" style="margin-top:6px">0 pendientes</div></div></div>
<div class="workspace"><div class="panel"><div class="panel-title"><span>Inspección visual</span><span id="uid">SIN INSPECCIÓN</span></div><div class="compare"><div class="viewer"><span class="tag">IMAGEN ALINEADA</span><img id="image" style="display:none"></div><div class="viewer"><span class="tag">MAPA DE DETECCIÓN</span><img id="overlay" style="display:none"></div></div></div><div class="panel"><div class="panel-title">Diagnóstico</div><div class="diag"><div class="condition" id="digital">NO DATA</div><div class="row"><span>Bruto</span><b id="rawD">--</b></div><div class="row"><span>Ruido base</span><b>1.96 %</b></div><div class="row"><span>Corregido</span><b id="corrD">--</b></div><div class="row"><span>Cambio visual</span><b id="change">--</b></div><div class="row"><span>Alineación</span><b id="align">--</b></div><div class="row"><span>Calidad</span><b id="qualityD">--</b></div><div class="row"><span>Umbral</span><b id="threshold">--</b></div><div class="note" id="note">El porcentaje representa superficie visual anómala respecto a la referencia limpia. No es una medición química.</div></div></div></div>
<div class="charts"><div class="panel"><div class="panel-title">Tendencia de suciedad</div><div class="chartbox"><canvas class="chart" id="dirtChart"></canvas></div></div><div class="panel"><div class="panel-title">Temperatura</div><div class="chartbox"><canvas class="chart" id="tempChart"></canvas></div></div><div class="panel"><div class="panel-title">Humedad</div><div class="chartbox"><canvas class="chart" id="humChart"></canvas></div></div></div>
<div class="bottom"><div class="panel"><div class="panel-title"><span>Historial de inspecciones</span><span id="histCount">0</span></div><div class="tablewrap"><table><thead><tr><th>Foto</th><th>ID</th><th>Fecha</th><th>Corregido</th><th>Estado</th><th>Calidad</th><th>T/H</th><th>SYNC</th></tr></thead><tbody id="history"></tbody></table></div></div><div><div class="panel"><div class="panel-title">Resumen</div><div class="stats"><div class="stat"><span>Total</span><b id="stTotal">0</b></div><div class="stat"><span>Limpias</span><b id="stClean">0</b></div><div class="stat"><span>Observación</span><b id="stObs">0</b></div><div class="stat"><span>Alertas</span><b id="stAlerts">0</b></div></div></div><div class="panel" style="margin-top:12px"><div class="panel-title">Eventos</div><div class="events" id="events">Sin eventos</div></div></div></div>
</section>
<section class="view" id="view-telemetry"><div class="telegrid"><div class="panel"><div class="panel-title">DHT11 · GPIO27</div><div class="tablewrap"><table><thead><tr><th>Fecha</th><th>Temp °C</th><th>Humedad %</th></tr></thead><tbody id="sensorRows"></tbody></table></div></div><div class="panel"><div class="panel-title">UV · I2C 0x55 · RAW</div><div class="note" style="margin:12px">El mapa de registros exacto del módulo UV no está verificado. Se muestran y guardan lecturas RAW seguras; no se etiquetan como irradiancia UVA calibrada.</div><div class="rawgrid" id="rawRegs"></div><div class="diag"><div class="row"><span>WORD 0x00</span><b id="w00">--</b></div><div class="row"><span>WORD 0x10</span><b id="w10">--</b></div><div class="row"><span>Estado</span><b id="uvrawstate">--</b></div></div></div></div><div class="panel" style="margin-top:12px"><div class="panel-title">Histórico UV RAW</div><div class="tablewrap"><table><thead><tr><th>Fecha</th><th>Conectado</th><th>Estado</th><th>WORD00</th><th>WORD10</th><th>Registros</th></tr></thead><tbody id="uvRows"></tbody></table></div></div></section>
<section class="view" id="view-database"><div class="dbgrid"><div class="panel"><div class="panel-title">Tablas</div><div class="sidebar" id="dbTables">Cargando...</div></div><div><div class="panel"><div class="panel-title"><span id="dbName">Seleccione una tabla</span><span id="dbCount"></span></div><div class="tablewrap"><table><thead id="dbHead"></thead><tbody id="dbBody"></tbody></table></div></div><div class="panel" style="margin-top:12px"><div class="panel-title">Estructura SQLite</div><div class="schema"><table><thead><tr><th>CID</th><th>Nombre</th><th>Tipo</th><th>NOT NULL</th><th>Default</th><th>PK</th></tr></thead><tbody id="schemaCols"></tbody></table><div class="note" id="fks">Foreign keys</div><pre id="createSql">Seleccione una tabla</pre></div></div></div></div></section>
</main>
<div class="modal" id="roiModal"><div class="modalbox"><div style="font-size:11px;font-weight:600">Definir área útil del agua</div><div class="note">Seleccione únicamente el interior del agua. Excluya bordes, paredes transparentes, piso, cables y objetos externos.</div><canvas id="roiCanvas"></canvas><div class="modalactions"><button class="btn" id="cancelRoi">Cancelar</button><button class="btn primary" id="saveRoi">Guardar área</button></div></div></div><div class="toast" id="toast"></div>
<script>
const $=id=>document.getElementById(id);const n=(v,d=0)=>Number.isFinite(Number(v))?Number(v):d;const esc=v=>String(v??"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");function cls(s){return s==="LIMPIO"?"green":s==="OBSERVACION"?"amber":"red"}function toast(m){$("toast").textContent=m;$("toast").style.display="block";clearTimeout(window.tt);window.tt=setTimeout(()=>$("toast").style.display="none",4000)}
document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>{document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));t.classList.add("active");$("view-"+t.dataset.view).classList.add("active");if(t.dataset.view==="database")loadDbTables()}));
function fileToJpeg(file){return new Promise((resolve,reject)=>{const u=URL.createObjectURL(file),im=new Image();im.onload=()=>{let w=im.naturalWidth,h=im.naturalHeight;if(w>1800){const s=1800/w;w=Math.round(w*s);h=Math.round(h*s)}const c=document.createElement("canvas");c.width=w;c.height=h;c.getContext("2d").drawImage(im,0,0,w,h);c.toBlob(b=>{URL.revokeObjectURL(u);b?resolve(b):reject(new Error("No se pudo convertir la imagen"))},"image/jpeg",.93)};im.onerror=()=>reject(new Error("Imagen no compatible"));im.src=u})}
async function upload(file,url){const b=await fileToJpeg(file),f=new FormData();f.append("image",b,"aquavision.jpg");const r=await fetch(url,{method:"POST",body:f}),d=await r.json();if(!r.ok)throw new Error(d.error||"Error");return d}
$("refInput").addEventListener("change",async e=>{const f=e.target.files[0];if(!f)return;try{toast("Registrando referencia...");const d=await upload(f,"/api/baseline");toast(d.message||"Referencia registrada");await refreshAll()}catch(x){toast(x.message)}e.target.value=""});$("inspInput").addEventListener("change",async e=>{const f=e.target.files[0];if(!f)return;try{toast("Procesando inspección...");const d=await upload(f,"/api/analyze");toast(`${d.inspection_uid} · ${d.analysis.corrected_percentage}%`);await refreshAll()}catch(x){toast(x.message)}e.target.value=""});
const rc=$("roiCanvas"),ctx=rc.getContext("2d"),rm=$("roiModal");let rim=null,start=null,sel=null;function pos(e){const r=rc.getBoundingClientRect(),p=e.touches?e.touches[0]:e;return{x:(p.clientX-r.left)*rc.width/r.width,y:(p.clientY-r.top)*rc.height/r.height}}function drawRoi(){if(!rim)return;ctx.clearRect(0,0,rc.width,rc.height);ctx.drawImage(rim,0,0,rc.width,rc.height);if(sel){ctx.fillStyle="rgba(70,160,195,.18)";ctx.strokeStyle="#6ec1de";ctx.lineWidth=4;ctx.fillRect(sel.x,sel.y,sel.w,sel.h);ctx.strokeRect(sel.x,sel.y,sel.w,sel.h)}}$("roiBtn").addEventListener("click",()=>{const im=new Image();im.onload=()=>{rim=im;rc.width=im.naturalWidth;rc.height=im.naturalHeight;sel=null;drawRoi();rm.style.display="flex"};im.onerror=()=>toast("Primero registre una referencia");im.src="/reference/current?t="+Date.now()});function begin(e){e.preventDefault();start=pos(e);sel={x:start.x,y:start.y,w:0,h:0}}function move(e){if(!start)return;e.preventDefault();const p=pos(e);sel={x:Math.min(start.x,p.x),y:Math.min(start.y,p.y),w:Math.abs(p.x-start.x),h:Math.abs(p.y-start.y)};drawRoi()}function end(e){if(e)e.preventDefault();start=null}rc.addEventListener("mousedown",begin);rc.addEventListener("mousemove",move);window.addEventListener("mouseup",end);rc.addEventListener("touchstart",begin,{passive:false});rc.addEventListener("touchmove",move,{passive:false});rc.addEventListener("touchend",end,{passive:false});$("cancelRoi").addEventListener("click",()=>rm.style.display="none");$("saveRoi").addEventListener("click",async()=>{if(!sel||sel.w<20||sel.h<20)return toast("Seleccione un área válida");const o={x:sel.x/rc.width,y:sel.y/rc.height,w:sel.w/rc.width,h:sel.h/rc.height};const r=await fetch("/api/roi",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(o)}),d=await r.json();if(!r.ok)return toast(d.error);rm.style.display="none";toast("Área guardada");refreshAll()});
function chart(id,vals,unit){const c=$(id),w=c.clientWidth||300,h=165,dpr=devicePixelRatio||1;c.width=w*dpr;c.height=h*dpr;const g=c.getContext("2d");g.setTransform(dpr,0,0,dpr,0,0);g.clearRect(0,0,w,h);const a=vals.map(Number),v=a.filter(Number.isFinite);if(!v.length){g.fillStyle="#607283";g.font="10px sans-serif";g.fillText("Sin datos",8,18);return}let mn=Math.min(...v),mx=Math.max(...v);if(mn===mx){mn-=1;mx+=1}const L=40,R=8,T=12,B=20;g.strokeStyle="#223542";for(let i=0;i<4;i++){const y=T+(h-T-B)*i/3;g.beginPath();g.moveTo(L,y);g.lineTo(w-R,y);g.stroke()}g.strokeStyle="#64a9c1";g.lineWidth=2;g.beginPath();let started=false;a.forEach((val,i)=>{if(!Number.isFinite(val))return;const x=L+(w-L-R)*i/Math.max(a.length-1,1),y=h-B-(val-mn)/(mx-mn)*(h-T-B);if(!started){g.moveTo(x,y);started=true}else g.lineTo(x,y)});g.stroke();g.fillStyle="#8295a3";g.font="8px sans-serif";g.fillText(mx.toFixed(1)+unit,2,10);g.fillText(mn.toFixed(1)+unit,2,h-3)}
function renderInspection(x){if(!x)return;const raw=n(x.raw_percentage??x.affected_percentage),corr=n(x.corrected_percentage??x.affected_percentage),c=cls(x.condition);$("raw").textContent=raw.toFixed(2);$("corrected").textContent=corr.toFixed(2);$("rawD").textContent=raw.toFixed(2)+" %";$("corrD").textContent=corr.toFixed(2)+" %";$("condition").textContent=x.condition;$("condition").className="value "+c;$("digital").textContent=x.condition;$("digital").className="condition "+c;$("quality").textContent=x.capture_quality||"--";$("alignmetric").textContent=n(x.alignment_score).toFixed(1);$("change").textContent=n(x.visual_change).toFixed(2)+" %";$("align").textContent=n(x.alignment_score).toFixed(1)+" %";$("qualityD").textContent=(x.capture_quality||"--")+" · "+n(x.quality_score).toFixed(0)+"/100";$("threshold").textContent=n(x.detection_threshold).toFixed(1);$("uid").textContent=x.inspection_uid||"--";if(x.image_filename){$("image").src="/images/"+x.image_filename+"?t="+Date.now();$("image").style.display="block"}if(x.overlay_filename){$("overlay").src="/overlays/"+x.overlay_filename+"?t="+Date.now();$("overlay").style.display="block"}}
async function loadInspection(id){const r=await fetch("/api/inspection/"+id);if(r.ok)renderInspection(await r.json())}
async function refreshStatus(){const d=await (await fetch("/api/status")).json(),s=d.sensor||{},u=d.uv||{};$("temp").textContent=s.temperature==null?"--":n(s.temperature).toFixed(1);$("hum").textContent=s.humidity==null?"--":n(s.humidity).toFixed(1);$("uvstatus").textContent=u.connected?"RAW OK":"OFF";$("uvstatus").className="value "+(u.connected?"blue":"red");let t=s.timestamp?"Última telemetría: "+s.timestamp.replace("T"," "):"Esperando datos";if(!d.baseline_ready)t+=" · REFERENCIA PENDIENTE";else if(!d.roi_configured)t+=" · DEFINIR ÁREA DE AGUA";$("telemetry").textContent=t;renderInspection(d.inspection);$("rawRegs").innerHTML=Object.entries(u.raw_registers||{}).map(([k,v])=>`<div class="raw"><span>${esc(k)}</span><b>0x${Number(v).toString(16).padStart(2,"0").toUpperCase()}</b></div>`).join("");$("w00").textContent=u.raw_word_00==null?"--":"0x"+Number(u.raw_word_00).toString(16).padStart(4,"0").toUpperCase();$("w10").textContent=u.raw_word_10==null?"--":"0x"+Number(u.raw_word_10).toString(16).padStart(4,"0").toUpperCase();$("uvrawstate").textContent=u.status||"--";const c=d.cloud||{},pending=n(c.pending_sensor)+n(c.pending_uv)+n(c.pending_inspections);$("cloudstatus").textContent=c.online?"SYNC":"LOCAL";$("cloudstatus").className="value "+(c.online?"green":"amber");$("cloudpending").textContent=pending+" pendientes"}
async function refreshHistory(){const d=await (await fetch("/api/history")).json(),s=d.sensors||[],i=d.inspections||[],u=d.uv||[];chart("dirtChart",i.map(x=>x.corrected_percentage??x.affected_percentage),"%");chart("tempChart",s.map(x=>x.temperature_c),"°C");chart("humChart",s.map(x=>x.humidity_pct),"%");$("sensorRows").innerHTML=s.slice(-60).reverse().map(x=>`<tr><td>${esc((x.captured_at||"").replace("T"," "))}</td><td>${n(x.temperature_c).toFixed(1)}</td><td>${n(x.humidity_pct).toFixed(1)}</td></tr>`).join("")||'<tr><td colspan="3">Sin datos</td></tr>';$("uvRows").innerHTML=u.slice(-60).reverse().map(x=>`<tr><td>${esc((x.captured_at||"").replace("T"," "))}</td><td>${Number(x.connected)?"SI":"NO"}</td><td>${esc(x.status)}</td><td>${x.raw_word_00??"--"}</td><td>${x.raw_word_10??"--"}</td><td>${esc(x.raw_registers_json||"")}</td></tr>`).join("")||'<tr><td colspan="6">Sin datos</td></tr>'}
async function refreshTable(){const rows=await (await fetch("/api/inspections")).json();$("histCount").textContent=rows.length+" registros";$("history").innerHTML=rows.map(x=>{const c=n(x.corrected_percentage??x.affected_percentage),cl=cls(x.condition),env=x.temperature_c==null?"--":n(x.temperature_c).toFixed(1)+"° / "+n(x.humidity_pct).toFixed(0)+"%";return `<tr class="historyrow" data-id="${x.id}"><td>${x.image_filename?`<img class="thumb" src="/images/${x.image_filename}">`:"--"}</td><td>${esc(x.inspection_uid||x.id)}</td><td>${esc((x.captured_at||"").replace("T"," "))}</td><td><b>${c.toFixed(2)} %</b></td><td><span class="badge ${cl}">${esc(x.condition)}</span></td><td>${esc(x.capture_quality||"--")}</td><td>${env}</td><td><span class="badge ${x.sync_status==="SYNCED"?"green":(x.sync_status==="ERROR"?"red":"amber")}">${esc(x.sync_status||"PENDING")}</span></td></tr>`}).join("")||'<tr><td colspan="8">Sin inspecciones</td></tr>';document.querySelectorAll(".historyrow").forEach(r=>r.addEventListener("click",()=>loadInspection(r.dataset.id)))}
async function refreshSummary(){const d=await (await fetch("/api/summary")).json(),s=d.stats||{},a=d.alerts||[];$("stTotal").textContent=s.total||0;$("stClean").textContent=s.clean||0;$("stObs").textContent=s.observation||0;$("stAlerts").textContent=a.length;$("events").innerHTML=a.map(x=>`<div class="event">${esc((x.created_at||"").replace("T"," "))} · ${esc(x.severity)}<br>${esc(x.message)}</div>`).join("")||"Sin eventos"}
let dbCurrent=null;async function loadDbTables(){const rows=await (await fetch("/api/db/tables")).json(),tables=rows.filter(x=>x.type==="table");$("dbTables").innerHTML=tables.map(x=>`<button class="dbbtn" data-table="${esc(x.name)}">${esc(x.name)}</button>`).join("");document.querySelectorAll(".dbbtn").forEach(b=>b.addEventListener("click",()=>{document.querySelectorAll(".dbbtn").forEach(x=>x.classList.remove("active"));b.classList.add("active");loadDb(b.dataset.table)}));if(!dbCurrent&&tables.length){const p=tables.find(x=>x.name==="inspections")||tables[0];document.querySelector(`[data-table="${p.name}"]`).click()}}
async function loadDb(name){dbCurrent=name;const [rr,sr]=await Promise.all([fetch("/api/db/rows/"+encodeURIComponent(name)+"?limit=50"),fetch("/api/db/schema/"+encodeURIComponent(name))]),rd=await rr.json(),sd=await sr.json(),cols=sd.columns||[],rows=rd.rows||[];$("dbName").textContent=name;$("dbCount").textContent=rd.total+" filas";$("dbHead").innerHTML="<tr>"+cols.map(c=>`<th>${esc(c.name)}</th>`).join("")+"</tr>";$("dbBody").innerHTML=rows.map(r=>"<tr>"+cols.map(c=>`<td>${esc(r[c.name]===null?"NULL":String(r[c.name]).slice(0,180))}</td>`).join("")+"</tr>").join("")||`<tr><td colspan="${Math.max(cols.length,1)}">Sin filas</td></tr>`;$("schemaCols").innerHTML=cols.map(c=>`<tr><td>${c.cid}</td><td>${esc(c.name)}</td><td>${esc(c.type)}</td><td>${c.notnull}</td><td>${esc(c.dflt_value??"")}</td><td>${c.pk}</td></tr>`).join("");const f=sd.foreign_keys||[];$("fks").innerHTML=f.length?f.map(k=>`${esc(k.from)} → ${esc(k.table)}.${esc(k.to)}`).join("<br>"):"Sin foreign keys";$("createSql").textContent=sd.create_sql||"Sin CREATE TABLE"}
async function refreshAll(){try{await Promise.all([refreshStatus(),refreshHistory(),refreshTable(),refreshSummary()])}catch(e){console.error(e)}}refreshAll();setInterval(refreshAll,5000);window.addEventListener("resize",refreshHistory);
</script>
</body>
</html>
"""


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    init_database()

    thread = threading.Thread(
        target=sensor_loop,
        daemon=True,
        name="dht11-monitor"
    )

    thread.start()

    uv_thread = threading.Thread(
        target=uv_loop,
        daemon=True,
        name="uv-raw-monitor"
    )

    uv_thread.start()

    sync_thread = threading.Thread(
        target=sync_loop,
        daemon=True,
        name="supabase-sync"
    )

    sync_thread.start()

    app.run(
        host="0.0.0.0",
        port=5000,
        threaded=True,
        debug=False
    )
