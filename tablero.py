"""Arma los datos que muestra el tablero web (nunca incluye la clave de la API)."""
import json
import math
import traceback
from calendar import monthrange
from datetime import date, timedelta

import cerebro
import riesgo
from aprendizaje import (calibracion_pronosticos, calidad_pronosticos, clv_fantasma, estadistica, evidencia,
                         resumen_fantasmas, senales_medidas)
from estrategias import PARAMETROS_BASE
from api import creditos_hoy, dias_restantes as dias_para_repartir
from base_datos import a_fecha, ahora, iso, leer_estado
from momios import decimal_a_americano, probabilidades_justas


# Reloj de cada deporte: (periodos, minutos por periodo, nombre del periodo, minutos reales por minuto de juego,
# minutos del descanso de medio tiempo). Los minutos reales incluyen pausas, tiempos fuera y comerciales.
RELOJES = {
    "americanfootball_nfl": (4, 15, "cuarto", 3.0, 13),
    "americanfootball_ncaaf": (4, 15, "cuarto", 3.3, 20),
    "basketball_nba": (4, 12, "cuarto", 2.6, 15),
    "basketball_euroleague": (4, 10, "cuarto", 2.6, 15),
    "icehockey_nhl": (3, 20, "periodo", 1.9, 18),  # en hockey hay descanso después del 1º y del 2º periodo
}


def _ordinal(n: int, femenino: bool = False) -> str:
    if femenino:
        return f"{n}ª"
    return {1: "1er", 3: "3er"}.get(n, f"{n}º")


def _progreso(a: dict, fase: str, momento, duracion_h: float) -> dict:
    """En qué parte va el partido y a qué hora se estima que termine (con el reloj de ESPN)."""
    fin_previsto = a_fecha(a["inicio"]) + timedelta(hours=duracion_h)
    if fase == "por_empezar":
        return {"texto": None, "fin": iso(fin_previsto)}
    if fase != "en_juego":
        return {"texto": None, "fin": None}
    deporte, periodo, reloj = a["deporte"], a["periodo"], a["reloj"] or 0
    detalle, reloj_texto = a["detalle"] or "", a["reloj_texto"] or ""
    leido = a_fecha(a["leido_vivo"]) if a["leido_vivo"] else momento
    medio = any(x in detalle.lower() for x in ("half", "ht"))
    if deporte.startswith("soccer") and periodo:
        minuto = reloj / 60
        if medio and periodo == 1:
            texto, resta = "Medio tiempo", 15 + 45 + 3
        elif periodo > 2:
            texto, resta = f"Tiempo extra · minuto {reloj_texto}", max(0, 120 - minuto) + 2
        else:
            texto = f"{_ordinal(periodo)} tiempo · minuto {reloj_texto}"
            resta = max(0, 90 - minuto) + 3 + (15 if periodo == 1 else 0)  # 3 min de reposición y el descanso
    elif deporte in RELOJES and periodo:
        n, largo, nombre, factor, descanso = RELOJES[deporte]
        if periodo > n:
            texto, resta = f"Tiempo extra · faltan {reloj_texto}", max(5, reloj / 60 * factor)
        else:
            if medio:
                texto = "Medio tiempo"
            elif reloj <= 0:
                texto = f"Fin del {_ordinal(periodo)} {nombre}"
            else:
                texto = f"{_ordinal(periodo)} {nombre} · faltan {reloj_texto} del {nombre}"
            resta = (reloj / 60 + (n - periodo) * largo) * factor
            if deporte.startswith("icehockey"):
                resta += descanso * (n - periodo)
            elif periodo <= n // 2:
                resta += descanso
    elif deporte.startswith("baseball") and periodo:
        mitad = "alta" if detalle.startswith("Top") else "baja" if detalle.startswith("Bot") else "cambio"
        medias = max(1, (9 - periodo) * 2 + (2 if mitad == "alta" else 1 if mitad in ("baja", "cambio") else 0))
        texto = f"{_ordinal(periodo, True)} entrada" + ({"alta": " (parte alta)", "baja": " (parte baja)"}.get(mitad, ""))
        resta = medias * 10  # unos 10 minutos reales por media entrada
    else:  # sin reloj en vivo (Euroliga, peleas): según la duración típica
        return {"texto": None, "fin": iso(fin_previsto) if fin_previsto > momento else None}
    return {"texto": texto, "fin": iso(leido + timedelta(minutes=resta)), "leido": iso(leido)}


