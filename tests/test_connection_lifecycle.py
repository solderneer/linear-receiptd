import asyncio
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from linear_receiptd.config import Settings
from linear_receiptd.queue import Queue
from linear_receiptd.worker import Printer, run_worker
from timiniprint.transport.bluetooth.backend import SppBackend
from timiniprint.transport.bluetooth.adapters.bleak_adapter import _BleakSocket
from timiniprint.transport.bluetooth.adapters.linux_att import _LinuxAttClient


class PrinterReconnectTests(unittest.IsolatedAsyncioTestCase):
    def printer(self, old):
        printer = Printer.__new__(Printer)
        printer.connection = old
        printer.device = object()
        printer.connector = SimpleNamespace(connect=AsyncMock(return_value=AsyncMock()))
        return printer

    async def test_dead_saved_connection_is_replaced_before_payload_writes(self):
        old = SimpleNamespace(check_connection=AsyncMock(return_value=False), disconnect=AsyncMock())
        printer = self.printer(old)
        await printer.connect()
        old.disconnect.assert_awaited_once()
        printer.connector.connect.assert_awaited_once_with(printer.device)
        self.assertIsNot(printer.connection, old)
        printer.connection.send_standard_payload.assert_not_awaited()

    async def test_healthy_saved_connection_is_reused(self):
        old = SimpleNamespace(check_connection=AsyncMock(return_value=True), disconnect=AsyncMock())
        printer = self.printer(old)
        await printer.connect()
        printer.connector.connect.assert_not_awaited()
        old.disconnect.assert_not_awaited()
        self.assertIs(printer.connection, old)

    async def test_failed_health_check_and_failed_reconnect_clear_saved_connection(self):
        old = SimpleNamespace(check_connection=AsyncMock(side_effect=OSError("link lost")), disconnect=AsyncMock())
        printer = self.printer(old)
        printer.connector.connect.side_effect = ConnectionError("offline")
        with self.assertRaises(ConnectionError):
            await printer.connect()
        self.assertIsNone(printer.connection)
        old.disconnect.assert_awaited_once()

    async def test_worker_connects_while_paused_without_claiming_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            queue = Queue(Path(directory) / "queue.sqlite3")
            job_id = queue.enqueue("issue")
            stop = asyncio.Event()
            async def connected():
                stop.set()
            printer = SimpleNamespace(connect=AsyncMock(side_effect=connected), close=AsyncMock(), send=AsyncMock())
            linear = SimpleNamespace(issue=AsyncMock())
            await run_worker(queue, Settings(user_id="user"), linear, printer, stop)
            self.assertEqual(queue.get(job_id)["status"], "queued")
            printer.connect.assert_awaited_once()
            printer.send.assert_not_awaited()
            linear.issue.assert_not_awaited()
            printer.close.assert_awaited_once()

    async def test_idle_worker_checks_again_and_recovers_after_offline_connection(self):
        for initially_offline in (False, True):
            with self.subTest(initially_offline=initially_offline), tempfile.TemporaryDirectory() as directory:
                queue = Queue(Path(directory) / "queue.sqlite3")
                stop = asyncio.Event()
                attempts = 0
                async def connected():
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1 and initially_offline:
                        raise ConnectionError("printer asleep")
                    if attempts == 2:
                        stop.set()
                printer = SimpleNamespace(connect=AsyncMock(side_effect=connected), close=AsyncMock(), send=AsyncMock())
                # Replace only this module's clock, not asyncio's time source.
                clock = SimpleNamespace(monotonic=Mock(side_effect=[0, 0, 20, 20]))
                with patch("linear_receiptd.worker.time", clock):
                    await asyncio.wait_for(run_worker(queue, Settings(user_id="user"),
                                                     SimpleNamespace(issue=AsyncMock()), printer, stop), 4)
                self.assertEqual(attempts, 2)
                printer.send.assert_not_awaited()
                self.assertTrue(queue.paused())

    async def test_backend_liveness_uses_active_fallback_without_writes(self):
        backend = SppBackend()
        adapter = SimpleNamespace(check_connection=Mock(return_value=False), send=Mock())
        backend._sock = adapter
        backend._connected = True
        self.assertFalse(await backend.check_connection())
        self.assertFalse(backend.is_connected())
        adapter.send.assert_not_called()


class AdapterLivenessTests(unittest.TestCase):
    def test_bleak_processes_idle_disconnect_callback_without_gatt_writes(self):
        adapter = _BleakSocket()
        loop = asyncio.new_event_loop()
        client = SimpleNamespace(is_connected=True, write_gatt_char=AsyncMock())
        adapter._loop, adapter._client, adapter._connected = loop, client, True
        loop.call_soon(setattr, client, "is_connected", False)
        try:
            self.assertFalse(adapter.check_connection())
            client.write_gatt_char.assert_not_awaited()
        finally:
            loop.close()

    def test_direct_att_eof_marks_link_dead_and_wakes_response_waiters(self):
        client = _LinuxAttClient()
        client._sock = object()
        client._rx_thread = threading.current_thread()
        client._running = True
        client._recv_pdu = Mock(return_value=b"")
        self.assertTrue(client.is_connected)
        client._rx_loop()
        self.assertFalse(client.is_connected)
        client._recv_pdu.assert_called_once()
        with self.assertRaises(ConnectionError):
            client._wait_for_response(expected_opcodes={0x13}, request_opcode=0x12,
                                      timeout=10, allow_attr_not_found=False)

    def test_direct_att_read_error_marks_link_dead(self):
        client = _LinuxAttClient()
        client._sock = object()
        client._rx_thread = threading.current_thread()
        client._running = True
        client._recv_pdu = Mock(side_effect=OSError("disconnected"))
        client._rx_loop()
        self.assertFalse(client.is_connected)
