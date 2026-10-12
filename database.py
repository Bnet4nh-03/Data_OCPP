"""SQLite: tạo schema, upsert ticket, truy vấn thống kê."""

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import config

log = logging.getLogger("ccts")

SCHEMA_VERSION = 1

# (tên trường API, tên cột SQLite). Mọi trường khác vẫn nằm trong raw_json.
FIELD_MAP = [
    ("cctsTicketId", "ccts_ticket_id"),
    ("thirdTicketId", "third_ticket_id"),
    ("cctsTicketName", "ccts_ticket_name"),
    ("cctsTicketStatus", "ccts_ticket_status"),
    ("severity", "severity"),
    ("urgencyLevel", "urgency_level"),
    ("chargeBoxPk", "charge_box_pk"),
    ("chargeBoxId", "charge_box_id"),
    ("chargeBoxModel", "charge_box_model"),
    ("stationCode", "station_code"),
    ("operatorName", "operator_name"),
    ("country", "country"),
    ("address", "address"),
    ("errorCode", "error_code"),
    ("errorName", "error_name"),
    ("errorCategory", "error_category"),
    ("errorDesc", "error_desc"),
    ("ticketSource", "ticket_source"),
    ("ticketCreator", "ticket_creator"),
    ("timezone", "timezone"),
    ("createTime", "create_time"),
    ("occurrenceTime", "occurrence_time"),
    ("slaTime", "sla_time"),
]
MAPPED_COLUMNS = [col for _, col in FIELD_MAP]

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS tickets (
    ccts_ticket_pk   TEXT PRIMARY KEY,
    {", ".join(f"{c} TEXT" for c in MAPPED_COLUMNS)},
    create_time_norm TEXT,          -- 'YYYY-MM-DD HH:MM:SS' nếu chuẩn hóa được
    raw_json         TEXT NOT NULL, -- object ticket gốc, bản mới nhất
    raw_hash         TEXT NOT NULL,
    source           TEXT,          -- 'api' hoặc 'import:<tên file>'
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    updated_at       TEXT NOT NULL  -- lần cuối nội dung thay đổi
);

