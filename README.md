# Monitor de citas — Embajada de Japón en Colombia

Monitorea el calendario de citas de visa ([embjpcol.rsvsys.jp](https://embjpcol.rsvsys.jp/reservations/calendar))
y notifica por [ntfy](https://ntfy.sh) cuando aparece un cupo **anterior** a la cita ya agendada
(3 de noviembre de 2026). Solo avisa — **nunca reserva**: el flujo de reserva del sitio usa
reCAPTCHA y queda deliberadamente fuera de alcance. El bot avisa, el humano agenda.

## Cómo funciona

1. `GET /` + `GET /reservations/calendar` → cookies y tokens CSRF iniciales.
2. `POST /ajax/reservations/calendar` con `disp_type=month` para el mes actual y el siguiente
   (`MONTHS_AHEAD=2`) → filtro grueso: un día es candidato si su ícono **no** es
   `icon_disabled.svg` (el `alt` dice "Cupos disponibles" siempre; la señal real es el
   nombre del archivo).
3. Solo para los días candidatos anteriores a `BEFORE`, `disp_type=day` → horas exactas
   con `残 N 件` y `N > 0`.
4. Si el conjunto de cupos cambió respecto a la corrida anterior, notifica por ntfy con
   prioridad urgente.

Los tokens (`_csrfToken`, `_Token[fields]`) rotan en cada respuesta; el cliente los relee
del HTML devuelto y los encadena, igual que el navegador. Hay una pausa de 2.5s entre
requests — es el sitio de una embajada, no se martillea, y no subas la frecuencia del cron.

## Setup

1. Crea un **repo público** (minutos de Actions ilimitados) y haz push de esto.
2. Elige un topic de ntfy largo e inadivinable (funciona como contraseña) y guárdalo como
   secret del repo: *Settings → Secrets and variables → Actions →* `NTFY_TOPIC`.
3. Suscríbete al topic en la app de ntfy.
4. Prueba a mano: *Actions → check-citas → Run workflow* (con `verbose` activado la
   primera vez, para ver el inventario de íconos).

## El cron

Colombia es UTC-5 sin horario de verano, así que 7:00am–8:00pm COT cruza la medianoche
UTC y necesita dos entradas:

```yaml
- cron: '*/10 12-23 * * 1-5'   # 7:00am–6:50pm COT, lun–vie
- cron: '*/10 0 * * 2-6'       # 7:00pm–7:50pm COT (mar–sáb UTC = lun–vie COT)
```

Dos realidades de los schedules de GitHub:

- **No son puntuales**: en horas pico se retrasan 3–15 minutos o se saltan ticks.
- **Se desactivan tras 60 días sin actividad en el repo.** Cualquier commit los reactiva;
  si el monitor va a vivir meses, un commit trivial de vez en cuando lo mantiene vivo.

## Estado entre corridas: por qué `actions/cache` y no un commit de `state.json`

El estado es solo la firma del último conjunto de cupos visto, para no notificar lo mismo
dos veces. Se persiste con `actions/cache` (key única por corrida + `restore-keys` por
prefijo, porque el cache es inmutable y no se puede actualizar bajo una key fija) en vez
de commitearlo al repo, por tres razones:

- Commitear generaría un commit por cada cambio de estado, exigiría `contents: write`, y
  tiene una carrera real: un dispatch manual y una corrida del cron cercanos pueden
  pisarse en el push y poner el job en rojo por algo que no es un fallo del monitoreo —
  exactamente el tipo de falsa alarma que este diseño evita.
- La desventaja del cache es que es efímero (GitHub borra entradas sin uso en ~7 días y
  evicta por LRU). Irrelevante aquí: el estado se refresca cada 10 minutos, y si algún
  día se pierde el costo es una notificación duplicada, no un cupo perdido. Falla en la
  dirección segura.
- Los datos de "a qué hora abren cupos" no necesitan el estado commiteado: cada corrida
  loguea con timestamp UTC (los logs de Actions se conservan 90 días) y cada notificación
  queda en el historial del topic de ntfy.

## Semántica de fallo

| Situación | Exit | Job | Aviso |
|---|---|---|---|
| Corrida normal, 0 cupos | 0 | verde | — |
| Cupos < `BEFORE` encontrados | 0 | verde | ntfy urgente |
| Red/timeout/5xx tras 3 reintentos | 2 | rojo | email de GitHub |
| HTML inesperado o HTTP 4xx (parser roto / bloqueo) | 3 | rojo | email de GitHub + ntfy |

El ntfy extra en parser-roto existe porque los emails de workflow fallido se ignoran
fácil, y un parser roto deja al bot ciego indefinidamente.

## Correr local

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
NTFY_TOPIC=mi-topic .venv/bin/python monitor.py --verbose
```

`--dump` guarda los HTML crudos de cada vista (útil si cambian el sitio y hay que
regenerar fixtures). También soporta Telegram vía `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID`.

## Tests

```bash
.venv/bin/pytest -q
```

Usan HTML fijo en `tests/fixtures/` — cero requests reales. Corren en CI solo en push
(workflow `tests`), no en cada corrida del monitoreo.
