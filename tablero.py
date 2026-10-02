"""Arma los datos que muestra el tablero web (nunca incluye la clave de la API)."""
import json
import math
from calendar import monthrange
from datetime import date, timedelta

from aprendizaje import estadistica
from api import creditos_hoy
from base_datos import a_fecha, ahora, iso, leer_estado
from momios import decimal_a_americano, probabilidades_justas

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
        "apuestas": len(apuestas),
        "abiertas": sum(1 for a in apuestas if a["estado"] == "abierta"),
        "liquidadas": len(decididas),
        "ganadas": ganadas,
        "acierto": ganadas / len(decididas) if decididas else None,
        "apostado": apostado,
        "ganancia": ganancia,
        "rendimiento": ganancia / apostado if apostado else None,
        "clv": clv if n_clv else None,
        "clv_n": n_clv,
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
    """Banca real, esperada (según el valor de cada apuesta) y su rango normal por suerte, día por día."""
    dias_total = max(28, math.ceil((hasta - inicio).total_seconds() / 86400))

    def objetivo(dia: float) -> float:
        puntos = [(0, 0.0)] + [(7 * (i + 1), o) for i, o in enumerate(objetivos)]
        for (d0, o0), (d1, o1) in zip(puntos, puntos[1:]):
            if dia <= d1:
                return inicial * (1 + o0 + (o1 - o0) * (dia - d0) / (d1 - d0))
        ultimo_dia, ultimo = puntos[-1]
        return inicial * (1 + ultimo * dia / ultimo_dia)

    orden = sorted(liquidadas, key=lambda a: a["liquidada"])
    marcas = [i for i in range(dias_total + 1) if inicio + timedelta(days=i) <= hasta]
    transcurrido = (hasta - inicio).total_seconds() / 86400
    if not marcas or marcas[-1] < transcurrido:
        marcas.append(transcurrido)
    real, esperado, banda_alta, banda_baja = [], [], [], []
    for dia in marcas:
        corte = iso(inicio + timedelta(days=dia))
        hechas = [a for a in orden if a["liquidada"] <= corte]
        decididas = [a for a in hechas if a["estado"] in ("ganada", "perdida")]
        r = inicial + sum(a["ganancia"] or 0 for a in hechas)
        e = inicial + sum(a["monto"] * a["valor"] for a in decididas)
        sd = math.sqrt(sum((a["monto"] * a["momio"]) ** 2 * a["prob_justa"] * (1 - a["prob_justa"])
                           for a in decididas))
        x = round(dia, 3)
        real.append({"x": x, "y": round(r, 2)})
        esperado.append({"x": x, "y": round(e, 2)})
        banda_alta.append({"x": x, "y": round(e + sd, 2)})
        banda_baja.append({"x": x, "y": round(e - sd, 2)})
    return {
        "objetivo": [{"x": d, "y": round(objetivo(d), 2)} for d in range(dias_total + 1)],
        "real": real, "esperado": esperado, "banda_alta": banda_alta, "banda_baja": banda_baja,
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
    dias_restantes = monthrange(hoy.year, hoy.month)[1] - hoy.day + 1
    return {
        "total": total, "reserva": reserva, "restantes": restantes,
        "usados": total - restantes if restantes is not None else None,
        "hoy": sum(por_dia.get(hoy.isoformat(), vacio).values()),
        "disponibles_hoy": creditos_hoy(con, restantes, reserva) if restantes is not None else None,
        "plan_diario": (total - reserva) / monthrange(hoy.year, hoy.month)[1],
        "ritmo_restante": (restantes - reserva) / dias_restantes if restantes is not None else None,
        "por_dia": serie,
    }


def estado(con, config: dict) -> dict:
    momento = ahora()
    inicial = config["banca_inicial"]
    referencia = config["casa_referencia"]
    texto_inicio = leer_estado(con, "fecha_inicio")
    inicio = a_fecha(texto_inicio) if texto_inicio else momento
    objetivos = config["objetivos_semana"]

    todas = [dict(r) for r in con.execute(
        """SELECT a.*, e.local, e.visitante, e.marcador_local, e.marcador_visitante
           FROM apuestas a JOIN eventos e ON e.id = a.evento_id ORDER BY a.colocada""")]
    principal = [a for a in todas if a["estrategia"] == "Principal"]
    liquidadas = [a for a in principal if a["estado"] != "abierta"]
    resumen = _resumen(principal)
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
    for a in principal:
        por_deporte.setdefault(nombre_deporte(a["deporte"]), []).append(a)
        por_liga.setdefault((nombre_deporte(a["deporte"]), a["liga"]), []).append(a)
    deportes = sorted(({"deporte": d, **_resumen(lista)} for d, lista in por_deporte.items()),
                      key=lambda x: -x["apuestas"])
    ligas = sorted(({"deporte": d, "liga": l, **_resumen(lista)} for (d, l), lista in por_liga.items()),
                   key=lambda x: (x["deporte"], -x["apuestas"]))

    # Apuestas activas con el movimiento del mercado desde que se apostó
    activas = []
    for a in principal:
        if a["estado"] != "abierta":
            continue
        inicio_partido = a_fecha(a["inicio"])
        duracion = config["duracion_horas"].get(a["deporte"].split("_")[0], 4)
        if momento < inicio_partido:
            fase = "por_empezar"
        elif momento < inicio_partido + timedelta(hours=duracion):
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
            "partido": f"{a['local']} vs {a['visitante']}", "liga": a["liga"],
            "deporte": nombre_deporte(a["deporte"]), "inicio": a["inicio"], "fase": fase,
            "seleccion": "Empate" if a["seleccion"] == "Draw" else a["seleccion"], "casa": a["casa"],
            "momio": a["momio"], "americano": decimal_a_americano(a["momio"]), "monto": a["monto"],
            "potencial": a["monto"] * (a["momio"] - 1), "prob": a["prob_justa"], "valor": a["valor"],
            "momio_ref": a["momio_ref"], "momio_ref_actual": momio_ref_actual, "movimiento": movimiento,
        })
    activas.sort(key=lambda x: x["inicio"])

    historial = [{
        "liquidada": a["liquidada"], "partido": f"{a['local']} vs {a['visitante']}", "liga": a["liga"],
        "deporte": nombre_deporte(a["deporte"]),
        "seleccion": "Empate" if a["seleccion"] == "Draw" else a["seleccion"], "casa": a["casa"],
        "momio": a["momio"], "monto": a["monto"], "estado": a["estado"], "ganancia": a["ganancia"],
        "marcador": (f"{a['marcador_local']}-{a['marcador_visitante']}"
                     if a["marcador_local"] is not None else None),
        "clv": a["clv"], "nota": a["nota"],
    } for a in sorted(liquidadas, key=lambda a: a["liquidada"], reverse=True)[:40]]

    # Laboratorio: todas las estrategias compitiendo, cada una con su banca
    laboratorio = []
    for e in con.execute("SELECT * FROM estrategias ORDER BY CASE rol WHEN 'principal' THEN 0 "
                         "WHEN 'retadora' THEN 1 WHEN 'control' THEN 2 ELSE 3 END, creada"):
        r = _resumen([a for a in todas if a["estrategia"] == e["nombre"]])
        laboratorio.append({"nombre": e["nombre"], "rol": e["rol"], "tipo": e["tipo"],
                            "descripcion": e["descripcion"], "parametros": json.loads(e["parametros"]),
                            "banca": inicial + r["ganancia"], **r})

    # Calibración: probabilidad que estimamos contra lo que realmente pasó
    vistas, calibracion = set(), []
    tramos = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    grupos = {t: [] for t in tramos}
    for a in todas:
        if a["estado"] not in ("ganada", "perdida") or (a["evento_id"], a["seleccion"]) in vistas:
            continue
        vistas.add((a["evento_id"], a["seleccion"]))
        for t in tramos:
            if t[0] <= a["prob_justa"] < t[1]:
                grupos[t].append(a)
    for (bajo, alto), lista in grupos.items():
        if lista:
            calibracion.append({"tramo": f"{bajo:.0%}–{min(alto, 1):.0%}", "n": len(lista),
                                "estimada": sum(a["prob_justa"] for a in lista) / len(lista),
                                "real": sum(1 for a in lista if a["estado"] == "ganada") / len(lista)})

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
        "banca": banca,
        "disponible": banca - resumen["en_juego"],
        "resumen": resumen,
        "clv_objetivo": config["clv_objetivo"],
        "semanas": semanas,
        "curva": _curva(liquidadas, inicial, inicio, objetivos, momento),
        "activas": activas,
        "deportes": deportes,
        "ligas": ligas,
        "historial": historial,
        "laboratorio": laboratorio,
        "calibracion": calibracion,
        "bitacora": bitacora,
        "creditos": _creditos(con, config, inicio, restantes),
        "sistema": {
            "motor_activo": motor_activo,
            "ultimo_ciclo": ultimo_ciclo,
            "creditos_restantes": restantes,
            "creditos_hoy": creditos_hoy(con, restantes, config["reserva_creditos"]) if restantes is not None else None,
            "horas_barrido": config["horas_barrido"],
            "proxima_revision": iso(a_fecha(ultima_revision) + timedelta(
                days=config["aprendizaje"]["dias_entre_revisiones"])) if ultima_revision else None,
        },
    }
