"""Motor del simulador: corre solo mientras la ventana esté abierta y sirve el tablero web.

Cada ciclo (cada 10 minutos):
  1. Pide resultados de los deportes con partidos terminados y liquida apuestas.
  2. Mide el CLV de los partidos que ya empezaron.
  3. Toma la foto de cierre de las ligas con apuestas que empiezan pronto.
  4. En los horarios de barrido, busca momios nuevos y deja que cada estrategia apueste.
  5. Una vez por semana corre la revisión de aprendizaje.
"""
import argparse
import hashlib
import json
import sys
import threading
import traceback
import urllib.error
import webbrowser
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aprendizaje
import avisos
import diario
import estrategias
import estudio
import externos
import marcadores
import riesgo
import tablero
from api import (CARPETA, ErrorAPI, cargar_config, creditos_hoy, deportes_activos, descargar_momios,
                 descargar_resultados, dias_restantes, proximos_inicios)
from base_datos import a_fecha, ahora, anotar, conectar, guardar_estado, iso, leer_estado, podar
from momios import probabilidades_justas

PUERTO = 8765
URL = f"http://127.0.0.1:{PUERTO}"
buscar_ahora = threading.Event()


def log(mensaje: str) -> None:
    print(f"[{datetime.now():%d/%m %H:%M}] {mensaje}", flush=True)


def _seguro(descripcion: str, funcion, *args, **kwargs):
    """Las tareas gratuitas no deben detener el ciclo: si una falla, se registra y se sigue con lo demás."""
    try:
        return funcion(*args, **kwargs)
    except Exception as e:
        log(f"{descripcion} falló y se omitió en este ciclo: {e!r}")
        traceback.print_exc()
        return None


def inicializar(con, config: dict) -> None:
    estrategias.sembrar(con)
    if not leer_estado(con, "fecha_inicio"):
        guardar_estado(con, "fecha_inicio", iso(ahora()))
        guardar_estado(con, "ultima_revision", iso(ahora()))
        anotar(con, "inicio", f"Arranca la simulación con banca ficticia de "
                              f"${config['banca_inicial']:,.0f} {config['moneda']}.")
    fin = a_fecha(leer_estado(con, "fecha_inicio")) + timedelta(days=7 * len(config["objetivos_semana"]))
    guardar_estado(con, "fin_simulacion", fin.astimezone().date().isoformat())  # hasta ahí se reparten los créditos
    if not leer_estado(con, "modo_objetivo_inicio"):  # una vez: la Principal busca la meta con más volumen
        estrategias.ajustar_umbral(con, ("Principal", "Sin cerebro"), 0.015)
        anotar(con, "ajuste", "Modo objetivo: la Principal baja su valor mínimo de 2% a 1.5% para tener más apuestas con "
                              "ventaja (en 18,700 partidos históricos sigue ganando: +4.9%, t = 2.7, con 27% más apuestas). "
                              "El cerebro apuesta menos en las de menor ventaja.")
        guardar_estado(con, "modo_objetivo_inicio", iso(ahora()))
    if not leer_estado(con, "medio_kelly"):  # una vez: de ¼ a ½ de Kelly en las estrategias de valor
        for nombre, parametros in con.execute("SELECT nombre, parametros FROM estrategias WHERE tipo = 'valor'").fetchall():
            p = json.loads(parametros)
            if p.get("kelly", 0.25) == 0.25:
                con.execute("UPDATE estrategias SET parametros = ? WHERE nombre = ?", (json.dumps({**p, "kelly": 0.5}), nombre))
        anotar(con, "ajuste", "Montos: de ¼ a ½ de Kelly. El cerebro ya hace conservadora la ventaja estimada, así que usar además "
                              "solo ¼ de Kelly dejaba apuestas de $70 que no aportan nada. Ahora no se apuesta si la ventaja "
                              "estimada es menor a 0.5%, y el modo objetivo sigue cuidando el riesgo total.")
        guardar_estado(con, "medio_kelly", True)
    # "México real" apuesta en todas las casas mexicanas configuradas (incluidos sus momios mejorados)
    fila = con.execute("SELECT parametros FROM estrategias WHERE rol = 'mexico'").fetchone()
    if fila:
        p, casas = json.loads(fila[0]), [c["clave"] for c in config["casas_mexico"]]
        if p.get("casas_permitidas") != casas:
            con.execute("UPDATE estrategias SET parametros = ? WHERE rol = 'mexico'",
                        (json.dumps({**p, "casas_permitidas": casas}),))
    con.commit()


