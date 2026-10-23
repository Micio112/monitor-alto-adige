#!/usr/bin/env python3
"""Monitor Alto Adige – notifiche Telegram."""
import hashlib
import re
import json
import os
import sys
import traceback
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUBS_URL = os.environ.get("SUBS_URL")
SUBS_KEY = os.environ.get("SUBS_KEY")
SITE_URL = (os.environ.get("SITE_URL") or "https://monitor-alto-adige-pub.pages.dev").strip().rstrip("/")

TZ = ZoneInfo("Europe/Rome")
STATE_DIR = Path(".state")
STATE_DIR.mkdir(exist_ok=True)

def html_esc(s):
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))

COMUNI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro"]
LIVELLI_ALLERTA = ["gialla", "arancione", "rossa"]

SOGLIA_TOTALE = 50
SOGLIA_ROSSI = 5
SOGLIA_ARANCIONI = 10
SOGLIA_GIALLI = 15
SOGLIA_VERDI = 30

STAZIONI_NOMI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Silandro", "Dobbiaco"]
STAZIONI_NOMI_DE = ["Bozen", "Meran", "Brixen", "Bruneck", "Schlanders", "Toblach"]
GIORNI = {0: "Lunedì", 1: "Martedì", 2: "Mercoledì", 3: "Giovedì", 4: "Venerdì", 5: "Sabato", 6: "Domenica"}
GIORNI_DE = {0: "Montag", 1: "Dienstag", 2: "Mittwoch", 3: "Donnerstag", 4: "Freitag", 5: "Samstag", 6: "Sonntag"}
CERCHI = {"1": "🔴", "2": "🟠", "3": "🟡", "4": "🟢", "5": "🔵"}

COMUNI_FARM = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro", "Laives"]

# Zone / arterie per report viabilità
def _testo_zona_evento(e):
    """Testo combinato IT+DE per matching zone (place, zone, strada)."""
    parts = [
        e.get("messageZoneDescIt") or "",
        e.get("messageZoneDescDe") or "",
        e.get("placeIt") or "",
        e.get("placeDe") or "",
        e.get("messageStreetInternetDescIt") or "",
        e.get("messageStreetInternetDescDe") or "",
        e.get("messageStreetNr") or "",
    ]
    return " ".join(parts).lower()


# (titolo_it, titolo_de, predicato)
GRUPPI_VIABILITA = [
    ("A22", "A22", lambda e: (e.get("messageStreetNr") or "").upper() == "A22"
     or ("brenner" in _testo_zona_evento(e) and "a22" in _testo_zona_evento(e))),
    ("MEBO / SS38 / SS42", "MEBO / SS38 / SS42", lambda e: any(
        x in ((e.get("messageStreetNr") or "") + " " + _testo_zona_evento(e)).upper()
        for x in ("MEBO", "SS38", "SS42")
    )),
    ("Bolzano", "Bozen", lambda e: any(
        x in _testo_zona_evento(e) for x in ("bolzano", "bozen")
    )),
    ("Merano / Burgraviato", "Meran / Burggrafenamt", lambda e: any(
        x in _testo_zona_evento(e) for x in ("burgraviato", "burggrafenamt", "merano", "meran")
    )),
    ("Bressanone / Val d'Isarco", "Brixen / Eisacktal", lambda e: any(
        x in _testo_zona_evento(e) for x in ("isarco", "eisack", "bressanone", "brixen")
    )),
    ("Brunico / Val Pusteria", "Bruneck / Pustertal", lambda e: any(
        x in _testo_zona_evento(e) for x in ("pusteria", "pustertal", "brunico", "bruneck")
    )),
    ("Vipiteno", "Sterzing", lambda e: any(
        x in _testo_zona_evento(e) for x in ("vipiteno", "sterzing")
    )),
    ("Silandro / Val Venosta", "Schlanders / Vinschgau", lambda e: any(
        x in _testo_zona_evento(e) for x in ("venosta", "vinschgau", "silandro", "schlanders")
    )),
]


def ora_roma():
    return datetime.now(TZ)


def ora_str():
    return ora_roma().strftime("%d/%m/%Y %H:%M")


def nome_giorno(iso_date, lang="it"):
    """Nome del giorno della settimana (IT/DE) da data ISO."""
    if not iso_date:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso_date).replace("Z", "+00:00")[:19])
        mappa = GIORNI_DE if lang == "de" else GIORNI
        return mappa.get(dt.weekday(), str(iso_date)[:10])
    except Exception:
        return str(iso_date)[:10]


def format_data_it(val):
    """Converte date ISO / varie in gg/mm/aaaa (opz. con ora se presente)."""
    if not val:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # già in formato italiano tipo 17/10/2026
    if len(s) >= 10 and s[2] == "/" and s[5] == "/":
        return s[:16] if len(s) > 10 else s[:10]
    try:
        raw = s.replace("Z", "+00:00")
        if "T" in raw or "+" in raw[10:] or raw.endswith("Z"):
            dt = datetime.fromisoformat(raw[:25] if len(raw) > 25 else raw)
        else:
            # 2026-10-17 o 2026-10-17 08:00
            dt = datetime.fromisoformat(raw[:19].replace(" ", "T"))
        if dt.hour or dt.minute or "T" in s or " " in s[10:]:
            return dt.strftime("%d/%m/%Y %H:%M")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        # prova solo data YYYY-MM-DD
        try:
            if len(s) >= 10 and s[4] == "-" and s[7] == "-":
                y, m, d = s[:10].split("-")
                return f"{d}/{m}/{y}"
        except Exception:
            pass
        return s


def is_weekend_or_holiday():
    d = ora_roma().date()
    if d.weekday() >= 5:
        return True
    # Festività fisse IT (semplificato)
    fixed = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}
    return (d.month, d.day) in fixed


# ---------- stato anti-spam ----------
def _state_path(name):
    return STATE_DIR / f"{name}.json"