CREATE INDEX IF NOT EXISTS idx_tickets_ticket_id   ON tickets(ccts_ticket_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status      ON tickets(ccts_ticket_status);
CREATE INDEX IF NOT EXISTS idx_tickets_severity    ON tickets(severity);
CREATE INDEX IF NOT EXISTS idx_tickets_charge_box  ON tickets(charge_box_id);
CREATE INDEX IF NOT EXISTS idx_tickets_create_time ON tickets(create_time_norm);

-- Ticket không có cctsTicketPk: chỉ khử trùng khi nội dung giống hệt
CREATE TABLE IF NOT EXISTS tickets_no_pk (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_hash      TEXT NOT NULL UNIQUE,
    raw_json      TEXT NOT NULL,
    source        TEXT,
    first_seen_at TEXT NOT NULL
);

-- Bản raw_json cũ trước mỗi lần ticket thay đổi
CREATE TABLE IF NOT EXISTS ticket_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ccts_ticket_pk TEXT NOT NULL,
    raw_json       TEXT NOT NULL,
    raw_hash       TEXT NOT NULL,
    valid_until    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_pk ON ticket_history(ccts_ticket_pk);

CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    mode           TEXT,
    page_size      INTEGER,
    start_page     INTEGER,
    last_page      INTEGER,
    api_total      INTEGER,
    received       INTEGER DEFAULT 0,
    unique_in_run  INTEGER DEFAULT 0,
    new            INTEGER DEFAULT 0,
    updated        INTEGER DEFAULT 0,
    status         TEXT,
    message        TEXT,
    started_at     TEXT,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_UPSERT_SQL = f"""
INSERT INTO tickets (
    ccts_ticket_pk, {", ".join(MAPPED_COLUMNS)}, create_time_norm,
    raw_json, raw_hash, source, first_seen_at, last_seen_at, updated_at
) VALUES ({", ".join(["?"] * (len(MAPPED_COLUMNS) + 8))})
ON CONFLICT(ccts_ticket_pk) DO UPDATE SET
    {", ".join(f"{c} = excluded.{c}" for c in MAPPED_COLUMNS)},
    create_time_norm = excluded.create_time_norm,
    raw_json     = excluded.raw_json,
    raw_hash     = excluded.raw_hash,
    source       = excluded.source,
    last_seen_at = excluded.last_seen_at,
    updated_at   = excluded.updated_at
"""

_TZ = timezone(timedelta(minutes=config.TIMEZONE_OFFSET))


def now_str():
    return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path=config.DB_FILE):
    """
    Mở (hoặc tạo) database. Không bao giờ xóa dữ liệu có sẵn.

    WAL (Write-Ahead Logging): ghi nhanh hơn và an toàn khi chương trình bị tắt
    đột ngột - transaction đã COMMIT không bị mất. Khi đóng, close() gọi
    wal_checkpoint(TRUNCATE) để gộp file -wal vào file .db chính.
    synchronous=FULL: ưu tiên an toàn dữ liệu hơn tốc độ.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return conn


def close(conn):
    if conn is None:
        return
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as exc:
        log.warning("Không checkpoint được WAL: %s", exc)
    conn.close()


# ---------------------------------------------------------------- chuẩn hóa

def _to_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value)
    return text if text.strip() != "" else None


_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                 "%Y/%m/%d %H:%M:%S", "%Y-%m-%d")


def normalize_time(value):
    """Chuẩn hóa về 'YYYY-MM-DD HH:MM:SS' (giờ địa phương như API trả về). Không được -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = value / 1000 if value > 1e11 else value  # epoch ms hoặc s
        try:
            return datetime.fromtimestamp(ts, _TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def ticket_pk(item):
    pk = item.get("cctsTicketPk")
    if pk is None:
        return None
    pk = str(pk).strip()
    return pk or None


def _hash(item):
    canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row(pk, item, source, ts):
    raw_json = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    values = [pk]
    values += [_to_text(item.get(api_key)) for api_key, _ in FIELD_MAP]
    values += [normalize_time(item.get("createTime")), raw_json, _hash(item),
               source, ts, ts, ts]
    return values


# ---------------------------------------------------------------- ghi dữ liệu

def new_counts():
    return {"received": 0, "new": 0, "updated": 0, "unchanged": 0,
            "skipped": 0, "dup_in_batch": 0, "no_pk": 0, "invalid": 0}


def upsert_tickets(conn, items, source, overwrite=True, counts=None):
    """
    Ghi một lô ticket trong MỘT transaction.
      overwrite=True : ticket đã có -> cập nhật theo dữ liệu mới (dùng cho API)
      overwrite=False: ticket đã có -> bỏ qua (dùng cho import JSON cũ, tránh
                       ghi đè dữ liệu mới hơn bằng dữ liệu cũ)
    Không dùng INSERT OR REPLACE (vì nó DELETE rồi INSERT lại, làm mất first_seen_at).
    Trả về dict thống kê; nếu truyền counts thì cộng dồn vào đó.
    """
    c = new_counts()
    ts = now_str()

    by_pk = {}
    no_pk = []
    for item in items:
        c["received"] += 1
        if not isinstance(item, dict):
            c["invalid"] += 1
            log.warning("Bỏ qua phần tử không phải object: %r", str(item)[:100])
            continue
        pk = ticket_pk(item)
        if pk is None:
            no_pk.append(item)
            continue
        if pk in by_pk:
            c["dup_in_batch"] += 1  # trùng trong cùng lô: giữ bản sau cùng
        by_pk[pk] = item

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _existing_hashes(conn, list(by_pk))
        upsert_rows, history_pks, touch_pks = [], [], []
        for pk, item in by_pk.items():
            row = _row(pk, item, source, ts)
            old_hash = existing.get(pk)
            if old_hash is None:
                c["new"] += 1
                upsert_rows.append(row)
            elif not overwrite:
                c["skipped"] += 1
            elif old_hash == row[len(MAPPED_COLUMNS) + 3]:
                c["unchanged"] += 1
                touch_pks.append((ts, pk))
            else:
                c["updated"] += 1
                upsert_rows.append(row)
                history_pks.append((ts, pk))

        if config.KEEP_HISTORY and history_pks:
            conn.executemany(
                "INSERT INTO ticket_history(ccts_ticket_pk, raw_json, raw_hash, valid_until) "
                "SELECT ccts_ticket_pk, raw_json, raw_hash, ? FROM tickets WHERE ccts_ticket_pk = ?",
                history_pks,
            )
        if upsert_rows:
            conn.executemany(_UPSERT_SQL, upsert_rows)
        if touch_pks:
            conn.executemany(
                "UPDATE tickets SET last_seen_at = ? WHERE ccts_ticket_pk = ?", touch_pks
            )
        if no_pk:
            _store_no_pk(conn, no_pk, source, ts, c)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    if counts is not None:
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
    return c


def _existing_hashes(conn, pks):
    result = {}
    for i in range(0, len(pks), 500):
        chunk = pks[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT ccts_ticket_pk, raw_hash FROM tickets WHERE ccts_ticket_pk IN ({placeholders})",
            chunk,
        ):
            result[row[0]] = row[1]
    return result


def _store_no_pk(conn, items, source, ts, c):
    for item in items:
        c["no_pk"] += 1
        log.warning(
            "Ticket không có cctsTicketPk (cctsTicketId=%s, chargeBoxId=%s, createTime=%s) -> %s",
            item.get("cctsTicketId"), item.get("chargeBoxId"), item.get("createTime"),
            config.MISSING_PK_POLICY,
        )
    if config.MISSING_PK_POLICY != "quarantine":
        return
    rows = [(_hash(it), json.dumps(it, ensure_ascii=False, separators=(",", ":")), source, ts)
            for it in items]
    conn.executemany(
        "INSERT INTO tickets_no_pk(raw_hash, raw_json, source, first_seen_at) VALUES (?,?,?,?) "
        "ON CONFLICT(raw_hash) DO NOTHING",
        rows,
    )


def existing_pk_count(conn, pks):
    return len(_existing_hashes(conn, list(pks)))


# ---------------------------------------------------------------- sync_runs

def start_run(conn, mode, page_size, start_page, api_total):
    cur = conn.execute(
        "INSERT INTO sync_runs(mode, page_size, start_page, last_page, api_total, status, started_at) "
        "VALUES (?,?,?,?,?, 'running', ?)",
        (mode, page_size, start_page, start_page - 1, api_total, now_str()),
    )
    return cur.lastrowid


def update_run(conn, run_id, **fields):
    if not fields:
        return
    allowed = {"last_page", "api_total", "received", "unique_in_run", "new",
               "updated", "status", "message", "finished_at"}
    keys = [k for k in fields if k in allowed]
    conn.execute(
        f"UPDATE sync_runs SET {', '.join(f'{k} = ?' for k in keys)} WHERE id = ?",
        [fields[k] for k in keys] + [run_id],
    )


# ---------------------------------------------------------------- truy vấn

def count_tickets(conn):
    return conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]


