"""Generate user services without a shell, repository paths, or embedded secrets."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = "io.linear-receiptd"
MARKER = "# Managed by linear-receiptd; review before editing."


def command(config_path, action):
    # Do not resolve the venv Python symlink into the base interpreter.
    return [os.path.abspath(sys.executable), "-m", "linear_receiptd", "--config",
            str(Path(config_path).expanduser().resolve()), action]


def systemd_quote(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def unit_text(config_path, action="serve"):
    receiver = action == "serve"
    if action not in ("serve", "tunnel"):
        raise ValueError("Unknown service command")
    description = "Linear receipt printer" if receiver else "ngrok tunnel for Linear receipts"
    return f"""{MARKER}
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={' '.join(systemd_quote(arg) for arg in command(config_path, action))}
Environment=PYTHONUNBUFFERED=1
Environment=TIMINIPRINT_NO_UPDATE_CHECK=1
UMask=0077
NoNewPrivileges=true
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec={'infinity' if receiver else '30'}

[Install]
WantedBy=default.target
"""


def launch_agent(config_path, settings, action="serve"):
    label = LABEL if action == "serve" else LABEL + ".ngrok"
    logs = settings.database.parent / "logs"
    return plistlib.dumps({
        "Label": label, "ProgramArguments": command(config_path, action),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "ExitTimeOut": 0 if action == "serve" else 30, "Umask": 0o077,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "TIMINIPRINT_NO_UPDATE_CHECK": "1"},
        "StandardOutPath": str(logs / f"{label}.log"),
        "StandardErrorPath": str(logs / f"{label}.log"),
    }, sort_keys=False)


def service_files(settings, config_path, platform=None):
    platform = sys.platform if platform is None else platform
    actions = ["serve", "tunnel"] if settings.tunnel_provider == "ngrok" else ["serve"]
    if platform == "linux":
        return {("linear-receiptd" if action == "serve" else "linear-receiptd-ngrok") + ".service":
                unit_text(config_path, action).encode() for action in actions}
    if platform == "darwin":
        return {(LABEL if action == "serve" else LABEL + ".ngrok") + ".plist":
                launch_agent(config_path, settings, action) for action in actions}
    raise ValueError("Automatic service installation supports systemd Linux and macOS; run serve and tunnel in your supervisor elsewhere")


def prepare_services(settings, config_path, output, platform=None):
    contents = service_files(settings, config_path, platform)
    directory = Path(output).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        (directory / name).write_bytes(content)
    return directory


def install(settings, config_path):
    settings.secrets("linear_api_key", "linear_webhook_secret")
    if not settings.public_url:
        raise ValueError("Set a stable public_url with setup first")
    if settings.tunnel_provider == "ngrok":
        from .setup import ngrok_executable
        settings.secrets("ngrok_authtoken")
        ngrok_executable(settings)
    if not settings.printer_config.is_file():
        raise ValueError("Printer configuration is missing; run setup")
    contents = service_files(settings, config_path)
    destination = (Path.home() / ".config/systemd/user" if sys.platform == "linux"
                   else Path.home() / "Library/LaunchAgents")
    destination.mkdir(parents=True, exist_ok=True)
    # Refuse to replace the original installation or a different checkout.
    # Preflight every destination before writing any file or starting services.
    for name, content in contents.items():
        target = destination / name
        if target.exists() and target.read_bytes() != content:
            raise ValueError(f"Existing service differs: {target}. The running installation was left untouched.")
    if sys.platform == "linux":
        if not shutil.which("systemctl"):
            raise ValueError("systemd is unavailable; run serve and tunnel with your supervisor")
        # Validate in isolation; do not overwrite the real files on failure.
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for name, content in contents.items():
                path = Path(temporary) / name
                path.write_bytes(content)
                paths.append(str(path))
            subprocess.run(["systemd-analyze", "--user", "verify", *paths], check=True)
    else:
        (settings.database.parent / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, content in contents.items():
        target = destination / name
        target.write_bytes(content)
        target.chmod(0o600)
    if sys.platform == "linux":
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", *contents], check=True)
    else:
        domain = f"gui/{os.getuid()}"
        for name in contents:
            label = name.removesuffix(".plist")
            loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True)
            if loaded.returncode:
                subprocess.run(["launchctl", "bootstrap", domain, str(destination / name)], check=True)
    print("User services installed. Printing follows the saved pause state.")
