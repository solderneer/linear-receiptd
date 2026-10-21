import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
from aiohttp.test_utils import TestServer

from linear_receiptd.config import Settings, write_private
from linear_receiptd.diagnostics import check_endpoint
from linear_receiptd.queue import Queue
from linear_receiptd.receipt import font
from linear_receiptd.setup import printer_config, tunnel
from linear_receiptd.webhook import create_app


class PortabilityTests(unittest.TestCase):
    def test_custom_config_paths_and_legacy_origin_are_portable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.json"
            write_private(path, {"user_id": "any-user", "ngrok_url": "https://example.test/", "database": "state/queue.sqlite3"})
            settings = Settings.load(path)
            self.assertEqual(settings.user_id, "any-user")
            self.assertEqual(settings.public_url, "https://example.test")
            self.assertEqual(settings.database, root / "state/queue.sqlite3")
            self.assertEqual(settings.secrets_file, root / "secrets.json")
            self.assertEqual(settings.printer_config, root / "printer.json")

    def test_invalid_transfer_and_endpoint_settings_are_rejected(self):
        cases = [{"port": True}, {"drain_seconds": float("nan")}, {"transfer_chunk_size": 0},
                 {"transfer_delay_ms": float("inf")}, {"transfer_write_response": 1},
                 {"public_url": "http://example.test"}, {"public_url": "https://example.test/admin"},
                 {"public_url": "https://example.test:99999"}, {"tunnel_provider": "invalid"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for values in cases:
                with self.subTest(values=values):
                    write_private(path, {"user_id": "user", **values})
                    with self.assertRaises(ValueError):
                        Settings.load(path)

    def test_bundled_catalog_config_accepts_linux_address_and_macos_uuid(self):
        from timiniprint.devices import PrinterCatalog
        for address in ("AA:BB:CC:DD:EE:FF", "243E23AE-4A99-406C-B317-18F1BD7B4CBE"):
            device = PrinterCatalog.load().device_from_printer_config(printer_config(address))
            self.assertEqual(device.protocol_family.value, "instaprint_core")
            self.assertEqual(device.transport_target.ble_endpoint.address, address)
            self.assertIsNone(device.transport_target.classic_endpoint)

    def test_fonts_are_available_without_system_font_discovery(self):
        for kwargs in ({}, {"bold": True}, {"mono": True}):
            face = font(18, **kwargs)
            self.assertGreater(face.getlength("Receipt test"), 0)

    def test_ngrok_uses_saved_executable_and_protected_config_not_cli_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "ngrok with spaces"
            executable.touch()
            settings = Settings(user_id="user", public_url="https://example.test", ngrok_path=str(executable),
                                database=root / "queue.sqlite3", secrets_file=root / "secrets.json")
            write_private(settings.secrets_file, {"ngrok_authtoken": "fake-test-token"})
            with patch("linear_receiptd.setup.subprocess.call", return_value=0) as call:
                self.assertEqual(tunnel(settings), 0)
            args = call.call_args.args[0]
            self.assertEqual(args[0], str(executable))
            self.assertNotIn("fake-test-token", " ".join(args))
            self.assertEqual((root / "ngrok-agent.json").stat().st_mode & 0o777, 0o600)
            self.assertFalse(json.loads((root / "ngrok-agent.json").read_text())["agent"]["web_addr"])


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_public_diagnostic_cannot_create_a_print_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = Queue(root / "queue.sqlite3")
            settings = Settings(user_id="user", secrets_file=root / "secrets.json")
            write_private(settings.secrets_file, {"linear_webhook_secret": "fake-test-secret"})
            server = TestServer(create_app(queue, "user", "fake-test-secret"))
            await server.start_server()
            try:
                settings.public_url = str(server.make_url("/")).rstrip("/")
                async with aiohttp.ClientSession() as session:
                    await check_endpoint(session, settings)
                self.assertEqual(queue.rows(), [])
                self.assertTrue(queue.paused())
            finally:
                await server.close()


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_discovers_a_new_user_and_saves_only_local_configuration(self):
        from linear_receiptd.setup import configure
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {"id": "different-user", "name": "Example User", "displayName": "Example"}
            linear = Mock(viewer=AsyncMock(return_value=identity))
            answers = ["external", "https://example.test", "EXAMPLE COMPANY", "AA:BB:CC:DD:EE:FF", "My Printer"]
            with patch("linear_receiptd.setup.Linear", return_value=linear), \
                 patch("linear_receiptd.setup.getpass.getpass", side_effect=["fake-key", "fake-signing-secret"]), \
                 patch("builtins.input", side_effect=answers), patch("builtins.print"):
                await configure(root / "config.json")
            settings = Settings.load(root / "config.json")
            self.assertEqual(settings.user_id, "different-user")
            self.assertEqual(settings.assignee_name, "Example")
            self.assertEqual(settings.receipt_header, "EXAMPLE COMPANY")
            self.assertEqual(settings.secrets_file, root / "secrets.json")
            self.assertEqual(settings.printer_config, root / "printer.json")
            self.assertNotIn("fake-key", (root / "config.json").read_text())
            self.assertEqual(settings.secrets("linear_api_key")["linear_api_key"], "fake-key")
            self.assertFalse((root / ".config-candidate.json").exists())
