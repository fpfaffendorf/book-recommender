"""
Recomendador de libros: experimento de variabilidad.

Le pide a Claude N veces la misma recomendación de 5 libros sobre el tema que
indique el prompt (por ejemplo, trading cuantitativo para principiantes en
2026), guarda cada respuesta en una base SQLite local y grafica cuántas veces
fue recomendado cada libro a lo largo de las iteraciones.

Uso:
    python book-recommender.py                 # abre la aplicación
    python book-recommender.py --db otra.db    # usa otra base de datos

Credenciales: el SDK de Anthropic las toma de la variable ANTHROPIC_API_KEY
(o de un perfil creado con `ant auth login`).
"""

from __future__ import annotations

import argparse
import json
import math
import queue
import re
import sqlite3
import threading
import tkinter as tk
import unicodedata
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import anthropic
import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.colors import ListedColormap
from matplotlib.figure import Figure
from matplotlib.text import Annotation
from matplotlib.ticker import FuncFormatter, MaxNLocator
from pydantic import BaseModel, Field, ValidationError

# --------------------------------------------------------------------------
# Configuración
# --------------------------------------------------------------------------

APP_TITLE = "Recomendaciones de libros"
DEFAULT_DB = Path(__file__).with_name("recomendaciones_libros.db")

MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "medium"
# Modelos que admiten el fallback del lado del servidor: si el modelo se niega
# a responder (stop_reason "refusal"), la API reintenta con otro modelo.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
BOOKS_PER_RUN = 5
MAX_RUNS_PER_BATCH = 500
MAX_WORKERS = 8
MAX_TOP_K = 30

# Búsqueda web (herramienta del servidor de Anthropic: cuesta USD 10 cada 1000 búsquedas, más los
# tokens de los resultados que lee el modelo).
WEB_SEARCH_MAX_USES = 10  # tope de búsquedas por ejecución
MAX_CONTINUATIONS = 5  # veces que se reanuda una investigación larga pausada (stop_reason "pause_turn")
EXTRACTION_EFFORT = "low"  # el paso de extracción solo transcribe el informe
# Modelos con "filtrado dinámico" de resultados (web_search_20260209); el resto usa la versión básica.
DYNAMIC_FILTER_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

# Instrucciones de formato: las agrega siempre la aplicación como mensaje de sistema, separadas
# del prompt que edita el usuario. Así, aunque el usuario cambie o borre parte de su prompt, cada
# respuesta sigue trayendo los datos que la app necesita. Además, la salida estructurada
# (output_format=BookList) obliga al modelo a respetar el esquema JSON.
SYSTEM_PROMPT = (
    "Sos un asesor bibliográfico. Recomendás libros sobre el tema que pida el usuario, "
    "adecuados al público, al nivel y al idioma que indique.\n"
    f"Sin importar cómo esté redactado el pedido, respondé con exactamente {BOOKS_PER_RUN} libros "
    "distintos, solo con el objeto JSON pedido. Reglas para cada libro:\n"
    "- Tiene que ser un libro real y ya publicado.\n"
    "- title: el título exacto, tal como figura en la portada de la edición que recomendás.\n"
    "- author: el autor o los autores, separados por coma.\n"
    "- isbn13: el ISBN-13 de esa misma edición, con 13 dígitos y sin guiones. Si el pedido indica "
    "un idioma, elegí una edición en ese idioma; si no, la edición impresa más reciente en el "
    "idioma original del libro."
)

# Con búsqueda web, cada ejecución tiene dos pasos (ver ask_claude):
# 1) investigación: el modelo busca en internet, analiza y escribe un informe con citas;
# 2) extracción: otra llamada transcribe el informe al JSON (salida estructurada).
RESEARCH_SYSTEM_PROMPT = (
    "Sos un asesor bibliográfico. Recomendás libros sobre el tema que pida el usuario, "
    "adecuados al público, al nivel y al idioma que indique.\n"
    "No respondas de memoria: antes de recomendar, investigá en internet con la herramienta de "
    "búsqueda web. Buscá recomendaciones actuales en fuentes variadas (especialistas, programas de "
    "cursos, reseñas, listas de lectura, editoriales), compará qué libros se repiten y por qué, y "
    "analizá cuáles se ajustan mejor al pedido. Verificá en la web (editorial, librería o catálogo "
    "de biblioteca) que cada libro existe y cuál es el ISBN-13 de la edición que recomendás.\n"
    f"Terminá con un informe que recomiende exactamente {BOOKS_PER_RUN} libros distintos. Para cada "
    "uno indicá: el título exacto tal como figura en la portada de esa edición, el autor o los "
    "autores, el ISBN-13 de esa misma edición (13 dígitos, sin guiones) y una justificación breve "
    "basada en lo que encontraste. Si el pedido indica un idioma, elegí ediciones en ese idioma; si "
    "no, la edición impresa más reciente en el idioma original del libro. Si no pudiste verificar "
    "un ISBN, decilo."
)
EXTRACTION_SYSTEM_PROMPT = (
    "Vas a recibir el informe de una investigación bibliográfica. Extraé los libros que el informe "
    "recomienda, en el mismo orden, copiando título, autor e ISBN-13 tal como aparecen en el "
    "informe. No agregues libros ni completes datos con lo que sepas: si el informe no da el "
    "ISBN-13 de un libro, dejá isbn13 vacío."
)


def instructions_for(web_search: bool) -> str:
    """Instrucciones fijas que agrega la app (se guardan con cada lote)."""
    if web_search:
        return f"[Investigación]\n{RESEARCH_SYSTEM_PROMPT}\n\n[Extracción]\n{EXTRACTION_SYSTEM_PROMPT}"
    return SYSTEM_PROMPT


# Prompt editable: solo el pedido en sí; el formato lo pone SYSTEM_PROMPT.
DEFAULT_PROMPT = (
    "Recomendame libros en inglés sobre trading cuantitativo para principiantes, "
    "para alguien que empieza a estudiar el tema en el año 2026."
)

# Paleta (modo claro). Un solo color de datos: la identidad de cada libro la da
# la etiqueta (o el panel), no el color.
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
ABSENT = "#f0efec"
ACCENT = "#2a78d6"
MAX_PANELS = 12  # paneles en la vista "Evolución acumulada"

matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["Segoe UI", "DejaVu Sans"]


# --------------------------------------------------------------------------
# Esquema de la respuesta (salida estructurada)
# --------------------------------------------------------------------------


class BookRecommendation(BaseModel):
    title: str = Field(description="Título exacto, como figura en la portada de la edición recomendada.")
    author: str = Field(description="Autor o autores, separados por coma.")
    isbn13: str = Field(description="ISBN-13 de esa misma edición: 13 dígitos sin guiones.")


class BookList(BaseModel):
    books: list[BookRecommendation] = Field(description=f"Exactamente {BOOKS_PER_RUN} libros distintos.")


# --------------------------------------------------------------------------
# Normalización: ISBN y clave de "obra"
# --------------------------------------------------------------------------


def _isbn13_checksum_ok(digits: str) -> bool:
    if len(digits) != 13 or not digits.isdigit():
        return False
    return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(digits)) % 10 == 0


def _isbn10_checksum_ok(digits: str) -> bool:
    if len(digits) != 10 or not digits[:9].isdigit() or not (digits[9].isdigit() or digits[9] == "X"):
        return False
    return sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits)) % 11 == 0


def _isbn10_to_13(digits: str) -> str:
    core = "978" + digits[:9]
    check = (10 - sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(core)) % 10) % 10
    return core + str(check)


def normalize_isbn(raw: str) -> tuple[str, bool]:
    """Devuelve (isbn, es_válido). Pasa ISBN-10 a ISBN-13 y valida el dígito verificador."""
    digits = re.sub(r"[^0-9X]", "", (raw or "").upper())
    if _isbn13_checksum_ok(digits) and digits.startswith(("978", "979")):
        return digits, True
    if _isbn10_checksum_ok(digits):
        return _isbn10_to_13(digits), True
    return digits, False


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


