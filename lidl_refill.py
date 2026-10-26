#!/usr/bin/env python3
"""Lidl Connect: free +1 GB refill when remaining data drops below a threshold.

Standard library only. API calls follow github.com/majd-abed/lidl-auto-refill
(OBSERVED_NETWORK.md), which were captured from the real customer portal.

Config via environment (or ~/.lidl-refill.env, read automatically):
  LIDL_USER, LIDL_PASS     login (phone number, password)
  LIDL_DRY_RUN=1           only log what would happen (default)
  LIDL_THRESHOLD=0.3       refill below this many GB
  LIDL_OFFER_ID=CCS_92061  tariff option id of the FREE 1 GB refill
  LIDL_VERBOSE=1           log every check
"""

import fcntl
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

HOME = Path.home()
ENV_FILE = HOME / ".lidl-refill.env"
STATE_DIR = Path(os.environ.get("LIDL_STATE_DIR") or HOME)
STATE_FILE = STATE_DIR / ".lidl-refill-state.json"
LOCK_FILE = STATE_DIR / ".lidl-refill.lock"

TOKEN_URL = "https://api.lidl-connect.de/api/token"
GRAPHQL_URL = "https://api.lidl-connect.de/api/graphql"
PORTAL_URL = "https://kundenkonto.lidl-connect.de/"
BUNDLE_PATH = "/assets/brfrontend/dist/production/js/lidl.js"
FALLBACK_CLIENT = ("lidl", "lidl")  # published in the portal bundle, seen 2026-10-25
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

LOGIN_BACKOFF = 30 * 60      # after a failed login
REFILL_SETTLE = 3 * 60       # Lidl may answer HTTP 500 yet apply the refill
REFILL_MAX_FAILS = 3         # unconfirmed refills in a row before pausing
REFILL_FAIL_PAUSE = 60 * 60
DRY_LOG_INTERVAL = 10 * 60

CONSUMPTIONS_QUERY = """query consumptions {
  consumptions {
    consumptionsForUnit {
      consumed unit formattedUnit type description expirationDate left max __typename
    }
    __typename
  }
}"""

REFILL_MUTATION = """mutation bookTariffOptionsDirect($bookTariffoptionsDirectInput: BookTariffoptionsDirectInput!) {
  bookTariffoptionsDirect(bookTariffoptionsDirectInput: $bookTariffoptionsDirectInput) {
    success
    __typename
  }
}"""


class LoginError(Exception):
    pass


