"""Modelo estadístico propio para fútbol: Dixon-Coles con pesos por antigüedad.

Cada equipo tiene una fuerza de ataque (goles que mete contra una defensa promedio) y
una de defensa (cuánto deja meter, 1 = promedio), más una ventaja de local. Los goles
de cada lado siguen una distribución de Poisson, con la corrección de Dixon-Coles para
marcadores bajos (0-0, 1-0, 0-1, 1-1). Los partidos recientes pesan más.
"""
import math

XI = 0.0019     # el peso de un partido baja a la mitad en ~1 año
PREVIA = 2.0    # partidos promedio ficticios que suavizan a equipos con pocos datos (ascendidos)


def _tau(i: int, j: int, lam: float, mu: float, rho: float) -> float:
    if i == 0 and j == 0:
        return 1 - lam * mu * rho
    if i == 0 and j == 1:
        return 1 + lam * rho
    if i == 1 and j == 0:
        return 1 + mu * rho
    if i == 1 and j == 1:
        return 1 - rho
    return 1.0


class Modelo:
    def __init__(self, ataque: dict, defensa: dict, casa: float, rho: float, partidos: dict):
        self.ataque, self.defensa, self.casa, self.rho, self.partidos = ataque, defensa, casa, rho, partidos

    def probabilidades(self, local: str, visitante: str, minimo_partidos: float = 5,
                       max_goles: int = 10) -> tuple[float, float, float] | None:
        """(gana local, empate, gana visitante), o None si algún equipo tiene pocos datos."""
        if min(self.partidos.get(local, 0), self.partidos.get(visitante, 0)) < minimo_partidos:
            return None
        lam = self.ataque[local] * self.defensa[visitante] * self.casa
        mu = self.ataque[visitante] * self.defensa[local]
        p_l = [math.exp(-lam)]
        p_v = [math.exp(-mu)]
        for k in range(1, max_goles + 1):
            p_l.append(p_l[-1] * lam / k)
            p_v.append(p_v[-1] * mu / k)
        local_gana = empate = visitante_gana = 0.0
        for i in range(max_goles + 1):
            for j in range(max_goles + 1):
                p = p_l[i] * p_v[j] * (_tau(i, j, lam, mu, self.rho) if i < 2 and j < 2 else 1)
                if i > j:
                    local_gana += p
                elif i == j:
                    empate += p
                else:
                    visitante_gana += p
        total = local_gana + empate + visitante_gana
        return local_gana / total, empate / total, visitante_gana / total


def ajustar(partidos: list[tuple], corte, ventana: int = 730, inicial: Modelo | None = None,
            iteraciones: int = 40) -> Modelo | None:
    """partidos: [(fecha, local, visitante, goles_local, goles_visitante)]. Usa solo los
    anteriores a `corte` dentro de la ventana (días)."""
    datos = [(l, v, gl, gv, math.exp(-XI * (corte - f).days))
             for f, l, v, gl, gv in partidos if 0 < (corte - f).days <= ventana]
    if len(datos) < 60:
        return None
    equipos = sorted({d[0] for d in datos} | {d[1] for d in datos})
    idx = {e: i for i, e in enumerate(equipos)}
    n = len(equipos)
    L = [idx[d[0]] for d in datos]
    V = [idx[d[1]] for d in datos]
    GL = [d[2] for d in datos]
    GV = [d[3] for d in datos]
    W = [d[4] for d in datos]
    marcados, recibidos, jugados = [0.0] * n, [0.0] * n, [0.0] * n
    for l, v, gl, gv, w in zip(L, V, GL, GV, W):
        marcados[l] += w * gl
        marcados[v] += w * gv
        recibidos[l] += w * gv
        recibidos[v] += w * gl
        jugados[l] += w
        jugados[v] += w
    media = sum(marcados) / sum(jugados)

    ataque = [inicial.ataque.get(e, media) if inicial else media for e in equipos]
    defensa = [inicial.defensa.get(e, 1.0) if inicial else 1.0 for e in equipos]
    casa = inicial.casa if inicial else 1.25
    for _ in range(iteraciones):
        den = [0.0] * n
        for l, v, w in zip(L, V, W):
            den[l] += w * defensa[v] * casa
            den[v] += w * defensa[l]
        ataque = [(marcados[i] + PREVIA * media) / (den[i] + PREVIA) for i in range(n)]
        den = [0.0] * n
        for l, v, w in zip(L, V, W):
            den[l] += w * ataque[v]
            den[v] += w * ataque[l] * casa
        defensa = [(recibidos[i] + PREVIA * media) / (den[i] + PREVIA * media) for i in range(n)]
        escala = sum(defensa) / n
        defensa = [d / escala for d in defensa]
        ataque = [a * escala for a in ataque]
        casa = (sum(w * gl for gl, w in zip(GL, W))
                / sum(w * ataque[l] * defensa[v] for l, v, w in zip(L, V, W)))

    # Corrección de marcadores bajos: el rho que mejor explica los 0-0, 1-0, 0-1 y 1-1
    bajos = [(ataque[l] * defensa[v] * casa, ataque[v] * defensa[l], gl, gv, w)
             for l, v, gl, gv, w in zip(L, V, GL, GV, W) if gl < 2 and gv < 2]
    mejor_rho, mejor = 0.0, -math.inf
    for paso in range(-25, 11):
        rho = paso / 100
        total = 0.0
        for lam, mu, gl, gv, w in bajos:
            t = _tau(gl, gv, lam, mu, rho)
            if t <= 0:
                total = -math.inf
                break
            total += w * math.log(t)
        if total > mejor:
            mejor_rho, mejor = rho, total
    return Modelo(dict(zip(equipos, ataque)), dict(zip(equipos, defensa)), casa, mejor_rho,
                  dict(zip(equipos, jugados)))
