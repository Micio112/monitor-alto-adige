import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import traceback
import sys

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
SUBS_URL = os.environ.get("SUBS_URL")
SUBS_KEY = os.environ.get("SUBS_KEY")

TZ = ZoneInfo("Europe/Rome")

COMUNI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro"]
LIVELLI_ALLERTA = ["gialla", "arancione", "rossa"]

SOGLIA_TOTALE = 50
SOGLIA_ROSSI = 5
SOGLIA_ARANCIONI = 10

CERCHI = {
    "1": "🔴",
    "2": "🟠",
    "3": "🟡",
    "4": "🟢",
    "5": "🔵",
}

def ora_roma():
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M")

def prendi_destinatari():
    ids = []
    if CHAT_ID:
        ids.append(str(CHAT_ID))
    if SUBS_URL and SUBS_KEY:
        try:
            r = requests.get(SUBS_URL, params={"key": SUBS_KEY}, timeout=15)
            if r.ok:
                dati = r.json()
                if isinstance(dati, list):
                    for u in dati:
                        cid = str(u.get("id") if isinstance(u, dict) else u).strip()
                        if cid and cid not in ids:
                            ids.append(cid)
            else:
                print("Errore HTTP iscritti:", r.status_code)
        except Exception as e:
            print("Errore recupero iscritti:", e)
    print(f"Destinatari: {len(ids)} -> {ids}")
    return ids

def invia_messaggio(testo):
    if not TOKEN:
        print("Token mancante")
        return False
    destinatari = prendi_destinatari()
    if not destinatari:
        print("Nessun destinatario")
        return False
    ok_globale = True
    for cid in destinatari:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                data={"chat_id": cid, "text": testo, "parse_mode": "HTML"},
                timeout=15,
            )
            if r.ok:
                print(f"Inviato a {cid}")
            else:
                ok_globale = False
                print(f"Errore invio a {cid}: {r.text[:200]}")
        except Exception as e:
            ok_globale = False
            print(f"Errore invio a {cid}: {e}")
    return ok_globale

# ========== PRONTO SOCCORSO ==========
def prendi_pronto_soccorso():
    try:
        dati = requests.get(
            "https://dati.retecivica.bz.it/services/PS_Queue/json", timeout=15
        ).json()
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
    ])

def controlla_ps():
    messaggi = []
    for nome, d in prendi_pronto_soccorso().items():
        motivi = []
        if d["totale"] >= SOGLIA_TOTALE:
            motivi.append(f"totale ≥ {SOGLIA_TOTALE} ({d['totale']})")
        if d["codici"]["1"] >= SOGLIA_ROSSI:
            motivi.append(f"rossi ≥ {SOGLIA_ROSSI} ({d['codici']['1']})")
        if d["codici"]["2"] >= SOGLIA_ARANCIONI:
            motivi.append(f"arancioni ≥ {SOGLIA_ARANCIONI} ({d['codici']['2']})")
        if motivi:
            testo = formatta_ospedale(nome, d)
            testo += "\n\n⚠️ Motivo allerta: " + ", ".join(motivi)
            messaggi.append(testo)
    return messaggi

# ========== ALLERTE PC ==========
def prendi_allerte_nazionali():
    try:
        url = f"https://allertameteo.app/api/alert/{','.join(COMUNI)}"
        dati = requests.get(url, timeout=15).json()
        if not dati.get("success"):
            return []
        data = dati.get("data", {})
        if isinstance(data, dict) and "comune" in data:
            data = {data["comune"]: data}
        risultato = []
        for nome, info in data.items():
            if not info:
                continue
            oggi = info.get("oggi", {}).get("allerta", {})
            colore = (oggi.get("colore") or "verde").lower()
            if colore in LIVELLI_ALLERTA:
                emoji = {"gialla": "🟡", "arancione": "🟠", "rossa": "🔴"}.get(colore, "⚠️")
                risultato.append(
                    f"{emoji} <b>Allerta {colore.capitalize()}</b>\n"
                    f"Comune: {info.get('comune', nome)}\n"
                    f"{oggi.get('descrizione', '')}\n"
                    f"Zona: {info.get('zona', '—')}"
                )
        return risultato
    except Exception as e:
        print("Errore allerte nazionali:", e)
        return []

# ========== METEO ==========
def prendi_bollettino_meteo():
    try:
        url = "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=it"
        dati = requests.get(url, timeout=15).json()
        oggi = dati.get("today", {})
        domani = dati.get("tomorrow", {})
        stazioni = oggi.get("stationData") or []
        bolzano = stazioni[0] if stazioni else {}
        simbolo = (bolzano.get("symbol") or {}).get("description", "—")
        tmin = bolzano.get("min")
        tmax = bolzano.get("max")

        testo = (
            f"🌤 <b>Bollettino Meteo Alto Adige</b>\n"
            f"<i>{ora_roma()}</i>\n\n"
            f"<b>Oggi – {oggi.get('title', '—')}</b>\n"
            f"{oggi.get('weather', '')}\n"
            f"{oggi.get('temperatures', '')}\n"
        )
        if tmin is not None and tmax is not None:
            testo += f"\n📍 <b>Bolzano</b>: {simbolo}\nMin {tmin}° / Max {tmax}°\n"
        if domani:
            testo += (
                f"\n<b>Domani – {domani.get('title', '—')}</b>\n"
                f"{domani.get('weather', '')}\n"
                f"{domani.get('temperatures', '')}\n"
            )
        forecasts = dati.get("dayForecasts") or []
        if forecasts:
            testo += "\n<b>Prossimi giorni</b>\n"
            for g in forecasts:
                desc = (g.get("symbol") or {}).get("description", "—")
                data = (g.get("date") or "")[:10]
                tmax = g.get("tempMax") or {}
                tmin = g.get("tempMin") or {}
                testo += (
                    f"• {data}: {desc} "
                    f"(max {tmax.get('min', '?')}–{tmax.get('max', '?')}° / "
                    f"min {tmin.get('min', '?')}–{tmin.get('max', '?')}°)\n"
                )
        testo += "\n🔗 https://meteo.provincia.bz.it"
        return testo
    except Exception as e:
        print("Errore bollettino meteo:", e)
        return None