_SUBTITLE_RE = re.compile(r"\s+[-–—]\s+|[:(\[]")
_EDITION_RE = re.compile(
    # inglés: "2nd edition", "revised edition"
    r"\b(\d+(st|nd|rd|th)|first|second|third|fourth|fifth|sixth|revised|updated|expanded)\s+(edition|ed\.?)"
    # español: "2.ª edición", "segunda edición", "edición revisada"
    r"|\b(\d+\.?\s*(ª|º|a|ra|da|ta)?|primera|segunda|tercera|cuarta|quinta|sexta|nueva)\s+edici[oó]n"
    r"|\bedici[oó]n\s+(revisada|actualizada|ampliada|corregida)",
    re.IGNORECASE,
)
_NAME_SUFFIXES = {"jr", "sr", "phd", "dr", "cfa", "ii", "iii"}


def main_title(title: str) -> str:
    """Título sin subtítulo ni mención de edición: 'Quantitative Trading: How to...' -> 'Quantitative Trading'."""
    head = _SUBTITLE_RE.split(title, maxsplit=1)[0]
    head = _EDITION_RE.sub("", head).strip(" ,.;")
    return head or title.strip()


def first_author_surname(author: str) -> str:
    first = re.split(r",|;|&|\band\b|\by\b", author or "", maxsplit=1)[0]
    words = [w for w in slug(first).split() if w not in _NAME_SUFFIXES]
    return words[-1] if words else ""


def work_key(title: str, author: str) -> str:
    """Clave de la obra: agrupa ediciones distintas del mismo libro (distinto ISBN)."""
    return f"{slug(main_title(title))}|{first_author_surname(author)}"


@dataclass
class NormalizedBook:
    position: int
    title: str
    author: str
    isbn_raw: str
    isbn13: str
    isbn_valid: bool
    book_key: str  # identificador único del libro en la base: el ISBN-13 si es válido
    work_key: str


def normalize_books(items: list[BookRecommendation]) -> tuple[list[NormalizedBook], list[str]]:
    """Limpia la lista devuelta por el modelo. Devuelve (libros, avisos)."""
    books: list[NormalizedBook] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for item in items:
        title, author = item.title.strip(), item.author.strip()
        if not title:
            warnings.append("un libro vino sin título")
            continue
        isbn13, valid = normalize_isbn(item.isbn13)
        if not valid:
            warnings.append(f"ISBN inválido para «{main_title(title)}»: {item.isbn13!r}")
        key = isbn13 if valid else f"sin-isbn:{slug(title)}|{slug(author)}"
        if key in seen:
            warnings.append(f"libro repetido en la misma respuesta: «{main_title(title)}»")
            continue
        seen.add(key)
        books.append(
            NormalizedBook(
                position=len(books) + 1,
                title=title,
                author=author,
                isbn_raw=item.isbn13,
                isbn13=isbn13,
                isbn_valid=valid,
                book_key=key,
                work_key=work_key(title, author),
            )
        )
    if len(books) > BOOKS_PER_RUN:
        warnings.append(f"devolvió {len(books)} libros; se guardan los primeros {BOOKS_PER_RUN}")
        books = books[:BOOKS_PER_RUN]
    elif len(books) < BOOKS_PER_RUN:
        warnings.append(f"devolvió {len(books)} libros en lugar de {BOOKS_PER_RUN}")
    return books, warnings


# --------------------------------------------------------------------------
# Llamada a Claude
# --------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class RunResult:
    started_at: str
    model_requested: str
    finished_at: str = ""
    model_served: str | None = None
    status: str = "ok"  # "ok" | "error"
    error: str | None = None
    warning: str | None = None
    raw_response: str | None = None  # JSON con los libros
    input_tokens: int = 0  # sumados entre todas las llamadas de la ejecución
    output_tokens: int = 0
    request_id: str | None = None
    web_searches: int = 0
    research_text: str | None = None  # informe de la investigación (solo con búsqueda web)
    sources: list[dict] = field(default_factory=list)  # fuentes citadas: {"url", "title"}
    books: list[NormalizedBook] = field(default_factory=list)
    fatal: bool = False  # error de configuración: no tiene sentido seguir con el lote

    def fail(self, message: str, *, fatal: bool = False) -> RunResult:
        self.status, self.error, self.fatal = "error", message, fatal
        return self


class _RunFailed(Exception):
    """Corta la ejecución; el motivo ya quedó anotado en el RunResult."""


def ask_claude(client: anthropic.Anthropic, model: str, effort: str, prompt: str,
               web_search: bool = False) -> RunResult:
    """Hace una ejecución completa (sin historial previo) y normaliza la respuesta.

    Sin búsqueda web: una sola llamada con salida estructurada (JSON con los libros).
    Con búsqueda web: dos llamadas. (1) Investigación: Claude busca en internet, analiza y escribe
    un informe con citas. (2) Extracción: otra llamada pasa el informe a JSON. Se separan porque
    la búsqueda web siempre devuelve citas y la API rechaza las citas junto con la salida
    estructurada.
    """
    result = RunResult(started_at=now_iso(), model_requested=model)
    try:
        if web_search:
            report = _research(client, model, effort, prompt, result)
            parsed = _structured_books(client, model, EXTRACTION_EFFORT, EXTRACTION_SYSTEM_PROMPT,
                                       f"Informe:\n\n{report}", result, "extracción")
        else:
            parsed = _structured_books(client, model, effort, SYSTEM_PROMPT, prompt, result, "recomendación")
        result.books, warnings = normalize_books(parsed.books)
        if web_search and result.web_searches == 0:
            warnings.insert(0, "no hizo ninguna búsqueda web: respondió con lo que ya sabía")
        result.warning = "; ".join(warnings) or None
        if not result.books:
            result.fail("La respuesta no trajo ningún libro utilizable.")
    except _RunFailed:
        pass
    except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
        result.fail(f"{type(exc).__name__}: {exc}")
    except anthropic.APIStatusError as exc:
        # 4xx (salvo 408/409/429): credenciales, modelo inexistente, parámetros. Se repetiría en todo el lote.
        fatal = 400 <= exc.status_code < 500 and exc.status_code not in (408, 409, 429)
        result.fail(f"{type(exc).__name__} ({exc.status_code}): {exc.message}", fatal=fatal)
    except (ValidationError, ValueError) as exc:
        result.fail(f"La respuesta no respeta el formato pedido: {exc}")
    except anthropic.AnthropicError as exc:
        result.fail(f"{type(exc).__name__}: {exc}", fatal=True)
    except TypeError as exc:  # p. ej. el SDK no encontró credenciales
        result.fail(f"{type(exc).__name__}: {exc}", fatal=True)
    finally:
        result.finished_at = now_iso()
    return result