def _seguro(funcion, *args):
    """Una sección que falle no debe tumbar todo el tablero: se omite y se registra el error."""
    try:
        return funcion(*args)
    except Exception:
        traceback.print_exc()
        return None

# Casas cuyos momios no cuestan créditos: DraftKings (ESPN) y las casas mexicanas que se leen a mano
ORIGENES_GRATIS = ("draftkings", "caliente", "codere_mx", "strendus", "betano_mx", "playdoit", "draftea")
DEPORTES = {"soccer": "Fútbol", "basketball": "Básquetbol", "americanfootball": "Fútbol americano",
            "baseball": "Béisbol", "icehockey": "Hockey", "mma": "MMA", "boxing": "Box", "tennis": "Tenis"}


def nombre_deporte(clave: str) -> str:
    return DEPORTES.get(clave.split("_")[0], clave)


def _resumen(apuestas: list[dict]) -> dict:
    """Métricas de un grupo de apuestas."""
    decididas = [a for a in apuestas if a["estado"] in ("ganada", "perdida")]
    ganadas = sum(1 for a in decididas if a["estado"] == "ganada")
    apostado = sum(a["monto"] for a in decididas)
    ganancia = sum(a["ganancia"] or 0 for a in apuestas if a["estado"] != "abierta")
    n_clv, clv, _ = estadistica([a["clv"] for a in apuestas if a["clv"] is not None])
    return {
        "apuestas": sum(1 for a in apuestas if a["estado"] != "anulada"),
        "abiertas": sum(1 for a in apuestas if a["estado"] == "abierta"),
        "liquidadas": len(decididas),
        "ganadas": ganadas,
        "acierto": ganadas / len(decididas) if decididas else None,
        "apostado": apostado,
        "ganancia": ganancia,
        "rendimiento": ganancia / apostado if apostado else None,
        "clv": clv if n_clv else None,
        "clv_n": n_clv,
        "gana_cierre": (sum(1 for a in apuestas if (a["clv"] or 0) > 0 and a["estado"] != "anulada") / n_clv
                        if n_clv else None),
        "en_juego": sum(a["monto"] for a in apuestas if a["estado"] == "abierta"),
    }


def _ultima_ref(con, evento_id: str, referencia: str, antes_de: str) -> tuple[str, dict] | tuple[None, None]:
    capturado = con.execute("""SELECT MAX(capturado) FROM momios WHERE evento_id = ? AND casa = ?
                               AND mercado = 'h2h' AND capturado < ?""",
                            (evento_id, referencia, antes_de)).fetchone()[0]
    if not capturado:
        return None, None
    momios = {r[0]: r[1] for r in con.execute(
        "SELECT seleccion, momio FROM momios WHERE evento_id = ? AND casa = ? AND mercado = 'h2h' AND capturado = ?",
        (evento_id, referencia, capturado))}
    return capturado, momios


