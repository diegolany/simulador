"""Momios de casas sin servicio de datos (Caliente).

Durante una sesión con Diego, Claude lee los momios en la página de Caliente con el navegador (sin iniciar sesión
ni resolver captchas por él) y los deja en externos/caliente.json. Al subirse el archivo, el bot los empareja con
sus partidos, actualiza el precio justo de Pinnacle si está viejo y los compara como a cualquier otra casa. Cada
archivo se procesa una sola vez.

Formato del archivo:
{"casa": "caliente", "capturado": "2026-10-04T06:20:00+00:00",
 "partidos": [{"deporte": "americanfootball_nfl", "inicio": "2026-10-04T17:00:00+00:00",
               "equipos": ["New York Jets", "Chicago Bears"],
               "momios": {"New York Jets": 2.65, "Chicago Bears": 1.53}}]}
En fútbol, el empate va como "Empate". Los momios van en decimal.
"""
import json
from datetime import timedelta
from pathlib import Path

import marcadores
from base_datos import a_fecha, ahora, iso

CARPETA = Path(__file__).parent / "externos"


def cargar(casa: str = "caliente") -> dict | None:
    archivo = CARPETA / f"{casa}.json"
    if not archivo.exists():
        return None
    return json.loads(archivo.read_text(encoding="utf-8"))


def emparejar(con, datos: dict) -> dict:
    """{evento_id: {"casa", "precios": {selección como la nombra el bot: momio}}} de los partidos que se encontraron."""
    por_deporte = {}
    for p in datos.get("partidos", []):
        if len(p.get("equipos", [])) != 2 or not p.get("inicio"):
            continue
        por_deporte.setdefault(p["deporte"], []).append({
            "inicio": a_fecha(p["inicio"]), "local": [p["equipos"][0]], "visitante": [p["equipos"][1]],
            "momios": p["momios"], "url": p.get("url")})
    mapa = {}
    for deporte, candidatos in por_deporte.items():
        for ev in con.execute("SELECT id, local, visitante, inicio FROM eventos WHERE deporte = ? AND inicio > ?",
                              (deporte, iso(ahora()))).fetchall():
            # Caliente pone primero al visitante en deportes de EE. UU.: el emparejador acepta el orden invertido
            partido, invertido = marcadores.emparejar(ev["local"], ev["visitante"], a_fecha(ev["inicio"]), candidatos, 6)
            if not partido:
                continue
            equipo_local, equipo_visitante = partido["local"][0], partido["visitante"][0]
            if invertido:
                equipo_local, equipo_visitante = equipo_visitante, equipo_local
            momios = partido["momios"]
            precios = {ev["local"]: momios.get(equipo_local), ev["visitante"]: momios.get(equipo_visitante)}
            if momios.get("Empate"):
                precios["Draw"] = momios["Empate"]
            if all(v and v > 1 for v in precios.values()):
                mapa[ev["id"]] = {"casa": datos.get("casa", "caliente"), "precios": precios, "deporte": deporte,
                                  "url": partido.get("url")}
    return mapa


def reciente(datos: dict, horas: float = 6) -> bool:
    """Un archivo con momios de hace más de unas horas ya no sirve: los precios cambiaron."""
    return ahora() - a_fecha(datos["capturado"]) <= timedelta(hours=horas)
