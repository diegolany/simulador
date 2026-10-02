"""Estudio en tiempos muertos: el bot se pone al día solo, sin gastar créditos de momios.

Cuando football-data.co.uk publica resultados nuevos (o pasó una semana), descarga la base
histórica de fútbol y repite la prueba con temporadas pasadas. El resultado por liga ajusta
la prioridad con la que se gastan los créditos (aprendizaje.valor_ligas).
"""
import urllib.error
import urllib.request
from datetime import timedelta

import backtest_futbol
from base_datos import a_fecha, ahora, anotar, guardar_estado, iso, leer_estado

BASE = "https://www.football-data.co.uk"
TEMPORADAS = ["2021", "2122", "2223", "2324", "2425", "2526", "2627"]
EUROPA = ["E0", "SP1", "D1", "I1", "F1", "N1", "P1"]
AMERICA = ["MEX", "ARG", "BRA", "USA"]


def _pedir(url: str, metodo: str = "GET"):
    peticion = urllib.request.Request(url, method=metodo, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(peticion, timeout=60)


def _marca_remota() -> str | None:
    """Fecha de la última actualización publicada (cambia cuando hay resultados nuevos)."""
    try:
        with _pedir(f"{BASE}/mmz4281/{TEMPORADAS[-1]}/E0.csv", "HEAD") as resp:
            return resp.headers.get("Last-Modified")
    except (urllib.error.URLError, OSError):
        return None


def estudiar(con, config: dict) -> bool:
    """Repite el estudio si hay datos nuevos (máximo cada 2 días) o si pasó el plazo. Devuelve si lo hizo."""
    ultima = leer_estado(con, "estudio_fecha")
    if ultima and ahora() - a_fecha(ultima) < timedelta(days=2):
        return False
    marca = _marca_remota()
    vencido = not ultima or ahora() - a_fecha(ultima) >= timedelta(days=config["estudio"]["dias_entre_estudios"])
    if not vencido and (not marca or marca == leer_estado(con, "estudio_marca")):
        return False

    carpeta = backtest_futbol.CARPETA
    carpeta.mkdir(exist_ok=True)
    archivos = [(f"{BASE}/mmz4281/{t}/{liga}.csv", f"{liga}_{t}.csv") for liga in EUROPA for t in TEMPORADAS]
    archivos += [(f"{BASE}/new/{liga}.csv", f"{liga}.csv") for liga in AMERICA]
    for url, nombre in archivos:
        with _pedir(url) as resp:
            (carpeta / nombre).write_bytes(resp.read())

    anterior = (leer_estado(con, "evidencia") or {}).get("total", {}).get("Mercado (la actual)", {})
    resultado = backtest_futbol.ejecutar()
    guardar_estado(con, "evidencia", resultado)
    guardar_estado(con, "estudio_fecha", iso(ahora()))
    guardar_estado(con, "estudio_marca", marca)
    actual = resultado["total"]["Mercado (la actual)"]
    alta = resultado["total"]["Alta certeza"]
    por_clave = resultado["por_clave"]
    mejores = sorted((r for r in por_clave.items() if r[1].get("n", 0) >= 50),
                     key=lambda r: r[1]["rendimiento"], reverse=True)
    cambio = (f" (antes {anterior['rendimiento']:+.1%})" if anterior.get("rendimiento") is not None else "")
    anotar(con, "aprendizaje",
           f"Estudio con datos históricos actualizados: {resultado['partidos']:,} partidos. La regla de la Principal "
           f"rinde {actual['rendimiento']:+.1%}{cambio} con t = {actual['t']:.1f}; Alta certeza acierta "
           f"{alta['acierto']:.0%} y rinde {alta['rendimiento']:+.1%}. Ligas con más ventaja histórica: "
           + ", ".join(f"{backtest_futbol.LIGAS[_codigo(c)]} {r['rendimiento']:+.1%}" for c, r in mejores[:3])
           + ". La prioridad para gastar créditos se ajustó con estos datos.")
    con.commit()
    return True


def _codigo(clave: str) -> str:
    return next(c for c, k in backtest_futbol.CLAVES.items() if k == clave)
