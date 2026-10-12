"""Đồng bộ ticket từ API vào SQLite: phân trang, checkpoint, tiếp tục, phát hiện bất thường."""

import json
import logging
import math
import os
import time
from pathlib import Path

import config
import database
from api_client import ApiError, AuthError

log = logging.getLogger("ccts")

PROGRESS_EVERY = 1  # in tiến độ mỗi N trang (trang bất thường luôn được in)


# ---------------------------------------------------------------- checkpoint

def load_checkpoint(path=config.CHECKPOINT_FILE):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Checkpoint %s hỏng hoặc không đọc được (%s) - bỏ qua.", path.name, exc)
        return None
    if not isinstance(data, dict) or "records" in data:
        log.warning("%s không phải checkpoint định dạng mới - bỏ qua.", path.name)
        return None
    return data


def save_checkpoint(data, path=config.CHECKPOINT_FILE):
    """Ghi an toàn: ghi file tạm -> fsync -> os.replace (thao tác nguyên tử)."""
    path = Path(path)
    data = dict(data, updated_at=database.now_str())
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- sync

class StopSync(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class SyncRun:
    def __init__(self, conn, client, mode, delay, max_pages):
        self.conn = conn
        self.client = client
        self.page_size = client.page_size
        self.mode = mode
        self.delay = delay
        self.max_pages = max_pages

        self.seen = set()          # PK đã nhận trong lần chạy này (chỉ lưu ID, không lưu ticket)
        self.counts = database.new_counts()
        self.pages_fetched = 0
        self.short_pages = []      # trang trả ít hơn page_size
        self.empty_mid_pages = []  # trang rỗng khi chưa tới cuối dự kiến
        self.total_first = None
        self.total_last = None
        self.total_min = None
        self.total_max = None
        self.warnings = []
        self.prev_sig = None
        self.cp = {}
        self.run_id = None
        self.write_checkpoint = mode != "new"  # --new không đụng tới checkpoint quét đầy đủ

    # ------------------------------------------------ tiện ích
    def warn(self, msg, *args):
        text = msg % args if args else msg
        self.warnings.append(text)
        log.warning(text)

    def expected_pages(self):
        if not self.total_last:
            return None
        return math.ceil(self.total_last / self.page_size)

    def _track_total(self, total):
        if total is None:
            return
        if self.total_first is None:
            self.total_first = total
        self.total_last = total
        self.total_min = total if self.total_min is None else min(self.total_min, total)
        self.total_max = total if self.total_max is None else max(self.total_max, total)

    def fetch(self, page):
        if self.max_pages and self.pages_fetched >= self.max_pages:
            raise StopSync("stopped_limit", f"Đạt giới hạn --max-pages {self.max_pages}")
        if self.pages_fetched:
            time.sleep(self.delay)
        records, total = self.client.fetch_page(page)
        self.pages_fetched += 1
        self._track_total(total)
        return records

    def store(self, page, records):
        pks = [database.ticket_pk(r) for r in records if isinstance(r, dict)]
        pks = [p for p in pks if p]
        new_in_run = sum(1 for p in set(pks) if p not in self.seen)
        self.seen.update(pks)
        c = database.upsert_tickets(self.conn, records, "api", overwrite=True, counts=self.counts)
        return c, pks, new_in_run

    def save_progress(self, page, status="running", message=""):
        self.cp.update(
            status=status,
            mode=self.mode,
            page_size=self.page_size,
            last_completed_page=page,
            api_total=self.total_last,
            message=message,
            run_id=self.run_id,
        )
        if self.write_checkpoint:
            save_checkpoint(self.cp)
        database.update_run(
            self.conn, self.run_id, last_page=page, api_total=self.total_last,
            received=self.counts["received"], unique_in_run=len(self.seen),
            new=self.counts["new"], updated=self.counts["updated"],
            status=status, message=message,
        )

    def progress(self, page, c, force=False):
        if not force and page % PROGRESS_EVERY != 0:
            return
        exp = self.expected_pages()
        log.info(
            "Trang %s/%s | nhận %s | mới %s | cập nhật %s | DB %s | API total %s",
            f"{page:,}", f"{exp:,}" if exp else "?", c["received"], c["new"], c["updated"],
            f"{database.count_tickets(self.conn):,}",
            f"{self.total_last:,}" if self.total_last is not None else "?",
        )

    # ------------------------------------------------ quét trang
    def scan(self, start_page, stop_when_no_new=False, verify_resume=False, first_records=None):
        """
        Quét từ start_page tới khi hết dữ liệu (hoặc tới khi đã đuổi kịp nếu stop_when_no_new).
        Trả về lý do dừng.
        """
        page = start_page
        empty_streak = 0
        dup_streak = 0
        no_new_streak = 0
        backtracks = 0

        while True:
            if first_records is not None:
                records, first_records = first_records, None
            else:
                records = self.fetch(page)

            # Xác minh điểm tiếp tục: trang phải chứa ít nhất 1 ticket đã có trong DB
            if verify_resume:
                pks = [database.ticket_pk(r) for r in records if isinstance(r, dict)]
                known = database.existing_pk_count(self.conn, [p for p in pks if p])
                if known == 0 and records and page > 2 and backtracks < config.RESUME_MAX_BACKTRACK:
                    backtracks += 1
                    new_page = max(2, page - config.RESUME_OVERLAP_PAGES)
                    log.info("Trang %s không chứa ticket nào đã có trong DB -> lùi về trang %s "
                             "để tránh bỏ sót (lần %s).", page, new_page, backtracks)
                    # Vẫn lưu dữ liệu trang này - dữ liệu hợp lệ, PK chống trùng
                    self.store(page, records)
                    page = new_page
                    continue
                if known == 0 and records and page > 2:
                    self.warn("Không xác minh được điểm tiếp tục sau %s lần lùi trang. "
                              "Có thể có khoảng trống - nên chạy 'sync --full' để kiểm tra.",
                              backtracks)
                elif records:
                    log.info("Điểm tiếp tục trang %s đã xác minh (%s/%s ticket đã có trong DB).",
                             page, known, len(pks))
                verify_resume = False

            # --- trang rỗng
            if not records:
                empty_streak += 1
                exp = self.expected_pages()
                if exp and page <= exp:
                    self.empty_mid_pages.append(page)
                    self.warn("Trang %s rỗng dù total dự kiến %s trang.", page, exp)
                else:
                    log.info("Trang %s rỗng.", page)
                if empty_streak >= config.EMPTY_PAGES_TO_STOP:
                    self.save_progress(page, message="end: empty pages")
                    return "end"
                page += 1
                continue
            empty_streak = 0

            # --- trang lặp nguyên vẹn
            sig = tuple(database.ticket_pk(r) if isinstance(r, dict) else None for r in records)
            if sig == self.prev_sig:
                raise StopSync(
                    "stopped_anomaly",
                    f"Trang {page} giống hệt trang trước - API có thể bỏ qua pageNum. Dừng để tránh lặp vô hạn.",
                )
            self.prev_sig = sig

            c, pks, new_in_run = self.store(page, records)

            if len(records) < self.page_size:
                exp = self.expected_pages()
                if exp is None or page < exp:
                    self.short_pages.append(page)
                    log.info("Trang %s chỉ có %s/%s bản ghi (chưa phải trang cuối theo total) - "
                             "tiếp tục, không coi là hết dữ liệu.", page, len(records), self.page_size)

            if pks and new_in_run == 0:
                dup_streak += 1
                log.info("Trang %s chỉ chứa ticket đã nhận trong lần chạy này (trôi trang?).", page)
                if dup_streak >= config.MAX_CONSECUTIVE_DUPLICATE_PAGES:
                    raise StopSync(
                        "stopped_anomaly",
                        f"{dup_streak} trang liên tiếp không có ticket mới trong lần chạy - "
                        "phân trang bất thường. Dừng.",
                    )
            else:
                dup_streak = 0

            self.save_progress(page)
            self.progress(page, c, force=c["no_pk"] > 0 or c["invalid"] > 0)

            if stop_when_no_new:
                no_new_streak = no_new_streak + 1 if c["new"] == 0 else 0
                if no_new_streak >= config.INCREMENTAL_STOP_PAGES:
                    self.progress(page, c, force=True)
                    return "caught_up"

            exp = self.expected_pages()
            if exp:
                limit = exp + max(config.EXTRA_PAGES_MIN, int(exp * config.EXTRA_PAGES_RATIO))
                if page >= limit:
                    raise StopSync(
                        "stopped_anomaly",
                        f"Đã đọc tới trang {page}, vượt xa số trang dự kiến {exp}. Dừng để tránh vòng lặp.",
                    )
            page += 1


def _resume_page(cp, page_size, total_now):
    """Tính trang bắt đầu khi tiếp tục, bù cho việc ticket mới đẩy dữ liệu xuống các trang sau."""
    last = int(cp.get("last_completed_page") or 0)
    old_ps = int(cp.get("page_size") or page_size)
    offset = last * old_ps  # số dòng đã đọc theo vị trí cũ
    old_total = cp.get("api_total")
    shift = 0
    if old_total is not None and total_now is not None:
        shift = total_now - int(old_total)  # >0: có ticket mới chèn lên đầu
    offset = max(0, offset + shift)
    page = offset // page_size + 1 - config.RESUME_OVERLAP_PAGES
    return max(2, page), last, shift


def run_sync(conn, client, mode="auto", delay=config.REQUEST_DELAY, max_pages=None, start_page=None):
    """
    mode:
      auto - tiếp tục nếu lần trước dở dang, nếu không thì quét đầy đủ từ trang 1
      full - quét đầy đủ từ trang 1 (đồng bộ lại, khóa chính chống trùng)
      new  - chỉ lấy ticket mới ở đầu danh sách, dừng khi đã đuổi kịp
    """
    run = SyncRun(conn, client, mode, delay, max_pages)
    cp_old = load_checkpoint()
    run.cp = {"started_at": database.now_str()}

    # 1. Trang đầu: lấy total và kiểm tra response
    log.info("Gọi trang 1 (pageSize=%s) để lấy tổng số ticket...", run.page_size)
    first = run.fetch(1)
    total = run.total_last
    log.info("API báo total: %s | trang 1 trả %s ticket | DB hiện có %s",
             f"{total:,}" if total is not None else "(không có)", len(first),
             f"{database.count_tickets(conn):,}")
    if total is None:
        run.warn("API không trả data.total - không thể đối chiếu tổng số.")
    if not first:
        if total:
            raise ApiError("Trang 1 rỗng nhưng total > 0. Dừng để tránh hiểu nhầm là hết dữ liệu.")
        log.info("API không có ticket nào.")
        return

    # 2. Xác định điểm bắt đầu
    resume_from = None
    if start_page:
        resume_from = max(2, int(start_page))
        log.info("Bắt đầu từ trang %s theo tham số --start-page.", resume_from)
    elif mode == "auto" and cp_old and cp_old.get("status") != "completed" \
            and cp_old.get("mode") in ("auto", "full") and cp_old.get("last_completed_page"):
        resume_from, last, shift = _resume_page(cp_old, run.page_size, total)
        log.info("Lần chạy trước dừng ở trang %s (trạng thái: %s, lúc %s).",
                 last, cp_old.get("status"), cp_old.get("updated_at"))
        log.info("Total thay đổi %+d kể từ đó -> tiếp tục từ trang %s (đã lùi %s trang dự phòng, "
                 "sẽ xác minh bằng khóa chính).", shift, resume_from, config.RESUME_OVERLAP_PAGES)
        run.cp["started_at"] = cp_old.get("started_at") or run.cp["started_at"]
        run.cp["resumed_from"] = resume_from
    if mode == "new":
        log.info("Chế độ --new: quét từ đầu, dừng sau %s trang liên tiếp không có ticket mới.",
                 config.INCREMENTAL_STOP_PAGES)

    run.run_id = database.start_run(conn, mode, run.page_size, resume_from or 1, total)
    status, message = "completed", ""
    try:
        if mode == "new":
            run.scan(1, stop_when_no_new=True, first_records=first)
            message = "đã đuổi kịp dữ liệu mới"
        elif resume_from:
            run.store(1, first)
            run.save_progress(1)
            run.scan(resume_from, verify_resume=True)
            message = "đã quét tới cuối (tiếp tục từ lần trước)"
        else:
            run.scan(1, first_records=first)
            message = "đã quét đầy đủ từ trang 1"

        if mode != "new":
            run.save_progress(run.cp.get("last_completed_page") or 0, status="completed", message=message)
            run.write_checkpoint = False
            # Ticket mới phát sinh trong lúc quét nằm ở đầu danh sách -> quét lại phần đầu
            log.info("Quét lại phần đầu danh sách để lấy ticket phát sinh trong lúc đồng bộ...")
            run.prev_sig = None
            run.scan(1, stop_when_no_new=True)
    except StopSync as exc:
        status, message = exc.status, exc.message
        log.warning(message)
    except AuthError as exc:
        status, message = "auth_error", str(exc)
        log.error("LỖI XÁC THỰC: %s. Cập nhật cookie/token trong credentials.json rồi chạy lại.", exc)
    except ApiError as exc:
        status, message = "error", str(exc)
        log.error("Lỗi API: %s", exc)
    except KeyboardInterrupt:
        status, message = "interrupted", "người dùng dừng (Ctrl+C)"
        log.warning("Đã dừng theo yêu cầu. Các trang đã commit được giữ nguyên.")
    except Exception as exc:
        status, message = "error", f"{type(exc).__name__}: {exc}"
        log.exception("Lỗi không mong đợi")
    finally:
        if run.write_checkpoint:
            run.save_progress(run.cp.get("last_completed_page") or 0, status=status, message=message)
        database.update_run(conn, run.run_id, status=status, message=message,
                            finished_at=database.now_str(),
                            received=run.counts["received"], unique_in_run=len(run.seen),
                            new=run.counts["new"], updated=run.counts["updated"])
        _final_report(run, status, message, resumed=bool(resume_from))
    return status


def _final_report(run, status, message, resumed):
    db_total = database.count_tickets(run.conn)
    no_pk_total = run.conn.execute("SELECT COUNT(*) FROM tickets_no_pk").fetchone()[0]
    c = run.counts
    log.info("=" * 70)
    log.info("KẾT QUẢ ĐỒNG BỘ - trạng thái: %s (%s)", status, message)
    log.info("  Trang đã gọi            : %s", f"{run.pages_fetched:,}")
    log.info("  Bản ghi nhận được       : %s", f"{c['received']:,}")
    log.info("  Ticket khác nhau (lần chạy): %s", f"{len(run.seen):,}")
    log.info("  Thêm mới / cập nhật / không đổi: %s / %s / %s",
             f"{c['new']:,}", f"{c['updated']:,}", f"{c['unchanged']:,}")
    log.info("  Thiếu cctsTicketPk      : %s (lần chạy) | %s (trong DB)", c["no_pk"], no_pk_total)
    if run.total_first is not None:
        log.info("  API total đầu / cuối    : %s / %s (min %s, max %s)",
                 f"{run.total_first:,}", f"{run.total_last:,}",
                 f"{run.total_min:,}", f"{run.total_max:,}")
    log.info("  Tổng ticket trong DB    : %s", f"{db_total:,}")

    if run.short_pages:
        sample = ", ".join(map(str, run.short_pages[:10]))
        log.info("  Trang trả thiếu bản ghi : %s trang (vd: %s)", len(run.short_pages), sample)
    if run.empty_mid_pages:
        log.info("  Trang rỗng giữa chừng   : %s", ", ".join(map(str, run.empty_mid_pages[:10])))

    # Đối chiếu - không khẳng định đủ chỉ dựa vào total
    if run.total_last is not None:
        diff = db_total - run.total_last
        if diff == 0:
            log.info("  ĐỐI CHIẾU: số ticket trong DB BẰNG total API (%s).", f"{db_total:,}")
            log.info("    Lưu ý: bằng nhau về số lượng chưa chứng minh trùng khớp từng ticket.")
        elif diff < 0:
            log.warning("  ĐỐI CHIẾU: DB ÍT HƠN total API %s ticket.", f"{-diff:,}")
            if status != "completed":
                log.warning("    Lần chạy chưa hoàn tất - chạy lại 'python main.py sync' để tiếp tục.")
            else:
                log.warning("    Có thể do: phân trang trôi khi dữ liệu thay đổi, API lọc theo quyền, "
                            "hoặc ticket bị xóa/ẩn. Chạy 'python main.py sync --full' để quét lại.")
        else:
            log.warning("  ĐỐI CHIẾU: DB NHIỀU HƠN total API %s ticket.", f"{diff:,}")
            log.warning("    Có thể do ticket đã bị xóa/ẩn khỏi API, hoặc import từ JSON cũ. "
                        "Dữ liệu cũ không bị xóa khỏi DB.")
    if resumed:
        log.info("  Lần chạy này TIẾP TỤC từ checkpoint: tính đầy đủ dựa trên giả định thứ tự sắp xếp "
                 "ổn định. Để chắc chắn, định kỳ chạy 'sync --full'.")
    for w in run.warnings[:20]:
        log.info("  ! %s", w)
    log.info("=" * 70)


# ---------------------------------------------------------------- probe

def probe(client, page_size):
    """Kiểm tra cơ chế phân trang với page_size cho trước. Gọi 4 request."""
    def pks(records):
        return [database.ticket_pk(r) for r in records if isinstance(r, dict)]

    p1, total = client.fetch_page(1, page_size)
    time.sleep(config.REQUEST_DELAY)
    p2, _ = client.fetch_page(2, page_size)
    time.sleep(config.REQUEST_DELAY)
    big, _ = client.fetch_page(1, page_size * 2)
    time.sleep(config.REQUEST_DELAY)
    p1b, _ = client.fetch_page(1, page_size)

    log.info("PROBE pageSize=%s | API total=%s", page_size, total)
    log.info("  Trang 1: %s bản ghi | Trang 2: %s bản ghi | Trang 1 (pageSize=%s): %s bản ghi",
             len(p1), len(p2), page_size * 2, len(big))
    ok = True
    if len(p1) < page_size and total and total > page_size:
        log.warning("  API trả ít hơn pageSize ở trang 1 -> có thể bị giới hạn pageSize tối đa = %s", len(p1))
        ok = False
    if pks(p1) == pks(p2) and p1:
        log.warning("  Trang 1 và 2 giống hệt -> API bỏ qua pageNum!")
        ok = False
    overlap = set(pks(p1)) & set(pks(p2))
    if overlap:
        log.warning("  Trang 1 và 2 trùng %s ticket.", len(overlap))
        ok = False
    if pks(big) == pks(p1) + pks(p2):
        log.info("  page1(2N) == page1(N) + page2(N): phân trang theo offset nhất quán.")
    else:
        log.warning("  page1(2N) KHÁC page1(N)+page2(N): thứ tự không ổn định hoặc dữ liệu đổi giữa các request.")
        ok = False
    if pks(p1) != pks(p1b):
        log.warning("  Gọi lại trang 1 cho kết quả khác (dữ liệu đang thay đổi hoặc thứ tự không ổn định).")
    log.info("  Kết luận: %s", "OK" if ok else "CẦN KIỂM TRA THÊM trước khi dùng pageSize này")
    return ok
