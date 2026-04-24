from time import sleep, time
from gpiozero import Button

# ── Estado ────────────────────────────────────────────────────────────────────
state = {
    "sensors": {"S1": False, "S2": False, "S3": False, "S4": False},
    "entry_counter": 0,
    "exit_counter": 0,
}

# ── Sensores ──────────────────────────────────────────────────────────────────
SENSORS_HW = {
    "S1": Button(26, pull_up=True),
    "S2": Button(16, pull_up=True),
    "S3": Button(20, pull_up=True),
    "S4": Button(12, pull_up=True),
}

SALIDA_SIDE = {"S2"}
INGRESO_SIDE = {"S1", "S3", "S4"}

first_activation: dict[str, float] = {}
event_counted = False
last_event_time = 0.0
time_cooldown = 2.0


def sensor_loop():
    global first_activation, event_counted, last_event_time

    while True:
        current_time = time()
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

        if current_time - last_event_time < time_cooldown:
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

            if not (t_ing == float("inf") and t_sal == float("inf")):
                if t_ing <= t_sal:
                    state["entry_counter"] += 1
                    evento = "INGRESO"
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