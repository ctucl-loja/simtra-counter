# Simtra Counter — Contador de Pasajeros

Aplicación web para el conteo de pasajeros mediante sensores GPIO en una Raspberry Pi. Detecta ingresos y salidas según el orden de activación de los sensores y expone la información en tiempo real vía Flask.

---

## Requisitos

- Raspberry Pi (cualquier modelo con GPIO)
- Python 3.9+
- 4 sensores conectados a los pines GPIO: **21 (S1)**, **20 (S2)**, **16 (S3)**, **12 (S4)**
- Acceso SSH o consola como usuario `admin`

---

## Instalación

### 1. Clonar el repositorio

```bash
cd /home/admin
git clone https://github.com/ctucl-loja/simtra-counter.git
cd simtra-counter
```

### 2. Crear y activar el entorno virtual

```bash
python3 -m venv /home/admin/env
source /home/admin/env/bin/activate
```

### 3. Instalar dependencias

```bash
pip install flask gpiozero
```

> En PC/Mac (sin GPIO) la aplicación arranca automáticamente en **modo simulación**; no se requiere hardware adicional.

---

## Configurar el servicio systemd

### 1. Crear el archivo de servicio

```bash
sudo nano /etc/systemd/system/simtra-counter.service
```

Pegar el siguiente contenido:

```ini
[Unit]
Description=Aplicacion de Conteo de Pasajeros
After=network.target

[Service]
User=admin
WorkingDirectory=/home/admin/simtra-counter/
ExecStart=/home/admin/env/bin/python3 app.py --host 0.0.0.0 --port 4000
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Guardar con `Ctrl+O`, luego `Ctrl+X`.

### 2. Habilitar e iniciar el servicio

```bash
sudo systemctl daemon-reload
sudo systemctl enable simtra-counter
sudo systemctl start simtra-counter
```

### 3. Verificar el estado

```bash
sudo systemctl status simtra-counter
```

La aplicación queda disponible en `http://<ip-raspberry>:4000`.

---

## Comandos útiles

| Acción | Comando |
|---|---|
| Ver logs en tiempo real | `sudo journalctl -u simtra-counter -f` |
| Reiniciar el servicio | `sudo systemctl restart simtra-counter` |
| Detener el servicio | `sudo systemctl stop simtra-counter` |
| Deshabilitar el servicio | `sudo systemctl disable simtra-counter` |

---

## Conexión de sensores GPIO

| Sensor | Pin GPIO (BCM) | Función |
|---|---|---|
| S4 | 12 | Ingreso |
| S3 | 16 | Ingreso |
| S2 | 20 | Salida |
| S1 | 21 | Ingreso |

Los sensores se configuran con `pull_up=True`. Se cuenta un **ingreso** cuando S1 se activa antes que S2, y una **salida** cuando S2 se activa primero. El evento requiere al menos 3 sensores activos simultáneamente.

---

## API

| Endpoint | Método | Descripción |
|---|---|---|
| `/` | GET | Interfaz web principal |
| `/api/state` | GET | Estado actual en JSON |
| `/api/reset` | POST | Reinicia los contadores a 0 |

### Ejemplo de respuesta `/api/state`

```json
{
  "sensors": {"S1": false, "S2": false, "S3": false, "S4": false},
  "entry_counter": 12,
  "exit_counter": 8
}
```

![Diagrama GPIO](docs/gpio-diagram.png)