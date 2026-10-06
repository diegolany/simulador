"""Estrategias de apuesta, liquidación con resultados reales y medición del CLV.

Cada estrategia tiene su propia banca ficticia. La "Principal" es la cartera de
$100,000 que se muestra en el tablero; las "retadoras" son experimentos que compiten
contra ella; el "Apostador casual" es el grupo de control que muestra por qué
pierde la mayoría.

Realismo al apostar: se cuenta el momio publicado menos un deslizamiento (el precio se mueve antes de que entre
la apuesta, y más mientras más tiempo pasó desde que se vio), cada casa tiene un tope por apuesta, las casas que
ya te habrían limitado solo aceptan montos chicos y nunca hay más de cierta parte de la banca en juego.
"""
import json
from datetime import timedelta
from statistics import median

import cerebro
import marcadores
import riesgo
from base_datos import a_fecha, ahora, anotar, iso, leer_estado
from momios import fraccion_kelly, margen_casa, probabilidades_justas, valor_esperado

PARAMETROS_BASE = {
    "umbral": 0.02,         # valor mínimo sobre la probabilidad justa
    "momio_min": 1.30,
    "momio_max": 5.0,
    "horas_min": 0.17,      # no apostar a menos de 10 minutos del inicio
    "horas_max": 48,
    "kelly": 0.5,           # fracción de Kelly (½: el cerebro ya hace conservadora la ventaja estimada)
    "tope": 0.02,           # máximo 2% de la banca por apuesta
    "ligas_bloqueadas": [],
    "casas_bloqueadas": [],
    "umbral_margen": 0.0,   # valor extra exigido por cada punto de margen de Pinnacle arriba de 2.5%
    "referencia": "pinnacle",  # pinnacle, o consenso (mitad Pinnacle, mitad casas de intercambio)
    "cerebro": True,        # filtro y monto según la ventaja real que estima el cerebro
}
MARGEN_EFICIENTE = 0.025
INTERCAMBIOS = ("betfair_ex_eu", "matchbook")

