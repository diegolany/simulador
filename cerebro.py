"""El cerebro del bot: aprende de cada apuesta ya medida cuánto de la ventaja que ve es real y en qué situaciones.

Trabaja como un analista que revisa su historial:
1. Factor de realismo: de cada 1% de valor que el bot calcula al apostar, ¿cuánto confirma el mercado al cierre
   (CLV)? Arranca con lo que mostró la prueba histórica y se ajusta con cada apuesta medida.
   Aprende de las apuestas reales y de las apuestas fantasma (todo precio que vio, aunque no lo haya apostado).
2. Mapa de ventaja: en qué situaciones (liga, casa, tipo de momio, anticipación, eficiencia del mercado, equipo
   popular) el CLV sale
   mejor o peor de lo que el valor predecía. Es una regresión con "encogimiento" (ridge): cada efecto empieza en cero
   y solo se mueve cuando muchas apuestas lo respaldan, así una racha corta no lo engaña.
Con eso estima la ventaja real de cada apuesta nueva: si no es positiva no apuesta, y el monto de Kelly se calcula con
la ventaja estimada, no con la que se ve a simple vista. No gasta créditos.
"""
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from base_datos import leer_estado

DIMENSIONES = {"liga": "Liga", "casa": "Casa", "momio": "Tipo de momio", "anticipacion": "Anticipación",
               "mercado": "Eficiencia del mercado", "popular": "Equipo popular"}
TOPE_EFECTO = 0.04    # un solo rasgo no mueve la estimación más de 4 puntos
TOPE_AJUSTE = 0.05    # la suma de rasgos tampoco más de 5 puntos
# Equipos con mucha afición: las casas comunes a veces los cobran caros (o los regalan) porque reciben más apuestas
POPULARES = ("america", "guadalajara", "chivas", "cruz azul", "pumas", "unam", "monterrey", "tigres", "toluca",
             "real madrid", "barcelona", "atletico madrid", "manchester united", "manchester city", "liverpool",
             "arsenal", "chelsea", "tottenham", "juventus", "milan", "inter", "bayern", "paris saint germain",
             "boca juniors", "river plate", "flamengo", "corinthians", "dallas cowboys", "kansas city chiefs",
             "green bay packers", "pittsburgh steelers", "san francisco 49ers", "new england patriots",
             "philadelphia eagles", "los angeles lakers", "golden state warriors", "boston celtics",
             "new york yankees", "los angeles dodgers", "toronto maple leafs", "montreal canadiens")


def es_popular(nombre: str | None) -> bool:
    if not nombre or nombre == "Draw":
        return False
    texto = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode().lower()
    texto = " " + " ".join(re.findall(r"[a-z0-9]+", texto)) + " "
    return any(f" {p} " in texto for p in POPULARES)


