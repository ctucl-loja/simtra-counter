from time import sleep, time
from gpiozero import Button

# Pines candidatos (los mismos del contador, sin asumir cuál es cuál)
CANDIDATE_PINS = [12, 16, 20, 26]

buttons = {pin: Button(pin, pull_up=True) for pin in CANDIDATE_PINS}


def monitor_mode():
    """Imprime cada cambio de estado. Tapa un sensor a la vez para identificarlo."""
    print("── MODO MONITOR ──")
    print("Tapa cada sensor uno por uno. Ctrl+C para salir.\n")
    print(f"Pines vigilados: {CANDIDATE_PINS}\n")

    last = {pin: False for pin in CANDIDATE_PINS}
    t0 = time()

    while True:
        for pin, btn in buttons.items():
            pressed = btn.is_pressed
            if pressed != last[pin]:
                ts = time() - t0
                estado = "ACTIVO  " if pressed else "libre   "
                activos = [p for p, b in buttons.items() if b.is_pressed]
                print(f"[{ts:6.2f}s] GPIO{pin:>2} -> {estado} | activos ahora: {activos}")
                last[pin] = pressed
        sleep(0.02)


def wait_single_activation(already_assigned: set[int], label: str) -> int:
    """Espera a que se active UN pin no asignado todavía y lo devuelve."""
    print(f"\n➡  Activa el sensor de: {label}")
    print("   (tapa el haz y mantenlo unos segundos)")

    # Esperar a que todos los no asignados estén libres antes de empezar
    while any(buttons[p].is_pressed for p in CANDIDATE_PINS if p not in already_assigned):
        sleep(0.05)

    # Esperar activación estable (~300 ms) de un solo pin no asignado
    detected = None
    stable_since = None
    STABLE_MS = 0.3

    while True:
        activos = [p for p in CANDIDATE_PINS
                   if p not in already_assigned and buttons[p].is_pressed]

        if len(activos) == 1:
            pin = activos[0]
            if detected != pin:
                detected, stable_since = pin, time()
            elif time() - stable_since >= STABLE_MS:
                print(f"   ✓ Detectado: GPIO{pin}")
                # Esperar a que se libere antes de seguir
                while buttons[pin].is_pressed:
                    sleep(0.05)
                return pin
        else:
            detected, stable_since = None, None

        sleep(0.02)


def calibration_mode():
    """Guía paso a paso: pie → cabeza, luego identifica el adelantado."""
    print("── MODO CALIBRACIÓN ──")
    print("Se te pedirá activar cada sensor en orden.\n")

    assigned: dict[str, int] = {}
    prompts = [
        ("S1", "el PIE (más abajo)"),
        ("S2", "la rodilla / segundo desde abajo"),
        ("S3", "el pecho / tercero desde abajo"),
        ("S4", "la CABEZA (más arriba)"),
    ]

    for label, desc in prompts:
        pin = wait_single_activation(set(assigned.values()), f"{label} — {desc}")
        assigned[label] = pin

    # Identificar el sensor adelantado
    print("\n➡  Ahora cruza la barrera caminando MUY lento hacia adelante.")
    print("   Detectaremos cuál se activa primero (el adelantado).")

    while any(b.is_pressed for b in buttons.values()):
        sleep(0.05)

    first_times: dict[int, float] = {}
    while len(first_times) < 2:
        for pin, btn in buttons.items():
            if btn.is_pressed and pin not in first_times:
                first_times[pin] = time()
        sleep(0.005)

    adelantado_pin = min(first_times, key=first_times.get)
    adelantado_label = next(lbl for lbl, p in assigned.items() if p == adelantado_pin)

    # ── Resultado ─────────────────────────────────────────────────────────
    print("\n" + "═" * 50)
    print("  MAPEO DETECTADO")
    print("═" * 50)
    for label in ("S1", "S2", "S3", "S4"):
        marca = "  ← ADELANTADO" if assigned[label] == adelantado_pin else ""
        print(f"  {label} = GPIO{assigned[label]}{marca}")
    print(f"\n  Sensor adelantado: {adelantado_label} (GPIO{adelantado_pin})")

    print("\n── Código listo para pegar ──\n")
    print("SENSORS_HW = {")
    for label in ("S1", "S2", "S3", "S4"):
        print(f'    "{label}": Button({assigned[label]}, pull_up=True),')
    print("}")
    print(f'\nADELANTADO = "{adelantado_label}"')


if __name__ == "__main__":
    print("1) Monitor en tiempo real")
    print("2) Calibración guiada (genera mapeo S1–S4)")
    choice = input("\nElige [1/2]: ").strip()

    try:
        if choice == "1":
            monitor_mode()
        else:
            calibration_mode()
    except KeyboardInterrupt:
        print("\nSaliendo.")