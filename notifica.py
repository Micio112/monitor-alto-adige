import requests
from datetime import datetime
import traceback

TOKEN = "8597497470:AAFCgNleoBHBPxmBdi9Avt-652fJ-dOVYI4"
CHAT_ID = "135222778"
COMUNI = ["Bolzano", "Merano", "Bressanone", "Brunico", "Vipiteno", "Silandro"]
LIVELLI_ALLERTA = ["gialla", "arancione", "rossa"]

LOG_FILE = r"C:\Users\HUAWEI\OneDrive\Desktop\AlertAABZ\log.txt"

def scrivi_log(messaggio):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {messaggio}\n")

def invia_messaggio(testo):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    dati = {
        "chat_id": CHAT_ID,
        "text": testo,
        "parse_mode": "HTML"
    }
    try:
        risposta = requests.post(url, data=dati, timeout=10)
        return risposta.ok
    except Exception as e:
        scrivi_log(f"Errore invio messaggio: {e}")
        return False

def prendi_allerte_reali():
    try:
        comuni_str = ",".join(COMUNI)
        url = f"https://allertameteo.app/api/alert/{comuni_str}"
        risposta = requests.get(url, timeout=15)
        dati = risposta.json()

        if not dati.get("success"):
            return []

        allerte_trovate = []
        data = dati.get("data", {})

        if isinstance(data, dict) and "comune" in data:
            data = {data["comune"]: data}

        for nome, info in data.items():
            if not info:
                continue
            oggi = info.get("oggi", {}).get("allerta", {})
            colore = oggi.get("colore", "verde").lower()
            descrizione = oggi.get("descrizione", "")

            if colore in LIVELLI_ALLERTA:
                allerte_trovate.append({
                    "comune": info.get("comune", nome),
                    "livello": colore.capitalize(),
                    "descrizione": descrizione,
                    "zona": info.get("zona", "")
                })
        return allerte_trovate
    except Exception as e:
        scrivi_log(f"Errore recupero allerte: {e}")
        return []

def controlla_e_notifica():
    scrivi_log("Script avviato")
    try:
        allerte = prendi_allerte_reali()
        messaggi = []

        for a in allerte:
            messaggi.append(
                f"🚨 <b>Allerta {a['livello']}</b>\n"
                f"Comune: {a['comune']}\n"
                f"{a['descrizione']}\n"
                f"Zona: {a['zona']}"
            )

        if messaggi:
            testo = "🔔 <b>Monitor Alto Adige – Allerte</b>\n"
            testo += f"<i>{datetime.now().strftime('%d/%m/%Y %H:%M')}</i>\n\n"
            testo += "\n\n".join(messaggi)
            if invia_messaggio(testo):
                scrivi_log("Notifica allerta inviata")
            else:
                scrivi_log("Errore invio notifica allerta")
               else:
            scrivi_log("Nessuna allerta attiva")

    except Exception as e:
        scrivi_log(f"Errore generale: {e}")
        scrivi_log(traceback.format_exc())

if __name__ == "__main__":
    controlla_e_notifica()