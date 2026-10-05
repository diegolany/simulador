"""Avisos por Telegram: cada apuesta nueva de la Principal y de "México real" le llega a Diego con botones.

Diego la hace a mano en la app de la casa (las casas no permiten apostar con bots) y contesta con un botón:
✅ La hice / ❌ No estaba el momio / ⏭️ Paso. Las respuestas se leen en el siguiente ciclo (cada 5-15 min), así que
la libreta de "lo que se hizo en la vida real" se arma sola. Solo corre en la nube (secreto TELEGRAM_TOKEN): en la PC
no, para que no se dupliquen mensajes ni se roben las respuestas.

El primer chat privado que le escriba al bot queda como el único autorizado.
"""
import html
import json
import os
import re
import urllib.error
import urllib.request
from datetime import timedelta

from base_datos import a_fecha, ahora, anotar, guardar_estado, iso, leer_estado

ROLES = ("principal", "mexico")
MAX_POR_CICLO = 8
RESPUESTAS = {"h": ("hecha", "✅ La hiciste"), "n": ("no_habia", "❌ No estaba el momio"), "p": ("paso", "⏭️ La pasaste")}


def token() -> str:
    return os.environ.get("TELEGRAM_TOKEN", "").strip()


def _llamar(metodo: str, datos: dict):
    """Llama a la API de Telegram. Nunca truena el ciclo ni escribe el token en los registros."""
    clave = token()
    peticion = urllib.request.Request(f"https://api.telegram.org/bot{clave}/{metodo}",
                                      data=json.dumps(datos).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(peticion, timeout=15) as r:
            return json.load(r).get("result")
    except (urllib.error.URLError, OSError, ValueError) as e:
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


def _seleccion(s: str) -> str:
    return "Empate" if s.lower() in ("draw", "empate", "x") else s


def _texto(a, config) -> tuple[str, float]:
    t = config["telegram"]
    inicio = a_fecha(a["inicio"]).astimezone()
    vence = min(a_fecha(a["inicio"]), ahora() + timedelta(minutes=t["minutos_vigencia"])).astimezone()
    minimo = (1 + t["valor_minimo"]) / a["prob_justa"]
    monto = max(10, round(a["monto"] / config["banca_inicial"] * t["banca_real"] / 10) * 10)
    casa = a["casa"].replace("_", " ").title()
    lineas = [
        "🧪 <b>PRÁCTICA, sin dinero</b>" if t["practica"] else "💰 <b>APUESTA REAL</b>",
        f"🏟️ {html.escape(a['liga'] or a['deporte'])}: <b>{html.escape(a['local'])} vs {html.escape(a['visitante'])}</b>",
        f"🕒 Empieza {inicio:%d/%m %H:%M}",
        f"🎯 Apostar a: <b>{html.escape(_seleccion(a['seleccion']))}</b>",
        f"📈 El bot la tomó en {html.escape(casa)} a {a['momio']:.2f} ({americano(a['momio'])}), "
        f"valor {a['valor']:+.1%}",
        f"✅ Hazla solo si te pagan <b>{minimo:.2f} ({americano(minimo)}) o más</b>",
        f"💵 Monto (banca de ${t['banca_real']:,.0f}): <b>${monto:,.0f}</b>",
        f"⏳ Vale hasta las {vence:%H:%M}",
    ]
    if a["rol"] == "mexico":
        lineas.insert(1, "🇲🇽 Momio de casa mexicana")
    return "\n".join(lineas), monto


def _conectar(con, mensaje) -> None:
    chat = mensaje["chat"]["id"]
    guardar_estado(con, "telegram_chat", chat)
    guardar_estado(con, "avisos_desde", con.execute("SELECT COALESCE(MAX(id), 0) FROM apuestas").fetchone()[0])
    anotar(con, "sistema", "Telegram conectado: desde ahora las apuestas de la Principal y de México real llegan al celular.")
    _enviar(con, "✅ <b>Conectado.</b>\nTe mando aquí cada apuesta nueva de la Principal y de México real.\n\n"
                 "1. Ábrela en Caliente o Codere y revisa el momio.\n"
                 "2. Si paga lo mínimo o más, hazla con el monto sugerido.\n"
                 "3. Pícale ✅ La hice, ❌ No estaba o ⏭️ Paso.\n\n"
                 "Si te dieron otro momio, respóndeme el mensaje de la apuesta con el número (ej. 1.95 o -105).\n"
                 "Los botones se registran en el siguiente ciclo (5 a 15 min).")


def leer_respuestas(con) -> None:
    desde = leer_estado(con, "telegram_offset", 0)
    cambios = _llamar("getUpdates", {"offset": desde, "timeout": 0,
                                     "allowed_updates": ["message", "callback_query"]}) or []
    for u in cambios:
        guardar_estado(con, "telegram_offset", u["update_id"] + 1)
        chat = leer_estado(con, "telegram_chat")
        mensaje = u.get("message")
        if mensaje and mensaje["chat"]["type"] == "private":
            if chat is None:
                _conectar(con, mensaje)
            elif mensaje["chat"]["id"] == chat:
                _mensaje(con, mensaje)
        consulta = u.get("callback_query")
        if consulta and chat is not None and (consulta.get("message") or {}).get("chat", {}).get("id") == chat:
            _boton(con, consulta)
    con.commit()


def _mensaje(con, mensaje) -> None:
    original = (mensaje.get("reply_to_message") or {}).get("message_id")
    aviso = original and con.execute("SELECT apuesta_id FROM avisos WHERE mensaje_id = ?", (original,)).fetchone()
    momio = leer_momio(mensaje.get("text", ""))
    if aviso and momio:
        con.execute("UPDATE avisos SET momio_real = ?, respuesta = 'hecha', respondido = COALESCE(respondido, ?) "
                    "WHERE apuesta_id = ?", (momio, iso(ahora()), aviso[0]))
        _enviar(con, f"📝 Anotado: la hiciste a {momio:.2f} ({americano(momio)}).", responder_a=original)
    else:
        _enviar(con, "Te escribo solo cuando hay apuesta. Para corregir un momio, responde al mensaje de esa apuesta "
                     "con el número (ej. 1.95 o -105).")


def _boton(con, consulta) -> None:
    clave, _, ident = consulta.get("data", "").partition(":")
    if not ident.isdigit():
        return
    ident = int(ident)
    fila = con.execute("SELECT texto, mensaje_id FROM avisos WHERE apuesta_id = ?", (ident,)).fetchone()
    if clave not in RESPUESTAS or not fila:
        return
    respuesta, etiqueta = RESPUESTAS[clave]
    con.execute("UPDATE avisos SET respuesta = ?, respondido = ? WHERE apuesta_id = ?", (respuesta, iso(ahora()), ident))
    _llamar("answerCallbackQuery", {"callback_query_id": consulta["id"], "text": etiqueta})
    extra = "\n<i>Si te dieron otro momio, respóndeme este mensaje con el número.</i>" if respuesta == "hecha" else ""
    _llamar("editMessageText", {"chat_id": leer_estado(con, "telegram_chat"), "message_id": fila[1],
                                "text": f"{fila[0]}\n\n<b>{etiqueta}</b>{extra}", "parse_mode": "HTML",
                                "disable_web_page_preview": True})
    anotar(con, "sistema", f"Telegram: {etiqueta.split(' ', 1)[1].lower()} (apuesta #{ident}).")


def enviar_nuevas(con, config: dict) -> None:
    desde = leer_estado(con, "avisos_desde", 0)
    filas = con.execute(
        "SELECT a.*, e.local, e.visitante, s.rol FROM apuestas a JOIN eventos e ON e.id = a.evento_id "
        "JOIN estrategias s ON s.nombre = a.estrategia LEFT JOIN avisos v ON v.apuesta_id = a.id "
        f"WHERE a.id > ? AND s.rol IN ({','.join('?' * len(ROLES))}) AND v.apuesta_id IS NULL ORDER BY a.id",
        (desde, *ROLES)).fetchall()
    enviados = 0
    for a in filas:
        if a["inicio"] <= iso(ahora()):  # ya empezó: no tiene caso avisar
            con.execute("INSERT INTO avisos (apuesta_id, enviado, respuesta) VALUES (?, ?, 'tarde')",
                        (a["id"], iso(ahora())))
            continue
        if enviados >= MAX_POR_CICLO:
            break
        texto, monto = _texto(a, config)
        casas = [c for c in config["casas_mexico"] if c.get("cuenta") or c.get("lectura")][:2]
        botones = [[{"text": f"Abrir {c['nombre']}", "url": c["url"]} for c in casas],
                   [{"text": "✅ La hice", "callback_data": f"h:{a['id']}"},
                    {"text": "❌ No estaba", "callback_data": f"n:{a['id']}"},
                    {"text": "⏭️ Paso", "callback_data": f"p:{a['id']}"}]]
        enviado = _enviar(con, texto, botones)
        if not enviado:
            break  # Telegram no respondió: se reintenta en el siguiente ciclo
        con.execute("INSERT INTO avisos (apuesta_id, mensaje_id, texto, enviado, monto_real) VALUES (?, ?, ?, ?, ?)",
                    (a["id"], enviado["message_id"], texto, iso(ahora()), monto))
        enviados += 1
    con.commit()


def avisar_resultados(con) -> None:
    filas = con.execute(
        "SELECT v.apuesta_id, v.mensaje_id, v.respuesta, v.momio_real, v.monto_real, a.estado, a.momio, a.ganancia "
        "FROM avisos v JOIN apuestas a ON a.id = v.apuesta_id "
        "WHERE v.resultado_avisado = 0 AND v.mensaje_id IS NOT NULL AND a.estado != 'abierta'").fetchall()
    for f in filas:
        if f["estado"] == "anulada":
            texto = "↩️ Partido anulado o pospuesto: se devuelve lo apostado."
        else:
            gano = f["estado"] == "ganada"
            texto = f"{'🟢 Ganó' if gano else '🔴 Perdió'}. Simulador: {f['ganancia']:+,.0f}."
            if f["respuesta"] == "hecha":
                momio = f["momio_real"] or f["momio"]
                real = f["monto_real"] * (momio - 1) if gano else -f["monto_real"]
                texto += f"\nTú (si la hiciste a {momio:.2f} con ${f['monto_real']:,.0f}): <b>{real:+,.0f}</b>"
        if _enviar(con, texto, responder_a=f["mensaje_id"]):
            con.execute("UPDATE avisos SET resultado_avisado = 1 WHERE apuesta_id = ?", (f["apuesta_id"],))
    con.commit()


def ciclo(con, config: dict) -> None:
    if not token() or not config.get("telegram", {}).get("activo"):
        return
    leer_respuestas(con)
    if leer_estado(con, "telegram_chat") is None:
        return
    enviar_nuevas(con, config)
    avisar_resultados(con)
