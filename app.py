from flask import Flask, render_template, jsonify
from threading import Thread, Lock
from time import sleep, time

app = Flask(__name__)

# ── Estado compartido ────────────────────────────────────────────────────────
state_lock = Lock()
state = {
    "sensors": {"S1": False, "S2": False, "S3": False, "S4": False},
    "entry_counter": 0,
    "exit_counter": 0,
}

# ── Lógica de conteo (reemplaza con gpiozero en Raspberry Pi) ────────────────
# Para desarrollo en PC se simula con variables; en RPi descomenta gpiozero.

try:
    from gpiozero import Button

    SENSORS_HW = {
        "S1": Button(26, pull_up=True),
        "S2": Button(16, pull_up=True),
        "S3": Button(20, pull_up=True),
        "S4": Button(21, pull_up=True),
    }
    SIMULATION = False
except Exception:
    SENSORS_HW = None
    SIMULATION = True  # modo demo en PC


SALIDA_SIDE = {"S1"}
INGRESO_SIDE  = {"S2", "S3", "S4"}

first_activation: dict[str, float] = {}
event_counted   = False
last_event_time = 0.0
time_cooldown   = 2.0


def sensor_loop():
    """Lee sensores y actualiza el estado global."""
    global first_activation, event_counted, last_event_time

    # Contador de demo para simular activaciones en PC
    demo_tick = 0

    while True:
        current_time = time()

        if SIMULATION:
            # ── Demo: alterna activaciones cada ~3 s para visualizar ──
            demo_tick += 1
            cycle = demo_tick % 120          # ~6 s por ciclo completo (50 ms × 120)
            if cycle < 40:
                raw = {"S1": False, "S2": False, "S3": False, "S4": False}
            elif cycle < 70:
                # Simula INGRESO: S1 primero, luego el resto
                raw = {"S1": cycle >= 43, "S2": cycle >= 52,
                       "S3": cycle >= 52, "S4": cycle >= 52}
            elif cycle < 85:
                raw = {"S1": False, "S2": False, "S3": False, "S4": False}
            else:
                # Simula SALIDA: S2/S3/S4 primero
                raw = {"S1": cycle >= 100, "S2": cycle >= 88,
                       "S3": cycle >= 88,  "S4": cycle >= 88}
        else:
            raw = {name: btn.is_pressed for name, btn in SENSORS_HW.items()}

        active_count = sum(raw.values())

        # 1. Registrar primera activación
        for name, active in raw.items():
            if active and name not in first_activation:
                first_activation[name] = current_time

        # 2. Reset al liberar la barra
        if active_count == 0:
            first_activation.clear()
            event_counted = False
            with state_lock:
                state["sensors"] = raw
            sleep(0.05)
            continue

        # 3. Cooldown
        if current_time - last_event_time < time_cooldown:
            with state_lock:
                state["sensors"] = raw
            sleep(0.01)
            continue

        # 4. Contar con ≥3 sensores activos
        if active_count >= 3 and not event_counted:
            t_ing = min(
                (first_activation[s] for s in INGRESO_SIDE if s in first_activation),
                default=float("inf"),
            )
            t_sal = min(
                (first_activation[s] for s in SALIDA_SIDE  if s in first_activation),
                default=float("inf"),
            )

            if not (t_ing == float("inf") and t_sal == float("inf")):
                with state_lock:
                    if t_ing <= t_sal:
                        state["entry_counter"] += 1
                    else:
                        state["exit_counter"] += 1

                last_event_time = current_time
                event_counted   = True

        with state_lock:
            state["sensors"] = raw

        sleep(0.05)


# ── Rutas Flask ───────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with state_lock:
        snap = dict(state)
    return render_template("index.html", state=snap, simulation=SIMULATION)


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify(state)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with state_lock:
        state["entry_counter"] = 0
        state["exit_counter"]  = 0
    return jsonify({"ok": True})


# ── Arranque ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t = Thread(target=sensor_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000, debug=False)