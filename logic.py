from time import sleep, time
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

BUZZER = Buzzer(26)          # activo en HIGH
BUZZER_DURATION = 1.0        # segundos

SALIDA_SIDE = {"S2"}
INGRESO_SIDE = {"S1", "S3", "S4"}

# ── Runtime ───────────────────────────────────────────────────────────────────
first_activation: dict[str, float] = {}
event_counted = False
last_event_time = 0.0
buzzer_off_at = 0.0
TIME_COOLDOWN = 2.0


def trigger_buzzer(now: float) -> None:
    global buzzer_off_at
    BUZZER.on()
    buzzer_off_at = now + BUZZER_DURATION


def update_buzzer(now: float) -> None:
    global buzzer_off_at
    if buzzer_off_at and now >= buzzer_off_at:
        BUZZER.off()
        buzzer_off_at = 0.0


def sensor_loop():
    global event_counted, last_event_time

    while True:
        current_time = time()
        update_buzzer(current_time)

        raw = {name: btn.is_pressed for name, btn in SENSORS_HW.items()}
        active_count = sum(raw.values())

        for name, active in raw.items():
            if active and name not in first_activation:
                first_activation[name] = current_time

        if active_count == 0:
            first_activation.clear()
            event_counted = False
            state["sensors"] = raw
            sleep(0.05)
            continue

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
                    trigger_buzzer(current_time)
                else:
                    state["exit_counter"] += 1
                    evento = "SALIDA"

                last_event_time = current_time
                event_counted = True

                print(f"[{evento}] Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']}")

        state["sensors"] = raw
        sleep(0.05)


if __name__ == "__main__":
    try:
        sensor_loop()
    except KeyboardInterrupt:
        print(f"\nFinal -> Ingresos: {state['entry_counter']} | Salidas: {state['exit_counter']}")
    finally:
        BUZZER.off()