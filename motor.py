"""Motor del simulador: corre solo mientras la ventana esté abierta y sirve el tablero web.

Cada ciclo (cada 10 minutos):
  1. Pide resultados de los deportes con partidos terminados y liquida apuestas.
  2. Mide el CLV de los partidos que ya empezaron.
  3. Toma la foto de cierre de las ligas con apuestas que empiezan pronto.
  4. En los horarios de barrido, busca momios nuevos y deja que cada estrategia apueste.
  5. Una vez por semana corre la revisión de aprendizaje.
"""
import argparse
import hashlib
import json
import sys
import threading
import traceback
import urllib.error
import webbrowser
from calendar import monthrange
from datetime import date, datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aprendizaje
import estrategias
import estudio
import marcadores
import tablero
from api import (CARPETA, ErrorAPI, cargar_config, creditos_hoy, deportes_activos, descargar_momios,
                 descargar_resultados, proximos_inicios)
from base_datos import a_fecha, ahora, anotar, conectar, guardar_estado, iso, leer_estado, podar

PUERTO = 8765
URL = f"http://127.0.0.1:{PUERTO}"
buscar_ahora = threading.Event()


def log(mensaje: str) -> None:
    print(f"[{datetime.now():%d/%m %H:%M}] {mensaje}", flush=True)


def inicializar(con, config: dict) -> None:
    estrategias.sembrar(con)
    if not leer_estado(con, "fecha_inicio"):
        guardar_estado(con, "fecha_inicio", iso(ahora()))
        guardar_estado(con, "ultima_revision", iso(ahora()))
        anotar(con, "inicio", f"Arranca la simulación con banca ficticia de "
                              f"${config['banca_inicial']:,.0f} {config['moneda']}.")
    con.commit()


def _duracion(config: dict, deporte: str) -> timedelta:
    return timedelta(hours=config["duracion_horas"].get(deporte.split("_")[0], 4))


def deportes_por_liquidar(con, config: dict) -> list[str]:
    """Deportes con partidos que ya debieron terminar y que ESPN no resolvió (respaldo con créditos).
    Se agrupan para gastar menos: se piden cuando el más viejo lleva horas esperando o ya son varios."""
    momento = ahora()
    pendientes = {}
    for f in con.execute("""SELECT DISTINCT a.evento_id, a.deporte, a.inicio FROM apuestas a
                            JOIN eventos e ON e.id = a.evento_id
                            WHERE a.estado = 'abierta' AND e.terminado = 0"""):
        fin = a_fecha(f["inicio"]) + _duracion(config, f["deporte"])
        if marcadores.cubierto(f["deporte"]) and momento - fin < timedelta(hours=6):
            continue  # se le da tiempo a ESPN antes de gastar créditos
        if fin < momento:
            pendientes.setdefault(f["deporte"], []).append(fin)
    elegidos = []
    for deporte, fines in pendientes.items():
        ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'scores' AND deporte = ?",
                             (deporte,)).fetchone()[0]
        if ultima and momento - a_fecha(ultima) < timedelta(hours=config["horas_min_entre_resultados"]):
            continue
        espera = momento - min(fines)
        if espera >= timedelta(hours=config["horas_espera_resultados"]) or len(fines) >= config["lote_resultados"]:
            elegidos.append((min(fines), deporte))
    return [d for _, d in sorted(elegidos)]


def ligas_para_cierre(con, config: dict) -> list[str]:
    """Ligas con apuestas abiertas que empiezan pronto y sin foto reciente: esa foto es el 'cierre'."""
    momento = ahora()
    limite = momento + timedelta(minutes=config["minutos_captura_cierre"])
    deportes = [f[0] for f in con.execute(
        "SELECT DISTINCT deporte FROM apuestas WHERE estado = 'abierta' AND inicio > ? AND inicio <= ?",
        (iso(momento), iso(limite)))]
    elegidos = []
    for deporte in deportes:
        ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'odds' AND deporte = ?",
                             (deporte,)).fetchone()[0]
        if not ultima or momento - a_fecha(ultima) >= timedelta(minutes=50):
            elegidos.append(deporte)
    return elegidos


