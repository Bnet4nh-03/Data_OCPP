# CCTS Ticket Manager — CNPowercore API → SQLite

Lấy ticket từ API `findCCTSTicket` và lưu vào SQLite (`ccts_tickets.db`), có checkpoint để tiếp tục khi bị dừng, import JSON cũ, thống kê, tra cứu và xuất JSON.

## Cấu trúc

| File | Vai trò |
|---|---|
| `main.py` | Điểm chạy: menu + các lệnh dòng lệnh |
| `config.py` | Cấu hình (URL, headers, pageSize, delay, retry, đường dẫn file). **Không chứa cookie/token** |
| `credentials.json` | Cookie `ssoticket` và API token (chỉ để trên máy bạn) |
| `api_client.py` | Gọi API: session, cookie, headers, payload như script gốc; timeout, retry, phát hiện lỗi xác thực |
| `database.py` | Schema SQLite, upsert, truy vấn |
| `sync.py` | Phân trang, checkpoint, tiếp tục, phát hiện bất thường, báo cáo đối chiếu |
| `importer.py` | Import JSON cũ, đọc streaming |
| `test.py` | Script gốc (giữ nguyên, không còn dùng) |

Chương trình tự tạo khi chạy: `ccts_tickets.db`, `ccts_sync_checkpoint.json`, `ccts_manager.log`.

> **Checkpoint mới tên là `ccts_sync_checkpoint.json`**, không phải `ccts_checkpoint.json`. File cũ chứa toàn bộ records; dùng tên khác để chương trình không bao giờ ghi đè lên dữ liệu cũ của bạn.

## 1. Cài đặt (Windows)

```bat
cd D:\00_WORKSPACE\01_Python\04_Takedata_CCVN
pip install -r requirements.txt
```

Chỉ cần `requests`. `sqlite3`, `json`, `argparse`… có sẵn trong Python.

## 2. Cấu hình cookie và token

Tạo `credentials.json` (đã có sẵn, lấy từ `test.py`; mẫu: `credentials.example.json`):

```json
{
  "ssoticket": "giá trị cookie ssoticket",
  "api_token": "giá trị token trong payload"
}
```

Cách lấy giá trị mới khi hết hạn: đăng nhập `console.cnpowercore.com` → F12 → tab **Network** → lọc `findCCTSTicket` → chọn request:
- **Headers → Cookie**: lấy giá trị sau `ssoticket=`
- **Payload**: lấy giá trị `token`

Biến môi trường được ưu tiên hơn file (tiện khi không muốn lưu file):

```powershell
$env:CCTS_SSOTICKET = "..."
$env:CCTS_API_TOKEN = "..."
```

Chương trình không in hay ghi cookie/token ra màn hình hoặc vào log. Đừng chia sẻ `credentials.json` (đã có trong `.gitignore`). `test.py` vẫn chứa cookie/token cũ được viết thẳng trong code. Nên xóa hai giá trị đó khỏi file, hoặc xóa luôn `test.py`.

Nếu cookie/token hết hạn, chương trình dừng ngay (không thử lại liên tục) và báo `LỖI XÁC THỰC`. Cập nhật `credentials.json` rồi chạy `python main.py sync`: chương trình tiếp tục từ chỗ đã dừng.

## 3. Chạy

Chạy `python main.py` (không tham số) để mở menu số. Hoặc dùng lệnh trong CMD/PowerShell:

```bat
python main.py sync                      :: đồng bộ, tự tiếp tục nếu lần trước dở dang
python main.py sync --page-size 100      :: nhanh hơn 10 lần (đã kiểm tra, xem mục 9)
python main.py sync --new                :: chỉ lấy ticket mới (dùng hằng ngày)
python main.py sync --full               :: quét lại toàn bộ từ trang 1
python main.py sync --max-pages 5        :: chạy thử 5 trang
python main.py sync --delay 0.5          :: chậm hơn, nhẹ cho server
python main.py probe --page-size 100     :: kiểm tra phân trang (4 request, không ghi DB)
python main.py import-json               :: import ccts_tickets.json / ccts_checkpoint.json
python main.py stats
python main.py find 8TKWCR6DLHC0         :: theo cctsTicketPk, cctsTicketId hoặc thirdTicketId
python main.py find 8TKWCR6DLHC0 --raw   :: kèm raw_json đầy đủ
python main.py list --from 2026-10-01 --to 2026-10-09 --status Open --limit 100
python main.py list --charge-box BSS-255041002298
python main.py export-json                         :: ra ccts_export_YYYY-MM-DD.json
python main.py export-json --jsonl --out backup.jsonl
python main.py check
```

