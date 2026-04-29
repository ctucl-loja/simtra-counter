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
                json={"direction": direction,"door":"FRONT"},
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            print(f"[HTTP ERROR] {e}")

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

        # Marca el inicio de actividad sostenida
        if active_count > 0 and any_active_since is None:
            any_active_since = current_time

        if active_count == 0:
            first_activation.clear()
            event_counted = False
            any_active_since = None
            last_linger_beep = 0.0
            state["sensors"] = raw
            sleep(0.05)
            continue

        # Alarma de permanencia: alguien se quedó en las barreras
        if (any_active_since is not None
                and current_time - any_active_since >= LINGER_THRESHOLD
                and current_time - last_linger_beep >= LINGER_BEEP_PERIOD
                and not buzzer_is_active()):
            trigger_buzzer(current_time, LINGER_BEEP_DURATION)
            last_linger_beep = current_time

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

                print(f"[{evento}] Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']}")

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
    print("Sensor loop iniciado. Ctrl+C para salir.\n")
    try:
        sensor_loop()
    except KeyboardInterrupt:
        print("\n\nInterrumpido por el usuario.")
    except Exception as e:
        print(f"\n\nError inesperado: {e}")
    finally:
        shutdown()
        print(f"Final -> Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']}")