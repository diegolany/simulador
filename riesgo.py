"""Riesgo y confianza: lo que mide un apostador profesional además del CLV.

- Caídas de la banca (drawdown) y rachas: cuánto se puede hundir la banca en el camino.
- Suerte: qué parte de la ganancia o la pérdida es varianza y no mérito o error del método.
- Confianza: probabilidad de que la ventaja sea real, corregida por competir muchas estrategias a la vez
  (Bayes empírico: con 12 estrategias, alguna va a destacar por pura suerte; su CLV se "encoge" hacia cero
  según su incertidumbre).
- Proyección: miles de futuros posibles de la banca hasta el final del periodo (Monte Carlo).
- Exposición y salud de las cuentas: cuánto dinero está en juego y qué casas ya te habrían limitado.
Todo se calcula con los datos del simulador: 0 créditos.
"""
import math
import random
from statistics import NormalDist

from base_datos import a_fecha, ahora, anotar, guardar_estado, leer_estado

NORMAL = NormalDist()
SD_PREVIA, PESO_SD = 0.04, 5  # dispersión típica del CLV por apuesta; pesa como 5 apuestas (evita confianza falsa)
SD_MIN = 0.04            # dispersión usada cuando no hay datos
TAU_MIN, TAU_MAX = 0.01, 0.05  # rango de la diferencia real esperada entre estrategias
CONFIRMA_T = 2.0         # t para dar por comprobada una ventaja (~95%)
TOPE_FALTAN = 100_000    # más que esto ya no tiene sentido práctico


def _percentil(ordenados: list[float], q: float) -> float:
    i = q * (len(ordenados) - 1)
    bajo = int(i)
    alto = min(bajo + 1, len(ordenados) - 1)
    return ordenados[bajo] + (ordenados[alto] - ordenados[bajo]) * (i - bajo)


def _media_sd(valores: list[float]) -> tuple[int, float, float]:
    """(n, media, dispersión). La dispersión se mezcla con la típica (pesa como 5 apuestas): con 1 o 2 apuestas
    no se sabe cuánto varía el CLV, y suponer que casi nada daría una confianza falsa."""
    n = len(valores)
    if not n:
        return 0, 0.0, SD_MIN
    media = sum(valores) / n
    suma = sum((v - media) ** 2 for v in valores)
    return n, media, math.sqrt((suma + PESO_SD * SD_PREVIA ** 2) / (n - 1 + PESO_SD))


def unicas(apuestas: list[dict]) -> list[dict]:
    """Varias estrategias pueden tomar el mismo precio: para medir el mercado cuenta una sola vez."""
    vistas, salida = set(), []
    for a in apuestas:
        clave = (a["evento_id"], a["seleccion"], a["casa"], round(a["momio"], 2))
        if clave not in vistas:
            vistas.add(clave)
            salida.append(a)
    return salida


# ---------- Confianza ----------

def posteriores(grupos: dict[str, list[float]]) -> dict[str, dict]:
    """CLV de cada estrategia encogido hacia cero (Bayes empírico).

    La dispersión real entre estrategias (tau) se estima con todas: si las diferencias observadas no son
    mayores que el ruido, todas se encogen mucho. Devuelve la ventaja estimada, su incertidumbre y la
    probabilidad de que sea positiva."""
    datos = {}
    for nombre, valores in grupos.items():
        n, media, sd = _media_sd(valores)
        if n:
            datos[nombre] = (n, media, sd / math.sqrt(n))
    medidas = [d for d in datos.values() if d[0] >= 5]
    tau2 = 0.0
    if len(medidas) >= 3:
        tau2 = (sum(m * m for _, m, _ in medidas) - sum(e * e for _, _, e in medidas)) / len(medidas)
    tau2 = min(TAU_MAX ** 2, max(TAU_MIN ** 2, tau2))
    salida = {}
    for nombre, (n, media, ee) in datos.items():
        peso = tau2 / (tau2 + ee * ee)
        ventaja, incertidumbre = media * peso, math.sqrt(peso) * ee
        salida[nombre] = {"n": n, "media": media, "ee": ee, "ventaja": ventaja, "incertidumbre": incertidumbre,
                          "p_ventaja": NORMAL.cdf(ventaja / incertidumbre), "tau": math.sqrt(tau2)}
    return salida


