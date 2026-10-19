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

COMUNI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro"]
LIVELLI_ALLERTA = ["gialla", "arancione", "rossa"]

SOGLIA_TOTALE = 50
SOGLIA_ROSSI = 5
SOGLIA_ARANCIONI = 10
SOGLIA_GIALLI = 15
SOGLIA_VERDI = 30

STAZIONI_NOMI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Silandro", "Dobbiaco"]
GIORNI = {0: "Lunedì", 1: "Martedì", 2: "Mercoledì", 3: "Giovedì", 4: "Venerdì", 5: "Sabato", 6: "Domenica"}
CERCHI = {"1": "🔴", "2": "🟠", "3": "🟡", "4": "🟢", "5": "🔵"}

COMUNI_FARM = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro", "Laives"]

# Zone / arterie per report viabilità
GRUPPI_VIABILITA = [
    ("A22", lambda e: (e.get("messageStreetNr") or "").upper() == "A22"
     or "brennero" in ((e.get("placeIt") or "") + (e.get("messageStreetInternetDescIt") or "")).lower()
     and "a22" in ((e.get("messageStreetNr") or "") + (e.get("placeIt") or "")).lower()),
    ("MEBO / SS38 / SS42", lambda e: any(
        x in ((e.get("messageStreetNr") or "") + " " + (e.get("placeIt") or "")).upper()
        for x in ("MEBO", "SS38", "SS42")
    )),
    ("Bolzano", lambda e: "bolzano" in (e.get("messageZoneDescIt") or "").lower()
     or "bolzano" in (e.get("placeIt") or "").lower()),
    ("Merano / Burgraviato", lambda e: "burgraviato" in (e.get("messageZoneDescIt") or "").lower()
     or "merano" in (e.get("placeIt") or "").lower()),
    ("Bressanone / Val d'Isarco", lambda e: "isarco" in (e.get("messageZoneDescIt") or "").lower()
     or "bressanone" in (e.get("placeIt") or "").lower()),
    ("Brunico / Val Pusteria", lambda e: "pusteria" in (e.get("messageZoneDescIt") or "").lower()
     or "brunico" in (e.get("placeIt") or "").lower()),
    ("Vipiteno", lambda e: "vipiteno" in ((e.get("placeIt") or "") + (e.get("messageZoneDescIt") or "")).lower()),
    ("Silandro / Val Venosta", lambda e: "venosta" in (e.get("messageZoneDescIt") or "").lower()
     or "silandro" in (e.get("placeIt") or "").lower()),
]


def ora_roma():
    return datetime.now(TZ)


def ora_str():
    return ora_roma().strftime("%d/%m/%Y %H:%M")


