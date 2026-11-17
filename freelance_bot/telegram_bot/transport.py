"""Minimal Telegram Bot API polling transport.

Using urllib keeps the first launch reliable even before optional UI/AI
dependencies are installed. A richer handler layer can subscribe to commands
without changing the transport.
"""

import json
import threading
import time
from dataclasses import dataclass
from urllib import parse, request
from urllib.error import HTTPError
from collections.abc import Callable


@dataclass(slots=True)
class TelegramReply:
    """Text and inline keyboard returned by an owner command or callback."""

    text: str
    reply_markup: dict | None = None


@dataclass(slots=True)
class TelegramCallbackResult:
    """Optional edit plus additional messages produced by an inline callback."""

    edit: TelegramReply | None = None
    messages: list[TelegramReply] | None = None
    notice: str = ""


class TelegramPolling:
    EXPECTED_COMMANDS = (
        {"command": "start", "description": "Включить получение заказов"},
        {"command": "stop", "description": "Остановить доставку заказов"},
        {"command": "menu", "description": "Открыть главное меню"},
        {"command": "orders", "description": "Проверить актуальные заказы"},
        {"command": "status", "description": "Показать состояние системы"},
    )

    def __init__(self, token: str, allowed_user_ids: set[int] | None = None, *, checkpoint_load=None, checkpoint_save=None) -> None:
        self.token = token.strip()
        self.allowed_user_ids = allowed_user_ids or set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._checkpoint_load = checkpoint_load
        self._checkpoint_save = checkpoint_save
        self._offset = self._load_checkpoint()
        self._handlers: dict[str, Callable[[int, str], str | TelegramReply | list[TelegramReply]]] = {}
        self._menu_handlers: dict[str, Callable[[int, str], str | TelegramReply | list[TelegramReply]]] = {}
        self._callback_handler: Callable[[int, str], TelegramReply | TelegramCallbackResult | None] | None = None
        self._post_handler: Callable[[dict], None] | None = None
        self._idle_handler: Callable[[], None] | None = None
        self._self_check_error_handler: Callable[[str], None] | None = None
        self._document_handler: Callable[[int, dict, dict], str | TelegramReply | list[TelegramReply] | None] | None = None
        self.last_error = ""
        self.connected = False

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def command(self, name: str, handler: Callable[[int, str], str | TelegramReply | list[TelegramReply]]) -> None:
        self._handlers[name.removeprefix("/")] = handler

    def menu(self, text: str, handler: Callable[[int, str], str | TelegramReply | list[TelegramReply]]) -> None:
        """Register a reply-keyboard action for an allowed user."""
        self._menu_handlers[text] = handler

    def on_callback(self, handler: Callable[[int, str], TelegramReply | TelegramCallbackResult | None]) -> None:
        """Register the owner-only inline keyboard callback handler."""
        self._callback_handler = handler

    def on_post(self, handler: Callable[[dict], None]) -> None:
        """Receive text from channels/groups where the bot can see posts."""
        self._post_handler = handler

    def on_idle(self, handler: Callable[[], None]) -> None:
        """Run durable pending work after a polling response without blocking intake."""
        self._idle_handler = handler

    def on_self_check_error(self, handler: Callable[[str], None]) -> None:
        """Record startup self-check failures through the application's diagnostic log."""
        self._self_check_error_handler = handler

    @property
    def registered_commands(self) -> tuple[str, ...]:
        """Expose local command handlers so startup can verify the control plane."""
        return tuple(sorted(self._handlers))

    def on_document(self, handler: Callable[[int, dict, dict], str | TelegramReply | list[TelegramReply] | None]) -> None:
        """Register a handler for owner-sent documents without reading file content in Qt."""
        self._document_handler = handler

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-polling", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        """Restore Bot API commands before polling, then keep polling on self-check failure too."""
        try:
            self._startup_self_check()
        except Exception as exc:  # Self-check diagnostics must never disable receiving updates.
            self.last_error = str(exc)
            if self._self_check_error_handler:
                try:
                    self._self_check_error_handler("startup_self_check_failed")
                except Exception:
                    pass
        self._poll()

    def _startup_self_check(self) -> None:
        """Verify Bot API access and restore the exact public command list if needed."""
        expected_handlers = {item["command"] for item in self.EXPECTED_COMMANDS}
        if expected_handlers != set(self._handlers):
            raise RuntimeError("Telegram command handlers are incomplete or contain unexpected commands")
        if self._post_handler is None or self._idle_handler is None:
            raise RuntimeError("Telegram intake and delivery handlers are incomplete")
        me = self._call("getMe", {})
        if not me or not me.get("ok") or not isinstance(me.get("result"), dict):
            raise RuntimeError("Telegram getMe failed")
        self.connected = True
        registered = self._call("getMyCommands", {})
        if not registered or not registered.get("ok") or not isinstance(registered.get("result"), list):
            raise RuntimeError("Telegram getMyCommands failed")
        if registered["result"] != list(self.EXPECTED_COMMANDS):
            restored = self._call("setMyCommands", {
                "commands": json.dumps(self.EXPECTED_COMMANDS, ensure_ascii=False),
            })
            if not restored or not restored.get("ok"):
                raise RuntimeError("Telegram setMyCommands failed")

    def stop(self) -> None:
        self._stop.set()
        self.connected = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def send_message(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> bool:
        if not self.enabled:
            return False
        result = self.send_message_result(chat_id, text, reply_markup)
        return bool(result and result.get("ok"))

    def send_message_result(self, chat_id: int | str, text: str, reply_markup: dict | None = None) -> dict | None:
        """Send a message and return Telegram's result so callers can persist its message ID."""
        if not self.enabled:
            return None
        values = {"chat_id": chat_id, "text": text}
        if reply_markup:
            values["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        try:
            return self._call("sendMessage", values)
        except HTTPError as exc:
            # Telegram's HTTP error body contains a definitive {"ok": false}
            # response, so callers can release a delivery reservation safely.
            try:
                return json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise exc

    def edit_message(self, chat_id: int | str, message_id: int, text: str, reply_markup: dict | None = None) -> bool:
        """Edit an existing bot message while keeping navigation in one card."""
        values = {"chat_id": chat_id, "message_id": message_id, "text": text}
        values["reply_markup"] = json.dumps(reply_markup or {"inline_keyboard": []}, ensure_ascii=False)
        result = self._call("editMessageText", values)
        if result and result.get("ok"):
            return True
        return bool(result and "message is not modified" in str(result.get("description", "")).lower())

    def answer_callback(self, callback_query_id: str, text: str = "") -> bool:
        """Acknowledge an inline button so Telegram removes its loading spinner."""
        values = {"callback_query_id": callback_query_id}
        if text:
            values["text"] = text
        result = self._call("answerCallbackQuery", values)
        return bool(result and result.get("ok"))

    def get_chat(self, chat_id: int | str) -> dict | None:
        """Ask Bot API for a chat only; this never joins a chat or changes access."""
        if not self.enabled:
            return None
        result = self._call("getChat", {"chat_id": chat_id})
        return result.get("result") if result and result.get("ok") and isinstance(result.get("result"), dict) else None

    def download_document(self, file_id: str, max_bytes: int = 1_048_576) -> bytes:
        """Download a bounded Telegram document through the Bot API."""
        file_info = self._call("getFile", {"file_id": file_id})
        file_path = ((file_info or {}).get("result") or {}).get("file_path")
        if not file_path:
            raise ValueError("Telegram не вернул путь к файлу.")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with request.urlopen(request.Request(url), timeout=35) as response:
            content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("Файл слишком большой. Максимальный размер — 1 МБ.")
        return content

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                response = self._call("getUpdates", {"timeout": 20, "offset": self._offset})
                if not response or not response.get("ok"):
                    self.connected = False
                    time.sleep(3)
                    continue
                self.connected = True
                for update in response.get("result", []):
                    if not self.process_update(update):
                        break
                if self._idle_handler:
                    self._idle_handler()
            except Exception as exc:  # Network failures must not kill the desktop app.
                self.connected = False
                self.last_error = str(exc)
                self._stop.wait(5)

    def process_update(self, update: dict) -> bool:
        """Handle and checkpoint one update only after its durable handler succeeds."""
        # Публикация должна попасть в SQLite inbox до сдвига общего Bot API offset.
        if not self._handle_update(update):
            return False
        try:
            next_offset = int(update["update_id"]) + 1
            if self._checkpoint_save:
                self._checkpoint_save(next_offset)
            self._offset = next_offset
            return True
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            return False

    def _handle_update(self, update: dict) -> bool:
        callback = update.get("callback_query") or {}
        if callback:
            user_id = int((callback.get("from") or {}).get("id", 0))
            callback_id = str(callback.get("id", ""))
            if user_id not in self.allowed_user_ids:
                if callback_id:
                    self.answer_callback(callback_id, "Недостаточно прав.")
                return True
            message = callback.get("message") or {}
            chat = message.get("chat") or {}
            if not self._callback_handler or not chat or not message.get("message_id"):
                if callback_id:
                    self.answer_callback(callback_id)
                return True
            try:
                result = self._callback_handler(user_id, str(callback.get("data", "")))
            except Exception:
                if callback_id:
                    try:
                        self.answer_callback(callback_id, "Не удалось выполнить действие.")
                    except Exception:
                        pass
                return True
            if result is None:
                if callback_id:
                    self.answer_callback(callback_id)
                return True
            if isinstance(result, TelegramReply):
                result = TelegramCallbackResult(edit=result)
            edited = True
            try:
                if result.edit is not None:
                    edited = self.edit_message(chat["id"], int(message["message_id"]), result.edit.text, result.edit.reply_markup)
                for reply in result.messages or []:
                    if not self.send_message(chat["id"], reply.text, reply.reply_markup):
                        edited = False
                        break
            except Exception:
                # Stale inline keyboards and transient Bot API errors must not wedge polling.
                edited = False
            if callback_id:
                try:
                    self.answer_callback(callback_id, result.notice if edited else (result.notice or "Действие больше недоступно."))
                except Exception:
                    pass
            return True

        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        document = message.get("document") or {}
        user_id = int(user.get("id", 0))
        if document and self._document_handler and user_id in self.allowed_user_ids:
            response = self._document_handler(user_id, document, chat)
            if response:
                self._send_replies(chat["id"], response)
            return True
        original_text = str(message.get("text") or message.get("caption") or "")
        text = original_text.strip()
        if not chat:
            return True
        is_channel_post = "channel_post" in update
        command, _, args = text.partition(" ")
        handler = self._handlers.get(command.lstrip("/").split("@", 1)[0])
        menu_handler = self._menu_handlers.get(text)
        # A missing owner allowlist is a locked control plane, never open access.
        if handler and user_id in self.allowed_user_ids:
            self._send_replies(chat["id"], handler(user_id, args.strip()))
            return True
        if menu_handler and user_id in self.allowed_user_ids:
            self._send_replies(chat["id"], menu_handler(user_id, ""))
            return True
        # Channel posts and ordinary group messages can both be sources. The
        # application applies the configured watched-chat allowlist.
        if self._post_handler and text and (is_channel_post or not text.startswith("/")):
            result = self._post_handler({
                "chat": chat, "message": message, "text": original_text,
                "update_id": update.get("update_id"),
            })
            return result is not False
        return True

    def _send_replies(self, chat_id: int | str, response: str | TelegramReply | list[TelegramReply]) -> None:
        """Send a command result that may be a status message followed by a page."""
        if isinstance(response, str):
            self.send_message(chat_id, response)
            return
        replies = response if isinstance(response, list) else [response]
        for reply in replies:
            self.send_message(chat_id, reply.text, reply.reply_markup)

    def _load_checkpoint(self) -> int:
        """Load a single bot-wide offset; sources never own independent offsets."""
        if not self._checkpoint_load:
            return 0
        try:
            return max(0, int(self._checkpoint_load() or 0))
        except (TypeError, ValueError):
            return 0

    def _call(self, method: str, values: dict) -> dict | None:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        payload = parse.urlencode(values).encode("utf-8")
        with request.urlopen(request.Request(url, data=payload), timeout=35) as response:
            return json.loads(response.read().decode("utf-8"))
