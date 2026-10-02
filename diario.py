"""Diario del apostador virtual: cada día cuenta qué hizo, por qué, qué aprendió y qué opina.

Se escribe con plantillas a partir de los datos del simulador (no usa inteligencia artificial
ni gasta créditos). La entrada de hoy se reescribe en cada ciclo; al cambiar el día se cierra
la de ayer con sus últimos resultados.
"""
import json
from datetime import date, datetime, time, timedelta

from aprendizaje import calibracion_pronosticos, estadistica
from base_datos import a_fecha, ahora, guardar_estado, iso, leer_estado

CARTERAS = ("Principal",)


def _origen(casa: str) -> str:
    return "🆓 DraftKings" if casa == "draftkings" else "💳 Créditos"
MOTIVOS = {"barrido": "búsquedas de momios", "cierre": "fotos de cierre", "resultados": "consultas de resultados"}


def _nombre(seleccion: str) -> str:
    return "el empate" if seleccion == "Draw" else seleccion


def _clv(con, estrategia: str) -> tuple[int, float, float]:
    return estadistica([r[0] for r in con.execute(
        "SELECT clv FROM apuestas WHERE estrategia = ? AND clv IS NOT NULL AND estado != 'anulada'", (estrategia,))])


def escribir(con, config: dict, dia: date | None = None) -> None:
    dia = dia or date.today()
    inicio_dia = datetime.combine(dia, time()).astimezone()
    desde, hasta = iso(inicio_dia), iso(inicio_dia + timedelta(days=1))
    inicial = config["banca_inicial"]
    nombres = {r[0]: r[1] for r in con.execute("SELECT deporte, MAX(liga) FROM eventos GROUP BY deporte")}
    secciones = []

    resumen = []
    for cartera in CARTERAS:
        nuevas, gratis = con.execute("""SELECT COUNT(*), COALESCE(SUM(casa = 'draftkings'), 0) FROM apuestas
                                        WHERE estrategia = ? AND colocada >= ? AND colocada < ?""",
                                     (cartera, desde, hasta)).fetchone()
        ganadas, perdidas, ganancia = con.execute(
            """SELECT COALESCE(SUM(estado = 'ganada'), 0), COALESCE(SUM(estado = 'perdida'), 0), COALESCE(SUM(ganancia), 0)
               FROM apuestas WHERE estrategia = ? AND estado IN ('ganada', 'perdida') AND liquidada >= ? AND liquidada < ?""",
            (cartera, desde, hasta)).fetchone()
        total = con.execute("SELECT COALESCE(SUM(ganancia), 0) FROM apuestas WHERE estrategia = ? AND estado != 'abierta'",
                            (cartera,)).fetchone()[0]
        resumen.append(f"Cuenta {cartera}: {nuevas} apuestas nuevas ({nuevas - gratis} con créditos y {gratis} con "
                       f"momios gratuitos de DraftKings); {ganadas} ganadas y {perdidas} perdidas "
                       f"({ganancia:+,.0f} $). Banca: ${inicial + total:,.0f} ({total / inicial:+.2%} desde el inicio).")
    secciones.append({"titulo": "Resumen del día", "parrafos": resumen})

    consumo = con.execute("""SELECT motivo, COUNT(*), SUM(costo), GROUP_CONCAT(DISTINCT deporte) FROM consumo_api
                             WHERE fecha >= ? AND fecha < ? GROUP BY motivo""", (desde, hasta)).fetchall()
    trabajo = []
    if consumo:
        detalle = "; ".join(f"{r[1]} {MOTIVOS.get(r[0], r[0])} ({', '.join(nombres.get(d, d) for d in (r[3] or '').split(','))})"
                            for r in consumo)
        trabajo.append(f"Gasté {sum(r[2] or 0 for r in consumo)} créditos: {detalle}. Elegí esas ligas porque tenían "
                       f"partidos próximos, momios viejos y buen historial de valor.")
    else:
        trabajo.append("No gasté créditos: ninguna liga reunía partidos próximos, momios viejos y buen historial.")
    gratis = con.execute("""SELECT COUNT(DISTINCT evento_id) FROM momios WHERE casa = 'draftkings'
                            AND capturado >= ? AND capturado < ?""", (desde, hasta)).fetchone()[0]
    trabajo.append(f"Sin gastar: revisé marcadores en ESPN y en la Euroliga, y usé los momios de DraftKings de "
                   f"{gratis} partidos para apostar sin gastar créditos y como casa extra.")
    secciones.append({"titulo": "Cómo trabajé", "parrafos": trabajo})

    hechas = con.execute("""SELECT a.casa, e.local, e.visitante, a.seleccion, a.momio, a.razon
                            FROM apuestas a JOIN eventos e ON e.id = a.evento_id
                            WHERE a.estrategia = 'Principal' AND a.colocada >= ? AND a.colocada < ?
                            ORDER BY a.colocada""", (desde, hasta)).fetchall()
    secciones.append({"titulo": "Mis apuestas y por qué", "lista": [
        f"{_origen(r['casa'])} · {r['local']} vs {r['visitante']} → {_nombre(r['seleccion'])} a {r['momio']:.2f}. {r['razon'] or ''}"
        for r in hechas] or ["Ninguna apuesta cumplió mis reglas: prefiero no apostar a apostar sin valor."]})

    resultados = []
    for r in con.execute("""SELECT a.estrategia, e.local, e.visitante, e.marcador_local, e.marcador_visitante,
                                   a.seleccion, a.estado, a.ganancia, a.clv
                            FROM apuestas a JOIN eventos e ON e.id = a.evento_id
                            WHERE a.estrategia = 'Principal' AND a.estado IN ('ganada', 'perdida')
                              AND a.liquidada >= ? AND a.liquidada < ?""", (desde, hasta)):
        lectura = ""
        if r["clv"] is not None:
            lectura = f" Mi precio contra el cierre: {r['clv']:+.1%}"
            if r["estado"] == "perdida" and r["clv"] > 0:
                lectura += " — buena apuesta que no salió; eso es varianza, no error."
            elif r["estado"] == "ganada" and r["clv"] < 0:
                lectura += " — gané, pero el mercado no me dio la razón en el precio: hubo suerte."
            else:
                lectura += "."
        resultados.append(f"{r['estrategia']} · {r['local']} {r['marcador_local']}-{r['marcador_visitante']} {r['visitante']}: "
                          f"aposté a {_nombre(r['seleccion'])} y {'gané' if r['estado'] == 'ganada' else 'perdí'} "
                          f"${abs(r['ganancia']):,.0f}.{lectura}")
    if resultados:
        secciones.append({"titulo": "Resultados", "lista": resultados})

    aprendo = []
    for cartera in CARTERAS:
        n, media, ee = _clv(con, cartera)
        if n:
            juicio = ("le estoy ganando al mercado" if media - ee > 0 else "voy por debajo del mercado" if media + ee < 0
                      else "todavía no se distingue de la suerte")
            aprendo.append(f"{cartera}: mi precio promedio contra el cierre (CLV) es {media:+.2%} en {n} apuestas; {juicio}.")
    ranking = []
    for (nombre,) in con.execute("SELECT nombre FROM estrategias WHERE rol IN ('principal', 'retadora', 'gratuita')"):
        n, media, _ = _clv(con, nombre)
        if n >= 5:
            ranking.append((media, nombre, n))
    if ranking:
        media, nombre, n = max(ranking)
        aprendo.append(f"En el laboratorio, la estrategia con mejor CLV es {nombre} ({media:+.2%} en {n} apuestas).")
    partidos, tramos = calibracion_pronosticos(con)
    if partidos:
        grande = max(tramos, key=lambda t: t["n"])
        aprendo.append(f"Calibración: ya tengo {partidos} partidos pronosticados con resultado. Cuando estimo "
                       f"{grande['tramo']} (promedio {grande['estimada']:.0%}), se cumple {grande['real']:.0%} "
                       f"({grande['n']} casos). " + ("Está bien calibrado." if abs(grande['real'] - grande['estimada']) < 0.06
                                                     else "Todavía hay diferencia; necesito más partidos."))
    for (mensaje,) in con.execute("""SELECT mensaje FROM bitacora WHERE tipo IN ('aprendizaje', 'ajuste', 'promocion', 'retiro', 'nueva')
                                      AND fecha >= ? AND fecha < ? ORDER BY id""", (desde, hasta)):
        aprendo.append(mensaje)
    secciones.append({"titulo": "Lo que estoy aprendiendo", "parrafos": aprendo or [
        "Todavía no tengo suficientes datos medidos para sacar conclusiones."]})

    opinion = []
    terminadas = con.execute("""SELECT COUNT(*) FROM apuestas WHERE estrategia = 'Principal'
                                AND estado IN ('ganada', 'perdida')""").fetchone()[0]
    if terminadas < 30:
        opinion.append(f"La Principal lleva {terminadas} apuestas terminadas: con tan pocas, la ganancia o la pérdida "
                       f"es casi pura suerte. Me guío por el CLV, que dice antes si estoy tomando buenos precios.")
    n, media, _ = _clv(con, "Apostador casual")
    if n >= 5:
        opinion.append(f"El apostador casual (mi grupo de control) lleva CLV {media:+.2%}: "
                       + ("como esperaba, apostar al favorito sin buscar valor pierde contra el mercado." if media < 0
                          else "por ahora va mejor de lo esperado; con más datos debería caer."))
    por_origen = {}
    for casa, clv in con.execute("""SELECT a.casa, a.clv FROM apuestas a JOIN estrategias e ON e.nombre = a.estrategia
                                    WHERE e.tipo = 'valor' AND a.clv IS NOT NULL AND a.estado != 'anulada'"""):
        por_origen.setdefault("gratis" if casa == "draftkings" else "creditos", []).append(clv)
    n_c, m_c, _ = estadistica(por_origen.get("creditos", []))
    n_g, m_g, _ = estadistica(por_origen.get("gratis", []))
    if n_c >= 10 and n_g >= 10:
        opinion.append(f"Por origen del momio: con créditos llevo CLV {m_c:+.2%} ({n_c} apuestas) y con DraftKings gratis "
                       f"{m_g:+.2%} ({n_g}). " + ("Los momios gratuitos están rindiendo igual o mejor." if m_g >= m_c
                                                 else "Los momios pagados siguen dando mejores precios que los gratuitos."))
    elif n_g:
        opinion.append(f"Ya tengo {n_g} apuestas con momios gratuitos medidas (CLV {m_g:+.2%}); con 10 o más las comparo "
                       f"contra las de créditos.")
    secciones.append({"titulo": "Mi opinión", "parrafos": opinion or ["Sin opinión firme todavía: faltan datos."]})

    plan = []
    calendario = leer_estado(con, "calendario", {})
    limite = iso(ahora() + timedelta(hours=24))
    proximos = sorted(((sum(1 for t in inicios if iso(ahora()) < t <= limite), d) for d, inicios in calendario.items()),
                      reverse=True)[:3]
    if dia == date.today() and proximos and proximos[0][0]:
        plan.append("Para las próximas 24 h vigilo: " + ", ".join(f"{nombres.get(d, d)} ({n} partidos)" for n, d in proximos if n) + ".")
        plan.append(f"Tengo {leer_estado(con, 'ahorro', 0):.1f} créditos ahorrados para gastarlos cuando valga la pena.")
    if plan:
        secciones.append({"titulo": "Plan", "parrafos": plan})

    con.execute("""INSERT INTO diario (dia, texto, actualizado) VALUES (?, ?, ?)
                   ON CONFLICT(dia) DO UPDATE SET texto = excluded.texto, actualizado = excluded.actualizado""",
                (dia.isoformat(), json.dumps(secciones, ensure_ascii=False), iso(ahora())))
    con.commit()


def actualizar(con, config: dict) -> None:
    """Escribe la entrada de hoy y, al cambiar el día, cierra la de ayer con sus últimos resultados."""
    ayer = date.today() - timedelta(days=1)
    inicio = leer_estado(con, "fecha_inicio")
    if leer_estado(con, "diario_cerrado") != ayer.isoformat():
        if inicio and ayer >= a_fecha(inicio).astimezone().date():
            escribir(con, config, ayer)
        guardar_estado(con, "diario_cerrado", ayer.isoformat())
    escribir(con, config)
