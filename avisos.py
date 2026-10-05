"""Avisos por Telegram: cada apuesta nueva de la Principal y de "México real" le llega a Diego con botones.

Diego la hace a mano en la app de la casa (las casas no permiten apostar con bots) y contesta con un botón:
✅ Caliente / ✅ Codere / ❌ No estaba el momio / ⏭️ Paso. Las respuestas se leen en el siguiente ciclo (cada 5-15 min),
así que la libreta de "lo que se hizo en la vida real" se arma sola, con sus estadísticas (/resumen en el chat y la
pestaña "Plan real"). Solo corre en la nube (secreto TELEGRAM_TOKEN): en la PC no, para que no se dupliquen mensajes
ni se roben las respuestas.

El primer chat privado que le escriba al bot queda como el único autorizado.
"""
import html
import json
import os
import re
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import externos
from base_datos import a_fecha, ahora, anotar, guardar_estado, iso, leer_estado

ROLES = ("principal", "mexico")
MAX_POR_CICLO = 8
RESPUESTAS = {"h": ("hecha", "✅ Apostaste"), "n": ("no_habia", "❌ No cuadró el momio"), "p": ("paso", "⏭️ No apostaste")}
fallas = 0  # llamadas a Telegram que fallaron en este ciclo


def token() -> str:
    return os.environ.get("TELEGRAM_TOKEN", "").strip()


