import os

from .formatting import telegram_html
from .http import ApiError, Client
from .providers import required


def configured():
    return all(os.environ.get(name, "").strip() for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"))


class Telegram:
    interval = 1.1  # Stay below Telegram's per-chat message rate limit.

    def __init__(self, display_timezone="UTC"):
        self.client = Client("Telegram")
        self.url = "https://api.telegram.org/bot" + required("TELEGRAM_BOT_TOKEN") + "/sendMessage"
        self.chat_id = required("TELEGRAM_CHAT_ID")
        self.display_timezone = display_timezone

    def send(self, event):
        message = telegram_html(event, self.display_timezone)
        if message is not None:
            try:
                return self.post(message, parse_mode="HTML")
            except ApiError as error:
                # A markup Telegram refuses must not block the queue; plain text still informs.
                if error.status != 400:
                    raise
        self.post(event["text"][:4000])

    def post(self, text, **fields):
        result = self.client.request("POST", self.url, json={"chat_id": self.chat_id, "text": text, **fields,
                                      "link_preview_options": {"is_disabled": True}})
        if not result.get("ok"):
            raise ApiError("Telegram", result.get("error_code"))
