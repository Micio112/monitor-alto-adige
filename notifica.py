import requests
from datetime import datetime
import os
import traceback

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

COMUNI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro"]
LIVELLI_ALLERTA = ["gialla", "arancione", "rossa"]

# Soglie Pronto Soccorso
SOGLIA_TOTALE = 50
SOGLIA_ROSSI = 5
SOGLIA_ARANCIONI = 10

def invia_messaggio(testo):
    if not TOKEN or not CHAT_ID:
        print("Token o Chat ID mancanti")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    dati = {"chat_id": CHAT_ID, "text": testo, "parse_mode": "HTML"}
    try:
        r = requests.post(url, data=dati, timeout=10)
        return r.ok
    except Exception as e:
        print("Errore invio:", e)
        return False

def prendi_allerte():
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
            colore = oggi.get("colore", "verde").lower()
            if colore in LIVELLI_ALLERTA:
                risultato.append({
                    "comune": info.get("comune", nome),
                    "livello": colore.capitalize(),
                    "descrizione": oggi.get("descrizione", ""),
                    "zona": info.get("zona", "")
                })
        return risultato
    except Exception as e:
        print("Errore allerte:", e)
        return []

def prendi_pronto_soccorso():
    try:
        dati = requests.get("https://dati.retecivica.bz.it/services/PS_Queue/json", timeout=15).json()
        ospedali = {}
        for r in dati:
            nome = r["HOSPITAL_DESCRIPTION"]
            if nome not in ospedali:
                ospedali[nome] = {"totale": 0, "rossi": 0, "arancioni": 0}
            q = int(r.get("QUEUE_LENGTH") or 0)
            ospedali[nome]["totale"] += q
            if r.get("TRIAGE_CODE") == "1":
                ospedali[nome]["rossi"] += q
            elif r.get("TRIAGE_CODE") == "2":
                ospedali[nome]["arancioni"] += q
        return ospedali
    except Exception as e:
        print("Errore PS:", e)
        return {}

def controlla_e_notifica():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Controllo in corso...")
    messaggi = []

    # Allerte meteo / PC
    for a in prendi_allerte():
        messaggi.append(
            f"🚨 <b>Allerta {a['livello']}</b>\n"
            f"Comune: {a['comune']}\n"
            f"{a['descrizione']}\n"
            f"Zona: {a['zona']}"
        )

    # Pronto Soccorso
    for nome, d in prendi_pronto_soccorso().items():
        motivi = []
        if d["totale"] >= SOGLIA_TOTALE:
            motivi.append(f"totale {d['totale']} pazienti")
        if d["rossi"] >= SOGLIA_ROSSI:
            motivi.append(f"{d['rossi']} codici rossi")
        if d["arancioni"] >= SOGLIA_ARANCIONI:
            motivi.append(f"{d['arancioni']} codici arancioni")
        if motivi:
            messaggi.append(
                f"🏥 <b>{nome}</b>\n"
                f"⚠️ {', '.join(motivi)}\n"
                f"Dettaglio: {d['totale']} totali · {d['rossi']} rossi · {d['arancioni']} arancioni"
            )

    if messaggi:
        testo = "🔔 <b>Monitor Alto Adige</b>\n"
        testo += f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M')}</i>\n\n"
        testo += "\n\n".join(messaggi)
        if invia_messaggio(testo):
            print("Notifica inviata")
        else:
            print("Errore invio")
    else:
        print("Nessuna condizione di allerta")

if __name__ == "__main__":
    try:
        controlla_e_notifica()
    except Exception as e:
        print("Errore generale:", e)
        print(traceback.format_exc())
