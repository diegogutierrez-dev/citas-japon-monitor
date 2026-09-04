#!/usr/bin/env python3
"""
Monitor de citas — Embajada de Japón en Colombia (rsvsys / CakePHP).

Notifica solo cuando aparece un cupo ANTERIOR a la cita que ya tengo.
Nunca reserva: el flujo de reserva usa reCAPTCHA y queda fuera de alcance.

Flujo:
  1. GET / y GET /reservations/calendar  -> cookies + tokens iniciales
  2. POST /ajax/reservations/calendar (disp_type=month) por mes del rango
     -> filtro grueso: días cuyo ícono NO es "disabled"
  3. POST (disp_type=day) solo en esos días -> horas exactas con 残N件
  4. Descarta lo que sea >= BEFORE y notifica si el resultado cambió

Los tokens (_csrfToken, _Token[fields]) rotan en cada respuesta, así que se
releen del HTML después de cada request.

Uso:
    python monitor.py
    python monitor.py --verbose     # inventario de íconos por mes
    python monitor.py --dump        # guarda los HTML crudos

Env:
    MONTHS_AHEAD  meses a revisar contando el actual (default 2)
    BEFORE        fecha ISO límite; solo notifica cupos anteriores
                  (default 2026-11-03, la cita ya agendada)
    NTFY_TOPIC    notifica vía https://ntfy.sh/<topic>
    TELEGRAM_TOKEN + TELEGRAM_CHAT_ID
    STATE_FILE    ruta del estado entre corridas (default state.json)

Exit codes (el workflow de Actions los traduce a job rojo/verde):
    0  corrida normal, con o sin cupos
    2  fallo de red tras agotar reintentos (5xx, timeout, conexión)
    3  HTML inesperado o HTTP 4xx: el parser se rompió o nos bloquearon
"""

import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://embjpcol.rsvsys.jp"
CALENDAR = f"{BASE}/reservations/calendar"
AJAX = f"{BASE}/ajax/reservations/calendar"

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
MONTHS_AHEAD = int(os.getenv("MONTHS_AHEAD", "2"))
BEFORE = date.fromisoformat(os.getenv("BEFORE", "2026-11-03"))
PAUSE = 2.5  # segundos entre requests; es el sitio de una embajada
RETRIES = 3

EXIT_NETWORK = 2
EXIT_PARSE = 3

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SLOT_RE = re.compile(r"残\s*(\d+)\s*件")
TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
YEAR_RE = re.compile(r"(\d{4})\s*年")
MONTH_RE = re.compile(r"<b>\s*(\d{1,2})\s*</b>\s*月")

# Íconos que significan "no hay nada". Cualquier otro se trata como candidato.
# El alt dice "Cupos disponibles" siempre; la señal real es el nombre del archivo.
NO_SLOTS = ("icon_disabled", "icon_cross", "icon_close")

VERBOSE = "--verbose" in sys.argv
DUMP = "--dump" in sys.argv


class NetworkError(Exception):
    """Fallo de red tras agotar reintentos: reintentable en la próxima corrida."""


class ParseError(Exception):
    """El sitio respondió algo que no entendemos: hay que revisar a mano."""


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {msg}", flush=True)


class Rsvsys:
    """Cliente que mantiene sesión y tokens frescos."""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept-Language": "es,ja;q=0.8",
        })
        self.fields = {}

    def _request(self, method: str, url: str, desc: str, **kw) -> requests.Response:
        """Un 4xx no se reintenta (bloqueo o cambio del sitio); 5xx y errores
        de conexión sí, con backoff."""
        last = None
        for attempt in range(RETRIES):
            try:
                r = self.s.request(method, url, timeout=30, **kw)
            except requests.RequestException as e:
                last = e
            else:
                if r.status_code < 400:
                    return r
                if r.status_code < 500:
                    raise ParseError(f"{desc}: HTTP {r.status_code} — posible bloqueo o cambio del sitio")
                last = requests.HTTPError(f"HTTP {r.status_code}")
            if attempt < RETRIES - 1:
                wait = 5 * (attempt + 1)
                log(f"error de red en {desc} ({last}); reintento en {wait}s")
                time.sleep(wait)
        raise NetworkError(f"{desc}: {last}")

    def bootstrap(self) -> str:
        self._request("GET", BASE + "/", "bootstrap raíz")
        time.sleep(1)
        r = self._request("GET", CALENDAR, "bootstrap calendario")
        self._absorb(r.text)
        if "_csrfToken" not in self.fields:
            raise ParseError("bootstrap: el calendario no trae _csrfToken (¿cambió el HTML?)")
        return r.text

    def _absorb(self, html: str) -> None:
        """Relee todos los inputs del form (tokens incluidos)."""
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", action=re.compile(r"reservations/calendar"))
        if form is None:
            return
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value", "")
        sel = form.find("select", attrs={"name": "stock"})
        if sel:
            opt = sel.find("option", selected=True) or sel.find("option")
            if opt:
                data["stock"] = opt.get("value", "1")
        self.fields.update(data)

    def view(self, target: date, disp_type: str) -> str:
        """POST al endpoint ajax pidiendo una fecha/vista concreta."""
        payload = dict(self.fields)
        payload.update({
            "_method": "POST",
            "date": target.strftime("%Y/%m/%d"),
            "disp_type": disp_type,
            "search": "exec",
        })
        payload.setdefault("event", "9")
        payload.setdefault("plan", "8")
        payload.setdefault("stock", "1")

        r = self._request(
            "POST",
            AJAX,
            f"vista {disp_type} {target:%Y-%m-%d}",
            data=payload,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": CALENDAR,
                "Origin": BASE,
            },
        )
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
        self._absorb(html)  # tokens nuevos para el siguiente request
        if DUMP:
            Path(f"dump_{disp_type}_{target:%Y%m%d}.html").write_text(html, encoding="utf-8")
        return html


