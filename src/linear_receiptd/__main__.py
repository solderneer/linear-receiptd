import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import aiohttp

from .config import DEFAULT_CONFIG, Settings
from .linear import Linear, LinearError
from .queue import Queue
from .receipt import render_receipt


async def fetch_issue(settings, identifier):
    async with aiohttp.ClientSession() as session:
        issue = await Linear(session, settings.secrets("linear_api_key")["linear_api_key"]).issue(identifier)
    if issue is None:
        raise ValueError("Issue was not found")
    return issue


async def doctor(settings, *, endpoint=False):
    credentials = settings.secrets("linear_api_key", "linear_webhook_secret")
    if not settings.public_url:
        raise ValueError("Public URL is missing; run linear-receiptd setup")
    if settings.tunnel_provider == "ngrok":
        from .setup import ngrok_executable
        ngrok_executable(settings)
        settings.secrets("ngrok_authtoken")
    from .worker import Printer
    Printer(settings)  # Validate catalog, profile, and transport configuration without connecting.
    async with aiohttp.ClientSession() as session:
        viewer = await Linear(session, credentials["linear_api_key"]).viewer()
        if viewer["id"] != settings.user_id:
            raise ValueError("Linear key belongs to a different user")
        print(f"Linear identity verified: {settings.assignee_name}")
        if endpoint:
            from .diagnostics import check_endpoint
            await check_endpoint(session, settings)
            print("Public HTTPS health, signature rejection, and signed no-op verified.")
    print("Printer configuration is valid; no BLE connection or print commands sent.")
    print(f"Webhook URL: {settings.public_url}/webhooks/linear")


async def print_image(settings, path):
    from .worker import Printer
    settings.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with settings.database.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("The daemon owns this printer. Pause and stop it gracefully before a manual print.")
        printer = Printer(settings)
        payload = await asyncio.to_thread(printer.prepare, path)
        try:
            await printer.connect()
            await printer.send(payload)
        finally:
            await printer.close()
    print("Bytes transferred and drain finished; inspect the paper before retrying.")


def main():
    os.environ.setdefault("TIMINIPRINT_NO_UPDATE_CHECK", "1")
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="linear-receiptd")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Render a receipt without printing")
    source = preview.add_mutually_exclusive_group(required=True)
    source.add_argument("--issue-json", type=Path)
    source.add_argument("--issue", help="Fetch a Linear issue using your local API key")
    preview.add_argument("--output", type=Path, required=True)
    preview.add_argument("--assignee")
    preview.add_argument("--header")
    commands.add_parser("setup", help="Configure your Linear identity, BLE printer, and tunnel")
    scan = commands.add_parser("scan", help="List BLE names and this machine's addresses/UUIDs; no printing")
    scan.add_argument("--seconds", type=float, default=10)
    doctor_parser = commands.add_parser("doctor", help="Validate configuration and Linear identity; no printing")
    doctor_parser.add_argument("--check-endpoint", action="store_true", help="Also test the running public HTTPS endpoint with a signed no-op")
    commands.add_parser("serve", help="Run the signed receiver and BLE queue worker in the foreground")
    commands.add_parser("tunnel", help="Run ngrok using your configured executable and stable domain")
    prepare = commands.add_parser("prepare-services", help="Generate systemd units or LaunchAgents for inspection")
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--platform", choices=("linux", "darwin"))
    commands.add_parser("install", help="Install user services; refuse to overwrite another installation")
    commands.add_parser("status", help="Show the durable queue")
    commands.add_parser("pause", help="Pause after the active receipt finishes")
    commands.add_parser("resume", help="Enable printing queued and future assignments")
    manual = commands.add_parser("print-image", help="Explicitly print an image or PDF using saved BLE settings")
    manual.add_argument("path", type=Path)
    reprint = commands.add_parser("reprint", help="Explicitly queue a fresh receipt after inspecting the paper")
    target = reprint.add_mutually_exclusive_group(required=True)
    target.add_argument("--issue")
    target.add_argument("--job", type=int)
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    if args.command == "setup":
        from .setup import configure
        asyncio.run(configure(args.config))
        return
    if args.command == "scan":
        from .diagnostics import scan
        if not 0 < args.seconds <= 120:
            raise ValueError("Scan duration must be greater than zero and at most 120 seconds")
        asyncio.run(scan(args.seconds))
        return
    if args.command == "preview":
        settings = Settings.load(args.config) if args.config.exists() else None
        if args.issue_json:
            issue = json.loads(args.issue_json.read_text())
        else:
            if settings is None:
                raise ValueError("Run linear-receiptd setup first")
            issue = asyncio.run(fetch_issue(settings, args.issue))
        result = render_receipt(issue, args.output,
                                assignee=args.assignee or (settings.assignee_name if settings else ""),
                                header=args.header or (settings.receipt_header if settings else "OPAL ELECTRONICS"))
        print(f"Preview saved: {args.output} ({result.width}×{result.height}); no print sent.")
        return
    settings = Settings.load(args.config)
    if args.command == "serve":
        from .service import serve
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        asyncio.run(serve(settings))
    elif args.command == "doctor":
        asyncio.run(doctor(settings, endpoint=args.check_endpoint))
    elif args.command == "tunnel":
        from .setup import tunnel
        raise SystemExit(tunnel(settings))
    elif args.command == "prepare-services":
        from .services import prepare_services
        print(prepare_services(settings, args.config, args.output, args.platform))
    elif args.command == "install":
        from .services import install
        install(settings, args.config)
    elif args.command == "print-image":
        asyncio.run(print_image(settings, args.path))
    else:
        queue = Queue(settings.database)
        if args.command == "status":
            print("Printing paused" if queue.paused() else "Printing enabled")
            print(f"Transfer mode: {settings.transfer_mode}")
            if settings.transfer_mode == "chunked":
                print(f"BLE writes: <= {settings.transfer_chunk_size} bytes, {settings.transfer_delay_ms:g} ms pause")
                print("BLE acknowledgment: " + ("required for each write" if settings.transfer_write_response else "disabled"))
            for job in queue.rows():
                identifier = (json.loads(job["snapshot"]).get("identifier") if job["snapshot"] else None) or job["issue_id"]
                print(f"{job['id']:>4}  {job['status']:<10}  {identifier}" + (f"  ({job['error']})" if job['error'] else ""))
            print("'sent' means bytes transferred and drain elapsed; inspect physical output when uncertain.")
        elif args.command in ("pause", "resume"):
            queue.pause(args.command == "pause")
            print("Paused; the active receipt will finish." if args.command == "pause" else "Printing enabled for queued and future assignments.")
        elif args.command == "reprint":
            if args.job is not None:
                job = queue.get(args.job)
                if job["status"] in ("queued", "preparing", "sending"):
                    raise ValueError("That job is still active; pause and inspect before requesting a reprint")
                identifier = job["issue_id"]
            else:
                identifier = args.issue
            print(f"Queued explicit reprint {queue.enqueue(identifier)}; queue is {'paused' if queue.paused() else 'active'}.")


def entrypoint():
    try:
        main()
    except (OSError, ValueError, LinearError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    entrypoint()