def _curva(liquidadas: list[dict], inicial: float, inicio, objetivos: list[float], hasta) -> dict:
    """Banca real y esperada (según el valor de cada apuesta) con su rango normal por suerte, después de cada
    apuesta cerrada; y el objetivo día por día."""
    dias_total = max(28, math.ceil((hasta - inicio).total_seconds() / 86400))

    def objetivo(dia: float) -> float:
        puntos = [(0, 0.0)] + [(7 * (i + 1), o) for i, o in enumerate(objetivos)]
        for (d0, o0), (d1, o1) in zip(puntos, puntos[1:]):
            if dia <= d1:
                return inicial * (1 + o0 + (o1 - o0) * (dia - d0) / (d1 - d0))
        ultimo_dia, ultimo = puntos[-1]
        return inicial * (1 + ultimo * dia / ultimo_dia)

    # Un punto por cada apuesta que se cierra (la banca solo cambia ahí) y uno final en este momento
    transcurrido = max(0.0, (hasta - inicio).total_seconds() / 86400)
    real, esperado, banda_alta, banda_baja = [], [], [], []
    r = e = inicial
    varianza = 0.0

    def punto(x):
        sd = math.sqrt(varianza)
        real.append({"x": round(x, 4), "y": round(r, 2)})
        esperado.append({"x": round(x, 4), "y": round(e, 2)})
        banda_alta.append({"x": round(x, 4), "y": round(e + sd, 2)})
        banda_baja.append({"x": round(x, 4), "y": round(e - sd, 2)})

    punto(0)
    for a in sorted(liquidadas, key=lambda a: a["liquidada"]):
        r += a["ganancia"] or 0
        if a["estado"] in ("ganada", "perdida"):
            e += a["monto"] * a["valor"]
            varianza += (a["monto"] * a["momio"]) ** 2 * a["prob_justa"] * (1 - a["prob_justa"])
        punto(max(0.0, (a_fecha(a["liquidada"]) - inicio).total_seconds() / 86400))
    punto(transcurrido)
    return {
        "objetivo": [{"x": d, "y": round(objetivo(d), 2)} for d in range(dias_total + 1)],
        "real": real, "esperado": esperado, "banda_alta": banda_alta, "banda_baja": banda_baja,
        "hoy": round(transcurrido, 4), "dias": dias_total,
    }


def _creditos(con, config: dict, inicio, restantes) -> dict:
    """Créditos de la API usados por día y motivo, contra el ritmo que alcanza para todo el mes."""
    total, reserva = config["creditos_mes"], config["reserva_creditos"]
    vacio = {"barrido": 0, "cierre": 0, "resultados": 0}
    por_dia = {}
    for fecha, motivo, costo in con.execute("SELECT fecha, motivo, costo FROM consumo_api"):
        dia = por_dia.setdefault(a_fecha(fecha).astimezone().date().isoformat(), dict(vacio))
        dia[motivo or "barrido"] += costo
    primero, hoy = inicio.astimezone().date(), date.today()
    serie = []
    for i in range(max(28, (hoy - primero).days + 1)):
        dia = (primero + timedelta(days=i)).isoformat()
        serie.append({"dia": dia, **por_dia.get(dia, vacio)})
    dias_restantes = dias_para_repartir(con)
    return {
        "total": total, "reserva": reserva, "restantes": restantes,
        "usados": total - restantes if restantes is not None else None,
        "hoy": sum(por_dia.get(hoy.isoformat(), vacio).values()),
        "disponibles_hoy": creditos_hoy(con, restantes, reserva) if restantes is not None else None,
        "plan_diario": (total - reserva) / monthrange(hoy.year, hoy.month)[1],
        "ritmo_restante": (restantes - reserva) / dias_restantes if restantes is not None else None,
        "por_dia": serie,
    }


def _decisiones(con) -> list[dict]:
    """Últimas veces que el bot decidió gastar créditos, y qué encontró."""
    nombres = {r[0]: r[1] for r in con.execute("SELECT deporte, MAX(liga) FROM eventos GROUP BY deporte")}
    return [{"fecha": r["fecha"], "motivo": r["motivo"], "liga": nombres.get(r["deporte"], r["deporte"]),
             "costo": r["costo"], "senales": r["senales"]}
            for r in con.execute("SELECT fecha, motivo, deporte, costo, senales FROM consumo_api ORDER BY id DESC LIMIT 8")]