def _request_kwargs(model: str, effort: str) -> dict:
    kwargs: dict = dict(model=model, max_tokens=MAX_TOKENS, output_config={"effort": effort})
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def web_search_tool(model: str) -> dict:
    version = "web_search_20260209" if model in DYNAMIC_FILTER_MODELS else "web_search_20250305"
    return {"type": version, "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES}


def _fail(result: RunResult, message: str):
    result.fail(message)
    raise _RunFailed


def _record(result: RunResult, response) -> None:
    """Suma tokens y búsquedas de una respuesta y anota qué modelo respondió."""
    result.model_served = result.model_served or response.model
    result.request_id = result.request_id or getattr(response, "_request_id", None)
    usage = response.usage
    result.input_tokens += usage.input_tokens or 0
    result.output_tokens += usage.output_tokens or 0
    server_tools = getattr(usage, "server_tool_use", None)
    result.web_searches += (getattr(server_tools, "web_search_requests", None) or 0) if server_tools else 0


def _check_stop(result: RunResult, response, stage: str) -> None:
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        _fail(result, f"El modelo se negó a responder ({stage}; categoría: {category}).")
    if response.stop_reason == "max_tokens":
        _fail(result, f"La respuesta se cortó por max_tokens ({stage}).")


def _research(client, model: str, effort: str, prompt: str, result: RunResult) -> str:
    """Paso 1: Claude investiga en internet y escribe un informe con citas."""
    tools = [web_search_tool(model)]
    messages: list[dict] = [{"role": "user", "content": prompt}]
    texts: list[str] = []
    sources: dict[str, str] = {}
    for _ in range(MAX_CONTINUATIONS + 1):
        response = client.beta.messages.create(
            **_request_kwargs(model, effort), system=RESEARCH_SYSTEM_PROMPT, tools=tools, messages=messages,
        )
        _record(result, response)
        _check_stop(result, response, "investigación")
        for block in response.content:
            if block.type != "text":
                continue
            texts.append(block.text)
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if url and url not in sources:
                    sources[url] = getattr(citation, "title", None) or ""
        if response.stop_reason != "pause_turn":
            break
        # La API pausó una investigación larga: se reenvía lo recibido, sin cambios, para que siga.
        messages.append({"role": "assistant", "content": response.content})
    else:
        _fail(result, f"La investigación no terminó después de {MAX_CONTINUATIONS} reanudaciones.")
    result.research_text = "".join(texts).strip()
    result.sources = [{"url": url, "title": title} for url, title in sources.items()]
    if not result.research_text:
        _fail(result, "La investigación no devolvió ningún informe.")
    return result.research_text


def _structured_books(client, model: str, effort: str, system: str, content: str, result: RunResult,
                      stage: str) -> BookList:
    """Llamada con salida estructurada: el modelo devuelve el JSON con los libros."""
    response = client.beta.messages.parse(
        **_request_kwargs(model, effort), system=system, messages=[{"role": "user", "content": content}],
        output_format=BookList,
    )
    _record(result, response)
    result.raw_response = "".join(b.text for b in response.content if b.type == "text")
    _check_stop(result, response, stage)
    parsed = response.parsed_output
    if parsed is None:
        try:
            parsed = BookList.model_validate_json(result.raw_response or "")
        except ValidationError as exc:
            _fail(result, f"La respuesta no respeta el formato pedido ({stage}): {exc}")
    return parsed


# --------------------------------------------------------------------------
# Base de datos (SQLite)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    effort       TEXT    NOT NULL,
    prompt       TEXT    NOT NULL,            -- prompt del usuario
    n_requested  INTEGER NOT NULL,
    system_prompt TEXT,                       -- instrucciones fijas que agregó la app
    web_search   INTEGER NOT NULL DEFAULT 0   -- 1 si el modelo investigó en internet
);
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         INTEGER NOT NULL REFERENCES batches(id),
    started_at       TEXT    NOT NULL,
    finished_at      TEXT    NOT NULL,
    model_requested  TEXT    NOT NULL,
    model_served     TEXT,
    status           TEXT    NOT NULL CHECK (status IN ('ok', 'error')),
    error            TEXT,
    warning          TEXT,
    raw_response     TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    request_id       TEXT,
    web_searches     INTEGER,                   -- búsquedas web hechas en la ejecución
    research_text    TEXT,                      -- informe de la investigación
    sources          TEXT                       -- fuentes citadas (JSON: [{"url", "title"}])
);
CREATE TABLE IF NOT EXISTS books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_key        TEXT    NOT NULL UNIQUE,   -- ISBN-13 válido, o 'sin-isbn:título|autor'
    isbn13          TEXT,
    isbn_valid      INTEGER NOT NULL,
    title           TEXT    NOT NULL,
    author          TEXT    NOT NULL,
    work_key        TEXT    NOT NULL,          -- agrupa ediciones de la misma obra
    first_seen_run  INTEGER REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS recommendations (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    book_id     INTEGER NOT NULL REFERENCES books(id),
    position    INTEGER NOT NULL,
    title_raw   TEXT    NOT NULL,
    author_raw  TEXT    NOT NULL,
    isbn_raw    TEXT    NOT NULL,
    PRIMARY KEY (run_id, book_id)
);
CREATE INDEX IF NOT EXISTS idx_runs_batch ON runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_book ON recommendations(book_id);
CREATE INDEX IF NOT EXISTS idx_books_work ON books(work_key);
"""


@dataclass
class Group:
    """Un libro (agrupado por ISBN) o una obra (agrupada por título + autor)."""

    key: str
    title: str
    author: str
    isbns: list[str]
    isbn_status: str  # "válido" | "inválido" | "mixto"
    iterations: list[int]  # iteraciones (1..n) en las que fue recomendado

    @property
    def total(self) -> int:
        return len(self.iterations)


@dataclass
class Analysis:
    ok_run_ids: list[int]  # la iteración i corresponde a ok_run_ids[i - 1]
    groups: list[Group]  # ordenados de más a menos recomendado
    total_runs: int
    error_runs: int
    tokens_in: int
    tokens_out: int
    web_searches: int

    @property
    def n_iterations(self) -> int:
        return len(self.ok_run_ids)


# Un "experimento" se identifica por el texto del prompt y por si usó búsqueda web (0/1):
# así no se mezclan temas distintos ni respuestas de memoria con respuestas investigadas.
PromptKey = tuple[str, int]

# Columnas agregadas después de la primera versión: se suman a bases ya existentes.
MIGRATIONS = [
    ("batches", "system_prompt", "TEXT"),
    ("batches", "web_search", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "web_searches", "INTEGER"),
    ("runs", "research_text", "TEXT"),
    ("runs", "sources", "TEXT"),
]


class Database:
    """Cada operación abre su propia conexión, así se puede usar desde varios hilos."""

    def __init__(self, path: Path):
        self.path = Path(path)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            for table, column, declaration in MIGRATIONS:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # commit al salir, rollback si hay excepción
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _run_filter(batch_id: int | None, prompt_key: PromptKey | None) -> tuple[str, tuple]:
        """Filtro sobre runs (alias r): un lote puntual, o todos los lotes de un mismo prompt y modo."""
        if batch_id:
            return "AND r.batch_id = ?", (batch_id,)
        if prompt_key is not None:
            return ("AND r.batch_id IN (SELECT id FROM batches WHERE prompt = ? AND web_search = ?)",
                    tuple(prompt_key))
        return "", ()

    def create_batch(self, model: str, effort: str, prompt: str, n: int, web_search: bool = False) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO batches (created_at, model, effort, prompt, n_requested, system_prompt, web_search)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (now_iso(), model, effort, prompt, n, instructions_for(web_search), int(web_search)),
            )
            return cur.lastrowid

    def list_batches(self, prompt_key: PromptKey | None = None) -> list[sqlite3.Row]:
        where, params = ("WHERE b.prompt = ? AND b.web_search = ?", tuple(prompt_key)) if prompt_key else ("", ())
        with self._conn() as conn:
            return conn.execute(
                f"""SELECT b.*, COUNT(r.id) AS n_runs
                    FROM batches b LEFT JOIN runs r ON r.batch_id = b.id
                    {where}
                    GROUP BY b.id ORDER BY b.id DESC""",
                params,
            ).fetchall()

    def list_prompts(self) -> list[sqlite3.Row]:
        """Combinaciones distintas de prompt y modo (con o sin búsqueda web), de la más reciente a la más vieja."""
        with self._conn() as conn:
            return conn.execute(
                """SELECT b.prompt, b.web_search, COUNT(DISTINCT b.id) AS n_batches, COUNT(r.id) AS n_runs,
                          MAX(b.id) AS last_batch
                   FROM batches b LEFT JOIN runs r ON r.batch_id = b.id
                   GROUP BY b.prompt, b.web_search ORDER BY last_batch DESC"""
            ).fetchall()

    def latest_prompt_key(self) -> PromptKey | None:
        with self._conn() as conn:
            row = conn.execute("SELECT prompt, web_search FROM batches ORDER BY id DESC LIMIT 1").fetchone()
        return (row["prompt"], row["web_search"]) if row else None

    def save_run(self, batch_id: int, result: RunResult) -> int:
        with self._conn() as conn:
            run_id = conn.execute(
                """INSERT INTO runs (batch_id, started_at, finished_at, model_requested, model_served, status,
                                     error, warning, raw_response, input_tokens, output_tokens, request_id,
                                     web_searches, research_text, sources)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    batch_id, result.started_at, result.finished_at, result.model_requested, result.model_served,
                    result.status, result.error, result.warning, result.raw_response,
                    result.input_tokens, result.output_tokens, result.request_id,
                    result.web_searches, result.research_text,
                    json.dumps(result.sources, ensure_ascii=False) if result.sources else None,
                ),
            ).lastrowid
            for book in result.books:
                conn.execute(
                    """INSERT INTO books (book_key, isbn13, isbn_valid, title, author, work_key, first_seen_run)
                       VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (book_key) DO NOTHING""",
                    (book.book_key, book.isbn13 or None, int(book.isbn_valid), book.title, book.author,
                     book.work_key, run_id),
                )
                book_id = conn.execute("SELECT id FROM books WHERE book_key = ?", (book.book_key,)).fetchone()[0]
                conn.execute(
                    """INSERT OR IGNORE INTO recommendations
                       (run_id, book_id, position, title_raw, author_raw, isbn_raw) VALUES (?, ?, ?, ?, ?, ?)""",
                    (run_id, book_id, book.position, book.title, book.author, book.isbn_raw),
                )
            return run_id

    def analyze(self, batch_id: int | None = None, group_by: str = "work",
                prompt_key: PromptKey | None = None) -> Analysis:
        """Cuenta, para cada libro u obra, en qué iteraciones fue recomendado."""
        where, params = self._run_filter(batch_id, prompt_key)
        with self._conn() as conn:
            totals = conn.execute(
                f"""SELECT COUNT(*) AS total, COALESCE(SUM(status = 'error'), 0) AS errors,
                           COALESCE(SUM(input_tokens), 0) AS tin, COALESCE(SUM(output_tokens), 0) AS tout,
                           COALESCE(SUM(web_searches), 0) AS searches
                    FROM runs r WHERE 1 = 1 {where}""",
                params,
            ).fetchone()
            ok_run_ids = [
                row[0]
                for row in conn.execute(f"SELECT id FROM runs r WHERE status = 'ok' {where} ORDER BY id", params)
            ]
            rows = conn.execute(
                f"""SELECT rc.run_id, b.id AS book_id, b.book_key, b.work_key, b.title, b.author,
                           b.isbn13, b.isbn_valid
                    FROM recommendations rc
                    JOIN runs r  ON r.id = rc.run_id
                    JOIN books b ON b.id = rc.book_id
                    WHERE r.status = 'ok' {where}
                    ORDER BY rc.run_id, rc.position""",
                params,
            ).fetchall()

        iteration_of = {run_id: i for i, run_id in enumerate(ok_run_ids, start=1)}
        hits_per_book: Counter[int] = Counter()
        members: dict[str, dict[int, sqlite3.Row]] = {}
        iterations: dict[str, set[int]] = {}
        for row in rows:
            key = row["work_key"] if group_by == "work" else row["book_key"]
            members.setdefault(key, {})[row["book_id"]] = row
            iterations.setdefault(key, set()).add(iteration_of[row["run_id"]])
            hits_per_book[row["book_id"]] += 1

        groups = []
        for key, books in members.items():
            # Etiqueta del grupo: la variante (edición) más recomendada.
            label = max(books.values(), key=lambda b: (hits_per_book[b["book_id"]], -b["book_id"]))
            validity = {bool(b["isbn_valid"]) for b in books.values()}
            groups.append(
                Group(
                    key=key,
                    title=label["title"],
                    author=label["author"],
                    isbns=sorted({b["isbn13"] for b in books.values() if b["isbn13"]}),
                    isbn_status="válido" if validity == {True} else "inválido" if validity == {False} else "mixto",
                    iterations=sorted(iterations[key]),
                )
            )
        groups.sort(key=lambda g: (-g.total, g.iterations[0], g.title.lower()))
        return Analysis(
            ok_run_ids=ok_run_ids,
            groups=groups,
            total_runs=totals["total"],
            error_runs=totals["errors"],
            tokens_in=totals["tin"],
            tokens_out=totals["tout"],
            web_searches=totals["searches"],
        )

    def list_runs(self, batch_id: int | None = None, prompt_key: PromptKey | None = None,
                  limit: int = 2000) -> list[dict]:
        where, params = self._run_filter(batch_id, prompt_key)
        with self._conn() as conn:
            runs = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM runs r WHERE 1 = 1 {where} ORDER BY r.id DESC LIMIT ?", (*params, limit)
                )
            ]
            titles: dict[int, list[str]] = {}
            if runs:
                ids = [r["id"] for r in runs]
                for row in conn.execute(
                    f"""SELECT run_id, title_raw FROM recommendations
                        WHERE run_id IN ({",".join("?" * len(ids))}) ORDER BY run_id, position""",
                    ids,
                ):
                    titles.setdefault(row["run_id"], []).append(row["title_raw"])
        for run in runs:
            run["titles"] = titles.get(run["id"], [])
        return runs

    def run_detail(self, run_id: int) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        with self._conn() as conn:
            run = conn.execute(
                """SELECT r.*, b.prompt AS batch_prompt, b.web_search AS batch_web_search
                   FROM runs r JOIN batches b ON b.id = r.batch_id WHERE r.id = ?""",
                (run_id,),
            ).fetchone()
            books = conn.execute(
                """SELECT rc.position, rc.title_raw, rc.author_raw, rc.isbn_raw, b.isbn13, b.isbn_valid
                   FROM recommendations rc JOIN books b ON b.id = rc.book_id
                   WHERE rc.run_id = ? ORDER BY rc.position""",
                (run_id,),
            ).fetchall()
        return run, books