# ------------------------------------------------------------- parsers

def parse_month(html: str):
    """Devuelve (dias_candidatos, inventario_iconos) de una vista de mes."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="sc_cal_month")
    if table is None:
        raise ParseError("vista de mes sin table.sc_cal_month")

    header = soup.find("div", class_="c_cal_navex_date")
    header_html = str(header) if header else html
    y = YEAR_RE.search(header_html)
    m = MONTH_RE.search(header_html)
    if not (y and m):
        raise ParseError("vista de mes sin encabezado año/mes reconocible")
    year, month = int(y.group(1)), int(m.group(1))

    candidates, inventory = [], {}
    for td in table.find_all("td"):
        day_div = td.find("div", class_="sc_cal_date")
        img = td.find("img")
        if day_div is None or img is None:
            continue
        try:
            day = int(day_div.get_text(strip=True))
        except ValueError:
            continue

        icon = os.path.basename(img.get("src", "").split("?")[0])
        inventory[icon] = inventory.get(icon, 0) + 1
        if not any(tag in icon for tag in NO_SLOTS):
            candidates.append(date(year, month, day))

    return candidates, inventory


def parse_day(html: str):
    """Devuelve [(hora, cupos), ...] con cupos > 0 de una vista de día."""
    soup = BeautifulSoup(html, "html.parser")
    if not SLOT_RE.search(soup.get_text(" ", strip=True)):
        raise ParseError("vista de día sin ningún '残 N 件'")
    out = []
    for row in soup.find_all("tr"):
        text = row.get_text(" ", strip=True)
        slot = SLOT_RE.search(text)
        if not slot or int(slot.group(1)) == 0:
            continue
        hour = TIME_RE.search(text)
        out.append((hour.group(1) if hour else "?", int(slot.group(1))))
    return out


def filter_before(days, before: date):
    """Solo días que mejoran la cita actual (estrictamente anteriores)."""
    return sorted({d for d in days if d < before})


# ------------------------------------------------------------- notify

def notify(title: str, body: str, priority: str = "urgent", tags: str = "calendar") -> None:
    topic = os.getenv("NTFY_TOPIC")
    try:
        if topic:
            requests.post(
                f"https://ntfy.sh/{topic}",
                data=body.encode("utf-8"),
                headers={"Title": title.encode("utf-8"), "Priority": priority,
                         "Tags": tags, "Click": CALENDAR},
                timeout=20,
            )
        token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if token and chat:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": f"*{title}*\n{body}\n{CALENDAR}",
                      "parse_mode": "Markdown"},
                timeout=20,
            )
        if not topic and not (token and chat):
            log(f"[sin canal] {title} — {body}")
    except requests.RequestException as e:
        log(f"ERROR notificando ({title}): {e}")


# ------------------------------------------------------------- main

def months_to_check(n: int):
    today = date.today()
    y, m = today.year, today.month
    for _ in range(n):
        yield date(y, m, 1)
        m += 1
        if m > 12:
            y, m = y + 1, 1


def check():
    """Corre el flujo completo y devuelve [(fecha_iso, hora, cupos), ...]."""
    client = Rsvsys()
    client.bootstrap()

    candidate_days = []
    for first in months_to_check(MONTHS_AHEAD):
        time.sleep(PAUSE)
        html = client.view(first, "month")
        days, inventory = parse_month(html)
        candidate_days += days
        if VERBOSE:
            log(f"{first:%Y-%m}: iconos={inventory} candidatos={[str(d) for d in days]}")

    found = []
    for day in filter_before(candidate_days, BEFORE):
        time.sleep(PAUSE)
        html = client.view(day, "day")
        for hour, count in parse_day(html):
            found.append((day.isoformat(), hour, count))

    found.sort()
    return found


def main() -> int:
    try:
        found = check()
    except NetworkError as e:
        log(f"ERROR de red (reintentos agotados): {e}")
        return EXIT_NETWORK
    except ParseError as e:
        log(f"ERROR de parseo: {e}")
        # El email de workflow fallido de GitHub se ignora fácil; un parser
        # roto deja al bot ciego indefinidamente, así que también va por ntfy.
        notify("Monitor de citas roto", f"{e}\nRevisar si cambió el HTML del sitio.",
               priority="default", tags="warning")
        return EXIT_PARSE

    signature = "|".join(f"{d} {h}x{c}" for d, h, c in found)
    log(f"{len(found)} cupos antes de {BEFORE}: {signature or 'ninguno'}")

    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass

    if found and signature != state.get("signature"):
        body = "\n".join(f"{d} {h} — {c} cupo(s)" for d, h, c in found[:20])
        notify(f"Cita disponible ({len(found)})", body)

    STATE_FILE.write_text(json.dumps({"signature": signature}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
