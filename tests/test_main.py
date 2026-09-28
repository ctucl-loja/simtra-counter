"""
Pruebas de la cola local de pasajeros de main.py (sin hardware ni red).

gpiozero se reemplaza por un doble y requests.post por una función que
registra el payload, así que corre en cualquier PC:

    python3 -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BUS_MANAGER = Path(os.getenv("SIMTRA_BUS_MANAGER_PATH",
                             ROOT.parent / "display" / "simtra-bus-manager"))


class _FakeDevice:
    is_pressed = False

    def __init__(self, *args, **kwargs):
        pass

    def on(self):
        pass

    def off(self):
        pass

    def close(self):
        pass


sys.modules["gpiozero"] = types.SimpleNamespace(Button=_FakeDevice, Buzzer=_FakeDevice)
sys.path.insert(0, str(ROOT))
import main  # noqa: E402

main.log = lambda message: None   # silencia los logs durante las pruebas


class FakeResponse:
    def __init__(self, status_code, text="{}"):
        self.status_code = status_code
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300


class PassengerQueueTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = main.open_local_db(Path(tmp.name) / "events.sqlite3")
        self.addCleanup(self.conn.close)

        # Cola en memoria limpia en cada prueba
        while not main._new_events.empty():
            main._new_events.get_nowait()

        self.sent = []
        self.responses = []
        original = main.requests.post
        self.addCleanup(setattr, main.requests, "post", original)
        main.requests.post = self.fake_post

    def fake_post(self, url, json=None, timeout=None):
        self.sent.append(json)
        outcome = self.responses.pop(0) if self.responses else FakeResponse(201)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def detect(self, direction="ENTRY"):
        """Simula un cruce y un ciclo del hilo de sincronización."""
        main.send_passenger_event(direction)
        new_ids = main._store_new_events(self.conn)
        main.sync_pending(self.conn, new_ids)
        return new_ids

    def sync(self):
        main.sync_pending(self.conn, [])

    def rows(self):
        return self.conn.execute(
            "SELECT event_id, timestamp, direction, door, uploaded, attempts, last_error, rejected "
            "FROM passenger_events ORDER BY id").fetchall()

    # ── 1. evento nuevo ──────────────────────────────────────────────────────

    def test_evento_nuevo_se_guarda_con_event_id_y_timestamp(self):
        self.responses = [requests.ConnectionError("backend apagado")]
        self.detect()

        (event_id, timestamp, direction, door, uploaded, *_), = self.rows()
        self.assertTrue(event_id)
        self.assertIsNotNone(datetime.fromisoformat(timestamp).tzinfo)   # ISO con zona
        self.assertEqual((direction, door, uploaded), ("ENTRY", main.DOOR, 0))

    def test_cada_cruce_tiene_su_propio_event_id(self):
        self.detect()
        self.detect("EXIT")
        ids = [row[0] for row in self.rows()]
        self.assertEqual(len(set(ids)), 2)

    # ── 2. reintentos estables ───────────────────────────────────────────────

    def test_reintentos_usan_el_mismo_event_id_y_timestamp(self):
        self.responses = [requests.Timeout("sin respuesta"), FakeResponse(500), FakeResponse(201)]
        self.detect()
        self.sync()
        self.sync()

        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[0], self.sent[1])
        self.assertEqual(self.sent[0], self.sent[2])
        event_id, timestamp, *_ = self.rows()[0]
        self.assertEqual(self.sent[0]["event_id"], event_id)
        self.assertEqual(self.sent[0]["timestamp"], timestamp)

    # ── 3. éxito ─────────────────────────────────────────────────────────────

    def test_respuesta_exitosa_marca_uploaded(self):
        self.detect()
        *_, uploaded, attempts, last_error, rejected = self.rows()[0]
        self.assertEqual((uploaded, attempts, last_error, rejected), (1, 1, None, 0))

    def test_registro_existente_devuelto_por_el_backend_tambien_marca_uploaded(self):
        # El primer POST llegó pero la respuesta se perdió (timeout); el backend
        # contesta el reintento con el registro existente (2xx).
        self.responses = [requests.Timeout("respuesta perdida"),
                          FakeResponse(201, '{"id": 7, "event_id": "..."}')]
        self.detect()
        self.sync()
        self.assertEqual(self.rows()[0][4], 1)
        self.sync()
        self.assertEqual(len(self.sent), 2)   # ya subido: no se vuelve a enviar

    # ── 4. fallos ────────────────────────────────────────────────────────────

    def test_timeout_o_error_http_deja_uploaded_en_false(self):
        for outcome in (requests.Timeout("timeout"), requests.ConnectionError("caído"),
                        FakeResponse(500), FakeResponse(422)):
            with self.subTest(outcome=outcome):
                self.conn.execute("DELETE FROM passenger_events")
                self.responses = [outcome]
                self.detect()
                (*_, uploaded, attempts, last_error, rejected), = self.rows()
                self.assertEqual((uploaded, attempts, rejected), (0, 1, 0))
                self.assertTrue(last_error)

    def test_backend_caido_corta_la_tanda(self):
        self.responses = [requests.ConnectionError("caído")] * 3
        self.detect()
        self.detect()
        self.sync()
        # 1 intento del primero + 1 del primero en la segunda tanda + 1 en sync
        self.assertEqual(len(self.sent), 3)
        self.assertTrue(all(p["event_id"] == self.sent[0]["event_id"] for p in self.sent))

    def test_409_no_se_reintenta_ni_bloquea_la_cola(self):
        self.responses = [FakeResponse(409, '{"detail": "El ID ya pertenece a un evento diferente"}')]
        self.detect()
        self.detect("EXIT")
        self.sync()

        first, second = self.rows()
        self.assertEqual((first[4], first[7]), (0, 1))     # uploaded=0, rejected=1
        self.assertIn("409", first[6])
        self.assertEqual(second[4], 1)
        self.assertEqual(len(self.sent), 2)                # el 409 no se reenvió

    # ── 5. payload ───────────────────────────────────────────────────────────

    def test_payload_exacto_sin_ubicacion(self):
        self.detect()
        self.assertEqual(set(self.sent[0]), {"event_id", "timestamp", "direction", "door"})

    @unittest.skipUnless((BUS_MANAGER / "schemas.py").exists(),
                         f"no se encontró simtra-bus-manager en {BUS_MANAGER}")
    def test_payload_valida_contra_passenger_create(self):
        try:
            import pydantic  # noqa: F401
        except ImportError:
            self.skipTest("pydantic no está instalado")
        sys.path.insert(0, str(BUS_MANAGER))
        self.addCleanup(sys.path.remove, str(BUS_MANAGER))
        from schemas import PassengerCreate

        self.detect()
        parsed = PassengerCreate.model_validate(self.sent[0])   # extra="forbid"
        self.assertEqual(parsed.event_id, self.sent[0]["event_id"])
        self.assertEqual(parsed.timestamp, datetime.fromisoformat(self.sent[0]["timestamp"]))


class LocalDbMigrationTest(unittest.TestCase):

    def test_base_vieja_se_migra_sin_perder_datos(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            old = sqlite3.connect(path)
            # Tabla tal como la creaba la versión anterior (sin event_id/timestamp)
            old.execute(
                "CREATE TABLE passenger_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "direction TEXT NOT NULL, door TEXT NOT NULL, created_at TEXT NOT NULL, "
                "uploaded INTEGER NOT NULL DEFAULT 0, uploaded_at TEXT, "
                "attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT)")
            old.executemany(
                "INSERT INTO passenger_events (direction, door, created_at, uploaded) VALUES (?, ?, ?, ?)",
                [("ENTRY", "FRONT", "2026-09-28T10:00:00+00:00", 0),
                 ("EXIT", "FRONT", "2026-09-28T10:01:00+00:00", 1)])
            old.commit()
            old.close()

            conn = main.open_local_db(path)
            rows = conn.execute(
                "SELECT direction, created_at, timestamp, event_id, uploaded, rejected "
                "FROM passenger_events ORDER BY id").fetchall()
            conn.close()
            # Abrir otra vez no rompe ni cambia los event_id ya asignados
            again = main.open_local_db(path)
            ids_again = [r[0] for r in again.execute("SELECT event_id FROM passenger_events ORDER BY id")]
            again.close()

        self.assertEqual([r[0] for r in rows], ["ENTRY", "EXIT"])
        self.assertEqual([r[2] for r in rows], [r[1] for r in rows])   # timestamp = created_at
        self.assertTrue(all(r[3] for r in rows))
        self.assertEqual(len({r[3] for r in rows}), 2)
        self.assertEqual([(r[4], r[5]) for r in rows], [(0, 0), (1, 0)])
        self.assertEqual(ids_again, [r[3] for r in rows])


if __name__ == "__main__":
    unittest.main()
