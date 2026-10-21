import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from linear_receiptd.config import Settings, write_private
from linear_receiptd.queue import Queue
from linear_receiptd.receipt import render_receipt
from linear_receiptd.webhook import create_app, verify
from linear_receiptd.worker import Printer, TransferUncertain, process_one, run_worker

USER = "user-shan"
SECRET = "test-signing-secret"


def event(*, action="update", old=None, assignee=USER, changed_at="2026-10-21T17:00:00Z"):
    return {
        "type": "Issue", "action": action, "organizationId": "org", "webhookId": "subscription-id",
        "data": {"id": "issue-uuid", "updatedAt": changed_at, "assigneeId": assignee},
        "updatedFrom": {"assigneeId": old}, "webhookTimestamp": int(time.time() * 1000),
    }


def signed(payload):
    raw = json.dumps(payload).encode()
    return raw, hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.queue = Queue(Path(self.directory.name) / "queue.sqlite3")

    def test_only_real_assignments_and_created_assigned_issues_are_queued(self):
        edited = event()
        edited["updatedFrom"] = {"title": "Old title"}
        self.assertEqual(self.queue.accept("edit", edited, USER), "ignored")
        self.assertEqual(self.queue.accept("other", event(assignee="someone-else"), USER), "ignored")
        self.assertEqual(self.queue.accept("same", event(old=USER), USER), "ignored")
        self.assertEqual(self.queue.accept("unassigned-to-me", event(old=None), USER), "queued")
        created = event(action="create", changed_at="2026-10-21T18:00:00Z")
        created.pop("updatedFrom")
        self.assertEqual(self.queue.accept("create", created, USER), "queued")
        self.assertEqual(len(self.queue.rows()), 2)

    def test_duplicate_delivery_and_same_event_across_subscriptions_print_once(self):
        payload = event()
        self.assertEqual(self.queue.accept("delivery-1", payload, USER), "queued")
        self.assertEqual(self.queue.accept("delivery-1", payload, USER), "duplicate")
        payload["webhookId"] = "another-subscription"
        payload["webhookTimestamp"] += 1000
        self.assertEqual(self.queue.accept("delivery-2", payload, USER), "duplicate")
        self.assertEqual(len(self.queue.rows()), 1)
        # A later reassignment back to this user is a separate receipt.
        self.assertEqual(self.queue.accept("delivery-3", event(old="other", changed_at="2026-10-22T00:00:00Z"), USER), "queued")

    def test_paused_start_and_atomic_claim_across_two_connections(self):
        self.queue.enqueue("issue")
        self.assertIsNone(self.queue.claim())
        self.queue.pause(False)
        second = Queue(self.queue.path)
        job = self.queue.claim()
        self.assertEqual(job["status"], "preparing")
        self.assertIsNone(second.claim())

    def test_crash_before_writes_retries_and_during_writes_requires_review(self):
        self.queue.pause(False)
        first = self.queue.enqueue("one")
        second = self.queue.enqueue("two")
        self.queue.update(first, status="preparing")
        self.queue.update(second, status="sending")
        self.assertEqual(self.queue.recover(), 1)
        self.assertEqual(self.queue.get(first)["status"], "queued")
        self.assertEqual(self.queue.get(second)["status"], "uncertain")
        self.assertTrue(self.queue.paused())
        self.assertIsNone(self.queue.claim())

    def test_backoff_keeps_unsent_jobs_durable(self):
        self.queue.pause(False)
        job_id = self.queue.enqueue("offline")
        job = self.queue.claim()
        self.queue.retry(job, "offline")
        self.assertIsNone(self.queue.claim())
        restored = Queue(self.queue.path).get(job_id)
        self.assertEqual(restored["status"], "queued")
        self.assertGreater(restored["next_attempt"], time.time())


