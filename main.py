import os
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import sleep, time

import requests
from dotenv import load_dotenv
from gpiozero import Button, Buzzer

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "sensors": {"S1": False, "S2": False, "S3": False, "S4": False},
    "entry_counter": 0,
    "exit_counter": 0,
}

# ── Hardware ──────────────────────────────────────────────────────────────────
SENSORS_HW = {
    "S1": Button(21, pull_up=True),
    "S2": Button(20, pull_up=True),
    "S3": Button(16, pull_up=True),
    "S4": Button(12, pull_up=True),
}

BUZZER = Buzzer(26)

# ── Config (desde .env) ───────────────────────────────────────────────────────
def env_str(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name}={raw!r} no es un número válido") from None


def env_sensor_set(name: str, default: str) -> set[str]:
    sensors = {s.strip().upper() for s in env_str(name, default).split(",") if s.strip()}
    unknown = sensors - SENSORS_HW.keys()
    if not sensors or unknown:
        raise ValueError(
            f"{name} inválido: {sorted(unknown) or 'vacío'} "
            f"(sensores válidos: {', '.join(SENSORS_HW)})"
        )
    return sensors


SALIDA_SIDE = env_sensor_set("SALIDA_SIDE", "S2")
INGRESO_SIDE = env_sensor_set("INGRESO_SIDE", "S1,S3,S4")
if SALIDA_SIDE & INGRESO_SIDE:
    raise ValueError(f"Un sensor no puede estar en ambos lados: {sorted(SALIDA_SIDE & INGRESO_SIDE)}")

EVENT_BEEP_DURATION = env_float("EVENT_BEEP_DURATION", 0.2)    # pitido al confirmar ingreso
TIME_COOLDOWN = env_float("TIME_COOLDOWN", 0.2)                # entre eventos de cruce

LINGER_THRESHOLD = env_float("LINGER_THRESHOLD", 1.5)          # segundos antes de iniciar alarma
LINGER_BEEP_DURATION = env_float("LINGER_BEEP_DURATION", 0.1)  # duración de cada pitido de alarma
LINGER_BEEP_PERIOD = env_float("LINGER_BEEP_PERIOD", 0.4)      # periodo entre pitidos de alarma

# IP de la Raspberry Pi donde corre simtra-bus-manager (no localhost en producción)
SIMTRA_BACKEND_URL = env_str("SIMTRA_BACKEND_URL", "http://localhost:8000").rstrip("/")
API_URL = f"{SIMTRA_BACKEND_URL}/api/passenger"
HTTP_TIMEOUT = env_float("HTTP_TIMEOUT", 3.0)

DIRECTION_ENTRY = env_str("DIRECTION_ENTRY", "ENTRY")
DIRECTION_EXIT = env_str("DIRECTION_EXIT", "EXIT")
DOOR = env_str("DOOR", "FRONT")

# Cola local de eventos (SQLite)
_db_path = Path(env_str("LOCAL_DB_PATH", "passenger_events.sqlite3"))
LOCAL_DB_PATH = _db_path if _db_path.is_absolute() else BASE_DIR / _db_path
PENDING_SYNC_INTERVAL = env_float("PENDING_SYNC_INTERVAL", 5.0)  # segundos entre reintentos

# ── Runtime ───────────────────────────────────────────────────────────────────
first_activation: dict[str, float] = {}
event_counted = False
last_event_time = 0.0
buzzer_off_at = 0.0
any_active_since: float | None = None
last_linger_beep = 0.0

# ── Logging ───────────────────────────────────────────────────────────────────
_log_counter = 0
_log_lock = threading.Lock()


def log(message: str) -> None:
    """Log enumerado (para eventos normales: ingresos, salidas, HTTP, sistema, etc.)."""
    global _log_counter
    with _log_lock:
        _log_counter += 1
        n = _log_counter
    print(f"[{n}] {message}")


def log_alarm(message: str) -> None:
    """Log de alarma (sensores obstruidos). Nunca se enumera."""
    print(f"[ALARMA] {message}")


# ── Buzzer ────────────────────────────────────────────────────────────────────
def trigger_buzzer(now: float, duration: float) -> None:
    global buzzer_off_at
    BUZZER.on()
    buzzer_off_at = now + duration


