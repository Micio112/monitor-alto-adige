import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import traceback

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TZ = ZoneInfo("Europe/Rome")

COMUNI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro"]
LIVELLI_ALLERTA = ["gialla", "arancione", "rossa"]

SOGLIA_TOTALE = 50
SOGLIA_ROSSI = 5
SOGLIA_ARANCIONI = 10

# Emoji cerchi per i codici triage
CERCHI = {
    "1": "🔴",  # Rosso
    "2": "🟠",  # Arancione
    "3": "🟡",  # Giallo
    "4": "🟢",  # Verde
    "5": "🔵",  # Blu
}
NOMI_CODICE = {
    "1": "Rosso",
    "2": "Arancione",
    "3": "Giallo",
    "4": "Verde",
    "5": "Blu",
}

def ora_roma():
    return datetime.now(TZ).strftime("%d/%m/%Y %H:%M")

def invia_messaggio(testo):
    if not TOKEN or not CHAT_ID:
        print("Token o Chat ID mancanti")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    dati = {"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=dati, timeout=15)
        return r.ok
    except Exception as e:
        print("Errore invio:", e)
        return False

# ========== PRONTO SOCCORSO ==========
def prendi_pronto_soccorso():
    try:
        dati = requests.get(
            "https://dati.retecivica.bz.it/services/PS_Queue/json",
            timeout=15
        ).json()
        ospedali = {}
        for r in dati:
            nome = r["HOSPITAL_DESCRIPTION"]
            if nome not in ospedali:
                ospedali[nome] = {
                    "totale": 0,
                    "codici": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
                    "aggiornamento": r.get("LAST_UPDATE_TIME"),
                }
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
    """Report completo con tutti i codici colore (cerchi emoji)"""
    c = d["codici"]
    righe = [
        f"🏥 <b>{nome}</b>",
        f"Totale in attesa: <b>{d['totale']}</b>",
        "",
        f"{CERCHI['1']} Rosso: <b>{c['1']}</b>",
        f"{CERCHI['2']} Arancione: <b>{c['2']}</b>",
        f"{CERCHI['3']} Giallo: <b>{c['3']}</b>",
        f"{CERCHI['4']} Verde: <b>{c['4']}</b>",
        f"{CERCHI['5']} Blu: <b>{c['5']}</b>",
    ]
    return "\n".join(righe)

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

# ========== ALLERTE PC NAZIONALI ==========
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

# ========== BOLLETTINO METEO UFFICIALE ALTO ADIGE ==========
def prendi_bollettino_meteo():
    """Bollettino ufficiale Servizio Meteo Provincia (SIAG)"""
    try:
        url = "https://api-weather.services.siag.it/api/v2/bulletinHD?format=json&lang=it"
        dati = requests.get(url, timeout=15).json()
        oggi = dati.get("today", {})
        domani = dati.get("tomorrow", {})

        # stationData: ordine tipico Bolzano, Merano, Bressanone, Brunico, ...
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

        testo += (
            "\n🔗 Fonte: Servizio Meteo Provincia di Bolzano\n"
            "https://meteo.provincia.bz.it"
        )
        return testo
    except Exception as e:
        print("Errore bollettino meteo:", e)
        return None

# ========== MAIN ==========
def controlla_emergenze():
    """Controlli frequenti: PS + allerte PC"""
    print(f"[{ora_roma()}] Controllo emergenze...")
    messaggi = []
    messaggi.extend(controlla_ps())
    messaggi.extend(prendi_allerte_nazionali())

    if messaggi:
        testo = f"🔔 <b>Monitor Alto Adige</b>\n<i>{ora_roma()}</i>\n\n"
        testo += "\n\n————————\n\n".join(messaggi)
        if invia_messaggio(testo):
            print("Notifica emergenza inviata")
        else:
            print("Errore invio")
    else:
        print("Nessuna condizione di allerta")

def invia_meteo_giornaliero():
    """Report meteo giornaliero (da lanciare 1-2 volte al giorno)"""
    print(f"[{ora_roma()}] Invio bollettino meteo...")
    testo = prendi_bollettino_meteo()
    if testo and invia_messaggio(testo):
        print("Bollettino meteo inviato")
    else:
        print("Bollettino meteo non inviato")

if __name__ == "__main__":
    import sys
    try:
        modo = (sys.argv[1] if len(sys.argv) > 1 else "check").lower()
        if modo == "meteo":
            invia_meteo_giornaliero()
        else:
            controlla_emergenze()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
