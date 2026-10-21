from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from linear_receiptd.config import Settings
from linear_receiptd.services import install, launch_agent, service_files, unit_text


class ServiceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux" and shutil.which("systemd-analyze"), "systemd validation is unavailable")
    def test_generated_services_pass_systemd_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for action in ("serve", "tunnel"):
                path = Path(directory) / f"test-receipts-{action}.service"
                path.write_text(unit_text(Path(directory) / "config.json", action))
                paths.append(str(path))
            result = subprocess.run(["systemd-analyze", "--user", "verify", *paths], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_macos_launch_agents_have_absolute_venv_python_and_passive_drain(self):
        settings = Settings(user_id="user", database=Path("/tmp/test-data/queue.sqlite3"))
        with patch("linear_receiptd.services.sys.executable", "/tmp/my venv/bin/python"):
            agent = plistlib.loads(launch_agent(Path("/tmp/config with spaces.json"), settings))
        self.assertEqual(agent["ProgramArguments"], ["/tmp/my venv/bin/python", "-m", "linear_receiptd", "--config", "/tmp/config with spaces.json", "serve"])
        self.assertEqual(agent["ExitTimeOut"], 0)
        self.assertEqual(agent["Umask"], 0o077)
        self.assertTrue(agent["RunAtLoad"])
        self.assertNotIn("WorkingDirectory", agent)

    def test_external_tunnel_generates_only_receiver_on_both_platforms(self):
        settings = Settings(user_id="user", tunnel_provider="external")
        self.assertEqual(list(service_files(settings, "/tmp/config.json", "linux")), ["linear-receiptd.service"])
        self.assertEqual(list(service_files(settings, "/tmp/config.json", "darwin")), ["io.linear-receiptd.plist"])

    def test_install_does_not_replace_the_current_installation_or_start_services(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            destination = home / ".config/systemd/user"
            destination.mkdir(parents=True)
            target = destination / "linear-receiptd.service"
            original = "[Service]\nExecStart=/old/receipts serve\n"
            target.write_text(original)
            settings = Mock(public_url="https://example.test", tunnel_provider="external")
            settings.printer_config.is_file.return_value = True
            with patch("linear_receiptd.services.sys.platform", "linux"), \
                 patch("linear_receiptd.services.Path.home", return_value=home), \
                 patch("linear_receiptd.services.subprocess.run") as commands:
                with self.assertRaisesRegex(ValueError, "Existing service differs"):
                    install(settings, home / "config.json")
            commands.assert_not_called()
            self.assertEqual(target.read_text(), original)
            self.assertFalse((destination / "linear-receiptd-ngrok.service").exists())

    def test_systemd_escapes_spaces_percent_and_environment_expansion(self):
        text = unit_text(Path('/tmp/a b%$name/config.json'))
        self.assertIn('"/tmp/a b%%$$name/config.json"', text)
        self.assertIn("TimeoutStopSec=infinity", text)
        self.assertNotIn("WorkingDirectory=", text)

    def test_ngrok_generates_both_launch_agents_without_embedded_credentials(self):
        files = service_files(Settings(user_id="user"), "/tmp/config.json", "darwin")
        self.assertEqual(len(files), 2)
        tunnel = plistlib.loads(files["io.linear-receiptd.ngrok.plist"])
        self.assertEqual(tunnel["ProgramArguments"][-1], "tunnel")
        self.assertNotIn("NGROK_AUTHTOKEN", tunnel["EnvironmentVariables"])
