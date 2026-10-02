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
from momios import americano_a_decimal, probabilidades_justas

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
    "soccer_efl_champ": "soccer/eng.2",
    "soccer_germany_bundesliga2": "soccer/ger.2",
    "soccer_italy_serie_b": "soccer/ita.2",
    "soccer_spain_segunda_division": "soccer/esp.2",
    "soccer_france_ligue_two": "soccer/fra.2",
    "soccer_turkey_super_league": "soccer/tur.1",
    "soccer_belgium_first_div": "soccer/bel.1",
    "soccer_spl": "soccer/sco.1",
    "soccer_denmark_superliga": "soccer/den.1",
    "soccer_sweden_allsvenskan": "soccer/swe.1",
    "soccer_norway_eliteserien": "soccer/nor.1",
    "soccer_japan_j_league": "soccer/jpn.1",
    "soccer_korea_kleague1": "soccer/kor.1",
    "soccer_australia_aleague": "soccer/aus.1",
    "soccer_chile_campeonato": "soccer/chi.1",
    "soccer_conmebol_copa_libertadores": "soccer/conmebol.libertadores",
    "soccer_conmebol_copa_sudamericana": "soccer/conmebol.sudamericana",
    "soccer_uefa_europa_conference_league": "soccer/uefa.europa.conf",
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


def cubierto_momios(deporte: str) -> bool:
    """Ligas donde ESPN publica momios (DraftKings) de apertura y cierre."""
    return deporte in RUTAS


def _momios_espn(ev: dict) -> dict | None:
    """Momio actual (o de cierre, si ya empezó) que ESPN publica para local, visitante y empate."""
    for o in ev.get("competitions", [{}])[0].get("odds") or []:
        linea = o.get("moneyline") or {}
        precios = {}
        for lado in ("home", "away", "draw"):
            valor = ((linea.get(lado) or {}).get("close") or {}).get("odds")
            try:
                if valor:
                    precios[lado] = americano_a_decimal(100.0 if valor == "EVEN" else float(valor))
            except ValueError:
                pass
        if "home" in precios and "away" in precios:
            casa = ((o.get("provider") or {}).get("name") or "espn").lower().replace(" ", "")
            return {"casa": casa, **precios}
    return None


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
            "momios": _momios_espn(ev),
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


def precios(deporte: str, local: str, visitante: str, inicio, cache: dict) -> tuple[str, dict] | None:
    """(casa, {selección: momio}) que ESPN publica para un partido, con los nombres de The Odds API."""
    if not cubierto_momios(deporte):
        return None
    partido, invertido = emparejar(local, visitante, inicio, candidatos(deporte, inicio, cache))
    m = partido.get("momios") if partido else None
    if not m:
        return None
    por_seleccion = {local: m["away" if invertido else "home"], visitante: m["home" if invertido else "away"]}
    if "draw" in m:
        por_seleccion["Draw"] = m["draw"]
    return m["casa"], por_seleccion


def agregar_casa_espn(con, capturado: str) -> int:
    """Suma a una descarga de momios los de la casa que publica ESPN (DraftKings) con la misma hora:
    una casa más para comparar contra Pinnacle sin gastar créditos. Devuelve cuántos partidos agregó."""
    cache, agregados = {}, 0
    for ev in con.execute("""SELECT DISTINCT e.id, e.deporte, e.local, e.visitante, e.inicio
                             FROM momios m JOIN eventos e ON e.id = m.evento_id WHERE m.capturado = ?""",
                          (capturado,)).fetchall():
        inicio = a_fecha(ev["inicio"])
        encontrado = precios(ev["deporte"], ev["local"], ev["visitante"], inicio, cache) if inicio > ahora() else None
        if not encontrado:
            continue
        casa, por_seleccion = encontrado
        if con.execute("SELECT 1 FROM momios WHERE evento_id = ? AND capturado = ? AND casa = ? LIMIT 1",
                       (ev["id"], capturado, casa)).fetchone():
            continue  # dos descargas en el mismo segundo: ya se agregó
        for seleccion, momio in por_seleccion.items():
            con.execute("""INSERT INTO momios (evento_id, casa, mercado, seleccion, punto, momio, capturado, actualizado_casa)
                           VALUES (?, ?, 'h2h', ?, NULL, ?, ?, ?)""",
                        (ev["id"], casa, seleccion, round(momio, 3), capturado, capturado))
        agregados += 1
    con.commit()
    return agregados


def probabilidad_cierre(deporte: str, local: str, visitante: str, inicio, seleccion: str, cache: dict) -> float | None:
    """Probabilidad justa de la selección según el momio de cierre que publica ESPN (sin la comisión)."""
    encontrado = precios(deporte, local, visitante, inicio, cache)
    if not encontrado or seleccion not in encontrado[1]:
        return None
    por_seleccion = encontrado[1]
    selecciones = list(por_seleccion)
    justas = dict(zip(selecciones, probabilidades_justas([por_seleccion[s] for s in selecciones])))
    return justas[seleccion]


def resolver_pronosticos(con, limite: int = 200) -> int:
    """Pone el resultado a cada partido pronosticado (aunque no se haya apostado), gratis, para medir la
    calibración con muchos más partidos. Devuelve cuántos resolvió."""
    momento = ahora()
    filas = con.execute("""SELECT evento_id, deporte, local, visitante, inicio FROM pronosticos
                           WHERE resultado IS NULL AND inicio < ? ORDER BY inicio LIMIT ?""",
                        (iso(momento - timedelta(hours=3)), limite)).fetchall()
    cache, resueltos = {}, 0
    for f in filas:
        inicio = a_fecha(f["inicio"])
        goles = None
        ev = con.execute("SELECT marcador_local, marcador_visitante, terminado FROM eventos WHERE id = ?",
                         (f["evento_id"],)).fetchone()
        if ev and ev["terminado"]:
            goles = (ev["marcador_local"], ev["marcador_visitante"])
        elif cubierto(f["deporte"]):
            partido, invertido = emparejar(f["local"], f["visitante"], inicio, candidatos(f["deporte"], inicio, cache),
                                           30 if f["deporte"] in VENTANA_AMPLIA else 3)
            if partido and partido["fase"] == "post" and partido["terminado"]:
                try:
                    goles = (int(float(partido["goles_local"])), int(float(partido["goles_visitante"])))
                    goles = goles[::-1] if invertido else goles
                except (TypeError, ValueError):
                    goles = None
        if goles:
            resultado = "local" if goles[0] > goles[1] else "visitante" if goles[1] > goles[0] else "empate"
        elif momento - inicio > timedelta(days=3):
            resultado = "sin_dato"
        else:
            continue
        con.execute("UPDATE pronosticos SET resultado = ? WHERE evento_id = ?", (resultado, f["evento_id"]))
        resueltos += 1
    con.commit()
    return resueltos


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