def prob_mejor(a: dict, b: dict) -> float:
    """Probabilidad de que la ventaja real de `a` sea mayor que la de `b` (ambas ya encogidas)."""
    sd = math.sqrt(a["incertidumbre"] ** 2 + b["incertidumbre"] ** 2)
    return NORMAL.cdf((a["ventaja"] - b["ventaja"]) / sd)


def _faltan(media: float, sd: float, tau: float, n: int, objetivo: float = 0.95) -> int | None:
    """Apuestas medidas que faltan para que la probabilidad de ventaja real (ya encogida) llegue a 95%,
    si el CLV promedio se mantiene como hasta hoy."""
    if media <= 0:
        return None
    z = NORMAL.inv_cdf(objetivo)

    def alcanza(k: int) -> bool:
        peso = tau * tau / (tau * tau + sd * sd / k)
        return media * math.sqrt(peso) * math.sqrt(k) / sd >= z
    if alcanza(n):
        return 0
    if not alcanza(TOPE_FALTAN):
        return TOPE_FALTAN
    bajo, alto = n, TOPE_FALTAN
    while alto - bajo > 1:
        medio = (bajo + alto) // 2
        bajo, alto = (bajo, medio) if alcanza(medio) else (medio, alto)
    return alto - n


def confianza(clvs: list[float], post: dict | None, momios_liquidadas: list[float]) -> dict:
    """Qué tan comprobada está la ventaja de una cartera y cuántas apuestas faltan para saberlo."""
    n, media, sd = _media_sd(clvs)
    if not n:
        return {"n": 0, "p_ventaja": post["p_ventaja"] if post else None}
    ee = sd / math.sqrt(n)
    faltan_clv = _faltan(media, sd, post["tau"] if post else TAU_MIN, n)
    faltan_ganancia = None
    if media > 0 and momios_liquidadas:
        # Ganancia por $1 apostado: su varianza es ~ (momio - 1), mucho mayor que la del CLV
        varianza = sum(m - 1 for m in momios_liquidadas) / len(momios_liquidadas)
        faltan_ganancia = min(TOPE_FALTAN, max(0, math.ceil(CONFIRMA_T ** 2 * varianza / media ** 2)
                                              - len(momios_liquidadas)))
    return {"n": n, "media": media, "ic": [media - 1.96 * ee, media + 1.96 * ee], "t": media / ee,
            "gana_cierre": sum(1 for c in clvs if c > 0) / n,
            "ventaja": post["ventaja"] if post else None, "p_ventaja": post["p_ventaja"] if post else None,
            "faltan_clv": faltan_clv, "faltan_ganancia": faltan_ganancia}


# ---------- Banca ----------

def caidas(apuestas: list[dict], inicial: float) -> dict:
    """Caída máxima y actual desde el punto más alto de la banca, y rachas de resultados."""
    cerradas = sorted((a for a in apuestas if a["estado"] in ("ganada", "perdida")), key=lambda a: a["liquidada"])
    banca = pico = inicial
    maxima = maxima_pct = 0.0
    racha = peor = desde_pico = 0
    for a in cerradas:
        banca += a["ganancia"] or 0
        if banca >= pico:
            pico, desde_pico = banca, 0
        else:
            desde_pico += 1
        if pico - banca > maxima:
            maxima, maxima_pct = pico - banca, (pico - banca) / pico
        if a["estado"] == "perdida":
            racha = racha - 1 if racha < 0 else -1
        else:
            racha = racha + 1 if racha > 0 else 1
        peor = min(peor, racha)
    return {"n": len(cerradas), "maxima": maxima, "maxima_pct": maxima_pct, "actual": pico - banca,
            "actual_pct": (pico - banca) / pico, "pico": pico, "peor_racha": -peor, "racha": racha,
            "desde_pico": desde_pico}


