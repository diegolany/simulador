"""Marcadores gratis (sin créditos) desde el scoreboard público de ESPN.

Muestra el marcador en vivo de las apuestas activas y cierra las apuestas en cuanto
termina el partido. Si ESPN no cubre la liga o no se logra emparejar el partido, el
motor usa después los resultados de The Odds API (con créditos) como respaldo.
"""
import json
import re
import unicodedata
import urllib.error
import urllib.request
from datetime import timedelta, timezone
from difflib import SequenceMatcher

from base_datos import a_fecha, ahora, iso

URL = "https://site.api.espn.com/apis/site/v2/sports/{ruta}/scoreboard?dates={fecha}&limit=300"
RUTAS = {
    "soccer_mexico_ligamx": "soccer/mex.1",
    "soccer_epl": "soccer/eng.1",
    "soccer_spain_la_liga": "soccer/esp.1",
    "soccer_germany_bundesliga": "soccer/ger.1",
    "soccer_italy_serie_a": "soccer/ita.1",
    "soccer_france_ligue_one": "soccer/fra.1",
    "soccer_uefa_champs_league": "soccer/uefa.champions",
    "soccer_uefa_europa_league": "soccer/uefa.europa",
    "soccer_netherlands_eredivisie": "soccer/ned.1",
    "soccer_portugal_primeira_liga": "soccer/por.1",
    "soccer_usa_mls": "soccer/usa.1",
    "soccer_brazil_campeonato": "soccer/bra.1",
    "soccer_argentina_primera_division": "soccer/arg.1",
    "basketball_nba": "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "americanfootball_ncaaf": "football/college-football",
    "baseball_mlb": "baseball/mlb",
    "icehockey_nhl": "hockey/nhl",
}
EXTRA = {"americanfootball_ncaaf": "&groups=80"}  # todos los partidos de primera división, no solo el top 25
# Fuentes gratuitas para lo que el scoreboard normal de ESPN no trae
EUROLIGA = "https://api-live.euroleague.net/v2/competitions/E/seasons/E{temporada}/games"
UFC = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard?dates={fecha}"
VENTANA_AMPLIA = {"basketball_euroleague", "mma_mixed_martial_arts"}  # sin hora exacta: basta el día y los nombres
RELLENO = {"fc", "cf", "sc", "ac", "cd", "ca", "club", "de", "del", "the", "afc", "sv", "fk"}


def cubierto(deporte: str) -> bool:
    return deporte in RUTAS or deporte in VENTANA_AMPLIA


def _json(url: str):
    peticion = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(peticion, timeout=30) as resp:
        return json.load(resp)


def _pedir(deporte: str, fecha) -> list:
    return _json(URL.format(ruta=RUTAS[deporte], fecha=fecha.strftime("%Y%m%d")) + EXTRA.get(deporte, "")).get("events", [])


def _textos(club: dict) -> list[str]:
    """Todos los nombres con que la Euroliga identifica a un club (patrocinador, corto, editorial…)."""
    return [v for k, v in club.items() if isinstance(v, str) and v and "url" not in k.lower() and len(v) > 3]


def _euroliga(fecha) -> list[dict]:
    temporada = fecha.year if fecha.month >= 8 else fecha.year - 1
    datos = _json(EUROLIGA.format(temporada=temporada))
    partidos = []
    for g in datos.get("data", datos) if isinstance(datos, dict) else datos:
        local, visita = g.get("local") or {}, g.get("road") or {}
        if not g.get("date"):
            continue
        partidos.append({
            "inicio": a_fecha(g["date"]).replace(tzinfo=timezone(timedelta(hours=1))),  # hora de Europa central
            "local": _textos(local.get("club") or {}), "visitante": _textos(visita.get("club") or {}),
            "goles_local": local.get("score"), "goles_visitante": visita.get("score"),
            "fase": "post" if g.get("played") else "pre", "terminado": bool(g.get("played")),
            "detalle": "Final" if g.get("played") else None,
        })
    return partidos


def _peleas(fecha) -> list[dict]:
    """Cada pelea de las funciones de UFC: el ganador cuenta como 1-0."""
    peleas = []
    for ev in _json(UFC.format(fecha=fecha.strftime("%Y%m%d"))).get("events", []):
        for comp in ev.get("competitions", []):
            rivales = comp.get("competitors", [])
            if len(rivales) != 2:
                continue
            estado = comp.get("status", {}).get("type", {})
            nombre = lambda r: [(r.get("athlete") or {}).get(k) for k in ("displayName", "fullName", "shortName")
                                if (r.get("athlete") or {}).get(k)]
            peleas.append({
                "inicio": a_fecha(comp.get("date") or ev["date"]),
                "local": nombre(rivales[0]), "visitante": nombre(rivales[1]),
                "goles_local": int(bool(rivales[0].get("winner"))), "goles_visitante": int(bool(rivales[1].get("winner"))),
                "fase": estado.get("state"), "terminado": bool(estado.get("completed")),
                "detalle": estado.get("shortDetail") or estado.get("description"),
            })
    return peleas