# ========== VIABILITÀ ==========
def prendi_viabilita():
    try:
        dati = requests.get(
            "https://static-verkehr.provinz.bz.it/publications/traffic/traffic.json",
            timeout=20,
        ).json()
        return dati if isinstance(dati, list) else []
    except Exception as e:
        print("Errore viabilità:", e)
        return []

def formatta_evento_viabilita(ev):
    strada = " ".join(
        filter(
            None,
            [
                ev.get("messageStreetNr"),
                ev.get("messageStreetInternetDescIt") or ev.get("messageStreetWapDescIt"),
            ],
        )
    )
    zona = ev.get("messageZoneDescIt") or ""
    grad = ev.get("messageGradDescIt") or ""
    testo = (ev.get("placeIt") or ev.get("descriptionIt") or "").strip()
    dal = ev.get("beginDate") or ""
    al = ev.get("endDate") or ""
    tipo = ev.get("subTycodeValue") or ""
    emoji = "🚧" if tipo == "BAUSTELLE" else ("🚫" if tipo == "SPERRE" else "⚠️")
    righe = [
        f"{emoji} <b>{strada or 'Evento viabilità'}</b>",
        f"{grad}" + (f" · {zona}" if zona else ""),
        f"Dal {dal}" + (f" al {al}" if al else ""),
        testo[:400] + ("…" if len(testo) > 400 else ""),
    ]
    return "\n".join(r for r in righe if r)

def report_viabilita_mattutino():
    eventi = prendi_viabilita()
    if not eventi:
        return f"🚗 <b>Viabilità Alto Adige</b>\n<i>{ora_roma()}</i>\n\nNessun dato disponibile."

    chiusure = [e for e in eventi if e.get("subTycodeValue") == "SPERRE"]
    cantieri = [e for e in eventi if e.get("subTycodeValue") == "BAUSTELLE"]
    altri = [e for e in eventi if e.get("subTycodeValue") not in ("SPERRE", "BAUSTELLE")]

    testo = (
        f"🚗 <b>Report viabilità mattutino</b>\n"
        f"<i>{ora_roma()}</i>\n\n"
        f"🚫 Chiusure: <b>{len(chiusure)}</b>\n"
        f"🚧 Cantieri: <b>{len(cantieri)}</b>\n"
        f"⚠️ Altri eventi: <b>{len(altri)}</b>\n"
        f"Totale segnalazioni: <b>{len(eventi)}</b>\n"
    )
    if chiusure:
        testo += "\n<b>Principali chiusure</b>\n"
        for ev in chiusure[:8]:
            testo += "\n" + formatta_evento_viabilita(ev) + "\n"
    testo += "\n🔗 https://traffico.provincia.bz.it"
    return testo

def nuove_chiusure():
    eventi = prendi_viabilita()
    ora = datetime.now(TZ)
    nuove = []
    for ev in eventi:
        if ev.get("subTycodeValue") != "SPERRE":
            continue
        pub = ev.get("publishDateTime")
        if not pub:
            continue
        try:
            dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            delta = (ora - dt.astimezone(TZ)).total_seconds()
            if 0 <= delta <= 2 * 3600:
                nuove.append(ev)
        except Exception:
            continue
    return nuove

# ========== MAIN ==========
def controlla_emergenze():
    print(f"[{ora_roma()}] Controllo emergenze...")
    messaggi = []
    messaggi.extend(controlla_ps())
    messaggi.extend(prendi_allerte_nazionali())
    for ev in nuove_chiusure():
        messaggi.append(
            "🚫 <b>Nuova chiusura / interruzione</b>\n" + formatta_evento_viabilita(ev)
        )

    if messaggi:
        testo = f"🔔 <b>Monitor Alto Adige</b>\n<i>{ora_roma()}</i>\n\n"
        testo += "\n\n————————\n\n".join(messaggi)
        if invia_messaggio(testo):
            print("Notifica emergenza inviata")
        else:
            print("Errore invio notifica")
    else:
        print("Nessuna condizione di allerta")

def invia_meteo_giornaliero():
    print(f"[{ora_roma()}] Invio bollettino meteo...")
    testo = prendi_bollettino_meteo()
    if testo and invia_messaggio(testo):
        print("Bollettino meteo inviato")
    else:
        print("Bollettino meteo non inviato")

if __name__ == "__main__":
    try:
        modo = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
        if modo == "meteo":
            invia_meteo_giornaliero()
        elif modo == "viabilita":
            print(f"[{ora_roma()}] Report viabilità mattutino...")
            testo = report_viabilita_mattutino()
            if invia_messaggio(testo):
                print("Report viabilità inviato")
            else:
                print("Errore invio report viabilità")
        else:
            controlla_emergenze()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
