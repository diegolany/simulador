"""Cómo aprende el bot para no estancarse.

La señal de aprendizaje es el CLV (valor contra el momio de cierre), no la ganancia:
la ganancia tarda cientos de apuestas en dejar de ser suerte, el CLV no.

1. Prioridad de ligas (cada barrido): gasta los créditos donde más valor ha
   encontrado, sin dejar de explorar ligas poco revisadas.
2. Revisión semanal:
   - Filtros: la Principal deja de apostar en ligas o casas con CLV negativo
     comprobado, y regresa si los experimentos muestran que mejoraron.
   - Promoción: si una retadora le gana claramente a la Principal, sus reglas
     pasan a ser las de la Principal (y las reglas viejas quedan como retadora).
   - Retiro: las retadoras que pierden contra el mercado salen del laboratorio.
   - Variantes nuevas: se crean retadoras con cambios a las reglas de la
     Principal para seguir buscando mejoras.
"""
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from base_datos import a_fecha, ahora, anotar, guardar_estado, iso, leer_estado

OPCIONES = {
    "umbral": [0.01, 0.015, 0.02, 0.03, 0.04, 0.06],
    "momio_min": [1.15, 1.30, 1.50, 1.80],
    "momio_max": [2.5, 3.5, 5.0, 8.0, 15.0],
    "horas_max": [3, 6, 12, 24, 48, 72],
    "horas_min": [0.17, 2, 6, 24],
}
NOMBRES = {"umbral": "valor mínimo", "momio_min": "momio mínimo", "momio_max": "momio máximo",
           "horas_max": "máximo de horas antes del partido", "horas_min": "mínimo de horas antes del partido"}


def estadistica(valores: list[float]) -> tuple[int, float, float]:
    """(cantidad, promedio, error estándar)."""
    n = len(valores)
    if n == 0:
        return 0, 0.0, 0.0
    media = sum(valores) / n
    if n == 1:
        return 1, media, abs(media) + 0.05
    varianza = sum((v - media) ** 2 for v in valores) / (n - 1)
    return n, media, math.sqrt(varianza / n)


def _clv_por(con, campo: str, estrategia: str | None = None) -> dict:
    sql = (f"SELECT a.{campo}, a.clv FROM apuestas a JOIN estrategias e ON e.nombre = a.estrategia "
           f"WHERE e.tipo = 'valor' AND a.clv IS NOT NULL")
    parametros = []
    if estrategia:
        sql += " AND a.estrategia = ?"
        parametros.append(estrategia)
    grupos = {}
    for clave, clv in con.execute(sql, parametros):
        grupos.setdefault(clave, []).append(clv)
    return grupos


def prioridad_ligas(con, config: dict, deportes: list[str]) -> list[str]:
    valores = valor_ligas(con, config, deportes)
    return sorted(deportes, key=lambda d: valores[d], reverse=True)


def valor_ligas(con, config: dict, deportes: list[str]) -> dict[str, float]:
    """Qué tanto vale descargar cada liga: valor encontrado por crédito (ajustado por su CLV)
    más un bono de exploración que crece si se revisa poco. Las nunca revisadas valen el máximo."""
    descargas = {f[0]: (f[1], f[2] or 0) for f in con.execute(
        "SELECT deporte, COUNT(*), SUM(senales) FROM consumo_api WHERE endpoint = 'odds' GROUP BY deporte")}
    total = sum(n for n, _ in descargas.values())
    clv = _clv_por(con, "deporte")
    previa = (evidencia(con) or {}).get("por_clave", {})
    tasas = {d: (s + 1) / (n + 2) for d, (n, s) in descargas.items()}
    maxima = max(tasas.values(), default=1)
    puntajes = {}
    for d in deportes:
        n, _ = descargas.get(d, (0, 0))
        if n == 0:
            puntajes[d] = 3.0 * _factor_historico(previa.get(d))
            continue
        m, media, _ = estadistica(clv.get(d, []))
        factor = min(2.0, max(0.2, 1 + 20 * media * m / (m + 20)))
        # Lo aprendido de temporadas pasadas pesa mientras haya pocos datos en vivo de esa liga
        factor *= 1 + (_factor_historico(previa.get(d)) - 1) * 20 / (m + 20)
        bono = config["aprendizaje"]["exploracion"] * math.sqrt(math.log(total + 1) / (n + 1))
        puntajes[d] = tasas[d] / maxima * factor + bono
    return puntajes