def _duracion(config: dict, deporte: str) -> timedelta:
    return timedelta(hours=config["duracion_horas"].get(deporte.split("_")[0], 4))


def deportes_por_liquidar(con, config: dict) -> list[str]:
    """Deportes con partidos que ya debieron terminar y que ESPN no resolvió (respaldo con créditos).
    Se agrupan para gastar menos: se piden cuando el más viejo lleva horas esperando o ya son varios."""
    momento = ahora()
    pendientes = {}
    # Los pospuestos no se piden: no habrá resultado, y a las 48 h se anulan solos (regla de las casas)
    for f in con.execute("""SELECT DISTINCT a.evento_id, a.deporte, a.inicio FROM apuestas a
                            JOIN eventos e ON e.id = a.evento_id
                            WHERE a.estado = 'abierta' AND e.terminado = 0 AND e.pospuesto IS NULL"""):
        fin = a_fecha(f["inicio"]) + _duracion(config, f["deporte"])
        if marcadores.cubierto(f["deporte"]) and momento - fin < timedelta(hours=6):
            continue  # se le da tiempo a ESPN antes de gastar créditos
        if fin < momento:
            pendientes.setdefault(f["deporte"], []).append(fin)
    elegidos = []
    for deporte, fines in pendientes.items():
        ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'scores' AND deporte = ?",
                             (deporte,)).fetchone()[0]
        if ultima and momento - a_fecha(ultima) < timedelta(hours=config["horas_min_entre_resultados"]):
            continue
        espera = momento - min(fines)
        if espera >= timedelta(hours=config["horas_espera_resultados"]) or len(fines) >= config["lote_resultados"]:
            elegidos.append((min(fines), deporte))
    return [d for _, d in sorted(elegidos)]


def ligas_para_cierre(con, config: dict) -> list[str]:
    """Ligas con apuestas abiertas que empiezan pronto y sin foto reciente: esa foto es el 'cierre'."""
    momento = ahora()
    limite = momento + timedelta(minutes=config["minutos_captura_cierre"])
    # Donde ESPN publica el cierre de DraftKings, el CLV sale gratis: solo se paga la foto en las demás ligas
    deportes = [f[0] for f in con.execute(
        "SELECT DISTINCT deporte FROM apuestas WHERE estado = 'abierta' AND inicio > ? AND inicio <= ?",
        (iso(momento), iso(limite))) if not marcadores.cubierto_momios(f[0])]
    elegidos = []
    for deporte in deportes:
        ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'odds' AND deporte = ?",
                             (deporte,)).fetchone()[0]
        if not ultima or momento - a_fecha(ultima) >= timedelta(minutes=50):
            elegidos.append(deporte)
    return elegidos


def calendario(con, config: dict, activos: dict) -> dict:
    """{liga: [inicios]} de los partidos de las próximas horas. Se refresca gratis cada pocas horas."""
    actualizado = leer_estado(con, "calendario_actualizado")
    if actualizado and ahora() - a_fecha(actualizado) < timedelta(hours=config["decision"]["horas_calendario"]):
        return leer_estado(con, "calendario", {})
    limite = ahora() + timedelta(hours=config["horas_ventana"])
    cal = {}
    for deporte in config["deportes_candidatos"]:
        if deporte in activos:
            inicios = sorted(t for t in proximos_inicios(config["api_key"], deporte) if ahora() < t <= limite)
            if inicios:
                cal[deporte] = [iso(t) for t in inicios]
    guardar_estado(con, "calendario", cal)
    guardar_estado(con, "calendario_actualizado", iso(ahora()))
    return cal


