import os
import queue
import sqlite3
import threading
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
# hilo de sincronización es dueño de la conexión SQLite: intenta el POST y, si
# falla, deja el evento guardado con uploaded=0 para reintentarlo luego.
_new_events: queue.Queue = queue.Queue()
_sync_stop = threading.Event()
_sync_wake = threading.Event()
_sync_thread: threading.Thread | None = None


def open_local_db() -> sqlite3.Connection:
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS passenger_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            direction   TEXT    NOT NULL,
            door        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            uploaded    INTEGER NOT NULL DEFAULT 0,
            uploaded_at TEXT,
            attempts    INTEGER NOT NULL DEFAULT 0,
            last_error  TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_passenger_events_pending "
        "ON passenger_events (uploaded, id)"
    )
    conn.commit()
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def post_passenger(direction: str, door: str) -> str | None:
    """Hace el POST. Devuelve None si fue exitoso, o el motivo del fallo."""
    try:
        resp = requests.post(
            API_URL,
            json={"direction": direction, "door": door},  # payload de PassengerCreate
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        return f"{type(e).__name__}: {e}"
    if not resp.ok:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    return None


def _mark_attempt(conn: sqlite3.Connection, event_id: int, error: str | None) -> None:
    if error is None:
        conn.execute(
            "UPDATE passenger_events SET uploaded = 1, uploaded_at = ?, "
            "attempts = attempts + 1, last_error = NULL WHERE id = ?",
            (_now_iso(), event_id),
        )
    else:
        conn.execute(
            "UPDATE passenger_events SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (error, event_id),
        )
    conn.commit()


def _store_new_events(conn: sqlite3.Connection) -> list[int]:
    """Pasa los eventos encolados en memoria a SQLite (uploaded=0)."""
    ids = []
    while True:
        try:
            direction, door, created_at = _new_events.get_nowait()
        except queue.Empty:
            return ids
        cur = conn.execute(
            "INSERT INTO passenger_events (direction, door, created_at) VALUES (?, ?, ?)",
            (direction, door, created_at),
        )
        conn.commit()
        ids.append(cur.lastrowid)


def sync_pending(conn: sqlite3.Connection, new_ids: list[int]) -> None:
    """Sube en orden (más antiguo primero) todos los eventos con uploaded=0."""
    pending = conn.execute(
        "SELECT id, direction, door FROM passenger_events WHERE uploaded = 0 ORDER BY id"
    ).fetchall()

    for event_id, direction, door in pending:
        if _sync_stop.is_set():
            return
        is_new = event_id in new_ids
        if not is_new:
            log(f"[SYNC] Reintentando evento pendiente #{event_id} ({direction})")

        error = post_passenger(direction, door)
        _mark_attempt(conn, event_id, error)

        if error is None:
            if is_new:
                log(f"[HTTP OK] Evento #{event_id} ({direction}) enviado al backend")
            else:
                log(f"[SYNC] Evento pendiente #{event_id} ({direction}) -> uploaded=true")
            continue

        log(f"[HTTP ERROR] Evento #{event_id} ({direction}) no enviado: {error}")
        if is_new:
            log(f"[LOCAL] Evento #{event_id} guardado localmente como pendiente (uploaded=false)")
        # Otros eventos nuevos de esta tanda también quedan guardados como pendientes
        for other_id in new_ids:
            if other_id > event_id:
                log(f"[LOCAL] Evento #{other_id} guardado localmente como pendiente (uploaded=false)")
        # Backend caído o inalcanzable: se corta la tanda y se reintenta en el
        # próximo ciclo, para no pagar un timeout por cada evento pendiente.
        return


def sync_loop() -> None:
    try:
        conn = open_local_db()
    except sqlite3.Error as e:
        log(f"[LOCAL ERROR] No se pudo abrir la base local {LOCAL_DB_PATH}: {e}")
        return

    pending_count = conn.execute(
        "SELECT COUNT(*) FROM passenger_events WHERE uploaded = 0"
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
            for event_id in _store_new_events(conn):
                log(f"[LOCAL] Evento #{event_id} guardado localmente como pendiente (uploaded=false)")
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
    """Encola el evento para el hilo de sincronización (no bloquea el loop)."""
    _new_events.put((direction, DOOR, _now_iso()))
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