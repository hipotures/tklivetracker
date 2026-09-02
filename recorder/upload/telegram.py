from pathlib import Path
import os

from ..utils.logger_manager import logger # Relative import (up one level)
from ..utils.utils import read_telegram_config # Relative import (up one level)
from ..utils.security import redact_secrets


FREE_USER_MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
PREMIUM_USER_MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024


def _load_pyrogram():
    try:
        from pyrogram import Client
        from pyrogram.enums import ParseMode
    except ImportError as error:
        raise RuntimeError(
            "Telegram upload requires: uv sync --extra telegram-upload"
        ) from error
    return Client, ParseMode


class Telegram:

    def __init__(self, config_path):
        client_class, self.parse_mode = _load_pyrogram()
        config = read_telegram_config(config_path)

        self.api_id = config["api_id"]
        self.api_hash = config["api_hash"]
        self.bot_token = config["bot_token"]
        self.chat_id = config["chat_id"]

        private_dir = Path(
            config.get("session_path")
            or Path(config_path).absolute().parent / "private"
        )
        private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            private_dir.chmod(0o700)
        self.private_dir = private_dir

        self.app = client_class(
            'telegram_session',
            workdir=str(private_dir),
            api_id=self.api_id,
            api_hash=self.api_hash,
            bot_token=self.bot_token
        )

    def _protect_session_files(self) -> None:
        if os.name != "posix":
            return
        for session_file in self.private_dir.glob("telegram_session*"):
            if session_file.is_file():
                session_file.chmod(0o600)

    def upload(self, file_path: str):
        """
        Upload a file to the bot's own chat (saved messages).
        """
        try:
            self.app.start()
            self._protect_session_files()

            me = self.app.get_me()
            is_premium = me.is_premium
            max_size = (
                PREMIUM_USER_MAX_FILE_SIZE
                if is_premium else FREE_USER_MAX_FILE_SIZE
            )

            file_size = Path(file_path).stat().st_size
            logger.info(f"File to upload: {Path(file_path).name} "
                        f"({round(file_size / (1024 * 1024))} MB)")

            if file_size > max_size:
                logger.warning("The file is too large to be "
                               "uploaded with this type of account.")
                return

            logger.info("Uploading video on Telegram... This may take a while depending on the file size.")
            self.app.send_document(
                chat_id=self.chat_id,
                document=file_path,
                caption='🎥 <b>Video recorded by TkLiveTracker</b>',
                parse_mode=self.parse_mode.HTML,
                force_document=True,
            )
            logger.info("File successfully uploaded to Telegram.\n")

        except Exception as e:
            logger.error(
                "Error during Telegram upload: %s",
                redact_secrets(e, self.bot_token, self.api_hash),
            )

        finally:
            self.app.stop()
            self._protect_session_files()
