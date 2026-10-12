
import requests
import json
import time
from pathlib import Path

url = (
    "https://cloud.cnpowercore.com:8091/"
    "ccts/cctsTicket/findCCTSTicket"
)

session = requests.Session()

# Thay bằng thông tin hiện tại đang hoạt động của bạn
session.cookies.set(
    "ssoticket",
    "VfSE2VX7YiWGGo/YIDWb1h0Xt+6KG+oKA+KrCBCwtJouXYc4ii4ZlgVEbEd7xghq9Vk7N7znhrIL5X+ob/1zrR1zwVdEKa5OWH4LD3k+1mz1XTPQ68OgbGQiVfrfVmGBI4CC20pBfzTnc5kBhQvSLCeYQDx+Ce/yUdzARRC7CLV4VgBtqI34wGIo2tD9BfiPrV/ncCUF14DIdsiNDVM6cEXBhATqpxPOEfw4429Bw5LhU1fQmy/Lm/XJ+CE1tlBA1z+QQ//B5NZjpfPOBBuYt993Fc5RNVR8le+snrhUKgJdVuI+EaOrPqBQ13qlf/HsJE3W+2+3BfCSxYR1jFW58Q==",
    domain="cloud.cnpowercore.com",
    path="/",
)

headers = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://console.cnpowercore.com",
    "Referer": "https://console.cnpowercore.com/",
}

API_TOKEN = "YOUR_CURRENT_API_TOKEN"

PAGE_SIZE = 10
OUTPUT_FILE = Path("ccts_tickets.json")
CHECKPOINT_FILE = Path("ccts_checkpoint.json")


def fetch_page(page_num):
    payload = {
        "page": {
            "pageNum": page_num,
            "pageSize": PAGE_SIZE,
        },
        "timezoneOffset": 420,
        "searchTypeForChargeBoxId": 1,
        "token": 'WEOhMui9ZaV2xW1gBWlUNy0L/Ny7O/ctKAM+sT8T04CPxzjAa01NVv3Yo46ElqBx9u5HcuW8ilbNli6KtdgmyU3/K+K2ASjDDW4vbvWrsluUGTSnRw5nuQzo8Tq7DxWdv7QyBLMJzQ4i+pqu6uUhnGwLz3LF1VunVNKmWSZ3gpaORGsestlfpS7XCRjZORHfLtTmrdu04BOuCdBlV8CuVMDuX5zAU+xno7/pexjjmx0meCvqp9j2+ikUfcWLoE/G4CNgX9JVmhn1mJOlkyFt/KLEm/3ylc2VseYStvWnji/jOAHmNwtU5mQbAmRBioJvirRtuJY9i2rUg3w9s1ajiQ==',
    }

    response = session.post(
        url,
        headers=headers,
        json=payload,
        timeout=60,
    )

    response.raise_for_status()
    result = response.json()

    if result.get("success") is not True:
        raise RuntimeError(
            f"API lỗi ở trang {page_num}: "
            f"{result.get('code')} - {result.get('message')}"
        )

    data = result.get("data") or {}

    return data


def save_json(path, data):
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
        )


def main():
    # Lấy trang đầu để xác định tổng số ticket
    first_data = fetch_page(1)

    total = int(first_data.get("total") or 0)
    first_records = first_data.get("list") or []

    if total == 0:
        save_json(OUTPUT_FILE, [])
        print("API không có bản ghi nào.")
        return

    if not first_records:
        raise RuntimeError(
            "Trang đầu rỗng nhưng total > 0. Dừng để tránh xuất thiếu dữ liệu."
        )

    # Khởi tạo checkpoint
    records = list(first_records)
    seen_ids = {
        str(item["cctsTicketPk"])
        for item in records
        if item.get("cctsTicketPk") is not None
    }

    save_json(CHECKPOINT_FILE, {
        "total": total,
        "last_completed_page": 1,
        "records": records,
    })

    print(f"Tổng API báo: {total:,}")
    print(f"Trang 1 lấy được: {len(first_records)}")
    print("Bắt đầu tải dữ liệu...")

    page_num = 2
    empty_pages = 0

    while len(records) < total:
        data = fetch_page(page_num)
        page_records = data.get("list") or []

        if not page_records:
            empty_pages += 1
            print(f"Trang {page_num} rỗng.")

            if empty_pages >= 2:
                print(
                    "Hai trang liên tiếp rỗng. "
                    "Dừng để tránh vòng lặp vô hạn."
                )
                break

            page_num += 1
            continue

        empty_pages = 0
        new_count = 0

        for item in page_records:
            ticket_id = item.get("cctsTicketPk")

            # Khử trùng lặp nếu API trả lại bản ghi ở trang trước
            if ticket_id is not None:
                ticket_id = str(ticket_id)

                if ticket_id in seen_ids:
                    continue

                seen_ids.add(ticket_id)

            records.append(item)
            new_count += 1

        # Lưu checkpoint sau mỗi trang
        save_json(CHECKPOINT_FILE, {
            "total": total,
            "last_completed_page": page_num,
            "records": records,
        })

        if page_num % 20 == 0 or len(records) >= total:
            print(
                f"Trang {page_num} | "
                f"Nhận {len(page_records)} | "
                f"Mới {new_count} | "
                f"Tổng đã lưu {len(records):,}/{total:,}"
            )

        if new_count == 0:
            print(
                "Trang này không có ticket mới. "
                "Kiểm tra phân trang trước khi tiếp tục."
            )

        page_num += 1
        time.sleep(0.15)

    # Xuất JSON cuối cùng
    save_json(OUTPUT_FILE, records)

    print("\nHoàn tất!")
    print(f"Tổng bản ghi đã lưu: {len(records):,}")
    print(f"File JSON: {OUTPUT_FILE.resolve()}")

    if len(records) != total:
        print(
            "CẢNH BÁO: số bản ghi lưu khác total API. "
            "Cần kiểm tra phân trang hoặc dữ liệu thay đổi trong lúc tải."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nĐã dừng theo yêu cầu. "
            "Checkpoint được lưu đến trang hoàn thành gần nhất."
        )
    except Exception as exc:
        print(f"\nCó lỗi: {exc}")
        print(
            "Nếu checkpoint đã được tạo, dữ liệu đã tải "
            "được lưu trong ccts_checkpoint.json."
        )