# --------------------------------------------------------------------------
# Ejecución de un lote en segundo plano
# --------------------------------------------------------------------------


class BatchRunner(threading.Thread):
    """Ejecuta N llamadas (con hasta `workers` en paralelo) y avisa a la interfaz por una cola."""

    def __init__(self, *, db, client, batch_id, model, effort, prompt, n, workers, events, cancel,
                 web_search=False):
        super().__init__(daemon=True, name=f"lote-{batch_id}")
        self.db, self.client, self.batch_id = db, client, batch_id
        self.model, self.effort, self.prompt, self.web_search = model, effort, prompt, web_search
        self.n, self.workers = n, workers
        self.events: queue.Queue = events
        self.cancel: threading.Event = cancel
        self._fatal_lock = threading.Lock()

    def run(self) -> None:
        try:
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="claude") as pool:
                futures = [pool.submit(self._one) for _ in range(self.n)]
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as exc:  # error inesperado (p. ej. de la base de datos)
                        self._report_fatal(f"Error inesperado: {exc!r}")
                        self.events.put(("skipped", None))
        finally:
            self.events.put(("finished", self.batch_id))

    def _one(self) -> None:
        if self.cancel.is_set():
            self.events.put(("skipped", None))
            return
        result = ask_claude(self.client, self.model, self.effort, self.prompt, web_search=self.web_search)
        run_id = self.db.save_run(self.batch_id, result)
        self.events.put(("run", (run_id, result)))
        if result.fatal:
            self._report_fatal(result.error or "Error de configuración.")

    def _report_fatal(self, message: str) -> None:
        with self._fatal_lock:
            if not self.cancel.is_set():
                self.cancel.set()
                self.events.put(("fatal", message))


# --------------------------------------------------------------------------
# Gráficos
# --------------------------------------------------------------------------


@dataclass
class HoverSpec:
    axes: list
    # (índice del eje, x, y en coordenadas de datos) -> (texto, x de la guía vertical o None)
    text_fn: Callable[[int, float, float], tuple[str, float | None] | None]
    crosshair: bool = False