def _ultima_descarga(con, deporte: str):
    ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'odds' AND deporte = ?",
                         (deporte,)).fetchone()[0]
    return a_fecha(ultima) if ultima else None


def puntajes_descarga(con, config: dict, cal: dict) -> dict:
    """Partidos con valor que se esperan por cada crédito gastado en cada liga ahora mismo:
    urgencia (partidos que empiezan pronto) x antigüedad de sus momios x valor por partido de la liga."""
    momento = ahora()
    valores = aprendizaje.valor_ligas(con, config, list(cal))
    # Partidos próximos en los que la Principal ya apostó: volver a descargarlos sirve menos (solo al laboratorio y a
    # las apuestas fantasma), así que pesan una cuarta parte
    apostados = {}
    for deporte, inicio in con.execute("""SELECT DISTINCT deporte, inicio FROM apuestas WHERE estrategia = 'Principal'
                                          AND estado = 'abierta' AND inicio > ?""", (iso(momento),)):
        apostados.setdefault(deporte, []).append(inicio)
    puntajes = {}
    for deporte, inicios in cal.items():
        urgencia = 0.0
        ya = list(apostados.get(deporte, []))
        for texto in inicios:
            if texto in ya:
                ya.remove(texto)
                peso = 0.25
            else:
                peso = 1.0
            horas = (a_fecha(texto) - momento).total_seconds() / 3600
            if horas > 0.17:
                urgencia += peso * (1.0 if horas <= 3 else 0.6 if horas <= 12 else 0.3 if horas <= 48 else 0.1)
        ultima = _ultima_descarga(con, deporte)
        horas_desde = (momento - ultima).total_seconds() / 3600 if ultima else 99
        if urgencia == 0 or horas_desde < config["horas_min_entre_descargas"]:
            continue
        puntajes[deporte] = urgencia * min(1.0, horas_desde / 4) * valores[deporte]
    return puntajes


def decidir_descargas(con, config: dict, activos: dict, cal: dict, restantes: int) -> int:
    """Cada ciclo decide si vale la pena gastar créditos ahora o guardarlos para un mejor momento.
    Los créditos se acumulan en un ahorro que se llena a ritmo constante (lo que alcanza para el mes)
    y se gasta cuando aparece una liga con partidos próximos, momios viejos y buen historial."""
    d = config["decision"]
    momento = ahora()
    por_hora = max(0.0, (restantes - config["reserva_creditos"]) / dias_restantes(con) * d["porcion_busqueda"] / 24)
    ahorro = leer_estado(con, "ahorro", d["capacidad_ahorro"] / 2)
    ultima = leer_estado(con, "ahorro_actualizado")
    if ultima:
        ahorro += por_hora * (momento - a_fecha(ultima)).total_seconds() / 3600
    ahorro = min(d["capacidad_ahorro"], ahorro)
    costo_liga = len(config["mercados"]) * len(config["region"].split(","))
    puntajes = puntajes_descarga(con, config, cal)
    gastado = 0
    while puntajes and ahorro >= costo_liga and creditos_hoy(con, restantes - gastado, config["reserva_creditos"]) >= costo_liga:
        deporte, puntaje = max(puntajes.items(), key=lambda x: x[1])
        if puntaje < d["puntaje_minimo"]:
            break
        log(f"Decisión: vale la pena ahora {activos.get(deporte, deporte)} (puntaje {puntaje:.1f}, ahorro {ahorro:.1f})")
        costo = apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "barrido")
        gastado += costo
        ahorro -= costo
        del puntajes[deporte]
    guardar_estado(con, "ahorro", round(ahorro, 3))
    guardar_estado(con, "ahorro_actualizado", iso(momento))
    return gastado


