"""db.py — Persistencia SQLite para portfolio.

Tablas:
  state            — capital actual (1 fila, siempre sobreescrita)
  open_positions   — posiciones activas (JSON blob por posición)
  closed_positions — historial completo de trades (append-only, JSON blob)
  capital_history  — curva de capital (un punto por hora aprox.)

DATABASE_PATH env var (default /data/portfolio.db).
/data debe ser un Railway Volume para persistencia entre redeploys.
Sin Volume: usa /tmp/portfolio.db (sobrevive reinicios, no redeploys).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_DB_PATH = os.environ.get("DATABASE_PATH", "/data/portfolio.db")


def _get_path():
    path = _DB_PATH
    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            fallback = os.path.join("/tmp", os.path.basename(path))
            log.warning("No se puede crear %s — usando %s", directory, fallback)
            return fallback
    return path


def _conn():
    """Nueva conexión SQLite por operación (thread-safe)."""
    return sqlite3.connect(_get_path())


def init_db():
    """Crear tablas si no existen. Llamar al arrancar la app."""
    path = _get_path()
    log.info("DB: %s", path)
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS state (
                id                  INTEGER PRIMARY KEY CHECK (id = 1),
                capital_inicial     REAL,
                capital_total       REAL,
                capital_disponible  REAL,
                session_start       TEXT,
                updated_at          TEXT
            );
            CREATE TABLE IF NOT EXISTS open_positions (
                condition_id TEXT PRIMARY KEY,
                data         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS closed_positions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT,
                close_time   TEXT,
                status       TEXT,
                pnl          REAL,
                data         TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capital_history (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT,
                capital REAL
            );
        """)


def save_state(capital_inicial, capital_total, capital_disponible, session_start):
    now = datetime.now(timezone.utc).isoformat()
    ss  = session_start.isoformat() if hasattr(session_start, "isoformat") else str(session_start)
    try:
        with _conn() as conn:
            conn.execute("""
                INSERT INTO state
                    (id, capital_inicial, capital_total, capital_disponible,
                     session_start, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    capital_inicial    = excluded.capital_inicial,
                    capital_total      = excluded.capital_total,
                    capital_disponible = excluded.capital_disponible,
                    session_start      = excluded.session_start,
                    updated_at         = excluded.updated_at
            """, (capital_inicial, capital_total, capital_disponible, ss, now))
    except Exception as e:
        log.warning("db.save_state: %s", e)


def load_state():
    """Devuelve dict con capital fields o None si no hay datos."""
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT capital_inicial, capital_total, capital_disponible, session_start "
                "FROM state WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return {
            "capital_inicial":    row[0],
            "capital_total":      row[1],
            "capital_disponible": row[2],
            "session_start":      row[3],
        }
    except Exception as e:
        log.warning("db.load_state: %s", e)
        return None


def upsert_open_position(condition_id, data):
    try:
        with _conn() as conn:
            conn.execute("""
                INSERT INTO open_positions (condition_id, data) VALUES (?, ?)
                ON CONFLICT(condition_id) DO UPDATE SET data = excluded.data
            """, (condition_id, json.dumps(data)))
    except Exception as e:
        log.warning("db.upsert_open_position: %s", e)


def delete_open_position(condition_id):
    try:
        with _conn() as conn:
            conn.execute(
                "DELETE FROM open_positions WHERE condition_id = ?", (condition_id,)
            )
    except Exception as e:
        log.warning("db.delete_open_position: %s", e)


def load_open_positions():
    """Devuelve {condition_id: data_dict}."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT condition_id, data FROM open_positions"
            ).fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}
    except Exception as e:
        log.warning("db.load_open_positions: %s", e)
        return {}


def insert_closed_position(pos):
    try:
        with _conn() as conn:
            conn.execute("""
                INSERT INTO closed_positions (condition_id, close_time, status, pnl, data)
                VALUES (?, ?, ?, ?, ?)
            """, (
                pos.get("condition_id", ""),
                pos.get("close_time", ""),
                pos.get("status", ""),
                pos.get("pnl", 0.0),
                json.dumps(pos),
            ))
    except Exception as e:
        log.warning("db.insert_closed_position: %s", e)


def load_closed_positions():
    """Devuelve lista de dicts ordenada cronológicamente."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT data FROM closed_positions ORDER BY id"
            ).fetchall()
        return [json.loads(r[0]) for r in rows]
    except Exception as e:
        log.warning("db.load_closed_positions: %s", e)
        return []


def append_capital_point(ts, capital):
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO capital_history (ts, capital) VALUES (?, ?)",
                (ts, capital),
            )
    except Exception as e:
        log.warning("db.append_capital_point: %s", e)


def load_capital_history(limit=500):
    """Devuelve últimos N puntos para el gráfico del dashboard."""
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT ts, capital FROM capital_history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"time": r[0], "capital": r[1]} for r in reversed(rows)]
    except Exception as e:
        log.warning("db.load_capital_history: %s", e)
        return []