def _evidencia(con) -> dict | None:
    """Resultados de la prueba con temporadas pasadas (la más reciente del estudio automático)."""
    datos = evidencia(con)
    if not datos:
        return None
    partidos = sum(c["partidos"] for c in datos["calidad"].values())
    return {"umbral": datos["umbral"], "partidos": partidos, "estrategias": datos["total"],
            "fecha": leer_estado(con, "estudio_fecha")}


def _origen(a: dict) -> str:
    return a["casa"] if a["casa"] in ORIGENES_GRATIS else "creditos"


def _fila(a: dict) -> dict:
    """Una apuesta tal como se muestra en historiales; `fuente` dice de dónde salió el momio."""
    return {
        "estrategia": a["estrategia"], "colocada": a["colocada"], "liquidada": a["liquidada"],
        "partido": f"{a['local']} vs {a['visitante']}", "liga": a["liga"], "deporte": nombre_deporte(a["deporte"]),
        "seleccion": "Empate" if a["seleccion"] == "Draw" else a["seleccion"], "casa": a["casa"],
        "fuente": _origen(a),
        "momio": a["momio"], "momio_visto": a["momio_visto"], "monto": a["monto"], "estado": a["estado"],
        "ganancia": a["ganancia"],
        "marcador": f"{a['marcador_local']}-{a['marcador_visitante']}" if a["marcador_local"] is not None else None,
        "clv": a["clv"], "clv_fuente": a["clv_fuente"], "nota": a["nota"], "razon": a["razon"],
        "valor": a["valor"], "ventaja": a["ventaja_estimada"],
    }


def _cartera(apuestas: list[dict], inicial: float, inicio, momento, objetivos: list[float]) -> dict:
    """Banca, objetivos por semana, curva y resultados por deporte y liga de una cartera."""
    liquidadas = [a for a in apuestas if a["estado"] != "abierta"]
    resumen = _resumen(apuestas)
    banca = inicial + resumen["ganancia"]

    # Objetivo vs real por semana
    semanas = []
    for i, objetivo in enumerate(objetivos):
        desde, hasta = inicio + timedelta(days=7 * i), inicio + timedelta(days=7 * (i + 1))
        corte = iso(min(hasta, momento))
        real = sum(a["ganancia"] or 0 for a in liquidadas if a["liquidada"] <= corte) / inicial
        if momento >= hasta:
            situacion = "cumplido" if real >= objetivo else "no_cumplido"
        elif momento >= desde:
            situacion = "en_curso"
        else:
            situacion = "pendiente"
        semanas.append({"semana": i + 1, "desde": iso(desde), "hasta": iso(hasta), "objetivo": objetivo,
                        "banca_objetivo": inicial * (1 + objetivo),
                        "real": real if situacion != "pendiente" else None,
                        "banca_real": inicial * (1 + real) if situacion != "pendiente" else None,
                        "situacion": situacion})

    # Por deporte y por liga
    por_deporte, por_liga = {}, {}
    for a in apuestas:
        if a["estado"] == "anulada":
            continue  # devueltas: no cuentan como apuestas del deporte
        por_deporte.setdefault(nombre_deporte(a["deporte"]), []).append(a)
        por_liga.setdefault((nombre_deporte(a["deporte"]), a["liga"]), []).append(a)
    deportes = sorted(({"deporte": d, **_resumen(lista)} for d, lista in por_deporte.items()),
                      key=lambda x: -x["apuestas"])
    ligas = sorted(({"deporte": d, "liga": l, **_resumen(lista)} for (d, l), lista in por_liga.items()),
                   key=lambda x: (x["deporte"], -x["apuestas"]))
    return {"banca": banca, "disponible": banca - resumen["en_juego"], "resumen": resumen, "semanas": semanas,
            "curva": _curva(liquidadas, inicial, inicio, objetivos, momento), "deportes": deportes, "ligas": ligas}


