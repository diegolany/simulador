"""Revisión diaria del simulador (la usa Claude en la tarea programada de las 10 a.m.).

Baja una copia de la base de la nube, revisa la salud del bot (corridas de GitHub, créditos, errores), corre un ciclo
completo de prueba SIN gastar créditos (la API de momios queda bloqueada) y resume cómo va contra la meta.
No modifica el código ni la nube. Uso:  python revision/revision_diaria.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROYECTO = Path(__file__).resolve().parent.parent
GIT = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd" / "git.exe"
TRABAJO = Path(tempfile.gettempdir()) / "simulador_revision"
sys.path.insert(0, str(PROYECTO))


def titulo(texto):
    print(f"\n=== {texto} ===")


def git(*args) -> subprocess.CompletedProcess:
    return subprocess.run([str(GIT) if GIT.exists() else "git", *args], cwd=PROYECTO, capture_output=True)


def bajar_base() -> Path:
    TRABAJO.mkdir(exist_ok=True)
    git("fetch", "-q", "origin")
    destino = TRABAJO / "nube.db"
    destino.write_bytes(git("show", "origin/datos:simulador.db").stdout)
    return destino


def corridas_github():
    titulo("CORRIDAS EN GITHUB (últimas 100)")
    try:
        peticion = urllib.request.Request("https://api.github.com/repos/diegolany/simulador/actions/runs?per_page=100",
                                          headers={"User-Agent": "revision-simulador"})
        corridas = json.load(urllib.request.urlopen(peticion, timeout=30))["workflow_runs"]
    except Exception as e:
        print(f"No se pudo consultar: {e!r}")
        return
    fallidas = [c for c in corridas if c["conclusion"] not in ("success", None)]
    duraciones = [(datetime.fromisoformat(c["updated_at"].replace("Z", "+00:00"))
                   - datetime.fromisoformat(c["run_started_at"].replace("Z", "+00:00"))).total_seconds()
                  for c in corridas if c["conclusion"] == "success"]
    print(f"bien: {len(corridas) - len(fallidas)}, fallidas: {len(fallidas)}, última: {corridas[0]['created_at']} "
          f"({corridas[0]['conclusion']}), duración promedio {sum(duraciones) / max(1, len(duraciones)):.0f} s, "
          f"máxima {max(duraciones, default=0):.0f} s")
    for c in fallidas[:5]:
        print(f"  FALLÓ {c['created_at']} {c['event']} {c['html_url']}")


def revisar_codigo():
    titulo("CÓDIGO")
    archivos = [str(p) for p in PROYECTO.glob("*.py")]
    r = subprocess.run([sys.executable, "-m", "pyflakes", *archivos], capture_output=True, text=True)
    if r.returncode in (0, 1) and "No module named" not in r.stderr:
        avisos = [l for l in r.stdout.splitlines() if "f-string is missing placeholders" not in l]
        print("pyflakes:", "sin avisos" if not avisos else "\n  " + "\n  ".join(avisos))
    else:
        import py_compile
        for a in archivos:
            py_compile.compile(a, doraise=True)
        print("compilación: ok (pyflakes no está instalado)")
    print("cambios de las últimas 48 h:")
    print("  " + git("log", "--since=48 hours ago", "--format=%h %ad %s", "--date=short").stdout.decode("utf-8", "replace")
          .strip().replace("\n", "\n  "))


def ciclo_de_prueba(base: Path):
    """Un ciclo completo sobre una copia, con la API de momios bloqueada (ESPN sí se consulta: es gratis)."""
    titulo("CICLO DE PRUEBA (0 créditos)")
    copia = TRABAJO / "prueba.db"
    shutil.copy(base, copia)
    import base_datos
    base_datos.RUTA = copia
    import api, estudio, motor, tablero  # noqa: E401
    api.llamar = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("la prueba no debe gastar créditos"))
    con = base_datos.conectar()
    cfg = api.cargar_config()
    ligas = {r[0]: r[1] for r in con.execute("SELECT deporte, MAX(liga) FROM eventos GROUP BY deporte")}
    motor.deportes_activos = lambda clave: (ligas, base_datos.leer_estado(con, "restantes"))
    motor.proximos_inicios = lambda clave, dep: []
    motor.decidir_descargas = lambda *a, **k: 0
    motor.descargar_resultados = lambda *a, **k: (0, 0)
    motor.ligas_para_cierre = lambda *a, **k: []
    motor.apostar_con_captura = lambda *a, **k: 0
    estudio.estudiar = lambda *a, **k: False
    salida = io.StringIO()
    try:
        with contextlib.redirect_stdout(salida):
            motor.inicializar(con, cfg)
            motor.ciclo(con, cfg)
            motor.analizar_sin_gastar(con, cfg)
            tablero.estado(con, cfg)
            motor.exportar(con, cfg, TRABAJO / "publicar")
        print("ciclo, análisis gratis, tablero y exportación: OK")
    except Exception:
        print("ERROR en el ciclo de prueba:")
        traceback.print_exc(file=sys.stdout)
    fallas = [l for l in salida.getvalue().splitlines() if "falló" in l or "Traceback" in l or "Error" in l]
    print("tareas que fallaron dentro del ciclo:", "ninguna" if not fallas else "\n  " + "\n  ".join(fallas[:15]))
    for archivo in sorted((TRABAJO / "publicar").glob("*.json")):
        print(f"  {archivo.name}: {archivo.stat().st_size / 1024:,.0f} KB")
    con.close()


def metricas(base: Path):
    import base_datos
    base_datos.RUTA = base
    import api, aprendizaje, cerebro, riesgo  # noqa: E401
    con = base_datos.conectar()
    cfg = api.cargar_config()
    leer = lambda c, d=None: base_datos.leer_estado(con, c, d)  # noqa: E731
    ahora = datetime.now(timezone.utc)
    inicio = base_datos.a_fecha(leer("fecha_inicio"))
    dia = (ahora - inicio).total_seconds() / 86400
    inicial = cfg["banca_inicial"]

    titulo("BANCA DE LA PRINCIPAL CONTRA LA META")
    ganancia = con.execute("SELECT COALESCE(SUM(ganancia), 0) FROM apuestas WHERE estrategia = 'Principal' "
                           "AND estado != 'abierta'").fetchone()[0]
    puntos = [(0, 0.0)] + [(7 * (i + 1), o) for i, o in enumerate(cfg["objetivos_semana"])]
    objetivo = next((o0 + (o1 - o0) * (dia - d0) / (d1 - d0) for (d0, o0), (d1, o1) in zip(puntos, puntos[1:]) if dia <= d1),
                    cfg["objetivos_semana"][-1])
    print(f"día {dia:.1f} de 28 · banca ${inicial + ganancia:,.0f} ({ganancia / inicial:+.2%}) · objetivo a hoy "
          f"{objetivo:+.2%} · {'ARRIBA' if ganancia / inicial >= objetivo else 'ABAJO'} de la meta")
    for r in con.execute("""SELECT substr(colocada, 1, 10) d, COUNT(*), ROUND(AVG(monto)), ROUND(SUM(monto))
                            FROM apuestas WHERE estrategia = 'Principal' AND colocada >= ? GROUP BY d""",
                         (base_datos.iso(ahora - timedelta(days=4)),)):
        print(f"  apuestas {r[0]}: {r[1]} (promedio ${r[2]:,.0f}, total ${r[3]:,.0f})")
    p = riesgo.panel(con, cfg)
    c, pr, o = p["confianza"], p["proyeccion"], leer("modo_objetivo", {})
    print(f"CLV {c.get('media', 0):+.2%} en {c.get('n', 0)} medidas · le gana al cierre {c.get('gana_cierre') or 0:.0%} · "
          f"prob. ventaja real {c.get('p_ventaja') or 0:.0%}")
    if pr:
        print(f"prob. de cumplir la meta {pr['prob_meta']:.0%} · de terminar en pérdida {pr['prob_perdida']:.0%} · "
              f"caída máxima {p['caidas']['maxima_pct']:.1%} · modo objetivo ×{o.get('multiplicador', 1)}")

    titulo("CRÉDITOS")
    restantes = leer("restantes")
    print(f"restantes {restantes} · días para repartir {api.dias_restantes(con)} · ahorro {leer('ahorro', 0):.1f}")
    for r in con.execute("""SELECT substr(fecha, 1, 10) d, motivo, SUM(costo), COUNT(*) FROM consumo_api
                            WHERE fecha >= ? GROUP BY d, motivo ORDER BY d""", (base_datos.iso(ahora - timedelta(days=2)),)):
        print(f"  {r[0]} {r[1]}: {r[2]} créditos en {r[3]} consultas")

    titulo("LABORATORIO (ventaja real estimada, ya corregida por suerte)")
    for nombre, v in sorted(riesgo.posteriores_laboratorio(con).items(), key=lambda x: -x[1]["ventaja"])[:8]:
        print(f"  {nombre:<26} n={v['n']:>4} CLV {v['media']:+.2%} → {v['ventaja']:+.2%} (prob. {v['p_ventaja']:.0%})")

    titulo("APUESTAS FANTASMA Y CEREBRO")
    f = aprendizaje.resumen_fantasmas(con, cfg)
    print(f"anotadas {f['total']:,}, medidas {f['medidas']:,}")
    for r in f["por_rango"]:
        print(f"  valor {r['rango']:<12} n={r['n']:>5} CLV {r['clv']:+.2%} gana al cierre {r['gana_cierre']:.0%}")
    m = cerebro.obtener(con, cfg)
    print(f"cerebro: factor {m.factor:.2f} con {m.n_efectivo:.0f} mediciones efectivas")

    titulo("TELEGRAM Y MODO REAL")
    import re as _re
    import avisos
    s = avisos.estadisticas(con, cfg)
    print(_re.sub(r"</?\w+>", "", avisos.texto_resumen(s)))
    desde_avisos = leer("avisos_desde", 0)
    sin_aviso = con.execute("""SELECT COUNT(*) FROM apuestas a JOIN estrategias e ON e.nombre = a.estrategia
                               LEFT JOIN avisos v ON v.apuesta_id = a.id WHERE a.id > ? AND e.rol = 'principal'
                               AND v.apuesta_id IS NULL""", (desde_avisos,)).fetchone()[0]
    print(f"conectado: {s['conectado']} · última lectura de Telegram: {s['ultima_lectura']} · "
          f"apuestas de la Principal sin alerta: {sin_aviso}")
    for c in cfg["casas_mexico"]:
        if c.get("cuenta"):
            ultimo_barrido = con.execute("SELECT MAX(capturado) FROM senales WHERE casa = ?", (c["clave"],)).fetchone()[0]
            enlaces = con.execute("SELECT COUNT(*) FROM enlaces WHERE casa = ?", (c["clave"],)).fetchone()[0]
            print(f"  {c['nombre']}: último barrido {ultimo_barrido or '—'} · partidos con enlace directo {enlaces}")

    titulo("SEÑALES DE ALERTA (últimas 24 h)")
    desde = base_datos.iso(ahora - timedelta(hours=24))
    alertas = []
    # Solo estrategias de valor: el control y los experimentos DraftKings apuestan sin valor a propósito
    for r in con.execute("""SELECT a.estrategia, a.seleccion, a.casa, a.valor, a.clv FROM apuestas a
                            JOIN estrategias e ON e.nombre = a.estrategia WHERE e.tipo = 'valor' AND a.colocada >= ?
                            AND (a.valor > 0.08 OR a.clv < -0.08) AND a.estado != 'anulada'""", (desde,)):
        alertas.append(f"apuesta rara: {r['estrategia']} {r['seleccion']} en {r['casa']} valor {r['valor']:+.1%} "
                       f"CLV {r['clv'] if r['clv'] is None else format(r['clv'], '+.1%')}")
    for r in con.execute("SELECT fecha, tipo, mensaje FROM bitacora WHERE fecha >= ? AND tipo IN ('sistema', 'riesgo')",
                         (desde,)):
        if any(x in r["mensaje"] for x in ("Corrección", "anularon", "Freno", "limitado", "no se usó", "ninguno coincide")):
            alertas.append(f"bitácora {r['fecha'][:16]}: {r['mensaje'][:180]}")
    abiertas_viejas = con.execute("""SELECT COUNT(*) FROM apuestas a JOIN eventos e ON e.id = a.evento_id
                                     WHERE a.estado = 'abierta' AND a.inicio < ? AND e.pospuesto IS NULL""",
                                  (base_datos.iso(ahora - timedelta(hours=12)),)).fetchone()[0]
    if abiertas_viejas:
        alertas.append(f"{abiertas_viejas} apuestas siguen abiertas 12 h después de su partido")
    ultimo = leer("ultimo_ciclo")
    if ultimo and ahora - base_datos.a_fecha(ultimo) > timedelta(minutes=30):
        alertas.append(f"el bot no corre desde {ultimo}")
    if s["fallas"]:
        alertas.append(f"Telegram: {s['fallas']} fallas de envío acumuladas")
    if sin_aviso:
        alertas.append(f"Telegram: {sin_aviso} apuestas de la Principal no se avisaron")
    if s["conectado"] and s["ultima_lectura"] and ahora - base_datos.a_fecha(s["ultima_lectura"]) > timedelta(minutes=45):
        alertas.append(f"Telegram: no se leen respuestas desde {s['ultima_lectura']}")
    print("ninguna" if not alertas else "\n".join("  " + a for a in alertas))
    con.close()


if __name__ == "__main__":
    base = bajar_base()
    corridas_github()
    revisar_codigo()
    metricas(base)
    ciclo_de_prueba(base)