def _nombres(equipo: dict) -> list[str]:
    return [equipo[k] for k in ("displayName", "shortDisplayName", "name", "location") if equipo.get(k)]


def _partidos(eventos: list) -> list[dict]:
    partidos = []
    for ev in eventos:
        competidores = {c.get("homeAway"): c for c in ev.get("competitions", [{}])[0].get("competitors", [])}
        if "home" not in competidores or "away" not in competidores or "date" not in ev:
            continue
        estado = ev.get("status", {}).get("type", {})
        partidos.append({
            "inicio": a_fecha(ev["date"]),
            "local": _nombres(competidores["home"].get("team", {})),
            "visitante": _nombres(competidores["away"].get("team", {})),
            "goles_local": competidores["home"].get("score"),
            "goles_visitante": competidores["away"].get("score"),
            "fase": estado.get("state"),            # pre, in, post
            "terminado": bool(estado.get("completed")),
            "detalle": estado.get("shortDetail") or estado.get("description"),
        })
    return partidos


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return " ".join(p for p in re.findall(r"[a-z0-9]+", texto) if p not in RELLENO)


def _parecido(nombre: str, opciones: list[str]) -> float:
    a = _normalizar(nombre)
    mejor = 0.0
    for opcion in opciones:
        b = _normalizar(opcion)
        if not a or not b:
            continue
        if a == b or a in b or b in a:
            return 1.0
        mejor = max(mejor, SequenceMatcher(None, a, b).ratio())
    return mejor


def emparejar(local: str, visitante: str, inicio, candidatos: list[dict],
              ventana_horas: float = 3) -> tuple[dict | None, bool]:
    """Busca el partido de ESPN que corresponde. Devuelve (partido, local/visitante invertidos)."""
    mejor, puntaje_mejor, invertido = None, 0.0, False
    for c in candidatos:
        if abs(c["inicio"] - inicio) > timedelta(hours=ventana_horas):
            continue
        directo = min(_parecido(local, c["local"]), _parecido(visitante, c["visitante"]))
        cruzado = min(_parecido(local, c["visitante"]), _parecido(visitante, c["local"]))
        for puntaje, inv in ((directo, False), (cruzado, True)):
            if puntaje > puntaje_mejor:
                mejor, puntaje_mejor, invertido = c, puntaje, inv
    return (mejor, invertido) if puntaje_mejor >= 0.6 else (None, False)


def candidatos(deporte: str, inicio, cache: dict) -> list[dict]:
    """Partidos del día del evento y del anterior (ESPN agrupa por hora del este de EE. UU.)."""
    lista = []
    fechas = [inicio.date()] if deporte == "basketball_euroleague" else {inicio.date(), (inicio - timedelta(days=1)).date()}
    for fecha in fechas:
        clave = ("euroliga",) if deporte == "basketball_euroleague" else (deporte, fecha)  # la Euroliga trae toda la temporada
        if clave not in cache:
            try:
                if deporte == "basketball_euroleague":
                    cache[clave] = _euroliga(fecha)
                elif deporte == "mma_mixed_martial_arts":
                    cache[clave] = _peleas(fecha)
                else:
                    cache[clave] = _partidos(_pedir(deporte, fecha))
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                cache[clave] = []
        lista += cache[clave]
    return lista


def actualizar(con) -> tuple[int, int]:
    """Actualiza el marcador de los partidos apostados que ya empezaron.
    Devuelve (partidos en juego, partidos terminados)."""
    pendientes = con.execute(
        """SELECT DISTINCT e.id, e.deporte, e.local, e.visitante, e.inicio FROM eventos e
           JOIN apuestas a ON a.evento_id = e.id
           WHERE a.estado = 'abierta' AND e.terminado = 0 AND e.inicio <= ?""", (iso(ahora()),)).fetchall()
    cache, vivos, terminados = {}, 0, 0
    for ev in pendientes:
        if not cubierto(ev["deporte"]):
            continue
        inicio = a_fecha(ev["inicio"])
        partido, invertido = emparejar(ev["local"], ev["visitante"], inicio,
                                       candidatos(ev["deporte"], inicio, cache),
                                       30 if ev["deporte"] in VENTANA_AMPLIA else 3)
        if not partido or partido["fase"] == "pre":
            continue
        if partido["fase"] == "post" and not partido["terminado"]:  # suspendido o pospuesto
            con.execute("UPDATE eventos SET detalle = ? WHERE id = ?", (partido["detalle"], ev["id"]))
            continue
        try:
            goles_local, goles_visitante = int(float(partido["goles_local"])), int(float(partido["goles_visitante"]))
        except (TypeError, ValueError):
            continue
        if invertido:
            goles_local, goles_visitante = goles_visitante, goles_local
        terminado = int(partido["fase"] == "post")
        con.execute("""UPDATE eventos SET marcador_local = ?, marcador_visitante = ?, detalle = ?, terminado = ?
                       WHERE id = ?""", (goles_local, goles_visitante, partido["detalle"], terminado, ev["id"]))
        terminados += terminado
        vivos += 1 - terminado
    con.commit()
    return vivos, terminados