def historiales(con) -> dict:
    """Todas las apuestas de cada estrategia (abiertas y cerradas), para el historial del laboratorio. Va en un
    archivo aparte que la página solo pide al abrir una estrategia: crece con cada apuesta y haría lento el tablero."""
    por_estrategia = {}
    for a in con.execute("""SELECT a.*, e.local, e.visitante, e.marcador_local, e.marcador_visitante
                            FROM apuestas a JOIN eventos e ON e.id = a.evento_id ORDER BY a.colocada DESC"""):
        por_estrategia.setdefault(a["estrategia"], []).append(_fila(dict(a)))
    return por_estrategia


def plan_real(con, config: dict) -> dict:
    """Lo que hace falta para pasar a dinero real: cómo van las casas mexicanas (precios leídos, CLV de sus apuestas
    fantasma, mejor valor visto) y la cartera "México real"."""
    casas = []
    for c in config["casas_mexico"]:
        n, medidas, clv, gana, mejor, ultima = con.execute(
            """SELECT COUNT(*), COUNT(clv), AVG(clv), AVG(CASE WHEN clv IS NOT NULL THEN clv > 0 END), MAX(valor),
                      MAX(capturado) FROM senales WHERE casa = ?""", (c["clave"],)).fetchone()
        con_valor = con.execute("SELECT COUNT(*), AVG(clv) FROM senales WHERE casa = ? AND valor >= 0.015 AND clv IS NOT NULL",
                                (c["clave"],)).fetchone()
        casas.append({**c, "precios": n, "medidos": medidas, "clv": clv, "gana_cierre": gana, "mejor_valor": mejor,
                      "ultimo_barrido": ultima, "con_valor": con_valor[0], "clv_con_valor": con_valor[1]})
    return {"casas": casas}


def analisis_apuestas(con, config: dict) -> dict:
    """Análisis completo de cada apuesta de la Principal (lo que consideró al apostar, el resultado, el CLV y la suerte).
    Va en un archivo aparte que la página solo pide al abrir una apuesta en el diario."""
    salida = {}
    for a in con.execute("""SELECT a.*, e.local, e.visitante, e.marcador_local, e.marcador_visitante, e.detalle
                            FROM apuestas a JOIN eventos e ON e.id = a.evento_id
                            WHERE a.estrategia = 'Principal' ORDER BY a.colocada DESC"""):
        a = dict(a)
        cerrada = a["estado"] in ("ganada", "perdida")
        esperado = a["monto"] * a["valor"]
        salida[str(a["id"])] = {
            "partido": f"{a['local']} vs {a['visitante']}", "liga": a["liga"], "deporte": nombre_deporte(a["deporte"]),
            "inicio": a["inicio"], "colocada": a["colocada"], "casa": a["casa"], "fuente": _origen(a),
            "seleccion": "Empate" if a["seleccion"] == "Draw" else a["seleccion"], "local": a["local"],
            "visitante": a["visitante"], "momio": a["momio"], "momio_visto": a["momio_visto"], "monto": a["monto"],
            "prob": a["prob_justa"], "valor": a["valor"], "ventaja": a["ventaja_estimada"],
            "gana_si": a["monto"] * (a["momio"] - 1), "esperado": esperado, "estado": a["estado"], "ganancia": a["ganancia"],
            "marcador": f"{a['marcador_local']}-{a['marcador_visitante']}" if a["marcador_local"] is not None else None,
            "nota": a["nota"], "razon": a["razon"],
            "cierre": {"prob": a["prob_cierre"], "clv": a["clv"], "fuente": a["clv_fuente"]} if a["clv"] is not None else None,
            "suerte": (a["ganancia"] - esperado) if cerrada else None,
            "detalle": json.loads(a["analisis"]) if a["analisis"] else None,
        }
    return salida