# Estrategia México (desde el 6 de octubre de 2026): todas apuestan solo en casas permitidas en México
# (config casas_mexico; motor.inicializar les pone la lista). Lo de casas europeas quedó archivado.
ESTRATEGIAS_INICIALES = [
    ("Principal", "valor", "principal",
     "Valor de 1.5% o más contra Pinnacle en casas permitidas en México, momios 1.30 a 5.00, hasta 48 h antes",
     {"umbral": 0.015}),
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
    ("Valor según eficiencia", "valor", "retadora",
     "Exige más valor donde Pinnacle cobra más margen (mercado menos eficiente): 1.5% más 1 punto por cada punto de "
     "margen arriba de 2.5%. En pruebas históricas rindió +6.2% (t = 3.1)",
     {"umbral": 0.015, "umbral_margen": 1.0}),
    ("Sin cerebro", "valor", "retadora",
     "Reglas de la Principal con el monto según el valor a simple vista, sin el cerebro: mide si el cerebro ayuda",
     {"umbral": 0.015, "cerebro": False}),
    ("Solo mejorados", "valor", "retadora",
     "Solo los momios mejorados de Caliente y Codere con valor de 1.5% o más (monto máximo $500)",
     {"umbral": 0.015, "solo_mejorados": True}),
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
        anotar(con, "nueva", f"Nuevas estrategias en el laboratorio: {', '.join(nuevas)}.")


def activas(con) -> list[dict]:
    filas = con.execute("SELECT nombre, tipo, rol, descripcion, parametros FROM estrategias WHERE rol != 'retirada'").fetchall()
    return [{"nombre": f["nombre"], "tipo": f["tipo"], "rol": f["rol"], "descripcion": f["descripcion"],
             "p": {**PARAMETROS_BASE, **json.loads(f["parametros"])}} for f in filas]


def ajustar_umbral(con, nombres: tuple, umbral: float) -> None:
    """Cambia el valor mínimo de esas estrategias (y su descripción si lo menciona)."""
    for nombre in nombres:
        fila = con.execute("SELECT parametros, descripcion FROM estrategias WHERE nombre = ?", (nombre,)).fetchone()
        if not fila:
            continue
        parametros = {**json.loads(fila["parametros"]), "umbral": umbral}
        descripcion = (fila["descripcion"] or "").replace("Valor de 2% o más", f"Valor de {umbral:.1%} o más".replace(".0%", "%"))
        con.execute("UPDATE estrategias SET parametros = ?, descripcion = ? WHERE nombre = ?",
                    (json.dumps(parametros), descripcion, nombre))


def banca(con, estrategia: str, inicial: float) -> tuple[float, float]:
    """(banca realizada, dinero en juego en apuestas abiertas)."""
    ganado, en_juego = con.execute(
        """SELECT COALESCE(SUM(CASE WHEN estado != 'abierta' THEN ganancia END), 0),
                  COALESCE(SUM(CASE WHEN estado = 'abierta' THEN monto END), 0)
           FROM apuestas WHERE estrategia = ?""", (estrategia,)).fetchone()
    return inicial + ganado, en_juego


def _foto(con, capturado: str, evento_id: str | None = None) -> dict:
    """{evento: {casa: {"momios": {seleccion: momio}, "actualizado": iso}}} de una captura (o de un partido de ella)."""
    sql = """SELECT evento_id, casa, seleccion, momio, actualizado_casa FROM momios
             WHERE capturado = ? AND mercado = 'h2h'"""
    parametros = [capturado]
    if evento_id:
        sql += " AND evento_id = ?"
        parametros.append(evento_id)
    foto = {}
    for f in con.execute(sql, parametros):
        casa = foto.setdefault(f["evento_id"], {}).setdefault(f["casa"], {"momios": {}, "actualizado": None})
        casa["momios"][f["seleccion"]] = f["momio"]
        casa["actualizado"] = f["actualizado_casa"]
    return foto


def deslizamiento(config: dict, minutos: float) -> float:
    """Parte del precio que se pierde al apostar: el momio se mueve antes de que entre la apuesta, y se mueve más
    mientras más tiempo pasó desde que se vio."""
    e = config["ejecucion"]
    return min(e["deslizamiento_max"], e["deslizamiento_base"] + e["deslizamiento_por_minuto"] * max(0.0, minutos))


def precio_ejecutado(momio: float, desliz: float) -> float:
    """Momio que realmente se habría conseguido."""
    return max(1.01, round(momio / (1 + desliz), 3))


def _justas_de(momios: dict, selecciones: list) -> dict:
    return dict(zip(selecciones, probabilidades_justas([momios[s] for s in selecciones])))


def _consenso(casas: dict, selecciones: list, justas: dict, momento, max_antiguedad) -> dict:
    """Precio justo de consenso: mitad Pinnacle y mitad casas de intercambio (frescas y con liquidez, margen < 8%).
    Si no hay intercambios útiles, se queda el de Pinnacle."""
    otras = []
    for casa in INTERCAMBIOS:
        d = casas.get(casa)
        if (not d or set(d["momios"]) != set(selecciones) or not d["actualizado"]
                or momento - a_fecha(d["actualizado"]) > max_antiguedad
                or margen_casa(list(d["momios"].values())) > 0.08):
            continue
        otras.append(_justas_de(d["momios"], selecciones))
    if not otras:
        return justas
    mezcla = {s: 0.5 * justas[s] + 0.5 * sum(o[s] for o in otras) / len(otras) for s in selecciones}
    total = sum(mezcla.values())
    return {s: v / total for s, v in mezcla.items()}


class _Contexto:
    """Lo que se consulta una vez por pasada: el cerebro, las cuentas limitadas y la caída de cada banca."""

    def __init__(self, con, config: dict):
        self.con, self.config = con, config
        self.mente = cerebro.obtener(con, config)
        self.limitadas = riesgo.cuentas_limitadas(con)
        self.descartadas = 0
        self._caidas = {}
        # Agresividad que eligió el modo objetivo para la cartera Principal (1 = normal)
        modo = leer_estado(con, "modo_objetivo") or {}
        self.multiplicador = modo.get("multiplicador", 1.0) if config["objetivo"]["activo"] else 1.0

    def caida(self, estrategia: str) -> float:
        if estrategia not in self._caidas:
            filas = [dict(r) for r in self.con.execute(
                "SELECT estado, ganancia, liquidada FROM apuestas WHERE estrategia = ? AND estado IN ('ganada', 'perdida')",
                (estrategia,))]
            self._caidas[estrategia] = riesgo.caidas(filas, self.config["banca_inicial"])["actual_pct"]
        return self._caidas[estrategia]


def _monto(ctx: _Contexto, est: dict, fraccion: float, casa: str, deporte: str) -> tuple[int, float, list[str], float]:
    """Monto en pesos con los controles de riesgo y de realismo. Devuelve (monto, banca, notas, multiplicador);
    monto 0 = no apostar."""
    config = ctx.config
    r, e = config["riesgo"], config["ejecucion"]
    actual, en_juego = banca(ctx.con, est["nombre"], config["banca_inicial"])
    notas = []
    if est["tipo"] == "favorito":  # el apostador casual no controla su riesgo: solo apuesta lo que tiene libre
        monto = int(fraccion * actual / 10) * 10
        return (monto if config["apuesta_minima"] <= monto <= actual - en_juego else 0), actual, notas, 1.0
    multiplicador = ctx.multiplicador if est["rol"] == "principal" else 1.0
    if multiplicador != 1.0:
        fraccion *= multiplicador
        notas.append(f"modo objetivo: montos ×{multiplicador:g} (lo que más acerca a la meta sin pasar el límite de riesgo)")
    if est["tipo"] == "valor" and ctx.caida(est["nombre"]) >= r["freno_caida"]:
        fraccion *= r["factor_freno"]
        notas.append(f"freno por caída de {ctx.caida(est['nombre']):.0%} de la banca: monto al {r['factor_freno']:.0%}")
    monto = fraccion * actual
    limite = e["limite_apuesta"] if deporte in e["ligas_mayores"] else e["limite_apuesta_liga_menor"]
    if casa.endswith("_mejorado"):  # los momios mejorados tienen monto máximo bajo
        limite = min(limite, e["limite_momio_mejorado"])
        notas.append(f"momio mejorado: la casa acepta máximo ${limite:,.0f}")
    elif casa in ctx.limitadas:
        limite = min(limite, e["limite_cuenta_limitada"])
        notas.append(f"{casa} ya te habría limitado: acepta máximo ${limite:,.0f}")
    elif monto > limite:
        notas.append(f"tope de la casa para esta liga: ${limite:,.0f}")
    monto = min(monto, limite)
    espacio = r["max_exposicion"] * actual - en_juego
    if monto > espacio:
        monto = espacio
        notas.append(f"tope de exposición: máximo {r['max_exposicion']:.0%} de la banca en juego")
    monto = int(monto / 10) * 10
    return (monto if monto >= config["apuesta_minima"] else 0), actual, notas, multiplicador


def _registrar(con, est: dict, ev, sel: str, casa: str, momio: float, visto: float, momio_ref: float, prob: float,
               monto: int, colocada: str, razon: str, margen: float, ventaja: float | None, minutos: float,
               multiplicador: float = 1.0, analisis: str | None = None) -> None:
    con.execute(
        """INSERT INTO apuestas (estrategia, evento_id, deporte, liga, mercado, seleccion, casa, momio, momio_ref,
                                 prob_justa, valor, monto, colocada, inicio, razon, momio_visto, margen_ref,
                                 ventaja_estimada, minutos_precio, multiplicador, analisis, kelly)
           VALUES (?, ?, ?, ?, 'h2h', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (est["nombre"], ev["id"], ev["deporte"], ev["liga"], sel, casa, momio, momio_ref, prob,
         valor_esperado(prob, momio), monto, colocada, ev["inicio"], razon, visto, round(margen, 4),
         round(ventaja, 4) if ventaja is not None else None, minutos, multiplicador, analisis,
         None if est["p"].get("fijo") else est["p"]["kelly"]))


def _analisis(p: dict, probs: dict, referencia: str, margen_ref: float, sel: str, visto: float, momio: float,
              desliz: float, ventaja: float | None, ajustes, factor: float, fraccion: float, monto: int, actual: float,
              notas: list, multiplicador: float, horas: float, precios_casas: list, margen_elegida: float) -> str:
    """Todo lo que el bot consideró al apostar, guardado tal cual para revisarlo después en el diario."""
    valor = valor_esperado(probs[sel], momio)
    estimada = ventaja if ventaja is not None else valor
    return json.dumps({
        "referencia": referencia, "probabilidades": {s: round(v, 4) for s, v in probs.items()},
        "margen_ref": round(margen_ref, 4), "margen_casa": round(margen_elegida, 4),
        "precios": sorted(([c, round(m, 3)] for c, m in precios_casas), key=lambda x: -x[1])[:12],
        "visto": round(visto, 3), "momio": momio, "deslizamiento": round(desliz, 4), "valor": round(valor, 4),
        "cerebro": None if ventaja is None else {"factor": round(factor, 3), "ventaja": round(ventaja, 4),
                                                "ajustes": [[d, n, round(e, 4)] for d, n, e in ajustes]},
        "kelly": {"completo": round(max(0.0, estimada / (momio - 1)), 4), "fraccion": p["kelly"], "tope": p["tope"],
                  "fijo": p.get("fijo"), "elegida": round(fraccion, 4), "multiplicador": multiplicador},
        "notas": notas, "monto": monto, "banca": round(actual, 2), "horas": round(horas, 1),
        "reglas": {"umbral": p["umbral"], "umbral_margen": p.get("umbral_margen", 0), "momio_min": p["momio_min"],
                   "momio_max": p["momio_max"], "horas_min": p["horas_min"], "horas_max": p["horas_max"]},
    }, ensure_ascii=False)


def _anotar_senales(con, config: dict, ev, precios: list, justas: dict, margen: float, capturado: str) -> None:
    """Apuestas fantasma: guarda cada precio (casa, selección, momio ya con deslizamiento) con valor de al menos −2%
    para medir después su CLV. El mismo precio visto otra vez no se repite."""
    f = config["fantasmas"]
    mexicanas = {c["clave"] for c in config["casas_mexico"]}
    for casa, sel, momio in precios:
        if sel not in justas or casa not in mexicanas:
            continue  # solo se mide lo que se puede apostar desde México
        v = valor_esperado(justas[sel], momio)
        # Se guarda casi todo (cobran más comisión): así se mide qué tan seguido alguna le gana al mercado
        if -0.10 <= v <= config["valor_sospechoso"] and momio <= f["momio_maximo"]:
            con.execute("""INSERT OR IGNORE INTO senales (evento_id, deporte, liga, casa, seleccion, momio, prob_justa,
                                                          valor, margen_ref, capturado, inicio)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (ev["id"], ev["deporte"], ev["liga"], casa, sel, round(momio, 3), justas[sel], v,
                         round(margen, 4), capturado, ev["inicio"]))


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
    # Minutos desde que se vio el precio: al reanalizar fotos viejas el precio pudo moverse más
    minutos = max(0.0, (ahora() - momento).total_seconds() / 60)
    desliz = deslizamiento(config, minutos)
    estrategias = activas(con)
    ctx = _Contexto(con, config)
    ya_apostadas = {(f[0], f[1]) for f in con.execute(
        f"SELECT estrategia, evento_id FROM apuestas WHERE evento_id IN ({','.join('?' * len(foto))})",
        list(foto))}
    eventos = {f["id"]: f for f in con.execute(
        f"SELECT id, deporte, liga, local, visitante, inicio FROM eventos WHERE id IN ({','.join('?' * len(foto))})",
        list(foto))}
    colocadas, senales = {}, 0

    for evento_id, casas in foto.items():
        ev = eventos[evento_id]
        horas = (a_fecha(ev["inicio"]) - momento).total_seconds() / 3600
        ref = casas.get(referencia)
        empieza_ya = a_fecha(ev["inicio"]) - ahora() < timedelta(minutes=10)
        if horas <= 0 or empieza_ya or not ref or len(ref["momios"]) < 2:
            continue
        selecciones = list(ref["momios"])
        justas = _justas_de(ref["momios"], selecciones)
        margen = margen_casa(list(ref["momios"].values()))
        if ev["local"] in justas and ev["visitante"] in justas:  # pronóstico para medir la calibración
            con.execute("""INSERT INTO pronosticos (evento_id, deporte, local, visitante, inicio, prob_local, prob_empate,
                                                    prob_visitante, capturado) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(evento_id) DO UPDATE SET prob_local = excluded.prob_local,
                               prob_empate = excluded.prob_empate, prob_visitante = excluded.prob_visitante,
                               capturado = excluded.capturado, inicio = excluded.inicio
                           WHERE pronosticos.resultado IS NULL""",
                        (evento_id, ev["deporte"], ev["local"], ev["visitante"], ev["inicio"], justas[ev["local"]],
                         justas.get("Draw"), justas[ev["visitante"]], capturado))
        ofertas = []
        for casa, datos in casas.items():
            if casa in excluidas or not datos["actualizado"]:
                continue
            if set(datos["momios"]) != set(selecciones):
                continue  # otro mercado: ej. hockey europeo a 3 vías (tiempo regular) contra 2 vías de Pinnacle
            if momento - a_fecha(datos["actualizado"]) > max_antiguedad:
                continue  # momio viejo: probablemente ya no está disponible
            for sel, visto in datos["momios"].items():
                if sel in justas:
                    ofertas.append({"casa": casa, "sel": sel, "visto": visto, "momio": precio_ejecutado(visto, desliz)})
        if any(0.02 <= valor_esperado(justas[o["sel"]], o["momio"]) <= config["valor_sospechoso"] and o["momio"] <= 10
               for o in ofertas):
            senales += 1
        if minutos <= 5:  # solo precios recién vistos (los del reanálisis de fotos viejas ya se anotaron)
            _anotar_senales(con, config, ev, [(o["casa"], o["sel"], o["momio"]) for o in ofertas], justas, margen,
                            capturado)

        consenso = None
        for est in estrategias:
            p = est["p"]
            if est["tipo"] not in ("valor", "favorito"):
                continue  # los experimentos DraftKings apuestan aparte, con momios gratuitos
            if (est["nombre"], evento_id) in ya_apostadas or not p["horas_min"] <= horas <= p["horas_max"]:
                continue
            if ev["liga"] in p["ligas_bloqueadas"]:
                continue
            ventaja, ajustes = None, []
            if est["tipo"] == "valor":
                probs, nombre_ref = justas, "Pinnacle"
                if p["referencia"] == "consenso":
                    if consenso is None:
                        consenso = _consenso(casas, selecciones, justas, momento, max_antiguedad)
                    probs, nombre_ref = consenso, "El consenso (Pinnacle + intercambios)"
                umbral = p["umbral"] + p["umbral_margen"] * max(0.0, margen - MARGEN_EFICIENTE)
                candidatas = []
                for o in ofertas:
                    v = valor_esperado(probs[o["sel"]], o["momio"])
                    if (o["casa"] in p["casas_bloqueadas"] or not p["momio_min"] <= o["momio"] <= p["momio_max"]
                            or not umbral <= v <= config["valor_sospechoso"]
                            or (p.get("casas_permitidas") and o["casa"] not in p["casas_permitidas"])):
                        continue
                    estimada, aj = v, []
                    if p["cerebro"]:
                        estimada, aj = ctx.mente.estimar(v, cerebro.rasgos(ev["liga"], o["casa"], o["momio"], horas, margen, o["sel"]))
                        if estimada < config["cerebro"]["ventaja_minima"]:
                            ctx.descartadas += 1
                            continue  # el cerebro no ve ventaja real que valga la pena en este tipo de apuesta
                    candidatas.append((estimada, o, aj))
                if not candidatas:
                    continue
                estimada, elegida, ajustes = max(candidatas, key=lambda c: c[0])
                casa, sel, momio, visto = elegida["casa"], elegida["sel"], elegida["momio"], elegida["visto"]
                prob = probs[sel]
                ventaja = estimada if p["cerebro"] else None
                fraccion = fraccion_kelly(min(0.99, (1 + estimada) / momio), momio, p["kelly"], p["tope"])
            else:  # favorito: lo que haría un apostador casual
                sel = max((s for s in selecciones if s != "Draw"), key=lambda s: justas[s])
                precios = [o["visto"] for o in ofertas if o["sel"] == sel]
                if len(precios) < 2:
                    continue
                casa, momio, fraccion = "promedio", round(median(precios), 2), p["fijo"]
                visto, prob = momio, justas[sel]

            monto, actual, notas, multiplicador = _monto(ctx, est, fraccion, casa, ev["deporte"])
            if not monto:
                continue
            analisis = None
            if est["tipo"] == "valor":
                razon = _razon_valor(casa, sel, visto, momio, prob, horas, monto, actual, ventaja, ajustes, notas,
                                     referencia=nombre_ref, kelly=p["kelly"])
                analisis = _analisis(p, probs, nombre_ref, margen, sel, visto, momio, desliz, ventaja, ajustes,
                                     ctx.mente.factor, fraccion, monto, actual, notas, multiplicador, horas,
                                     [(o["casa"], o["visto"]) for o in ofertas if o["sel"] == sel]
                                     + [(f"{referencia} (referencia)", ref["momios"][sel])],
                                     margen_casa(list(casas[casa]["momios"].values())))
            else:
                razon = (f"Control: apuesto al favorito ({prob:.0%}) al momio promedio de {len(precios)} casas "
                         f"({momio:.2f}), sin buscar valor, como lo haría un apostador casual.")
            _registrar(con, est, ev, sel, casa, momio, visto, ref["momios"][sel], prob, monto, capturado, razon,
                       margen, ventaja, round(minutos, 1), multiplicador, analisis)
            ya_apostadas.add((est["nombre"], evento_id))
            colocadas[est["nombre"]] = colocadas.get(est["nombre"], 0) + 1
    con.commit()
    return colocadas, senales


def _nombre(seleccion: str) -> str:
    return "el empate" if seleccion == "Draw" else seleccion


def _razon_valor(casa: str, sel: str, visto: float, momio: float, prob: float, horas: float, monto: int,
                 actual: float, ventaja: float | None = None, ajustes=(), notas=(), extra: str = "",
                 fijo: bool = False, referencia: str = "Pinnacle", kelly: float = 0.5) -> str:
    texto = f"{casa} paga {visto:.2f} por {_nombre(sel)}"
    if momio < visto:
        texto += f" (cuento {momio:.2f} por el movimiento del precio al apostar)"
    texto += (f". {referencia}, sin comisión, le da {prob:.0%} (precio justo {1 / prob:.2f}): "
              f"valor {valor_esperado(prob, momio):+.1%}.")
    if ventaja is not None:
        explicacion = cerebro.explicar(ajustes)
        texto += f" El cerebro estima la ventaja real en {ventaja:+.1%}" + (f" ({explicacion})." if explicacion else ".")
    fraccion = {0.25: "¼", 0.5: "½", 1.0: "Kelly completo"}.get(kelly, f"{kelly:g}")
    modo = ("monto fijo de experimento" if fijo else f"{fraccion} de Kelly sobre la ventaja estimada" if ventaja is not None
            else f"{fraccion} de Kelly")
    texto += f" Faltan {horas:.0f} h. Apuesto ${monto:,.0f} ({monto / actual:.2%} de la banca, {modo})."
    if notas:
        texto += " Control de riesgo: " + "; ".join(notas) + "."
    return texto + extra


def _califica(p: dict, momio: float, visto: float, valor: float, umbral: float, apertura: float | None,
              cambio_pinnacle: float | None, tope: float) -> bool:
    """¿La selección cumple las reglas de la estrategia? Los experimentos DraftKings añaden una condición:
    'movimiento' = su momio bajó desde la apertura (entró dinero); 'pinnacle_mueve' = Pinnacle subió su probabilidad."""
    if not p["momio_min"] <= momio <= p["momio_max"] or not umbral <= valor <= tope:
        return False
    modo = p.get("modo", "valor")
    if modo == "movimiento":
        return bool(apertura) and (apertura - visto) / apertura >= p["movimiento_min"]
    if modo == "pinnacle_mueve":
        return cambio_pinnacle is not None and cambio_pinnacle >= p["movimiento_min"]
    return True


def apostar_gratis(con, config: dict) -> dict:
    """Momios gratuitos: el de DraftKings que ESPN publica sin costo, comparado contra el último precio justo de
    Pinnacle ya descargado si tiene menos de `max_horas_referencia` horas. Cada estrategia de valor (la Principal
    y las retadoras) aplica sus mismas reglas y apuesta con su misma banca; la apuesta queda marcada como
    DraftKings. Devuelve ({estrategia: apuestas nuevas}, resumen)."""
    cache = {}

    def precio(ev):
        if not marcadores.cubierto_momios(ev["deporte"]):
            return None
        return marcadores.precios(ev["deporte"], ev["local"], ev["visitante"], a_fecha(ev["inicio"]), cache,
                                  con_apertura=True)
    # Un momio de DraftKings más de 4% arriba del precio justo delata una foto vieja de Pinnacle
    return _apostar_con_precios(con, config, precio, 0, "Momio gratuito (ESPN)", config["valor_sospechoso_dk"], True)


def apostar_externo(con, config: dict, mapa: dict, capturado: str, nota: str) -> dict:
    """Momios de una casa sin servicio de datos (Caliente) que se leyeron a mano en el navegador.
    `mapa` = {evento_id: {"casa": ..., "precios": {selección: momio decimal}}}. El precio se cuenta con el
    deslizamiento de los minutos que pasaron desde que se leyó. Solo apuestan las estrategias de valor."""
    minutos = max(0.0, (ahora() - a_fecha(capturado)).total_seconds() / 60)

    def precio(ev):
        p = mapa.get(ev["id"])
        return (p["casa"], p["precios"], {}) if p else None
    # Para medir, cualquier foto de Pinnacle del día sirve (lo que califica al precio es el CLV contra el cierre);
    # para apostar se sigue pidiendo una foto de menos de una hora
    return _apostar_con_precios(con, config, precio, minutos, nota, config["valor_sospechoso"], False, set(mapa),
                                config["mexico"]["horas_referencia_medicion"])


def _apostar_con_precios(con, config: dict, obtener_precio, minutos: float, nota: str, tope_valor: float,
                         con_experimentos: bool, eventos: set | None = None, horas_referencia: float | None = None) -> dict:
    """Compara el momio de una casa externa (obtener_precio(evento) -> (casa, precios, apertura)) contra el último
    precio justo de Pinnacle ya descargado y deja que cada estrategia apueste con sus reglas."""
    tipos = ("valor", "gratis") if con_experimentos else ("valor",)  # gratis = experimentos DraftKings
    estrategias = [e for e in activas(con) if e["tipo"] in tipos]
    resumen = {"comparados": 0, "mejor": None}  # lo que vio, para explicarlo aunque no apueste
    if not estrategias:
        return {}, resumen
    momento = ahora()
    referencia = config["casa_referencia"]
    max_antiguedad = timedelta(minutes=config["max_minutos_momio"])
    desliz = deslizamiento(config, minutos)
    filas = con.execute(
        """SELECT e.id, e.deporte, e.liga, e.local, e.visitante, e.inicio, MAX(m.capturado) AS cap
           FROM eventos e JOIN momios m ON m.evento_id = e.id AND m.casa = ? AND m.mercado = 'h2h'
           WHERE e.inicio > ? AND e.inicio <= ? AND m.capturado >= ?
           GROUP BY e.id""",
        (referencia, iso(momento + timedelta(minutes=10)),
         iso(momento + timedelta(hours=max(e["p"]["horas_max"] for e in estrategias))),
         iso(momento - timedelta(hours=horas_referencia or config["max_horas_referencia"])))).fetchall()
    ya_apostadas = {(f[0], f[1]) for f in con.execute("SELECT estrategia, evento_id FROM apuestas WHERE inicio > ?",
                                                      (iso(momento),))}
    colocadas, ctx = {}, None
    for ev in filas:
        if eventos is not None and ev["id"] not in eventos:
            continue
        pendientes = [e for e in estrategias if (e["nombre"], ev["id"]) not in ya_apostadas]
        if not pendientes:
            continue
        ref = {r[0]: r[1] for r in con.execute(
            "SELECT seleccion, momio FROM momios WHERE evento_id = ? AND casa = ? AND mercado = 'h2h' AND capturado = ?",
            (ev["id"], referencia, ev["cap"]))}
        inicio = a_fecha(ev["inicio"])
        encontrado = obtener_precio(ev)
        if len(ref) < 2 or not encontrado or set(encontrado[1]) != set(ref):
            continue  # sin precio gratuito o con otras opciones que la referencia
        casa, precios, apertura = encontrado
        selecciones = list(ref)
        justas = _justas_de(ref, selecciones)
        margen = margen_casa(list(ref.values()))
        ejecutados = {s: precio_ejecutado(precios[s], desliz) for s in selecciones}
        _anotar_senales(con, config, ev, [(casa, s, ejecutados[s]) for s in selecciones], justas, margen, iso(momento))
        # Foto anterior de Pinnacle: para ver hacia dónde se movió el dinero profesional
        anterior = con.execute("""SELECT MAX(capturado) FROM momios WHERE evento_id = ? AND casa = ? AND mercado = 'h2h'
                                  AND capturado < ?""", (ev["id"], referencia, ev["cap"])).fetchone()[0]
        previas = {}
        if anterior:
            viejos = {r[0]: r[1] for r in con.execute("""SELECT seleccion, momio FROM momios WHERE evento_id = ?
                                                         AND casa = ? AND mercado = 'h2h' AND capturado = ?""",
                                                      (ev["id"], referencia, anterior))}
            if set(viejos) == set(selecciones):
                previas = dict(zip(viejos, probabilidades_justas(list(viejos.values()))))
        horas = (inicio - momento).total_seconds() / 3600
        edad = (momento - a_fecha(ev["cap"])).total_seconds() / 3600
        resumen["comparados"] += 1
        for s in selecciones:
            v = valor_esperado(justas[s], ejecutados[s])
            if 1.30 <= precios[s] <= 5 and (resumen["mejor"] is None or v > resumen["mejor"]["valor"]):
                resumen["mejor"] = {"valor": v, "partido": f"{ev['local']} vs {ev['visitante']}",
                                    "seleccion": _nombre(s), "momio": round(precios[s], 2), "justo": round(1 / justas[s], 2)}
        if ctx is None:
            ctx = _Contexto(con, config)
        consenso, registrado = None, False
        for est in pendientes:
            p = est["p"]
            if ev["liga"] in p["ligas_bloqueadas"] or casa in p["casas_bloqueadas"] or not p["horas_min"] <= horas <= p["horas_max"]:
                continue
            if p.get("casas_permitidas") and casa not in p["casas_permitidas"]:
                continue  # la cartera México solo apuesta en casas mexicanas
            probs, nombre_ref = justas, "Pinnacle"
            if est["tipo"] == "valor" and p["referencia"] == "consenso":
                if consenso is None:  # intercambios de la misma descarga de Pinnacle
                    casas_cap = _foto(con, ev["cap"], ev["id"]).get(ev["id"], {})
                    consenso = _consenso(casas_cap, selecciones, justas, a_fecha(ev["cap"]), max_antiguedad)
                probs, nombre_ref = consenso, "El consenso (Pinnacle + intercambios)"
            usa_cerebro = est["tipo"] == "valor" and p["cerebro"]
            # Las estrategias de valor no apuestan contra una foto vieja de Pinnacle: si el mercado se movió después,
            # el "valor" es falso. Y DraftKings casi nunca paga más que el precio justo: más de 4% delata una foto vieja
            if est["tipo"] == "valor" and edad * 60 > config["max_minutos_referencia_valor"]:
                continue
            tope = tope_valor if est["tipo"] == "valor" else config["valor_sospechoso"]
            umbral = p["umbral"] + p["umbral_margen"] * max(0.0, margen - MARGEN_EFICIENTE)
            candidatas = []
            for s in selecciones:
                v = valor_esperado(probs[s], ejecutados[s])
                if not _califica(p, ejecutados[s], precios[s], v, umbral, apertura.get(s),
                                 justas[s] - previas[s] if s in previas else None, tope):
                    continue
                estimada, aj = v, []
                if usa_cerebro:
                    estimada, aj = ctx.mente.estimar(v, cerebro.rasgos(ev["liga"], casa, ejecutados[s], horas, margen, s))
                    if estimada < config["cerebro"]["ventaja_minima"]:
                        ctx.descartadas += 1
                        continue
                candidatas.append((estimada, s, aj))
            if not candidatas:
                continue
            estimada, sel, ajustes = max(candidatas, key=lambda c: c[0])
            momio = ejecutados[sel]
            # Las estrategias de valor usan una fracción de Kelly; los experimentos, un monto fijo pequeño para medir
            fraccion = p.get("fijo") or fraccion_kelly(min(0.99, (1 + estimada) / momio), momio, p["kelly"], p["tope"])
            monto, actual, notas, multiplicador = _monto(ctx, est, fraccion, casa, ev["deporte"])
            if not monto:
                continue
            colocada = iso(momento)
            if not registrado:  # el momio tomado queda guardado para poder revisarlo después
                for s, m in precios.items():
                    con.execute("""INSERT INTO momios (evento_id, casa, mercado, seleccion, punto, momio, capturado,
                                                       actualizado_casa) VALUES (?, ?, 'h2h', ?, NULL, ?, ?, ?)""",
                                (ev["id"], casa, s, round(m, 3), colocada, colocada))
                registrado = True
            razon = _razon_valor(casa, sel, precios[sel], momio, probs[sel], horas, monto, actual,
                                 estimada if usa_cerebro else None, ajustes, notas,
                                 f" {nota}; precio justo de hace {edad:.1f} h."
                                 + (f" Experimento: {est['descripcion']}." if est["tipo"] == "gratis" else ""),
                                 fijo=bool(p.get("fijo")), referencia=nombre_ref, kelly=p["kelly"])
            analisis = _analisis(p, probs, nombre_ref, margen, sel, precios[sel], momio, desliz,
                                 estimada if usa_cerebro else None, ajustes, ctx.mente.factor, fraccion, monto, actual,
                                 notas, multiplicador, horas,
                                 [(casa, precios[sel]), (f"{referencia} (referencia)", ref[sel])],
                                 margen_casa(list(precios.values())))
            _registrar(con, est, ev, sel, casa, momio, round(precios[sel], 3), ref[sel], probs[sel], monto, colocada,
                       razon, margen, estimada if usa_cerebro else None, 0.0, multiplicador, analisis)
            ya_apostadas.add((est["nombre"], ev["id"]))
            colocadas[est["nombre"]] = colocadas.get(est["nombre"], 0) + 1
    con.commit()
    return colocadas, resumen


def reanalizar(con, config: dict, max_minutos: int, silencioso: bool = False) -> tuple[int, int]:
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
    if silencioso:
        pass  # en el ciclo automático solo cuentan las apuestas nuevas, que ya quedan registradas
    elif capturas:
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


def liquidar(con, referencia: str = "pinnacle") -> int:
    """Cierra las apuestas de partidos terminados. Devuelve cuántas se liquidaron."""
    momento = iso(ahora())
    filas = con.execute(
        """SELECT a.id, a.seleccion, a.momio, a.monto, a.casa, e.local, e.visitante, e.marcador_local,
                  e.marcador_visitante, e.id AS evento_id
           FROM apuestas a JOIN eventos e ON e.id = a.evento_id
           WHERE a.estado = 'abierta' AND e.terminado = 1""").fetchall()
    for f in filas:
        # ¿El mercado de la apuesta tenía empate? Se mira la casa de la apuesta y la referencia, no cualquier casa:
        # algunas casas europeas listan el hockey a 3 vías aunque la apuesta fue al de 2 vías
        hay_empate = f["seleccion"] == "Draw" or con.execute(
            "SELECT 1 FROM momios WHERE evento_id = ? AND seleccion = 'Draw' AND casa IN (?, ?) LIMIT 1",
            (f["evento_id"], f["casa"], referencia)).fetchone() is not None
        estado = _resultado(f["seleccion"], f["local"], f["visitante"], f["marcador_local"],
                            f["marcador_visitante"], hay_empate)
        ganancia = {"ganada": f["monto"] * (f["momio"] - 1), "perdida": -f["monto"], "anulada": 0.0}[estado]
        con.execute("UPDATE apuestas SET estado = ?, ganancia = ?, liquidada = ? WHERE id = ?",
                    (estado, round(ganancia, 2), momento, f["id"]))
    # Partido pospuesto o suspendido: como en las casas, se devuelve el dinero si no se juega en 48 h;
    # si se canceló o se abandonó, de inmediato. El CLV de esas apuestas no cuenta.
    limite_pospuesto = iso(ahora() - timedelta(hours=48))
    for ev in con.execute("""SELECT DISTINCT e.id, e.local, e.visitante, e.detalle FROM eventos e
                             JOIN apuestas a ON a.evento_id = e.id
                             WHERE a.estado = 'abierta' AND e.terminado = 0 AND e.pospuesto IS NOT NULL
                               AND (e.pospuesto <= ? OR lower(COALESCE(e.detalle, '')) LIKE '%cancel%'
                                    OR lower(COALESCE(e.detalle, '')) LIKE '%abandon%')""",
                          (limite_pospuesto,)).fetchall():
        cancelado = any(p in (ev["detalle"] or "").lower() for p in ("cancel", "abandon"))
        cur = con.execute("""UPDATE apuestas SET estado = 'anulada', ganancia = 0, liquidada = ?, clv = NULL,
                                    clv_fuente = NULL, nota = ? WHERE evento_id = ? AND estado = 'abierta'""",
                          (momento, "Partido cancelado: se devuelve el dinero" if cancelado
                           else "Partido pospuesto y no se jugó en 48 h: se devuelve el dinero", ev["id"]))
        anotar(con, "sistema", f"{ev['local']} vs {ev['visitante']} {'se canceló' if cancelado else 'se pospuso y no se jugó en 48 h'}"
                               f": se anularon {cur.rowcount} apuestas y se devolvió el dinero (regla de las casas).")

    # Sin resultado después de 4 días (la API solo guarda 3): se anula y se devuelve el dinero
    limite = iso(ahora() - timedelta(days=4))
    cur = con.execute("""UPDATE apuestas SET estado = 'anulada', ganancia = 0, liquidada = ?,
                                nota = 'Sin resultado disponible'
                         WHERE estado = 'abierta' AND inicio < ?""", (momento, limite))
    if cur.rowcount:
        anotar(con, "sistema", f"Se anularon {cur.rowcount} apuestas sin resultado disponible después de 4 días.")
    con.commit()
    return len(filas)


def reconstruir_senales(con, config: dict) -> int:
    """Una vez: apuestas fantasma de las fotos de momios que siguen guardadas, para aprender de inmediato con lo
    que el bot ya vio. Devuelve cuántas señales quedaron."""
    referencia = config["casa_referencia"]
    excluidas = set(config["casas_excluidas"]) | {referencia}
    max_antiguedad = timedelta(minutes=config["max_minutos_momio"])
    desliz = deslizamiento(config, 0)
    capturas = [r[0] for r in con.execute(
        "SELECT DISTINCT capturado FROM momios WHERE casa = ? ORDER BY capturado", (referencia,))]
    for capturado in capturas:
        foto = _foto(con, capturado)
        momento = a_fecha(capturado)
        eventos = {f["id"]: f for f in con.execute(
            f"SELECT id, deporte, liga, inicio FROM eventos WHERE id IN ({','.join('?' * len(foto))})", list(foto))}
        for evento_id, casas in foto.items():
            ev, ref = eventos.get(evento_id), casas.get(referencia)
            if not ev or not ref or len(ref["momios"]) < 2 or a_fecha(ev["inicio"]) <= momento:
                continue
            selecciones = list(ref["momios"])
            justas = _justas_de(ref["momios"], selecciones)
            precios = [(casa, sel, precio_ejecutado(m, desliz)) for casa, d in casas.items()
                       if casa not in excluidas and d["actualizado"] and set(d["momios"]) == set(selecciones)
                       and momento - a_fecha(d["actualizado"]) <= max_antiguedad
                       for sel, m in d["momios"].items()]
            _anotar_senales(con, config, ev, precios, justas, margen_casa(list(ref["momios"].values())), capturado)
    con.commit()
    return con.execute("SELECT COUNT(*) FROM senales").fetchone()[0]


def medir_senales(con, config: dict) -> int:
    """CLV de las apuestas fantasma de partidos que ya empezaron: contra la última foto de Pinnacle antes del inicio
    (si es posterior a la señal) o, si no la hay, contra el cierre de DraftKings que publica ESPN. Gratis."""
    referencia = config["casa_referencia"]
    momento = ahora()
    medidas, cache = 0, {}
    for ev in con.execute("""SELECT DISTINCT s.evento_id, s.deporte, s.inicio, e.local, e.visitante FROM senales s
                             LEFT JOIN eventos e ON e.id = s.evento_id
                             WHERE s.revisado = 0 AND s.inicio <= ?""", (iso(momento),)).fetchall():
        cierre = con.execute("""SELECT MAX(capturado) FROM momios WHERE evento_id = ? AND casa = ? AND mercado = 'h2h'
                                AND capturado < ?""", (ev["evento_id"], referencia, ev["inicio"])).fetchone()[0]
        justas = {}
        if cierre:
            momios = {r[0]: r[1] for r in con.execute(
                """SELECT seleccion, momio FROM momios WHERE evento_id = ? AND casa = ? AND mercado = 'h2h'
                   AND capturado = ?""", (ev["evento_id"], referencia, cierre))}
            if len(momios) >= 2:
                justas = dict(zip(momios, probabilidades_justas(list(momios.values()))))
        inicio = a_fecha(ev["inicio"])
        espn = bool(ev["local"]) and marcadores.cubierto_momios(ev["deporte"])
        for s in con.execute("SELECT id, seleccion, momio, capturado FROM senales WHERE evento_id = ? AND revisado = 0",
                             (ev["evento_id"],)).fetchall():
            p, fuente = None, None
            if cierre and cierre > s["capturado"] and s["seleccion"] in justas:
                p, fuente = justas[s["seleccion"]], "pinnacle"
            elif espn:
                p = marcadores.probabilidad_cierre(ev["deporte"], ev["local"], ev["visitante"], inicio, s["seleccion"], cache)
                fuente = "draftkings" if p is not None else None
                if p is None and momento < inicio + timedelta(hours=6):
                    continue  # ESPN todavía puede publicar el cierre: se reintenta en el siguiente ciclo
            con.execute("""UPDATE senales SET prob_cierre = ?, clv = ?, clv_fuente = ?, revisado = 1 WHERE id = ?""",
                        (p, s["momio"] * p - 1 if p is not None else None, fuente, s["id"]))
            medidas += p is not None
    con.commit()
    return medidas


def calcular_clv(con, config: dict) -> int:
    """Para partidos que ya empezaron, compara el momio apostado contra la última foto de Pinnacle
    antes del inicio. Si no hubo foto posterior a la apuesta, el CLV queda vacío."""
    referencia = config["casa_referencia"]
    filas = con.execute("""SELECT a.id, a.evento_id, a.seleccion, a.momio, a.colocada, a.inicio, a.deporte,
                                  e.local, e.visitante
                           FROM apuestas a JOIN eventos e ON e.id = a.evento_id
                           WHERE a.cierre_revisado = 0 AND a.estado != 'anulada' AND a.inicio <= ?""",
                        (iso(ahora()),)).fetchall()
    cache = {}
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
        fuente = "pinnacle" if clv is not None else None
        if clv is None:  # sin foto de Pinnacle posterior a la apuesta: cierre publicado por ESPN (gratis)
            inicio = a_fecha(f["inicio"])
            p = marcadores.probabilidad_cierre(f["deporte"], f["local"], f["visitante"], inicio, f["seleccion"], cache)
            if p is not None:
                clv, prob_cierre, fuente = f["momio"] * p - 1, p, "draftkings"
            elif marcadores.cubierto_momios(f["deporte"]) and ahora() < inicio + timedelta(hours=6):
                continue  # ESPN todavía puede publicarlo: se reintenta en el siguiente ciclo
        con.execute("""UPDATE apuestas SET momio_cierre_ref = ?, prob_cierre = ?, clv = ?, clv_fuente = ?,
                       cierre_revisado = 1 WHERE id = ?""", (momio_cierre, prob_cierre, clv, fuente, f["id"]))
    con.commit()
    return len(filas)
