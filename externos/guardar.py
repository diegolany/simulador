"""Guarda y sube un barrido de una casa mexicana hecho con externos/barrido.js.

    python externos/guardar.py paginas caliente            → imprime las páginas que debe leer barrido.js
    python externos/guardar.py caliente salida.json        → arma caliente.json y caliente_mejorado.json y los sube

Los archivos se suben a la rama "barridos" del repositorio con la API de GitHub (token en github_token.txt, que
no se sube). La nube los toma de ahí en el siguiente ciclo; así los barridos nunca chocan con los cambios de código
que Diego sube con GitHub Desktop. Sin token, quedan en la carpeta temporal y se avisa.
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CARPETA = Path(__file__).parent
PROYECTO = CARPETA.parent
REPO = "diegolany/simulador"
RAMA = "barridos"


def paginas(casa: str) -> dict:
    """Ligas exactas de la casa y, para los deportes de EE. UU. (sin liga propia), la sección del deporte."""
    config = json.loads((PROYECTO / "config.json").read_text(encoding="utf-8"))
    c = next(x for x in config["casas_mexico"] if x["clave"] == casa)
    salida = {k: v for k, v in (c.get("deportes") or {}).items() if k != "soccer"}
    salida.update(c.get("ligas") or {})
    return salida


def armar(casa: str, datos: dict, capturado: str) -> dict:
    partidos, por_url = [], {}
    for deporte, a, b, inicio, m1, empate, m2, url in datos["p"]:
        momios = {a: m1, b: m2}
        if empate:
            momios["Empate"] = empate
        p = {"deporte": deporte, "inicio": inicio + ":00+00:00", "equipos": [a, b], "momios": momios, "url": url}
        partidos.append(p)
        if url:
            por_url[url] = p
    mejorados = []
    for url, momios in datos.get("m", []):
        base = por_url.get(url)
        if base and set(momios) <= set(base["momios"]):  # mismas selecciones que el mercado normal
            mejorados.append({**base, "momios": {**base["momios"], **momios}})
    return {f"externos/{casa}.json": {"casa": casa, "capturado": capturado, "partidos": partidos},
            f"externos/{casa}_mejorado.json": {"casa": f"{casa}_mejorado", "capturado": capturado, "partidos": mejorados}}


def _api(metodo: str, ruta: str, token: str, cuerpo=None):
    peticion = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{ruta}", method=metodo,
                                      data=json.dumps(cuerpo).encode() if cuerpo is not None else None,
                                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                               "Content-Type": "application/json"})
    with urllib.request.urlopen(peticion, timeout=30) as r:
        return json.load(r)


def subir(archivos: dict, mensaje: str) -> str:
    """Un solo commit en la rama de barridos con todos los archivos."""
    ruta_token = PROYECTO / "github_token.txt"
    if not ruta_token.exists():
        return "sin github_token.txt: no se subió"
    token = re.search(r"(github_pat_\w+|gh[pousr]_\w+)", ruta_token.read_text(encoding="utf-8-sig")).group(1)
    try:
        padre = _api("GET", f"git/ref/heads/{RAMA}", token)["object"]["sha"]
        base = _api("GET", f"git/commits/{padre}", token)["tree"]["sha"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        padre = base = None  # primera vez: se crea la rama
    arbol = {"tree": [{"path": ruta, "mode": "100644", "type": "blob",
                       "content": json.dumps(contenido, ensure_ascii=False, indent=1)} for ruta, contenido in archivos.items()]}
    if base:
        arbol["base_tree"] = base
    arbol = _api("POST", "git/trees", token, arbol)["sha"]
    commit = _api("POST", "git/commits", token, {"message": mensaje, "tree": arbol, "parents": [padre] if padre else []})["sha"]
    if padre:
        _api("PATCH", f"git/refs/heads/{RAMA}", token, {"sha": commit})
    else:
        _api("POST", "git/refs", token, {"ref": f"refs/heads/{RAMA}", "sha": commit})
    return f"subido a la rama {RAMA} ({commit[:7]})"


def copiar_temporal(archivos: dict) -> None:
    """Copia local del último barrido (por si no hay token o falla la subida)."""
    copia = Path(os.environ.get("TEMP", "/tmp")) / "barridos"
    for ruta, contenido in archivos.items():
        destino = copia / ruta
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps(contenido, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    if sys.argv[1] == "paginas":
        print(json.dumps(paginas(sys.argv[2]), ensure_ascii=False))
        return 0
    casa, crudo = sys.argv[1], Path(sys.argv[2]).read_text(encoding="utf-8").strip()
    datos = json.loads(crudo)
    while isinstance(datos, str):  # el navegador devuelve el JSON como texto (a veces dos veces)
        datos = json.loads(datos)
    capturado = datetime.now(timezone.utc).isoformat(timespec="seconds")
    archivos = armar(casa, datos, capturado)
    copiar_temporal(archivos)
    normales, mejorados = (len(c["partidos"]) for c in archivos.values())
    resultado = subir(archivos, f"Barrido {casa} {capturado[:16]}")
    if resultado.startswith("sin github_token"):  # sin token: en la carpeta del proyecto, para subirlo con Push origin
        for ruta, contenido in archivos.items():
            (PROYECTO / ruta).write_text(json.dumps(contenido, ensure_ascii=False, indent=1), encoding="utf-8")
        resultado += " (quedó en la carpeta del proyecto: hay que darle Push origin)"
    print(f"{casa}: {normales} partidos, {mejorados} con momios mejorados · {resultado}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
