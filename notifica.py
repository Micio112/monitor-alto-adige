import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import os
import traceback
import sys

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # admin di riserva
SUBS_URL = os.environ.get("SUBS_URL")         # es. https://monitor-bot.xxx.workers.dev/subscribers
SUBS_KEY = os.environ.get("SUBS_KEY")         # stessa API_KEY del Worker

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
    """Restituisce la lista di Chat ID a cui inviare (admin + iscritti approvati)."""
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
                        if isinstance(u, dict):
                            cid = str(u.get("id", "")).strip()
                        else:
                            cid = str(u).strip()
                        if cid and cid not in ids:
                            ids.append(cid)
            else:
                print("Errore HTTP iscritti:", r.status_code, r.text[:200])
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
                data={
                    "chat_id": cid,
                    "text": testo,
                    "parse_mode": "HTML",
                },
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
            "https://dati.retecivica.bz.it/services/PS_Queue/json",
            timeout=15,
        ).json()
        ospedali = {}
        for r in dati:
            nome = r["HOSPITAL_DESCRIPTION"]
            if nome not in ospedali:
                ospedali[nome] = {
                    "totale": 0,
                    "codici": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
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

# ========== BOLLETTINO METEO UFFICIALE ==========
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
        else:
            controlla_emergenze()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
