"""
Cấu hình chương trình CCTS Ticket Manager.

Thông tin xác thực (cookie ssoticket + API token) KHÔNG nằm trong file này.
Chúng được đọc theo thứ tự ưu tiên:
  1. Biến môi trường CCTS_SSOTICKET và CCTS_API_TOKEN
  2. File credentials.json (cùng thư mục với main.py)
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------- API
API_URL = "https://cloud.cnpowercore.com:8091/ccts/cctsTicket/findCCTSTicket"
COOKIE_DOMAIN = "cloud.cnpowercore.com"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://console.cnpowercore.com",
    "Referer": "https://console.cnpowercore.com/",
}

TIMEZONE_OFFSET = 420
SEARCH_TYPE_FOR_CHARGE_BOX_ID = 1

# Script gốc dùng 10. Có thể tăng sau khi đã kiểm tra bằng: python main.py probe --page-size 50
PAGE_SIZE = 1000

# Độ trễ (giây) giữa các request
REQUEST_DELAY = 15

# (connect timeout, read timeout) tính bằng giây
REQUEST_TIMEOUT = (15, 180)

# Số lần thử lại khi lỗi mạng / timeout / HTTP 5xx / 429
MAX_RETRIES = 5
RETRY_BACKOFF = 2.0  # giây, nhân đôi sau mỗi lần thử

# ---------------------------------------------------------------- Đồng bộ
# Số trang lùi lại khi tiếp tục (resume) để bù cho việc dữ liệu bị trôi trang
RESUME_OVERLAP_PAGES = 3
# Số lần lùi thêm nếu trang resume không chứa ticket nào đã có trong DB
RESUME_MAX_BACKTRACK = 10

# Số trang rỗng liên tiếp để xác nhận đã hết dữ liệu
EMPTY_PAGES_TO_STOP = 2

# Dừng nếu liên tiếp N trang chỉ chứa ticket đã nhận TRONG LẦN CHẠY NÀY
# (dấu hiệu API bỏ qua pageNum hoặc phân trang bất thường)
MAX_CONSECUTIVE_DUPLICATE_PAGES = 20

# Chế độ --new: dừng sau N trang liên tiếp không có ticket mới
INCREMENTAL_STOP_PAGES = 3

# Biên an toàn: cho phép đọc vượt số trang dự kiến (theo total) thêm bao nhiêu %
EXTRA_PAGES_RATIO = 0.05
EXTRA_PAGES_MIN = 20

# Ticket không có cctsTicketPk:
#   "quarantine" -> lưu vào bảng tickets_no_pk (chỉ khử trùng khi nội dung giống hệt)
#   "skip"       -> bỏ qua, chỉ ghi log
MISSING_PK_POLICY = "quarantine"

# Lưu raw_json cũ vào bảng ticket_history mỗi khi ticket thay đổi
KEEP_HISTORY = True

# ---------------------------------------------------------------- File
DB_FILE = BASE_DIR / "ccts_tickets.db"
# Tên khác với ccts_checkpoint.json cũ (định dạng cũ chứa toàn bộ records)
# để không bao giờ ghi đè lên file dữ liệu cũ.
CHECKPOINT_FILE = BASE_DIR / "ccts_sync_checkpoint.json"
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
LOG_FILE = BASE_DIR / "ccts_manager.log"

IMPORT_BATCH_SIZE = 1000


class ConfigError(Exception):
    pass


def load_credentials():
    """Trả về (ssoticket, api_token). Không bao giờ in giá trị ra màn hình."""
    ssoticket = os.environ.get("CCTS_SSOTICKET", "").strip()
    api_token = os.environ.get("CCTS_API_TOKEN", "").strip()

    if (not ssoticket or not api_token) and CREDENTIALS_FILE.exists():
        try:
            with CREDENTIALS_FILE.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"{CREDENTIALS_FILE.name} không phải JSON hợp lệ: {exc}"
            ) from exc
        ssoticket = ssoticket or str(data.get("ssoticket") or "").strip()
        api_token = api_token or str(data.get("api_token") or "").strip()

    placeholders = {"", "YOUR_SSOTICKET", "YOUR_API_TOKEN", "YOUR_CURRENT_API_TOKEN"}
    if ssoticket in placeholders or api_token in placeholders:
        raise ConfigError(
            "Thiếu cookie ssoticket hoặc API token. Hãy tạo file credentials.json "
            "(xem credentials.example.json) hoặc đặt biến môi trường "
            "CCTS_SSOTICKET / CCTS_API_TOKEN."
        )
    return ssoticket, api_token