def _llamar(metodo: str, datos: dict):
    """Llama a la API de Telegram. Nunca truena el ciclo ni escribe el token en los registros."""
    global fallas
    clave = token()
    peticion = urllib.request.Request(f"https://api.telegram.org/bot{clave}/{metodo}",
                                      data=json.dumps(datos).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(peticion, timeout=15) as r:
            return json.load(r).get("result")
    except (urllib.error.URLError, OSError, ValueError) as e:
        if metodo != "answerCallbackQuery":  # contestar un botón viejo siempre falla; no es una falla del sistema
            fallas += 1
        print(f"Telegram {metodo}: {str(e).replace(clave, '***')}", flush=True)
        return None


def _enviar(con, texto: str, botones=None, responder_a=None):
    datos = {"chat_id": leer_estado(con, "telegram_chat"), "text": texto, "parse_mode": "HTML",
             "disable_web_page_preview": True}
    if botones:
        datos["reply_markup"] = {"inline_keyboard": botones}
    if responder_a:
        datos["reply_parameters"] = {"message_id": responder_a, "allow_sending_without_reply": True}
    return _llamar("sendMessage", datos)


def americano(d: float) -> str:
    return f"+{(d - 1) * 100:.0f}" if d >= 2 else f"−{100 / (d - 1):.0f}"


def leer_momio(texto: str):
    """'1.95', '1,95', '+150' o '-110' → momio decimal."""
    m = re.search(r"([+\-−])?\s*(\d+(?:[.,]\d+)?)", texto)
    if not m:
        return None
    signo, n = m.group(1), float(m.group(2).replace(",", "."))
    if n >= 100:  # americano
        return round(1 + 100 / n, 3) if signo in ("-", "−") else round(1 + n / 100, 3)
    return n if 1.01 <= n <= 50 else None


ICONOS = {"soccer": "⚽", "americanfootball": "🏈", "basketball": "🏀", "icehockey": "🏒", "baseball": "⚾",
          "tennis": "🎾", "mma": "🥊", "boxing": "🥊", "rugbyleague": "🏉", "rugbyunion": "🏉", "aussierules": "🏉",
          "cricket": "🏏", "golf": "⛳"}


def icono(deporte: str) -> str:
    return ICONOS.get(deporte.split("_")[0], "🏟️")


def leer_respuesta(texto: str, casas: list) -> tuple:
    """'50 +460 codere', '$100 a 1.95', '-105' → (monto, momio decimal, clave de la casa); lo que no venga, None.
    El momio lleva signo (americano) o punto decimal; un número sin signo ni punto es el monto."""
    t = texto.lower().replace("−", "-")
    casa = next((c["clave"] for c in casas if c["nombre"].lower() in t or c["clave"] in t), None)
    monto = momio = None
    m = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", t)
    if m:
        monto = float(m.group(1).replace(",", ""))
        t = t[:m.start()] + " " + t[m.end():]
    for signo, numero in re.findall(r"(?<![\w.,])([+-]?)(\d+(?:[.,]\d+)?)(?![\w.,])", t):
        n = float(numero.replace(",", "."))
        if signo and n >= 100 and momio is None:
            momio = round(1 + n / 100, 3) if signo == "+" else round(1 + 100 / n, 3)
        elif not signo and ("." in numero or "," in numero) and 1.01 <= n <= 50 and momio is None:
            momio = n
        elif not signo and numero.isdigit() and monto is None:
            monto = n
    return monto, momio, casa


def _seleccion(s: str) -> str:
    return "Empate" if s.lower() in ("draw", "empate", "x") else s


def _cuentas(config) -> list:
    return [c for c in config["casas_mexico"] if c.get("cuenta")]


def _texto(a, config, sugerida=None) -> tuple[str, float, float]:
    """sugerida: (nombre de la casa mexicana recomendada, su último momio leído o None)."""
    t = config["telegram"]
    inicio = a_fecha(a["inicio"]).astimezone()
    vence = min(a_fecha(a["inicio"]), ahora() + timedelta(minutes=t["minutos_vigencia"])).astimezone()
    minimo = (1 + t["valor_minimo"]) / a["prob_justa"]
    monto = max(10, round(a["monto"] / config["banca_inicial"] * t["banca_real"] / 10) * 10)
    casa = a["casa"].replace("_", " ").title()
    lineas = [
        "🧪 <b>PRÁCTICA, sin dinero</b>" if t["practica"] else "💰 <b>APUESTA REAL</b>",
        f"{icono(a['deporte'])} {html.escape(a['liga'] or a['deporte'])}: <b>{html.escape(a['local'])} vs {html.escape(a['visitante'])}</b>",
        f"🕒 Empieza {inicio:%d/%m %H:%M}",
        f"🎯 Apostar a: <b>{html.escape(_seleccion(a['seleccion']))}</b>",
        *([f"🏦 Apostar en: <b>{sugerida[0]}</b>" + (
            f", paga {sugerida[1]:.2f} ({americano(sugerida[1])})"
            + (" ⚠️ menos del mínimo en la última lectura" if sugerida[1] < minimo else "") if sugerida[1] else "")]
          if sugerida else []),
        f"📈 El bot la tomó en {html.escape(casa)} a {a['momio']:.2f} ({americano(a['momio'])}), "
        f"valor {a['valor']:+.1%}",
        f"✅ Hazla solo si te pagan <b>{minimo:.2f} ({americano(minimo)}) o más</b>",
        f"💵 Monto (banca de ${t['banca_real']:,.0f}): <b>${monto:,.0f}</b>",
        f"⏳ Vale hasta las {vence:%H:%M}",
    ]
    if a["rol"] == "mexico":
        lineas.insert(1, "🇲🇽 Momio de casa mexicana")
    return "\n".join(lineas), monto, minimo


def _conectar(con, mensaje) -> None:
    chat = mensaje["chat"]["id"]
    guardar_estado(con, "telegram_chat", chat)
    guardar_estado(con, "avisos_desde", con.execute("SELECT COALESCE(MAX(id), 0) FROM apuestas").fetchone()[0])
    anotar(con, "sistema", "Telegram conectado: desde ahora las apuestas de la Principal y de México real llegan al celular.")
    _llamar("setMyCommands", {"commands": [{"command": "resumen", "description": "Estadísticas de las alertas"}]})
    _enviar(con, "✅ <b>Conectado.</b>\nTe mando aquí cada apuesta nueva de la Principal y de México real.\n\n"
                 "1. Ábrela en Caliente o Codere y revisa el momio.\n"
                 "2. Si paga lo mínimo o más, hazla con el monto sugerido.\n"
                 "3. Pícale la casa donde la hiciste, ❌ No estaba o ⏭️ Paso.\n\n"
                 "Para anotar lo que apostaste, respóndeme el mensaje de la apuesta con monto, momio y casa "
                 "(ej. 50 +460 codere).\nLos botones se registran en el siguiente ciclo (5 a 15 min).\n"
                 "Escribe /resumen para ver las estadísticas.")


def leer_respuestas(con, config) -> None:
    desde = leer_estado(con, "telegram_offset", 0)
    previo = leer_estado(con, "telegram_leido")
    cambios = _llamar("getUpdates", {"offset": desde, "timeout": 0,
                                     "allowed_updates": ["message", "callback_query"]})
    if cambios is None:
        return
    guardar_estado(con, "telegram_leido", iso(ahora()))
    for u in cambios:
        guardar_estado(con, "telegram_offset", u["update_id"] + 1)
        chat = leer_estado(con, "telegram_chat")
        mensaje = u.get("message")
        if mensaje and mensaje["chat"]["type"] == "private":
            if chat is None:
                _conectar(con, mensaje)
            elif mensaje["chat"]["id"] == chat:
                _mensaje(con, config, mensaje)
        consulta = u.get("callback_query")
        if consulta and chat is not None and (consulta.get("message") or {}).get("chat", {}).get("id") == chat:
            _boton(con, config, consulta, previo)
    con.commit()


def _mensaje(con, config, mensaje) -> None:
    texto = mensaje.get("text", "")
    if texto.strip().lower().lstrip("/").startswith("resumen"):
        _enviar(con, texto_resumen(estadisticas(con, config)))
        return
    original = (mensaje.get("reply_to_message") or {}).get("message_id")
    aviso = original and con.execute("SELECT apuesta_id, respuesta FROM avisos WHERE mensaje_id = ?",
                                     (original,)).fetchone()
    monto, momio, casa = leer_respuesta(texto, config["casas_mexico"])
    nombre = next((c["nombre"] for c in config["casas_mexico"] if c["clave"] == casa), None)
    partes = ([f"${monto:,.0f}"] if monto else []) + ([f"a {momio:.2f} ({americano(momio)})"] if momio else []) \
        + ([f"en {nombre}"] if nombre else [])
    simulacro = original and "SIMULACRO" in (mensaje["reply_to_message"].get("text") or "")
    if original and simulacro and partes:
        _enviar(con, f"🔧 Simulacro: anotaría {' '.join(partes)}. Funcionó; no cuenta en las estadísticas.",
                responder_a=original)
    elif aviso and partes:
        momento = iso(datetime.fromtimestamp(mensaje["date"], timezone.utc) if mensaje.get("date") else ahora())
        solo_vio = aviso["respuesta"] == "no_habia" and not monto  # anota el momio que había, no una apuesta
        con.execute("UPDATE avisos SET momio_real = COALESCE(?, momio_real), monto_real = COALESCE(?, monto_real), "
                    "casa_real = COALESCE(?, casa_real), respuesta = ?, respondido = COALESCE(respondido, ?) "
                    "WHERE apuesta_id = ?", (momio, monto, casa, "no_habia" if solo_vio else "hecha", momento,
                                             aviso["apuesta_id"]))
        _enviar(con, f"📝 Anotado: {'había' if solo_vio else 'apostaste'} {' '.join(partes)}.", responder_a=original)
    else:
        _enviar(con, "Te escribo solo cuando hay apuesta. Para anotar lo que apostaste, responde al mensaje de esa "
                     "apuesta con monto, momio y casa (ej. 50 +460 codere). Escribe /resumen para ver las estadísticas.")


def _simulacro(con, config, consulta, partes, previo) -> None:
    if not partes or partes[0] not in RESPUESTAS:
        return
    etiqueta = RESPUESTAS[partes[0]][1]
    if len(partes) > 1:
        etiqueta += " en " + next((c["nombre"] for c in config["casas_mexico"] if c["clave"] == partes[1]), partes[1])
    mensaje = consulta["message"]
    enviado = datetime.fromtimestamp(mensaje["date"], timezone.utc)
    inicio = max(enviado, a_fecha(previo)) if previo else enviado
    minutos = ((inicio + (ahora() - inicio) / 2) - enviado).total_seconds() / 60
    _llamar("answerCallbackQuery", {"callback_query_id": consulta["id"], "text": etiqueta})
    _llamar("editMessageText", {"chat_id": mensaje["chat"]["id"], "message_id": mensaje["message_id"],
                                "text": f"{mensaje.get('text', '')}\n\n{etiqueta} (≈{minutos:.0f} min después del aviso)\n"
                                        "Simulacro: funcionó, pero no cuenta en las estadísticas."})


def _boton(con, config, consulta, previo) -> None:
    partes = consulta.get("data", "").split(":")
    if partes[0] == "t":  # simulacro: se contesta igual, pero no entra a las estadísticas
        _simulacro(con, config, consulta, partes[1:], previo)
        return
    if len(partes) < 2 or partes[0] not in RESPUESTAS or not partes[1].isdigit():
        return
    ident, casa = int(partes[1]), (partes[2] if len(partes) > 2 else None)
    fila = con.execute("SELECT texto, mensaje_id, enviado, casa_sugerida, momio_casa, momio_minimo FROM avisos "
                       "WHERE apuesta_id = ?", (ident,)).fetchone()
    if not fila:
        return
    respuesta, etiqueta = RESPUESTAS[partes[0]]
    momio = None
    if respuesta == "hecha" and not casa:  # "Aposté": en la casa recomendada, al monto sugerido
        casa = fila["casa_sugerida"]
        # momio: el leído en esa casa si llegaba al mínimo; si no se conocía, el mínimo (solo se apuesta si paga eso)
        momio = fila["momio_casa"] if fila["momio_casa"] and fila["momio_casa"] >= (fila["momio_minimo"] or 0) \
            else fila["momio_minimo"]
    if casa:
        nombre = next((c["nombre"] for c in config["casas_mexico"] if c["clave"] == casa), casa)
        etiqueta += f" en {nombre}"
    # Telegram no dice a qué hora se picó el botón: se estima a la mitad entre esta lectura y la anterior
    fin, enviado = ahora(), a_fecha(fila["enviado"])
    inicio = max(enviado, a_fecha(previo)) if previo else enviado
    momento = iso(inicio + (fin - inicio) / 2)
    con.execute("UPDATE avisos SET respuesta = ?, respondido = ?, casa_real = ?, momio_real = COALESCE(momio_real, ?) "
                "WHERE apuesta_id = ?", (respuesta, momento, casa if respuesta == "hecha" else None, momio, ident))
    _llamar("answerCallbackQuery", {"callback_query_id": consulta["id"], "text": etiqueta})
    extra = ""
    _llamar("editMessageText", {"chat_id": leer_estado(con, "telegram_chat"), "message_id": fila["mensaje_id"],
                                "text": f"{fila['texto']}\n\n<b>{etiqueta}</b>{extra}", "parse_mode": "HTML",
                                "disable_web_page_preview": True})
    anotar(con, "sistema", f"Telegram: {etiqueta.split(' ', 1)[1].lower()} (apuesta #{ident}).")


def _boton_casa(con, casa, a) -> dict:
    """Abre el partido exacto si salió en un barrido; si no, la liga o la sección del deporte. Con `app_url`
    (enlace universal de la casa) se abre en la app del celular en lugar del navegador."""
    fila = con.execute("SELECT url FROM enlaces WHERE evento_id = ? AND casa = ?", (a["evento_id"], casa["clave"])).fetchone()
    ruta = (casa.get("ligas") or {}).get(a["deporte"]) or (casa.get("deportes") or {}).get(a["deporte"].split("_")[0])
    if fila:
        destino, texto = fila[0], f"🎯 {casa['nombre']}: partido"
    elif ruta:
        destino, texto = casa["web"] + ruta, f"{casa['nombre']}: liga"
    else:
        destino, texto = casa["url"], f"Abrir {casa['nombre']}"
    if casa.get("app_url"):
        destino = casa["app_url"].format(urllib.parse.quote(destino, safe=""))
    return {"text": texto, "url": destino}


def _precios_mexico(con, config) -> dict:
    """{evento_id: {clave de casa: {selección: momio}}} del último barrido de cada casa con cuenta (si es reciente)."""
    precios = {}
    for c in _cuentas(config):
        datos = externos.cargar(c["clave"])
        if datos and externos.reciente(datos):
            for evento, v in externos.emparejar(con, datos).items():
                precios.setdefault(evento, {})[c["clave"]] = v["precios"]
    return precios


def _recomendar(a, cuentas, precios) -> tuple:
    """La casa mexicana donde hacer la apuesta: la de la propia apuesta si es de México real; si no, la que pagó más
    en el último barrido; sin lectura, la primera con cuenta (Caliente abre la app directo)."""
    propia = next((c for c in cuentas if c["clave"] == a["casa"]), None)
    if propia:
        return propia, a["momio_visto"] or a["momio"]
    leidas = [(precios.get(a["evento_id"], {}).get(c["clave"], {}).get(a["seleccion"]), c) for c in cuentas]
    leidas = [(m, c) for m, c in leidas if m]
    if leidas:
        m, c = max(leidas, key=lambda x: x[0])
        return c, m
    return (cuentas[0], None) if cuentas else (None, None)


def enviar_nuevas(con, config: dict) -> None:
    desde = leer_estado(con, "avisos_desde", 0)
    filas = con.execute(
        "SELECT a.*, e.local, e.visitante, s.rol FROM apuestas a JOIN eventos e ON e.id = a.evento_id "
        "JOIN estrategias s ON s.nombre = a.estrategia LEFT JOIN avisos v ON v.apuesta_id = a.id "
        f"WHERE a.id > ? AND s.rol IN ({','.join('?' * len(ROLES))}) AND v.apuesta_id IS NULL ORDER BY a.id",
        (desde, *ROLES)).fetchall()
    enviados, cuentas = 0, _cuentas(config)
    precios = _precios_mexico(con, config) if filas else {}
    for a in filas:
        if a["inicio"] <= iso(ahora()):  # ya empezó: no tiene caso avisar
            con.execute("INSERT INTO avisos (apuesta_id, enviado, respuesta) VALUES (?, ?, 'tarde')",
                        (a["id"], iso(ahora())))
            continue
        if enviados >= MAX_POR_CICLO:
            break
        casa, momio_casa = _recomendar(a, cuentas, precios)
        texto, monto, minimo = _texto(a, config, (casa["nombre"], momio_casa) if casa else None)
        enlaces = [_boton_casa(con, c, a) for c in cuentas]
        for b, c in zip(enlaces, cuentas):
            if casa and c["clave"] == casa["clave"]:
                b["text"] = "⭐ " + b["text"]
        botones = [enlaces, [{"text": "✅ Aposté", "callback_data": f"h:{a['id']}"}],
                   [{"text": "❌ No cuadra", "callback_data": f"n:{a['id']}"},
                    {"text": "⏭️ No apostar", "callback_data": f"p:{a['id']}"}]]
        enviado = _enviar(con, texto, [fila for fila in botones if fila])
        if not enviado:
            break  # Telegram no respondió: se reintenta en el siguiente ciclo
        con.execute("INSERT INTO avisos (apuesta_id, mensaje_id, texto, enviado, monto_real, momio_minimo, casa_sugerida, "
                    "momio_casa) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (a["id"], enviado["message_id"], texto, iso(ahora()), monto, minimo, casa and casa["clave"], momio_casa))
        enviados += 1
    con.commit()


def _resultado_real(f) -> float:
    momio = f["momio_real"] or f["momio"]
    return f["monto_real"] * (momio - 1) if f["estado"] == "ganada" else -f["monto_real"] if f["estado"] == "perdida" else 0.0


def avisar_resultados(con) -> None:
    filas = con.execute(
        "SELECT v.apuesta_id, v.mensaje_id, v.respuesta, v.momio_real, v.monto_real, a.estado, a.momio, a.ganancia "
        "FROM avisos v JOIN apuestas a ON a.id = v.apuesta_id "
        "WHERE v.resultado_avisado = 0 AND v.mensaje_id IS NOT NULL AND a.estado != 'abierta'").fetchall()
    for f in filas:
        if f["estado"] == "anulada":
            texto = "↩️ Partido anulado o pospuesto: se devuelve lo apostado."
        else:
            texto = f"{'🟢 Ganó' if f['estado'] == 'ganada' else '🔴 Perdió'}. Simulador: {f['ganancia']:+,.0f}."
            if f["respuesta"] == "hecha":
                texto += (f"\nTú (a {f['momio_real'] or f['momio']:.2f} con ${f['monto_real']:,.0f}): "
                          f"<b>{_resultado_real(f):+,.0f}</b>")
        if _enviar(con, texto, responder_a=f["mensaje_id"]):
            con.execute("UPDATE avisos SET resultado_avisado = 1 WHERE apuesta_id = ?", (f["apuesta_id"],))
    con.commit()


def estadisticas(con, config: dict) -> dict:
    """Todo lo que mide la fase de alertas: si los momios se consiguen, en qué casa, qué tan rápido contesta Diego,
    cómo va el resultado real contra el simulador y si el sistema de avisos funciona."""
    filas = con.execute(
        "SELECT v.*, a.momio, a.colocada, a.estado, a.ganancia FROM avisos v JOIN apuestas a ON a.id = v.apuesta_id"
    ).fetchall()
    escala = config["telegram"]["banca_real"] / config["banca_inicial"]
    enviadas = [f for f in filas if f["mensaje_id"]]
    hechas = [f for f in enviadas if f["respuesta"] == "hecha"]
    no_habia = [f for f in enviadas if f["respuesta"] == "no_habia"]
    vencida = lambda f: f["respuesta"] is None and a_fecha(f["enviado"]) < ahora() - timedelta(
        minutes=config["telegram"]["minutos_vigencia"])
    al_momio = [f for f in hechas if not f["momio_real"] or f["momio_real"] >= (f["momio_minimo"] or 0) - 1e-9]
    minutos = lambda a, b: (a_fecha(b) - a_fecha(a)).total_seconds() / 60
    respuesta = [minutos(f["enviado"], f["respondido"]) for f in enviadas if f["respondido"]]
    retraso = [minutos(f["colocada"], f["enviado"]) for f in enviadas]
    cerradas = [f for f in hechas if f["estado"] in ("ganada", "perdida", "anulada")]
    sim = [f for f in enviadas if f["estado"] in ("ganada", "perdida", "anulada")]
    casas, ids_al_momio = {}, {f["apuesta_id"] for f in al_momio}
    for c in config["casas_mexico"]:
        suyas = [f for f in hechas if f["casa_real"] == c["clave"]]
        if c.get("cuenta") or suyas:
            con_momio = [f["momio_real"] / f["momio"] - 1 for f in suyas if f["momio_real"]]
            casas[c["clave"]] = {
                "nombre": c["nombre"], "hechas": len(suyas),
                "al_momio": sum(1 for f in suyas if f["apuesta_id"] in ids_al_momio),
                "diferencia": statistics.mean(con_momio) if con_momio else None,
                "resultado": sum(_resultado_real(f) for f in suyas if f["estado"] in ("ganada", "perdida", "anulada")),
                "monto": sum(f["monto_real"] for f in suyas)}
    intentadas = len(hechas) + len(no_habia)
    return {
        "conectado": leer_estado(con, "telegram_chat") is not None,
        "ultima_lectura": leer_estado(con, "telegram_leido"),
        "fallas": leer_estado(con, "telegram_fallas", 0),
        "enviadas": len(enviadas),
        "tarde": sum(1 for f in filas if f["respuesta"] == "tarde"),
        "hechas": len(hechas), "al_momio": len(al_momio), "bajo_momio": len(hechas) - len(al_momio),
        "no_habia": len(no_habia), "paso": sum(1 for f in enviadas if f["respuesta"] == "paso"),
        "sin_respuesta": sum(1 for f in enviadas if vencida(f)),
        "pendientes": sum(1 for f in enviadas if f["respuesta"] is None and not vencida(f)),
        "tasa_conseguido": len(al_momio) / intentadas if intentadas else None,
        "respuesta_mediana": statistics.median(respuesta) if respuesta else None,
        "respuesta_rapidas": sum(1 for m in respuesta if m <= 15) / len(respuesta) if respuesta else None,
        "retraso_mediano": statistics.median(retraso) if retraso else None,
        "resultado_real": sum(_resultado_real(f) for f in cerradas),
        "monto_real": sum(f["monto_real"] for f in cerradas),
        "resultado_simulador": sum((f["ganancia"] or 0) * escala for f in sim),
        "cerradas_real": len(cerradas), "cerradas_simulador": len(sim),
        "casas": list(casas.values()),
    }


def texto_resumen(s: dict) -> str:
    p = lambda x: "—" if x is None else f"{x:.0%}"
    m = lambda x: "—" if x is None else f"{x:.0f} min"
    lineas = [
        "📊 <b>Resumen de alertas</b>",
        f"📨 Enviadas: {s['enviadas']} · pendientes {s['pendientes']} · sin contestar {s['sin_respuesta']}",
        f"🎯 Momio conseguido: <b>{p(s['tasa_conseguido'])}</b> ({s['al_momio']} al momio, {s['bajo_momio']} más bajo, "
        f"{s['no_habia']} no estaba) · pasaste {s['paso']}",
        f"⏱️ Tardas en contestar: {m(s['respuesta_mediana'])} (mediana) · {p(s['respuesta_rapidas'])} en menos de 15 min",
        f"💰 Tú: <b>{s['resultado_real']:+,.0f}</b> en {s['cerradas_real']} terminadas (${s['monto_real']:,.0f} apostados)"
        f" · simulador a tu escala: {s['resultado_simulador']:+,.0f} en {s['cerradas_simulador']}",
    ]
    for c in s["casas"]:
        dif = "" if c["diferencia"] is None else f", momio {c['diferencia']:+.1%} vs el del bot"
        lineas.append(f"🏦 {c['nombre']}: {c['hechas']} hechas ({c['al_momio']} al momio{dif}) · {c['resultado']:+,.0f}")
    lineas.append(f"⚙️ Sistema: aviso {m(s['retraso_mediano'])} después de detectar la apuesta · "
                  f"{s['tarde']} detectadas tarde · {s['fallas']} fallas de envío")
    return "\n".join(lineas)


def ciclo(con, config: dict) -> None:
    global fallas
    if not token() or not config.get("telegram", {}).get("activo"):
        return
    fallas = 0
    leer_respuestas(con, config)
    if leer_estado(con, "telegram_chat") is not None:
        enviar_nuevas(con, config)
        avisar_resultados(con)
    if fallas:
        guardar_estado(con, "telegram_fallas", leer_estado(con, "telegram_fallas", 0) + fallas)
        con.commit()
