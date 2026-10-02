"""Motor del simulador: corre solo mientras la ventana esté abierta y sirve el tablero web.

Cada ciclo (cada 10 minutos):
  1. Pide resultados de los deportes con partidos terminados y liquida apuestas.
  2. Mide el CLV de los partidos que ya empezaron.
  3. Toma la foto de cierre de las ligas con apuestas que empiezan pronto.
  4. En los horarios de barrido, busca momios nuevos y deja que cada estrategia apueste.
  5. Una vez por semana corre la revisión de aprendizaje.
"""
import argparse
import json
import shutil
import sys
import threading
import traceback
import urllib.error
import webbrowser
from datetime import date, datetime, time as hora, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aprendizaje
import estrategias
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


def barrido_pendiente(con, config: dict) -> tuple[bool, int]:
    """(¿ya pasó un horario de barrido sin hacerse?, cuántos horarios quedan hoy)."""
    momento = datetime.now().astimezone()
    ultimo = leer_estado(con, "ultimo_barrido")
    ultimo = a_fecha(ultimo) if ultimo else None
    horarios = [datetime.combine(date.today(), hora(h)).astimezone() for h in sorted(config["horas_barrido"])]
    pendiente = any(h <= momento and (ultimo is None or ultimo < h) for h in horarios)
    return pendiente, sum(1 for h in horarios if h > momento)


def apostar_con_captura(con, config: dict, deporte: str, liga: str, motivo: str) -> int:
    capturado, id_consumo, costo = descargar_momios(con, config, deporte, liga, motivo)
    colocadas, senales = estrategias.colocar_apuestas(con, config, capturado)
    con.execute("UPDATE consumo_api SET senales = ? WHERE id = ?", (senales, id_consumo))
    con.commit()
    detalle = ", ".join(f"{n} {e}" for e, n in colocadas.items()) or "sin apuestas"
    log(f"  {liga} ({motivo}): {senales} partidos con valor; apuestas: {detalle}")
    return costo


def barrido(con, config: dict, activos: dict, disponibles: int, horarios_restantes: int, manual: bool = False) -> int:
    presupuesto = int(disponibles * config["porcion_barrido"] / (1 + horarios_restantes))
    if manual:  # búsqueda pedida a mano: hasta la mitad de lo que queda hoy
        presupuesto = max(presupuesto, disponibles // 2)
    presupuesto = max(presupuesto, min(disponibles, 1))
    momento = ahora()
    ventana = momento + timedelta(hours=config["horas_ventana"])
    candidatos = []
    for deporte in config["deportes_candidatos"]:
        if deporte in activos and any(momento + timedelta(minutes=10) < t <= ventana
                                      for t in proximos_inicios(config["api_key"], deporte)):
            candidatos.append(deporte)
    orden = aprendizaje.prioridad_ligas(con, config, candidatos)
    log(f"Barrido: {len(candidatos)} ligas con partidos próximos, presupuesto {presupuesto} créditos")
    gastado = 0
    costo_liga = len(config["mercados"]) * len(config["region"].split(","))
    for deporte in orden:
        if presupuesto - gastado < costo_liga:
            break
        ultima = con.execute("SELECT MAX(fecha) FROM consumo_api WHERE endpoint = 'odds' AND deporte = ?",
                             (deporte,)).fetchone()[0]
        if ultima and momento - a_fecha(ultima) < timedelta(hours=config["horas_min_entre_descargas"]):
            continue
        gastado += apostar_con_captura(con, config, deporte, activos[deporte], "barrido")
    guardar_estado(con, "ultimo_barrido", iso(ahora()))
    con.commit()
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

    pendiente, horarios_restantes = barrido_pendiente(con, config)
    if pendiente or forzar_barrido:
        disponibles = creditos_hoy(con, restantes, config["reserva_creditos"])
        if disponibles:
            restantes -= barrido(con, config, activos, disponibles, horarios_restantes, manual=forzar_barrido)
        else:
            log("Barrido omitido: ya se usaron los créditos de hoy")
            guardar_estado(con, "ultimo_barrido", iso(ahora()))

    if aprendizaje.toca_revision(con, config):
        aprendizaje.revision(con, config)

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
    (carpeta / "estado.json").write_text(json.dumps(tablero.estado(con, config), ensure_ascii=False),
                                         encoding="utf-8")
    shutil.copy(CARPETA / "web" / "index.html", carpeta / "index.html")


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
