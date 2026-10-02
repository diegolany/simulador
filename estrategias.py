"""Estrategias de apuesta, liquidación con resultados reales y medición del CLV.

Cada estrategia tiene su propia banca ficticia. La "Principal" es la cartera de
$100,000 que se muestra en el tablero; las "retadoras" son experimentos que compiten
contra ella; el "Apostador casual" es el grupo de control que muestra por qué
pierde la mayoría.
"""
import json
from datetime import timedelta
from statistics import median

from base_datos import a_fecha, ahora, anotar, iso
from momios import fraccion_kelly, probabilidades_justas, valor_esperado

PARAMETROS_BASE = {
    "umbral": 0.02,         # valor mínimo sobre la probabilidad justa
    "momio_min": 1.30,
    "momio_max": 5.0,
    "horas_min": 0.17,      # no apostar a menos de 10 minutos del inicio
    "horas_max": 48,
    "kelly": 0.25,          # fracción de Kelly
    "tope": 0.02,           # máximo 2% de la banca por apuesta
    "ligas_bloqueadas": [],
    "casas_bloqueadas": [],
}

ESTRATEGIAS_INICIALES = [
    ("Principal", "valor", "principal",
     "Valor de 2% o más contra Pinnacle, momios 1.30 a 5.00, hasta 48 h antes",
     {}),
    ("Valor estricto", "valor", "retadora",
     "Solo valor de 4% o más",
     {"umbral": 0.04}),
    ("Valor amplio", "valor", "retadora",
     "Valor desde 1% y momios hasta 15 (incluye sorpresas)",
     {"umbral": 0.01, "momio_min": 1.15, "momio_max": 15.0}),
    ("Anticipada", "valor", "retadora",
     "Solo apuesta con más de 24 h de anticipación",
     {"horas_min": 24, "horas_max": 72}),
    ("Último momento", "valor", "retadora",
     "Solo apuesta en las 6 h previas al partido",
     {"horas_max": 6}),
    ("Alta certeza", "valor", "retadora",
     "Favoritos con valor: momios 1.25 a 1.80 (en pruebas históricas acertó 68%)",
     {"momio_min": 1.25, "momio_max": 1.80}),
    ("Apostador casual", "favorito", "control",
     "Control: 1% fijo al favorito, al momio promedio de las casas, sin buscar valor",
     {"fijo": 0.01, "horas_max": 24}),
]


def sembrar(con) -> None:
    """Crea las estrategias iniciales que falten (las retiradas o renombradas no se vuelven a crear)."""
    nuevas = []
    for nombre, tipo, rol, descripcion, cambios in ESTRATEGIAS_INICIALES:
        cur = con.execute("INSERT OR IGNORE INTO estrategias (nombre, tipo, rol, descripcion, parametros, creada) "
                          "VALUES (?, ?, ?, ?, ?, ?)",
                          (nombre, tipo, rol, descripcion, json.dumps({**PARAMETROS_BASE, **cambios}), iso(ahora())))
        if cur.rowcount:
            nuevas.append(nombre)
    if len(nuevas) == len(ESTRATEGIAS_INICIALES):
        anotar(con, "inicio", f"Se crearon {len(nuevas)} estrategias: la Principal, {len(nuevas) - 2} retadoras "
                              f"y un grupo de control.")
    elif nuevas:
        anotar(con, "nueva", f"Nueva retadora: {', '.join(nuevas)}. Respaldada por la prueba con temporadas pasadas.")


def activas(con) -> list[dict]:
    filas = con.execute("SELECT nombre, tipo, rol, parametros FROM estrategias WHERE rol != 'retirada'").fetchall()
    return [{"nombre": f["nombre"], "tipo": f["tipo"], "rol": f["rol"], "p": json.loads(f["parametros"])}
            for f in filas]


def banca(con, estrategia: str, inicial: float) -> tuple[float, float]:
    """(banca realizada, dinero en juego en apuestas abiertas)."""
    ganado, en_juego = con.execute(
        """SELECT COALESCE(SUM(CASE WHEN estado != 'abierta' THEN ganancia END), 0),
                  COALESCE(SUM(CASE WHEN estado = 'abierta' THEN monto END), 0)
           FROM apuestas WHERE estrategia = ?""", (estrategia,)).fetchone()
    return inicial + ganado, en_juego