class AuthRejected(Exception):
    pass


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if sep and key.strip() not in os.environ:
            parts = shlex.split(value, comments=True)
            os.environ[key.strip()] = " ".join(parts)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def http(method, url, body=None, headers=None):
    """Return (status, text). Never raises for HTTP error statuses."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json, text/html, */*")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("src"):
            self.sources.append(dict(attrs)["src"])


def discover_client():
    """The portal publishes its OAuth client id/secret in its JS bundle."""
    status, html = http("GET", PORTAL_URL)
    if status != 200:
        raise LoginError(f"Portal nicht erreichbar (HTTP {status})")
    parser = _Scripts()
    parser.feed(html)
    bundles = [urljoin(PORTAL_URL, s) for s in parser.sources
               if urlparse(urljoin(PORTAL_URL, s)).path == BUNDLE_PATH]
    if not bundles:
        raise LoginError("Portal-Bundle nicht gefunden – Lidl hat die Seite geändert")
    status, js = http("GET", bundles[0])
    q = r'("(?:[^"\\]|\\.)*")'
    m = re.search(r'\.commit\("selectClient",\{id:' + q + r',secret:' + q + r'\}\)', js)
    if status != 200 or not m:
        raise LoginError("Client-Konfiguration im Portal nicht gefunden – Lidl hat die Seite geändert")
    return json.loads(m.group(1)), json.loads(m.group(2))


def token_request(state, payload, is_login):
    status, text = http("POST", TOKEN_URL, {
        "client_id": state["client_id"], "client_secret": state["client_secret"], **payload})
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    if status != 200 or not data.get("access_token"):
        raise LoginError(f"Token-Anfrage abgelehnt (HTTP {status})")
    if data.get("needs_multifactor_authentication"):
        raise LoginError("Lidl verlangt Captcha/SMS-Code – bitte einmal normal im Browser einloggen")
    state["access_token"] = data["access_token"]
    state["refresh_token"] = data.get("refresh_token") or state.get("refresh_token")
    state["expires_at"] = time.time() + float(data.get("expires_in", 3600))
    if is_login:
        log("Login erfolgreich")


def login(state):
    user = re.sub(r"[\s/()-]", "", os.environ.get("LIDL_USER", ""))
    user = re.sub(r"^(\+|00)49", "0", user)
    password = os.environ.get("LIDL_PASS", "")
    if not user or not password:
        raise LoginError("LIDL_USER / LIDL_PASS fehlen (in ~/.lidl-refill.env eintragen)")
    if not state.get("client_id"):
        try:
            state["client_id"], state["client_secret"] = discover_client()
        except (LoginError, OSError) as e:
            # The portal blocks some datacenter IPs; the API itself may not.
            log(f"Hinweis: {e} – nutze bekannte Client-Kennung")
            state["client_id"], state["client_secret"] = FALLBACK_CLIENT
    try:
        token_request(state, {"grant_type": "password", "username": user, "password": password,
                              "captcha_code": "", "captcha_token": None}, True)
    except LoginError:
        # The published client credentials may have rotated; rediscover next time.
        state.pop("client_id", None)
        raise


def ensure_token(state, force_refresh=False):
    if not force_refresh and state.get("access_token") and state.get("expires_at", 0) > time.time() + 60:
        return
    if state.get("refresh_token") and state.get("client_id"):
        try:
            token_request(state, {"grant_type": "refresh_token", "refresh_token": state["refresh_token"]}, False)
            return
        except LoginError:
            state.pop("refresh_token", None)
    login(state)


def graphql(state, operation, query, variables):
    status, text = http("POST", GRAPHQL_URL, {"operationName": operation, "variables": variables, "query": query},
                        {"Authorization": "Bearer " + state["access_token"]})
    if status in (401, 403):
        raise AuthRejected()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    return status, payload


def remaining_gb(state):
    for attempt in range(2):
        try:
            status, payload = graphql(state, "consumptions", CONSUMPTIONS_QUERY, {})
            break
        except AuthRejected:
            if attempt:
                raise LoginError("Sitzung wird abgelehnt")
            ensure_token(state, force_refresh=True)
    if status != 200 or not payload or payload.get("errors") or not payload.get("data"):
        raise RuntimeError(f"Verbrauchsabfrage fehlgeschlagen (HTTP {status})")
    rows = payload["data"]["consumptions"]["consumptionsForUnit"]
    now = datetime.now(timezone.utc)
    total, found = 0.0, False
    for row in rows:
        # DATA = tariff bundle, REFILLABLE_DATA = the refill bucket; both count.
        if row.get("type") not in ("DATA", "REFILLABLE_DATA"):
            continue
        exp = row.get("expirationDate")
        if exp:
            try:
                if datetime.fromisoformat(exp.replace("Z", "+00:00")) <= now:
                    continue
            except ValueError:
                pass
        left = float(row.get("left") or 0)
        unit = (row.get("unit") or "GB").upper()
        total += left / 1024 if unit == "MB" else left
        found = True
    if not found:
        raise RuntimeError("Keine gültigen Datenvolumen-Einträge (Periodenwechsel?) – warte")
    return total


OPTIONS_QUERY = """query tariffOptions { tariffoptions { bookableTariffoptions {
  bookableTariffoptions { tariffoptionId name price formattedPrice } } } }"""


def offer_is_free(state, offer_id):
    """Only book if the portal currently lists this option at 0.00 EUR."""
    status, payload = graphql(state, "tariffOptions", OPTIONS_QUERY, {})
    try:
        options = payload["data"]["tariffoptions"]["bookableTariffoptions"]["bookableTariffoptions"]
    except (TypeError, KeyError):
        return None, f"Optionsliste nicht lesbar (HTTP {status})"  # transient, retry next run
    for o in options or []:
        if o.get("tariffoptionId") == offer_id:
            price = o.get("price")
            free = (isinstance(price, (int, float)) and price == 0) or str(o.get("formattedPrice", "")).startswith("0.00")
            return free, f"{o.get('name')} kostet {o.get('formattedPrice') or price}"
    return False, f"{offer_id} ist derzeit nicht buchbar"


def book_refill(state, offer_id):
    status, payload = graphql(state, "bookTariffOptionsDirect", REFILL_MUTATION,
                              {"bookTariffoptionsDirectInput": {"bookTariffoptions": [{"tariffoptionId": offer_id}]}})
    ok = bool(payload and (payload.get("data") or {}).get("bookTariffoptionsDirect", {}).get("success"))
    return status, ok


def run():
    load_env_file()
    dry = os.environ.get("LIDL_DRY_RUN", "1").strip() not in ("0", "false", "no")
    verbose = os.environ.get("LIDL_VERBOSE", "0").strip() in ("1", "true", "yes")
    threshold = float(os.environ.get("LIDL_THRESHOLD", "0.3"))
    offer_id = os.environ.get("LIDL_OFFER_ID", "CCS_92061").strip()
    interactive = sys.stdout.isatty()

    state = load_state()
    now = time.time()
    if state.get("login_blocked_until", 0) > now:
        if verbose or interactive:
            log(f"Login-Pause nach Fehler, noch {int(state['login_blocked_until'] - now) // 60} min")
        return 1

    try:
        ensure_token(state)
        gb = remaining_gb(state)
    except LoginError as e:
        state["login_blocked_until"] = now + LOGIN_BACKOFF
        for k in ("access_token", "refresh_token", "expires_at"):
            state.pop(k, None)
        save_state(state)
        log(f"FEHLER Login: {e}. Nächster Versuch in 30 min.")
        return 1
    except Exception as e:
        save_state(state)
        log(f"FEHLER: {e}")
        return 1
    state.pop("login_blocked_until", None)

    if verbose or interactive:
        log(f"{gb:.2f} GB übrig" + (" (Testmodus)" if dry else ""))

    # Follow up on a refill sent earlier.
    pending = state.get("pending")
    if pending:
        if gb >= pending["gb_before"] + 0.5:
            log(f"Nachbuchung bestätigt: {pending['gb_before']:.2f} → {gb:.2f} GB")
            state.pop("pending")
            state["refill_fails"] = 0
        elif now - pending["at"] < REFILL_SETTLE:
            save_state(state)
            return 0
        else:
            state.pop("pending")
            state["refill_fails"] = state.get("refill_fails", 0) + 1
            log(f"WARNUNG: Nachbuchung nicht bestätigt ({state['refill_fails']}/{REFILL_MAX_FAILS})")
            if state["refill_fails"] >= REFILL_MAX_FAILS:
                state["refill_paused_until"] = now + REFILL_FAIL_PAUSE
                state["refill_fails"] = 0
                log("Nachbuchen pausiert für 60 min – im Kundenkonto prüfen, ob die Option noch verfügbar ist")

    if gb < threshold and not state.get("pending") and state.get("refill_paused_until", 0) <= now:
        if dry:
            if now - state.get("dry_logged_at", 0) >= DRY_LOG_INTERVAL:
                log(f"{gb:.2f} GB < {threshold} GB – würde jetzt nachbuchen ({offer_id}). Testmodus aktiv.")
                state["dry_logged_at"] = now
        else:
            free, info = offer_is_free(state, offer_id)
            if free is None:
                log(f"Nachbuchung verschoben: {info}. Neuer Versuch beim nächsten Lauf.")
                save_state(state)
                return 1
            if not free:
                state["refill_paused_until"] = now + REFILL_FAIL_PAUSE
                log(f"NICHT gebucht: {info}. Pause 60 min – Option im Kundenkonto prüfen.")
                save_state(state)
                return 1
            status, ok = book_refill(state, offer_id)
            state["pending"] = {"at": now, "gb_before": gb}
            log(f"{gb:.2f} GB < {threshold} GB – Nachbuchung gesendet ({offer_id}): HTTP {status}, "
                f"{'success' if ok else 'keine Bestätigung, prüfe in 3 min'}")

    save_state(state)
    return 0


def main():
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0  # previous run still active
        return run()


if __name__ == "__main__":
    sys.exit(main())