def suerte(apuestas: list[dict]) -> dict | None:
    """Ganadas y ganancia reales contra lo esperado con la probabilidad justa (la del cierre si se conoce).
    z > 1: más suerte de lo normal; z < -1: menos. Entre -1 y 1 es lo de todos los días."""
    decididas = [a for a in apuestas if a["estado"] in ("ganada", "perdida")]
    if not decididas:
        return None
    prob = lambda a: a["prob_cierre"] or a["prob_justa"]
    esperadas = sum(prob(a) for a in decididas)
    reales = sum(1 for a in decididas if a["estado"] == "ganada")
    var_ganadas = sum(prob(a) * (1 - prob(a)) for a in decididas)
    esperada = sum(a["monto"] * (a["momio"] * prob(a) - 1) for a in decididas)
    real = sum(a["ganancia"] or 0 for a in decididas)
    sd = math.sqrt(sum((a["monto"] * a["momio"]) ** 2 * prob(a) * (1 - prob(a)) for a in decididas))
    return {"n": len(decididas), "ganadas": reales, "esperadas": esperadas,
            "z_ganadas": (reales - esperadas) / math.sqrt(var_ganadas) if var_ganadas else 0.0,
            "ganancia": real, "esperada": esperada, "suerte": real - esperada, "z": (real - esperada) / sd if sd else 0.0}


def exposicion(abiertas: list[dict], banca: float) -> dict:
    """Dinero en juego: total, por día de partido y por liga (para no cargar demasiado en un solo lugar)."""
    total = sum(a["monto"] for a in abiertas)
    por_dia, por_liga = {}, {}
    for a in abiertas:
        dia = a_fecha(a["inicio"]).astimezone().date().isoformat()
        d = por_dia.setdefault(dia, {"dia": dia, "monto": 0.0, "apuestas": 0})
        d["monto"] += a["monto"]
        d["apuestas"] += 1
        por_liga[a["liga"]] = por_liga.get(a["liga"], 0.0) + a["monto"]
    ligas = sorted(({"liga": l, "monto": m, "pct": m / total} for l, m in por_liga.items()), key=lambda x: -x["monto"])
    return {"total": total, "pct": total / banca if banca else 0.0, "apuestas": len(abiertas),
            "max_partido": max((a["monto"] for a in abiertas), default=0.0),
            "por_dia": [por_dia[k] for k in sorted(por_dia)], "por_liga": ligas[:6],
            "concentracion": ligas[0]["pct"] if ligas else 0.0}


# ---------- Cuentas en las casas ----------

def _estado_cuenta(n: int, media: float, t: float) -> str:
    """Las casas comunes limitan a quien les gana al cierre de forma constante (no a quien gana dinero)."""
    if n >= 30 and media >= 0.02 and t >= CONFIRMA_T:
        return "limitada"
    if n >= 15 and media >= 0.01:
        return "vigilada"
    return "normal"


def cuentas(apuestas: list[dict]) -> list[dict]:
    """Salud de la cuenta en cada casa, contando cada apuesta una sola vez (como si fuera una sola persona)."""
    por_casa = {}
    for a in unicas([a for a in apuestas if a["estado"] != "anulada" and a["casa"] != "promedio"]):
        por_casa.setdefault(a["casa"], []).append(a)
    salida = []
    for casa, lista in por_casa.items():
        clvs = [a["clv"] for a in lista if a["clv"] is not None]
        n, media, sd = _media_sd(clvs)
        t = media / (sd / math.sqrt(n)) if n else 0.0
        decididas = [a for a in lista if a["estado"] in ("ganada", "perdida")]
        salida.append({"casa": casa, "apuestas": len(lista), "apostado": sum(a["monto"] for a in lista),
                       "ganancia": sum(a["ganancia"] or 0 for a in decididas), "clv_n": n,
                       "clv": media if n else None, "gana_cierre": sum(1 for c in clvs if c > 0) / n if n else None,
                       "estado": _estado_cuenta(n, media, t)})
    return sorted(salida, key=lambda c: (-c["apuestas"], c["casa"]))


