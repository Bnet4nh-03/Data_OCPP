"""
Import file JSON cũ vào SQLite, không cần gọi API.

Hỗ trợ:
  - Danh sách ticket:         [ {...}, {...} ]
  - Checkpoint cũ:            { "total": ..., "last_completed_page": ..., "records": [ ... ] }
  - Response API:             { "success": true, "data": { "total": ..., "list": [ ... ] } }
  - Object có khóa "list":    { "list": [ ... ] }

File được đọc kiểu streaming (từng ticket một), không nạp toàn bộ vào RAM.
File gốc chỉ được mở ở chế độ đọc.
"""

import json
import logging
from pathlib import Path

import config
import database

log = logging.getLogger("ccts")

_WS = " \t\r\n"
_CHUNK = 1 << 20  # 1 MB


class InvalidJsonFile(Exception):
    pass


class _JsonStream:
    """Bộ đọc JSON tăng dần dựa trên json.JSONDecoder.raw_decode (thư viện chuẩn)."""

    def __init__(self, fh):
        self.fh = fh
        self.buf = ""
        self.pos = 0
        self.base = 0  # số ký tự đã bỏ khỏi buffer (để báo vị trí lỗi)
        self.eof = False
        self.decoder = json.JSONDecoder()

    def offset(self):
        return self.base + self.pos

    def _fill(self):
        if self.eof:
            return False
        if self.pos:
            self.base += self.pos
            self.buf = self.buf[self.pos:]
            self.pos = 0
        chunk = self.fh.read(_CHUNK)
        if not chunk:
            self.eof = True
            return False
        self.buf += chunk
        return True

    def peek(self):
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in _WS:
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._fill():
                return ""

    def expect(self, ch):
        got = self.peek()
        if got != ch:
            raise InvalidJsonFile(
                f"Mong đợi '{ch}' nhưng gặp {got!r} tại ký tự {self.offset()}"
            )
        self.pos += 1

    def read_value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buf, self.pos)
            except json.JSONDecodeError as exc:
                if self._fill():
                    continue
                raise InvalidJsonFile(
                    f"JSON lỗi tại ký tự {self.base + exc.pos}: {exc.msg}"
                ) from None
            # Giá trị chạm cuối buffer (vd. số bị cắt đôi) -> đọc thêm rồi parse lại
            if end == len(self.buf) and not self.eof and self._fill():
                continue
            self.pos = end
            return value

    def iter_array(self):
        self.expect("[")
        if self.peek() == "]":
            self.pos += 1
            return
        while True:
            yield self.read_value()
            ch = self.peek()
            self.pos += 1
            if ch == ",":
                continue
            if ch == "]":
                return
            raise InvalidJsonFile(
                f"Mong đợi ',' hoặc ']' nhưng gặp {ch!r} tại ký tự {self.offset() - 1}"
            )

    def iter_object(self):
        """Duyệt object: yield key, caller PHẢI đọc giá trị (read_value/iter_array/iter_object)."""
        self.expect("{")
        if self.peek() == "}":
            self.pos += 1
            return
        while True:
            key = self.read_value()
            if not isinstance(key, str):
                raise InvalidJsonFile(f"Khóa object không phải chuỗi tại ký tự {self.offset()}")
            self.expect(":")
            yield key
            ch = self.peek()
            self.pos += 1
            if ch == ",":
                continue
            if ch == "}":
                return
            raise InvalidJsonFile(
                f"Mong đợi ',' hoặc '}}' nhưng gặp {ch!r} tại ký tự {self.offset() - 1}"
            )


def iter_tickets(path, info):
    """
    Generator trả về từng phần tử ticket. Ghi định dạng và metadata vào dict info.
    Ném InvalidJsonFile nếu cấu trúc không được hỗ trợ.
    """
    info.setdefault("meta", {})
    with Path(path).open("r", encoding="utf-8-sig") as fh:
        st = _JsonStream(fh)
        first = st.peek()
        if first == "[":
            info["format"] = "danh sách ticket"
            yield from st.iter_array()
        elif first == "{":
            found = False
            for key in st.iter_object():
                if key in ("records", "list") and st.peek() == "[" and not found:
                    info["format"] = "checkpoint cũ (records)" if key == "records" else "object (list)"
                    found = True
                    yield from st.iter_array()
                elif key == "data" and st.peek() == "{" and not found:
                    for dkey in st.iter_object():
                        if dkey == "list" and st.peek() == "[" and not found:
                            info["format"] = "response API (data.list)"
                            found = True
                            yield from st.iter_array()
                        else:
                            _keep_meta(info, f"data.{dkey}", st.read_value())
                else:
                    _keep_meta(info, key, st.read_value())
            if not found:
                raise InvalidJsonFile(
                    "Object JSON không có 'records', 'list' hoặc 'data.list' dạng danh sách. "
                    f"Các khóa tìm thấy: {', '.join(info['meta']) or '(không có)'}"
                )
        elif first == "":
            raise InvalidJsonFile("File rỗng")
        else:
            raise InvalidJsonFile(f"File phải bắt đầu bằng '[' hoặc '{{', gặp {first!r}")
        if st.peek() != "":
            raise InvalidJsonFile(f"Có dữ liệu thừa sau JSON tại ký tự {st.offset()}")