Thêm `--db đường_dẫn.db` (đặt trước tên lệnh) để dùng database khác.

**Thời gian ước tính:** total khoảng 369.000 ticket. Với pageSize 10 cần khoảng 36.900 request (vài giờ). Với pageSize 100 cần khoảng 3.700 request.

## 4. Import JSON cũ

```bat
python main.py import-json --dry-run                 :: chỉ kiểm tra cấu trúc, không ghi
python main.py import-json                           :: import cả 2 file nếu có
python main.py import-json D:\backup\old.json        :: file bất kỳ
python main.py import-json --overwrite               :: cho phép ghi đè ticket đã có
```

- Hỗ trợ 3 dạng: `[ ... ]`, `{"records": [...]}` (checkpoint cũ), `{"data": {"list": [...]}}` (response API).
- Trước khi ghi, chương trình đọc thử toàn bộ file. Nếu file lỗi, chương trình báo vị trí ký tự lỗi và không ghi gì vào DB.
- File được đọc streaming (từng ticket một) nên file lớn không chiếm nhiều RAM. Mỗi transaction ghi 1.000 ticket.
- **Mặc định ticket đã có trong DB sẽ được bỏ qua.** Lý do: dữ liệu từ API mới hơn dữ liệu trong file cũ. Dùng `--overwrite` nếu muốn ghi đè.
- File JSON gốc chỉ được mở để đọc.

Thứ tự khuyến nghị: `import-json` trước, sau đó `sync`. Ticket đã import vẫn được cập nhật theo API khi gặp lại.

## 5. Tiếp tục khi bị dừng

Có thể dừng bằng Ctrl+C, chương trình có thể bị tắt, mất mạng hoặc cookie hết hạn. Trong mọi trường hợp chỉ cần chạy lại:

```bat
python main.py sync
```

- Mỗi trang được ghi trong một transaction. Trang đã commit thì không mất. Checkpoint được ghi sau khi commit, theo kiểu ghi file tạm rồi `os.replace`, nên file không bị hỏng giữa chừng.
- Ticket sắp xếp theo `createTime` giảm dần, nên ticket mới chèn lên đầu và đẩy dữ liệu cũ xuống các trang sau. Vì vậy khi tiếp tục, chương trình:
  1. tính độ trôi theo thay đổi của total và lùi thêm 3 trang dự phòng;
  2. **xác minh** trang bắt đầu có chứa ticket đã có trong DB. Nếu không có, chương trình lùi tiếp (tối đa 10 lần) rồi mới quét tiếp;
  3. sau khi quét tới cuối, quét lại phần đầu danh sách để lấy ticket phát sinh trong lúc chạy.
- Nếu đã đổi `--page-size` giữa hai lần chạy, chương trình quy đổi theo vị trí bản ghi.
- Muốn chắc chắn đầy đủ thì chạy `python main.py sync --full`. Khóa chính chống trùng nên quét lại không tạo bản ghi lặp. Các ticket đổi trạng thái được cập nhật, bản cũ được lưu vào `ticket_history`.

## 6. Database

**Bảng `tickets`** (khóa chính `ccts_ticket_pk` = `cctsTicketPk`, lưu dạng TEXT để giữ nguyên số 19 chữ số):

- Các cột tra cứu: `ccts_ticket_id`, `third_ticket_id`, `ccts_ticket_name`, `ccts_ticket_status`, `severity`, `urgency_level`, `charge_box_pk`, `charge_box_id`, `charge_box_model`, `station_code`, `operator_name`, `country`, `address`, `error_code`, `error_name`, `error_category`, `error_desc`, `ticket_source`, `ticket_creator`, `timezone`, `create_time`, `occurrence_time`, `sla_time`.
- `create_time_norm`: createTime chuẩn hóa `YYYY-MM-DD HH:MM:SS`, dùng để lọc theo thời gian.
- `raw_json`: object gốc mới nhất. Giữ mọi trường, kể cả `assistantMap`, `ownerEntities`, `assistantEntities`…
- `raw_hash`, `source`, `first_seen_at`, `last_seen_at`, `updated_at`.

