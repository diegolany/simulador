"""El cerebro del bot: aprende de cada apuesta ya medida cuánto de la ventaja que ve es real y en qué situaciones.

Trabaja como un analista que revisa su historial:
1. Factor de realismo: de cada 1% de valor que el bot calcula al apostar, ¿cuánto confirma el mercado al cierre
   (CLV)? Arranca con lo que mostró la prueba histórica y se ajusta con cada apuesta medida.
2. Mapa de ventaja: en qué situaciones (liga, casa, tipo de momio, anticipación, eficiencia del mercado) el CLV sale
   mejor o peor de lo que el valor predecía. Es una regresión con "encogimiento" (ridge): cada efecto empieza en cero
   y solo se mueve cuando muchas apuestas lo respaldan, así una racha corta no lo engaña.
Con eso estima la ventaja real de cada apuesta nueva: si no es positiva no apuesta, y el monto de Kelly se calcula con
la ventaja estimada, no con la que se ve a simple vista. No gasta créditos.
"""
import json
from pathlib import Path

from base_datos import leer_estado

DIMENSIONES = {"liga": "Liga", "casa": "Casa", "momio": "Tipo de momio", "anticipacion": "Anticipación",
               "mercado": "Eficiencia del mercado"}
TOPE_EFECTO = 0.04    # un solo rasgo no mueve la estimación más de 4 puntos
TOPE_AJUSTE = 0.05    # la suma de rasgos tampoco más de 5 puntos


def rasgos(liga: str, casa: str, momio: float, horas: float, margen: float | None) -> dict:
    """Cómo se describe una apuesta para comparar su CLV con el de otras parecidas."""
    return {
        "liga": liga,
        "casa": casa,
        "momio": "favorito (menos de 1.80)" if momio < 1.8 else "parejo (1.80 a 3.00)" if momio <= 3 else "no favorito (más de 3.00)",
        "anticipacion": "menos de 6 h" if horas < 6 else "6 a 24 h" if horas <= 24 else "más de 24 h",
        "mercado": None if margen is None else "muy eficiente (margen < 3%)" if margen < 0.03
        else "normal (margen 3% a 5%)" if margen <= 0.05 else "poco eficiente (margen > 5%)",
    }


def factor_previo(con, config: dict) -> float:
    """CLV / valor de la regla actual en la prueba histórica, con un tope por prudencia (el pasado no es igual al vivo)."""
    c = config["cerebro"]
    datos = leer_estado(con, "evidencia")
    if not datos or "version" not in datos:
        archivo = Path(__file__).with_name("backtest_futbol.json")
        datos = json.loads(archivo.read_text(encoding="utf-8")) if archivo.exists() else {}
    r = (datos.get("total") or {}).get("Mercado (la actual)") or {}
    if r.get("clv") and r.get("valor"):
        return max(0.2, min(c["factor_previo_max"], r["clv"] / r["valor"]))
    return c["factor_previo"]


def _limitar(x: float, tope: float) -> float:
    return max(-tope, min(tope, x))


class Cerebro:
    def __init__(self, con, config: dict):
        c = config["cerebro"]
        filas, vistas = [], set()
        for f in con.execute(
                """SELECT a.evento_id, a.seleccion, a.casa, a.momio, a.valor, a.clv, a.liga, a.margen_ref,
                          (julianday(a.inicio) - julianday(a.colocada)) * 24 AS horas
                   FROM apuestas a JOIN estrategias e ON e.nombre = a.estrategia
                   WHERE e.tipo IN ('valor', 'gratis') AND a.clv IS NOT NULL AND a.estado != 'anulada'
                   ORDER BY a.colocada"""):
            clave = (f["evento_id"], f["seleccion"], f["casa"], round(f["momio"], 2))
            if clave in vistas:
                continue  # varias estrategias tomaron el mismo precio: el mercado lo midió una sola vez
            vistas.add(clave)
            filas.append({"valor": f["valor"], "clv": f["clv"],
                          "rasgos": rasgos(f["liga"], f["casa"], f["momio"], f["horas"], f["margen_ref"])})
        self.n = len(filas)
        self.previo = factor_previo(con, config)

        # 1. Factor de realismo: el previo pesa como `fuerza_previa` apuestas; las medidas lo van corrigiendo
        con_valor = [f for f in filas if f["valor"] >= 0.005]
        promedio = sum(f["valor"] for f in con_valor) / len(con_valor) if con_valor else 0.04
        peso = c["fuerza_previa"] * promedio
        self.n_factor = len(con_valor)
        self.factor = max(0.05, min(1.2, (sum(f["clv"] for f in con_valor) + self.previo * peso)
                                    / (sum(f["valor"] for f in con_valor) + peso)))

        # 2. Mapa de ventaja: efectos aditivos por rasgo, ajustados uno a la vez hasta estabilizarse (ridge)
        suavizado = c["suavizado"]
        self.efectos = {d: {} for d in DIMENSIONES}
        self.conteos = {d: {} for d in DIMENSIONES}
        for f in filas:
            for d in DIMENSIONES:
                nivel = f["rasgos"][d]
                if nivel is not None:
                    self.conteos[d][nivel] = self.conteos[d].get(nivel, 0) + 1
        for _ in range(12 if filas else 0):
            for d in DIMENSIONES:
                sumas = {}
                for f in filas:
                    nivel = f["rasgos"][d]
                    if nivel is None:
                        continue
                    otros = sum(self.efectos[o].get(f["rasgos"][o], 0.0) for o in DIMENSIONES if o != d)
                    sumas[nivel] = sumas.get(nivel, 0.0) + f["clv"] - self.factor * f["valor"] - otros
                self.efectos[d] = {nivel: _limitar(s / (self.conteos[d][nivel] + suavizado), TOPE_EFECTO)
                                   for nivel, s in sumas.items()}

    def estimar(self, valor: float, r: dict) -> tuple[float, list[tuple[str, str, float]]]:
        """(ventaja real estimada, ajustes que la explican) para una apuesta con ese valor y esos rasgos."""
        ajustes = [(DIMENSIONES[d], r[d], e) for d in DIMENSIONES
                   if r[d] is not None and abs(e := self.efectos[d].get(r[d], 0.0)) >= 0.001]
        ajustes.sort(key=lambda a: -abs(a[2]))
        return self.factor * valor + _limitar(sum(e for _, _, e in ajustes), TOPE_AJUSTE), ajustes

    def resumen(self) -> dict:
        efectos = [{"dimension": DIMENSIONES[d], "nivel": nivel, "n": self.conteos[d].get(nivel, 0), "efecto": e}
                   for d in DIMENSIONES for nivel, e in self.efectos[d].items()]
        efectos.sort(key=lambda x: -abs(x["efecto"]))
        return {"factor": self.factor, "previo": self.previo, "n": self.n, "n_factor": self.n_factor,
                "efectos": efectos[:20]}


def explicar(ajustes: list[tuple[str, str, float]], limite: int = 2) -> str:
    return "; ".join(f"{dim.lower()} {nivel}: {e * 100:+.1f} pts" for dim, nivel, e in ajustes[:limite])
