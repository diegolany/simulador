"""Prueba las estrategias con temporadas pasadas de fútbol, sin ver el futuro.

Para cada partido, el modelo se entrena solo con partidos anteriores a esa fecha. Se apuesta
1 unidad al mejor momio disponible y se mide: cuántas apuestas, acierto, rendimiento,
si el rendimiento es estadísticamente distinto de cero (t) y el CLV contra el cierre de Pinnacle.

Ligas europeas: se decide con los momios de apertura y se mide contra el cierre.
Ligas de América (Liga MX, Argentina, Brasil, MLS): los datos solo traen cierre, así que se
decide y se mide al cierre (sin CLV).
"""
import csv
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

from modelo_futbol import ajustar
from momios import probabilidades_justas

CARPETA = Path(__file__).parent / "historico"
LIGAS = {"E0": "Premier League", "SP1": "La Liga", "D1": "Bundesliga", "I1": "Serie A", "F1": "Ligue 1",
         "N1": "Eredivisie", "P1": "Primeira Liga", "MEX": "Liga MX", "ARG": "Argentina", "BRA": "Brasil",
         "USA": "MLS"}
UMBRAL = float(sys.argv[1]) if len(sys.argv) > 1 else 0.02


def _numero(texto):
    try:
        valor = float(texto)
        return valor if valor > 1 else None
    except (TypeError, ValueError):
        return None


def _fecha(texto):
    for formato in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            pass
    return None


def leer(liga: str) -> list[dict]:
    archivos = sorted(CARPETA.glob(f"{liga}_*.csv")) or [CARPETA / f"{liga}.csv"]
    filas = []
    for archivo in archivos:
        with open(archivo, encoding="utf-8-sig", errors="replace") as f:
            for r in csv.DictReader(f):
                local, visitante = r.get("HomeTeam") or r.get("Home"), r.get("AwayTeam") or r.get("Away")
                gl, gv = r.get("FTHG") or r.get("HG"), r.get("FTAG") or r.get("AG")
                fecha = _fecha(r.get("Date") or "")
                if not (local and visitante and fecha and gl not in (None, "") and gv not in (None, "")):
                    continue
                if fecha.year < 2020:
                    continue
                trio = lambda *c: [_numero(r.get(x)) for x in c]
                cierre = trio("PSCH", "PSCD", "PSCA")
                apertura = trio("PSH", "PSD", "PSA")
                tiene_apertura = all(apertura)
                filas.append({
                    "fecha": fecha, "local": local, "visitante": visitante, "gl": int(float(gl)), "gv": int(float(gv)),
                    "mercado": apertura if tiene_apertura else cierre,
                    "precio": trio("MaxH", "MaxD", "MaxA") if tiene_apertura else trio("MaxCH", "MaxCD", "MaxCA"),
                    "cierre": cierre if tiene_apertura else None,
                })
    filas.sort(key=lambda x: x["fecha"])
    return filas


ESTRATEGIAS = {
    "Mercado (la actual)": lambda pm, pd, m: pm * m - 1 >= UMBRAL and 1.30 <= m <= 5,
    "Modelo solo": lambda pm, pd, m: pd * m - 1 >= UMBRAL and 1.30 <= m <= 5,
    "Doble confirmación": lambda pm, pd, m: pm * m - 1 >= UMBRAL and pd * m - 1 >= UMBRAL and 1.30 <= m <= 5,
    "Mezcla 70% mercado / 30% modelo": lambda pm, pd, m: (0.7 * pm + 0.3 * pd) * m - 1 >= UMBRAL and 1.30 <= m <= 5,
    "Alta certeza": lambda pm, pd, m: pm * m - 1 >= UMBRAL and 1.25 <= m <= 1.80,
}


