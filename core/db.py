"""DB layer — soporta SQLite (default, POC) y SQL Server (Azure, opcional).

Selecciona backend con `DB_BACKEND` env var:
  - DB_BACKEND=sqlite (default) → archivo local en SQLITE_PATH (default backend/valuedata.db)
  - DB_BACKEND=sqlserver → pyodbc + Azure SQL (DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD)

El shim de SQLite expone la misma API que pyodbc:
  - cur.execute(sql, *params)            # variadic OR tupla
  - cur.executemany(sql, seq)
  - row.column_name                       # acceso por atributo (case-insensitive)
  - cur.rowcount, cn.commit(), cn.rollback()
  - context manager (`with get_conn() as cn:`) cierra conexión al salir

Y reescribe automáticamente:
  - fpoc.<tabla>     → fpoc_<tabla>      (SQLite no tiene schemas)
  - SYSUTCDATETIME() → CURRENT_TIMESTAMP
  - GETUTCDATE()     → CURRENT_TIMESTAMP
  - LEN(             → length(

Otras divergencias T-SQL (TOP, OFFSET FETCH, MERGE, OUTPUT INSERTED, DATEADD,
PERCENTILE_CONT) se traducen inline en cada módulo — son demasiado contextuales
para un rewriter genérico.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


# Adapters: sqlite3 no maneja datetime.time por default — lo guardamos como string
sqlite3.register_adapter(_dt.time, lambda t: t.isoformat())


_BACKEND = os.environ.get("DB_BACKEND", "sqlite").lower()
_SQLITE_PATH = os.environ.get(
    "SQLITE_PATH",
    str(Path(__file__).resolve().parent / "valuedata.db"),
)


# =============================================================================
# SQL rewriter bidireccional — soporta dos dialectos en el código fuente:
#   - "fpoc.<tabla>"  (notación schema dot, Azure SQL Server nativa)
#   - "fpoc_<tabla>"  (notación con prefijo, SQLite que no tiene schemas)
#
# Ambas formas SE PUEDEN MEZCLAR en el backend. El cursor wrapper de get_conn()
# las traduce on-the-fly al dialecto activo:
#   - SQLite      → todo se reescribe a fpoc_<tabla> (_rewrite_sql)
#   - SQL Server  → todo se reescribe a fpoc.<tabla> (_rewrite_sql_for_mssql)
#
# Esta tolerancia es intencional para soportar QA local en SQLite y deploy en
# Azure SQL sin reescribir queries. Si ves "fpoc.X" en un módulo Y "fpoc_X"
# en otro, NO es inconsistencia: ambos llegan al motor correcto.
# =============================================================================
# -------- SQL rewriter (sqlite-only) --------
_SCHEMA_RE = re.compile(r"\bfpoc\.(\w+)", re.IGNORECASE)
_SYSUTC_RE = re.compile(r"\bSYSUTCDATETIME\s*\(\s*\)", re.IGNORECASE)
_GETUTC_RE = re.compile(r"\bGETUTCDATE\s*\(\s*\)", re.IGNORECASE)
_LEN_RE = re.compile(r"\bLEN\s*\(", re.IGNORECASE)


def _rewrite_sql(sql: str) -> str:
    sql = _SCHEMA_RE.sub(r"fpoc_\1", sql)
    sql = _SYSUTC_RE.sub("CURRENT_TIMESTAMP", sql)
    sql = _GETUTC_RE.sub("CURRENT_TIMESTAMP", sql)
    sql = _LEN_RE.sub("length(", sql)
    return sql


# Reverso: fpoc_<tabla> -> fpoc.<tabla> + datetime('now', '-X days') -> DATEADD()
# Para que código SQLite-style funcione contra SQL Server.
_FPOC_TABLE_RE = re.compile(r"\bfpoc_([a-zA-Z_][a-zA-Z0-9_]*)\b")
_DATETIME_NOW_RE = re.compile(
    r"datetime\s*\(\s*'now'\s*,\s*'-?(\d+)\s+(day|days|hour|hours|minute|minutes)'\s*\)",
    re.IGNORECASE,
)
_CURRENT_TS_RE = re.compile(r"\bCURRENT_TIMESTAMP\b", re.IGNORECASE)
# LIMIT N al final del query (literal). Se traduce a SELECT TOP N en el primer SELECT.
_LIMIT_TAIL_RE = re.compile(r"\s+LIMIT\s+(\d+)\s*;?\s*$", re.IGNORECASE)
# LIMIT ? al final (placeholder). El último param se mueve al principio como TOP (?).
_LIMIT_PLACEHOLDER_TAIL_RE = re.compile(r"\s+LIMIT\s+\?\s*;?\s*$", re.IGNORECASE)
# LIMIT ? OFFSET ?  ó  LIMIT N OFFSET ? — se traduce a OFFSET ? ROWS FETCH NEXT ? ROWS ONLY.
# Requiere que el query ya tenga ORDER BY (sino MSSQL rechaza OFFSET).
_LIMIT_OFFSET_PLACEHOLDER_RE = re.compile(
    r"\s+LIMIT\s+(\?|\d+)\s+OFFSET\s+\?\s*;?\s*$", re.IGNORECASE
)
_SELECT_TOKEN_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
# INSERT OR IGNORE no tiene equivalente directo en T-SQL; lo neutralizamos a INSERT
# y dejamos al caller manejar duplicados (idealmente con MERGE o IF NOT EXISTS).
_INSERT_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
# last_insert_rowid() (SQLite) -> SCOPE_IDENTITY() (T-SQL) para devolver el id
# autoincrementado del INSERT recien hecho en el mismo cursor.
_LAST_INSERT_ROWID_RE = re.compile(r"\blast_insert_rowid\s*\(\s*\)", re.IGNORECASE)


def _rewrite_sql_for_mssql(sql: str, params: tuple = ()) -> tuple[str, tuple]:
    """Convierte SQL escrito en estilo SQLite a sintaxis SQL Server.

    Devuelve (sql_reescrito, params_reordenados). La función puede mover params
    cuando los placeholders cambian de posición (ej: LIMIT ? -> SELECT TOP (?)).

    Reglas:
    - fpoc_<tabla>                  -> fpoc.<tabla>      (schema dot notation)
    - datetime('now', '-N days')    -> DATEADD(day, -N, SYSUTCDATETIME())
    - ... LIMIT N                   -> SELECT TOP N ...           (literal)
    - ... LIMIT ?                   -> SELECT TOP (?) ...         (placeholder; reordena params)
    - ... LIMIT ?/N OFFSET ?        -> ... OFFSET ? ROWS FETCH NEXT ?/N ROWS ONLY
    - INSERT OR IGNORE              -> INSERT (caller maneja duplicado)
    """
    sql = _FPOC_TABLE_RE.sub(r"fpoc.\1", sql)

    def _datetime_repl(m):
        n = m.group(1)
        unit = m.group(2).lower()
        unit_map = {
            "day": "day", "days": "day",
            "hour": "hour", "hours": "hour",
            "minute": "minute", "minutes": "minute",
        }
        u = unit_map.get(unit, "day")
        return f"DATEADD({u}, -{n}, SYSUTCDATETIME())"

    sql = _DATETIME_NOW_RE.sub(_datetime_repl, sql)

    # Caso compuesto: LIMIT ? OFFSET ? o LIMIT N OFFSET ?
    m_off = _LIMIT_OFFSET_PLACEHOLDER_RE.search(sql)
    if m_off:
        limit_token = m_off.group(1)  # '?' o número literal
        sql = _LIMIT_OFFSET_PLACEHOLDER_RE.sub("", sql)
        if limit_token == "?":
            # params: [..., limit, offset]. MSSQL espera OFFSET ? FETCH NEXT ?
            # con ese mismo orden (offset va primero en el SQL).
            params = list(params)
            limit_p, offset_p = params[-2], params[-1]
            params = tuple(params[:-2] + [offset_p, limit_p])
            sql = sql + " OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        else:
            # LIMIT N (literal) OFFSET ?  →  OFFSET ? ROWS FETCH NEXT N ROWS ONLY
            sql = sql + f" OFFSET ? ROWS FETCH NEXT {limit_token} ROWS ONLY"
    else:
        # LIMIT ? (placeholder, sin OFFSET)
        m_p = _LIMIT_PLACEHOLDER_TAIL_RE.search(sql)
        if m_p:
            sql = _LIMIT_PLACEHOLDER_TAIL_RE.sub("", sql)
            # Mover el último param (limit) al principio, en SELECT TOP (?).
            params = list(params)
            limit_p = params.pop()
            params = tuple([limit_p] + params)
            sql = _SELECT_TOKEN_RE.sub("SELECT TOP (?)", sql, count=1)
        else:
            # LIMIT N literal (mantengo comportamiento original).
            m = _LIMIT_TAIL_RE.search(sql)
            if m:
                n = m.group(1)
                sql = _LIMIT_TAIL_RE.sub("", sql)
                sql = _SELECT_TOKEN_RE.sub(f"SELECT TOP {n}", sql, count=1)

    sql = _INSERT_OR_IGNORE_RE.sub("INSERT", sql)
    sql = _LAST_INSERT_ROWID_RE.sub("SCOPE_IDENTITY()", sql)
    return sql, tuple(params) if not isinstance(params, tuple) else params


# -------- Row con acceso por atributo (case-insensitive) --------
class AttrRow:
    """Wrap sqlite3.Row para soportar `row.column_name` (como pyodbc)."""
    __slots__ = ("_row", "_idx")

    def __init__(self, row: sqlite3.Row, description: tuple) -> None:
        self._row = row
        # description = ((name, type, ...), ...) — sqlite3 deja todo lower
        self._idx = {d[0].lower(): i for i, d in enumerate(description)}

    def __getattr__(self, name: str) -> Any:
        try:
            return self._row[self._idx[name.lower()]]
        except KeyError as e:
            raise AttributeError(name) from e

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._row[key]
        return self._row[self._idx[key.lower()]]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._row)

    def __len__(self) -> int:
        return len(self._row)

    def __repr__(self) -> str:
        return f"<AttrRow {tuple(self._row)}>"


# -------- Cursor wrapper (variadic params + auto-rewrite) --------
class SqliteCursor:
    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    def execute(self, sql: str, *params: Any) -> "SqliteCursor":
        sql = _rewrite_sql(sql)
        if len(params) == 1 and isinstance(params[0], (tuple, list)):
            self._cur.execute(sql, params[0])
        elif params:
            self._cur.execute(sql, params)
        else:
            self._cur.execute(sql)
        return self

    def executemany(self, sql: str, seq: Iterable[Any]) -> "SqliteCursor":
        sql = _rewrite_sql(sql)
        self._cur.executemany(sql, seq)
        return self

    def executescript(self, script: str) -> "SqliteCursor":
        # No reescribimos scripts (DDL los manejamos en SQL ya portable).
        self._cur.executescript(script)
        return self

    def fetchone(self) -> AttrRow | None:
        r = self._cur.fetchone()
        if r is None:
            return None
        return AttrRow(r, self._cur.description)

    def fetchall(self) -> list[AttrRow]:
        rows = self._cur.fetchall()
        if not rows:
            return []
        desc = self._cur.description
        return [AttrRow(r, desc) for r in rows]

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    # pyodbc compat — ignorado en sqlite (no hay fast_executemany)
    @property
    def fast_executemany(self) -> bool:
        return False

    @fast_executemany.setter
    def fast_executemany(self, value: bool) -> None:
        pass

    def close(self) -> None:
        self._cur.close()


class SqliteConn:
    """Wrap sqlite3.Connection para mantener la API de pyodbc + cierre en context manager."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw

    def cursor(self) -> SqliteCursor:
        return SqliteCursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def execute(self, sql: str, *params: Any) -> SqliteCursor:
        cur = self.cursor()
        cur.execute(sql, *params)
        return cur

    # context manager: cerramos al salir (commit/rollback lo hace el caller)
    def __enter__(self) -> "SqliteConn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is not None:
                self._raw.rollback()
        finally:
            self._raw.close()


