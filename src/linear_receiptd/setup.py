"""Interactive setup and a foreground ngrok child for service managers."""
from dataclasses import asdict
import getpass
from importlib.resources import files
import json
from pathlib import Path
import shutil
import subprocess
import sys

import aiohttp

from .config import Settings, write_private
from .linear import Linear


def printer_config(address, name="CorePrint receipt printer"):
    if not address.strip():
        raise ValueError("Printer address is required; run linear-receiptd scan")
    config = json.loads(files("linear_receiptd").joinpath("assets/coreprint.json").read_text())
    config["device"] = {"display_name": name, "transport_target": {
        "classic_endpoint": None,
        "ble_endpoint": {"name": name, "address": address.strip(), "paired": None,
                         "transport": "ble", "manufacturer_data": []},
        "display_address": address.strip(), "transport_badge": "[BLE]", "kind": "bluetooth",
    }}
    return config


async def configure(config_path):
    path = Path(config_path).expanduser().resolve()
    current = Settings.load(path) if path.exists() else Settings(
        printer_config=path.parent / "printer.json", secrets_file=path.parent / "secrets.json")
    credentials = current.secrets()
    print("Credentials are stored locally with mode 0600. Enter keeps a saved value.")
    key = getpass.getpass("Linear API key (Read permission)" + (" [saved]" if credentials.get("linear_api_key") else "") + ": ").strip()
    if key:
        credentials["linear_api_key"] = key
    if not credentials.get("linear_api_key"):
        raise ValueError("A Linear API key is required")
    async with aiohttp.ClientSession() as session:
        viewer = await Linear(session, credentials["linear_api_key"]).viewer()
    current.user_id = viewer["id"]
    current.assignee_name = viewer.get("displayName") or viewer.get("name") or ""
    print(f"Linear user: {current.assignee_name}")
    provider = input(f"Tunnel provider: ngrok or external [{current.tunnel_provider}]: ").strip()
    if provider:
        current.tunnel_provider = provider
    if current.tunnel_provider not in ("ngrok", "external"):
        raise ValueError("Choose ngrok or external")
    if current.tunnel_provider == "ngrok":
        value = getpass.getpass("ngrok authtoken" + (" [saved]" if credentials.get("ngrok_authtoken") else "") + ": ").strip()
        if value:
            credentials["ngrok_authtoken"] = value
        executable = current.ngrok_path or shutil.which("ngrok") or ""
        value = input(f"ngrok executable path [{executable}]: ").strip()
        current.ngrok_path = str(Path(value or executable).expanduser().resolve()) if value or executable else ""
        if not current.ngrok_path:
            raise ValueError("Install ngrok first, or choose external")
    origin = input(f"Public HTTPS origin [{current.public_url}]: ").strip()
    if origin:
        current.public_url = origin if "://" in origin else "https://" + origin
    header = input(f"Receipt heading [{current.receipt_header}]: ").strip()
    if header:
        current.receipt_header = header
    if not current.printer_config.exists():
        print("Run linear-receiptd scan to find the printer. macOS uses the UUID from this Mac's scan.")
        address = input("Printer BLE address or macOS UUID: ").strip()
        name = input("Printer name [Stone Printer]: ").strip() or "Stone Printer"
        write_private(current.printer_config, printer_config(address, name))
    values = {key: str(value) if isinstance(value, Path) else value for key, value in asdict(current).items()}
    # Validate a candidate without replacing a working configuration on error.
    candidate = path.with_name(".config-candidate.json")
    try:
        write_private(candidate, values)
        Settings.load(candidate)
    finally:
        candidate.unlink(missing_ok=True)
    write_private(path, values)
    write_private(current.secrets_file, credentials)
    print(f"\nLinear webhook URL: {current.public_url}/webhooks/linear")
    print("In Linear workspace Settings → API → Webhooks, create an Issues webhook, then copy its signing secret below.")
    value = getpass.getpass("Linear webhook signing secret" + (" [saved]" if credentials.get("linear_webhook_secret") else "") + ": ").strip()
    if value:
        credentials["linear_webhook_secret"] = value
    write_private(current.secrets_file, credentials)
    print(f"Saved {path}. Run linear-receiptd doctor, then linear-receiptd install.")
    print("A new queue starts paused. linear-receiptd resume enables printing.")


def ngrok_executable(settings):
    executable = settings.ngrok_path or shutil.which("ngrok")
    if not executable or not Path(executable).is_file():
        raise ValueError("ngrok is missing; install it and save ngrok_path with setup")
    return str(Path(executable).expanduser().resolve())


def tunnel(settings):
    if settings.tunnel_provider != "ngrok":
        raise ValueError("The external tunnel is managed outside linear-receiptd")
    credentials = settings.secrets("ngrok_authtoken")
    if not settings.public_url:
        raise ValueError("Set public_url with linear-receiptd setup")
    executable = ngrok_executable(settings)
    config = settings.database.parent / "ngrok-agent.json"
    write_private(config, {"version": "3", "agent": {
        "authtoken": credentials["ngrok_authtoken"], "web_addr": False, "update_check": False,
    }})
    return subprocess.call([executable, "http", f"http://127.0.0.1:{settings.port}",
                            "--url", settings.public_url, "--config", str(config),
                            "--inspect=false", "--log=stdout", "--log-format=json"])
