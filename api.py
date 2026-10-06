"""Cliente de The Odds API: momios, resultados y control de créditos.

Reglas de costo (plan gratuito: 500 créditos al mes):
  - /sports y /events son gratis.
  - /odds cuesta [mercados] x [regiones] y trae todos los partidos próximos de la liga.
  - /scores con daysFrom cuesta 2 y trae resultados de hasta 3 días atrás.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from calendar import monthrange
from datetime import date, datetime, time as hora
from pathlib import Path

from base_datos import a_fecha, ahora, iso, leer_estado

CARPETA = Path(__file__).parent
URL_BASE = "https://api.the-odds-api.com/v4"


class ErrorAPI(Exception):
    pass


def cargar_config() -> dict:
    """En la nube la clave viene del secreto ODDS_API_KEY; en la PC, de config.json."""
    config = json.loads((CARPETA / "config.json").read_text(encoding="utf-8"))
    config["api_key"] = os.environ.get("ODDS_API_KEY") or config["api_key"]
    return config


def llamar(ruta: str, parametros: dict, clave: str):
    """Hace una petición y devuelve (datos, créditos restantes del mes, costo de esta llamada)."""
    url = f"{URL_BASE}{ruta}?{urllib.parse.urlencode({**parametros, 'apiKey': clave})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            datos = json.load(resp)
            restantes = resp.headers.get("x-requests-remaining")
            costo = resp.headers.get("x-requests-last")
    except urllib.error.HTTPError as e:
        raise ErrorAPI(f"{e.code}: {e.read().decode('utf-8', 'replace')}") from None
    time.sleep(0.3)
    return datos, int(float(restantes)) if restantes else None, int(float(costo)) if costo else 0


def deportes_activos(clave: str) -> tuple[dict, int]:
    """Ligas en temporada {clave: nombre} y créditos restantes. Gratis."""
    deportes, restantes, _ = llamar("/sports", {}, clave)
    return {d["key"]: d["title"] for d in deportes if d["active"] and not d["has_outrights"]}, restantes


def proximos_inicios(clave: str, deporte: str) -> list[datetime]:
    """Horas de inicio de los partidos programados de una liga. Gratis."""
    eventos, _, _ = llamar(f"/sports/{deporte}/events", {"dateFormat": "iso"}, clave)
    return [a_fecha(e["commence_time"]) for e in eventos]


def dias_restantes(con) -> int:
    """Días entre los que se reparten los créditos que quedan: hasta el fin de la simulación si termina antes que
    el mes (los créditos que sobren después ya no ayudan a cumplir la meta), si no, hasta el fin del mes."""
    hoy = date.today()
    fin_mes = monthrange(hoy.year, hoy.month)[1] - hoy.day + 1
    fin = leer_estado(con, "fin_simulacion")
    if fin:
        dias = (date.fromisoformat(fin) - hoy).days + 1
        if 1 <= dias < fin_mes:
            return dias
    return fin_mes


def creditos_hoy(con, restantes: int, reserva: int = 0) -> int:
    """Créditos que todavía se pueden gastar hoy para que el cupo alcance todos los días que quedan
    sin tocar la reserva de emergencia."""
    inicio_dia = iso(datetime.combine(date.today(), hora()).astimezone())
    gastado = con.execute("SELECT COALESCE(SUM(costo), 0) FROM consumo_api WHERE fecha >= ?",
                          (inicio_dia,)).fetchone()[0]
    return max(0, (restantes - reserva + gastado) // dias_restantes(con) - gastado)


def registrar_consumo(con, endpoint: str, motivo: str, deporte: str, costo: int, restantes: int) -> int:
    cur = con.execute(
        "INSERT INTO consumo_api (fecha, endpoint, motivo, deporte, costo, restantes) VALUES (?, ?, ?, ?, ?, ?)",
        (iso(ahora()), endpoint, motivo, deporte, costo, restantes),
    )
    return cur.lastrowid


def descargar_momios(con, config: dict, deporte: str, liga: str, motivo: str) -> tuple[str, int, int]:
    """Baja los momios de una liga y los guarda. Devuelve (momento de captura, id de consumo, costo)."""
    eventos, restantes, costo = llamar(
        f"/sports/{deporte}/odds",
        # Solo Pinnacle (precio justo) y las casas permitidas en México que vienen en los datos (Betsson): mismo costo
        {**({"bookmakers": config["bookmakers"]} if config.get("bookmakers") else {"regions": config["region"]}),
         "markets": ",".join(config["mercados"]), "oddsFormat": "decimal", "dateFormat": "iso"},
        config["api_key"],
    )
    capturado = iso(ahora())
    # Casas que en México pagan distinto que su versión internacional (ej. Betsson México ~2.3% menos): se guarda
    # el precio que de verdad se podría apostar desde México
    ajustes = {c["clave"]: 1 + c["ajuste_precio"] for c in config.get("casas_mexico", []) if c.get("ajuste_precio")}
    for ev in eventos:
        con.execute(
            """INSERT INTO eventos (id, deporte, liga, local, visitante, inicio)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET inicio = excluded.inicio""",
            (ev["id"], deporte, liga, ev.get("home_team") or "", ev.get("away_team") or "",
             iso(a_fecha(ev["commence_time"]))),
        )
        for casa in ev.get("bookmakers", []):
            for mercado in casa["markets"]:
                actualizado = mercado.get("last_update") or casa.get("last_update")
                for opcion in mercado["outcomes"]:
                    con.execute(
                        """INSERT INTO momios (evento_id, casa, mercado, seleccion, punto, momio,
                                               capturado, actualizado_casa)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ev["id"], casa["key"], mercado["key"], opcion["name"], opcion.get("point"),
                         round(max(1.01, opcion["price"] * ajustes.get(casa["key"], 1.0)), 3), capturado,
                         iso(a_fecha(actualizado)) if actualizado else None),
                    )
    id_consumo = registrar_consumo(con, "odds", motivo, deporte, costo, restantes)
    con.execute("UPDATE consumo_api SET partidos = ? WHERE id = ?", (len(eventos), id_consumo))
    con.commit()
    return capturado, id_consumo, costo


def descargar_resultados(con, config: dict, deporte: str) -> tuple[int, int]:
    """Actualiza marcadores de los últimos 3 días. Devuelve (partidos terminados nuevos, costo)."""
    eventos, restantes, costo = llamar(f"/sports/{deporte}/scores", {"daysFrom": 3, "dateFormat": "iso"},
                                       config["api_key"])
    nuevos = 0
    for ev in eventos:
        if not ev.get("completed") or not ev.get("scores"):
            continue
        marcador = {s["name"]: s["score"] for s in ev["scores"]}
        try:
            local = int(float(marcador[ev["home_team"]]))
            visitante = int(float(marcador[ev["away_team"]]))
        except (KeyError, TypeError, ValueError):
            continue
        cur = con.execute(
            """UPDATE eventos SET marcador_local = ?, marcador_visitante = ?, terminado = 1
               WHERE id = ? AND terminado = 0""",
            (local, visitante, ev["id"]),
        )
        nuevos += cur.rowcount
    registrar_consumo(con, "scores", "resultados", deporte, costo, restantes)
    con.commit()
    return nuevos, costo
