import re
import time

import requests


class ApiError(RuntimeError):
    def __init__(self, service, status=None, reason=None, detail=None):
        self.status = status
        self.reason = reason
        # Never include response bodies or request URLs: Telegram URLs contain a key.
        message = f"{service}: HTTP {status}" if status else f"{service}: connection failed"
        super().__init__(message + (f" ({reason})" if reason else "") + (f": {detail}" if detail else ""))


class Client:
    def __init__(self, service, session=None):
        self.service = service
        self.session = session or requests.Session()

    def request(self, method, url, **kwargs):
        safe_retry = kwargs.pop("safe_retry", method == "GET")
        attempts = 3 if safe_retry else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, url, timeout=(10, 40),
                                                allow_redirects=False, **kwargs)
            except requests.RequestException:
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(self.service) from None
            reason = None
            detail = None
            if self.service == "Google Play" and response.status_code >= 400:
                try:
                    error = response.json().get("error", {})
                    reasons = [item.get("reason", "") for item in error.get("errors", [])]
                    reasons += [item.get("reason", "") for item in error.get("details", [])]
                    reasons.append(error.get("status", ""))
                    reason = next((item for item in reasons if isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item)), None)
                    message = error.get("message", "")
                    if isinstance(message, str) and not re.search(r"(?i)token|bearer|private.key|credential", message):
                        detail = re.sub(r"https?://\S+", "[endpoint]", " ".join(message.split()))[:240]
                except (ValueError, AttributeError, TypeError):
                    pass
            if response.status_code == 429 or response.status_code >= 500 or reason in ("rateLimitExceeded", "userRateLimitExceeded"):
                if attempt + 1 < attempts:
                    retry = response.headers.get("Retry-After", "")
                    time.sleep(min(int(retry), 30) if retry.isdigit() else 2 ** attempt)
                    continue
            if not 200 <= response.status_code < 300:
                raise ApiError(self.service, response.status_code, reason, detail)
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                raise ApiError(self.service, response.status_code) from None