def nome_giorno(iso_date):
    if not iso_date:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso_date).replace("Z", "+00:00")[:19])
        return GIORNI.get(dt.weekday(), str(iso_date)[:10])
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
        print("Token mancante")
        return False
    if isinstance(testo, dict):
        testo_it = testo.get("it") or testo.get("de") or ""
        testo_de = testo.get("de") or testo.get("it") or ""
    else:
        testo_it = testo or ""
        if testo_de is None:
            testo_de = testo_it
    dest = prendi_destinatari()
    if not dest:
        print("Nessun destinatario")
        return False
    testo_it = _append_site(testo_it)
    testo_de = _append_site(testo_de)
    ok = True
    for d in dest:
        body = testo_de if d.get("lang") == "de" else testo_it
        for pezzo in _spezza_messaggio(body):
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
                if not r.ok:
                    ok = False
                    print("Errore invio", d["id"], r.text[:250])
                    if "parse" in (r.text or "").lower() or r.status_code == 400:
                        r2 = requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            data={
                                "chat_id": d["id"],
                                "text": pezzo.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""),
                                "disable_web_page_preview": True,
                            },
                            timeout=20,
                        )
                        if r2.ok:
                            ok = True
            except Exception as e:
                ok = False
                print("Eccezione invio", d["id"], e)
    return ok


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
    """Max 1 notifica/ora per lo stesso PS con lo stesso tipo di motivo."""
    messaggi = []
    for nome, d in prendi_pronto_soccorso().items():
        c = d["codici"]
        motivi = []
        tipi = []
        if d["totale"] >= SOGLIA_TOTALE:
            motivi.append(f"totale ≥ {SOGLIA_TOTALE} ({d['totale']})")
            tipi.append("totale")
        if c["1"] >= SOGLIA_ROSSI:
            motivi.append(f"rossi ≥ {SOGLIA_ROSSI} ({c['1']})")
            tipi.append("rossi")
        if c["2"] >= SOGLIA_ARANCIONI:
            motivi.append(f"arancioni ≥ {SOGLIA_ARANCIONI} ({c['2']})")
            tipi.append("arancioni")
        if c["3"] >= SOGLIA_GIALLI:
            motivi.append(f"gialli ≥ {SOGLIA_GIALLI} ({c['3']})")
            tipi.append("gialli")
        if c["4"] >= SOGLIA_VERDI:
            motivi.append(f"verdi ≥ {SOGLIA_VERDI} ({c['4']})")
            tipi.append("verdi")
        if not motivi:
            continue
        key = "ps|" + nome + "|" + "+".join(sorted(tipi))
        if already_sent(key, ttl_hours=1):
            print(f"PS saltato (già inviato <1h): {nome} ({', '.join(tipi)})")
            continue
        motivi_de = []
        if "totale" in tipi:
            motivi_de.append(f"Gesamt ≥ {SOGLIA_TOTALE} ({d['totale']})")
        if "rossi" in tipi:
            motivi_de.append(f"Rot ≥ {SOGLIA_ROSSI} ({c['1']})")
        if "arancioni" in tipi:
            motivi_de.append(f"Orange ≥ {SOGLIA_ARANCIONI} ({c['2']})")
        if "gialli" in tipi:
            motivi_de.append(f"Gelb ≥ {SOGLIA_GIALLI} ({c['3']})")
        if "verdi" in tipi:
            motivi_de.append(f"Grün ≥ {SOGLIA_VERDI} ({c['4']})")
        testo_it = formatta_ospedale(nome, d, "it") + "\n⚠️ Motivo: " + ", ".join(motivi)
        testo_de = formatta_ospedale(nome, d, "de") + "\n⚠️ Grund: " + ", ".join(motivi_de)
        messaggi.append({"it": testo_it, "de": testo_de})
    return messaggi


# ========== ALLERTE METEO (1 volta per evento) ==========
def prendi_allerte_nazionali():
    try:
        url = f"https://allertameteo.app/api/alert/{','.join(COMUNI)}"
        dati = requests.get(url, timeout=15).json()
        if not dati.get("success"):
            return []
        data = dati.get("data", {})
        if isinstance(data, dict) and "comune" in data:
            data = {data["comune"]: data}
        out = []
        nl = chr(10)
        for nome, info in data.items():
            if not info:
                continue
            oggi = info.get("oggi", {}).get("allerta", {})
            colore = (oggi.get("colore") or "verde").lower()
            if colore not in LIVELLI_ALLERTA:
                continue
            key = f"allerta-{info.get('comune', nome)}-{colore}-{(oggi.get('descrizione') or '')[:40]}"
            if already_sent(key, ttl_hours=20):
                continue
            emoji = {"gialla": "🟡", "arancione": "🟠", "rossa": "🔴"}.get(colore, "⚠️")
            desc = oggi.get("descrizione", "") or ""
            comune = info.get("comune", nome)
            zona = info.get("zona", "—")
            col_de = {"gialla": "Gelb", "arancione": "Orange", "rossa": "Rot"}.get(colore, colore)
            it = (
                f"{emoji} <b>Allerta {colore.capitalize()}</b>" + nl
                + f"Comune: {comune}" + nl
                + f"{desc}" + nl
                + f"Zona: {zona}"
            )
            de = (
                f"{emoji} <b>Wetterwarnung {col_de}</b>" + nl
                + f"Gemeinde: {comune}" + nl
                + f"{desc}" + nl
                + f"Zone: {zona}"
            )
            out.append({"it": it, "de": de})
        return out
    except Exception as e:
        print("Errore allerte:", e)
        return []


