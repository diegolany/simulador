"""Matemáticas de momios: conversiones, margen de la casa, valor esperado y tamaño de apuesta.

Internamente todo se maneja en momio DECIMAL: lo que te regresan por cada $1 apostado,
incluyendo tu $1. Un decimal de 2.50 equivale a +150 americano.
"""


def americano_a_decimal(americano: float) -> float:
    """+150 -> 2.50 ; -120 -> 1.833"""
    if americano >= 100:
        return 1 + americano / 100
    if americano <= -100:
        return 1 + 100 / -americano
    raise ValueError(f"Momio americano inválido: {americano}")


def decimal_a_americano(decimal: float) -> int:
    """2.50 -> +150 ; 1.833 -> -120"""
    if decimal <= 1:
        raise ValueError(f"Momio decimal inválido: {decimal}")
    if decimal >= 2:
        return round((decimal - 1) * 100)
    return round(-100 / (decimal - 1))


def probabilidad_implicita(decimal: float) -> float:
    """Lo que el momio dice que va a pasar, todavía con el margen de la casa incluido."""
    return 1 / decimal


def margen_casa(decimales: list[float]) -> float:
    """Comisión de la casa en un mercado completo. -110 / -110 -> 0.0476 (4.76%)."""
    return sum(1 / d for d in decimales) - 1


def probabilidades_justas(decimales: list[float]) -> list[float]:
    """Quita el margen de la casa y devuelve lo que el mercado realmente cree.

    Método de potencia: las casas cargan más margen a los no favoritos, así que en
    lugar de repartir el margen parejo se busca el exponente k con el que la suma
    de (1/momio)^k da exactamente 1.
    """
    implicitas = [1 / d for d in decimales]
    bajo, alto = 0.01, 50.0
    for _ in range(100):
        k = (bajo + alto) / 2
        if sum(q ** k for q in implicitas) > 1:
            bajo = k
        else:
            alto = k
    k = (bajo + alto) / 2
    return [q ** k for q in implicitas]


def valor_esperado(probabilidad: float, decimal: float) -> float:
    """Ganancia promedio por cada $1 apostado. 50% a momio 2.20 -> +0.10 (+10%)."""
    return probabilidad * decimal - 1


def fraccion_kelly(probabilidad: float, decimal: float, fraccion: float = 0.25, tope: float = 0.02) -> float:
    """Parte de la banca a apostar. Kelly completo es demasiado agresivo para la varianza
    real, así que se usa una fracción (1/4) y un tope (2% de la banca)."""
    b = decimal - 1
    kelly = (b * probabilidad - (1 - probabilidad)) / b
    return max(0.0, min(kelly * fraccion, tope))


def valor_al_cierre(momio_apostado: float, probabilidad_justa_cierre: float) -> float:
    """CLV: qué tan bueno fue tu momio comparado con la probabilidad justa al cierre.
    Positivo = le ganaste al mercado, sin importar si la apuesta se ganó o se perdió."""
    return momio_apostado * probabilidad_justa_cierre - 1


if __name__ == "__main__":
    print("== Conversiones ==")
    for am in (+150, -120, -110):
        d = americano_a_decimal(am)
        print(f"  {am:+d} americano = {d:.3f} decimal = {probabilidad_implicita(d):.1%} implícito")

    print("\n== Margen de la casa en un partido parejo (-110 / -110) ==")
    d = americano_a_decimal(-110)
    print(f"  Suma de probabilidades: {2 / d:.1%}  ->  margen {margen_casa([d, d]):.2%}")

    print("\n== % de acierto vs valor ==")
    print(f"  60% de acierto a momio 1.50: {valor_esperado(0.60, 1.50):+.0%} por apuesta")
    print(f"  40% de acierto a momio 3.00: {valor_esperado(0.40, 3.00):+.0%} por apuesta")

    print("\n== Partido de fútbol: Pinnacle (referencia) vs otra casa ==")
    pinnacle = {"Local": 2.10, "Empate": 3.40, "Visitante": 3.60}
    justas = dict(zip(pinnacle, probabilidades_justas(list(pinnacle.values()))))
    print(f"  Margen de Pinnacle: {margen_casa(list(pinnacle.values())):.2%}")
    for sel, p in justas.items():
        print(f"  {sel:<10} momio {pinnacle[sel]:.2f} -> probabilidad justa {p:.1%}")
    otra = 2.25
    ev = valor_esperado(justas["Local"], otra)
    f = fraccion_kelly(justas["Local"], otra)
    print(f"  Otra casa paga al Local {otra:.2f} ({decimal_a_americano(otra):+d}): valor esperado {ev:+.1%}")
    print(f"  Apuesta sugerida (1/4 Kelly, tope 2%): {f:.2%} de la banca = ${f * 100000:,.0f} de $100,000")