Các cột tra cứu được ánh xạ theo cấu trúc thực tế của 749 ticket trong file cũ. Trường bị thiếu hoặc null được lưu là NULL.

**Bảng phụ:**
- `ticket_history`: raw_json cũ trước mỗi lần ticket thay đổi.
- `tickets_no_pk`: ticket thiếu `cctsTicketPk`. Chỉ gộp khi nội dung giống hệt nhau, không gộp theo trạm hay thời gian. Đổi `MISSING_PK_POLICY = "skip"` trong `config.py` nếu chỉ muốn ghi log.
- `sync_runs`: lịch sử các lần đồng bộ.

**Index:** khóa chính, `ccts_ticket_id`, `ccts_ticket_status`, `severity`, `charge_box_id`, `create_time_norm`.

**Cập nhật an toàn:** chương trình dùng `INSERT ... ON CONFLICT DO UPDATE`, không dùng `INSERT OR REPLACE`. `INSERT OR REPLACE` xóa bản ghi rồi tạo lại, nên sẽ làm mất `first_seen_at`.

**WAL:** bật `journal_mode=WAL`. Ghi nhanh hơn, và transaction đã commit không mất khi chương trình bị tắt đột ngột. Khi chạy sẽ thấy thêm `ccts_tickets.db-wal` và `-shm`. Lúc thoát, chương trình gộp WAL vào file `.db`. **Khi sao lưu, hãy copy file lúc chương trình không chạy**, hoặc dùng `export-json`. Chương trình dùng `synchronous=FULL` để ưu tiên an toàn hơn tốc độ.

## 7. Ví dụ log (từ lần chạy thật ngày 10/10/2026)

Import checkpoint cũ:

```
10:27:13 Import ccts_checkpoint.json (4.2 MB)
10:27:13 Bước 1: kiểm tra cấu trúc file...
10:27:13   Định dạng       : checkpoint cũ (records)
10:27:13   total           : 368707
10:27:13   last_completed_page: 75
10:27:13   Phần tử         : 749 (object: 749)
10:27:13   Có cctsTicketPk : 749 (khác nhau: 749)
10:27:13 Bước 2: ghi vào database (batch 1000, BỎ QUA ticket đã có)...
10:27:13   Thêm mới        : 749
10:27:13   Tổng ticket trong DB: 749
```

Sync thử 4 trang, pageSize 100, chạy sau khi đã import:

```
10:30:30 Gọi trang 1 (pageSize=100) để lấy tổng số ticket...
10:30:32 API báo total: 369,054 | trang 1 trả 100 ticket | DB hiện có 749
10:30:32 Trang 1/3,691 | nhận 100 | mới 100 | cập nhật 0 | DB 849 | API total 369,054
10:30:34 Trang 2/3,691 | nhận 100 | mới 100 | cập nhật 0 | DB 949 | API total 369,054
10:30:35 Trang 3/3,691 | nhận 100 | mới 99 | cập nhật 0 | DB 1,048 | API total 369,055
10:30:36 Trang 4/3,691 | nhận 100 | mới 48 | cập nhật 52 | DB 1,096 | API total 369,055
10:30:36 KẾT QUẢ ĐỒNG BỘ - trạng thái: stopped_limit (Đạt giới hạn --max-pages 4)
10:30:36   Ticket khác nhau (lần chạy): 399
10:30:36   API total đầu / cuối    : 369,054 / 369,055 (min 369,054, max 369,055)
10:30:36   ĐỐI CHIẾU: DB ÍT HƠN total API 367,959 ticket.
```

Log này cho thấy trôi trang có thật: total tăng 1 trong lúc chạy, và 400 bản ghi nhận được chỉ có 399 ticket khác nhau. 52 ticket từ file cũ đã đổi trạng thái nên được cập nhật. Khi chạy thật, tiến độ được in mỗi 10 trang (`PROGRESS_EVERY` trong `sync.py`). Log đầy đủ nằm trong `ccts_manager.log`.

## 8. Truy vấn SQLite trực tiếp

