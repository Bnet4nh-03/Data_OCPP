"""
CCTS Ticket Manager - CNPowercore API -> SQLite

Cách dùng nhanh (CMD / PowerShell):
    python main.py                      # menu đơn giản
    python main.py sync                 # đồng bộ (tự tiếp tục nếu lần trước dở dang)
    python main.py sync --new           # chỉ lấy ticket mới
    python main.py import-json          # import ccts_tickets.json / ccts_checkpoint.json
    python main.py stats
    python main.py -h                   # xem tất cả lệnh
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

import config
import database
import importer
import sync

log = logging.getLogger("ccts")


def setup_logging():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    log.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    try:
        fileh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        fileh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        log.addHandler(fileh)
    except OSError as exc:
        log.warning("Không mở được file log %s: %s", config.LOG_FILE, exc)


# ---------------------------------------------------------------- lệnh

def cmd_sync(conn, args):
    from api_client import CctsClient  # chỉ cần requests khi gọi API

    ssoticket, token = config.load_credentials()
    mode = "full" if args.full else "new" if args.new else "auto"
    with CctsClient(ssoticket, token, page_size=args.page_size) as client:
        status = sync.run_sync(conn, client, mode=mode, delay=args.delay,
                               max_pages=args.max_pages, start_page=args.start_page)
    return 0 if status in ("completed", "stopped_limit") else 1


def cmd_probe(conn, args):
    from api_client import CctsClient

    ssoticket, token = config.load_credentials()
    with CctsClient(ssoticket, token, page_size=args.page_size) as client:
        return 0 if sync.probe(client, args.page_size) else 1


def cmd_import(conn, args):
    files = args.files
    if not files:
        files = [str(config.BASE_DIR / n) for n in ("ccts_tickets.json", "ccts_checkpoint.json")
                 if (config.BASE_DIR / n).exists()]
        if not files:
            log.error("Không tìm thấy ccts_tickets.json hoặc ccts_checkpoint.json. "
                      "Chỉ định file: python main.py import-json <đường dẫn>")
            return 1
    rc = 0
    for f in files:
        try:
            importer.import_file(conn, f, overwrite=args.overwrite,
                                 validate=not args.no_validate, dry_run=args.dry_run)
        except importer.InvalidJsonFile as exc:
            log.error("File %s không hợp lệ: %s", f, exc)
            rc = 1
        except UnicodeDecodeError as exc:
            log.error("File %s không phải UTF-8: %s", f, exc)
            rc = 1
        log.info("-" * 70)
    return rc


def cmd_stats(conn, args):
    total = database.count_tickets(conn)
    print(f"\nTổng ticket trong DB: {total:,}")
    no_pk = conn.execute("SELECT COUNT(*) FROM tickets_no_pk").fetchone()[0]
    if no_pk:
        print(f"Ticket thiếu cctsTicketPk (bảng riêng): {no_pk:,}")
    rng = conn.execute("SELECT MIN(create_time_norm), MAX(create_time_norm) FROM tickets").fetchone()
    print(f"Khoảng createTime: {rng[0]} -> {rng[1]}")

    for title, col, limit in [("Theo trạng thái", "ccts_ticket_status", None),
                              ("Theo severity", "severity", None),
                              (f"Top {args.top} mã trạm sạc (chargeBoxId)", "charge_box_id", args.top),
                              (f"Top {args.top} lỗi (errorName)", "error_name", args.top),
                              ("Theo nguồn ticket", "ticket_source", None)]:
        print(f"\n{title}:")
        for r in database.group_count(conn, col, limit):
            print(f"  {r['n']:>10,}  {r['k']}")

    print("\nTheo tháng (createTime):")
    for r in conn.execute(
        "SELECT substr(create_time_norm, 1, 7) AS m, COUNT(*) AS n FROM tickets "
        "GROUP BY m ORDER BY m DESC LIMIT 24"
    ):
        print(f"  {r['n']:>10,}  {r['m'] or '(không chuẩn hóa được)'}")

    _print_last_run(conn)
    return 0


def _print_last_run(conn):
    r = conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    if r:
        print(f"\nLần đồng bộ gần nhất #{r['id']}: {r['mode']} | {r['status']} | "
              f"trang {r['start_page']}->{r['last_page']} | nhận {r['received']:,} | "
              f"mới {r['new']:,} | cập nhật {r['updated']:,} | API total {r['api_total']} | "
              f"{r['started_at']} -> {r['finished_at']}")
    cp = sync.load_checkpoint()
    if cp:
        print(f"Checkpoint: trạng thái {cp.get('status')} | trang {cp.get('last_completed_page')} | "
              f"pageSize {cp.get('page_size')} | API total {cp.get('api_total')} | {cp.get('updated_at')}")


_SHOW_FIELDS = [("PK", "ccts_ticket_pk"), ("Ticket ID", "ccts_ticket_id"),
                ("Third ID", "third_ticket_id"), ("Trạng thái", "ccts_ticket_status"),
                ("Severity", "severity"), ("Trạm sạc", "charge_box_id"),
                ("Station", "station_code"), ("Operator", "operator_name"),
                ("Lỗi", "error_name"), ("Tạo lúc", "create_time"),
                ("Địa chỉ", "address"), ("Nguồn", "source"),
                ("Lần đầu thấy", "first_seen_at"), ("Cập nhật", "updated_at")]


def cmd_find(conn, args):
    rows = database.find_ticket(conn, args.key.strip())
    if not rows:
        print(f"Không tìm thấy ticket '{args.key}'.")
        return 1
    for r in rows:
        print("-" * 70)
        for label, col in _SHOW_FIELDS:
            print(f"{label:>14}: {r[col] if r[col] is not None else ''}")
        print(f"{'Mô tả lỗi':>14}: {(r['error_desc'] or '')[:500]}")
        hist = conn.execute("SELECT COUNT(*) FROM ticket_history WHERE ccts_ticket_pk = ?",
                            (r["ccts_ticket_pk"],)).fetchone()[0]
        print(f"{'Số bản cũ':>14}: {hist}")
        if args.raw:
            print("raw_json:")
            print(json.dumps(json.loads(r["raw_json"]), ensure_ascii=False, indent=2))
    return 0


def _date_arg(value, end=False):
    if not value:
        return None
    v = value.strip()
    if len(v) == 10:
        v += " 23:59:59" if end else " 00:00:00"
    norm = database.normalize_time(v)
    if norm is None:
        raise SystemExit(f"Ngày không hợp lệ: {value} (dùng YYYY-MM-DD hoặc 'YYYY-MM-DD HH:MM:SS')")
    return norm


def cmd_list(conn, args):
    rows = database.list_tickets(
        conn, _date_arg(args.date_from), _date_arg(args.date_to, end=True),
        args.status, args.charge_box, args.severity, args.limit,
    )
    for r in rows:
        print(f"{r['create_time'] or '':19}  {r['ccts_ticket_id'] or '':14}  "
              f"{(r['ccts_ticket_status'] or '')[:28]:28}  {r['charge_box_id'] or '':20}  "
              f"{(r['error_name'] or '')[:40]}")
    print(f"({len(rows)} dòng, tối đa {args.limit})")
    return 0


def cmd_export(conn, args):
    out = Path(args.out or f"ccts_export_{database.now_str()[:10]}.{'jsonl' if args.jsonl else 'json'}")
    if out.exists() and not args.force:
        log.error("File %s đã tồn tại. Dùng --force để ghi đè hoặc --out tên khác.", out)
        return 1
    where, params = [], []
    if args.date_from:
        where.append("create_time_norm >= ?")
        params.append(_date_arg(args.date_from))
    if args.date_to:
        where.append("create_time_norm <= ?")
        params.append(_date_arg(args.date_to, end=True))
    sql = "SELECT raw_json FROM tickets"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY create_time_norm DESC, ccts_ticket_pk DESC"

    tmp = out.with_suffix(out.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        if not args.jsonl:
            f.write("[\n")
        for (raw,) in conn.execute(sql, params):  # đọc từng dòng, không nạp hết vào RAM
            if args.jsonl:
                f.write(raw + "\n")
            else:
                f.write(("  " if n == 0 else ",\n  ") + raw)
            n += 1
        if not args.jsonl:
            f.write("\n]\n")
    tmp.replace(out)
    log.info("Đã xuất %s ticket -> %s", f"{n:,}", out.resolve())
    return 0


def cmd_check(conn, args):
    ok = True
    print("\n[1] Toàn vẹn file database:")
    res = conn.execute("PRAGMA quick_check").fetchone()[0]
    print(f"    quick_check: {res}")
    ok &= res == "ok"

    print("[2] Cấu trúc bảng:")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tickets)")}
    expected = {"ccts_ticket_pk", "raw_json", "raw_hash", "create_time_norm", *database.MAPPED_COLUMNS}
    missing = expected - cols
    print(f"    tickets: {len(cols)} cột" + (f" | THIẾU: {sorted(missing)}" if missing else " | đủ"))
    ok &= not missing
    idx = [r["name"] for r in conn.execute("PRAGMA index_list(tickets)")]
    print(f"    index: {', '.join(sorted(idx))}")

    print("[3] Số lượng:")
    for t in ("tickets", "tickets_no_pk", "ticket_history", "sync_runs"):
        print(f"    {t:16}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:,}")

    print("[4] Chất lượng dữ liệu (tickets):")
    checks = [
        ("thiếu cctsTicketId", "ccts_ticket_id IS NULL"),
        ("thiếu trạng thái", "ccts_ticket_status IS NULL"),
        ("thiếu chargeBoxId", "charge_box_id IS NULL"),
        ("thiếu createTime", "create_time IS NULL"),
        ("createTime không chuẩn hóa được", "create_time IS NOT NULL AND create_time_norm IS NULL"),
    ]
    for label, cond in checks:
        print(f"    {label:32}: {conn.execute(f'SELECT COUNT(*) FROM tickets WHERE {cond}').fetchone()[0]:,}")
    try:
        bad = conn.execute("SELECT COUNT(*) FROM tickets WHERE json_valid(raw_json) = 0").fetchone()[0]
        print(f"    {'raw_json không hợp lệ':32}: {bad:,}")
        ok &= bad == 0
    except sqlite3.OperationalError:
        print("    (SQLite không có JSON1 - bỏ qua kiểm tra raw_json)")
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT ccts_ticket_id FROM tickets WHERE ccts_ticket_id IS NOT NULL "
        "GROUP BY ccts_ticket_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    print(f"    {'cctsTicketId trùng (PK khác)':32}: {dup:,}")

    print("[5] Đối chiếu với API:")
    _print_last_run(conn)
    cp = sync.load_checkpoint()
    if cp and cp.get("api_total") is not None:
        diff = database.count_tickets(conn) - int(cp["api_total"])
        print(f"    DB - API total (lần gần nhất) = {diff:+,}")
    print(f"\nKết luận: {'OK' if ok else 'CÓ VẤN ĐỀ - xem chi tiết ở trên'}")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI

def build_parser():
    p = argparse.ArgumentParser(description="CCTS Ticket Manager: CNPowercore API -> SQLite")
    p.add_argument("--db", default=str(config.DB_FILE), help="đường dẫn file SQLite")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("sync", help="lấy dữ liệu từ API và lưu vào SQLite")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--full", action="store_true", help="quét lại đầy đủ từ trang 1, bỏ qua checkpoint")
    g.add_argument("--new", action="store_true", help="chỉ lấy ticket mới ở đầu danh sách")
    s.add_argument("--page-size", type=int, default=config.PAGE_SIZE)
    s.add_argument("--delay", type=float, default=config.REQUEST_DELAY, help="giây giữa các request")
    s.add_argument("--max-pages", type=int, help="giới hạn số trang (để chạy thử)")
    s.add_argument("--start-page", type=int, help="bắt đầu từ trang chỉ định (nâng cao)")

    s = sub.add_parser("probe", help="kiểm tra cơ chế phân trang của API")
    s.add_argument("--page-size", type=int, default=config.PAGE_SIZE)

    s = sub.add_parser("import-json", help="import file JSON cũ vào database")
    s.add_argument("files", nargs="*", help="mặc định: ccts_tickets.json và ccts_checkpoint.json")
    s.add_argument("--overwrite", action="store_true",
                   help="cập nhật ticket đã có bằng dữ liệu trong file (mặc định: bỏ qua)")
    s.add_argument("--no-validate", action="store_true", help="bỏ bước đọc thử toàn bộ file")
    s.add_argument("--dry-run", action="store_true", help="chỉ kiểm tra file, không ghi DB")

    s = sub.add_parser("stats", help="thống kê database")
    s.add_argument("--top", type=int, default=20)

    s = sub.add_parser("find", help="tìm ticket theo cctsTicketPk / cctsTicketId / thirdTicketId")
    s.add_argument("key")
    s.add_argument("--raw", action="store_true", help="in raw_json đầy đủ")

    s = sub.add_parser("list", help="lọc ticket")
    s.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    s.add_argument("--status")
    s.add_argument("--charge-box")
    s.add_argument("--severity")
    s.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("export-json", help="xuất dữ liệu ra JSON")
    s.add_argument("--out")
    s.add_argument("--jsonl", action="store_true", help="mỗi dòng một ticket")
    s.add_argument("--from", dest="date_from")
    s.add_argument("--to", dest="date_to")
    s.add_argument("--force", action="store_true", help="ghi đè file đích nếu đã tồn tại")

    sub.add_parser("check", help="kiểm tra cấu trúc và tình trạng database")
    return p


COMMANDS = {"sync": cmd_sync, "probe": cmd_probe, "import-json": cmd_import, "stats": cmd_stats,
            "find": cmd_find, "list": cmd_list, "export-json": cmd_export, "check": cmd_check}

MENU = [
    ("Đồng bộ từ API (tự tiếp tục nếu dở dang)", ["sync"]),
    ("Chỉ lấy ticket mới", ["sync", "--new"]),
    ("Quét lại đầy đủ từ trang 1", ["sync", "--full"]),
    ("Import JSON cũ (ccts_tickets.json / ccts_checkpoint.json)", ["import-json"]),
    ("Thống kê", ["stats"]),
    ("Tìm ticket", ["find"]),
    ("Xuất JSON", ["export-json"]),
    ("Kiểm tra database", ["check"]),
]


def menu_argv():
    print("\n=== CCTS Ticket Manager ===")
    for i, (label, _) in enumerate(MENU, 1):
        print(f"  {i}. {label}")
    print("  0. Thoát")
    choice = input("Chọn: ").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(MENU):
        return None
    argv = list(MENU[int(choice) - 1][1])
    if argv == ["find"]:
        argv.append(input("Nhập cctsTicketPk hoặc cctsTicketId: ").strip())
    return argv


def main(argv=None):
    setup_logging()
    interactive = argv is None and len(sys.argv) == 1
    if interactive:
        argv = menu_argv()
        if argv is None:
            return 0
    args = build_parser().parse_args(argv)
    if not args.command:
        build_parser().print_help()
        return 0

    conn = None
    try:
        conn = database.connect(args.db)
        return COMMANDS[args.command](conn, args)
    except config.ConfigError as exc:
        log.error("Lỗi cấu hình: %s", exc)
        return 2
    except KeyboardInterrupt:
        log.warning("Đã dừng.")
        return 130
    finally:
        database.close(conn)
        if interactive:
            input("\nNhấn Enter để thoát...")


if __name__ == "__main__":
    sys.exit(main())
