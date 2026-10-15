"""Client minimal de l'API Bot de Telegram : messages, boutons, réponses.

Juste ce qu'il faut à la veille des likes (`ytmgc veille`) : envoyer une
proposition avec des boutons, la modifier, et lire les boutons touchés. Rien
d'autre que la bibliothèque standard ; le jeton du bot vient du `.env`
(`TELEGRAM_BOT_TOKEN`) et n'est jamais écrit ailleurs ni affiché.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

API = "https://api.telegram.org/bot{token}/{method}"

#: Un bouton : (libellé, données renvoyées quand on le touche, 64 octets max).
Button = tuple[str, str]


class TelegramError(RuntimeError):
    pass


def keyboard(rows: list[list[Button]]) -> dict:
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row]
                                for row in rows]}


class Bot:
    def __init__(
        self,
        token: str,
        *,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
    ) -> None:
        if not token:
            raise TelegramError("Jeton du bot absent : TELEGRAM_BOT_TOKEN dans le .env.")
        self._token = token
        self._open = opener or (lambda request, timeout: urllib.request.urlopen(
            request, timeout=timeout))

    def call(self, method: str, *, http_timeout: float = 30, **params: Any) -> Any:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        request = urllib.request.Request(
            API.format(token=self._token, method=method), data=body,
            headers={"Content-Type": "application/json"})
        return self._send(request, method, http_timeout)

    def _send(self, request: urllib.request.Request, method: str, http_timeout: float) -> Any:
        try:
            with self._open(request, http_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                # Le message d'origine contient l'adresse, donc le jeton : on le tait.
                raise TelegramError(f"{method} : erreur HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise TelegramError(f"{method} : Telegram injoignable ({exc.reason})") from None
        if not data.get("ok"):
            raise TelegramError(f"{method} : {data.get('description') or 'refusé'}")
        return data.get("result")

    def updates(self, offset: int | None, wait: int = 0) -> list[dict]:
        """Événements reçus depuis `offset` ; attend jusqu'à `wait` secondes."""
        return self.call("getUpdates", http_timeout=wait + 10, offset=offset, timeout=wait,
                         allowed_updates=["message", "callback_query"]) or []

    def send(self, chat_id: str, text: str, rows: list[list[Button]] | None = None,
             reply_to: int | None = None) -> int:
        result = self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                           disable_web_page_preview=True,
                           reply_markup=keyboard(rows) if rows else None,
                           reply_parameters={"message_id": reply_to} if reply_to else None)
        return int(result["message_id"])

    def send_audio(self, chat_id: str, path: Path, *, title: str = "",
                   performer: str = "") -> int:
        """Envoie un fichier son, écoutable dans la conversation."""
        boundary = uuid.uuid4().hex
        fields = {"chat_id": chat_id, "title": title, "performer": performer}
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode() for name, value in fields.items() if value)
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
                 f'filename="{path.name}"\r\nContent-Type: audio/mp4\r\n\r\n').encode()
        body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(
            API.format(token=self._token, method="sendAudio"), data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return int(self._send(request, "sendAudio", 120)["message_id"])

    def edit(self, chat_id: str, message_id: int, text: str,
             rows: list[list[Button]] | None = None) -> None:
        try:
            self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                      parse_mode="HTML", disable_web_page_preview=True,
                      reply_markup=keyboard(rows) if rows else None)
        except TelegramError as exc:
            if "not modified" not in str(exc):
                raise

    def answer(self, callback_id: str, text: str = "") -> None:
        """Arrête le sablier du bouton touché. Un bouton trop ancien ne se
        répond plus : ce n'est pas une erreur."""
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)
        except TelegramError:
            pass