def calendario(con, config: dict, activos: dict) -> dict:
    """{liga: [inicios]} de los partidos de las próximas horas. Se refresca gratis cada pocas horas."""
    actualizado = leer_estado(con, "calendario_actualizado")
    if actualizado and ahora() - a_fecha(actualizado) < timedelta(hours=config["decision"]["horas_calendario"]):
        return leer_estado(con, "calendario", {})
    limite = ahora() + timedelta(hours=config["horas_ventana"])
    cal = {}
    for deporte in config["deportes_candidatos"]:
        if deporte in activos:
            inicios = sorted(t for t in proximos_inicios(config["api_key"], deporte) if ahora() < t <= limite)
            if inicios:
                cal[deporte] = [iso(t) for t in inicios]
    guardar_estado(con, "calendario", cal)
    guardar_estado(con, "calendario_actualizado", iso(ahora()))
    return cal


def _ultima_descarga(con, deporte: str):
    ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'odds' AND deporte = ?",
                         (deporte,)).fetchone()[0]
    return a_fecha(ultima) if ultima else None


def puntajes_descarga(con, config: dict, cal: dict) -> dict:
    """Qué tanto conviene gastar un crédito en cada liga ahora mismo:
    urgencia (partidos que empiezan pronto) x antigüedad de sus momios x valor histórico de la liga."""
    momento = ahora()
    valores = aprendizaje.valor_ligas(con, config, list(cal))
    puntajes = {}
    for deporte, inicios in cal.items():
        urgencia = 0.0
        for texto in inicios:
            horas = (a_fecha(texto) - momento).total_seconds() / 3600
            if horas > 0.17:
                urgencia += 1.0 if horas <= 3 else 0.6 if horas <= 12 else 0.3 if horas <= 48 else 0.1
        ultima = _ultima_descarga(con, deporte)
        horas_desde = (momento - ultima).total_seconds() / 3600 if ultima else 99
        if urgencia == 0 or horas_desde < config["horas_min_entre_descargas"]:
            continue
        puntajes[deporte] = urgencia * min(1.0, horas_desde / 4) * valores[deporte]
    return puntajes


def decidir_descargas(con, config: dict, activos: dict, cal: dict, restantes: int) -> int:
    """Cada ciclo decide si vale la pena gastar créditos ahora o guardarlos para un mejor momento.
    Los créditos se acumulan en un ahorro que se llena a ritmo constante (lo que alcanza para el mes)
    y se gasta cuando aparece una liga con partidos próximos, momios viejos y buen historial."""
    d = config["decision"]
    momento = ahora()
    hoy = date.today()
    dias_restantes = monthrange(hoy.year, hoy.month)[1] - hoy.day + 1
    por_hora = max(0.0, (restantes - config["reserva_creditos"]) / dias_restantes * d["porcion_busqueda"] / 24)
    ahorro = leer_estado(con, "ahorro", d["capacidad_ahorro"] / 2)
    ultima = leer_estado(con, "ahorro_actualizado")
    if ultima:
        ahorro += por_hora * (momento - a_fecha(ultima)).total_seconds() / 3600
    ahorro = min(d["capacidad_ahorro"], ahorro)
    costo_liga = len(config["mercados"]) * len(config["region"].split(","))
    puntajes = puntajes_descarga(con, config, cal)
    gastado = 0
    while puntajes and ahorro >= costo_liga and creditos_hoy(con, restantes - gastado, config["reserva_creditos"]) >= costo_liga:
        deporte, puntaje = max(puntajes.items(), key=lambda x: x[1])
        if puntaje < d["puntaje_minimo"]:
            break
        log(f"Decisión: vale la pena ahora {activos.get(deporte, deporte)} (puntaje {puntaje:.1f}, ahorro {ahorro:.1f})")
        costo = apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "barrido")
        gastado += costo
        ahorro -= costo
        del puntajes[deporte]
    guardar_estado(con, "ahorro", round(ahorro, 3))
    guardar_estado(con, "ahorro_actualizado", iso(momento))
    return gastado