def evidencia(con) -> dict | None:
    """Resultado más reciente de la prueba con temporadas pasadas: el del estudio automático o el del archivo."""
    datos = leer_estado(con, "evidencia")
    if datos:
        return datos
    archivo = Path(__file__).with_name("backtest_futbol.json")
    return json.loads(archivo.read_text(encoding="utf-8")) if archivo.exists() else None


def _factor_historico(prueba: dict | None) -> float:
    """De 0.5 a 1.5 según la ventaja que tuvo la estrategia en esa liga en temporadas pasadas."""
    if not prueba or prueba.get("n", 0) < 50:
        return 1.0
    senal = prueba["clv"] if prueba.get("clv") is not None else prueba["rendimiento"] / 2
    return 1 + max(-0.5, min(0.5, 8 * senal))


def aprendizaje_diario(con) -> None:
    """Una vez al día: qué tipo de apuesta le está ganando al mercado y cuál no, según el CLV."""
    hoy = date.today().isoformat()
    if leer_estado(con, "aprendizaje_diario") == hoy:
        return
    guardar_estado(con, "aprendizaje_diario", hoy)
    filas = con.execute(
        """SELECT a.momio, a.clv, a.casa, a.liga, (julianday(a.inicio) - julianday(a.colocada)) * 24 AS horas
           FROM apuestas a JOIN estrategias e ON e.nombre = a.estrategia
           WHERE e.tipo = 'valor' AND a.clv IS NOT NULL""").fetchall()
    if len(filas) < 15:
        anotar(con, "aprendizaje", f"Aprendizaje del día: {len(filas)} apuestas con CLV medido. Con 15 o más empiezo "
                                   f"a comparar qué tipo de apuesta le gana al mercado.")
        return
    grupos = {}
    for f in filas:
        momio = "momios menores a 1.80" if f["momio"] < 1.8 else "momios de 1.80 a 3.00" if f["momio"] <= 3 else "momios mayores a 3.00"
        horas = "apuestas a menos de 6 h" if f["horas"] < 6 else "apuestas de 6 a 24 h antes" if f["horas"] <= 24 else "apuestas con más de 24 h"
        for clave in (momio, horas, f"la casa {f['casa']}", f"la liga {f['liga']}"):
            grupos.setdefault(clave, []).append(f["clv"])
    medidos = [(clave, *estadistica(v)) for clave, v in grupos.items() if len(v) >= 8]
    _, general, _ = estadistica([f["clv"] for f in filas])
    if not medidos:
        return
    mejor = max(medidos, key=lambda x: x[2])
    peor = min(medidos, key=lambda x: x[2])
    anotar(con, "aprendizaje", f"Aprendizaje del día ({len(filas)} apuestas con CLV, promedio {general:+.1%}): "
                               f"le va mejor con {mejor[0]} (CLV {mejor[2]:+.1%} en {mejor[1]}) y peor con "
                               f"{peor[0]} (CLV {peor[2]:+.1%} en {peor[1]}).")
    con.commit()