def apostar_con_captura(con, config: dict, deporte: str, liga: str, motivo: str) -> int:
    capturado, id_consumo, costo = descargar_momios(con, config, deporte, liga, motivo)
    extra = marcadores.agregar_casa_espn(con, capturado)  # gratis: DraftKings a la misma hora
    if extra:
        log(f"  + momios de DraftKings (ESPN) para {extra} partidos")
    colocadas, senales = estrategias.colocar_apuestas(con, config, capturado)
    con.execute("UPDATE consumo_api SET senales = ? WHERE id = ?", (senales, id_consumo))
    con.commit()
    detalle = ", ".join(f"{n} {e}" for e, n in colocadas.items()) or "sin apuestas"
    log(f"  {liga} ({motivo}): {senales} partidos con valor; apuestas: {detalle}")
    return costo


def barrido_manual(con, config: dict, activos: dict, cal: dict, presupuesto: int) -> int:
    """Búsqueda pedida a mano: descarga las ligas mejor puntuadas hasta agotar el presupuesto."""
    puntajes = puntajes_descarga(con, config, cal)
    log(f"Búsqueda manual: {len(puntajes)} ligas disponibles, presupuesto {presupuesto} créditos")
    gastado = 0
    costo_liga = len(config["mercados"]) * len(config["region"].split(","))
    for deporte in sorted(puntajes, key=puntajes.get, reverse=True):
        if presupuesto - gastado < costo_liga:
            break
        gastado += apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "barrido")
    if not gastado:
        anotar(con, "sistema", "Búsqueda manual sin descargas: todas las ligas con partidos próximos se revisaron hace "
                               f"menos de {config['horas_min_entre_descargas']:g} h (sus momios siguen frescos).")
    return gastado


