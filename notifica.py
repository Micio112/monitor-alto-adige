#!/usr/bin/env python3
"""Monitor Alto Adige – notifiche Telegram."""
import hashlib
import json
import os
import sys
import traceback
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUBS_URL = os.environ.get("SUBS_URL")
SUBS_KEY = os.environ.get("SUBS_KEY")

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

GRUPPI_VIABILITA = [
    ("A22", lambda e: (e.get("messageStreetNr") or "").upper() == "A22"
     or ("brennero" in ((e.get("placeIt") or "") + (e.get("messageStreetInternetDescIt") or "")).lower()
         and "a22" in ((e.get("messageStreetNr") or "") + (e.get("placeIt") or "")).lower())),
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


def is_weekend_or_holiday():
    d = ora_roma().date()
    if d.weekday() >= 5:
        return True
    fixed = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}
    return (d.month, d.day) in fixed


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


def already_sent(event_key, ttl_hours=36):
    st = load_state("events", {})
    now = ora_roma().timestamp()
    st = {k: v for k, v in st.items() if now - v < ttl_hours * 3600}
    if event_key in st:
        save_state("events", st)
        return True
    st[event_key] = now
    save_state("events", st)
    return False


def daily_flag(name):
    st = load_state("daily", {})
    today = str(ora_roma().date())
    if st.get(name) == today:
        return True
    st[name] = today
    st = {k: v for k, v in st.items() if v == today}
    save_state("daily", st)
    return False


def prendi_destinatari():
    ids = []
    if CHAT_ID:
        ids.append(str(CHAT_ID))
    if SUBS_URL and SUBS_KEY:
        try:
            r = requests.get(SUBS_URL, params={"key": SUBS_KEY}, timeout=15)
            if r.ok:
                for u in r.json() if isinstance(r.json(), list) else []:
                    cid = str(u.get("id") if isinstance(u, dict) else u).strip()
                    if cid and cid not in ids:
                        ids.append(cid)
        except Exception as e:
            print("Errore iscritti:", e)
    print("Destinatari:", ids)
    return ids


def invia_messaggio(testo):
    if not TOKEN:
        print("Token mancante")
        return False
    dest = prendi_destinatari()
    if not dest:
        return False
    ok = True
    for cid in dest:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": cid, "text": testo, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=15,
            )
            if not r.ok:
                ok = False
                print("Errore invio", cid, r.text[:200])
            else:
                print("Inviato a", cid)
        except Exception as e:
            ok = False
            print("Errore", cid, e)
    return ok


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