def rasgos(liga: str, casa: str, momio: float, horas: float, margen: float | None, seleccion: str | None = None) -> dict:
    """Cómo se describe una apuesta para comparar su CLV con el de otras parecidas."""
    return {
        "liga": liga,
        "casa": casa,
        "momio": "favorito (menos de 1.80)" if momio < 1.8 else "parejo (1.80 a 3.00)" if momio <= 3 else "no favorito (más de 3.00)",
        "anticipacion": "menos de 6 h" if horas < 6 else "6 a 24 h" if horas <= 24 else "más de 24 h",
        "mercado": None if margen is None else "muy eficiente (margen < 3%)" if margen < 0.03
        else "normal (margen 3% a 5%)" if margen <= 0.05 else "poco eficiente (margen > 5%)",
        "popular": "sí" if es_popular(seleccion) else None,
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


_memoria = {}


def obtener(con, config: dict) -> "Cerebro":
    """El cerebro se arma una vez por cada dato nuevo (no en cada pasada): con miles de mediciones tarda segundos."""
    huella = (tuple(con.execute("SELECT COUNT(*), MAX(id) FROM apuestas WHERE clv IS NOT NULL").fetchone())
              + tuple(con.execute("SELECT COUNT(*), MAX(id) FROM senales WHERE clv IS NOT NULL").fetchone()))
    if huella not in _memoria:
        _memoria.clear()
        _memoria[huella] = Cerebro(con, config)
    return _memoria[huella]


class Cerebro:
    def __init__(self, con, config: dict):
        c = config["cerebro"]
        filas, vistas = [], set()

        def agregar(f, horas):
            clave = (f["evento_id"], f["seleccion"], f["casa"], round(f["momio"], 2))
            if clave in vistas:
                return  # varias estrategias (o la apuesta y su fantasma) con el mismo precio: se midió una sola vez
            vistas.add(clave)
            filas.append({"grupo": (f["evento_id"], f["seleccion"]), "valor": f["valor"], "clv": f["clv"],
                          "rasgos": rasgos(f["liga"], f["casa"], f["momio"], horas, f["margen_ref"], f["seleccion"])})

        for f in con.execute(
                """SELECT a.evento_id, a.seleccion, a.casa, a.momio, a.valor, a.clv, a.liga, a.margen_ref,
                          (julianday(a.inicio) - julianday(a.colocada)) * 24 AS horas
                   FROM apuestas a JOIN estrategias e ON e.nombre = a.estrategia
                   WHERE e.tipo IN ('valor', 'gratis') AND a.clv IS NOT NULL AND a.estado != 'anulada'"""):
            agregar(f, f["horas"])
        # Apuestas fantasma: todo lo que el bot vio, lo haya apostado o no
        for f in con.execute(
                """SELECT evento_id, seleccion, casa, momio, valor, clv, liga, margen_ref,
                          (julianday(inicio) - julianday(capturado)) * 24 AS horas
                   FROM senales WHERE clv IS NOT NULL"""):
            agregar(f, f["horas"])
        # Varias casas del mismo partido y selección se miden contra el mismo cierre: no son independientes, así que
        # entre todas cuentan como una medición
        tamanos = Counter(f["grupo"] for f in filas)
        for f in filas:
            f["peso"] = 1 / tamanos[f["grupo"]]
        self.n = len(filas)
        self.n_efectivo = sum(f["peso"] for f in filas)
        self.previo = factor_previo(con, config)

        # 1. Factor de realismo: el previo pesa como `fuerza_previa` mediciones; las medidas lo van corrigiendo
        con_valor = [f for f in filas if f["valor"] >= 0.005]
        peso_total = sum(f["peso"] for f in con_valor)
        promedio = sum(f["peso"] * f["valor"] for f in con_valor) / peso_total if peso_total else 0.04
        previo = c["fuerza_previa"] * promedio
        self.n_factor = round(peso_total)
        self.factor = max(0.05, min(1.2, (sum(f["peso"] * f["clv"] for f in con_valor) + self.previo * previo)
                                    / (sum(f["peso"] * f["valor"] for f in con_valor) + previo)))

        # 2. Mapa de ventaja: efectos aditivos por rasgo, ajustados uno a la vez hasta estabilizarse (ridge)
        suavizado = c["suavizado"]
        self.efectos = {d: {} for d in DIMENSIONES}
        self.conteos = {d: {} for d in DIMENSIONES}
        pesos = {d: {} for d in DIMENSIONES}
        for f in filas:
            for d in DIMENSIONES:
                nivel = f["rasgos"][d]
                if nivel is not None:
                    self.conteos[d][nivel] = self.conteos[d].get(nivel, 0) + 1
                    pesos[d][nivel] = pesos[d].get(nivel, 0.0) + f["peso"]
        residuos = [f["clv"] - self.factor * f["valor"] for f in filas]
        for _ in range(12 if filas else 0):
            for d in DIMENSIONES:
                sumas = {}
                for f, residuo in zip(filas, residuos):
                    nivel = f["rasgos"][d]
                    if nivel is None:
                        continue
                    otros = sum(self.efectos[o].get(f["rasgos"][o], 0.0) for o in DIMENSIONES if o != d)
                    sumas[nivel] = sumas.get(nivel, 0.0) + f["peso"] * (residuo - otros)
                self.efectos[d] = {nivel: _limitar(s / (pesos[d][nivel] + suavizado), TOPE_EFECTO)
                                   for nivel, s in sumas.items()}

    def estimar(self, valor: float, r: dict) -> tuple[float, list[tuple[str, str, float]]]:
        """(ventaja real estimada, ajustes que la explican) para una apuesta con ese valor y esos rasgos."""
        ajustes = [(DIMENSIONES[d], r[d], e) for d in DIMENSIONES
                   if r.get(d) is not None and abs(e := self.efectos[d].get(r[d], 0.0)) >= 0.001]
        ajustes.sort(key=lambda a: -abs(a[2]))
        return self.factor * valor + _limitar(sum(e for _, _, e in ajustes), TOPE_AJUSTE), ajustes

    def resumen(self) -> dict:
        efectos = [{"dimension": DIMENSIONES[d], "nivel": nivel, "n": self.conteos[d].get(nivel, 0), "efecto": e}
                   for d in DIMENSIONES for nivel, e in self.efectos[d].items()]
        efectos.sort(key=lambda x: -abs(x["efecto"]))
        return {"factor": self.factor, "previo": self.previo, "n": self.n, "n_efectivo": round(self.n_efectivo),
                "n_factor": self.n_factor, "efectos": efectos[:20]}


def explicar(ajustes: list[tuple[str, str, float]], limite: int = 2) -> str:
    return "; ".join(f"{dim.lower()} {nivel}: {e * 100:+.1f} pts" for dim, nivel, e in ajustes[:limite])