def ciclo(con, config: dict, forzar_barrido: bool = False) -> None:
    activos, restantes = deportes_activos(config["api_key"])  # gratis
    guardar_estado(con, "restantes", restantes)

    if not leer_estado(con, "correccion_3_vias"):
        estrategias.anular_mercados_distintos(con, config["casa_referencia"])
        guardar_estado(con, "correccion_3_vias", True)
    if not leer_estado(con, "clv_espn_retroactivo"):  # una vez: medir con ESPN el CLV que quedó sin medir
        con.execute("UPDATE apuestas SET clv = NULL WHERE estado = 'anulada'")
        con.execute("UPDATE apuestas SET clv_fuente = 'pinnacle' WHERE clv IS NOT NULL AND clv_fuente IS NULL")
        con.execute("""UPDATE apuestas SET cierre_revisado = 0
                       WHERE clv IS NULL AND cierre_revisado = 1 AND estado != 'anulada'""")
        guardar_estado(con, "clv_espn_retroactivo", True)
    if not leer_estado(con, "partidos_retroactivo"):  # una vez: cuántos partidos traía cada descarga anterior
        con.execute("""UPDATE consumo_api SET partidos = NULLIF((
                           SELECT COUNT(DISTINCT m.evento_id) FROM momios m JOIN eventos e ON e.id = m.evento_id
                           WHERE e.deporte = consumo_api.deporte AND m.casa = ?
                             AND abs(julianday(m.capturado) - julianday(consumo_api.fecha)) < 0.002), 0)
                       WHERE endpoint = 'odds' AND partidos IS NULL""", (config["casa_referencia"],))
        # Las señales de NHL y MMA antes de corregir el error de 3 vías (2 de octubre) eran falsas
        con.execute("""UPDATE consumo_api SET senales = 0 WHERE endpoint = 'odds' AND fecha < '2026-10-02T12:00:00'
                       AND deporte IN ('icehockey_nhl', 'mma_mixed_martial_arts')""")
        guardar_estado(con, "partidos_retroactivo", True)
    if not leer_estado(con, "correccion_empates"):  # una vez: hockey que quedó empatado sin el gol de la tanda
        if _seguro("Corrección de empates", marcadores.revisar_empates, con) is not None:
            guardar_estado(con, "correccion_empates", True)
    if not leer_estado(con, "fantasmas_reconstruidas"):  # una vez: aprender de las fotos que siguen guardadas
        total = _seguro("Reconstruir apuestas fantasma", estrategias.reconstruir_senales, con, config)
        if total is not None:
            guardar_estado(con, "fantasmas_reconstruidas", True)
            anotar(con, "aprendizaje", f"Apuestas fantasma: se reconstruyeron {total:,} precios de las fotos guardadas; "
                                       "desde ahora el bot mide el CLV de todo lo que ve, no solo de lo que apuesta.")

    vivos, terminados = _seguro("Marcadores ESPN", marcadores.actualizar, con) or (0, 0)  # gratis
    if vivos or terminados:
        log(f"Marcadores ESPN: {vivos} partidos en juego, {terminados} terminados")
    for deporte in deportes_por_liquidar(con, config):
        if creditos_hoy(con, restantes, config["reserva_creditos"]) < 2:
            break
        nuevos, costo = descargar_resultados(con, config, deporte)
        restantes -= costo
        log(f"Resultados {activos.get(deporte, deporte)}: {nuevos} partidos terminados")
    liquidadas = estrategias.liquidar(con, config["casa_referencia"])
    if liquidadas:
        log(f"Se liquidaron {liquidadas} apuestas")
    estrategias.calcular_clv(con, config)
    _seguro("Apuestas fantasma", estrategias.medir_senales, con, config)  # gratis: CLV de lo que vio sin apostar
    _seguro("Modo objetivo", riesgo.modo_objetivo, con, config)  # qué tan agresivo apostar en este ciclo

    for deporte in ligas_para_cierre(con, config):
        if creditos_hoy(con, restantes, config["reserva_creditos"]) < 1:
            break
        restantes -= apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "cierre")

    cal = calendario(con, config, activos)  # gratis
    if forzar_barrido:
        disponibles = creditos_hoy(con, restantes, config["reserva_creditos"])
        presupuesto = max(disponibles // 2, min(disponibles, 1))
        if presupuesto < 1:  # cupo de hoy agotado: la búsqueda manual adelanta créditos de los próximos días
            presupuesto = max(0, min(config["decision"]["prestamo_manual"], restantes - config["reserva_creditos"]))
            anotar(con, "sistema", f"Búsqueda manual: el cupo de hoy ya se usó; se adelantan hasta {presupuesto} "
                                   f"créditos de los próximos días.")
        restantes -= barrido_manual(con, config, activos, cal, presupuesto)
    else:
        restantes -= decidir_descargas(con, config, activos, cal, restantes)
    restantes -= _seguro("Momios de Caliente", procesar_externos, con, config, activos, restantes) or 0
    # Gratis: con los momios ya descargados, apuestas que ahora sí entran en la ventana de alguna estrategia
    _seguro("Reanálisis", estrategias.reanalizar, con, config, config["minutos_reanalisis"], silencioso=True)
    _seguro("Búsqueda gratuita", apuestas_gratuitas, con, config)
    _seguro("Resultados de pronósticos", marcadores.resolver_pronosticos, con)  # gratis

    if aprendizaje.toca_revision(con, config):
        aprendizaje.revision(con, config)
    _seguro("Aprendizaje diario", aprendizaje.aprendizaje_diario, con, config)
    _seguro("Alertas de riesgo", riesgo.alertas, con, config)
    try:  # en tiempos muertos: ponerse al día con datos históricos nuevos (gratis)
        if estudio.estudiar(con, config):
            log("Estudio de datos históricos actualizado")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        log(f"Estudio pospuesto para el siguiente ciclo: {e}")

    if leer_estado(con, "ultima_poda") != date.today().isoformat():
        guardar_estado(con, "ultima_poda", date.today().isoformat())
        podar(con, config["casa_referencia"])

    _seguro("Diario", diario.actualizar, con, config)
    _seguro("Telegram", avisos.ciclo, con, config)
    guardar_estado(con, "restantes", restantes)
    guardar_estado(con, "ultimo_ciclo", iso(ahora()))
    con.commit()


def procesar_externos(con, config: dict, activos: dict, restantes: int) -> int:
    """Momios de casas mexicanas leídos a mano (externos/<casa>.json): cada archivo se procesa una sola vez. Si el
    precio justo de Pinnacle de esas ligas tiene más de 45 minutos, primero se actualiza (1 crédito por liga), porque
    una comparación contra una foto vieja daría valor falso. Devuelve los créditos gastados."""
    gastado = 0
    for casa in config["casas_mexico"]:
        datos = externos.cargar(casa["clave"])
        marca = "externo_procesado" if casa["clave"] == "caliente" else f"externo_procesado_{casa['clave']}"
        if not datos or leer_estado(con, marca) == datos["capturado"]:
            continue
        guardar_estado(con, marca, datos["capturado"])
        con.commit()
        nombre = casa["nombre"]
        if not externos.reciente(datos):
            anotar(con, "sistema", f"{nombre}: el archivo de momios es de hace más de 6 h; ya no son precios reales y no se usó.")
            continue
        mapa = externos.emparejar(con, datos)
        # Crédito solo donde vale la pena: ligas donde algún precio mexicano queda cerca o arriba del justo de la
        # última foto de Pinnacle (aunque sea vieja). Se confirma con una foto fresca, máximo unos créditos por barrido
        m, usados = config["mexico"], 0
        for deporte, mejor in sorted(_prometedoras(con, config, mapa).items(), key=lambda x: -x[1]):
            ultima = _ultima_descarga(con, deporte)
            if mejor < m["valor_para_confirmar"] or (ultima and ahora() - ultima < timedelta(minutes=45)):
                continue
            if gastado + usados >= m["max_creditos_por_barrido"] or restantes - gastado - usados - config["reserva_creditos"] < 1:
                break
            usados += apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "barrido")
        gastado += usados
        if not mapa:
            anotar(con, "sistema", f"{nombre}: se leyeron {len(datos.get('partidos', []))} partidos, pero ninguno coincide "
                                   f"con los partidos que sigue el bot ({usados} créditos usados).")
            continue
        con.executemany("INSERT OR REPLACE INTO enlaces (evento_id, casa, url) VALUES (?, ?, ?)",
                        [(e, casa.get("base", casa["clave"]), v["url"]) for e, v in mapa.items() if v.get("url")])
        colocadas, resumen = estrategias.apostar_externo(con, config, mapa, datos["capturado"],
                                                         f"Momio de {nombre} (lectura manual)")
        m = resumen["mejor"]
        anotar(con, "sistema", f"{nombre}: comparé {resumen['comparados']} de {len(mapa)} partidos contra el precio justo de "
                               f"Pinnacle ({usados} créditos para actualizarlo). "
                               + (f"El mejor: {m['partido']} ({m['seleccion']}) a {m['momio']:.2f} contra un justo de "
                                  f"{m['justo']:.2f} ({m['valor']:+.1%}). " if m else "")
                               + ("Apuestas: " + ", ".join(f"{n} {e}" for e, n in colocadas.items()) + "." if colocadas
                                  else "Ninguno cumplió las reglas, así que no aposté."))
        con.commit()
    return gastado