def _keep_meta(info, key, value):
    if isinstance(value, (dict, list)):
        info["meta"][key] = f"<{type(value).__name__}: {len(value)} phần tử>"
    else:
        info["meta"][key] = value


def validate_file(path):
    """Đọc thử toàn bộ file, không ghi DB. Trả về thông tin cấu trúc."""
    info = {}
    stats = {"items": 0, "objects": 0, "with_pk": 0, "unique_pk": 0}
    pks = set()
    for item in iter_tickets(path, info):
        stats["items"] += 1
        if isinstance(item, dict):
            stats["objects"] += 1
            pk = database.ticket_pk(item)
            if pk is not None:
                stats["with_pk"] += 1
                pks.add(pk)
    stats["unique_pk"] = len(pks)
    info["stats"] = stats
    return info


def import_file(conn, path, overwrite=False, validate=True, dry_run=False):
    path = Path(path)
    if not path.is_file():
        raise InvalidJsonFile(f"Không tìm thấy file: {path}")

    size_mb = path.stat().st_size / 1024 / 1024
    log.info("Import %s (%.1f MB)", path.name, size_mb)

    if validate or dry_run:
        log.info("Bước 1: kiểm tra cấu trúc file...")
        info = validate_file(path)
        s = info["stats"]
        log.info("  Định dạng       : %s", info.get("format"))
        for k, v in info["meta"].items():
            log.info("  %-16s: %s", k, v)
        log.info("  Phần tử         : %s (object: %s)", f"{s['items']:,}", f"{s['objects']:,}")
        log.info("  Có cctsTicketPk : %s (khác nhau: %s)", f"{s['with_pk']:,}", f"{s['unique_pk']:,}")
        if s["objects"] == 0:
            raise InvalidJsonFile("File không chứa ticket nào (không có object).")
        if dry_run:
            log.info("Chế độ --dry-run: không ghi vào database.")
            return None

    log.info("Bước 2: ghi vào database (batch %s, %s ticket đã có)...",
             config.IMPORT_BATCH_SIZE, "CẬP NHẬT" if overwrite else "BỎ QUA")
    counts = database.new_counts()
    info = {}
    source = f"import:{path.name}"
    batch = []
    try:
        for item in iter_tickets(path, info):
            batch.append(item)
            if len(batch) >= config.IMPORT_BATCH_SIZE:
                database.upsert_tickets(conn, batch, source, overwrite=overwrite, counts=counts)
                batch = []
                log.info("  ... đã xử lý %s", f"{counts['received']:,}")
        if batch:
            database.upsert_tickets(conn, batch, source, overwrite=overwrite, counts=counts)
    except InvalidJsonFile:
        log.error("File lỗi giữa chừng. %s bản ghi trước vị trí lỗi đã được commit.",
                  f"{counts['received']:,}")
        _report(counts, conn)
        raise
    _report(counts, conn)
    return counts


def _report(c, conn):
    log.info("Kết quả import:")
    log.info("  Đọc được        : %s", f"{c['received']:,}")
    log.info("  Thêm mới        : %s", f"{c['new']:,}")
    log.info("  Cập nhật        : %s", f"{c['updated']:,}")
    log.info("  Không đổi       : %s", f"{c['unchanged']:,}")
    log.info("  Bỏ qua (đã có)  : %s", f"{c['skipped']:,}")
    log.info("  Trùng trong lô  : %s", f"{c['dup_in_batch']:,}")
    log.info("  Thiếu PK        : %s (chính sách: %s)", f"{c['no_pk']:,}", config.MISSING_PK_POLICY)
    log.info("  Lỗi (không phải object): %s", f"{c['invalid']:,}")
    log.info("  Tổng ticket trong DB: %s", f"{database.count_tickets(conn):,}")