def _foto(con, capturado: str) -> dict:
    """{evento: {casa: {"momios": {seleccion: momio}, "actualizado": iso}}} de una captura."""
    foto = {}
    for f in con.execute("""SELECT evento_id, casa, seleccion, momio, actualizado_casa FROM momios
                            WHERE capturado = ? AND mercado = 'h2h'""", (capturado,)):
        casa = foto.setdefault(f["evento_id"], {}).setdefault(f["casa"], {"momios": {}, "actualizado": None})
        casa["momios"][f["seleccion"]] = f["momio"]
        casa["actualizado"] = f["actualizado_casa"]
    return foto


def colocar_apuestas(con, config: dict, capturado: str) -> tuple[dict, int]:
    """Evalúa la captura recién descargada con cada estrategia activa y registra sus apuestas.
    Devuelve ({estrategia: apuestas colocadas}, partidos con señal de valor)."""
    foto = _foto(con, capturado)
    if not foto:
        return {}, 0
    referencia = config["casa_referencia"]
    excluidas = set(config["casas_excluidas"]) | {referencia}
    momento = a_fecha(capturado)
    max_antiguedad = timedelta(minutes=config["max_minutos_momio"])
    estrategias = activas(con)
    ya_apostadas = {(f[0], f[1]) for f in con.execute(
        f"SELECT estrategia, evento_id FROM apuestas WHERE evento_id IN ({','.join('?' * len(foto))})",
        list(foto))}
    eventos = {f["id"]: f for f in con.execute(
        f"SELECT id, deporte, liga, inicio FROM eventos WHERE id IN ({','.join('?' * len(foto))})", list(foto))}
    colocadas, senales = {}, 0

    for evento_id, casas in foto.items():
        ev = eventos[evento_id]
        horas = (a_fecha(ev["inicio"]) - momento).total_seconds() / 3600
        ref = casas.get(referencia)
        empieza_ya = a_fecha(ev["inicio"]) - ahora() < timedelta(minutes=10)
        if horas <= 0 or empieza_ya or not ref or len(ref["momios"]) < 2:
            continue
        selecciones = list(ref["momios"])
        justas = dict(zip(selecciones, probabilidades_justas([ref["momios"][s] for s in selecciones])))
        ofertas = []
        for casa, datos in casas.items():
            if casa in excluidas or not datos["actualizado"]:
                continue
            if set(datos["momios"]) != set(selecciones):
                continue  # otro mercado: ej. hockey europeo a 3 vías (tiempo regular) contra 2 vías de Pinnacle
            if momento - a_fecha(datos["actualizado"]) > max_antiguedad:
                continue  # momio viejo: probablemente ya no está disponible
            for sel, momio in datos["momios"].items():
                if sel in justas:
                    ofertas.append({"casa": casa, "sel": sel, "momio": momio,
                                    "valor": valor_esperado(justas[sel], momio)})
        if any(0.02 <= o["valor"] <= config["valor_sospechoso"] and o["momio"] <= 10 for o in ofertas):
            senales += 1

        for est in estrategias:
            p = est["p"]
            if (est["nombre"], evento_id) in ya_apostadas or not p["horas_min"] <= horas <= p["horas_max"]:
                continue
            if ev["liga"] in p["ligas_bloqueadas"]:
                continue
            if est["tipo"] == "valor":
                candidatas = [o for o in ofertas
                              if o["casa"] not in p["casas_bloqueadas"]
                              and p["momio_min"] <= o["momio"] <= p["momio_max"]
                              and p["umbral"] <= o["valor"] <= config["valor_sospechoso"]]
                if not candidatas:
                    continue
                elegida = max(candidatas, key=lambda o: o["valor"])
                casa, sel, momio = elegida["casa"], elegida["sel"], elegida["momio"]
                fraccion = fraccion_kelly(justas[sel], momio, p["kelly"], p["tope"])
            else:  # favorito: lo que haría un apostador casual
                sel = max((s for s in selecciones if s != "Draw"), key=lambda s: justas[s])
                precios = [o["momio"] for o in ofertas if o["sel"] == sel]
                if len(precios) < 2:
                    continue
                casa, momio, fraccion = "promedio", round(median(precios), 2), p["fijo"]

            actual, en_juego = banca(con, est["nombre"], config["banca_inicial"])
            monto = int(fraccion * actual / 10) * 10
            if monto < config["apuesta_minima"] or monto > actual - en_juego:
                continue
            con.execute(
                """INSERT INTO apuestas (estrategia, evento_id, deporte, liga, mercado, seleccion, casa, momio,
                                         momio_ref, prob_justa, valor, monto, colocada, inicio)
                   VALUES (?, ?, ?, ?, 'h2h', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (est["nombre"], evento_id, ev["deporte"], ev["liga"], sel, casa, momio, ref["momios"][sel],
                 justas[sel], valor_esperado(justas[sel], momio), monto, capturado, ev["inicio"]),
            )
            ya_apostadas.add((est["nombre"], evento_id))
            colocadas[est["nombre"]] = colocadas.get(est["nombre"], 0) + 1
    con.commit()
    return colocadas, senales


def reanalizar(con, config: dict, max_minutos: int) -> tuple[int, int]:
    """Vuelve a evaluar con las estrategias actuales los momios descargados en los últimos minutos,
    sin gastar créditos. Los más viejos ya no son precios reales y no se usan.
    Devuelve (fotos revisadas, apuestas nuevas)."""
    desde = iso(ahora() - timedelta(minutes=max_minutos))
    capturas = [r[0] for r in con.execute(
        "SELECT DISTINCT capturado FROM momios WHERE capturado >= ? ORDER BY capturado DESC", (desde,))]
    nuevas = 0
    for capturado in capturas:  # de la más reciente a la más vieja: si se repite un partido, gana el precio más nuevo
        colocadas, _ = colocar_apuestas(con, config, capturado)
        nuevas += sum(colocadas.values())
    if capturas:
        anotar(con, "sistema", f"Análisis sin gastar créditos: se revisaron {len(capturas)} descargas de momios de los "
                               f"últimos {max_minutos} min con las reglas actuales; {nuevas} apuestas nuevas.")
    else:
        anotar(con, "sistema", f"Análisis sin gastar créditos: no hay momios de los últimos {max_minutos} min "
                               f"(los más viejos ya no son precios reales); hace falta una búsqueda nueva.")
    con.commit()
    return len(capturas), nuevas


def anular_mercados_distintos(con, referencia: str) -> int:
    """Corrección única (2 de octubre): anula las apuestas abiertas que se tomaron comparando un mercado
    con opciones distintas a las de la referencia, como el hockey europeo a 3 vías contra el de 2 vías."""
    def opciones(evento_id, casa, capturado):
        return {r[0] for r in con.execute("""SELECT seleccion FROM momios WHERE evento_id = ? AND casa = ?
                                             AND capturado = ? AND mercado = 'h2h'""", (evento_id, casa, capturado))}
    anuladas = 0
    for a in con.execute("SELECT id, evento_id, casa, colocada FROM apuestas WHERE estado = 'abierta'").fetchall():
        ref = opciones(a["evento_id"], referencia, a["colocada"])
        if a["casa"] == "promedio":
            casas = [r[0] for r in con.execute("SELECT DISTINCT casa FROM momios WHERE evento_id = ? AND capturado = ?",
                                               (a["evento_id"], a["colocada"]))]
            distinto = any(opciones(a["evento_id"], c, a["colocada"]) != ref for c in casas)
        else:
            distinto = opciones(a["evento_id"], a["casa"], a["colocada"]) != ref
        if ref and distinto:
            con.execute("""UPDATE apuestas SET estado = 'anulada', ganancia = 0, liquidada = ?,
                           nota = 'Anulada: mercado de 3 vías comparado contra 2 vías' WHERE id = ?""",
                        (iso(ahora()), a["id"]))
            anuladas += 1
    if anuladas:
        anotar(con, "sistema", f"Corrección: se anularon {anuladas} apuestas con valor falso. Comparaban el hockey "
                               "europeo a 3 vías (solo tiempo regular, con empate) contra el de 2 vías de Pinnacle "
                               "(con tiempo extra). Ahora solo se comparan mercados con las mismas opciones.")
    con.commit()
    return anuladas


def _resultado(seleccion: str, local: str, visitante: str, goles_local: int, goles_visitante: int,
               hay_empate: bool) -> str:
    if goles_local == goles_visitante:
        if seleccion == "Draw":
            return "ganada"
        return "perdida" if hay_empate else "anulada"  # sin empate en el mercado: se devuelve el dinero
    ganador = local if goles_local > goles_visitante else visitante
    return "ganada" if seleccion == ganador else "perdida"


def liquidar(con) -> int:
    """Cierra las apuestas de partidos terminados. Devuelve cuántas se liquidaron."""
    momento = iso(ahora())
    filas = con.execute(
        """SELECT a.id, a.seleccion, a.momio, a.monto, e.local, e.visitante, e.marcador_local,
                  e.marcador_visitante, e.id AS evento_id
           FROM apuestas a JOIN eventos e ON e.id = a.evento_id
           WHERE a.estado = 'abierta' AND e.terminado = 1""").fetchall()
    for f in filas:
        hay_empate = con.execute("SELECT 1 FROM momios WHERE evento_id = ? AND seleccion = 'Draw' LIMIT 1",
                                 (f["evento_id"],)).fetchone() is not None
        estado = _resultado(f["seleccion"], f["local"], f["visitante"], f["marcador_local"],
                            f["marcador_visitante"], hay_empate)
        ganancia = {"ganada": f["monto"] * (f["momio"] - 1), "perdida": -f["monto"], "anulada": 0.0}[estado]
        con.execute("UPDATE apuestas SET estado = ?, ganancia = ?, liquidada = ? WHERE id = ?",
                    (estado, round(ganancia, 2), momento, f["id"]))
    # Sin resultado después de 4 días (la API solo guarda 3): se anula y se devuelve el dinero
    limite = iso(ahora() - timedelta(days=4))
    cur = con.execute("""UPDATE apuestas SET estado = 'anulada', ganancia = 0, liquidada = ?,
                                nota = 'Sin resultado disponible'
                         WHERE estado = 'abierta' AND inicio < ?""", (momento, limite))
    if cur.rowcount:
        anotar(con, "sistema", f"Se anularon {cur.rowcount} apuestas sin resultado disponible después de 4 días.")
    con.commit()
    return len(filas)


def calcular_clv(con, config: dict) -> int:
    """Para partidos que ya empezaron, compara el momio apostado contra la última foto de Pinnacle
    antes del inicio. Si no hubo foto posterior a la apuesta, el CLV queda vacío."""
    referencia = config["casa_referencia"]
    filas = con.execute("""SELECT id, evento_id, seleccion, momio, colocada, inicio FROM apuestas
                           WHERE cierre_revisado = 0 AND inicio <= ?""", (iso(ahora()),)).fetchall()
    for f in filas:
        cierre = con.execute(
            """SELECT MAX(capturado) FROM momios
               WHERE evento_id = ? AND casa = ? AND mercado = 'h2h' AND capturado < ?""",
            (f["evento_id"], referencia, f["inicio"])).fetchone()[0]
        momio_cierre = prob_cierre = clv = None
        if cierre:
            momios = {r[0]: r[1] for r in con.execute(
                """SELECT seleccion, momio FROM momios
                   WHERE evento_id = ? AND casa = ? AND mercado = 'h2h' AND capturado = ?""",
                (f["evento_id"], referencia, cierre))}
            if f["seleccion"] in momios:
                justas = dict(zip(momios, probabilidades_justas(list(momios.values()))))
                momio_cierre, prob_cierre = momios[f["seleccion"]], justas[f["seleccion"]]
                if cierre > f["colocada"]:
                    clv = f["momio"] * prob_cierre - 1
        con.execute("""UPDATE apuestas SET momio_cierre_ref = ?, prob_cierre = ?, clv = ?, cierre_revisado = 1
                       WHERE id = ?""", (momio_cierre, prob_cierre, clv, f["id"]))
    con.commit()
    return len(filas)