# ========== TEMPORALI (1 volta per comune/evento) ==========
def controlla_temporali():
    """Usa descrizione allerta / bollettino SIAG se parla di temporali forti."""
    messaggi = []
    try:
        url = f"https://allertameteo.app/api/alert/{','.join(COMUNI)}"
        dati = requests.get(url, timeout=15).json()
        if not dati.get("success"):
            return []
        data = dati.get("data", {})
        if isinstance(data, dict) and "comune" in data:
            data = {data["comune"]: data}
        for nome, info in data.items():
            if not info:
                continue
            oggi = info.get("oggi", {})
            allerta = oggi.get("allerta") or {}
            dettagli = oggi.get("dettagli") or {}
            testo_l = " ".join([
                str(allerta.get("descrizione") or ""),
                str(dettagli.get("temporali") or ""),
                str(dettagli.get("idrogeologico") or ""),
            ]).lower()
            colore = (allerta.get("colore") or "verde").lower()
            if "temporal" not in testo_l and "temporali" not in testo_l:
                continue
            if colore not in LIVELLI_ALLERTA and "forti" not in testo_l and "intense" not in testo_l:
                continue
            comune = info.get("comune", nome)
            key = f"temporale-{comune}-{colore}-{date.today()}"
            if already_sent(key, ttl_hours=12):
                continue
            desc = allerta.get("descrizione") or dettagli.get("temporali") or ""
            col_de = {"gialla": "Gelb", "arancione": "Orange", "rossa": "Rot", "verde": "Grün"}.get(colore, colore)
            if not desc:
                desc_it = "Temporali previsti"
                desc_de = "Gewitter erwartet"
            else:
                desc_it = desc
                desc_de = desc
            nl = chr(10)
            messaggi.append({
                "it": (
                    f"⛈️ <b>Temporale / maltempo</b>{nl}"
                    f"Comune: <b>{comune}</b>{nl}"
                    f"Livello: {colore.capitalize()}{nl}"
                    f"{desc_it}"
                ),
                "de": (
                    f"⛈️ <b>Gewitter / Unwetter</b>{nl}"
                    f"Gemeinde: <b>{comune}</b>{nl}"
                    f"Stufe: {col_de}{nl}"
                    f"{desc_de}"
                ),
            })
    except Exception as e:
        print("Errore temporali:", e)
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