def probar(liga: str) -> tuple[dict, dict]:
    filas = leer(liga)
    historial = [(f["fecha"], f["local"], f["visitante"], f["gl"], f["gv"]) for f in filas]
    apuestas = {nombre: [] for nombre in ESTRATEGIAS}
    perdida_log = {"modelo": 0.0, "mercado": 0.0, "n": 0}
    modelo, ultimo_ajuste = None, None
    inicio_pruebas = filas[0]["fecha"] + timedelta(days=365)
    for f in filas:
        if f["fecha"] < inicio_pruebas or not all(f["mercado"]) or not all(f["precio"]):
            continue
        if ultimo_ajuste is None or (f["fecha"] - ultimo_ajuste).days >= 14:
            modelo = ajustar(historial, f["fecha"], inicial=modelo) or modelo
            ultimo_ajuste = f["fecha"]
        pd_ = modelo.probabilidades(f["local"], f["visitante"]) if modelo else None
        if not pd_:
            continue
        pm = probabilidades_justas(f["mercado"])
        resultado = 0 if f["gl"] > f["gv"] else 1 if f["gl"] == f["gv"] else 2
        perdida_log["modelo"] -= math.log(max(pd_[resultado], 1e-9))
        perdida_log["mercado"] -= math.log(max(pm[resultado], 1e-9))
        perdida_log["n"] += 1
        pc = probabilidades_justas(f["cierre"]) if f["cierre"] and all(f["cierre"]) else None
        for nombre, regla in ESTRATEGIAS.items():
            opciones = [(k, pm[k] * f["precio"][k] - 1) for k in range(3) if regla(pm[k], pd_[k], f["precio"][k])]
            if not opciones:
                continue
            k = max(opciones, key=lambda o: o[1])[0]
            momio = f["precio"][k]
            apuestas[nombre].append({"anio": f["fecha"].year, "gana": k == resultado,
                                     "ganancia": momio - 1 if k == resultado else -1.0,
                                     "clv": momio * pc[k] - 1 if pc else None, "momio": momio})
    return apuestas, perdida_log


def resumen(lista: list[dict]) -> dict:
    n = len(lista)
    if n < 2:
        return {"n": n}
    ganancias = [a["ganancia"] for a in lista]
    media = sum(ganancias) / n
    de = math.sqrt(sum((g - media) ** 2 for g in ganancias) / (n - 1))
    clvs = [a["clv"] for a in lista if a["clv"] is not None]
    return {"n": n, "acierto": sum(a["gana"] for a in lista) / n, "rendimiento": media, "t": media / (de / math.sqrt(n)),
            "clv": sum(clvs) / len(clvs) if clvs else None, "momio": sum(a["momio"] for a in lista) / n}


def main():
    total = {nombre: [] for nombre in ESTRATEGIAS}
    por_liga, calidad = {}, {}
    for liga, nombre_liga in LIGAS.items():
        apuestas, perdida = probar(liga)
        por_liga[nombre_liga] = {e: resumen(l) for e, l in apuestas.items()}
        calidad[nombre_liga] = {"partidos": perdida["n"], "modelo": perdida["modelo"] / max(perdida["n"], 1),
                                "mercado": perdida["mercado"] / max(perdida["n"], 1)}
        for e, l in apuestas.items():
            total[e] += l
        print(f"  {nombre_liga}: {perdida['n']} partidos probados", flush=True)

    print(f"\n=== TODAS LAS LIGAS (umbral de valor {UMBRAL:.0%}, 1 unidad por apuesta) ===")
    print(f"{'Estrategia':<34}{'Apuestas':>9}{'Acierto':>9}{'Rend.':>8}{'t':>7}{'CLV':>8}{'Momio':>7}")
    resultados = {}
    for e, l in total.items():
        r = resumen(l)
        resultados[e] = r
        if r["n"] < 2:
            continue
        clv = f"{r['clv']:+.1%}" if r["clv"] is not None else "—"
        print(f"{e:<34}{r['n']:>9}{r['acierto']:>9.1%}{r['rendimiento']:>+8.1%}{r['t']:>7.1f}{clv:>8}{r['momio']:>7.2f}")
        por_anio = {}
        for a in l:
            por_anio.setdefault(a["anio"], []).append(a["ganancia"])
        print("    por año: " + "  ".join(f"{anio} {sum(g) / len(g):+.1%} ({len(g)})" for anio, g in sorted(por_anio.items())))

    print("\n=== POR LIGA: rendimiento (apuestas) ===")
    for liga, datos in por_liga.items():
        celdas = "  ".join(f"{e.split(' ')[0]} {r['rendimiento']:+.1%}({r['n']})" if r.get("n", 0) >= 2 else f"{e.split(' ')[0]} —"
                           for e, r in datos.items())
        print(f"  {liga:<15} {celdas}")

    print("\n=== PRECISIÓN: pérdida logarítmica (menor = mejor) ===")
    for liga, c in calidad.items():
        print(f"  {liga:<15} modelo {c['modelo']:.4f}   mercado {c['mercado']:.4f}   ({c['partidos']} partidos)")

    (Path(__file__).parent / "backtest_futbol.json").write_text(
        json.dumps({"umbral": UMBRAL, "total": resultados, "por_liga": por_liga, "calidad": calidad}, ensure_ascii=False, indent=1),
        encoding="utf-8")


if __name__ == "__main__":
    main()