# -------- Factories --------
def _open_sqlite() -> SqliteConn:
    raw = sqlite3.connect(
        _SQLITE_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        timeout=30.0,
    )
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    return SqliteConn(raw)


class MssqlCursor:
    """Wrap pyodbc cursor para reescribir SQL estilo SQLite (fpoc_tabla,
    datetime('now', '-N days')) a sintaxis MSSQL en cada execute/executemany."""

    def __init__(self, cur) -> None:
        self._cur = cur

    def execute(self, sql, *params):
        # Normalizar params a tupla plana (algunos callers pasan lista/tupla single).
        flat = tuple(params[0]) if (len(params) == 1 and isinstance(params[0], (tuple, list))) else tuple(params)
        sql, flat = _rewrite_sql_for_mssql(sql, flat)
        if flat:
            self._cur.execute(sql, flat)
        else:
            self._cur.execute(sql)
        return self

    def executemany(self, sql, seq):
        # executemany no entra en reordering de LIMIT — sería ambiguo. Solo
        # aplica las reescrituras de strings (sin tocar params).
        sql, _ = _rewrite_sql_for_mssql(sql, ())
        self._cur.executemany(sql, seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def description(self):
        return self._cur.description

    @property
    def fast_executemany(self):
        return self._cur.fast_executemany

    @fast_executemany.setter
    def fast_executemany(self, value):
        self._cur.fast_executemany = value

    def close(self):
        self._cur.close()


class MssqlConn:
    """Wrapper de pyodbc.Connection que aplica el rewrite SQLite->MSSQL en
    cada cursor.execute. Emula el shape de SqliteConn (context manager)."""

    def __init__(self, raw) -> None:
        self._raw = raw

    def cursor(self) -> MssqlCursor:
        return MssqlCursor(self._raw.cursor())

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    def execute(self, sql, *params):
        cur = self.cursor()
        cur.execute(sql, *params)
        return cur

    def __enter__(self) -> "MssqlConn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Alineado con SqliteConn (CR fixes-qa M3): NO commit automático.
        # Los callers ya tienen `cn.commit()` explícito en cada path de
        # escritura. Si una excepción burbujea, hacemos rollback.
        # Beneficio: comportamiento consistente entre backends, evita commits
        # silenciosos de transacciones a medio armar cuando un caller olvida
        # llamar a commit() y NO está en un path de exception.
        try:
            if exc_type is not None:
                self._raw.rollback()
        finally:
            self._raw.close()


def _open_sqlserver():
    import pyodbc  # import perezoso

    server = os.environ["DB_SERVER"].replace("tcp:", "")
    cs = (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )
    return MssqlConn(pyodbc.connect(cs, autocommit=False))


def get_conn():
    """Devuelve una conexión usable con `with`. SQLite por default."""
    if _BACKEND == "sqlite":
        return _open_sqlite()
    if _BACKEND == "sqlserver":
        return _open_sqlserver()
    raise RuntimeError(f"DB_BACKEND inválido: {_BACKEND!r} (usar 'sqlite' o 'sqlserver')")


def conn_str() -> str:
    """Compat: solo tiene sentido para sqlserver."""
    if _BACKEND == "sqlite":
        return f"sqlite:///{_SQLITE_PATH}"
    server = os.environ["DB_SERVER"].replace("tcp:", "")
    return (
        f"DRIVER={{{os.environ.get('DB_DRIVER', 'ODBC Driver 17 for SQL Server')}}};"
        f"SERVER={server};"
        f"DATABASE={os.environ['DB_NAME']};"
        f"UID={os.environ['DB_USER']};"
        f"PWD={os.environ['DB_PASSWORD']};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
    )


# IntegrityError compat (para `except db.IntegrityError`)
IntegrityError = sqlite3.IntegrityError if _BACKEND == "sqlite" else None
if _BACKEND == "sqlserver":
    import pyodbc as _pyodbc  # type: ignore
    IntegrityError = _pyodbc.IntegrityError


def backend() -> str:
    return _BACKEND


def sqlite_path() -> str:
    return _SQLITE_PATH