def prendi_bollettino_meteo(quando="mattina"):
    """Restituisce dict {it, de} oppure None."""
    try:
        dati_it = requests.get(
            "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=it", timeout=15
        ).json()
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
    """PUSH: solo code/traffico live, non divieti/lavori etichettati come 'rallentamenti'.

    Esclude tipici falsi positivi della Centrale Viabilità:
    - divieti di peso/lunghezza (passi montani)
    - senso unico alternato / cantieri / lavori
    - restrizioni permanenti o di lunga durata
    """
    sub = (ev.get("subTycodeValue") or "").upper()
    # Tipi strutturali: non sono code in tempo reale
    if sub in ("BAUSTELLE", "SPERRE", "ALLGEMEIN"):
        return False

    t = " ".join([
        str(ev.get("placeIt") or ""),
        str(ev.get("placeDe") or ""),
        str(ev.get("descriptionIt") or ""),
        str(ev.get("descriptionDe") or ""),
        str(ev.get("subTycodeIt") or ""),
        str(ev.get("tycodeIt") or ""),
        str(ev.get("messageGradDescIt") or ""),
    ]).lower()

    # Falsi positivi: restrizioni e lavori (anche se la categoria dice "rallentamenti")
    escludi = (
        "divieto di transito", "divieto transito", "divieto per",
        "peso superiore", "lunghezza superiore", "rimorch",
        "senso unico", "lavori", "cantiere", "baustelle",
        "aperto giornalmente", "conclusiv", "confine provinciale",
        "non c'è una deviazione", "non c'e una deviazione",
        "veicoli di peso", "veicoli di lunghezza",
    )
    if any(x in t for x in escludi):
        return False

    # Accetta STAU (tipo ufficiale coda) oppure testo con coda/stau esplicito.
    # NON usare solo "rallentament": la provincia lo mette su molti avvisi statici.
    if sub == "STAU":
        return True
    strong = (
        "coda", "code ", " in coda", "stau", "stockung",
        "traffico rallentato", "lento scorrimento",
        "mezzi pesanti in coda", "traffico intenso",
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
    for _, pred in GRUPPI_VIABILITA:
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
                for titolo, pred in GRUPPI_VIABILITA:
                    gruppo = [e for e in chiusure if pred(e) and e.get("messageId") not in usati]
                    if not gruppo:
                        continue
                    for e in gruppo:
                        usati.add(e.get("messageId"))
                    testo += f"{nl}<i>{titolo}</i>{nl}"
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
                for titolo, pred in GRUPPI_VIABILITA:
                    gruppo = [e for e in chiusure if pred(e) and e.get("messageId") not in usati]
                    if not gruppo:
                        continue
                    for e in gruppo:
                        usati.add(e.get("messageId"))
                    testo += f"{nl}<i>{titolo}</i>{nl}"
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
            # solo pubblicate di recente
            if not (0 <= delta <= 45 * 60):
                continue
            # escludi avvisi "cronici" iniziati da più di 12 ore
            begin = _parse_evento_dt(ev.get("beginDate"))
            if begin is not None:
                age_begin = (ora - begin).total_seconds()
                if age_begin > 12 * 3600:
                    continue
            key_id = f"coda-{mid}"
            key_fp = _fingerprint_coda(ev)
            if was_sent(key_id, ttl_hours=18) or was_sent(key_fp, ttl_hours=18):
                continue
            code_candidati.append((delta, key_id, key_fp, ev))

    # Ordina dalle più recenti; applica rate limit globale
    code_candidati.sort(key=lambda x: x[0])  # delta crescente = più recente
    for delta, key_id, key_fp, ev in code_candidati:
        if not _coda_rate_ok(max_n=3, window_hours=2):
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
    """Estrae (start_min, end_min) da TURN_TIMETABLE tipo '19:00 - 23:59' o '00:00 - 08:29'."""
    import re
    tt = (f.get("TURN_TIMETABLE") or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", tt)
    if not m:
        return None
    h1, m1, h2, m2 = map(int, m.groups())
    return h1 * 60 + m1, h2 * 60 + m2


def is_farmacia_notturna(f):
    """Turno serale/notturno per il report della sera.

    Include:
    - fascia serale (inizio >= 18:00, es. 19:00–23:59)
    - dopo mezzanotte (00:00–08:29)
    - copertura notturna ampia (00:00–23:59 / quasi 24h)
    - orari che arrivano almeno fino a 23:00 (turno diurno lungo ancora aperto di sera)
    """
    orari = _parse_orario_turno(f)
    if orari:
        start, end = orari
        if start >= 18 * 60:
            return True
        if start <= 30:  # 00:00–00:30
            return True
        if start <= 6 * 60 and end <= 12 * 60:
            return True
        # Quasi tutto il giorno incluso sera/notte (es. 00:00–23:59, 08:30–23:59)
        if end >= 23 * 60:
            return True
        # 24h classico
        if start == 0 and end >= 23 * 60 + 50:
            return True
    t = " ".join([
        str(f.get("TURN_TIMETABLE") or ""),
        str(f.get("TURN_NOTES_I") or ""),
        str(f.get("TURN_NOTES_D") or ""),
    ]).lower()
    if any(k in t for k in ("notte", "nacht", "notturno", "notturna", "24h", "h24")):
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
        for x in on:
            comune = x.get("GEME_DESC_I") or ""
            if any(c.lower() in comune.lower() for c in COMUNI_FARM):
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
            key = (
                (f.get("PHAR_ID") or f.get("PHAR_DESC_I") or "").strip().lower(),
                (f.get("TURN_TIMETABLE") or f.get("PHAR_TIMETABLE") or "").strip(),
                (f.get("GEME_DESC_I") or "").strip().lower(),
            )
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
            testo += f"{nl}<b>{comune}</b>{nl}"
            for f in sorted(per_comune[comune], key=_sort_key):
                if lang == "de":
                    nome = f.get("PHAR_DESC_D") or f.get("PHAR_DESC_I") or farmacia
                    indirizzo = (f.get("PHAR_ADRESS_D") or f.get("PHAR_ADRESS_I") or "").strip()
                else:
                    nome = f.get("PHAR_DESC_I") or f.get("PHAR_DESC_D") or farmacia
                    indirizzo = (f.get("PHAR_ADRESS_I") or f.get("PHAR_ADRESS_D") or "").strip()
                orario = (f.get("TURN_TIMETABLE") or f.get("PHAR_TIMETABLE") or "").strip()
                testo += f"• <b>{nome}</b>"
                if orario:
                    testo += f" · 🕐 {orario}"
                testo += nl
                if indirizzo:
                    testo += f"  📍 {indirizzo}{nl}"
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

    # Meteo MATTINA 7:00–10:00 → solo oggi + prognosi
    if 7 <= h < 10:
        if not has_daily_flag("meteo_mattina"):
            print("Invio meteo mattina (oggi + prognosi)...")
            t = prendi_bollettino_meteo("mattina")
            if t:
                invia_messaggio(t)
                set_daily_flag("meteo_mattina")

    # Meteo SERA 18:00–21:00 → solo domani
    if 18 <= h < 21:
        if not has_daily_flag("meteo_sera"):
            print("Invio meteo sera (solo domani)...")
            t = prendi_bollettino_meteo("sera")
            if t:
                invia_messaggio(t)
                set_daily_flag("meteo_sera")

    # Farmacie SERA: ogni giorno 19:00–22:00 (finestra ampia per ritardi GitHub Actions UTC)
    # Solo turno notte/sera; flag SOLO dopo invio riuscito (se fallisce ritenta al run successivo)
    if (h == 19 and m >= 0) or h in (20, 21):
        if not has_daily_flag("farm_sera"):
            print(f"Invio farmacie notturne (fascia {h:02d}:{m:02d})...")
            msg = report_farmacie("sera")
            ok = invia_messaggio(msg)
            if ok:
                set_daily_flag("farm_sera")
                print("Farmacie serali inviate OK")
            else:
                print("Invio farmacie FALLITO – flag NON impostato, ritento al prossimo run")
        else:
            print("Farmacie serali già inviate oggi")

    # Farmacie MATTINA: solo weekend/festivi 7:30–10:00 → solo turno di GIORNO
    if is_weekend_or_holiday():
        if (h == 7 and m >= 30) or 8 <= h < 10:
            if not has_daily_flag("farm_mattina"):
                print("Invio farmacie diurne (weekend/festivo)...")
                invia_messaggio(report_farmacie("mattina"))
                set_daily_flag("farm_mattina")


def invia_meteo_giornaliero(quando=None):
    if quando is None:
        quando = "sera" if ora_roma().hour >= 15 else "mattina"
    t = prendi_bollettino_meteo(quando)
    if t:
        invia_messaggio(t)
        set_daily_flag("meteo_sera" if quando == "sera" else "meteo_mattina")


if __name__ == "__main__":
    try:
        modo = (sys.argv[1] if len(sys.argv) > 1 else "check").lower().strip()
        if modo == "meteo":
            invia_meteo_giornaliero()
        elif modo in ("meteo_sera", "meteo-sera"):
            invia_meteo_giornaliero("sera")
        elif modo in ("meteo_mattina", "meteo-mattina"):
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
        elif modo == "farmacie":
            quando = "sera" if ora_roma().hour >= 15 else "mattina"
            print(f"Modalità farmacie ({quando})...")
            ok = invia_messaggio(report_farmacie(quando))
            if ok:
                set_daily_flag("farm_sera" if quando == "sera" else "farm_mattina")
                print("Farmacie inviate OK")
            else:
                print("Invio farmacie FALLITO")
        else:
            controlla_emergenze()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
