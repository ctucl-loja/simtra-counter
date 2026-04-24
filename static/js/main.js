// ── Reloj ──────────────────────────────────────────────────────────────────────
const clockEl = document.getElementById("clock");
function updateClock() {
  const now  = new Date();
  const pad  = n => String(n).padStart(2, "0");
  clockEl.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}
setInterval(updateClock, 1000);
updateClock();

// ── Polling de estado ─────────────────────────────────────────────────────────
const POLL_MS     = 500;
const entryEl     = document.getElementById("entry-count");
const exitEl      = document.getElementById("exit-count");
const netEl       = document.getElementById("net-count");
const updateTag   = document.getElementById("update-tag");
const pollInfoEl  = document.getElementById("poll-info");
const sensorBars  = document.querySelectorAll(".sensor-bar[data-sensor]");

// cache SVG diagram rects
const diagramRects = {};
["S1","S2","S3","S4"].forEach(s => {
  diagramRects[s] = document.getElementById(`d-${s}`);
});

let prevEntry = parseInt(entryEl.textContent) || 0;
let prevExit  = parseInt(exitEl.textContent)  || 0;

function bump(el) {
  el.classList.remove("bump");
  void el.offsetWidth; // reflow
  el.classList.add("bump");
}

function applyState(data) {
  const { sensors, entry_counter, exit_counter } = data;

  // ── Contadores ──
  if (entry_counter !== prevEntry) {
    entryEl.textContent = entry_counter;
    bump(entryEl);
    prevEntry = entry_counter;
  }
  if (exit_counter !== prevExit) {
    exitEl.textContent = exit_counter;
    bump(exitEl);
    prevExit = exit_counter;
  }
  const net = entry_counter - exit_counter;
  netEl.textContent = net;

  // ── Barras de sensor ──
  sensorBars.forEach(bar => {
    const name   = bar.dataset.sensor;
    const active = sensors[name];
    if (active) {
      bar.classList.add("active");
    } else {
      bar.classList.remove("active");
    }
  });

  // ── Diagrama SVG ──
  const COLORS_IN  = { fill: "rgba(168,208,96,.25)", stroke: "rgba(168,208,96,.7)" };
  const COLORS_OUT = { fill: "rgba(240,112,80,.25)",  stroke: "rgba(240,112,80,.7)"  };
  const COLORS_OFF = { fill: "var(--surface3)",        stroke: "var(--border)"         };

  Object.entries(sensors).forEach(([name, active]) => {
    const rect = diagramRects[name];
    if (!rect) return;
    const colors = active
      ? (name === "S2" ? COLORS_IN : COLORS_OUT)
      : COLORS_OFF;
    rect.style.fill   = colors.fill;
    rect.style.stroke = colors.stroke;
  });

  // ── Indicador de actualización ──
  updateTag.textContent = `OK · ${new Date().toLocaleTimeString("es-EC", { hour12: false })}`;
}

async function poll() {
  const t0 = Date.now();
  try {
    const res  = await fetch("/api/state");
    const data = await res.json();
    applyState(data);
    pollInfoEl.textContent = `Poll: ${Date.now() - t0}ms`;
  } catch (err) {
    updateTag.textContent = "⚠ sin conexión";
    console.error("Poll error:", err);
  }
}

setInterval(poll, POLL_MS);
poll(); // primera llamada inmediata

// ── Reset ─────────────────────────────────────────────────────────────────────
document.getElementById("reset-btn").addEventListener("click", async () => {
  if (!confirm("¿Reiniciar ambos contadores a cero?")) return;
  await fetch("/api/reset", { method: "POST" });
  entryEl.textContent = "0";
  exitEl.textContent  = "0";
  netEl.textContent   = "0";
  prevEntry = 0;
  prevExit  = 0;
});