def load_state(name, default=None):
    p = _state_path(name)
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def save_state(name, data):
    _state_path(name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def was_sent(event_key, ttl_hours=36):
    """True se già notificato di recente (non marca)."""
    st = load_state("events", {})
    now = ora_roma().timestamp()
    st = {k: v for k, v in st.items() if now - v < max(ttl_hours, 48) * 3600}
    save_state("events", st)
    ts = st.get(event_key)
    if ts is None:
        return False
    return (now - ts) < ttl_hours * 3600


def mark_sent(event_key):
    """Segna un evento come già notificato."""
    st = load_state("events", {})
    now = ora_roma().timestamp()
    st = {k: v for k, v in st.items() if now - v < 48 * 3600}
    st[event_key] = now
    save_state("events", st)


def already_sent(event_key, ttl_hours=36):
    """Compatibilità: True se già inviato; altrimenti marca e ritorna False."""
    if was_sent(event_key, ttl_hours=ttl_hours):
        return True
    mark_sent(event_key)
    return False


def _coda_rate_ok(max_n=3, window_hours=2):
    """Max max_n notifiche code nella finestra (anti-spam globale)."""
    st = load_state("coda_rate", {"ts": []})
    now = ora_roma().timestamp()
    ts = [x for x in st.get("ts", []) if now - x < window_hours * 3600]
    return len(ts) < max_n


def _coda_rate_mark():
    st = load_state("coda_rate", {"ts": []})
    now = ora_roma().timestamp()
    ts = [x for x in st.get("ts", []) if now - x < 6 * 3600]
    ts.append(now)
    save_state("coda_rate", {"ts": ts})


def has_daily_flag(name):
    """True se il report è già stato inviato oggi."""
    st = load_state("daily", {})
    return st.get(name) == str(ora_roma().date())


def set_daily_flag(name):
    """Segna il report come inviato oggi (chiamare solo dopo invio riuscito)."""
    st = load_state("daily", {})
    today = str(ora_roma().date())
    st[name] = today
    st = {k: v for k, v in st.items() if v == today}
    save_state("daily", st)


def daily_flag(name):
    """Compatibilità: True se già fatto oggi; altrimenti marca e ritorna False.
    Preferire has_daily_flag + set_daily_flag dopo invio."""
    if has_daily_flag(name):
        return True
    set_daily_flag(name)
    return False



def _spezza_messaggio(testo, max_len=3500):
    """Telegram accetta max 4096 caratteri: spezza su a-capo."""
    testo = (testo or "").strip()
    if len(testo) <= max_len:
        return [testo] if testo else []
    parti = []
    nl = chr(10)
    while testo:
        if len(testo) <= max_len:
            parti.append(testo)
            break
        taglio = testo.rfind(nl, 0, max_len)
        if taglio < max_len // 3:
            taglio = max_len
        pezzo = testo[:taglio].rstrip()
        if pezzo:
            parti.append(pezzo)
        testo = testo[taglio:].lstrip()
    return parti


def prendi_destinatari():
    """Lista di dict {id, lang} (lang: it|de)."""
    by_id = {}
    if CHAT_ID:
        by_id[str(CHAT_ID)] = {"id": str(CHAT_ID), "lang": "it"}
    if SUBS_URL and SUBS_KEY:
        try:
            r = requests.get(SUBS_URL, params={"key": SUBS_KEY}, timeout=15)
            if r.ok:
                for u in r.json() if isinstance(r.json(), list) else []:
                    if isinstance(u, dict):
                        cid = str(u.get("id") or "").strip()
                        lang = "de" if str(u.get("lang") or "").lower() == "de" else "it"
                    else:
                        cid = str(u).strip()
                        lang = "it"
                    if cid:
                        by_id[cid] = {"id": cid, "lang": lang}
        except Exception as e:
            print("Errore iscritti:", e)
    dest = list(by_id.values())
    print("Destinatari:", [(d["id"], d["lang"]) for d in dest])
    return dest


def nome_ospedale(nome, lang="it"):
    if not nome:
        return nome
    if "/" in str(nome):
        parts = [x.strip() for x in str(nome).split("/")]
        if lang == "de" and len(parts) > 1:
            return parts[1]
        return parts[0]
    return nome


def _append_site(s):
    s = s or ""
    if SITE_URL and SITE_URL not in s:
        return s.rstrip() + "\n\n🌐 " + SITE_URL
    return s


def invia_messaggio(testo, testo_de=None):
    """Invia IT agli utenti it, DE (o IT) agli utenti de.
    testo può essere str oppure dict {"it":..., "de":...}.
    """
    if not TOKEN:
        print("Token mancante – controlla secret TELEGRAM_TOKEN")
        return False
    if isinstance(testo, dict):
        testo_it = testo.get("it") or testo.get("de") or ""
        testo_de = testo.get("de") or testo.get("it") or ""
    else:
        testo_it = testo or ""
        if testo_de is None:
            testo_de = testo_it
    if not (testo_it or "").strip() and not (testo_de or "").strip():
        print("Messaggio vuoto – non invio")
        return False
    dest = prendi_destinatari()
    if not dest:
        print("Nessun destinatario – controlla TELEGRAM_CHAT_ID / SUBS_URL / SUBS_KEY")
        return False
    testo_it = _append_site(testo_it)
    testo_de = _append_site(testo_de)
    print(f"Invio a {len(dest)} destinatari · IT {len(testo_it)} car. · DE {len(testo_de)} car.")
    inviati = 0
    for d in dest:
        body = testo_de if d.get("lang") == "de" else testo_it
        parti = _spezza_messaggio(body)
        if not parti:
            print("  skip", d["id"], "corpo vuoto")
            continue
        for pezzo in parti:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                    data={
                        "chat_id": d["id"],
                        "text": pezzo,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=20,
                )
                if r.ok:
                    inviati += 1
                    print(f"  OK {d['id']} ({d.get('lang','it')})")
                else:
                    print("  Errore invio", d["id"], r.status_code, r.text[:300])
                    # ritenta senza HTML
                    plain = (
                        pezzo.replace("<b>", "").replace("</b>", "")
                        .replace("<i>", "").replace("</i>", "")
                        .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                    )
                    r2 = requests.post(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={
                            "chat_id": d["id"],
                            "text": plain,
                            "disable_web_page_preview": True,
                        },
                        timeout=20,
                    )
                    if r2.ok:
                        inviati += 1
                        print(f"  OK plain {d['id']}")
                    else:
                        print("  Errore plain", d["id"], r2.text[:200])
            except Exception as e:
                print("  Eccezione invio", d["id"], e)
    print(f"Messaggi consegnati: {inviati}")

    # Web push disattivato
    # try:
    #     invia_web_push(testo_it)
    # except Exception as e:
    #     print("Web push errore:", e)

    return inviati > 0


def invia_web_push(testo):
    """Invia una web push a tutti gli iscritti salvati nel Worker."""
    print("=== Inizio invio web push ===")
    if not SUBS_URL or not SUBS_KEY:
        print("SUBS_URL o SUBS_KEY mancanti – skip")
        return
    try:
        from pywebpush import webpush
        print("pywebpush importato correttamente")
    except ImportError:
        print("pywebpush non installato – skip web push")
        return

    # Recupera le subscription dal Worker
    # Costruisce l'URL in modo sicuro
    base = SUBS_URL.rstrip("/")
    if base.endswith("/subs"):
        list_url = base[:-5] + "/push/list"
    elif base.endswith("/subscribers"):
        list_url = base[:-12] + "/push/list"
    else:
        # fallback: aggiungi /push/list alla root del worker
        from urllib.parse import urlparse
        parsed = urlparse(base)
        list_url = f"{parsed.scheme}://{parsed.netloc}/push/list"
    print("List URL:", list_url)

    try:
        r = requests.get(
            list_url,
            headers={"Authorization": f"Bearer {SUBS_KEY}"},
            timeout=15,
        )
        print("Status recupero subs:", r.status_code)
        if not r.ok:
            print("Impossibile recuperare push subscriptions:", r.status_code, r.text[:200])
            return
        subscriptions = r.json() if isinstance(r.json(), list) else []
        print(f"Trovate {len(subscriptions)} subscription")
    except Exception as e:
        print("Errore recupero push subs:", e)
        return

    if not subscriptions:
        print("Nessuna push subscription – fine")
        return

    vapid_private = os.environ.get("VAPID_PRIVATE_KEY")
    if not vapid_private:
        print("VAPID_PRIVATE_KEY mancante – skip web push")
        return
    print("VAPID_PRIVATE_KEY presente (lunghezza", len(vapid_private), ")")

    # Testo pulito per la notifica
    plain = (
        (testo or "")
        .replace("<b>", "").replace("</b>", "")
        .replace("<i>", "").replace("</i>", "")
        .replace("&", "&").replace("<", "<").replace(">", ">")
    )
    lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
    title = lines[0][:60] if lines else "Monitor Alto Adige"
    body = "\n".join(lines[1:4])[:180] if len(lines) > 1 else title
    print("Title:", title)
    print("Body:", body[:80])

    payload = json.dumps({"title": title, "body": body, "url": SITE_URL or "/"})

    ok = 0
    for i, sub in enumerate(subscriptions):
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": "mailto:monitor.altoadige@gmail.com"},
            )
            ok += 1
            print(f"  Push {i+1} OK")
        except Exception as e:
            print(f"  Push {i+1} fallita:", str(e)[:200])
    print(f"=== Web push inviate: {ok}/{len(subscriptions)} ===")


def invia_pacchetto(messaggi):
    """messaggi: lista di str o dict {it, de}."""
    if not messaggi:
        return False
    dest = prendi_destinatari()
    if not dest:
        return False
    ok_any = False
    for lang in ("it", "de"):
        ids = [d["id"] for d in dest if d.get("lang", "it") == lang]
        if not ids:
            continue
        parti = []
        for m in messaggi:
            if isinstance(m, dict):
                parti.append(m.get(lang) or m.get("it") or "")
            else:
                parti.append(m)
        header = (
            f"🔔 <b>Monitor Südtirol</b>\n<i>{ora_str()}</i>\n\n"
            if lang == "de"
            else f"🔔 <b>Monitor Alto Adige</b>\n<i>{ora_str()}</i>\n\n"
        )
        testo = header + "\n\n————————\n\n".join(p for p in parti if p)
        body = _append_site(testo)
        ok = True
        for cid in ids:
            for pezzo in _spezza_messaggio(body):
                try:
                    r = requests.post(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        data={
                            "chat_id": cid,
                            "text": pezzo,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                        timeout=20,
                    )
                    if not r.ok:
                        ok = False
                        print("Errore invio", cid, r.text[:200])
                except Exception as e:
                    ok = False
                    print("Eccezione invio", cid, e)
        if ok:
            ok_any = True
    if ok_any:
        for m in messaggi:
            if isinstance(m, dict):
                for k in m.get("_keys") or []:
                    mark_sent(k)
        # Web push disattivato
        # try:
        #     if testo_it:
        #         invia_web_push(testo_it)
        # except Exception as e:
        #     print("Web push da pacchetto errore:", e)
    return ok_any


# ========== PRONTO SOCCORSO ==========
def prendi_pronto_soccorso():
    try:
        dati = requests.get("https://dati.retecivica.bz.it/services/PS_Queue/json", timeout=15).json()
        ospedali = {}
        for r in dati:
            nome = r["HOSPITAL_DESCRIPTION"]
            if nome not in ospedali:
                ospedali[nome] = {"totale": 0, "codici": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}}
            q = int(r.get("QUEUE_LENGTH") or 0)
            cod = str(r.get("TRIAGE_CODE") or "")
            ospedali[nome]["totale"] += q
            if cod in ospedali[nome]["codici"]:
                ospedali[nome]["codici"][cod] += q
        return ospedali
    except Exception as e:
        print("Errore PS:", e)
        return {}


def formatta_ospedale(nome, d, lang="it"):
    c = d["codici"]
    n = nome_ospedale(nome, lang)
    if lang == "de":
        return "\n".join([
            f"🏥 <b>EH {n}</b>",
            f"Gesamt wartend: <b>{d['totale']}</b>",
            "",
            f"{CERCHI['1']} Rot: <b>{c['1']}</b>",
            f"{CERCHI['2']} Orange: <b>{c['2']}</b>",
            f"{CERCHI['3']} Gelb: <b>{c['3']}</b>",
            f"{CERCHI['4']} Grün: <b>{c['4']}</b>",
            f"{CERCHI['5']} Blau: <b>{c['5']}</b>",
            "",
            "⏳ <b>Lange Wartezeiten</b>",
        ])
    return "\n".join([
        f"🏥 <b>PS {n}</b>",
        f"Totale in attesa: <b>{d['totale']}</b>",
        "",
        f"{CERCHI['1']} Rosso: <b>{c['1']}</b>",
        f"{CERCHI['2']} Arancione: <b>{c['2']}</b>",
        f"{CERCHI['3']} Giallo: <b>{c['3']}</b>",
        f"{CERCHI['4']} Verde: <b>{c['4']}</b>",
        f"{CERCHI['5']} Blu: <b>{c['5']}</b>",
        "",
        "⏳ <b>Tempi di attesa lunghi</b>",
    ])