class SignatureTests(unittest.TestCase):
    def test_raw_bytes_are_authenticated_and_expired_requests_rejected(self):
        payload = event()
        raw, signature = signed(payload)
        self.assertEqual(verify(raw, signature, SECRET), payload)
        with self.assertRaises(PermissionError):
            verify(raw + b" ", signature, SECRET)
        with self.assertRaises(PermissionError):
            verify(raw, signature, SECRET, now=time.time() + 61)
        for invalid in (None, "bad", "é" * 64):
            with self.subTest(signature=invalid), self.assertRaises(PermissionError):
                verify(raw, invalid, SECRET)

    def test_signed_invalid_json_or_timestamp_is_rejected(self):
        for payload in ([], {"webhookTimestamp": True}, {"webhookTimestamp": float("nan")}, {"webhookTimestamp": "today"}):
            raw, signature = signed(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                verify(raw, signature, SECRET)


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.queue = Queue(Path(self.directory.name) / "queue.sqlite3")
        self.client = TestClient(TestServer(create_app(self.queue, USER, SECRET)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.directory.cleanup()

    async def test_http_ack_commits_once_and_public_surface_is_minimal(self):
        raw, signature = signed(event())
        headers = {"Linear-Signature": signature, "Linear-Delivery": "delivery-1"}
        for expected in ("queued", "duplicate"):
            response = await self.client.post("/webhooks/linear", data=raw, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["accepted"], expected)
        self.assertEqual(len(self.queue.rows()), 1)
        response = await self.client.post("/webhooks/linear", data=raw)
        self.assertEqual(response.status, 401)
        response = await self.client.get("/health")
        self.assertEqual(await response.json(), {"ok": True})
        for path in ("/status", "/resume", "/work/daemon-secrets.json"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 404)


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = Settings(user_id=USER, database=root / "queue.sqlite3", receipt_dir=root / "receipts")
        self.queue = Queue(self.settings.database)
        self.queue.pause(False)
        self.queue.accept("new", event(), USER)
        self.issue = {"id": "issue-uuid", "identifier": "DAI-1", "title": "Test", "assignee": {"id": USER}}
        self.linear = SimpleNamespace(issue=AsyncMock(return_value=self.issue))
        self.printer = SimpleNamespace(connect=AsyncMock(), prepare=Mock(return_value=b"raster"), send=AsyncMock(), close=AsyncMock())
        self.stop = asyncio.Event()

    async def test_offline_connection_retries_without_writes(self):
        self.printer.connect.side_effect = ConnectionError("offline")
        await process_one(self.queue, self.settings, self.linear, self.printer, self.stop)
        self.printer.send.assert_not_awaited()
        self.assertEqual(self.queue.get(1)["status"], "queued")

    async def test_interrupted_send_is_persisted_uncertain_and_pauses_queue(self):
        async def fail(payload):
            self.assertEqual(self.queue.get(1)["status"], "sending")
            raise ConnectionError("link lost after some bytes")
        self.printer.send.side_effect = fail
        with self.assertRaises(TransferUncertain):
            await process_one(self.queue, self.settings, self.linear, self.printer, self.stop)
        self.assertEqual(self.queue.get(1)["status"], "uncertain")
        self.assertTrue(self.queue.paused())
        self.printer.close.assert_awaited_once()
        self.assertIsNone(self.queue.claim())

    async def test_success_preserves_connection_and_records_snapshot(self):
        await process_one(self.queue, self.settings, self.linear, self.printer, self.stop)
        self.assertEqual(self.queue.get(1)["status"], "sent")
        self.assertEqual(json.loads(self.queue.get(1)["snapshot"])["identifier"], "DAI-1")
        self.printer.close.assert_not_awaited()

    async def test_later_unassignment_skips_receipt(self):
        self.issue["assignee"] = None
        await process_one(self.queue, self.settings, self.linear, self.printer, self.stop)
        self.assertEqual(self.queue.get(1)["status"], "skipped")
        self.printer.connect.assert_not_awaited()
        self.printer.send.assert_not_awaited()

    async def test_pause_during_connect_prevents_first_write(self):
        async def connected():
            self.queue.pause()
        self.printer.connect.side_effect = connected
        await process_one(self.queue, self.settings, self.linear, self.printer, self.stop)
        self.printer.send.assert_not_awaited()
        self.assertEqual(self.queue.get(1)["status"], "queued")

    async def test_shutdown_waits_for_in_flight_send_and_drain(self):
        started, finished = asyncio.Event(), asyncio.Event()
        async def transfer_and_drain(payload):
            started.set()
            await finished.wait()
        self.printer.send.side_effect = transfer_and_drain
        worker = asyncio.create_task(run_worker(self.queue, self.settings, self.linear, self.printer, self.stop))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            self.stop.set()
            await asyncio.sleep(0)
            self.assertFalse(worker.done())
            self.printer.close.assert_not_awaited()
            self.assertEqual(self.queue.get(1)["status"], "sending")
        finally:
            finished.set()
            await asyncio.wait_for(worker, timeout=2)
        self.assertEqual(self.queue.get(1)["status"], "sent")
        self.printer.close.assert_awaited_once()

    async def test_two_receipts_share_one_connection_and_drain_before_shutdown(self):
        printer = Printer.__new__(Printer)
        printer.device = SimpleNamespace(profile=SimpleNamespace(stream=SimpleNamespace(chunk_size=20, delay_ms=30)))
        printer.connection = None
        printer.connector = SimpleNamespace(connect=AsyncMock(return_value=AsyncMock()))
        printer.settings = self.settings
        events = []
        async def send(connection, payload, **pacing):
            self.assertEqual(pacing["hold_open_seconds"], 30)
            events.extend(["send", "drain"])
        with patch("linear_receiptd.sender.send_job_slowly", side_effect=send):
            for _ in range(2):
                await printer.connect()
                await printer.send(b"payload")
            connection = printer.connection
            connection.disconnect.assert_not_awaited()
            connection.disconnect.side_effect = lambda: events.append("disconnect")
            await printer.close()
        printer.connector.connect.assert_awaited_once()
        self.assertEqual(events, ["send", "drain", "send", "drain", "disconnect"])

    async def test_unpaced_sender_submits_full_raster_and_keeps_passive_drain(self):
        printer = Printer.__new__(Printer)
        printer.device = SimpleNamespace(profile=SimpleNamespace(stream=SimpleNamespace(chunk_size=20, delay_ms=30)))
        printer.connection = AsyncMock()
        self.settings.transfer_mode = "unpaced"
        printer.settings = self.settings
        payload = bytes.fromhex("1b401d49f00f1d763000") + bytes(range(256)) * 3 + b"\n\n\n\n"
        with patch("linear_receiptd.sender.asyncio.sleep", new_callable=AsyncMock) as sleep, patch("builtins.print"):
            await printer.send(payload)
        writes = [call.args[0] for call in printer.connection.send_standard_payload.await_args_list]
        self.assertEqual(writes, [payload[:6], payload[6:]])
        self.assertEqual(b"".join(writes), payload)
        self.assertFalse(any(0 < call.args[0] < 0.5 for call in sleep.await_args_list))
        self.assertEqual(sleep.await_args_list[-1].args, (30,))
        printer.connection.disconnect.assert_not_awaited()

    async def test_unpaced_device_keeps_uart_selection_and_transport_target(self):
        from timiniprint.devices import PrinterDevice
        from timiniprint.protocol.family import ProtocolFamily
        from linear_receiptd.device import without_ble_delays
        base = PrinterDevice(display_name="CTP520WH", profile=Mock(),
                             protocol_family=ProtocolFamily.INSTAPRINT_CORE,
                             protocol_variant="ctp500", image_pipeline=Mock(),
                             transport_target=Mock())
        fast = without_ble_delays(base)
        self.assertIs(fast.transport_target, base.transport_target)
        self.assertIs(fast.profile, base.profile)
        self.assertEqual(fast.protocol_variant, base.protocol_variant)
        self.assertEqual(fast.ble_transport_profile.standard_chunk_cap, 180)
        self.assertEqual(fast.ble_transport_profile.standard_write_delay_ms, 0)
        self.assertEqual(fast.ble_transport_profile.notify_service_uuids, base.ble_transport_profile.notify_service_uuids)
        # Changing the local description does not change global vendor defaults.
        self.assertEqual(base.ble_transport_profile.standard_write_delay_ms, 10)

    async def test_chunked_sender_bounds_calls_without_adding_a_second_delay(self):
        printer = Printer.__new__(Printer)
        printer.device = SimpleNamespace(profile=SimpleNamespace(stream=SimpleNamespace(chunk_size=20, delay_ms=30)))
        printer.connection = AsyncMock()
        self.settings.transfer_mode = "chunked"
        self.settings.transfer_chunk_size = 180
        self.settings.transfer_delay_ms = 10
        printer.settings = self.settings
        payload = bytes.fromhex("1b401d49f00f1d763000") + bytes(range(256)) * 3 + b"\n\n\n\n"
        with patch("linear_receiptd.sender.asyncio.sleep", new_callable=AsyncMock) as sleep, patch("builtins.print"):
            await printer.send(payload)
        writes = [call.args[0] for call in printer.connection.send_standard_payload.await_args_list]
        self.assertEqual(b"".join(writes), payload)
        self.assertTrue(all(len(chunk) <= 180 for chunk in writes))
        self.assertIn(180, [len(chunk) for chunk in writes])
        self.assertFalse(any(0 < call.args[0] < 0.5 for call in sleep.await_args_list))
        self.assertEqual(sleep.await_args_list[-1].args, (30,))
        printer.connection.disconnect.assert_not_awaited()

    async def test_chunked_transport_applies_requested_delay_and_rejects_invalid_sizes(self):
        from timiniprint.devices import PrinterDevice
        from timiniprint.protocol.family import ProtocolFamily
        from linear_receiptd.device import with_ble_pacing
        base = PrinterDevice(display_name="CTP520WH", profile=Mock(),
                             protocol_family=ProtocolFamily.INSTAPRINT_CORE,
                             protocol_variant="ctp500", image_pipeline=Mock())
        for chunk_size, delay in ((180, 10), (120, 15)):
            device = with_ble_pacing(base, chunk_size=chunk_size, delay_ms=delay, write_response=True)
            self.assertEqual(device.ble_transport_profile.standard_chunk_cap, chunk_size)
            self.assertEqual(device.ble_transport_profile.standard_write_delay_ms, delay)
            self.assertIs(device.ble_transport_profile.standard_write_response, True)
        for chunk_size, delay in ((0, 10), (513, 10), (True, 10), (180, -1), (180, float("nan"))):
            with self.subTest(chunk_size=chunk_size, delay=delay), self.assertRaises(ValueError):
                with_ble_pacing(base, chunk_size=chunk_size, delay_ms=delay)


class RenderingTests(unittest.TestCase):
    def test_empty_and_long_markdown_render_at_native_width_without_truncating_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            short = render_receipt({"identifier": "DAI-1", "title": "Empty"}, Path(directory) / "short.png")
            issue = {"identifier": "DAI-123", "title": "Long title " * 8, "status": "A very long workflow status name " * 3,
                     "description": "# Heading\n\n" + "A paragraph about a ticket. " * 160 + "\n\n- First\n- Second\n\n```\n" + "code_" * 50 + "\n```\n\nTAIL MARKER",
                     "url": "https://linear.app/opal/issue/DAI-123/test"}
            from linear_receiptd.receipt import Receipt
            seen = []
            original = Receipt.text
            def capture(receipt, text, **kwargs):
                seen.append(text)
                return original(receipt, text, **kwargs)
            with patch.object(Receipt, "text", capture):
                long = render_receipt(issue, Path(directory) / "long.png")
            self.assertEqual(short.width, 384)
            self.assertEqual(long.width, 384)
            self.assertEqual(long.mode, "1")
            self.assertGreater(long.height, 2048)
            self.assertIn("TAIL MARKER", seen)
            self.assertIn("OPAL ELECTRONICS", seen)

    def test_private_settings_file_is_not_world_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            write_private(path, {"credential": "fake"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