def cuentas_limitadas(con) -> set[str]:
    filas = [dict(r) for r in con.execute(
        """SELECT a.evento_id, a.seleccion, a.casa, a.momio, a.monto, a.estado, a.ganancia, a.clv FROM apuestas a
           JOIN estrategias e ON e.nombre = a.estrategia WHERE e.tipo IN ('valor', 'gratis')""")]
    return {c["casa"] for c in cuentas(filas) if c["estado"] == "limitada"}


# ---------- Proyección Monte Carlo ----------

def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limite, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limite:
            return k
        k += 1


def proyeccion(apuestas: list[dict], respaldo: list[dict], inicial: float, inicio, momento, objetivos: list[float],
               post: dict | None, pico: float, simulaciones: int = 2000, escala: float = 1.0) -> dict | None:
    """Simula miles de futuros de la banca hasta el final del periodo con el ritmo de apuestas, los momios y los
    montos de la cartera, y la ventaja estimada (con su incertidumbre). `escala` multiplica los montos de las
    apuestas futuras (la agresividad del modo objetivo). Devuelve percentiles por día y las probabilidades de
    cumplir cada objetivo, terminar en pérdida o sufrir caídas fuertes."""
    validas = [a for a in apuestas if a["estado"] != "anulada"]
    dias_total = 7 * len(objetivos)
    transcurrido = max(0.0, (momento - inicio).total_seconds() / 86400)
    if transcurrido >= dias_total:
        return None
    # Fracción de la banca de cada apuesta sin la agresividad con la que se hizo; luego se aplica `escala`
    fraccion = lambda a: a["monto"] / inicial / (a.get("multiplicador") or 1.0) * escala
    muestra = [(a["momio"], fraccion(a)) for a in validas]
    if len(muestra) < 8:  # pocas apuestas propias: se completa con las del resto del laboratorio
        muestra += [(a["momio"], fraccion(a)) for a in respaldo[:200]]
    if not muestra:
        return None
    ritmo = max(0.5, len(validas) / max(1.0, transcurrido))
    banca = inicial + sum(a["ganancia"] or 0 for a in validas if a["estado"] != "abierta")
    abiertas = [(a["momio"], a["monto"]) for a in validas if a["estado"] == "abierta"]
    ventaja = post["ventaja"] if post else 0.0
    incertidumbre = post["incertidumbre"] if post else TAU_MIN
    dias = list(range(math.floor(transcurrido) + 1, dias_total + 1))
    rng = random.Random(f"{len(validas)}|{banca:.0f}|{transcurrido:.2f}")
    por_dia = [[] for _ in dias]
    finales, semanas = [], {7 * (i + 1): [] for i in range(len(objetivos)) if 7 * (i + 1) > transcurrido}
    caida_10 = caida_20 = 0
    for _ in range(simulaciones):
        borde = rng.gauss(ventaja, incertidumbre)  # la ventaja real no se conoce: cada futuro usa una posible
        b, alto, peor = banca, max(pico, banca), (max(pico, banca) - banca) / max(pico, banca)
        for momio, monto in abiertas:
            p = min(0.99, max(0.01, (1 + borde) / momio))
            b += monto * (momio - 1) if rng.random() < p else -monto
        t = transcurrido
        for i, d in enumerate(dias):
            for _ in range(_poisson(rng, ritmo * (d - t))):
                momio, fraccion = rng.choice(muestra)
                monto = fraccion * b
                p = min(0.99, max(0.01, (1 + borde) / momio))
                b += monto * (momio - 1) if rng.random() < p else -monto
                if b > alto:
                    alto = b
                elif (alto - b) / alto > peor:
                    peor = (alto - b) / alto
            t = d
            por_dia[i].append(b)
            if d in semanas:
                semanas[d].append(b)
        finales.append(b)
        caida_10 += peor >= 0.10
        caida_20 += peor >= 0.20
    bandas = {q: [] for q in ("p5", "p25", "p50", "p75", "p95")}
    for valores in por_dia:
        valores.sort()
        for q in bandas:
            bandas[q].append(round(_percentil(valores, int(q[1:]) / 100), 2))
    finales.sort()
    meta = inicial * (1 + objetivos[-1])
    return {
        "dias": dias, **bandas, "desde": round(transcurrido, 3), "banca": banca,
        "prob_meta": sum(1 for b in finales if b >= meta) / simulaciones,
        "prob_semanas": [{"semana": d // 7, "objetivo": objetivos[d // 7 - 1],
                          "prob": sum(1 for b in v if b >= inicial * (1 + objetivos[d // 7 - 1])) / simulaciones}
                         for d, v in semanas.items()],
        "prob_perdida": sum(1 for b in finales if b < inicial) / simulaciones,
        "prob_caida_10": caida_10 / simulaciones, "prob_caida_20": caida_20 / simulaciones,
        "mediana": _percentil(finales, 0.5), "rango": [_percentil(finales, 0.05), _percentil(finales, 0.95)],
        "ventaja": ventaja, "incertidumbre": incertidumbre, "ritmo": ritmo, "simulaciones": simulaciones,
        "muestra": len(muestra), "propias": len(validas), "escala": escala,
    }


# ---------- Panel completo ----------

_memoria = {}

CAMPOS = """a.estrategia, a.evento_id, a.seleccion, a.casa, a.momio, a.monto, a.estado, a.ganancia, a.liquidada,
            a.colocada, a.inicio, a.liga, a.deporte, a.prob_justa, a.prob_cierre, a.clv, a.valor, a.multiplicador"""
MULTIPLICADORES = (0.5, 0.75, 1.0, 1.25, 1.5)


def grupos_clv(con) -> dict[str, list[float]]:
    """CLV medido de cada estrategia (sin anuladas), para compararlas entre sí."""
    grupos = {}
    for nombre, clv in con.execute("SELECT estrategia, clv FROM apuestas WHERE clv IS NOT NULL AND estado != 'anulada'"):
        grupos.setdefault(nombre, []).append(clv)
    return grupos


def posteriores_laboratorio(con) -> dict[str, dict]:
    """Posteriores de todas las estrategias. La de control (apostador casual) no entra al cálculo de la
    dispersión: su desventaja es conocida y haría creer que las demás difieren más de lo que difieren."""
    grupos = grupos_clv(con)
    control = {r[0] for r in con.execute("SELECT nombre FROM estrategias WHERE rol = 'control'")}
    resultado = posteriores({k: v for k, v in grupos.items() if k not in control})
    for nombre in control & set(grupos):
        resultado[nombre] = posteriores({nombre: grupos[nombre]})[nombre]
    return resultado


def _datos(con, config: dict) -> dict:
    """Lo que necesitan el panel y el modo objetivo: apuestas de la Principal, respaldo del laboratorio y su ventaja."""
    momento = ahora()
    inicial = config["banca_inicial"]
    texto_inicio = leer_estado(con, "fecha_inicio")
    todas = [dict(r) for r in con.execute(f"SELECT {CAMPOS} FROM apuestas a ORDER BY a.colocada")]
    tipos = {r[0]: r[1] for r in con.execute("SELECT nombre, tipo FROM estrategias")}
    principal = [a for a in todas if a["estrategia"] == "Principal"]
    return {"momento": momento, "inicial": inicial, "inicio": a_fecha(texto_inicio) if texto_inicio else momento,
            "todas": todas, "tipos": tipos, "principal": principal, "post": posteriores_laboratorio(con).get("Principal"),
            "caidas": caidas(principal, inicial),
            "respaldo": [a for a in todas if tipos.get(a["estrategia"]) == "valor" and a["estrategia"] != "Principal"
                         and a["estado"] != "anulada"]}


def _proyectar(d: dict, config: dict, simulaciones: int, escala: float) -> dict | None:
    return proyeccion(d["principal"], d["respaldo"], d["inicial"], d["inicio"], d["momento"], config["objetivos_semana"],
                      d["post"], d["caidas"]["pico"], simulaciones, escala)


def modo_objetivo(con, config: dict) -> dict:
    """La mentalidad de meta: en cada ciclo simula la cartera con distintas agresividades (montos ×0.5 a ×1.5) y elige
    la que da más probabilidad de cumplir la meta sin pasar el límite de riesgo (probabilidad de caer 20% o más).
    Solo sube de ×1 si es probable que la ventaja sea real: apostar más sin ventaja solo agrega riesgo.
    Si va adelante de la meta, la misma cuenta lo lleva a bajar el riesgo para cuidarla."""
    o = config["objetivo"]
    anterior = leer_estado(con, "modo_objetivo") or {}
    actual = anterior.get("multiplicador", 1.0)
    if not o["activo"]:
        resultado = {"multiplicador": 1.0, "opciones": [], "razon": "Modo objetivo apagado: montos normales."}
        guardar_estado(con, "modo_objetivo", resultado)
        return resultado
    d = _datos(con, config)
    opciones = []
    for m in MULTIPLICADORES:
        if not o["minimo"] <= m <= o["maximo"]:
            continue
        p = _proyectar(d, config, o["simulaciones"], m)
        if p is None:
            resultado = {"multiplicador": 1.0, "opciones": [], "razon": "El periodo terminó o aún no hay apuestas para simular."}
            guardar_estado(con, "modo_objetivo", resultado)
            return resultado
        opciones.append({"multiplicador": m, "prob_meta": p["prob_meta"], "prob_perdida": p["prob_perdida"],
                         "prob_caida_20": p["prob_caida_20"], "mediana": p["mediana"]})
    p_ventaja = d["post"]["p_ventaja"] if d["post"] else 0.5
    for x in opciones:
        x["permitida"] = x["prob_caida_20"] <= o["max_caida_20"] and (x["multiplicador"] <= 1.0
                                                                     or p_ventaja >= o["prob_ventaja_para_subir"])
    permitidas = [x for x in opciones if x["permitida"]]
    if not permitidas:
        elegida = opciones[0]  # todas pasan el límite: lo más prudente
    else:
        mejor = max(x["prob_meta"] for x in permitidas)
        # Entre las que dan casi lo mismo (1 punto), la de menos riesgo; y no se cambia por diferencias de ruido
        elegida = min((x for x in permitidas if x["prob_meta"] >= mejor - 0.01), key=lambda x: x["multiplicador"])
        sigue = next((x for x in permitidas if x["multiplicador"] == actual), None)
        if sigue and sigue["prob_meta"] >= elegida["prob_meta"] - 0.015:
            elegida = sigue
    normal = next((x for x in opciones if x["multiplicador"] == 1.0), elegida)
    m = elegida["multiplicador"]
    if m > 1:
        razon = (f"Subo los montos ×{m:g}: la ventaja parece real ({p_ventaja:.0%} de probabilidad) y así la probabilidad "
                 f"de cumplir la meta pasa de {normal['prob_meta']:.0%} a {elegida['prob_meta']:.0%}, con riesgo de caer "
                 f"20% de {elegida['prob_caida_20']:.0%} (límite {o['max_caida_20']:.0%}).")
    elif m < 1 and not normal.get("permitida", True):
        razon = (f"Bajo los montos ×{m:g}: con montos normales el riesgo de caer 20% sería {normal['prob_caida_20']:.0%}, "
                 f"arriba del límite de {o['max_caida_20']:.0%}. Así la probabilidad de cumplir la meta es "
                 f"{elegida['prob_meta']:.0%} (con montos normales, {normal['prob_meta']:.0%}) con riesgo de "
                 f"{elegida['prob_caida_20']:.0%}. Cuido la banca para seguir en carrera.")
    elif m < 1:
        razon = (f"Bajo los montos ×{m:g}: apostar más no acerca la meta lo suficiente ({normal['prob_meta']:.0%} con montos "
                 f"normales contra {elegida['prob_meta']:.0%}) y sí sube el riesgo de caer 20% ({normal['prob_caida_20']:.0%} "
                 f"contra {elegida['prob_caida_20']:.0%}). Cuido la banca para seguir en carrera.")
    else:
        razon = (f"Mantengo los montos normales: es lo que da más probabilidad de cumplir la meta "
                 f"({elegida['prob_meta']:.0%}) sin pasar el límite de riesgo.")
    if p_ventaja < o["prob_ventaja_para_subir"] and any(x["multiplicador"] > 1 for x in opciones):
        razon += (f" No subo más porque la probabilidad de que la ventaja sea real ({p_ventaja:.0%}) todavía no llega a "
                  f"{o['prob_ventaja_para_subir']:.0%}: sin ventaja, apostar más solo es arriesgar más.")
    resultado = {"multiplicador": m, "prob_meta": elegida["prob_meta"], "opciones": opciones, "razon": razon,
                 "p_ventaja": p_ventaja, "max_caida_20": o["max_caida_20"]}
    if m != actual:
        anotar(con, "riesgo", f"Modo objetivo: montos de ×{actual:g} a ×{m:g}. {razon}")
    guardar_estado(con, "modo_objetivo", resultado)
    con.commit()
    return resultado


def panel(con, config: dict) -> dict:
    """Todas las medidas de riesgo y confianza de la cartera Principal (se calcula una vez por ciclo)."""
    huella = tuple(con.execute("SELECT COUNT(*), MAX(liquidada), MAX(colocada), COALESCE(SUM(ganancia), 0) "
                               "FROM apuestas").fetchone())
    objetivo = leer_estado(con, "modo_objetivo") or {}
    escala = objetivo.get("multiplicador", 1.0) if config["objetivo"]["activo"] else 1.0
    clave = (huella, escala, ahora().strftime("%Y%m%d%H"))
    if clave in _memoria:
        return _memoria[clave]
    d = _datos(con, config)
    principal, inicial, c = d["principal"], d["inicial"], d["caidas"]
    banca = inicial + sum(a["ganancia"] or 0 for a in principal if a["estado"] != "abierta")
    resultado = {
        "caidas": c,
        "suerte": suerte(principal),
        "exposicion": exposicion([a for a in principal if a["estado"] == "abierta"], banca),
        "confianza": confianza([a["clv"] for a in principal if a["clv"] is not None and a["estado"] != "anulada"],
                               d["post"], [a["momio"] for a in principal if a["estado"] in ("ganada", "perdida")]),
        "proyeccion": _proyectar(d, config, config["riesgo"]["simulaciones"], escala),
        "objetivo": objetivo or None,
        "cuentas": cuentas([a for a in d["todas"] if d["tipos"].get(a["estrategia"]) in ("valor", "gratis")]),
        "freno": {"activo": c["actual_pct"] >= config["riesgo"]["freno_caida"], "umbral": config["riesgo"]["freno_caida"],
                  "factor": config["riesgo"]["factor_freno"]},
        "max_exposicion": config["riesgo"]["max_exposicion"],
    }
    _memoria.clear()
    _memoria[clave] = resultado
    return resultado


def alertas(con, config: dict) -> None:
    """Avisa en la bitácora cuando una casa ya te habría limitado o cuando se activa el freno por caída."""
    datos = panel(con, config)
    antes = set(leer_estado(con, "casas_limitadas", []))
    ahora_limitadas = {c["casa"] for c in datos["cuentas"] if c["estado"] == "limitada"}
    for casa in sorted(ahora_limitadas - antes):
        anotar(con, "riesgo", f"Realismo: en la vida real {casa} ya te habría limitado (le ganas al cierre de forma "
                              f"constante). Desde ahora el simulador solo apuesta ahí hasta "
                              f"${config['ejecucion']['limite_cuenta_limitada']:,.0f}.")
    guardar_estado(con, "casas_limitadas", sorted(ahora_limitadas))
    freno = datos["freno"]["activo"]
    if freno != bool(leer_estado(con, "freno_activo", False)):
        c = datos["caidas"]
        anotar(con, "riesgo", (f"Freno activado: la Principal cayó {c['actual_pct']:.1%} desde su punto más alto. Los montos "
                               f"bajan al {datos['freno']['factor']:.0%} hasta recuperarse.") if freno else
               "Freno desactivado: la banca se recuperó y los montos vuelven a la normalidad.")
        guardar_estado(con, "freno_activo", freno)
    con.commit()