def controlla_ps():
    """Antispam PS raffinato:
    - reset stato quando l'ospedale torna sotto tutte le soglie
    - nuovo tipo di motivo (es. entrano i rossi) → cooldown breve (20 min)
    - aumento significativo dei numeri (rossi +2, arancioni +4, totale +15) → 30 min
    - stessa situazione: 90 min
    - numeri quasi identici: 3 ore
    """
    messaggi = []
    now = ora_roma().timestamp()
    st = load_state("ps_last", {})
    # pulizia voci > 48h
    st = {
        k: v for k, v in st.items()
        if isinstance(v, dict) and now - float(v.get("ts") or 0) < 48 * 3600
    }

    # Priorità motivi (dal più grave)
    PRIORITA = ["rossi", "arancioni", "gialli", "totale", "verdi"]

    for nome, d in prendi_pronto_soccorso().items():
        c = d["codici"]
        motivi = []
        tipi = []
        if c["1"] >= SOGLIA_ROSSI:
            motivi.append(f"rossi ≥ {SOGLIA_ROSSI} ({c['1']})")
            tipi.append("rossi")
        if c["2"] >= SOGLIA_ARANCIONI:
            motivi.append(f"arancioni ≥ {SOGLIA_ARANCIONI} ({c['2']})")
            tipi.append("arancioni")
        if c["3"] >= SOGLIA_GIALLI:
            motivi.append(f"gialli ≥ {SOGLIA_GIALLI} ({c['3']})")
            tipi.append("gialli")
        if d["totale"] >= SOGLIA_TOTALE:
            motivi.append(f"totale ≥ {SOGLIA_TOTALE} ({d['totale']})")
            tipi.append("totale")
        if c["4"] >= SOGLIA_VERDI:
            motivi.append(f"verdi ≥ {SOGLIA_VERDI} ({c['4']})")
            tipi.append("verdi")

        # Nessuna criticità → cancella stato precedente (prossimo superamento = nuovo)
        if not motivi:
            if nome in st:
                del st[nome]
            continue

        # Ordina motivi per priorità
        motivi = sorted(motivi, key=lambda m: PRIORITA.index(m.split()[0]) if m.split()[0] in PRIORITA else 99)
        tipi = sorted(tipi, key=lambda t: PRIORITA.index(t) if t in PRIORITA else 99)

        # Severity più fine: peso + eccesso rispetto alla soglia
        severity = 0
        if "rossi" in tipi:
            severity += 8 + max(0, c["1"] - SOGLIA_ROSSI)
        if "arancioni" in tipi:
            severity += 4 + max(0, (c["2"] - SOGLIA_ARANCIONI) // 2)
        if "gialli" in tipi:
            severity += 2 + max(0, (c["3"] - SOGLIA_GIALLI) // 3)
        if "totale" in tipi:
            severity += 1 + max(0, (d["totale"] - SOGLIA_TOTALE) // 10)
        if "verdi" in tipi:
            severity += 1 + max(0, (c["4"] - SOGLIA_VERDI) // 5)

        last = st.get(nome) or {}
        last_ts = float(last.get("ts") or 0)
        age_h = (now - last_ts) / 3600 if last_ts else 999.0
        last_tipi = set(last.get("tipi") or [])
        cur_tipi = set(tipi)
        last_counts = last.get("counts") or {}

        # Nuovi tipi di motivo rispetto all'ultima notifica
        nuovi_tipi = cur_tipi - last_tipi
        # Aumenti significativi dei numeri
        delta_rossi = c["1"] - int(last_counts.get("1") or 0)
        delta_arancioni = c["2"] - int(last_counts.get("2") or 0)
        delta_totale = d["totale"] - int(last_counts.get("totale") or 0)
        aumento_significativo = (
            delta_rossi >= 2 or delta_arancioni >= 4 or delta_totale >= 15
        )
        numeri_quasi_identici = (
            abs(delta_rossi) <= 1 and abs(delta_arancioni) <= 2 and abs(delta_totale) <= 5
            and cur_tipi == last_tipi
        )

        if last_ts:
            if nuovi_tipi and age_h < 0.33:
                print(f"PS saltato (nuovo motivo ma <20 min): {nome} {nuovi_tipi}")
                continue
            if aumento_significativo and age_h < 0.5:
                print(f"PS saltato (aumento ma <30 min): {nome}")
                continue
            if not nuovi_tipi and not aumento_significativo:
                if numeri_quasi_identici and age_h < 3:
                    print(f"PS saltato (quasi identico <3h): {nome}")
                    continue
                if age_h < 1.5:
                    print(f"PS saltato (stessa situazione <90 min): {nome} ({', '.join(tipi)})")
                    continue

        # Aggiorna stato
        st[nome] = {
            "ts": now,
            "severity": severity,
            "tipi": list(tipi),
            "counts": {
                "totale": d["totale"],
                "1": c["1"], "2": c["2"], "3": c["3"], "4": c["4"], "5": c["5"],
            },
        }

        # Messaggio
        motivi_de = []
        if "rossi" in tipi:
            motivi_de.append(f"Rot ≥ {SOGLIA_ROSSI} ({c['1']})")
        if "arancioni" in tipi:
            motivi_de.append(f"Orange ≥ {SOGLIA_ARANCIONI} ({c['2']})")
        if "gialli" in tipi:
            motivi_de.append(f"Gelb ≥ {SOGLIA_GIALLI} ({c['3']})")
        if "totale" in tipi:
            motivi_de.append(f"Gesamt ≥ {SOGLIA_TOTALE} ({d['totale']})")
        if "verdi" in tipi:
            motivi_de.append(f"Grün ≥ {SOGLIA_VERDI} ({c['4']})")

        testo_it = formatta_ospedale(nome, d, "it") + "\n⚠️ Motivo: " + ", ".join(motivi)
        testo_de = formatta_ospedale(nome, d, "de") + "\n⚠️ Grund: " + ", ".join(motivi_de)

        if nuovi_tipi and last_ts:
            testo_it += "\n⬆️ Nuova criticità"
            testo_de += "\n⬆️ Neue Kritikalität"
        elif aumento_significativo and last_ts:
            testo_it += "\n⬆️ Aumento significativo"
            testo_de += "\n⬆️ Deutlicher Anstieg"

        messaggi.append({"it": testo_it, "de": testo_de})
        print(f"PS notifica: {nome} tipi={tipi} sev={severity} nuovi={nuovi_tipi}")

    save_state("ps_last", st)
    return messaggi

# ========== ALLERTE METEO ufficiali CFP (oggi, solo non-verdi) ==========
def _active_warning_level(levels):
    """Livello attivo = quello con comuni assegnati; altrimenti VERDE."""
    active = None
    for lv in levels or []:
        n = len(lv.get("municipalities") or [])
        if n > 0:
            if active is None or (lv.get("level") or 0) > (active.get("level") or 0):
                active = lv
    if active is None:
        for lv in levels or []:
            if str(lv.get("identifier") or "").upper() in ("VERDE", "GREEN"):
                return lv
        return (levels or [None])[0]
    return active


def prendi_allerte_nazionali():
    """Solo fonte ufficiale Provincia (CFP risk_potential).

    Regole:
    - max 1 notifica / fenomeno / livello / 24h
    - nuova allerta o salita di livello → notifica sempre (se non già inviata nelle 24h)
    - ritorno a verde → avviso di normalizzazione (1 volta)
    """
    try:
        oggi = ora_roma().date().isoformat()
        url = f"https://cms-lwz.afbs.it/fileadmin/export/risk_potential_{oggi}.json?language=it"
        url_de = f"https://cms-lwz.afbs.it/fileadmin/export/risk_potential_{oggi}.json?language=de"
        dati = requests.get(url, timeout=20).json()
        try:
            dati_de = requests.get(url_de, timeout=15).json()
        except Exception:
            dati_de = dati
        ph_de = {p.get("identifier"): p for p in (dati_de.get("phenomena") or [])}

        prev = load_state("cfp_levels", {})  # {identifier: int level}
        current = {}
        out = []
        nl = chr(10)
        link = "https://allerte.provincia.bz.it/it/reports/risk-potential/list"
        link_de = "https://warnungen.provinz.bz.it/de/reports/risk-potential/list"
        data_fmt = f"{oggi[8:10]}/{oggi[5:7]}/{oggi[:4]}"

        for ph in dati.get("phenomena") or []:
            ident_ph = ph.get("identifier") or ""
            if not ident_ph or ident_ph == "RISK_TEMPORALE":
                # i temporali hanno funzione dedicata
                active = _active_warning_level(ph.get("warningLevels") or [])
                liv = int(active.get("level") or 0) if active else 0
                current[ident_ph] = liv
                continue

            active = _active_warning_level(ph.get("warningLevels") or [])
            liv = int(active.get("level") or 0) if active else 0
            current[ident_ph] = liv
            prev_liv = int(prev.get(ident_ph, 0) or 0)

            titolo_it = (ph.get("title") or {}).get("it") or ident_ph
            titolo_de = (ph_de.get(ident_ph, ph).get("title") or {}).get("de") or titolo_it

            # --- Normalizzazione: era >0 e ora 0 ---
            if liv <= 0 and prev_liv > 0:
                key = f"cfp-norm-{ident_ph}-{oggi}"
                if not was_sent(key, ttl_hours=24):
                    out.append({
                        "it": (
                            f"🟢 <b>Fine allerta</b>{nl}"
                            f"<b>{html_esc(str(titolo_it).upper())}</b>{nl}"
                            f"Il livello è tornato a <b>verde</b>.{nl}"
                            f"📅 {data_fmt}{nl}"
                            f"🔗 {link}"
                        ),
                        "de": (
                            f"🟢 <b>Entwarnung</b>{nl}"
                            f"<b>{html_esc(str(titolo_de).upper())}</b>{nl}"
                            f"Die Stufe ist wieder <b>grün</b>.{nl}"
                            f"📅 {data_fmt}{nl}"
                            f"🔗 {link_de}"
                        ),
                        "_keys": [key],
                    })
                    print(f"Normalizzazione CFP: {ident_ph}")
                continue

            if liv <= 0:
                continue

            ident_lv = str((active or {}).get("identifier") or "").upper()
            colore = {"GIALLO": "gialla", "GIALLA": "gialla", "ARANCIONE": "arancione",
                      "ROSSO": "rossa", "ROSSA": "rossa"}.get(ident_lv, "")
            if colore not in LIVELLI_ALLERTA:
                # mappa da livello numerico
                colore = {1: "gialla", 2: "arancione", 3: "rossa"}.get(liv, "")
            if colore not in LIVELLI_ALLERTA:
                continue

            # Nuova o salita di livello: key include livello
            key = f"cfp-allerta-{ident_ph}-L{liv}-{oggi}"
            if was_sent(key, ttl_hours=24):
                continue

            sott_it = ((active or {}).get("title") or {}).get("it") or ""
            sott_de = ((active or {}).get("title") or {}).get("de") or sott_it
            emoji = {"gialla": "🟡", "arancione": "🟠", "rossa": "🔴"}.get(colore, "⚠️")
            col_de = {"gialla": "Gelb", "arancione": "Orange", "rossa": "Rot"}.get(colore, colore)
            extra = ""
            if prev_liv > 0 and liv > prev_liv:
                extra = f"{nl}⬆️ Livello aumentato (prima: {prev_liv})"
                extra_de = f"{nl}⬆️ Stufe erhöht (vorher: {prev_liv})"
            elif prev_liv == 0:
                extra = f"{nl}🆕 Nuova allerta"
                extra_de = f"{nl}🆕 Neue Warnung"
            else:
                extra = ""
                extra_de = ""

            # Nome fenomeno in MAIUSCOLO + grassetto
            nome_it = html_esc(str(titolo_it).upper())
            nome_de = html_esc(str(titolo_de).upper())
            out.append({
                "it": (
                    f"{emoji} <b>Allerta {colore.capitalize()}</b>{nl}"
                    f"<b>{nome_it}</b>{nl}"
                    + (sott_it + nl if sott_it else "")
                    + extra + (nl if extra else "")
                    + f"📅 {data_fmt}{nl}"
                    + f"🔗 {link}"
                ),
                "de": (
                    f"{emoji} <b>Wetterwarnung {col_de}</b>{nl}"
                    f"<b>{nome_de}</b>{nl}"
                    + (sott_de + nl if sott_de else "")
                    + extra_de + (nl if extra_de else "")
                    + f"📅 {data_fmt}{nl}"
                    + f"🔗 {link_de}"
                ),
                "_keys": [key],
            })
            print(f"Allerta CFP: {titolo_it} → L{liv} {ident_lv}")

        save_state("cfp_levels", current)
        return out
    except Exception as e:
        print("Errore allerte CFP:", e)
        return []


# ========== TEMPORALI (solo Provincia CFP, più accurato) ==========
def controlla_temporali():
    """Temporali solo da RISK_TEMPORALE ufficiale.

    - livello > 0 con comuni → notifica
    - max 1 / livello / 24h
    - normalizzazione a verde se prima era attivo
    """
    messaggi = []
    try:
        oggi = ora_roma().date().isoformat()
        url = f"https://cms-lwz.afbs.it/fileadmin/export/risk_potential_{oggi}.json?language=it"
        url_de = f"https://cms-lwz.afbs.it/fileadmin/export/risk_potential_{oggi}.json?language=de"
        dati = requests.get(url, timeout=15).json()
        try:
            dati_de = requests.get(url_de, timeout=12).json()
        except Exception:
            dati_de = dati
        ph_de_map = {p.get("identifier"): p for p in (dati_de.get("phenomena") or [])}
        prev = load_state("cfp_temporale", {"level": 0})
        prev_liv = int(prev.get("level") or 0)
        liv = 0
        active = None
        ph = None
        for p in dati.get("phenomena") or []:
            if p.get("identifier") != "RISK_TEMPORALE":
                continue
            ph = p
            active = _active_warning_level(p.get("warningLevels") or [])
            liv = int(active.get("level") or 0) if active else 0
            break

        save_state("cfp_temporale", {"level": liv, "day": oggi})
        nl = chr(10)
        data_fmt = f"{oggi[8:10]}/{oggi[5:7]}/{oggi[:4]}"
        link = "https://allerte.provincia.bz.it/it/reports/risk-potential/list"
        link_de = "https://warnungen.provinz.bz.it/de/reports/risk-potential/list"

        if liv <= 0:
            if prev_liv > 0:
                key = f"temporale-norm-{oggi}"
                if not was_sent(key, ttl_hours=24):
                    messaggi.append({
                        "it": (
                            f"🟢 <b>Fine allerta temporali</b>{nl}"
                            f"Il rischio temporali è tornato a <b>verde</b>.{nl}"
                            f"📅 {data_fmt}{nl}🔗 {link}"
                        ),
                        "de": (
                            f"🟢 <b>Entwarnung Gewitter</b>{nl}"
                            f"Die Gewitterwarnung ist wieder <b>grün</b>.{nl}"
                            f"📅 {data_fmt}{nl}🔗 {link_de}"
                        ),
                        "_keys": [key],
                    })
                    print("Normalizzazione temporali")
            return messaggi

        # livello attivo giallo+
        ident = str((active or {}).get("identifier") or "").upper()
        colore = {"GIALLO": "gialla", "GIALLA": "gialla", "ARANCIONE": "arancione",
                  "ROSSO": "rossa", "ROSSA": "rossa"}.get(ident) or {1: "gialla", 2: "arancione", 3: "rossa"}.get(liv, "gialla")
        key = f"temporale-cfp-L{liv}-{oggi}"
        if was_sent(key, ttl_hours=24):
            return messaggi

        titolo = (ph.get("title") or {}).get("it") if ph else "Temporali forti"
        titolo = titolo or "Temporali forti"
        titolo_de = (ph_de_map.get("RISK_TEMPORALE", ph or {}).get("title") or {}).get("de") if ph else "Starke Gewitter"
        titolo_de = titolo_de or "Starke Gewitter"
        sott = ((active or {}).get("title") or {}).get("it") or ""
        sott_de = ((active or {}).get("title") or {}).get("de") or sott
        n_com = len((active or {}).get("municipalities") or [])
        emoji = {"gialla": "🟡", "arancione": "🟠", "rossa": "🔴"}.get(colore, "⛈️")
        col_de = {"gialla": "Gelb", "arancione": "Orange", "rossa": "Rot"}.get(colore, colore)

        nome_it = html_esc(str(titolo).upper())
        nome_de = html_esc(str(titolo_de).upper())
        messaggi.append({
            "it": (
                f"⛈️ {emoji} <b>Temporali – allerta {colore}</b>{nl}"
                f"<b>{nome_it}</b>{nl}"
                + (sott + nl if sott else "")
                + (f"Comuni interessati: {n_com}{nl}" if n_com else "")
                + f"📅 {data_fmt}{nl}🔗 {link}"
            ),
            "de": (
                f"⛈️ {emoji} <b>Gewitter – Warnung {col_de}</b>{nl}"
                f"<b>{nome_de}</b>{nl}"
                + (sott_de + nl if sott_de else "")
                + (f"Betroffene Gemeinden: {n_com}{nl}" if n_com else "")
                + f"📅 {data_fmt}{nl}🔗 {link_de}"
            ),
            "_keys": [key],
        })
        print(f"Temporale CFP L{liv} {ident} comuni={n_com}")
    except Exception as e:
        print("Errore temporali CFP:", e)
    return messaggi


# ========== METEO ==========
def formatta_stazioni(station_data, lang="it"):
    if not station_data:
        return ""
    nomi = STAZIONI_NOMI_DE if lang == "de" else STAZIONI_NOMI
    label = "Station" if lang == "de" else "Stazione"
    righe = []
    for i, st in enumerate(station_data):
        nome = nomi[i] if i < len(nomi) else f"{label} {i+1}"
        sym = (st.get("symbol") or {}).get("description", "—")
        tmin, tmax = st.get("min"), st.get("max")
        if tmin is not None and tmax is not None:
            righe.append(f"• <b>{nome}</b>: {sym} · {tmin}° / {tmax}°")
        else:
            righe.append(f"• <b>{nome}</b>: {sym}")
    return chr(10).join(righe)


def _bollettino_meteo_lang(dati, quando, lang="it"):
    nl = chr(10)
    oggi = dati.get("today", {})
    domani = dati.get("tomorrow", {})
    if lang == "de":
        link = "https://wetter.provinz.bz.it/de"
        if quando in ("sera", "domani", "evening"):
            testo = (
                f"🌙 <b>Wetter morgen – Südtirol</b>{nl}"
                f"<i>{ora_str()}</i>{nl}{nl}"
                f"<b>{domani.get('title', 'Morgen')}</b>{nl}"
                f"{domani.get('weather', '')}{nl}{domani.get('temperatures', '')}{nl}"
            )
            if domani.get("stationData"):
                testo += f"{nl}📍 <b>Gemeinden</b>{nl}" + formatta_stazioni(domani["stationData"], "de") + nl
            testo += f"{nl}🔗 {link}"
            return testo
        testo = (
            f"🌤 <b>Wetterbericht Südtirol</b>{nl}"
            f"<i>{ora_str()}</i>{nl}{nl}"
            f"<b>Heute – {oggi.get('title', '—')}</b>{nl}"
            f"{oggi.get('weather', '')}{nl}{oggi.get('temperatures', '')}{nl}"
        )
        if oggi.get("stationData"):
            testo += f"{nl}📍 <b>Gemeinden (heute)</b>{nl}" + formatta_stazioni(oggi["stationData"], "de") + nl
        forecasts = dati.get("dayForecasts") or []
        if forecasts:
            testo += f"{nl}<b>Prognose nächste Tage</b>{nl}"
            for g in forecasts:
                giorno = nome_giorno(g.get("date"), "de")
                data_fmt = format_data_it(g.get("date"))
                etichetta = giorno if giorno != "—" else data_fmt
                if data_fmt and data_fmt not in etichetta:
                    etichetta = f"{giorno} {data_fmt}".strip()
                desc = (g.get("symbol") or {}).get("description", "—")
                tmax, tmin = g.get("tempMax") or {}, g.get("tempMin") or {}
                testo += (
                    f"• <b>{etichetta}</b>: {desc} "
                    f"(max {tmax.get('min', '?')}–{tmax.get('max', '?')}° / "
                    f"min {tmin.get('min', '?')}–{tmin.get('max', '?')}°)" + nl
                )
        testo += f"{nl}🔗 {link}"
        return testo
    # IT
    link = "https://meteo.provincia.bz.it"
    if quando in ("sera", "domani", "evening"):
        testo = (
            f"🌙 <b>Meteo di domani – Alto Adige</b>{nl}"
            f"<i>{ora_str()}</i>{nl}{nl}"
            f"<b>{domani.get('title', 'Domani')}</b>{nl}"
            f"{domani.get('weather', '')}{nl}{domani.get('temperatures', '')}{nl}"
        )
        if domani.get("stationData"):
            testo += f"{nl}📍 <b>Comuni</b>{nl}" + formatta_stazioni(domani["stationData"], "it") + nl
        testo += f"{nl}🔗 {link}"
        return testo
    testo = (
        f"🌤 <b>Bollettino Meteo Alto Adige</b>{nl}"
        f"<i>{ora_str()}</i>{nl}{nl}"
        f"<b>Oggi – {oggi.get('title', '—')}</b>{nl}"
        f"{oggi.get('weather', '')}{nl}{oggi.get('temperatures', '')}{nl}"
    )
    if oggi.get("stationData"):
        testo += f"{nl}📍 <b>Comuni (oggi)</b>{nl}" + formatta_stazioni(oggi["stationData"], "it") + nl
    forecasts = dati.get("dayForecasts") or []
    if forecasts:
        testo += f"{nl}<b>Prognosi prossimi giorni</b>{nl}"
        for g in forecasts:
            giorno = nome_giorno(g.get("date"), "it")
            data_fmt = format_data_it(g.get("date"))
            etichetta = giorno if giorno != "—" else data_fmt
            if data_fmt and data_fmt not in etichetta:
                etichetta = f"{giorno} {data_fmt}".strip()
            desc = (g.get("symbol") or {}).get("description", "—")
            tmax, tmin = g.get("tempMax") or {}, g.get("tempMin") or {}
            testo += (
                f"• <b>{etichetta}</b>: {desc} "
                f"(max {tmax.get('min', '?')}–{tmax.get('max', '?')}° / "
                f"min {tmin.get('min', '?')}–{tmin.get('max', '?')}°)" + nl
            )
    testo += f"{nl}🔗 {link}"
    return testo


def _meteo_bollettino_aggiornato(dati, quando="mattina"):
    """True se il bollettino SIAG è del giorno corrente (Roma) e ha contenuti utili.

    La provincia pubblica di solito il bollettino del giorno al mattino (campo date + hour).
    Non inviamo se il file è ancora quello di ieri o senza testo meteo.
    """
    if not isinstance(dati, dict):
        return False
    oggi_roma = ora_roma().date().isoformat()
    # data del bollettino (es. "2026-10-21T00:00:00" o "2026-10-21")
    bdate = str(dati.get("date") or "")
    if len(bdate) >= 10:
        bday = bdate[:10]
    else:
        bday = ""
    if bday and bday != oggi_roma:
        print(f"Meteo: bollettino data {bday} ≠ oggi Roma {oggi_roma} – non ancora aggiornato")
        return False
    if quando in ("sera", "domani", "evening"):
        parte = dati.get("tomorrow") or {}
    else:
        parte = dati.get("today") or {}
    # serve almeno titolo o testo condizioni
    if not (parte.get("weather") or parte.get("title") or parte.get("conditions")):
        print("Meteo: sezione oggi/domani senza contenuti – attendo aggiornamento")
        return False
    # hour es. "11:00" indica pubblicazione; se manca ma c'è testo ok
    print(f"Meteo: bollettino OK data={bday or oggi_roma} hour={dati.get('hour')} id={dati.get('id')}")
    return True


def prendi_bollettino_meteo(quando="mattina"):
    """Restituisce dict {it, de} oppure None se non ancora aggiornato / errore."""
    try:
        dati_it = requests.get(
            "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=it", timeout=15
        ).json()
        if not _meteo_bollettino_aggiornato(dati_it, quando):
            return None
        try:
            dati_de = requests.get(
                "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=de", timeout=15
            ).json()
        except Exception:
            dati_de = dati_it
        return {
            "it": _bollettino_meteo_lang(dati_it, quando, "it"),
            "de": _bollettino_meteo_lang(dati_de, quando, "de"),
        }
    except Exception as e:
        print("Errore meteo:", e)
        return None


# ========== VIABILITÀ ==========
def prendi_viabilita():
    try:
        dati = requests.get(
            "https://static-verkehr.provinz.bz.it/publications/traffic/traffic.json", timeout=20
        ).json()
        return dati if isinstance(dati, list) else []
    except Exception as e:
        print("Errore viabilità:", e)
        return []


def formatta_evento_viabilita(ev, lang="it"):
    if lang == "de":
        strada_desc = ev.get("messageStreetInternetDescDe") or ev.get("messageStreetWapDescDe") or ev.get("messageStreetInternetDescIt") or ev.get("messageStreetWapDescIt")
        zona = ev.get("messageZoneDescDe") or ev.get("messageZoneDescIt") or ""
        grad = ev.get("messageGradDescDe") or ev.get("messageGradDescIt") or ""
        testo = (ev.get("placeDe") or ev.get("descriptionDe") or ev.get("placeIt") or ev.get("descriptionIt") or "").strip()
        dal_lab, al_lab, fino = "Von", "bis", "Bis"
        evento = "Ereignis"
    else:
        strada_desc = ev.get("messageStreetInternetDescIt") or ev.get("messageStreetWapDescIt") or ev.get("messageStreetInternetDescDe")
        zona = ev.get("messageZoneDescIt") or ""
        grad = ev.get("messageGradDescIt") or ""
        testo = (ev.get("placeIt") or ev.get("descriptionIt") or "").strip()
        dal_lab, al_lab, fino = "Dal", "al", "Fino al"
        evento = "Evento"
    strada = " ".join(filter(None, [ev.get("messageStreetNr"), strada_desc]))
    dal = format_data_it(ev.get("beginDate") or "")
    al = format_data_it(ev.get("endDate") or "")
    tipo = (ev.get("subTycodeValue") or "").upper()
    emoji = "🚧" if tipo == "BAUSTELLE" else ("🚫" if tipo == "SPERRE" else "⚠️")
    if tipo == "STAU" or is_coda_rallentamento(ev):
        emoji = "🚗"
    if is_incidente(ev):
        emoji = "💥"
    periodo = ""
    if dal and al:
        periodo = f"{dal_lab} {dal} {al_lab} {al}"
    elif dal:
        periodo = f"{dal_lab} {dal}"
    elif al:
        periodo = f"{fino} {al}"
    righe = [
        f"{emoji} <b>{strada or evento}</b>",
        f"{grad}" + (f" · {zona}" if zona else ""),
        periodo,
        testo[:350] + ("…" if len(testo) > 350 else ""),
    ]
    return chr(10).join(r for r in righe if r)



def is_chiusura(ev):
    return (ev.get("subTycodeValue") or "").upper() == "SPERRE"


def is_incidente(ev):
    t = ((ev.get("placeIt") or "") + " " + (ev.get("placeDe") or "") + " " + (ev.get("subTycodeValue") or "")).lower()
    return any(x in t for x in ("incident", "unfall", "tamponamento", "scontro"))


def is_coda_rallentamento(ev):
    """Code, STAU, traffico intenso / rallentato (per report e classificazione ampia)."""
    sub = (ev.get("subTycodeValue") or "").upper()
    if sub == "STAU":
        return True
    t = " ".join([
        str(ev.get("placeIt") or ""),
        str(ev.get("placeDe") or ""),
        str(ev.get("descriptionIt") or ""),
        str(ev.get("descriptionDe") or ""),
        str(ev.get("subTycodeIt") or ""),
        str(ev.get("tycodeIt") or ""),
    ]).lower()
    keywords = (
        "coda", "code ", "stau", "rallentament", "rallentato", "traffico intenso",
        "stockung", "stockungen", "verkehrsbehinderung", "lento scorrimento",
        "traffico rallentato", "mezzi pesanti in coda",
    )
    return any(k in t for k in keywords)


def is_coda_notificabile(ev):
    """PUSH: code/STAU live. Esclude lavori, divieti e avvisi generici 'rallentamenti'."""
    sub = (ev.get("subTycodeValue") or "").upper()
    if sub in ("BAUSTELLE", "SPERRE", "ALLGEMEIN", "RADWEG_SPERRE", "VERANSTALTUNG", "FREI BEFAHRBAR"):
        return False
    # STAU ufficiale = sempre notificabile
    if sub == "STAU":
        return True

    t = " ".join([
        str(ev.get("placeIt") or ""),
        str(ev.get("placeDe") or ""),
        str(ev.get("descriptionIt") or ""),
        str(ev.get("descriptionDe") or ""),
        str(ev.get("subTycodeIt") or ""),
        str(ev.get("tycodeIt") or ""),
        str(ev.get("messageGradDescIt") or ""),
        str(ev.get("messageGradDescDe") or ""),
    ]).lower()

    escludi = (
        "divieto di transito", "divieto transito", "divieto per",
        "peso superiore", "lunghezza superiore", "rimorch",
        "senso unico", "lavori", "cantiere", "baustelle",
        "aperto giornalmente", "conclusiv", "confine provinciale",
        "non c'è una deviazione", "non c'e una deviazione",
        "veicoli di peso", "veicoli di lunghezza",
        "in caso di coda o traffico",  # avviso generico A22/Austria
        "corridoio di sal", "rettungsgasse",
        "fiera di", "manifestazione",
    )
    if any(x in t for x in escludi):
        return False

    strong = (
        "coda di", "coda per", "code per", " in coda", "km di coda", "km coda",
        "stau", "stockung", "im stau",
        "traffico rallentato", "lento scorrimento",
        "mezzi pesanti in coda", "coda di mezzi",
    )
    return any(k in t for k in strong)


def _fingerprint_coda(ev):
    """Chiave grezza per strada+zona (ignora testo che cambia a ogni aggiornamento)."""
    strada = (ev.get("messageStreetNr") or "").upper().strip()
    if not strada:
        strada = (ev.get("messageStreetInternetDescIt") or ev.get("messageStreetWapDescIt") or "X")[:30].upper()
    zona = (ev.get("messageZoneDescIt") or "").lower().strip()[:50]
    raw = f"{strada}|{zona}"
    return "coda-road-" + hashlib.md5(raw.encode()).hexdigest()[:14]



def _parse_evento_dt(val):
    """Parse beginDate/endDate/publishDateTime in timezone Roma."""
    if not val:
        return None
    try:
        s = str(val).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s[:26] if "T" in s else s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except Exception:
        return None


def evento_attivo_oggi(ev):
    """True se l'evento interessa la giornata odierna (niente eventi solo futuri)."""
    ora = ora_roma()
    inizio_giorno = ora.replace(hour=0, minute=0, second=0, microsecond=0)
    fine_giorno = ora.replace(hour=23, minute=59, second=59, microsecond=999999)
    begin = _parse_evento_dt(ev.get("beginDate"))
    end = _parse_evento_dt(ev.get("endDate"))

    # Inizio nel futuro (dopo adesso) → non ancora attivo
    if begin and begin > ora + timedelta(minutes=15):
        return False
    # Inizio oltre oggi → giornata futura
    if begin and begin > fine_giorno:
        return False
    # Già terminato ieri o prima
    if end and end < inizio_giorno:
        return False
    # Terminato chiaramente prima di adesso (con margine 30 min)
    if end and end < ora - timedelta(minutes=30):
        return False
    return True


def _fingerprint_viabilita(ev, prefisso="ev"):
    """Chiave stabile per anti-spam (strada + zona + tipo), non solo messageId."""
    pezzi = [
        prefisso,
        str(ev.get("messageStreetNr") or ""),
        str(ev.get("messageZoneDescIt") or ""),
        str(ev.get("subTycodeValue") or ""),
        (str(ev.get("placeIt") or "")[:80]),
    ]
    raw = "|".join(pezzi).lower().strip()
    return f"{prefisso}-" + hashlib.md5(raw.encode()).hexdigest()[:16]

def formatta_evento_viabilita_breve(ev, lang="it"):
    if lang == "de":
        testo = (ev.get("placeDe") or ev.get("placeIt") or "").strip().replace("<", "").replace(">", "")
        strada = " ".join(filter(None, [
            ev.get("messageStreetNr"),
            ev.get("messageStreetInternetDescDe") or ev.get("messageStreetWapDescDe") or ev.get("messageStreetInternetDescIt"),
        ]))
    else:
        testo = (ev.get("placeIt") or "").strip().replace("<", "").replace(">", "")
        strada = " ".join(filter(None, [
            ev.get("messageStreetNr"),
            ev.get("messageStreetInternetDescIt") or ev.get("messageStreetWapDescIt"),
        ]))
    if len(testo) > 160:
        testo = testo[:157] + "…"
    dal = format_data_it(ev.get("beginDate") or "")
    titolo = strada or (testo[:40] if testo else ("Ereignis" if lang == "de" else "Evento"))
    return f"• <b>{titolo}</b>" + (f" ({dal})" if dal else "") + (chr(10) + "  " + testo if testo else "")



def _pubblicato_ultime_ore(ev, ore=24):
    """True se publishDateTime è nelle ultime `ore` ore."""
    pub = ev.get("publishDateTime")
    if not pub:
        return False
    try:
        dt = datetime.fromisoformat(str(pub).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        delta = (ora_roma() - dt.astimezone(TZ)).total_seconds()
        return 0 <= delta <= ore * 3600
    except Exception:
        return False


def _chiusura_prioritaria(ev):
    """Chiusure su A22, MEBO/SS38/SS42 o comuni principali."""
    if not is_chiusura(ev):
        return False
    for *_rest, pred in GRUPPI_VIABILITA:
        if pred(ev):
            return True
    return False



def _testo_evento(ev):
    return " ".join([
        str(ev.get("placeIt") or ""),
        str(ev.get("placeDe") or ""),
        str(ev.get("descriptionIt") or ""),
        str(ev.get("messageStreetNr") or ""),
        str(ev.get("messageZoneDescIt") or ""),
        str(ev.get("messageStreetInternetDescIt") or ""),
    ]).lower()


def is_evento_estero(ev):
    """Avvisi Austria/Tirolo o regole generiche non AA."""
    t = _testo_evento(ev)
    if "corridoio di sal" in t:
        return True
    markers = ("austria", "österreich", "tirolo del nord", "nordtirol", "nauders", "pfunds", "reschenbundes", "b180")
    return any(m in t for m in markers)


def _inizio_testuale_futuro(ev):
    """True se il testo indica inizio dopo oggi (es. 'Da lunedì 19.10.2026')."""
    t = (ev.get("placeIt") or "") + " " + (ev.get("descriptionIt") or "")
    ora = ora_roma()
    # DD.MM.YYYY o DD/MM/YYYY dopo "da"
    for m in re.finditer(
        r"da\s+(?:luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabato|domenica)?\s*"
        r"(\d{1,2})[./](\d{1,2})[./](\d{2,4})",
        t,
        re.I,
    ):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            start = datetime(y, mo, d, tzinfo=TZ)
        except ValueError:
            continue
        if start.date() > ora.date():
            return True
    return False


def _dedupe_eventi(lista):
    """Rimuove duplicati per messageId e per fingerprint testo."""
    out = []
    visti_id = set()
    visti_fp = set()
    for ev in lista:
        mid = str(ev.get("messageId") or "")
        if mid and mid in visti_id:
            continue
        fp = hashlib.md5(
            (str(ev.get("messageStreetNr") or "") + "|" + (ev.get("placeIt") or "")[:120]).lower().encode()
        ).hexdigest()[:16]
        if fp in visti_fp:
            continue
        if mid:
            visti_id.add(mid)
        visti_fp.add(fp)
        out.append(ev)
    return out


def is_coda_per_report(ev):
    """Code reali per il report mattutino (più stretto di is_coda_rallentamento)."""
    if is_evento_estero(ev):
        return False
    if not evento_attivo_oggi(ev):
        return False
    if _inizio_testuale_futuro(ev):
        return False
    # Stessa logica push: no divieti/lavori etichettati come rallentamenti
    if not is_coda_notificabile(ev):
        # eccezione: testo con km di coda / code per traffico (live)
        t = _testo_evento(ev)
        if not re.search(r"\d+\s*km\s+di\s+coda|code?\s+per\s+traffico|in\s+coda", t):
            return False
        if any(x in t for x in ("divieto", "lavori", "cantiere", "senso unico", "peso superiore")):
            return False
    return True


def is_chiusura_per_report(ev):
    """Chiusure attive OGGI su A22/MEBO/città, senza estero né inizio futuro."""
    if not _chiusura_prioritaria(ev):
        return False
    if is_evento_estero(ev):
        return False
    if not evento_attivo_oggi(ev):
        return False
    if _inizio_testuale_futuro(ev):
        return False
    return True


def report_viabilita_mattutino():
    """Restituisce dict {it, de}."""
    eventi = prendi_viabilita()
    nl = chr(10)
    if not eventi:
        return {
            "it": f"🚗 <b>Viabilità Alto Adige</b>{nl}<i>{ora_str()}</i>{nl}{nl}Nessun dato.",
            "de": f"🚗 <b>Verkehr Südtirol</b>{nl}<i>{ora_str()}</i>{nl}{nl}Keine Daten.",
        }

    incidenti = _dedupe_eventi([
        e for e in eventi
        if is_incidente(e) and evento_attivo_oggi(e) and not is_evento_estero(e)
    ])
    code = _dedupe_eventi([e for e in eventi if is_coda_per_report(e)])
    chiusure = _dedupe_eventi([e for e in eventi if is_chiusura_per_report(e)])

    def _build(lang):
        if lang == "de":
            testo = (
                f"🚗 <b>Verkehrsbericht morgens</b>{nl}"
                f"<i>{ora_str()}</i>{nl}{nl}"
                f"💥 Unfälle: <b>{len(incidenti)}</b> · "
                f"🚗 Staus: <b>{len(code)}</b> · "
                f"🚫 Sperren (A22/MEBO/Städte): <b>{len(chiusure)}</b>{nl}"
            )
            if incidenti:
                testo += f"{nl}<b>—— Unfälle ——</b>{nl}"
                for ev in incidenti[:12]:
                    testo += formatta_evento_viabilita_breve(ev, "de") + nl
            else:
                testo += f"{nl}✅ Keine Unfälle gemeldet.{nl}"
            if code:
                testo += f"{nl}<b>—— Staus / Verlangsamungen ——</b>{nl}"
                for ev in code[:12]:
                    testo += formatta_evento_viabilita_breve(ev, "de") + nl
            else:
                testo += f"{nl}✅ Kein Stau / keine Verlangsamung.{nl}"
            if chiusure:
                testo += f"{nl}<b>—— Sperren (A22 · MEBO · Hauptorte) ——</b>{nl}"
                usati = set()
                for titolo_it, titolo_de, pred in GRUPPI_VIABILITA:
                    gruppo = [e for e in chiusure if pred(e) and e.get("messageId") not in usati]
                    if not gruppo:
                        continue
                    for e in gruppo:
                        usati.add(e.get("messageId"))
                    testo += f"{nl}<i>{titolo_de if lang == 'de' else titolo_it}</i>{nl}"
                    for ev in gruppo[:10]:
                        testo += formatta_evento_viabilita_breve(ev, "de") + nl
            else:
                testo += f"{nl}✅ Keine Sperren auf A22/MEBO/Hauptorten.{nl}"
            testo += f"{nl}🔗 https://verkehr.provinz.bz.it"
        else:
            testo = (
                f"🚗 <b>Report viabilità mattutino</b>{nl}"
                f"<i>{ora_str()}</i>{nl}{nl}"
                f"💥 Incidenti: <b>{len(incidenti)}</b> · "
                f"🚗 Code/rallentamenti: <b>{len(code)}</b> · "
                f"🚫 Chiusure (A22/MEBO/città): <b>{len(chiusure)}</b>{nl}"
            )
            if incidenti:
                testo += f"{nl}<b>—— Incidenti ——</b>{nl}"
                for ev in incidenti[:12]:
                    testo += formatta_evento_viabilita_breve(ev, "it") + nl
            else:
                testo += f"{nl}✅ Nessun incidente segnalato.{nl}"
            if code:
                testo += f"{nl}<b>—— Code / rallentamenti ——</b>{nl}"
                for ev in code[:12]:
                    testo += formatta_evento_viabilita_breve(ev, "it") + nl
            else:
                testo += f"{nl}✅ Nessuna coda/rallentamento.{nl}"
            if chiusure:
                testo += f"{nl}<b>—— Chiusure (A22 · MEBO · città principali) ——</b>{nl}"
                usati = set()
                for titolo_it, titolo_de, pred in GRUPPI_VIABILITA:
                    gruppo = [e for e in chiusure if pred(e) and e.get("messageId") not in usati]
                    if not gruppo:
                        continue
                    for e in gruppo:
                        usati.add(e.get("messageId"))
                    testo += f"{nl}<i>{titolo_de if lang == 'de' else titolo_it}</i>{nl}"
                    for ev in gruppo[:10]:
                        testo += formatta_evento_viabilita_breve(ev, "it") + nl
            else:
                testo += f"{nl}✅ Nessuna chiusura su A22/MEBO/città principali.{nl}"
            testo += f"{nl}🔗 https://traffico.provincia.bz.it"
        return testo

    out = {"it": _build("it"), "de": _build("de")}
    print(f"Report viabilità: it={len(out['it'])} de={len(out['de'])} car., inc={len(incidenti)}, code={len(code)}, chius={len(chiusure)}")
    return out



def nuove_chiusure_e_incidenti():
    """Push solo per incidenti e code ATTIVI OGGI, pubblicati di recente.

    Anti-spam code (rafforzato):
    - solo code/stau/rallentamenti chiari (non generico traffico intenso)
    - pubblicate da ≤ 45 minuti
    - stesso messageId: blocco 18h
    - stessa strada+zona: blocco 18h (anche se cambia l'id)
    - max 3 notifiche code ogni 2 ore (tutto il bot)
    """
    eventi = prendi_viabilita()
    ora = ora_roma()
    out = []
    code_candidati = []

    for ev in eventi:
        if not evento_attivo_oggi(ev):
            continue

        pub = _parse_evento_dt(ev.get("publishDateTime"))
        if pub is None:
            begin = _parse_evento_dt(ev.get("beginDate"))
            if not begin or (ora - begin).total_seconds() > 6 * 3600:
                continue
            delta = (ora - begin).total_seconds()
        else:
            delta = (ora - pub).total_seconds()

        mid = str(ev.get("messageId") or "") or "x"

        if is_incidente(ev):
            if not (0 <= delta <= 3 * 3600):
                continue
            key_id = f"incidente-{mid}"
            key_fp = _fingerprint_viabilita(ev, "incidente")
            if was_sent(key_id, ttl_hours=12) or was_sent(key_fp, ttl_hours=12):
                continue
            mark_sent(key_id)
            mark_sent(key_fp)
            out.append({"it": "💥 <b>Incidente stradale</b>\n" + formatta_evento_viabilita(ev, "it"), "de": "💥 <b>Unfall</b>\n" + formatta_evento_viabilita(ev, "de")})
            continue

        if is_coda_notificabile(ev):
            # pubblicate nelle ultime 3 ore (GitHub Actions può ritardare)
            if not (0 <= delta <= 3 * 3600):
                continue
            # escludi avvisi "cronici" iniziati da più di 18 ore
            begin = _parse_evento_dt(ev.get("beginDate"))
            if begin is not None:
                age_begin = (ora - begin).total_seconds()
                if age_begin > 18 * 3600:
                    continue
            key_id = f"coda-{mid}"
            key_fp = _fingerprint_coda(ev)
            if was_sent(key_id, ttl_hours=18) or was_sent(key_fp, ttl_hours=18):
                continue
            code_candidati.append((delta, key_id, key_fp, ev))

    # Ordina dalle più recenti; applica rate limit globale
    code_candidati.sort(key=lambda x: x[0])  # delta crescente = più recente
    for delta, key_id, key_fp, ev in code_candidati:
        if not _coda_rate_ok(max_n=5, window_hours=2):
            print(f"Anti-spam code: rate limit raggiunto, salto {key_fp}")
            break
        if was_sent(key_id, ttl_hours=18) or was_sent(key_fp, ttl_hours=18):
            continue
        mark_sent(key_id)
        mark_sent(key_fp)
        _coda_rate_mark()
        out.append({"it": "🚗 <b>Coda / rallentamento</b>" + chr(10) + formatta_evento_viabilita(ev, "it"), "de": "🚗 <b>Stau / Verlangsamung</b>" + chr(10) + formatta_evento_viabilita(ev, "de")})
        print(f"Coda notificata: {key_fp} (pubblicata {int(delta/60)} min fa)")

    return out


# ========== FARMACIE ==========
def _parse_orario_turno(f):
    """Estrae (start_min, end_min) da TURN_TIMETABLE o PHAR_TIMETABLE."""
    import re as _re
    tt = (f.get("TURN_TIMETABLE") or f.get("PHAR_TIMETABLE") or "").strip()
    m = _re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", tt)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    return h1 * 60 + m1, h2 * 60 + m2



def is_farmacia_notturna(f):
    """Turno serale/notturno per il report della sera.

    Include:
    - inizio >= 18:00
    - fascia dopo mezzanotte (00:00–08:29 o fino a mezzogiorno)
    - copertura fino a >= 22:00 (ancora aperta di sera)
    - note con 'notte' / 24h
    """
    orari = _parse_orario_turno(f)
    if orari:
        start, end = orari
        if start >= 18 * 60:
            return True
        # 00:00 – 08:29 / mattina (turno notturno classico)
        if start <= 30 and end <= 12 * 60:
            return True
        if start <= 30:
            return True
        # Aperta almeno fino alle 22:00
        if end >= 22 * 60:
            return True
        if start == 0 and end >= 23 * 60 + 50:
            return True
    txt = " ".join([
        str(f.get("TURN_TIMETABLE") or ""),
        str(f.get("TURN_NOTES_I") or ""),
        str(f.get("TURN_NOTES_D") or ""),
    ]).lower()
    if any(k in txt for k in ("notte", "nacht", "notturno", "notturna", "24h", "h24")):
        return True
    return False


def is_farmacia_diurna(f):
    """Turno diurno: copre la giornata, esclude solo la fascia puramente serale 19–23:59."""
    orari = _parse_orario_turno(f)
    if orari:
        start, end = orari
        # Solo sera fino a mezzanotte → non diurno
        if start >= 18 * 60 and end >= 23 * 60:
            return False
        # Solo dopo mezzanotte fino al mattino presto → notturno, non diurno
        if start <= 30 and end <= 10 * 60:
            return False
        # Altrimenti (es. 00:00–18:59, 08:30–23:59, 08:00–19:00) → utile di giorno
        return True
    return not is_farmacia_notturna(f)


def prendi_farmacie_turno(modo="tutte"):
    """modo: 'notte' | 'giorno' | 'tutte'."""
    try:
        dati = requests.get("https://daten.buergernetz.bz.it/services/pharmacy/v1/json", timeout=20).json()
        on = [x for x in dati if x.get("IS_TURN") == 1]
        filtrati = []
        nomi_ok = [c.lower() for c in COMUNI_FARM] + [
            "bozen", "meran", "brixen", "bruneck", "sterzing", "schlanders", "leifers"
        ]
        for x in on:
            comune = ((x.get("GEME_DESC_I") or "") + " " + (x.get("GEME_DESC_D") or "")).lower()
            if any(c in comune for c in nomi_ok):
                filtrati.append(x)
        lista = filtrati or on
        if modo == "notte":
            solo = [f for f in lista if is_farmacia_notturna(f)]
            print(f"Farmacie notte: {len(solo)} (su {len(lista)} in turno)")
            # Non fare fallback a tutte: evita di mischiare diurne e di re-inviare liste diverse
            return solo
        if modo == "giorno":
            solo = [f for f in lista if is_farmacia_diurna(f)]
            print(f"Farmacie giorno: {len(solo)} (su {len(lista)} in turno)")
            return solo
        return lista
    except Exception as e:
        print("Errore farmacie:", e)
        return []


def report_farmacie(quando="sera"):
    """quando: 'sera' → notte; 'mattina' → diurne. Restituisce dict {it, de}."""
    modo = "notte" if quando in ("sera", "notte", "notturno") else "giorno"
    farm = prendi_farmacie_turno(modo)
    if modo == "notte" and not farm:
        print("report_farmacie: notte vuota → fallback a tutte le in-turno")
        farm = prendi_farmacie_turno("tutte")
        modo = "notte"  # etichetta resta notturna
    nl = chr(10)

    def _build(lang):
        if lang == "de":
            etichetta = "Nachtdienst" if modo == "notte" else "Tagesdienst"
            none = "Keine Daten verfügbar."
            farmacia = "Apotheke"
            altro = "Andere"
        else:
            etichetta = "turno di notte" if modo == "notte" else "turno di giorno"
            none = "Nessun dato disponibile."
            farmacia = "Farmacia"
            altro = "Altro"
        if not farm:
            return f"💊 <b>{'Apotheken' if lang == 'de' else 'Farmacie'} – {etichetta}</b>{nl}<i>{ora_str()}</i>{nl}{nl}{none}"

        def _sort_key(f):
            o = _parse_orario_turno(f)
            if not o:
                return (2, 0, f.get("PHAR_DESC_I") or "")
            start, _ = o
            blocco = 0 if start >= 18 * 60 else (1 if start <= 30 else 2)
            return (blocco, start, f.get("PHAR_DESC_I") or "")

        visti = set()
        unici = []
        for f in farm:
            pid = f.get("PHAR_ID")
            pname = f.get("PHAR_DESC_I") or f.get("PHAR_DESC_D") or ""
            id_key = str(pid if pid is not None else pname).strip().lower()
            orario_key = str(f.get("TURN_TIMETABLE") or f.get("PHAR_TIMETABLE") or "").strip()
            geme_key = str(f.get("GEME_DESC_I") or "").strip().lower()
            key = (id_key, orario_key, geme_key)
            if key in visti:
                continue
            visti.add(key)
            unici.append(f)

        per_comune = {}
        for f in unici:
            if lang == "de":
                c = f.get("GEME_DESC_D") or f.get("GEME_DESC_I") or altro
            else:
                c = f.get("GEME_DESC_I") or f.get("GEME_DESC_D") or altro
            per_comune.setdefault(c, []).append(f)

        titolo = "Apotheken" if lang == "de" else "Farmacie"
        testo = f"💊 <b>{titolo} – {etichetta}</b>{nl}<i>{ora_str()}</i>{nl}"
        for comune in sorted(per_comune.keys()):
            testo += f"{nl}<b>{html_esc(comune)}</b>{nl}"
            for f in sorted(per_comune[comune], key=_sort_key):
                if lang == "de":
                    nome = f.get("PHAR_DESC_D") or f.get("PHAR_DESC_I") or farmacia
                    indirizzo = (f.get("PHAR_ADRESS_D") or f.get("PHAR_ADRESS_I") or "").strip()
                else:
                    nome = f.get("PHAR_DESC_I") or f.get("PHAR_DESC_D") or farmacia
                    indirizzo = (f.get("PHAR_ADRESS_I") or f.get("PHAR_ADRESS_D") or "").strip()
                orario = (f.get("TURN_TIMETABLE") or f.get("PHAR_TIMETABLE") or "").strip()
                testo += f"• <b>{html_esc(nome)}</b>"
                if orario:
                    testo += f" · 🕐 {orario}"
                testo += nl
                if indirizzo:
                    testo += f"  📍 {html_esc(indirizzo)}{nl}"
        return testo

    return {"it": _build("it"), "de": _build("de")}


# ========== ALLARMI PROTEZIONE CIVILE (comunicati operativi) ==========
def prendi_allarmi_pc(lang="it"):
    """Feed SIAG ufficiale – I = italiano, D = tedesco."""
    try:
        file = "BIS_Zivilschutz_D.json" if lang == "de" else "BIS_Zivilschutz_I.json"
        r = requests.get(
            f"https://static-weather.services.siag.it/sys/{file}",
            timeout=20,
            headers={"User-Agent": "MonitorAltoAdige/1.0", "Accept": "application/json"},
        )
        raw = r.content
        # Rimuovi BOM UTF-8 se presente
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        if raw[:2] == bytes([0xFF, 0xFE]):
            text = raw.decode("utf-16-le")
        elif raw[:2] == bytes([0xFE, 0xFF]):
            text = raw.decode("utf-16-be")
        else:
            text = raw.decode("utf-8", errors="replace")
        dati = json.loads(text.strip() or "[]")
        if not isinstance(dati, list):
            dati = [dati] if dati else []
        return dati
    except Exception as e:
        print("Errore allarmi PC:", e)
        return []


def controlla_allarmi_pc():
    """Una notifica per comunicato attivo (ARTC_STATUS == 16), bilingue."""
    out = []
    import re as _re
    lista_it = prendi_allarmi_pc("it")
    lista_de = prendi_allarmi_pc("de")
    by_id_de = {}
    for a in lista_de:
        aid = str(a.get("ARTC_ID") or "")
        if aid:
            by_id_de[aid] = a

    for a in lista_it:
        try:
            status = int(a.get("ARTC_STATUS") or 0)
        except Exception:
            status = 0
        if status != 16:
            continue
        aid = str(a.get("ARTC_ID") or "")
        if not aid:
            aid = hashlib.md5(
                (str(a.get("ARTC_TITLE") or "") + str(a.get("ARTC_DATE") or "")).encode()
            ).hexdigest()[:12]
        key = f"pc-allarme-{aid}"
        if already_sent(key, ttl_hours=48):
            continue
        a_de = by_id_de.get(str(a.get("ARTC_ID") or ""), a)

        def _bloc(src, lang):
            titolo = (src.get("ARTC_TITLE") or (
                "Zivilschutzmitteilung" if lang == "de" else "Comunicato di Protezione Civile"
            )).strip()
            titolo = _re.sub(r"<[^>]+>", "", titolo)
            sub = _re.sub(r"<[^>]+>", "", (src.get("ARTC_SUBTITLE") or "")).strip()
            testo = _re.sub(r"<[^>]+>", "", (src.get("ARTC_LCONTENT") or "")).strip()
            if len(testo) > 900:
                testo = testo[:897] + "…"
            data = (src.get("ARTC_DATE") or "").replace("T00:00:00", "").replace(" 00:00:00", "")
            ora = src.get("ARTC_TIME") or ""
            quando = " ".join(x for x in (format_data_it(data) if data else data, ora) if x)
            nl = chr(10)
            if lang == "de":
                blocco = f"🆘 <b>Zivilschutzwarnung</b>{nl}"
                link = "https://allerta-protezione-civile.provincia.bz.it/de/home"
            else:
                blocco = f"🆘 <b>Allarme di Protezione Civile</b>{nl}"
                link = "https://allerta-protezione-civile.provincia.bz.it/it/home"
            if quando:
                blocco += f"🕒 {quando}{nl}"
            blocco += f"<b>{titolo}</b>{nl}"
            if sub:
                blocco += f"{sub}{nl}"
            if testo:
                blocco += f"{nl}{testo}{nl}"
            blocco += f"{nl}🔗 {link}"
            return blocco

        out.append({"it": _bloc(a, "it"), "de": _bloc(a_de, "de")})
        print(f"Allarme PC nuovo: {(a.get('ARTC_TITLE') or '')[:60]}")
    return out



def _in_finestra_sera():
    """17:00–23:50 ora di Roma (larga per ritardi Actions e cambio ora)."""
    h = ora_roma().hour
    m = ora_roma().minute
    if h < 17:
        return False
    if h == 23 and m > 50:
        return False
    if h > 23:
        return False
    return True


def invia_report_serali(force=False):
    """Meteo di domani + farmacie di notte. Flag solo DOPO invio riuscito.

    force=True: ignora la fascia oraria (slot cron dedicati UTC).
    Non reinvia se il flag giornaliero è già impostato.
    """
    if not force and not _in_finestra_sera():
        print(f"Fuori fascia sera ({ora_str()}) – skip report serali")
        return

    h, m = ora_roma().hour, ora_roma().minute
    print(f"Report serali ({h:02d}:{m:02d} Roma, force={force})...")
    print(f"  flags: meteo_sera={has_daily_flag('meteo_sera')} farm_sera={has_daily_flag('farm_sera')}")
    print(f"  TOKEN={bool(TOKEN)} dest={len(prendi_destinatari())}")

    # --- Meteo SERA (solo domani) ---
    if has_daily_flag("meteo_sera"):
        print("Meteo sera già inviato oggi")
    else:
        print("Invio meteo sera (solo domani)...")
        bol = prendi_bollettino_meteo("sera")
        if not bol:
            # ritenta senza blocco data stretto: a sera il bollettino di oggi ha già "tomorrow"
            print("  retry meteo sera senza check data rigoroso...")
            try:
                dati_it = requests.get(
                    "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=it", timeout=15
                ).json()
                dati_de = requests.get(
                    "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=de", timeout=15
                ).json()
                tom = dati_it.get("tomorrow") or {}
                if tom.get("weather") or tom.get("title") or tom.get("conditions"):
                    bol = {
                        "it": _bollettino_meteo_lang(dati_it, "sera", "it"),
                        "de": _bollettino_meteo_lang(dati_de, "sera", "de"),
                    }
                    print("  meteo sera recuperato via retry")
            except Exception as e:
                print("  retry meteo fallito:", e)
        if bol:
            if invia_messaggio(bol):
                set_daily_flag("meteo_sera")
                print("Meteo sera OK")
            else:
                print("Meteo sera FALLITO – ritento al prossimo run")
        else:
            print("Meteo sera: bollettino non disponibile – ritento")

    # --- Farmacie SERA (notte + aperte fino a tardi) ---
    if has_daily_flag("farm_sera"):
        print("Farmacie serali già inviate oggi")
    else:
        print("Invio farmacie notturne...")
        farm = prendi_farmacie_turno("notte")
        print(f"  classificate notte: {len(farm)}")
        if not farm:
            print("  fallback a tutte le IS_TURN (comuni principali)")
            farm = prendi_farmacie_turno("tutte")
            print(f"  totale in turno: {len(farm)}")
        msg = report_farmacie("sera")
        testo_it = msg.get("it", "") if isinstance(msg, dict) else (msg or "")
        testo_de = msg.get("de", "") if isinstance(msg, dict) else ""
        vuoto = (
            (not farm)
            or ("Nessun dato" in testo_it)
            or ("Keine Daten" in testo_de)
        )
        if vuoto:
            print("Lista farmacie vuota – NON imposto flag, ritento")
            print("  it[:200]=", testo_it[:200])
        else:
            ok = invia_messaggio(msg)
            if ok:
                set_daily_flag("farm_sera")
                print("Farmacie serali OK")
            else:
                print("Invio farmacie FALLITO – flag NON impostato")


# ========== MAIN ==========
def controlla_emergenze():
    print(f"[{ora_str()}] Controllo emergenze...")
    messaggi = []
    messaggi.extend(controlla_ps())
    messaggi.extend(prendi_allerte_nazionali())
    messaggi.extend(controlla_temporali())
    messaggi.extend(nuove_chiusure_e_incidenti())
    messaggi.extend(controlla_allarmi_pc())

    if messaggi:
        invia_pacchetto(messaggi)
    else:
        print("Nessuna condizione di allerta")

    # Report automatici in fascia oraria (il flag si marca solo DOPO invio)
    h = ora_roma().hour
    m = ora_roma().minute

    # Viabilità mattina: finestra ampia 5:30–10:00 (così un run del cron basta)
    if (h == 5 and m >= 30) or (6 <= h < 10):
        if not has_daily_flag("viabilita_mattina"):
            print("Invio report viabilità (fascia mattina)...")
            invia_messaggio(report_viabilita_mattutino())
            set_daily_flag("viabilita_mattina")
        else:
            print("Report viabilità già inviato oggi")

    # Meteo MATTINA 7:00–15:00 → solo dopo che SIAG ha pubblicato il bollettino di OGGI
    # (finestra larga: il bollettino esce spesso tra le 8 e le 12; a volte dopo le 13)
    if 7 <= h < 15:
        if not has_daily_flag("meteo_mattina"):
            print(f"Tentativo meteo mattina ({h:02d}:{m:02d} Roma)...")
            bol = prendi_bollettino_meteo("mattina")
            if bol:
                if invia_messaggio(bol):
                    set_daily_flag("meteo_mattina")
                    print("Meteo mattina OK")
                else:
                    print("Meteo mattina FALLITO – ritento al prossimo run")
            else:
                print("Meteo mattina: bollettino non ancora aggiornato – ritento")
        else:
            print("Meteo mattina già inviato oggi")

    # Report serali (meteo domani + farmacie notte) – stessa logica del modo "sera"
    invia_report_serali()

    # Farmacie MATTINA: solo weekend/festivi 7:30–10:00 → solo turno di GIORNO
    if is_weekend_or_holiday():
        if (h == 7 and m >= 30) or 8 <= h < 10:
            if not has_daily_flag("farm_mattina"):
                print("Invio farmacie diurne (weekend/festivo)...")
                if invia_messaggio(report_farmacie("mattina")):
                    set_daily_flag("farm_mattina")


def invia_meteo_giornaliero(quando=None):
    if quando is None:
        quando = "sera" if ora_roma().hour >= 15 else "mattina"
    bol = prendi_bollettino_meteo(quando)
    if bol:
        if invia_messaggio(bol):
            set_daily_flag("meteo_sera" if quando == "sera" else "meteo_mattina")
            print(f"Meteo {quando} OK")
        else:
            print(f"Meteo {quando} FALLITO")
    else:
        print(f"Meteo {quando}: bollettino non disponibile")


if __name__ == "__main__":
    try:
        modo = (sys.argv[1] if len(sys.argv) > 1 else "check").lower().strip()
        if modo in ("meteo", "meteo_mattina", "meteo-mattina", "meteo_sera", "meteo-sera"):
            # Run manuale: azzera il flag così reinvia sempre
            is_manual = os.environ.get("GH_EVENT", "") == "workflow_dispatch"
            if is_manual or modo.startswith("meteo"):
                st = load_state("daily", {})
                if modo in ("meteo_sera", "meteo-sera"):
                    st.pop("meteo_sera", None)
                    save_state("daily", st)
                    invia_meteo_giornaliero("sera")
                else:
                    # meteo / meteo_mattina → bollettino di oggi
                    st.pop("meteo_mattina", None)
                    save_state("daily", st)
                    print(f"Invio meteo mattina forzato · dest={len(prendi_destinatari())}")
                    invia_meteo_giornaliero("mattina")
        elif modo in ("viabilita", "viabilità", "traffico"):
            print("Modalità viabilità: generazione report...")
            report = report_viabilita_mattutino()
            print("Invio report viabilità...")
            if invia_messaggio(report):
                set_daily_flag("viabilita_mattina")
                print("Report viabilità inviato")
            else:
                print("Invio report viabilità FALLITO")
        elif modo in ("farmacie", "sera", "evening", "sera_force", "force_sera"):
            # Slot dedicato / manuale: meteo domani + farmacie notte
            print(f"Modalità sera · TOKEN={bool(TOKEN)} CHAT_ID={CHAT_ID!r} SUBS_URL={bool(SUBS_URL)}")
            dest = prendi_destinatari()
            print(f"Destinatari trovati: {len(dest)} → {[d.get('id') for d in dest[:5]]}")
            # Solo run MANUALE (workflow_dispatch) o modo force: reset flag e reinvia
            is_manual = os.environ.get("GH_EVENT", "") == "workflow_dispatch"
            is_force = modo in ("sera_force", "force_sera")
            if is_manual or is_force:
                st = load_state("daily", {})
                had = {k: st.get(k) for k in ("meteo_sera", "farm_sera") if k in st}
                print(f"Run manuale/force: reset flag serali (erano: {had})")
                st.pop("meteo_sera", None)
                st.pop("farm_sera", None)
                save_state("daily", st)
            invia_report_serali(force=True)
        else:
            controlla_emergenze()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
