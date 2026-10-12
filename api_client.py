"""Gọi API CNPowercore findCCTSTicket. Giữ nguyên headers, cookie và payload của script gốc."""

import logging
import time

import requests

import config

log = logging.getLogger("ccts")


class ApiError(Exception):
    """Lỗi API có thể thử lại hoặc không xác định."""


class AuthError(ApiError):
    """Cookie / token hết hạn hoặc không hợp lệ. Không thử lại."""


_AUTH_HINTS = ("token", "login", "auth", "sso", "expired", "unauthor", "permission", "登录")


class CctsClient:
    def __init__(self, ssoticket, api_token, page_size=config.PAGE_SIZE):
        self.api_token = api_token
        self.page_size = page_size
        self.session = requests.Session()
        self.session.headers.update(config.HEADERS)
        self.session.cookies.set(
            "ssoticket", ssoticket, domain=config.COOKIE_DOMAIN, path="/"
        )

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _payload(self, page_num, page_size):
        return {
            "page": {"pageNum": page_num, "pageSize": page_size},
            "timezoneOffset": config.TIMEZONE_OFFSET,
            "searchTypeForChargeBoxId": config.SEARCH_TYPE_FOR_CHARGE_BOX_ID,
            "token": self.api_token,
        }

    def fetch_page(self, page_num, page_size=None):
        """
        Trả về (records, total). total = None nếu API không cung cấp.
        Thử lại với lỗi mạng/timeout/5xx/429; dừng ngay với lỗi xác thực.
        """
        page_size = page_size or self.page_size
        last_exc = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                return self._fetch_once(page_num, page_size)
            except AuthError:
                raise
            except (requests.ConnectionError, requests.Timeout, ApiError) as exc:
                last_exc = exc
                if attempt == config.MAX_RETRIES:
                    break
                wait = config.RETRY_BACKOFF * (2 ** (attempt - 1))
                log.warning(
                    "Trang %s lỗi (lần %s/%s): %s. Thử lại sau %.0fs...",
                    page_num, attempt, config.MAX_RETRIES, _short(exc), wait,
                )
                time.sleep(wait)
        raise ApiError(
            f"Trang {page_num} thất bại sau {config.MAX_RETRIES} lần thử: {_short(last_exc)}"
        )

    def _fetch_once(self, page_num, page_size):
        resp = self.session.post(
            config.API_URL,
            json=self._payload(page_num, page_size),
            timeout=config.REQUEST_TIMEOUT,
        )

        if resp.status_code in (401, 403):
            raise AuthError(f"HTTP {resp.status_code}: cookie ssoticket hoặc token không hợp lệ/hết hạn")
        if resp.status_code == 429 or resp.status_code >= 500:
            raise ApiError(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            # Lỗi 4xx khác thường không tự khỏi khi thử lại
            raise AuthError(f"HTTP {resp.status_code} (kiểm tra cookie/token/payload)")

        try:
            result = resp.json()
        except ValueError:
            ctype = resp.headers.get("Content-Type", "")
            if "html" in ctype.lower():
                raise AuthError(
                    "API trả về HTML thay vì JSON - có thể đã bị chuyển tới trang đăng nhập"
                )
            raise ApiError(f"Response không phải JSON (Content-Type: {ctype})")

        if not isinstance(result, dict):
            raise ApiError(f"Response JSON không phải object: {type(result).__name__}")

        if result.get("success") is not True:
            code = result.get("code")
            message = str(result.get("message") or "")
            text = f"{code} {message}".lower()
            if any(h in text for h in _AUTH_HINTS):
                raise AuthError(f"API từ chối xác thực: {code} - {message}")
            raise ApiError(f"API báo lỗi: {code} - {message}")

        data = result.get("data")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ApiError(f"'data' không phải object: {type(data).__name__}")

        records = data.get("list")
        if records is None:
            records = []
        if not isinstance(records, list):
            raise ApiError(f"'data.list' không phải danh sách: {type(records).__name__}")

        total = data.get("total")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            log.warning("data.total không phải số: %r", total)
            total = None

        return records, total


def _short(exc):
    text = str(exc)
    return text if len(text) <= 200 else text[:200] + "..."
