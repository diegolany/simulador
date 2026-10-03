"""Base de datos local (SQLite) del simulador.

Todas las fechas se guardan en UTC con formato ISO (2026-10-02T01:18:35+00:00) para
poder compararlas como texto; se convierten a hora de México solo al mostrarlas.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUTA = Path(__file__).with_name("simulador.db")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    id TEXT PRIMARY KEY,            -- id de The Odds API
    deporte TEXT NOT NULL,          -- clave, ej. soccer_mexico_ligamx
    liga TEXT,                      -- nombre legible, ej. Liga MX
    local TEXT NOT NULL,
    visitante TEXT NOT NULL,
    inicio TEXT NOT NULL,
    marcador_local INTEGER,
    marcador_visitante INTEGER,
    terminado INTEGER NOT NULL DEFAULT 0,
    detalle TEXT                    -- estado en vivo según ESPN, ej. "2nd Half - 67'"
);

-- Cada descarga agrega una foto nueva; nunca se borra, así se puede medir
-- cómo se movió el momio hasta el cierre (CLV).
CREATE TABLE IF NOT EXISTS momios (
    id INTEGER PRIMARY KEY,
    evento_id TEXT NOT NULL REFERENCES eventos(id),
    casa TEXT NOT NULL,             -- ej. pinnacle, betsson, unibet_nl
    mercado TEXT NOT NULL,          -- h2h (ganador), spreads (hándicap), totals (altas/bajas)
    seleccion TEXT NOT NULL,        -- nombre del equipo, Draw (empate), Over / Under
    punto REAL,                     -- línea de hándicap o total; vacío en h2h
    momio REAL NOT NULL,            -- decimal
    capturado TEXT NOT NULL,        -- cuándo lo descargamos
    actualizado_casa TEXT           -- cuándo la API vio ese momio en la casa
);
CREATE INDEX IF NOT EXISTS ix_momios_evento ON momios(evento_id, casa, mercado, capturado);
CREATE INDEX IF NOT EXISTS ix_momios_capturado ON momios(capturado);

CREATE TABLE IF NOT EXISTS consumo_api (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    endpoint TEXT NOT NULL,         -- odds, scores
    motivo TEXT,                    -- barrido, cierre, resultados
    deporte TEXT,
    costo INTEGER NOT NULL,
    restantes INTEGER,
    senales INTEGER                 -- partidos con valor encontrados en esa descarga
);

CREATE TABLE IF NOT EXISTS estrategias (
    nombre TEXT PRIMARY KEY,
    tipo TEXT NOT NULL,             -- valor, favorito
    rol TEXT NOT NULL,              -- principal, retadora, control, retirada
    descripcion TEXT,
    parametros TEXT NOT NULL,       -- JSON
    creada TEXT NOT NULL,
    retirada TEXT
);

CREATE TABLE IF NOT EXISTS apuestas (
    id INTEGER PRIMARY KEY,
    estrategia TEXT NOT NULL REFERENCES estrategias(nombre),
    evento_id TEXT NOT NULL REFERENCES eventos(id),
    deporte TEXT NOT NULL,
    liga TEXT,
    mercado TEXT NOT NULL,
    seleccion TEXT NOT NULL,
    casa TEXT NOT NULL,
    momio REAL NOT NULL,
    momio_ref REAL,                 -- momio de Pinnacle al apostar
    prob_justa REAL NOT NULL,       -- probabilidad justa al apostar
    valor REAL NOT NULL,            -- valor esperado al apostar
    monto REAL NOT NULL,
    colocada TEXT NOT NULL,
    inicio TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'abierta',   -- abierta, ganada, perdida, anulada
    ganancia REAL,
    liquidada TEXT,
    nota TEXT,
    momio_cierre_ref REAL,          -- momio de Pinnacle al cierre
    prob_cierre REAL,               -- probabilidad justa al cierre
    clv REAL,                       -- valor contra el cierre; vacío si no hubo foto posterior
    clv_fuente TEXT,                -- pinnacle (foto de cierre) o draftkings (cierre publicado por ESPN)
    razon TEXT,                     -- por qué se hizo la apuesta, en palabras
    momio_visto REAL,               -- momio publicado; `momio` es el que se habría conseguido (con deslizamiento)
    margen_ref REAL,                -- margen de Pinnacle en ese partido (qué tan eficiente es el mercado)
    ventaja_estimada REAL,          -- ventaja real que estimó el cerebro al apostar
    minutos_precio REAL,            -- minutos entre que se vio el precio y se apostó
    multiplicador REAL,             -- agresividad del modo objetivo al apostar (1 = normal)
    cierre_revisado INTEGER NOT NULL DEFAULT 0,
    UNIQUE (estrategia, evento_id)
);

-- Probabilidad justa de Pinnacle de cada partido descargado (se haya apostado o no) y su resultado:
-- mide la calibración con cientos de partidos en lugar de solo las apuestas.
CREATE TABLE IF NOT EXISTS pronosticos (
    evento_id TEXT PRIMARY KEY,
    deporte TEXT NOT NULL,
    local TEXT NOT NULL,
    visitante TEXT NOT NULL,
    inicio TEXT NOT NULL,
    prob_local REAL NOT NULL,
    prob_empate REAL,
    prob_visitante REAL NOT NULL,
    capturado TEXT NOT NULL,
    resultado TEXT                  -- local, empate, visitante, sin_dato
);

-- Diario del apostador virtual: lo que hizo y pensó cada día (JSON con secciones)
CREATE TABLE IF NOT EXISTS diario (
    dia TEXT PRIMARY KEY,           -- fecha local AAAA-MM-DD
    texto TEXT NOT NULL,
    actualizado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bitacora (
    id INTEGER PRIMARY KEY,
    fecha TEXT NOT NULL,
    tipo TEXT NOT NULL,             -- inicio, aprendizaje, ajuste, promocion, retiro, nueva, semana, sistema
    mensaje TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS estado (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def iso(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).isoformat(timespec="seconds")


def a_fecha(texto: str) -> datetime:
    return datetime.fromisoformat(texto.replace("Z", "+00:00"))


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(RUTA, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    nuevas = (("eventos", "detalle", "TEXT"), ("apuestas", "clv_fuente", "TEXT"), ("apuestas", "razon", "TEXT"),
              ("apuestas", "momio_visto", "REAL"), ("apuestas", "margen_ref", "REAL"),
              ("apuestas", "ventaja_estimada", "REAL"), ("apuestas", "minutos_precio", "REAL"),
              ("apuestas", "multiplicador", "REAL"))
    for tabla, columna, tipo in nuevas:  # bases creadas antes de esas columnas
        if columna not in {c[1] for c in con.execute(f"PRAGMA table_info({tabla})")}:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
    return con


def leer_estado(con, clave: str, defecto=None):
    fila = con.execute("SELECT valor FROM estado WHERE clave = ?", (clave,)).fetchone()
    return json.loads(fila[0]) if fila else defecto


def guardar_estado(con, clave: str, valor) -> None:
    con.execute("INSERT INTO estado (clave, valor) VALUES (?, ?) "
                "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, json.dumps(valor)))


def podar(con, referencia: str) -> None:
    """Borra fotos de momios que ya no sirven para que la base no crezca sin límite: las de
    otras casas después de 2 días y todo lo de partidos sin apuestas después de 4 días.
    Las fotos de la casa de referencia de partidos apostados se quedan (son el cierre)."""
    hace_2, hace_4 = iso(ahora() - timedelta(days=2)), iso(ahora() - timedelta(days=4))
    con.execute("DELETE FROM momios WHERE casa != ? AND capturado < ?", (referencia, hace_2))
    sin_apuestas = "SELECT id FROM eventos WHERE inicio < ? AND id NOT IN (SELECT evento_id FROM apuestas)"
    con.execute(f"DELETE FROM momios WHERE evento_id IN ({sin_apuestas})", (hace_4,))
    con.execute(f"DELETE FROM eventos WHERE id IN ({sin_apuestas})", (hace_4,))
    con.commit()
    con.execute("VACUUM")


def anotar(con, tipo: str, mensaje: str) -> None:
    """Agrega una entrada a la bitácora de aprendizaje."""
    con.execute("INSERT INTO bitacora (fecha, tipo, mensaje) VALUES (?, ?, ?)", (iso(ahora()), tipo, mensaje))
    print(f"[{datetime.now():%d/%m %H:%M}] {mensaje}")