def formatta_ospedale(nome, d):
    c = d["codici"]
    return "\n".join([
        f"🏥 <b>{nome}</b>",
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
    messaggi = []
    for nome, d in prendi_pronto_soccorso().items():
        c = d["codici"]
        motivi = []
        if d["totale"] >= SOGLIA_TOTALE:
            motivi.append(f"totale ≥ {SOGLIA_TOTALE} ({d['totale']})")
        if c["1"] >= SOGLIA_ROSSI:
            motivi.append(f"rossi ≥ {SOGLIA_ROSSI} ({c['1']})")
        if c["2"] >= SOGLIA_ARANCIONI:
            motivi.append(f"arancioni ≥ {SOGLIA_ARANCIONI} ({c['2']})")
        if c["3"] >= SOGLIA_GIALLI:
            motivi.append(f"gialli ≥ {SOGLIA_GIALLI} ({c['3']})")
        if c["4"] >= SOGLIA_VERDI:
            motivi.append(f"verdi ≥ {SOGLIA_VERDI} ({c['4']})")
        if motivi:
            testo = formatta_ospedale(nome, d)
            testo += "\n⚠️ Motivo: " + ", ".join(motivi)
            messaggi.append(testo)
    return messaggi


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
            out.append(
                f"{emoji} <b>Allerta {colore.capitalize()}</b>\n"
                f"Comune: {info.get('comune', nome)}\n"
                f"{oggi.get('descrizione', '')}\n"
                f"Zona: {info.get('zona', '—')}"
            )
        return out
    except Exception as e:
        print("Errore allerte:", e)
        return []


def controlla_temporali():
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
            messaggi.append(
                f"⛈️ <b>Temporale / maltempo</b>\n"
                f"Comune: <b>{comune}</b>\n"
                f"Livello: {colore.capitalize()}\n"
                f"{allerta.get('descrizione') or dettagli.get('temporali') or 'Temporali previsti'}"
            )
    except Exception as e:
        print("Errore temporali:", e)
    return messaggi
    def formatta_stazioni(station_data):
    if not station_data:
        return ""
    righe = []
    for i, st in enumerate(station_data):
        nome = STAZIONI_NOMI[i] if i < len(STAZIONI_NOMI) else f"Stazione {i+1}"
        sym = (st.get("symbol") or {}).get("description", "—")
        tmin, tmax = st.get("min"), st.get("max")
        if tmin is not None and tmax is not None:
            righe.append(f"• <b>{nome}</b>: {sym} · {tmin}° / {tmax}°")
        else:
            righe.append(f"• <b>{nome}</b>: {sym}")
    return "\n".join(righe)


def prendi_bollettino_meteo():
    try:
        dati = requests.get(
            "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=it", timeout=15
        ).json()
        oggi = dati.get("today", {})
        domani = dati.get("tomorrow", {})
        testo = (
            f"🌤 <b>Bollettino Meteo Alto Adige</b>\n"
            f"<i>{ora_str()}</i>\n\n"
            f"<b>Oggi – {oggi.get('title', '—')}</b>\n"
            f"{oggi.get('weather', '')}\n{oggi.get('temperatures', '')}\n"
        )
        if oggi.get("stationData"):
            testo += "\n📍 <b>Comuni (oggi)</b>\n" + formatta_stazioni(oggi["stationData"]) + "\n"
        if domani:
            testo += (
                f"\n<b>Domani – {domani.get('title', '—')}</b>\n"
                f"{domani.get('weather', '')}\n{domani.get('temperatures', '')}\n"
            )
            if domani.get("stationData"):
                testo += "\n📍 <b>Comuni (domani)</b>\n" + formatta_stazioni(domani["stationData"]) + "\n"
        forecasts = dati.get("dayForecasts") or []
        if forecasts:
            testo += "\n<b>Prossimi giorni</b>\n"
            for g in forecasts:
                giorno = nome_giorno(g.get("date"))
                desc = (g.get("symbol") or {}).get("description", "—")
                tmax, tmin = g.get("tempMax") or {}, g.get("tempMin") or {}
                testo += (
                    f"• <b>{giorno}</b>: {desc} "
                    f"(max {tmax.get('min', '?')}–{tmax.get('max', '?')}° / "
                    f"min {tmin.get('min', '?')}–{tmin.get('max', '?')}°)\n"
                )
        testo += "\n🔗 https://meteo.provincia.bz.it"
        return testo
    except Exception as e:
        print("Errore meteo:", e)
        return None


def prendi_viabilita():
    try:
        dati = requests.get(
            "https://static-verkehr.provinz.bz.it/publications/traffic/traffic.json", timeout=20
        ).json()
        return dati if isinstance(dati, list) else []
    except Exception as e:
        print("Errore viabilità:", e)
        return []


def formatta_evento_viabilita(ev):
    strada = " ".join(filter(None, [
        ev.get("messageStreetNr"),
        ev.get("messageStreetInternetDescIt") or ev.get("messageStreetWapDescIt"),
    ]))
    zona = ev.get("messageZoneDescIt") or ""
    grad = ev.get("messageGradDescIt") or ""
    testo = (ev.get("placeIt") or ev.get("descriptionIt") or "").strip()
    dal, al = ev.get("beginDate") or "", ev.get("endDate") or ""
    tipo = ev.get("subTycodeValue") or ""
    emoji = "🚧" if tipo == "BAUSTELLE" else ("🚫" if tipo == "SPERRE" else "⚠️")
    if "incident" in testo.lower() or "unfall" in (ev.get("placeDe") or "").lower():
        emoji = "💥"
    righe = [
        f"{emoji} <b>{strada or 'Evento'}</b>",
        f"{grad}" + (f" · {zona}" if zona else ""),
        f"Dal {dal}" + (f" al {al}" if al else ""),
        testo[:350] + ("…" if len(testo) > 350 else ""),
    ]
    return "\n".join(r for r in righe if r)


def is_chiusura(ev):
    return (ev.get("subTycodeValue") or "") == "SPERRE"


def is_incidente(ev):
    t = ((ev.get("placeIt") or "") + " " + (ev.get("placeDe") or "") + " " + (ev.get("subTycodeValue") or "")).lower()
    return any(x in t for x in ("incident", "unfall", "tamponamento", "scontro"))


def report_viabilita_mattutino():
    eventi = prendi_viabilita()
    if not eventi:
        return f"🚗 <b>Viabilità Alto Adige</b>\n<i>{ora_str()}</i>\n\nNessun dato."

    chiusure = [e for e in eventi if is_chiusura(e)]
    testo = (
        f"🚗 <b>Report viabilità mattutino</b>\n"
        f"<i>{ora_str()}</i>\n\n"
        f"🚫 Chiusure totali: <b>{len(chiusure)}</b> / {len(eventi)} segnalazioni\n"
    )

    usati = set()
    for titolo, pred in GRUPPI_VIABILITA:
        gruppo = []
        for e in chiusure:
            mid = e.get("messageId")
            if mid in usati:
                continue
            if pred(e):
                gruppo.append(e)
                usati.add(mid)
        if not gruppo:
            continue
        testo += f"\n<b>—— {titolo} ——</b>\n"
        for ev in gruppo[:12]:
            testo += "\n" + formatta_evento_viabilita(ev) + "\n"

    altre = [e for e in chiusure if e.get("messageId") not in usati]
    if altre:
        testo += "\n<b>—— Altre chiusure ——</b>\n"
        for ev in altre[:8]:
            testo += "\n" + formatta_evento_viabilita(ev) + "\n"

    testo += "\n🔗 https://traffico.provincia.bz.it"
    return testo


def nuove_chiusure_e_incidenti():
    eventi = prendi_viabilita()
    ora = ora_roma()
    out = []
    for ev in eventi:
        pub = ev.get("publishDateTime")
        if not pub:
            continue
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            delta = (ora - dt.astimezone(TZ)).total_seconds()
            if not (0 <= delta <= 2.5 * 3600):
                continue
        except Exception:
            continue

        mid = str(ev.get("messageId") or "")
        if is_incidente(ev):
            key = f"incidente-{mid}"
            if already_sent(key, ttl_hours=12):
                continue
            out.append("💥 <b>Incidente stradale</b>\n" + formatta_evento_viabilita(ev))
        elif is_chiusura(ev):
            key = f"chiusura-{mid}"
            if already_sent(key, ttl_hours=12):
                continue
            out.append("🚫 <b>Nuova chiusura</b>\n" + formatta_evento_viabilita(ev))
    return out


def prendi_farmacie_turno():
    try:
        dati = requests.get("http://daten.buergernetz.bz.it/services/pharmacy/v1/json", timeout=20).json()
        on = [x for x in dati if x.get("IS_TURN") == 1]
        filtrati = []
        for x in on:
            comune = x.get("GEME_DESC_I") or ""
            if any(c.lower() in comune.lower() for c in COMUNI_FARM):
                filtrati.append(x)
        return filtrati or on
    except Exception as e:
        print("Errore farmacie:", e)
        return []


def report_farmacie(quando="mattina"):
    farm = prendi_farmacie_turno()
    if not farm:
        return f"💊 <b>Farmacie di turno</b>\n<i>{ora_str()}</i>\n\nNessun dato disponibile."

    per_comune = {}
    for f in farm:
        c = f.get("GEME_DESC_I") or "Altro"
        per_comune.setdefault(c, []).append(f)

    testo = f"💊 <b>Farmacie di turno ({quando})</b>\n<i>{ora_str()}</i>\n"
    for comune in sorted(per_comune.keys()):
        testo += f"\n<b>{comune}</b>\n"
        for f in per_comune[comune]:
            nome = f.get("PHAR_DESC_I") or f.get("PHAR_DESC_D") or "Farmacia"
            indirizzo = f.get("PHAR_ADRESS_I") or f.get("PHAR_ADRESS_D") or ""
            cap = f.get("GEME_ZIP") or ""
            tel = f.get("PHAR_PHONE") or ""
            note = f.get("TURN_NOTES_I") or ""
            orario = f.get("TURN_TIMETABLE") or f.get("PHAR_TIMETABLE") or ""
            maps = ""
            if indirizzo and comune:
                q = requests.utils.quote(f"{indirizzo}, {cap} {comune}")
                maps = f"https://www.google.com/maps/search/?api=1&query={q}"
            testo += f"• <b>{nome}</b>\n  📍 {indirizzo}"
            if cap:
                testo += f", {cap}"
            testo += "\n"
            if tel:
                testo += f"  📞 {tel}\n"
            if orario:
                testo += f"  🕐 {orario}\n"
            if note:
                testo += f"  ℹ️ {note}\n"
            if maps:
                testo += f"  🗺 {maps}\n"
    return testo


def controlla_emergenze():
    print(f"[{ora_str()}] Controllo emergenze...")
    messaggi = []
    messaggi.extend(controlla_ps())
    messaggi.extend(prendi_allerte_nazionali())
    messaggi.extend(controlla_temporali())
    messaggi.extend(nuove_chiusure_e_incidenti())

    if messaggi:
        testo = f"🔔 <b>Monitor Alto Adige</b>\n<i>{ora_str()}</i>\n\n"
        testo += "\n\n————————\n\n".join(messaggi)
        invia_messaggio(testo)
    else:
        print("Nessuna condizione di allerta")

    h = ora_roma().hour
    m = ora_roma().minute

    if 6 <= h < 9 or (h == 5 and m >= 45):
        if not daily_flag("viabilita_mattina"):
            print("Invio report viabilità (fascia mattina)...")
            invia_messaggio(report_viabilita_mattutino())

    if 7 <= h < 10:
        if not daily_flag("meteo_mattina"):
            print("Invio bollettino meteo (fascia mattina)...")
            t = prendi_bollettino_meteo()
            if t:
                invia_messaggio(t)

    if 10 <= h < 12:
        if not daily_flag("meteo_tarda_mattina"):
            t = prendi_bollettino_meteo()
            if t:
                invia_messaggio(t)

    if is_weekend_or_holiday():
        if 8 <= h < 10 and not daily_flag("farm_mattina"):
            invia_messaggio(report_farmacie("mattina"))
        if 18 <= h < 20 and not daily_flag("farm_sera"):
            invia_messaggio(report_farmacie("sera"))


def invia_meteo_giornaliero():
    if daily_flag("meteo_manual"):
        print("Meteo già inviato oggi (flag)")
    t = prendi_bollettino_meteo()
    if t:
        invia_messaggio(t)


if __name__ == "__main__":
    try:
        modo = (sys.argv[1] if len(sys.argv) > 1 else "check").lower().strip()
        if modo == "meteo":
            invia_meteo_giornaliero()
        elif modo == "viabilita":
            invia_messaggio(report_viabilita_mattutino())
        elif modo == "farmacie":
            invia_messaggio(report_farmacie("sera" if ora_roma().hour >= 15 else "mattina"))
        else:
            controlla_emergenze()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