def _prometedoras(con, config: dict, mapa: dict) -> dict:
    """{deporte: mejor valor} de los precios de una casa mexicana contra la última foto de Pinnacle que haya."""
    referencia, mejores = config["casa_referencia"], {}
    for evento, v in mapa.items():
        cap = con.execute("SELECT MAX(capturado) FROM momios WHERE evento_id = ? AND casa = ? AND mercado = 'h2h'",
                          (evento, referencia)).fetchone()[0]
        if not cap:
            continue
        ref = {r[0]: r[1] for r in con.execute("""SELECT seleccion, momio FROM momios WHERE evento_id = ? AND casa = ?
                                                  AND mercado = 'h2h' AND capturado = ?""", (evento, referencia, cap))}
        if len(ref) < 2 or set(ref) != set(v["precios"]):
            continue
        justas = dict(zip(ref, probabilidades_justas(list(ref.values()))))
        mejor = max(justas[s] * v["precios"][s] - 1 for s in ref)
        mejores[v["deporte"]] = max(mejor, mejores.get(v["deporte"], -1.0))
    return mejores


def apuestas_gratuitas(con, config: dict, manual: bool = False) -> None:
    """Momios gratuitos de DraftKings (ESPN) para la Principal y el laboratorio, cada `minutos_gratis` minutos."""
    ultima = leer_estado(con, "gratis_ultimo")
    if not manual and ultima and ahora() - a_fecha(ultima) < timedelta(minutes=config["minutos_gratis"]):
        return
    colocadas, resumen = estrategias.apostar_gratis(con, config)
    guardar_estado(con, "gratis_ultimo", iso(ahora()))
    guardar_estado(con, "gratis_resumen", {**resumen, "fecha": iso(ahora()), "apuestas": sum(colocadas.values())})
    if colocadas:
        log("Apuestas con momios gratuitos: " + ", ".join(f"{n} {e}" for e, n in colocadas.items()))
    if manual:  # búsqueda pedida a mano: explicar qué vio aunque no haya apostado
        m = resumen["mejor"]
        if not resumen["comparados"]:
            texto = (f"no había partidos con precio justo de Pinnacle de menos de {config['max_horas_referencia']} h "
                     f"y momio de DraftKings para comparar; hace falta una búsqueda con créditos.")
        else:
            texto = (f"comparé {resumen['comparados']} partidos de DraftKings contra el precio justo de Pinnacle. "
                     + (f"El mejor fue {m['partido']} ({m['seleccion']}): DraftKings paga {m['momio']:.2f} y el precio "
                        f"justo es {m['justo']:.2f}, valor {m['valor']:+.1%} (la Principal necesita +1.5%). " if m else "")
                     + (f"Apuestas nuevas: " + ", ".join(f"{n} {e}" for e, n in colocadas.items()) + "."
                        if colocadas else "Ninguno cumplió las reglas, así que no aposté."))
        anotar(con, "sistema", "Búsqueda gratis: " + texto)