def apostar_con_captura(con, config: dict, deporte: str, liga: str, motivo: str) -> int:
    capturado, id_consumo, costo = descargar_momios(con, config, deporte, liga, motivo)
    colocadas, senales = estrategias.colocar_apuestas(con, config, capturado)
    con.execute("UPDATE consumo_api SET senales = ? WHERE id = ?", (senales, id_consumo))
    con.commit()
    detalle = ", ".join(f"{n} {e}" for e, n in colocadas.items()) or "sin apuestas"
    log(f"  {liga} ({motivo}): {senales} partidos con valor; apuestas: {detalle}")
    return costo


def barrido_manual(con, config: dict, activos: dict, cal: dict, presupuesto: int) -> int:
    """Búsqueda pedida a mano: descarga las ligas mejor puntuadas hasta agotar el presupuesto."""
    puntajes = puntajes_descarga(con, config, cal)
    log(f"Búsqueda manual: {len(puntajes)} ligas disponibles, presupuesto {presupuesto} créditos")
    gastado = 0
    costo_liga = len(config["mercados"]) * len(config["region"].split(","))
    for deporte in sorted(puntajes, key=puntajes.get, reverse=True):
        if presupuesto - gastado < costo_liga:
            break
        gastado += apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "barrido")
    return gastado