def calibracion_pronosticos(con) -> tuple[int, list[dict]]:
    """(partidos con resultado, tramos) comparando la probabilidad justa estimada contra lo que pasó."""
    tramos = [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    grupos, partidos = {t: [] for t in tramos}, 0
    for f in con.execute("""SELECT prob_local, prob_empate, prob_visitante, resultado FROM pronosticos
                            WHERE resultado IN ('local', 'empate', 'visitante')"""):
        if f["resultado"] == "empate" and f["prob_empate"] is None:
            continue  # empate en un mercado sin empate (ej. NFL): no cuenta
        partidos += 1
        for prob, nombre in ((f["prob_local"], "local"), (f["prob_empate"], "empate"), (f["prob_visitante"], "visitante")):
            if prob is not None:
                tramo = next(t for t in tramos if t[0] <= prob < t[1])
                grupos[tramo].append((prob, f["resultado"] == nombre))
    return partidos, [{"tramo": f"{a:.0%}–{min(b, 1):.0%}", "n": len(g), "estimada": sum(p for p, _ in g) / len(g),
                       "real": sum(1 for _, x in g if x) / len(g)} for (a, b), g in grupos.items() if g]


def toca_revision(con, config: dict) -> bool:
    ultima = leer_estado(con, "ultima_revision")
    dias = config["aprendizaje"]["dias_entre_revisiones"]
    return ultima is None or ahora() - a_fecha(ultima) >= timedelta(days=dias)


def _leer(con, nombre: str) -> dict:
    f = con.execute("SELECT * FROM estrategias WHERE nombre = ?", (nombre,)).fetchone()
    return {**dict(f), "p": json.loads(f["parametros"])}


def _guardar_parametros(con, nombre: str, parametros: dict, descripcion: str | None = None) -> None:
    con.execute("UPDATE estrategias SET parametros = ?, descripcion = COALESCE(?, descripcion) WHERE nombre = ?",
                (json.dumps(parametros), descripcion, nombre))


def _describir(p: dict) -> str:
    return (f"valor ≥ {p['umbral']:.1%}, momios {p['momio_min']:.2f}–{p['momio_max']:.2f}, "
            f"de {p['horas_min']:g} a {p['horas_max']:g} h antes")


def mutar(base: dict, existentes: list[dict], rng: random.Random) -> tuple[dict, str]:
    """Crea una variante cambiando 1 o 2 reglas de la Principal."""
    comparables = [{k: e[k] for k in OPCIONES} for e in existentes]
    for _ in range(20):
        p = {**base, "ligas_bloqueadas": [], "casas_bloqueadas": []}
        cambios = []
        for clave in rng.sample(list(OPCIONES), k=rng.choice([1, 2])):
            p[clave] = rng.choice([v for v in OPCIONES[clave] if v != base[clave]])
            cambios.append(clave)
        if p["horas_min"] >= p["horas_max"]:
            p["horas_max"] = 72
        if p["momio_min"] >= p["momio_max"]:
            p["momio_max"] = 15.0
        if {k: p[k] for k in OPCIONES} not in comparables:
            texto = "; ".join(f"{NOMBRES[c]} {base[c]:g} → {p[c]:g}" for c in cambios)
            return p, texto
    return p, "variación aleatoria"


def revision(con, config: dict) -> None:
    ap = config["aprendizaje"]
    inicio = a_fecha(leer_estado(con, "fecha_inicio"))
    semana = (ahora() - inicio).days // 7 + 1
    inicial = config["banca_inicial"]
    principal = _leer(con, "Principal")

    # Resumen de la semana para la bitácora
    hace_7 = iso(ahora() - timedelta(days=7))
    n_sem, ganancia_sem, apostado_sem = con.execute(
        """SELECT COUNT(*), COALESCE(SUM(ganancia), 0),
                  COALESCE(SUM(CASE WHEN estado IN ('ganada', 'perdida') THEN monto END), 0)
           FROM apuestas WHERE estrategia = 'Principal' AND estado != 'abierta' AND liquidada >= ?""",
        (hace_7,)).fetchone()
    ganancia_total = con.execute("""SELECT COALESCE(SUM(ganancia), 0) FROM apuestas
                                    WHERE estrategia = 'Principal' AND estado != 'abierta'""").fetchone()[0]
    clv_p = [v for vals in _clv_por(con, "estrategia", "Principal").values() for v in vals]
    n_p, m_p, e_p = estadistica(clv_p)
    rendimiento = f"{ganancia_sem / apostado_sem:+.1%}" if apostado_sem else "sin apuestas liquidadas"
    anotar(con, "semana",
           f"Revisión semana {semana - 1 if semana > 1 else 1}: {n_sem} apuestas liquidadas en 7 días, "
           f"ganancia ${ganancia_sem:,.0f} ({rendimiento} sobre lo apostado). "
           f"Acumulado: {ganancia_total / inicial:+.2%} de la banca. CLV de la Principal: "
           + (f"{m_p:+.2%} en {n_p} apuestas." if n_p else "todavía sin medir."))

    # 1. Filtros aprendidos: ligas y casas donde el mercado nos gana
    for campo, clave_param, nombre in (("liga", "ligas_bloqueadas", "la liga"),
                                        ("casa", "casas_bloqueadas", "la casa")):
        bloqueadas = set(principal["p"][clave_param])
        for valor, clvs in _clv_por(con, campo).items():
            n, media, ee = estadistica(clvs)
            if n < ap["min_apuestas_filtro"]:
                continue
            if valor not in bloqueadas and media + ee < -0.005:
                bloqueadas.add(valor)
                anotar(con, "ajuste", f"La Principal deja de apostar en {nombre} {valor}: "
                                      f"CLV {media:+.1%} en {n} apuestas (el mercado nos gana ahí).")
            elif valor in bloqueadas and media - ee > 0:
                bloqueadas.discard(valor)
                anotar(con, "ajuste", f"La Principal vuelve a apostar en {nombre} {valor}: "
                                      f"los experimentos muestran CLV {media:+.1%} en {n} apuestas.")
        principal["p"][clave_param] = sorted(bloqueadas)
    _guardar_parametros(con, "Principal", principal["p"])

    # 2. Promoción: una retadora con CLV claramente mejor pasa sus reglas a la Principal
    retadoras = [_leer(con, f[0]) for f in con.execute("SELECT nombre FROM estrategias WHERE rol = 'retadora'")]
    if n_p < ap["min_apuestas_clv_promocion"]:
        anotar(con, "aprendizaje", f"La Principal lleva {n_p} apuestas con CLV medido; se necesitan "
                                   f"{ap['min_apuestas_clv_promocion']} para compararla con las retadoras.")
    else:
        candidatas = []
        for r in retadoras:
            n, media, ee = estadistica([v for vals in _clv_por(con, "estrategia", r["nombre"]).values()
                                        for v in vals])
            if n >= ap["min_apuestas_clv_promocion"] and media - ee > m_p and media > m_p + 0.005:
                candidatas.append((media - ee, media, n, r))
        if candidatas:
            _, media, n, mejor = max(candidatas, key=lambda c: c[0])
            anterior = f"Principal anterior (S{semana})"
            con.execute("INSERT INTO estrategias (nombre, tipo, rol, descripcion, parametros, creada) "
                        "VALUES (?, 'valor', 'retadora', ?, ?, ?)",
                        (anterior, f"Reglas que tenía la Principal hasta la semana {semana}: "
                                   + _describir(principal["p"]),
                         json.dumps({**principal["p"], "ligas_bloqueadas": [], "casas_bloqueadas": []}),
                         iso(ahora())))
            nuevos = {**mejor["p"], "ligas_bloqueadas": principal["p"]["ligas_bloqueadas"],
                      "casas_bloqueadas": principal["p"]["casas_bloqueadas"]}
            _guardar_parametros(con, "Principal", nuevos, f"{_describir(nuevos)} (aprendida de {mejor['nombre']})")
            con.execute("UPDATE estrategias SET rol = 'retirada', retirada = ? WHERE nombre = ?",
                        (iso(ahora()), mejor["nombre"]))
            anotar(con, "promocion", f"¡Mejora! {mejor['nombre']} superó a la Principal (CLV {media:+.2%} contra "
                                     f"{m_p:+.2%}, {n} apuestas). La Principal adopta sus reglas: "
                                     f"{_describir(nuevos)}. Las reglas anteriores siguen compitiendo.")
            principal = _leer(con, "Principal")

    # 3. Retiro de retadoras que pierden contra el mercado
    for r in [_leer(con, f[0]) for f in con.execute("SELECT nombre FROM estrategias WHERE rol = 'retadora'")]:
        n, media, ee = estadistica([v for vals in _clv_por(con, "estrategia", r["nombre"]).values() for v in vals])
        if n >= ap["min_apuestas_clv_retiro"] and (media + ee < 0 or (n_p and media < m_p - 0.01)):
            con.execute("UPDATE estrategias SET rol = 'retirada', retirada = ? WHERE nombre = ?",
                        (iso(ahora()), r["nombre"]))
            anotar(con, "retiro", f"Se retira {r['nombre']}: CLV {media:+.2%} en {n} apuestas, "
                                  f"no le gana al mercado.")

    # 4. Variantes nuevas para seguir explorando
    rng = random.Random()
    activas = [_leer(con, f[0])["p"] for f in con.execute(
        "SELECT nombre FROM estrategias WHERE rol IN ('principal', 'retadora')")]
    cupo = ap["max_retadoras"] - con.execute("SELECT COUNT(*) FROM estrategias WHERE rol = 'retadora'").fetchone()[0]
    for letra in "ABCDEFGH"[:max(0, cupo)]:
        parametros, cambios = mutar(principal["p"], activas, rng)
        nombre = f"Variante S{semana}-{letra}"
        con.execute("INSERT INTO estrategias (nombre, tipo, rol, descripcion, parametros, creada) "
                    "VALUES (?, 'valor', 'retadora', ?, ?, ?)",
                    (nombre, f"Principal con {cambios}", json.dumps(parametros), iso(ahora())))
        activas.append(parametros)
        anotar(con, "nueva", f"Nueva retadora {nombre}: la Principal con {cambios}.")

    guardar_estado(con, "ultima_revision", iso(ahora()))
    con.commit()


if __name__ == "__main__":
    from api import cargar_config
    from base_datos import conectar

    revision(conectar(), cargar_config())