def update_buzzer(now: float) -> None:
    global buzzer_off_at
    if buzzer_off_at and now >= buzzer_off_at:
        BUZZER.off()
        buzzer_off_at = 0.0


def buzzer_is_active() -> bool:
    return buzzer_off_at != 0.0


# ── Cola local + envío al backend ─────────────────────────────────────────────
# El loop de sensores solo encola el evento en memoria (no bloquea). Un único
# hilo de sincronización es dueño de la conexión SQLite: guarda el evento,
# intenta el POST y, si falla, lo deja con uploaded=0 para reintentarlo luego.
#
# Cada cruce lleva un event_id (UUID) y el timestamp del cruce, generados UNA
# vez al detectarlo y guardados en SQLite: todos los reintentos envían los
# mismos valores. simtra-bus-manager deduplica por event_id (un reintento de un
# evento que ya guardó responde 2xx con el registro existente) y usa el
# timestamp para ubicar el cruce en su GPS histórico.
_new_events: queue.Queue = queue.Queue()
_sync_stop = threading.Event()
_sync_wake = threading.Event()
_sync_thread: threading.Thread | None = None

# Columnas agregadas después de la primera versión de la tabla. SQLite solo
# admite ADD COLUMN; las filas viejas se completan en migrate_local_db.
_ADDED_COLUMNS = {
    "event_id": "TEXT",
    "timestamp": "TEXT",
    "rejected": "INTEGER NOT NULL DEFAULT 0",
}