def ciclo(con, config: dict, forzar_barrido: bool = False) -> None:
    activos, restantes = deportes_activos(config["api_key"])  # gratis
    guardar_estado(con, "restantes", restantes)

    if not leer_estado(con, "correccion_3_vias"):
        estrategias.anular_mercados_distintos(con, config["casa_referencia"])
        guardar_estado(con, "correccion_3_vias", True)

    vivos, terminados = marcadores.actualizar(con)  # gratis
    if vivos or terminados:
        log(f"Marcadores ESPN: {vivos} partidos en juego, {terminados} terminados")
    for deporte in deportes_por_liquidar(con, config):
        if creditos_hoy(con, restantes, config["reserva_creditos"]) < 2:
            break
        nuevos, costo = descargar_resultados(con, config, deporte)
        restantes -= costo
        log(f"Resultados {activos.get(deporte, deporte)}: {nuevos} partidos terminados")
    liquidadas = estrategias.liquidar(con)
    if liquidadas:
        log(f"Se liquidaron {liquidadas} apuestas")
    estrategias.calcular_clv(con, config)

    for deporte in ligas_para_cierre(con, config):
        if creditos_hoy(con, restantes, config["reserva_creditos"]) < 1:
            break
        restantes -= apostar_con_captura(con, config, deporte, activos.get(deporte, deporte), "cierre")

    cal = calendario(con, config, activos)  # gratis
    if forzar_barrido:
        disponibles = creditos_hoy(con, restantes, config["reserva_creditos"])
        restantes -= barrido_manual(con, config, activos, cal, max(disponibles // 2, min(disponibles, 1)))
    else:
        restantes -= decidir_descargas(con, config, activos, cal, restantes)
    # Gratis: con los momios ya descargados, apuestas que ahora sí entran en la ventana de alguna estrategia
    estrategias.reanalizar(con, config, config["minutos_reanalisis"], silencioso=True)

    if aprendizaje.toca_revision(con, config):
        aprendizaje.revision(con, config)
    aprendizaje.aprendizaje_diario(con)
    try:  # en tiempos muertos: ponerse al día con datos históricos nuevos (gratis)
        if estudio.estudiar(con, config):
            log("Estudio de datos históricos actualizado")
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        log(f"Estudio pospuesto para el siguiente ciclo: {e}")

    if leer_estado(con, "ultima_poda") != date.today().isoformat():
        guardar_estado(con, "ultima_poda", date.today().isoformat())
        podar(con, config["casa_referencia"])

    guardar_estado(con, "restantes", restantes)
    guardar_estado(con, "ultimo_ciclo", iso(ahora()))
    con.commit()


def analizar_sin_gastar(con, config: dict) -> None:
    """Marcadores, liquidación, CLV y apuestas nuevas con los momios ya descargados: 0 créditos."""
    marcadores.actualizar(con)
    estrategias.liquidar(con)
    estrategias.calcular_clv(con, config)
    capturas, nuevas = estrategias.reanalizar(con, config, config["minutos_reanalisis"])
    log(f"Análisis sin gastar: {capturas} descargas revisadas, {nuevas} apuestas nuevas")
    guardar_estado(con, "ultimo_ciclo", iso(ahora()))
    con.commit()


def exportar(con, config: dict, carpeta: Path) -> None:
    """Tablero estático (página + estado.json) para publicarlo en internet."""
    carpeta.mkdir(parents=True, exist_ok=True)
    pagina = (CARPETA / "web" / "index.html").read_text(encoding="utf-8")
    version = hashlib.sha1(pagina.encode("utf-8")).hexdigest()[:10]  # la página se recarga sola si cambia
    datos = {**tablero.estado(con, config), "version": version}
    (carpeta / "estado.json").write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
    (carpeta / "index.html").write_text(pagina.replace("__VERSION__", version), encoding="utf-8")


def bucle_motor() -> None:
    con = conectar()
    while True:
        forzar = buscar_ahora.is_set()
        buscar_ahora.clear()
        config = cargar_config()
        try:
            ciclo(con, config, forzar)
        except ErrorAPI as e:
            log(f"La API respondió con error {e}")
        except urllib.error.URLError as e:
            log(f"Sin conexión a internet: {e.reason}")
        except Exception:
            traceback.print_exc()
        buscar_ahora.wait(timeout=config["minutos_entre_ciclos"] * 60)


class Tablero(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(CARPETA / "web"), **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/estado"):
            con = conectar()
            try:
                self._json(tablero.estado(con, cargar_config()))
            finally:
                con.close()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/buscar":
            buscar_ahora.set()
            self._json({"ok": True})
        else:
            self.send_error(404)

    def _json(self, datos) -> None:
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, *args):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulador de apuestas deportivas")
    parser.add_argument("--un-ciclo", action="store_true", help="corre un ciclo con barrido y termina")
    parser.add_argument("--ciclo", action="store_true", help="corre un ciclo normal y termina (modo nube)")
    parser.add_argument("--analizar", action="store_true", help="evalúa los momios recientes sin gastar créditos")
    parser.add_argument("--exportar", metavar="CARPETA", help="escribe el tablero estático en esa carpeta")
    parser.add_argument("--sin-motor", action="store_true", help="solo muestra el tablero, sin gastar créditos")
    parser.add_argument("--sin-navegador", action="store_true", help="no abre el navegador")
    args = parser.parse_args()

    config = cargar_config()
    if not args.sin_motor and not config["api_key"].strip():
        print("Falta la clave de The Odds API. El bot ya corre en la nube: https://diegolany.github.io/simulador/")
        return 1
    con = conectar()
    inicializar(con, config)
    if args.un_ciclo or args.ciclo or args.analizar:
        try:
            if args.analizar:
                analizar_sin_gastar(con, config)
            else:
                ciclo(con, config, forzar_barrido=args.un_ciclo)
        except (ErrorAPI, urllib.error.URLError) as e:
            log(f"Ciclo incompleto: {e}")
        if args.exportar:
            exportar(con, config, Path(args.exportar))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        return 0

    try:
        servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Tablero)
    except OSError:
        print("El simulador ya está abierto en otra ventana; abriendo el tablero.")
        webbrowser.open(URL)
        return 0
    if not args.sin_motor:
        threading.Thread(target=bucle_motor, daemon=True).start()
    print("=" * 60)
    print(f" Simulador de apuestas corriendo. Tablero: {URL}")
    print(" Deja esta ventana abierta; ciérrala para detener el motor.")
    print("=" * 60, flush=True)
    if not args.sin_navegador:
        webbrowser.open(URL)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
