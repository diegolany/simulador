"""¿Vale la pena hacer un barrido México ahora? Imprime "SI ..." o "NO ...".

Sí, cuando un partido de liga fuerte (config mexico.ligas_fuertes) empieza entre 30 min y 2.5 h desde ahora —es
cuando las casas mexicanas más se atrasan contra Pinnacle— y no hubo otro barrido en las últimas 2 horas.
Lee la lista pública de partidos del bot (no gasta créditos ni tokens de más).
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROYECTO = Path(__file__).parent.parent
MARCA = Path(os.environ.get("TEMP", "/tmp")) / "barridos" / "ultimo_barrido.txt"


def main() -> int:
    config = json.loads((PROYECTO / "config.json").read_text(encoding="utf-8"))
    fuertes = config["mexico"]["ligas_fuertes"]
    ahora = datetime.now(timezone.utc)
    if MARCA.exists() and ahora - datetime.fromisoformat(MARCA.read_text().strip()) < timedelta(hours=2):
        print("NO: ya hubo un barrido en las últimas 2 horas")
        return 0
    try:
        with urllib.request.urlopen("https://diegolany.github.io/simulador/partidos.json", timeout=20) as r:
            partidos = json.load(r)
    except OSError as e:
        print(f"SI: no se pudo leer la lista de partidos ({e}); se barre por si acaso")
        return 0
    proximos = [(dep, l, v, ini) for dep, l, v, ini in partidos if dep in fuertes
                and timedelta(minutes=30) <= datetime.fromisoformat(ini + "+00:00") - ahora <= timedelta(hours=2.5)]
    if not proximos:
        print("NO: ningún partido fuerte empieza en las próximas 2.5 horas")
        return 0
    if "--marcar" in sys.argv:
        MARCA.parent.mkdir(parents=True, exist_ok=True)
        MARCA.write_text(ahora.isoformat())
    print(f"SI: {len(proximos)} partidos fuertes pronto, ej. {proximos[0][1]} vs {proximos[0][2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
