"""Local configuration; credentials never appear in command arguments or logs."""
from dataclasses import dataclass
import json
import math
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from platformdirs import user_config_dir, user_data_dir

CONFIG_DIR = Path(user_config_dir("linear-receiptd", appauthor=False))
DATA_DIR = Path(user_data_dir("linear-receiptd", appauthor=False))
DEFAULT_CONFIG = CONFIG_DIR / "config.json"


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)



@dataclass
class Settings:
    user_id: str = ""
    assignee_name: str = ""
    receipt_header: str = "OPAL ELECTRONICS"
    port: int = 8765
    drain_seconds: float = 30
    transfer_mode: str = "chunked"
    transfer_chunk_size: int = 180
    transfer_delay_ms: float = 10
    transfer_write_response: bool = True
    database: Path = DATA_DIR / "queue.sqlite3"
    receipt_dir: Path = DATA_DIR / "receipts"
    printer_config: Path = CONFIG_DIR / "printer.json"
    secrets_file: Path = CONFIG_DIR / "secrets.json"
    public_url: str = ""
    tunnel_provider: str = "ngrok"
    ngrok_path: str = ""

    @classmethod
    def load(cls, path=DEFAULT_CONFIG):
        source = Path(path).resolve()
        values = json.loads(source.read_text())
        if not isinstance(values, dict):
            raise ValueError("Configuration must be a JSON object")
        # Accept the original local deployment when migrating configuration.
        legacy_url = values.pop("ngrok_url", "")
        values.setdefault("public_url", legacy_url)
        values.setdefault("secrets_file", str(source.parent / "secrets.json"))
        values.setdefault("printer_config", str(source.parent / "printer.json"))
        for key in ("database", "receipt_dir", "printer_config", "secrets_file"):
            if key in values:
                candidate = Path(values[key]).expanduser()
                values[key] = candidate if candidate.is_absolute() else source.parent / candidate
        try:
            settings = cls(**values)
        except TypeError as error:
            raise ValueError("Unknown configuration setting") from error
        if (not isinstance(settings.user_id, str) or not settings.user_id.strip()
                or isinstance(settings.port, bool) or not isinstance(settings.port, int)
                or not 1024 <= settings.port <= 65535
                or isinstance(settings.drain_seconds, bool)
                or not isinstance(settings.drain_seconds, (int, float))
                or not math.isfinite(settings.drain_seconds) or settings.drain_seconds < 0):
            raise ValueError("Invalid user, port, or drain interval")
        if settings.transfer_mode not in ("paced", "chunked", "unpaced"):
            raise ValueError("transfer_mode must be paced, chunked, or unpaced")
        if (isinstance(settings.transfer_chunk_size, bool)
                or not isinstance(settings.transfer_chunk_size, int)
                or not 1 <= settings.transfer_chunk_size <= 512):
            raise ValueError("transfer_chunk_size must be an integer from 1 to 512")
        if (isinstance(settings.transfer_delay_ms, bool)
                or not isinstance(settings.transfer_delay_ms, (int, float))
                or not math.isfinite(settings.transfer_delay_ms)
                or not 0 <= settings.transfer_delay_ms <= 1000):
            raise ValueError("transfer_delay_ms must be from 0 to 1000")
        if not isinstance(settings.transfer_write_response, bool):
            raise ValueError("transfer_write_response must be a boolean")
        if settings.tunnel_provider not in ("ngrok", "external"):
            raise ValueError("tunnel_provider must be ngrok or external")
        if not isinstance(settings.receipt_header, str) or not settings.receipt_header.strip():
            raise ValueError("receipt_header must be nonempty text")
        if settings.public_url:
            url = urlsplit(settings.public_url)
            if (url.scheme != "https" or not url.hostname or url.path not in ("", "/")
                    or url.query or url.fragment or url.username or url.password):
                raise ValueError("public_url must be an HTTPS origin, without a path")
            try:
                if url.port is not None and not 1 <= url.port <= 65535:
                    raise ValueError
            except ValueError as error:
                raise ValueError("Invalid public_url port") from error
            if not url.netloc:
                raise ValueError("public_url must be an HTTPS origin, without a path")
            settings.public_url = settings.public_url.rstrip("/")
        return settings

    def secrets(self, *required):
        values = json.loads(self.secrets_file.read_text()) if self.secrets_file.exists() else {}
        for key in required:
            if not isinstance(values.get(key), str) or not values[key].strip():
                raise ValueError(f"Missing {key}; run linear-receiptd setup in a terminal")
        return values
