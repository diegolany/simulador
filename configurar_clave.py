"""Guarda la clave de The Odds API en config.json y verifica que funcione (no gasta créditos)."""
import json
import sys

from api import CARPETA, ErrorAPI, llamar


def main() -> int:
    clave = input("Pega tu clave de The Odds API y presiona Enter: ").strip()
    if not clave:
        print("No escribiste ninguna clave.")
        return 1
    try:
        deportes, restantes, _ = llamar("/sports", {}, clave)
    except ErrorAPI as e:
        print(f"La clave no funcionó ({e}). Revisa que la copiaste completa.")
        return 1

    ruta = CARPETA / "config.json"
    config = json.loads(ruta.read_text(encoding="utf-8"))
    config["api_key"] = clave
    ruta.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Clave guardada. Deportes en temporada: {len(deportes)}. Créditos restantes este mes: {restantes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