def analizar_sin_gastar(con, config: dict) -> None:
    """Marcadores, liquidación, CLV y apuestas nuevas con los momios ya descargados: 0 créditos."""
    _seguro("Marcadores ESPN", marcadores.actualizar, con)
    estrategias.liquidar(con, config["casa_referencia"])
    estrategias.calcular_clv(con, config)
    _seguro("Apuestas fantasma", estrategias.medir_senales, con, config)
    _seguro("Modo objetivo", riesgo.modo_objetivo, con, config)
    capturas, nuevas = estrategias.reanalizar(con, config, config["minutos_reanalisis"])
    log(f"Análisis sin gastar: {capturas} descargas revisadas, {nuevas} apuestas nuevas")
    _seguro("Búsqueda gratuita", apuestas_gratuitas, con, config, manual=True)
    _seguro("Resultados de pronósticos", marcadores.resolver_pronosticos, con)
    _seguro("Alertas de riesgo", riesgo.alertas, con, config)
    _seguro("Diario", diario.actualizar, con, config)
    _seguro("Telegram", avisos.ciclo, con, config)
    guardar_estado(con, "ultimo_ciclo", iso(ahora()))
    con.commit()


def exportar(con, config: dict, carpeta: Path) -> None:
    """Tablero estático (página + estado.json) para publicarlo en internet."""
    carpeta.mkdir(parents=True, exist_ok=True)
    pagina = (CARPETA / "web" / "index.html").read_text(encoding="utf-8")
    version = hashlib.sha1(pagina.encode("utf-8")).hexdigest()[:10]  # la página se recarga sola si cambia
    datos = {**tablero.estado(con, config), "version": version}
    (carpeta / "estado.json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    (carpeta / "historial.json").write_text(json.dumps(tablero.historiales(con), ensure_ascii=False), encoding="utf-8")
    (carpeta / "analisis.json").write_text(json.dumps(tablero.analisis_apuestas(con, config), ensure_ascii=False),
                                           encoding="utf-8")
    (carpeta / "index.html").write_text(pagina.replace("__VERSION__", version), encoding="utf-8")
    # Partidos que sigue el bot en las próximas 50 h: el barrido de casas mexicanas solo devuelve esos (menos tokens)
    limite = iso(ahora() + timedelta(hours=50))
    partidos = [[r["deporte"], r["local"], r["visitante"], r["inicio"][:16]] for r in con.execute(
        "SELECT deporte, local, visitante, inicio FROM eventos WHERE inicio > ? AND inicio <= ? ORDER BY inicio",
        (iso(ahora()), limite))]
    (carpeta / "partidos.json").write_text(json.dumps(partidos, ensure_ascii=False), encoding="utf-8")


def bucle_motor() -> None:
    con = conectar()
    while True:
        forzar = buscar_ahora.is_set()
        buscar_ahora.clear()
        config = cargar_config()
        try:
            ciclo(con, config, forzar)
        except ErrorAPI as e:
            log(f"La API respondió con error {e}")
        except urllib.error.URLError as e:
            log(f"Sin conexión a internet: {e.reason}")
        except Exception:
            traceback.print_exc()
        buscar_ahora.wait(timeout=config["minutos_entre_ciclos"] * 60)


class Tablero(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CARPETA / "web"), **kwargs)

    def do_GET(self):
        if self.path.startswith(("/api/estado", "/api/historial", "/api/analisis")):
            con = conectar()
            try:
                if self.path.startswith("/api/historial"):
                    self._json(tablero.historiales(con))
                elif self.path.startswith("/api/analisis"):
                    self._json(tablero.analisis_apuestas(con, cargar_config()))
                else:
                    self._json(tablero.estado(con, cargar_config()))
            finally:
                con.close()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/buscar":
            buscar_ahora.set()
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _json(self, datos) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulador de apuestas deportivas")
    parser.add_argument("--un-ciclo", action="store_true", help="corre un ciclo con barrido y termina")
    parser.add_argument("--ciclo", action="store_true", help="corre un ciclo normal y termina (modo nube)")
    parser.add_argument("--analizar", action="store_true", help="evalúa los momios recientes sin gastar créditos")
    parser.add_argument("--exportar", metavar="CARPETA", help="escribe el tablero estático en esa carpeta")
    parser.add_argument("--sin-motor", action="store_true", help="solo muestra el tablero, sin gastar créditos")
    parser.add_argument("--sin-navegador", action="store_true", help="no abre el navegador")
    args = parser.parse_args()

    config = cargar_config()
    if not args.sin_motor and not config["api_key"].strip():
        print("Falta la clave de The Odds API. El bot ya corre en la nube: https://diegolany.github.io/simulador/")
        return 1
    con = conectar()
    inicializar(con, config)
    if args.un_ciclo or args.ciclo or args.analizar:
        try:
            if args.analizar:
                analizar_sin_gastar(con, config)
            else:
                ciclo(con, config, forzar_barrido=args.un_ciclo)
        except (ErrorAPI, urllib.error.URLError) as e:
            log(f"Ciclo incompleto: {e}")
        if args.exportar:
            exportar(con, config, Path(args.exportar))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        return 0

    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Tablero)
    except OSError:
        print("El simulador ya está abierto en otra ventana; abriendo el tablero.")
        webbrowser.open(URL)
        return 0
    if not args.sin_motor:
        threading.Thread(target=bucle_motor, daemon=True).start()
    print("=" * 60)
    print(f" Simulador de apuestas corriendo. Tablero: {URL}")
    print(" Deja esta ventana abierta; ciérrala para detener el motor.")
    print("=" * 60, flush=True)
    if not args.sin_navegador:
        webbrowser.open(URL)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
