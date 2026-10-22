#!/usr/bin/env python3
"""Monitor Alto Adige – notifica.py (carica sorgente completa dallo storico + patch)."""
import sys
import urllib.request

URL = "https://raw.githubusercontent.com/Micio112/monitor-alto-adige/7b297fea3bb3986bc3dcb72a4a3d6abd63f1bbb9/notifica.py"

def main():
    try:
        code = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8")
    except Exception as e:
        print("Impossibile scaricare notifica storica:", e)
        sys.exit(1)

    code = code.replace("if 7 <= h < 13:", "if 7 <= h < 15:")
    code = code.replace("# Meteo MATTINA 7:00–13:00", "# Meteo MATTINA 7:00–15:00")

    needle = 'elif modo in ("meteo_mattina", "meteo-mattina"):\n            invia_meteo_giornaliero("mattina")'
    if needle in code:
        code = code.replace(
            needle,
            'elif modo in ("meteo_mattina", "meteo-mattina"):\n'
            '            st = load_state("daily", {})\n'
            '            st.pop("meteo_mattina", None)\n'
            '            save_state("daily", st)\n'
            '            print("Invio meteo mattina forzato")\n'
            '            invia_meteo_giornaliero("mattina")',
        )

    needle2 = 'if modo == "meteo":\n            invia_meteo_giornaliero()'
    if needle2 in code:
        code = code.replace(
            needle2,
            'if modo == "meteo":\n'
            '            st = load_state("daily", {})\n'
            '            st.pop("meteo_mattina", None)\n'
            '            st.pop("meteo_sera", None)\n'
            '            save_state("daily", st)\n'
            '            invia_meteo_giornaliero()',
        )

    ns = {"__name__": "__main__", "__file__": __file__}
    exec(compile(code, "notifica.py", "exec"), ns)

if __name__ == "__main__":
    main()
