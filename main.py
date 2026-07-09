import threading
from time import sleep, time

import requests
from gpiozero import Button, Buzzer

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

# ── Config ────────────────────────────────────────────────────────────────────
SALIDA_SIDE = {"S2"}
INGRESO_SIDE = {"S1", "S3", "S4"}

EVENT_BEEP_DURATION = 0.2        # pitido al confirmar ingreso
TIME_COOLDOWN = 0.2              # entre eventos de cruce

LINGER_THRESHOLD = 1.5           # segundos antes de iniciar alarma
LINGER_BEEP_DURATION = 0.1       # duración de cada pitido de alarma
LINGER_BEEP_PERIOD = 0.4         # periodo entre pitidos de alarma

API_URL = "http://localhost:8000/api/passenger"
HTTP_TIMEOUT = 3.0

DIRECTION_ENTRY = 'ENTRY'
DIRECTION_EXIT = 'EXIT'

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


# ── HTTP ──────────────────────────────────────────────────────────────────────
def send_passenger_event(direction: str) -> None:
    """Envía el evento al backend en un thread aparte para no bloquear el loop."""
    def _post():
        try:
            requests.post(
                API_URL,
                json={"direction": direction, "door": "FRONT"},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            log(f"[HTTP ERROR] {e}")

    threading.Thread(target=_post, daemon=True).start()


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
    try:
        sensor_loop()
    except KeyboardInterrupt:
        log("Interrumpido por el usuario.")
    except Exception as e:
        log(f"Error inesperado: {e}")
    finally:
        shutdown()
        log(f"Final -> Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']}")