def estado(con, config: dict) -> dict:
    momento = ahora()
    inicial = config["banca_inicial"]
    referencia = config["casa_referencia"]
    texto_inicio = leer_estado(con, "fecha_inicio")
    inicio = a_fecha(texto_inicio) if texto_inicio else momento
    objetivos = config["objetivos_semana"]

    todas = [dict(r) for r in con.execute(
        """SELECT a.*, e.local, e.visitante, e.marcador_local, e.marcador_visitante, e.detalle, e.pospuesto,
                  e.periodo, e.reloj, e.reloj_texto, e.leido_vivo
           FROM apuestas a JOIN eventos e ON e.id = a.evento_id ORDER BY a.colocada""")]
    principal = _cartera([a for a in todas if a["estrategia"] == "Principal"], inicial, inicio, momento, objetivos)

    # Apuestas activas con el movimiento del mercado desde que se apostó
    activas = []
    for a in todas:
        if a["estado"] != "abierta":
            continue
        inicio_partido = a_fecha(a["inicio"])
        duracion = config["duracion_horas"].get(a["deporte"].split("_")[0], 4)
        if a["pospuesto"]:
            fase = "pospuesto"  # se devuelve el dinero si no se juega en 48 h (o de inmediato si se canceló)
        elif momento < inicio_partido:
            fase = "por_empezar"
        elif a["detalle"] or momento < inicio_partido + timedelta(hours=duracion):
            fase = "en_juego"
        else:
            fase = "esperando_resultado"
        capturado, momios = _ultima_ref(con, a["evento_id"], referencia, min(a["inicio"], iso(momento)))
        movimiento = momio_ref_actual = None
        if capturado and capturado > a["colocada"] and a["seleccion"] in momios:
            justas = dict(zip(momios, probabilidades_justas(list(momios.values()))))
            movimiento = a["momio"] * justas[a["seleccion"]] - 1
            momio_ref_actual = momios[a["seleccion"]]
        activas.append({
            "estrategia": a["estrategia"], "local": a["local"], "visitante": a["visitante"],
            "partido": f"{a['local']} vs {a['visitante']}", "liga": a["liga"],
            "deporte": nombre_deporte(a["deporte"]), "inicio": a["inicio"], "colocada": a["colocada"], "fase": fase,
            "marcador": (f"{a['marcador_local']}-{a['marcador_visitante']}"
                         if a["marcador_local"] is not None else None),
            "detalle": a["detalle"], "pospuesto": a["pospuesto"],
            "seleccion": "Empate" if a["seleccion"] == "Draw" else a["seleccion"], "casa": a["casa"],
            "momio": a["momio"], "americano": decimal_a_americano(a["momio"]), "monto": a["monto"],
            "potencial": a["monto"] * (a["momio"] - 1), "prob": a["prob_justa"], "valor": a["valor"],
            "momio_ref": a["momio_ref"], "momio_ref_actual": momio_ref_actual, "movimiento": movimiento,
            "razon": a["razon"], "fuente": _origen(a),
            "momio_visto": a["momio_visto"], "ventaja": a["ventaja_estimada"],
            "progreso": _progreso(a, fase, momento, duracion),
        })
    activas.sort(key=lambda x: x["inicio"])

    historial = [_fila(a) for a in sorted((a for a in todas if a["estado"] != "abierta"),
                                          key=lambda a: a["liquidada"], reverse=True)[:150]]

    # Laboratorio: todas las estrategias compitiendo, cada una con su banca. La confianza usa el CLV encogido:
    # ya descuenta la suerte de tener muchas estrategias compitiendo a la vez
    posts = _seguro(riesgo.posteriores_laboratorio, con) or {}
    fantasmas = _seguro(senales_medidas, con) or []
    laboratorio = []
    for e in con.execute("SELECT * FROM estrategias ORDER BY CASE rol WHEN 'principal' THEN 0 "
                         "WHEN 'mexico' THEN 1 WHEN 'retadora' THEN 2 WHEN 'experimento' THEN 3 WHEN 'control' THEN 4 ELSE 5 END, creada"):
        propias = [a for a in todas if a["estrategia"] == e["nombre"]]
        r = _resumen(propias)
        por_origen = {o: _resumen([a for a in propias if _origen(a) == o]) for o in ("creditos", *ORIGENES_GRATIS)}
        post = posts.get(e["nombre"])
        confianza = _seguro(riesgo.confianza, [a["clv"] for a in propias if a["clv"] is not None and a["estado"] != "anulada"],
                            post, [a["momio"] for a in propias if a["estado"] in ("ganada", "perdida")])
        caidas = riesgo.caidas(propias, inicial)
        # Laboratorio instantáneo: cómo le habría ido con sus reglas en todas las apuestas fantasma medidas
        prueba = None
        if e["tipo"] == "valor" and fantasmas:
            n, media, ee = estadistica(clv_fantasma(fantasmas, {**PARAMETROS_BASE, **json.loads(e["parametros"])}, config))
            prueba = {"n": n, "clv": media if n else None, "ee": ee if n else None}
        laboratorio.append({"nombre": e["nombre"], "rol": e["rol"], "tipo": e["tipo"],
                            "descripcion": e["descripcion"], "parametros": json.loads(e["parametros"]),
                            "banca": inicial + r["ganancia"], "por_origen": por_origen, "confianza": confianza,
                            "caida_max": caidas["maxima_pct"], "fantasma": prueba, **r})

    # Calibración con todos los partidos pronosticados (se haya apostado o no)
    partidos_calibrados, calibracion = calibracion_pronosticos(con)
    diario = [{"dia": r[0], "secciones": json.loads(r[1]), "actualizado": r[2]}
              for r in con.execute("SELECT dia, texto, actualizado FROM diario ORDER BY dia DESC LIMIT 14")]

    bitacora = [dict(r) for r in con.execute("SELECT fecha, tipo, mensaje FROM bitacora ORDER BY id DESC LIMIT 40")]

    restantes = leer_estado(con, "restantes")
    ultimo_ciclo = leer_estado(con, "ultimo_ciclo")
    ultima_revision = leer_estado(con, "ultima_revision")
    motor_activo = bool(ultimo_ciclo) and (
        momento - a_fecha(ultimo_ciclo) < timedelta(minutes=config["minutos_entre_ciclos"] * 2.5))

    return {
        "generado": iso(momento),
        "moneda": config["moneda"],
        "inicio": iso(inicio),
        "dia": (momento - inicio).days + 1,
        "banca_inicial": inicial,
        **principal,
        "clv_objetivo": config["clv_objetivo"],
        "activas": activas,
        "historial": historial,
        "laboratorio": laboratorio,
        "calibracion": calibracion,
        "calibracion_partidos": partidos_calibrados,
        "calidad_pronosticos": _seguro(calidad_pronosticos, con),
        "fantasmas": _seguro(resumen_fantasmas, con, config),
        "plan": _seguro(plan_real, con, config),
        "riesgo": _seguro(riesgo.panel, con, config),
        "cerebro": _seguro(lambda: cerebro.obtener(con, config).resumen()),
        "config_riesgo": {**config["riesgo"], "deslizamiento_base": config["ejecucion"]["deslizamiento_base"]},
        "diario": diario,
        "bitacora": bitacora,
        "creditos": _creditos(con, config, inicio, restantes),
        "evidencia": _evidencia(con),
        "sistema": {
            "motor_activo": motor_activo,
            "ultimo_ciclo": ultimo_ciclo,
            "creditos_restantes": restantes,
            "creditos_hoy": creditos_hoy(con, restantes, config["reserva_creditos"]) if restantes is not None else None,
            "ahorro": leer_estado(con, "ahorro"),
            "capacidad_ahorro": config["decision"]["capacidad_ahorro"],
            "decisiones": _decisiones(con),
            "proxima_revision": iso(a_fecha(ultima_revision) + timedelta(
                days=config["aprendizaje"]["dias_entre_revisiones"])) if ultima_revision else None,
        },
    }
