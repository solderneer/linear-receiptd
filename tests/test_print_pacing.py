import unittest
import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from linear_receiptd.sender import send_job_slowly, wait_for_disconnect
from linear_receiptd.__main__ import print_image


class PrintPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_writes_respect_configured_limit_and_preserve_job(self):
        payload = bytes.fromhex("1b401d49f00f1d763000") + bytes(range(100)) + b"\n\n\n\n"
        for chunk_size in (8, 20):
            with self.subTest(chunk_size=chunk_size):
                connection = AsyncMock()
                with patch("linear_receiptd.sender.asyncio.sleep", new_callable=AsyncMock) as sleep, patch("builtins.print"):
                    await send_job_slowly(connection, payload, chunk_size=chunk_size, delay_ms=30, hold_open_seconds=15)
                chunks = [call.args[0] for call in connection.send_standard_payload.await_args_list]
                self.assertEqual(b"".join(chunks), payload)
                self.assertTrue(all(len(chunk) <= chunk_size for chunk in chunks))
                self.assertEqual(chunks[0], payload[:6])
                self.assertGreaterEqual(sum(call.args == (0.03,) for call in sleep.await_args_list), len(chunks))

    async def test_unrecognized_job_is_rejected_before_any_write(self):
        connection = AsyncMock()
        with self.assertRaises(ValueError):
            await send_job_slowly(connection, b"other protocol", chunk_size=20, delay_ms=30)
        connection.send_standard_payload.assert_not_awaited()

    async def test_connection_closes_only_after_final_passive_drain(self):
        payload = bytes.fromhex("1b401d49f00f1d763000") + bytes(range(40)) + b"\n\n\n\n"
        events = []
        connection = AsyncMock()

        async def sent(data):
            events.append(("write", bytes(data)))

        async def waited(seconds):
            events.append(("wait", seconds))

        async def disconnected():
            events.append(("disconnect", None))

        connection.send_standard_payload.side_effect = sent
        connection.disconnect.side_effect = disconnected
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(database=Path(directory) / "queue.sqlite3")
            async def send(payload):
                await send_job_slowly(connection, payload, chunk_size=20, delay_ms=30, hold_open_seconds=15)
            printer = SimpleNamespace(prepare=lambda path: payload, connect=AsyncMock(),
                                      send=send, close=disconnected)
            with patch("linear_receiptd.worker.Printer", return_value=printer), \
                 patch("linear_receiptd.sender.asyncio.sleep", side_effect=waited), \
                 patch("builtins.print"):
                await print_image(settings, "sample.png")
        self.assertEqual(events[-2:], [("wait", 15), ("disconnect", None)])
        self.assertEqual(b"".join(value for event, value in events if event == "write"), payload)

    async def test_default_keeps_link_open_until_owner_requests_disconnect(self):
        payload = bytes.fromhex("1b401d49f00f1d763000") + bytes(range(40))
        connection = AsyncMock()
        owner_waited = False

        async def owner_input():
            nonlocal owner_waited
            self.assertGreater(connection.send_standard_payload.await_count, 0)
            connection.disconnect.assert_not_awaited()
            owner_waited = True

        with patch("linear_receiptd.sender.wait_for_disconnect", side_effect=owner_input), \
             patch("linear_receiptd.sender.asyncio.sleep", new_callable=AsyncMock), \
             patch("builtins.print"):
            await send_job_slowly(connection, payload, chunk_size=20, delay_ms=30)
        self.assertTrue(owner_waited)
        connection.disconnect.assert_not_awaited()

    async def test_disconnect_wait_stays_pending_until_gui_pipe_requests_close(self):
        read_fd, write_fd = os.pipe()
        try:
            task = asyncio.create_task(wait_for_disconnect(read_fd))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            os.write(write_fd, b"\n")
            await asyncio.wait_for(task, timeout=1)
        finally:
            os.close(read_fd)
            os.close(write_fd)