def migrate_local_db(conn: sqlite3.Connection) -> None:
    """Agrega columnas faltantes a una base vieja sin borrar datos."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(passenger_events)")}
    for name, definition in _ADDED_COLUMNS.items():
        if name not in present:
            conn.execute(f"ALTER TABLE passenger_events ADD COLUMN {name} {definition}")
            log(f"[LOCAL] Migración: columna passenger_events.{name} agregada")

    # Filas anteriores a la migración: created_at ya era la hora del cruce.
    conn.execute("UPDATE passenger_events SET timestamp = created_at WHERE timestamp IS NULL")
    for (row_id,) in conn.execute(
            "SELECT id FROM passenger_events WHERE event_id IS NULL").fetchall():
        conn.execute("UPDATE passenger_events SET event_id = ? WHERE id = ?",
                     (str(uuid.uuid4()), row_id))

    conn.execute("DROP INDEX IF EXISTS idx_passenger_events_pending")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_passenger_events_event_id "
        "ON passenger_events (event_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_passenger_events_queue "
        "ON passenger_events (uploaded, rejected, id)"
    )
    conn.commit()


def open_local_db(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or LOCAL_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS passenger_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT,
            timestamp   TEXT,
            direction   TEXT    NOT NULL,
            door        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            uploaded    INTEGER NOT NULL DEFAULT 0,
            uploaded_at TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT,
            rejected    INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    migrate_local_db(conn)
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# Resultados de un intento de envío
SEND_OK = "ok"                # 2xx: guardado (o ya existía con ese event_id)
SEND_CONFLICT = "conflict"    # 409: event_id ya usado con otros datos; no se reintenta
SEND_HTTP_ERROR = "http"      # otro código HTTP: se reintenta más tarde
SEND_NETWORK_ERROR = "net"    # timeout / red / backend caído: se reintenta más tarde


def passenger_payload(event_id: str, timestamp: str, direction: str, door: str) -> dict:
    """Cuerpo de PassengerCreate (simtra-bus-manager). Sin latitude/longitude:
    la ubicación la decide siempre el backend."""
    return {"event_id": event_id, "timestamp": timestamp, "direction": direction, "door": door}


def post_passenger(payload: dict) -> tuple[str, str | None]:
    """Hace el POST. Devuelve (resultado, motivo del fallo o None)."""
    try:
        resp = requests.post(API_URL, json=payload, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        return SEND_NETWORK_ERROR, f"{type(e).__name__}: {e}"
    if resp.ok:
        return SEND_OK, None
    error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    if resp.status_code == 409:
        return SEND_CONFLICT, error
    return SEND_HTTP_ERROR, error


def _mark_attempt(conn: sqlite3.Connection, row_id: int, result: str, error: str | None) -> None:
    if result == SEND_OK:
        conn.execute(
            "UPDATE passenger_events SET uploaded = 1, uploaded_at = ?, "
            "attempts = attempts + 1, last_error = NULL WHERE id = ?",
            (_now_iso(), row_id),
        )
    else:
        conn.execute(
            "UPDATE passenger_events SET attempts = attempts + 1, last_error = ?, "
            "rejected = ? WHERE id = ?",
            (error, int(result == SEND_CONFLICT), row_id),
        )
    conn.commit()


def _store_new_events(conn: sqlite3.Connection) -> list[int]:
    """Pasa los eventos encolados en memoria a SQLite (uploaded=0)."""
    ids = []
    while True:
        try:
            event_id, timestamp, direction, door = _new_events.get_nowait()
        except queue.Empty:
            return ids
        cur = conn.execute(
            "INSERT INTO passenger_events (event_id, timestamp, direction, door, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, timestamp, direction, door, _now_iso()),
        )
        conn.commit()
        ids.append(cur.lastrowid)


def sync_pending(conn: sqlite3.Connection, new_ids: list[int]) -> None:
    """Sube en orden (más antiguo primero) los eventos con uploaded=0 no rechazados."""
    pending = conn.execute(
        "SELECT id, event_id, timestamp, direction, door FROM passenger_events "
        "WHERE uploaded = 0 AND rejected = 0 ORDER BY id"
    ).fetchall()

    for row_id, event_id, timestamp, direction, door in pending:
        if _sync_stop.is_set():
            return
        is_new = row_id in new_ids
        label = f"#{row_id} ({direction}, {event_id}, {timestamp})"
        if not is_new:
            log(f"[SYNC] Reintentando evento pendiente {label}")

        result, error = post_passenger(passenger_payload(event_id, timestamp, direction, door))
        _mark_attempt(conn, row_id, result, error)

        if result == SEND_OK:
            if is_new:
                log(f"[HTTP OK] Evento {label} enviado al backend")
            else:
                log(f"[SYNC] Evento pendiente {label} -> uploaded=true")
            continue

        if result == SEND_CONFLICT:
            # El backend ya tiene ese event_id con otros datos. Reintentar no
            # lo arregla: queda en la base local marcado rejected=1 para
            # revisión manual, y la cola sigue con los demás.
            log(f"[HTTP 409] Evento {label} rechazado: el event_id ya existe en el "
                f"backend con datos distintos. No se reintenta. {error}")
            continue

        log(f"[HTTP ERROR] Evento {label} no enviado: {error}")
        if is_new:
            log(f"[LOCAL] Evento #{row_id} guardado localmente como pendiente (uploaded=false)")
        if result == SEND_HTTP_ERROR:
            continue   # el backend responde: se prueba con el siguiente

        # Backend caído o inalcanzable: se corta la tanda y se reintenta en el
        # próximo ciclo, para no pagar un timeout por cada evento pendiente.
        for other_id in new_ids:
            if other_id > row_id:
                log(f"[LOCAL] Evento #{other_id} guardado localmente como pendiente (uploaded=false)")
        return


def sync_loop() -> None:
    try:
        conn = open_local_db()
    except sqlite3.Error as e:
        log(f"[LOCAL ERROR] No se pudo abrir la base local {LOCAL_DB_PATH}: {e}")
        return

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM passenger_events WHERE uploaded = 0 AND rejected = 0"
    ).fetchone()[0]
    log(f"[SYNC] Base local {LOCAL_DB_PATH} ({pending_count} eventos pendientes). Backend: {API_URL}")

    try:
        while not _sync_stop.is_set():
            # Despierta apenas llega un evento nuevo, o cada PENDING_SYNC_INTERVAL
            _sync_wake.wait(PENDING_SYNC_INTERVAL)
            _sync_wake.clear()
            if _sync_stop.is_set():
                break
            try:
                new_ids = _store_new_events(conn)
                sync_pending(conn, new_ids)
            except sqlite3.Error as e:
                log(f"[LOCAL ERROR] {e}")
                sleep(PENDING_SYNC_INTERVAL)
    finally:
        # Al apagar, lo que quedó en memoria se persiste sin intentar enviarlo
        try:
            for row_id in _store_new_events(conn):
                log(f"[LOCAL] Evento #{row_id} guardado localmente como pendiente (uploaded=false)")
        finally:
            conn.close()


def start_sync_thread() -> None:
    global _sync_thread
    _sync_thread = threading.Thread(target=sync_loop, name="passenger-sync", daemon=True)
    _sync_thread.start()


def stop_sync_thread() -> None:
    _sync_stop.set()
    _sync_wake.set()
    if _sync_thread is not None:
        _sync_thread.join(timeout=HTTP_TIMEOUT + 2)


def send_passenger_event(direction: str) -> None:
    """Registra el cruce (event_id + hora del cruce) y lo encola para el hilo
    de sincronización. No bloquea el loop de sensores."""
    _new_events.put((str(uuid.uuid4()), _now_iso(), direction, DOOR))
    _sync_wake.set()


# ── Sensor loop ───────────────────────────────────────────────────────────────
def sensor_loop():
    global event_counted, last_event_time
    global any_active_since, last_linger_beep

    while True:
        current_time = time()
        update_buzzer(current_time)

        raw = {name: btn.is_pressed for name, btn in SENSORS_HW.items()}
        active_count = sum(raw.values())

        for name, active in raw.items():
            if active and name not in first_activation:
                first_activation[name] = current_time

        # Marca el inicio de actividad sostenida con más de 2 sensores obstruidos a la vez
        if active_count > 2 and any_active_since is None:
            any_active_since = current_time
        elif active_count <= 2:
            any_active_since = None
            last_linger_beep = 0.0

        if active_count == 0:
            first_activation.clear()
            event_counted = False
            state["sensors"] = raw
            sleep(0.05)
            continue

        # Alarma de permanencia: más de 2 sensores obstruidos a la vez
        if (any_active_since is not None
                and current_time - any_active_since >= LINGER_THRESHOLD
                and current_time - last_linger_beep >= LINGER_BEEP_PERIOD
                and not buzzer_is_active()):
            trigger_buzzer(current_time, LINGER_BEEP_DURATION)
            last_linger_beep = current_time

            sensores_activos = [s for s, active in raw.items() if active]
            tiempo_obstruido = current_time - any_active_since
            log_alarm(
                f"Sensores obstruidos: {', '.join(sensores_activos)} "
                f"(bloqueados por {tiempo_obstruido:.1f}s)"
            )

        if current_time - last_event_time < TIME_COOLDOWN:
            state["sensors"] = raw
            sleep(0.01)
            continue

        if active_count >= 3 and not event_counted:
            t_ing = min(
                (first_activation[s] for s in INGRESO_SIDE if s in first_activation),
                default=float("inf"),
            )
            t_sal = min(
                (first_activation[s] for s in SALIDA_SIDE if s in first_activation),
                default=float("inf"),
            )

            if t_ing != float("inf") or t_sal != float("inf"):
                if t_ing <= t_sal:
                    state["entry_counter"] += 1
                    evento = "INGRESO"
                    send_passenger_event(DIRECTION_ENTRY)
                else:
                    state["exit_counter"] += 1
                    evento = "SALIDA"
                    send_passenger_event(DIRECTION_EXIT)

                trigger_buzzer(current_time, EVENT_BEEP_DURATION)
                last_event_time = current_time
                event_counted = True

                log(f"[{evento}] Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']}")

        state["sensors"] = raw
        sleep(0.05)


# ── Shutdown ──────────────────────────────────────────────────────────────────
def shutdown():
    """Apaga el buzzer y libera los GPIO."""
    BUZZER.off()
    BUZZER.close()
    for btn in SENSORS_HW.values():
        btn.close()


if __name__ == "__main__":
    log("Sensor loop iniciado. Ctrl+C para salir.")
    start_sync_thread()
    try:
        sensor_loop()
    except KeyboardInterrupt:
        log("Interrumpido por el usuario.")
    except Exception as e:
        log(f"Error inesperado: {e}")
    finally:
        stop_sync_thread()
        shutdown()
        print(f"Final -> Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']} | Total: {int(state['entry_counter'])+int(state['exit_counter'])}")