def short_label(group: Group, width: int = 50) -> str:
    surname = group.author.split(",")[0].strip().split()[-1] if group.author.strip() else "?"
    text = f"{main_title(group.title)} ({surname})"
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def _style_axes(ax, grid_axis: str = "y") -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=AXIS, labelcolor=INK_2, labelsize=9)
    if grid_axis:
        ax.grid(axis=grid_axis, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def _title(ax, text: str) -> None:
    ax.set_title(text, loc="left", fontsize=12, color=INK, pad=12)


def draw_empty(fig: Figure, message: str) -> None:
    fig.clear()
    fig.text(0.5, 0.5, message, ha="center", va="center", fontsize=12, color=INK_2)


def draw_cumulative(fig: Figure, analysis: Analysis, top_k: int) -> HoverSpec:
    """Un panel por libro: cuántas veces fue recomendado hasta cada iteración.

    Se usan paneles separados (y no líneas superpuestas) porque los libros que el modelo
    recomienda siempre tienen exactamente la misma curva y se taparían entre sí.
    """
    fig.clear()
    n = analysis.n_iterations
    groups = analysis.groups[: min(top_k, MAX_PANELS)]
    cols = 1 if len(groups) == 1 else 2 if len(groups) <= 4 else 3 if len(groups) <= 9 else 4
    rows = math.ceil(len(groups) / cols)
    axes = list(fig.subplots(rows, cols, sharex=True, sharey=True, squeeze=False).ravel())
    xs = list(range(n + 1))
    label_width = {1: 90, 2: 60, 3: 42, 4: 32}[cols]
    curves: list[list[int]] = []
    for idx, (ax, group) in enumerate(zip(axes, groups)):
        _style_axes(ax)
        hits, count, ys = set(group.iterations), 0, []
        for i in xs:
            count += i in hits
            ys.append(count)
        curves.append(ys)
        ax.plot([0, n], [0, n], color=MUTED, linewidth=1, linestyle=(0, (4, 4)), zorder=1)
        ax.step(xs, ys, where="post", color=ACCENT, linewidth=2, zorder=3)
        ax.set_title(short_label(group, label_width), loc="left", fontsize=9, color=INK)
        # Arriba a la izquierda siempre hay lugar: la curva nunca supera la diagonal.
        ax.text(0.03, 0.95, f"{group.total}/{n} · {100 * group.total / n:.0f}%", transform=ax.transAxes,
                ha="left", va="top", fontsize=9, color=INK_2)
        if idx + cols >= len(groups):  # último panel de su columna: mostrar los números del eje x
            ax.tick_params(labelbottom=True)
    for ax in axes[len(groups):]:
        ax.set_visible(False)
    axes = axes[: len(groups)]
    axes[0].set_xlim(0, n)
    axes[0].set_ylim(0, n * 1.06)
    axes[0].xaxis.set_major_locator(MaxNLocator(integer=True, nbins=6))
    axes[0].yaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))
    fig.supxlabel("Iteración (respuesta válida n.º)", fontsize=9, color=INK_2)
    fig.supylabel("Veces recomendado (acumulado)", fontsize=9, color=INK_2)
    fig.suptitle(
        f"Veces recomendado hasta cada iteración · {len(groups)} más recomendados de {len(analysis.groups)}"
        " · punteada = recomendado en todas",
        x=0.01, ha="left", fontsize=12, color=INK,
    )

    def text_fn(idx: int, x: float, _y: float):
        i = min(max(int(round(x)), 1), n)
        group, ys = groups[idx], curves[idx]
        state = "recomendado" if i in set(group.iterations) else "no recomendado"
        return (
            f"{group.title}\n{group.author}\n"
            f"Iteración {i} (ejecución #{analysis.ok_run_ids[i - 1]}): {state}\n"
            f"Acumulado: {ys[i]} de {i} iteraciones",
            i,
        )

    return HoverSpec(axes, text_fn, crosshair=True)


def draw_presence(fig: Figure, analysis: Analysis, top_k: int) -> HoverSpec:
    """Matriz libro × iteración: celda coloreada si el libro fue recomendado en esa iteración."""
    fig.clear()
    ax = fig.add_subplot()
    _style_axes(ax, grid_axis="")
    n = analysis.n_iterations
    groups = analysis.groups[:top_k]
    hit_sets = [set(g.iterations) for g in groups]
    matrix = [[1 if i in hits else 0 for i in range(1, n + 1)] for hits in hit_sets]

    ax.imshow(matrix, aspect="auto", interpolation="nearest", vmin=0, vmax=1,
              cmap=ListedColormap([ABSENT, ACCENT]))
    if n <= 80:  # separación de 2 px entre celdas, mientras quepa
        ax.set_xticks([x - 0.5 for x in range(n + 1)], minor=True)
        ax.set_yticks([y - 0.5 for y in range(len(groups) + 1)], minor=True)
        ax.grid(which="minor", color=SURFACE, linewidth=2)
        ax.tick_params(which="minor", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_yticks(range(len(groups)), [short_label(g) for g in groups])
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=20))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v) + 1}"))
    ax.set_xlabel("Iteración (respuesta válida n.º)", color=INK_2, fontsize=9)
    for row, group in enumerate(groups):
        ax.annotate(f"{group.total}", xy=(1.0, row), xycoords=("axes fraction", "data"), xytext=(8, 0),
                    textcoords="offset points", va="center", fontsize=9, color=INK_2)
    _title(ax, f"Presencia por iteración · azul = recomendado · total a la derecha ({len(groups)} de {len(analysis.groups)})")

    def text_fn(_idx: int, x: float, y: float):
        col, row = int(round(x)), int(round(y))
        if not (0 <= col < n and 0 <= row < len(groups)):
            return None
        group = groups[row]
        state = "recomendado" if (col + 1) in hit_sets[row] else "no recomendado"
        return (
            f"{group.title}\n{group.author}\n"
            f"Iteración {col + 1} (ejecución #{analysis.ok_run_ids[col]}): {state}\n"
            f"Total: {group.total} de {n} iteraciones",
            None,
        )

    return HoverSpec([ax], text_fn)


def draw_ranking(fig: Figure, analysis: Analysis, top_k: int) -> HoverSpec:
    """Barras horizontales: en cuántas iteraciones fue recomendado cada libro."""
    fig.clear()
    ax = fig.add_subplot()
    _style_axes(ax, grid_axis="x")
    n = analysis.n_iterations
    groups = analysis.groups[:top_k]
    ax.barh(range(len(groups)), [g.total for g in groups], height=0.7, color=ACCENT, zorder=3)
    ax.set_yticks(range(len(groups)), [short_label(g) for g in groups])
    ax.invert_yaxis()
    ax.set_xlim(0, max(n, 1) * 1.14)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.tick_params(axis="y", length=0)
    ax.spines["left"].set_visible(False)
    ax.set_xlabel("Iteraciones en las que fue recomendado", color=INK_2, fontsize=9)
    for row, group in enumerate(groups):
        ax.annotate(f"{group.total} ({100 * group.total / max(n, 1):.0f}%)", xy=(group.total, row),
                    xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK_2)
    _title(ax, f"Ranking · veces recomendado en {n} iteraciones ({len(groups)} de {len(analysis.groups)})")

    def text_fn(_idx: int, _x: float, y: float):
        row = int(round(y))
        if not 0 <= row < len(groups):
            return None
        group = groups[row]
        isbns = ", ".join(group.isbns) or "—"
        return (
            f"{group.title}\n{group.author}\nISBN: {isbns} ({group.isbn_status})\n"
            f"Recomendado en {group.total} de {n} iteraciones ({100 * group.total / max(n, 1):.0f}%)",
            None,
        )

    return HoverSpec([ax], text_fn)


VIEWS = {
    "cumulative": ("Evolución acumulada", draw_cumulative),
    "presence": ("Presencia por iteración", draw_presence),
    "ranking": ("Ranking total", draw_ranking),
}


# --------------------------------------------------------------------------
# Interfaz (Tkinter)
# --------------------------------------------------------------------------