def group_count(conn, column, limit=None):
    if column not in MAPPED_COLUMNS:  # tên cột không lấy từ input người dùng
        raise ValueError(column)
    sql = (f"SELECT COALESCE({column}, '(null)') AS k, COUNT(*) AS n FROM tickets "
           f"GROUP BY {column} ORDER BY n DESC, k")
    params = ()
    if limit:
        sql += " LIMIT ?"
        params = (limit,)
    return conn.execute(sql, params).fetchall()


def find_ticket(conn, key):
    return conn.execute(
        "SELECT * FROM tickets WHERE ccts_ticket_pk = ? OR ccts_ticket_id = ? OR third_ticket_id = ?",
        (key, key, key),
    ).fetchall()


def list_tickets(conn, date_from=None, date_to=None, status=None, charge_box=None,
                 severity=None, limit=50):
    where, params = [], []
    if date_from:
        where.append("create_time_norm >= ?")
        params.append(date_from)
    if date_to:
        where.append("create_time_norm <= ?")
        params.append(date_to)
    if status:
        where.append("ccts_ticket_status = ?")
        params.append(status)
    if charge_box:
        where.append("charge_box_id = ?")
        params.append(charge_box)
    if severity:
        where.append("severity = ?")
        params.append(severity)
    sql = "SELECT * FROM tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY create_time_norm DESC, ccts_ticket_pk DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()