Có thể mở bằng [DB Browser for SQLite](https://sqlitebrowser.org/) hoặc Python:

```sql
-- Ticket theo trạng thái
SELECT ccts_ticket_status, COUNT(*) FROM tickets GROUP BY 1 ORDER BY 2 DESC;

-- Top trạm sạc nhiều ticket nhất
SELECT charge_box_id, station_code, COUNT(*) n FROM tickets
GROUP BY charge_box_id ORDER BY n DESC LIMIT 20;

-- Ticket Open trong khoảng thời gian
SELECT ccts_ticket_id, create_time, charge_box_id, error_name FROM tickets
WHERE ccts_ticket_status = 'Open'
  AND create_time_norm BETWEEN '2026-10-01 00:00:00' AND '2026-10-09 23:59:59'
ORDER BY create_time_norm DESC;

-- Đọc trường lồng nhau trong raw_json (JSON1 có sẵn trong SQLite của Python)
SELECT ccts_ticket_id, json_extract(raw_json, '$.slaTimeout') AS sla_timeout,
       json_extract(raw_json, '$.assistantName') AS assistants
FROM tickets WHERE json_extract(raw_json, '$.slaTimeout') = 1;

-- Lỗi theo ngày
SELECT substr(create_time_norm, 1, 10) d, error_name, COUNT(*) FROM tickets
GROUP BY d, error_name ORDER BY d DESC, 3 DESC;

-- Lịch sử thay đổi của một ticket
SELECT valid_until, json_extract(raw_json, '$.cctsTicketStatus') FROM ticket_history
WHERE ccts_ticket_pk = '1161124699679096833' ORDER BY id;
```

Trong Python, luôn truyền tham số bằng `?`:

```python
import sqlite3
conn = sqlite3.connect("ccts_tickets.db")
rows = conn.execute("SELECT * FROM tickets WHERE charge_box_id = ?", ("BSS-255041002298",)).fetchall()
```

## 9. Giả định về phân trang và giới hạn

Đã kiểm tra bằng `probe` ngày 10/10/2026: với pageSize 10, 100 và 500, `trang1(2N)` đúng bằng `trang1(N) + trang2(N)`. Nghĩa là API phân trang theo offset và tôn trọng `pageSize` ít nhất tới 500. Request pageSize 1000 chậm rõ rệt, nên dùng 100.

Các giả định cần lưu ý:

1. **Thứ tự theo `createTime` giảm dần.** Điều này được suy ra từ dữ liệu (749 ticket cũ đều theo thứ tự này), không có tài liệu API xác nhận. Ticket mới làm dữ liệu trôi xuống các trang sau. Hậu quả là đọc lặp lại (đã khử trùng bằng khóa chính), không gây mất dữ liệu.
2. **Nếu ticket bị xóa hoặc ẩn khỏi danh sách trong lúc quét,** dữ liệu trôi lên trên và có thể bỏ sót. Chương trình không tự phát hiện được việc này. Báo cáo cuối sẽ cho thấy DB ít hơn total; khi đó chạy `sync --full`.
3. **Ticket cùng `createTime`** có thể đổi thứ tự giữa các request nếu API không sắp xếp thêm theo khóa phụ.
4. **Trang trả ít hơn pageSize** (script cũ: 75 trang chỉ được 749 bản ghi) không được coi là trang cuối. Chương trình chỉ dừng khi có 2 trang rỗng liên tiếp, khi API trả lại đúng trang trước (tức là bỏ qua pageNum), khi 20 trang liên tiếp không có ticket mới trong lần chạy, hoặc khi đọc vượt quá số trang dự kiến theo total + 5%.
5. **`data.total` chỉ dùng để đối chiếu.** Báo cáo cuối nêu rõ chênh lệch. Số lượng bằng nhau vẫn chưa chứng minh từng ticket đều khớp.
6. **`sync --new` chỉ lấy ticket mới ở đầu danh sách.** Thay đổi trạng thái của ticket cũ chỉ được cập nhật khi quét lại tới trang chứa nó (`sync` / `sync --full`).
7. Ticket được tạo sau lần đọc trang 1 cuối cùng sẽ có ở lần `sync --new` tiếp theo.
8. API có thể chỉ trả ticket thuộc quyền của tài khoản đang đăng nhập.
9. `createTime` được lưu theo giờ API trả về, với `timezoneOffset = 420` (UTC+7). Ticket ở múi giờ khác (Manila, Jakarta…) không được quy đổi.

Đề xuất sử dụng: chạy `import-json`, sau đó `sync --page-size 100` một lần đầy đủ. Hằng ngày chạy `sync --new --page-size 100`. Định kỳ chạy `sync --full --page-size 100` và `check`.