ALL_BATCHES = "Todos los lotes"
ALL_PROMPTS = "Todos los prompts (mezcla temas distintos)"


class App:
    def __init__(self, root: tk.Tk, db: Database):
        self.root, self.db = root, db
        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.runner: BatchRunner | None = None
        self.client: anthropic.Anthropic | None = None
        # Filtros: las etiquetas de los combos se arman a partir de estos dos valores.
        self.selected_prompt: PromptKey | None = db.latest_prompt_key()  # None = todos los prompts
        self.selected_batch: int | None = None  # None = todos los lotes del prompt
        self.prompt_options: dict[str, PromptKey | None] = {ALL_PROMPTS: None}
        self.batch_ids: dict[str, int | None] = {ALL_BATCHES: None}
        self.analysis: Analysis | None = None
        self.hover: HoverSpec | None = None
        self._annot: Annotation | None = None
        self._vlines: list = []
        self._progress = {"done": 0, "ok": 0, "error": 0, "n": 0, "batch": None}
        self._fatal_message: str | None = None

        root.title(APP_TITLE)
        root.geometry("1320x880")
        root.minsize(1000, 700)

        self.model_var = tk.StringVar(value=MODELS[0])
        self.effort_var = tk.StringVar(value=DEFAULT_EFFORT)
        self.n_var = tk.StringVar(value="10")
        self.workers_var = tk.StringVar(value="3")
        self.web_var = tk.BooleanVar(value=True)
        self.batch_var = tk.StringVar(value=ALL_BATCHES)
        self.prompt_filter_var = tk.StringVar(value=ALL_PROMPTS)
        self.group_var = tk.StringVar(value="work")
        self.view_var = tk.StringVar(value="cumulative")
        self.topk_var = tk.StringVar(value="8")
        self.status_var = tk.StringVar(value="Listo.")
        self.summary_var = tk.StringVar()

        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_all()
        root.after(250, self._poll_events)

    # ---------------------------------------------------------------- layout
    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Summary.TLabel", foreground=INK_2)
        style.configure("Treeview", rowheight=24)

        run_bar = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        run_bar.pack(fill="x")
        ttk.Label(run_bar, text="Modelo").pack(side="left")
        ttk.Combobox(run_bar, textvariable=self.model_var, values=MODELS, width=20).pack(side="left", padx=(4, 14))
        ttk.Label(run_bar, text="Esfuerzo").pack(side="left")
        ttk.Combobox(run_bar, textvariable=self.effort_var, values=EFFORTS, width=8, state="readonly").pack(
            side="left", padx=(4, 14))
        ttk.Label(run_bar, text="Ejecuciones (N)").pack(side="left")
        ttk.Spinbox(run_bar, from_=1, to=MAX_RUNS_PER_BATCH, textvariable=self.n_var, width=6).pack(
            side="left", padx=(4, 14))
        ttk.Label(run_bar, text="En paralelo").pack(side="left")
        ttk.Spinbox(run_bar, from_=1, to=MAX_WORKERS, textvariable=self.workers_var, width=4).pack(
            side="left", padx=(4, 14))
        self.run_button = ttk.Button(run_bar, text="▶ Ejecutar", command=self.start_batch)
        self.run_button.pack(side="left")
        self.cancel_button = ttk.Button(run_bar, text="■ Cancelar", command=self.cancel_batch, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(run_bar, text="Buscar en internet", variable=self.web_var).pack(side="left", padx=(14, 0))

        prompt_box = ttk.LabelFrame(self.root, text="Prompt (se envía igual, sin historial, en cada ejecución)",
                                    padding=(8, 4, 8, 8))
        prompt_box.pack(fill="x", padx=12, pady=(4, 4))
        note = ttk.Frame(prompt_box)
        note.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Label(note, style="Summary.TLabel",
                  text="Escribí el tema, el público y el idioma. La aplicación agrega por su cuenta el formato "
                       f"({BOOKS_PER_RUN} libros con título, autor e ISBN-13) y la instrucción de investigar "
                       "en internet.").pack(side="left")
        ttk.Button(note, text="Ver instrucciones fijas", command=self._show_system_prompt).pack(
            side="left", padx=(8, 0))
        self.prompt_text = tk.Text(prompt_box, height=3, wrap="word", font=("Segoe UI", 10), relief="flat",
                                   highlightthickness=1, highlightbackground=AXIS)
        self.prompt_text.insert("1.0", DEFAULT_PROMPT)
        self.prompt_text.pack(side="left", fill="x", expand=True)
        ttk.Button(prompt_box, text="Restaurar", command=self._reset_prompt).pack(side="left", padx=(8, 0))

        progress_bar = ttk.Frame(self.root, padding=(12, 2, 12, 6))
        progress_bar.pack(fill="x")
        self.progress = ttk.Progressbar(progress_bar, mode="determinate", length=320)
        self.progress.pack(side="left")
        ttk.Label(progress_bar, textvariable=self.status_var).pack(side="left", padx=(10, 0))

        ttk.Separator(self.root).pack(fill="x", padx=12, pady=(2, 6))

        prompt_row = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        prompt_row.pack(fill="x")
        ttk.Label(prompt_row, text="Prompt analizado").pack(side="left")
        self.prompt_combo = ttk.Combobox(prompt_row, textvariable=self.prompt_filter_var, state="readonly")
        self.prompt_combo.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.prompt_combo.bind("<<ComboboxSelected>>", self._on_prompt_selected)

        filters = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        filters.pack(fill="x")
        ttk.Label(filters, text="Lote").pack(side="left")
        self.batch_combo = ttk.Combobox(filters, textvariable=self.batch_var, width=58, state="readonly")
        self.batch_combo.pack(side="left", padx=(4, 14))
        self.batch_combo.bind("<<ComboboxSelected>>", self._on_batch_selected)
        ttk.Label(filters, text="Contar por").pack(side="left")
        ttk.Radiobutton(filters, text="Obra (título + autor)", value="work", variable=self.group_var,
                        command=self.refresh_all).pack(side="left", padx=(4, 0))
        ttk.Radiobutton(filters, text="ISBN", value="isbn", variable=self.group_var,
                        command=self.refresh_all).pack(side="left", padx=(6, 14))
        ttk.Label(filters, text="Mostrar top").pack(side="left")
        topk = ttk.Spinbox(filters, from_=1, to=MAX_TOP_K, textvariable=self.topk_var, width=4,
                           command=self._redraw_chart)
        topk.pack(side="left", padx=(4, 0))
        topk.bind("<Return>", lambda _e: self._redraw_chart())
        topk.bind("<FocusOut>", lambda _e: self._redraw_chart())

        ttk.Label(self.root, textvariable=self.summary_var, style="Summary.TLabel", padding=(12, 0, 12, 6)).pack(
            fill="x")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        notebook.add(self._build_chart_tab(notebook), text="Gráfico")
        notebook.add(self._build_books_tab(notebook), text="Libros")
        notebook.add(self._build_runs_tab(notebook), text="Ejecuciones")

    def _build_chart_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=6)
        views = ttk.Frame(frame)
        views.pack(fill="x", pady=(0, 4))
        ttk.Label(views, text="Vista").pack(side="left")
        for value, (label, _fn) in VIEWS.items():
            ttk.Radiobutton(views, text=label, value=value, variable=self.view_var,
                            command=self._redraw_chart).pack(side="left", padx=(8, 0))
        ttk.Label(views, text="Pasá el mouse sobre el gráfico para ver los valores.",
                  style="Summary.TLabel").pack(side="right")

        # El lienzo va en un contenedor que no propaga su tamaño pedido: con la pantalla escalada
        # (p. ej. 125 %) matplotlib pide un lienzo más grande que el espacio y agrandaría la ventana.
        toolbar_frame = ttk.Frame(frame)
        toolbar_frame.pack(side="bottom", fill="x")
        holder = ttk.Frame(frame, width=600, height=400)
        holder.pack(fill="both", expand=True)
        holder.pack_propagate(False)
        self.figure = Figure(figsize=(6, 4), dpi=100, layout="constrained", facecolor=SURFACE)
        self.canvas = FigureCanvasTkAgg(self.figure, master=holder)
        toolbar = NavigationToolbar2Tk(self.canvas, toolbar_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side="left", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        return frame

    def _build_books_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=6)
        columns = {
            "rank": ("#", 40, "e"),
            "title": ("Título", 380, "w"),
            "author": ("Autor", 200, "w"),
            "isbn": ("ISBN-13", 230, "w"),
            "valid": ("ISBN", 90, "w"),
            "count": ("Veces", 60, "e"),
            "pct": ("% iteraciones", 100, "e"),
            "first": ("Primera iteración", 120, "e"),
        }
        self.books_tree = self._make_tree(frame, columns)
        return frame

    def _build_runs_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=6)
        paned = ttk.Panedwindow(frame, orient="vertical")
        paned.pack(fill="both", expand=True)
        top = ttk.Frame(paned)
        columns = {
            "run": ("Ejecución", 75, "e"),
            "iter": ("Iteración", 70, "e"),
            "batch": ("Lote", 50, "e"),
            "time": ("Fecha", 150, "w"),
            "model": ("Modelo", 140, "w"),
            "status": ("Estado", 70, "w"),
            "books": ("Libros recomendados", 420, "w"),
            "note": ("Avisos / error", 220, "w"),
        }
        self.runs_tree = self._make_tree(top, columns)
        self.runs_tree.bind("<<TreeviewSelect>>", self._show_run_detail)
        bottom = ttk.Frame(paned)
        self.detail_text = tk.Text(bottom, height=10, wrap="word", font=("Consolas", 9), relief="flat",
                                   highlightthickness=1, highlightbackground=AXIS)
        self.detail_text.pack(fill="both", expand=True, pady=(6, 0))
        self.detail_text.insert("1.0", "Elegí una ejecución para ver su respuesta completa.")
        self.detail_text.configure(state="disabled")
        paned.add(top, weight=3)
        paned.add(bottom, weight=1)
        return frame

    @staticmethod
    def _make_tree(parent, columns: dict) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=list(columns), show="headings")
        for key, (heading, width, anchor) in columns.items():
            tree.heading(key, text=heading, anchor=anchor)
            tree.column(key, width=width, anchor=anchor, stretch=key in ("title", "books", "note"))
        scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        return tree

    # ------------------------------------------------------------- acciones
    def _reset_prompt(self) -> None:
        self.prompt_text.delete("1.0", "end")
        self.prompt_text.insert("1.0", DEFAULT_PROMPT)

    def _show_system_prompt(self) -> None:
        if self.web_var.get():
            intro = ("Con «Buscar en internet» cada ejecución hace dos llamadas: la investigación (con búsqueda "
                     f"web, hasta {WEB_SEARCH_MAX_USES} búsquedas) y la extracción del informe a JSON. "
                     "Instrucciones que agrega la aplicación además de tu prompt:")
        else:
            intro = "Instrucciones que la aplicación envía en cada ejecución, además de tu prompt:"
        messagebox.showinfo(APP_TITLE, f"{intro}\n\n{instructions_for(self.web_var.get())}")

    @staticmethod
    def _read_int(var: tk.StringVar, low: int, high: int) -> int | None:
        try:
            value = int(var.get())
        except ValueError:
            return None
        return value if low <= value <= high else None

    def start_batch(self) -> None:
        if self.runner and self.runner.is_alive():
            return
        prompt = self.prompt_text.get("1.0", "end").strip()
        model = self.model_var.get().strip()
        n = self._read_int(self.n_var, 1, MAX_RUNS_PER_BATCH)
        workers = self._read_int(self.workers_var, 1, MAX_WORKERS)
        if not prompt or not model:
            messagebox.showerror(APP_TITLE, "Completá el modelo y el prompt.")
            return
        if n is None or workers is None:
            messagebox.showerror(APP_TITLE, f"N tiene que estar entre 1 y {MAX_RUNS_PER_BATCH}, "
                                            f"y «En paralelo» entre 1 y {MAX_WORKERS}.")
            return
        if self.client is None:
            try:
                self.client = anthropic.Anthropic(max_retries=4)
            except Exception as exc:  # noqa: BLE001 - se muestra al usuario
                messagebox.showerror(APP_TITLE, f"No se pudo crear el cliente de Anthropic:\n{exc}\n\n"
                                                "Definí la variable de entorno ANTHROPIC_API_KEY y volvé a abrir la app.")
                return

        web_search = self.web_var.get()
        batch_id = self.db.create_batch(model, self.effort_var.get(), prompt, n, web_search=web_search)
        # Mostrar lo acumulado para este prompt y modo (incluye lotes anteriores iguales).
        self.selected_prompt, self.selected_batch = (prompt, int(web_search)), None
        self.cancel_event = threading.Event()
        self._fatal_message = None
        self._progress = {"done": 0, "ok": 0, "error": 0, "n": n, "batch": batch_id}
        self.progress.configure(maximum=n, value=0)
        self._set_running(True)
        self._update_status()
        self.runner = BatchRunner(
            db=self.db, client=self.client, batch_id=batch_id, model=model, effort=self.effort_var.get(),
            prompt=prompt, n=n, workers=workers, events=self.events, cancel=self.cancel_event,
            web_search=web_search,
        )
        self.runner.start()
        self.refresh_all()

    def cancel_batch(self) -> None:
        self.cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.status_var.set(self.status_var.get() + " · cancelando (se esperan las llamadas en curso)…")

    def _set_running(self, running: bool) -> None:
        self.run_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _update_status(self, finished: bool = False) -> None:
        p = self._progress
        text = f"Lote {p['batch']}: {p['done']}/{p['n']} · válidas {p['ok']} · con error {p['error']}"
        if finished:
            skipped = p["n"] - p["ok"] - p["error"]
            text += f" · canceladas {skipped}" if skipped else ""
            text = "Terminado. " + text
        self.status_var.set(text)

    def _poll_events(self) -> None:
        changed, finished = False, False
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "run":
                    _run_id, result = payload
                    self._progress["done"] += 1
                    self._progress["ok" if result.status == "ok" else "error"] += 1
                    changed = True
                elif kind == "skipped":
                    self._progress["done"] += 1
                elif kind == "fatal":
                    self._fatal_message = payload
                elif kind == "finished":
                    finished = True
        except queue.Empty:
            pass

        if changed or finished:
            self.progress.configure(value=self._progress["done"])
            self._update_status(finished=finished)
        if changed:
            self.refresh_all()
        if finished:
            self._set_running(False)
        if self._fatal_message:
            message, self._fatal_message = self._fatal_message, None
            messagebox.showerror(APP_TITLE, f"Se detuvo el lote:\n\n{message}")
        self.root.after(250, self._poll_events)

    def _on_close(self) -> None:
        if self.runner and self.runner.is_alive():
            if not messagebox.askyesno(APP_TITLE, "Hay un lote en curso. ¿Cancelarlo y salir?\n"
                                                  "Las llamadas que ya empezaron terminan y se guardan."):
                return
            self.cancel_event.set()
        self.root.destroy()

    # ------------------------------------------------------------ refresco
    def _on_prompt_selected(self, _event=None) -> None:
        self.selected_prompt = self.prompt_options.get(self.prompt_filter_var.get())
        self.selected_batch = None
        self.refresh_all()

    def _on_batch_selected(self, _event=None) -> None:
        self.selected_batch = self.batch_ids.get(self.batch_var.get())
        self.refresh_all()

    def _refresh_filters(self) -> None:
        self.prompt_options = {ALL_PROMPTS: None}
        for p in self.db.list_prompts():
            text = " ".join(p["prompt"].split())
            text = text if len(text) <= 110 else text[:109] + "…"
            mode = "con búsqueda web" if p["web_search"] else "sin búsqueda web"
            batches = f"{p['n_batches']} lote{'s' if p['n_batches'] != 1 else ''}"
            label = f"«{text}» · {mode} · {batches} · {p['n_runs']} ejec."
            if label in self.prompt_options:  # dos prompts que se ven iguales al recortarlos
                label += f" · último lote {p['last_batch']}"
            self.prompt_options[label] = (p["prompt"], p["web_search"])
        if self.selected_prompt not in self.prompt_options.values():
            self.selected_prompt = None
        self.prompt_combo.configure(values=list(self.prompt_options))
        self.prompt_filter_var.set(next(k for k, v in self.prompt_options.items() if v == self.selected_prompt))

        self.batch_ids = {ALL_BATCHES: None}
        for b in self.db.list_batches(self.selected_prompt):
            label = (f"Lote {b['id']} · {b['created_at'].replace('T', ' ')} · {b['model']} · "
                     f"esfuerzo {b['effort']} · {b['n_runs']}/{b['n_requested']} ejec.")
            self.batch_ids[label] = b["id"]
        if self.selected_batch not in self.batch_ids.values():
            self.selected_batch = None
        self.batch_combo.configure(values=list(self.batch_ids))
        self.batch_var.set(next(k for k, v in self.batch_ids.items() if v == self.selected_batch))

    def refresh_all(self) -> None:
        self._refresh_filters()
        self.analysis = self.db.analyze(self.selected_batch, self.group_var.get(), prompt_key=self.selected_prompt)
        a = self.analysis
        unit = "Obras distintas" if self.group_var.get() == "work" else "ISBN distintos"
        self.summary_var.set(
            f"Ejecuciones: {a.total_runs} (válidas {a.n_iterations} · con error {a.error_runs}) · "
            f"{unit}: {len(a.groups)} · Búsquedas web: {a.web_searches:,} · "
            f"Tokens: {a.tokens_in:,} entrada / {a.tokens_out:,} salida".replace(",", ".")
        )
        self._redraw_chart()
        self._fill_books()
        self._fill_runs()

    def _redraw_chart(self) -> None:
        if self.analysis is None:
            return
        top_k = self._read_int(self.topk_var, 1, MAX_TOP_K) or 8
        self.hover, self._annot, self._vlines = None, None, []
        if self.analysis.n_iterations == 0:
            draw_empty(self.figure, "Todavía no hay respuestas válidas en esta selección.\n"
                                    "Elegí N y apretá «Ejecutar».")
        else:
            _label, draw = VIEWS[self.view_var.get()]
            self.hover = draw(self.figure, self.analysis, top_k)
            # Tooltip a nivel figura (en píxeles), así queda por encima de todos los paneles.
            self._annot = Annotation(
                "", xy=(0, 0), xycoords="figure pixels", xytext=(14, 14), textcoords="offset points",
                fontsize=9, color=INK, zorder=10, visible=False, multialignment="left",
                bbox=dict(boxstyle="round,pad=0.6", facecolor="white", edgecolor=AXIS, linewidth=0.8),
            )
            self.figure.add_artist(self._annot)
            if self.hover.crosshair:
                self._vlines = [ax.axvline(0, color=MUTED, linewidth=1, zorder=2, visible=False)
                                for ax in self.hover.axes]
        self.canvas.draw_idle()

    def _hide_hover(self) -> None:
        if self._annot is not None and self._annot.get_visible():
            self._annot.set_visible(False)
            for line in self._vlines:
                line.set_visible(False)
            self.canvas.draw_idle()

    def _on_motion(self, event) -> None:
        spec, annot = self.hover, self._annot
        if spec is None or annot is None:
            return
        if event.inaxes not in spec.axes or event.xdata is None:
            self._hide_hover()
            return
        idx = spec.axes.index(event.inaxes)
        found = spec.text_fn(idx, event.xdata, event.ydata)
        if not found:
            self._hide_hover()
            return
        text, snap_x = found
        x_px = event.x
        if snap_x is not None:
            x_px = event.inaxes.transData.transform((snap_x, event.ydata))[0]
        bbox = self.figure.bbox
        right, top = event.x > bbox.width * 0.55, event.y > bbox.height * 0.5
        annot.xy = (x_px, event.y)
        annot.xyann = (-14 if right else 14, -14 if top else 14)
        annot.set_horizontalalignment("right" if right else "left")
        annot.set_verticalalignment("top" if top else "bottom")
        annot.set_text(text)
        annot.set_visible(True)
        for i, line in enumerate(self._vlines):
            line.set_visible(i == idx and snap_x is not None)
            if i == idx and snap_x is not None:
                line.set_xdata([snap_x, snap_x])
        self.canvas.draw_idle()

    def _fill_books(self) -> None:
        tree, a = self.books_tree, self.analysis
        tree.delete(*tree.get_children())
        n = max(a.n_iterations, 1)
        icons = {"válido": "✓ válido", "inválido": "✗ inválido", "mixto": "± mixto"}
        for rank, g in enumerate(a.groups, start=1):
            tree.insert("", "end", values=(
                rank, g.title, g.author, ", ".join(g.isbns) or "—", icons[g.isbn_status],
                g.total, f"{100 * g.total / n:.0f}%", g.iterations[0],
            ))

    def _fill_runs(self) -> None:
        tree = self.runs_tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        iteration_of = {run_id: i for i, run_id in enumerate(self.analysis.ok_run_ids, start=1)}
        for run in self.db.list_runs(self.selected_batch, prompt_key=self.selected_prompt):
            model = run["model_served"] or run["model_requested"]
            tree.insert("", "end", iid=str(run["id"]), values=(
                run["id"], iteration_of.get(run["id"], "—"), run["batch_id"],
                run["finished_at"].replace("T", " "), model, run["status"],
                " | ".join(main_title(t) for t in run["titles"]), run["error"] or run["warning"] or "",
            ))
        keep = [iid for iid in selected if tree.exists(iid)]
        if keep:
            tree.selection_set(keep)

    def _show_run_detail(self, _event=None) -> None:
        selection = self.runs_tree.selection()
        if not selection:
            return
        run, books = self.db.run_detail(int(selection[0]))
        if run is None:
            return
        served = f" (respondió {run['model_served']})" if run["model_served"] and run["model_served"] != run[
            "model_requested"] else ""
        lines = [
            f"Ejecución #{run['id']} · lote {run['batch_id']} · {run['model_requested']}{served} · "
            f"{run['started_at'].replace('T', ' ')} → {run['finished_at'][11:]}",
            f"Prompt: {run['batch_prompt']}",
            f"Estado: {run['status']} · tokens {run['input_tokens'] or 0} entrada / {run['output_tokens'] or 0} salida"
            f" · request-id {run['request_id'] or '—'}",
        ]
        if run["batch_web_search"]:
            lines.append(f"Búsquedas web: {run['web_searches'] or 0}")
        if run["error"]:
            lines.append(f"Error: {run['error']}")
        if run["warning"]:
            lines.append(f"Avisos: {run['warning']}")
        lines.append("")
        for b in books:
            check = "✓" if b["isbn_valid"] else "✗ inválido"
            lines.append(f"{b['position']}. {b['title_raw']} — {b['author_raw']} — ISBN {b['isbn_raw']} {check}")
        sources = json.loads(run["sources"]) if run["sources"] else []
        if sources:
            lines += ["", "Fuentes citadas:"] + [f"- {s['title'] or s['url']}: {s['url']}" for s in sources]
        if run["research_text"]:
            lines += ["", "Informe de la investigación:", run["research_text"]]
        if run["raw_response"]:
            label = "JSON extraído del informe:" if run["research_text"] else "Respuesta cruda del modelo:"
            lines += ["", label, run["raw_response"]]
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")


# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"archivo SQLite (por defecto {DEFAULT_DB.name})")
    args = parser.parse_args()
    try:  # nitidez en pantallas de alta resolución (Windows)
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - no es Windows o no está disponible
        pass
    db = Database(args.db)
    root = tk.Tk()
    App(root, db)
    root.mainloop()


if __name__ == "__main__":
    main()
