import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from timiniprint import reporting
from timiniprint.devices.bluetooth_profiles import BleBulkWriteProfile, get_ble_transport_profile
from timiniprint.transport.bluetooth.adapters.bleak_adapter_endpoint_resolver import _BleWriteEndpointResolver
from timiniprint.transport.bluetooth.adapters.bleak_adapter_transport import _BleakTransportSession
from timiniprint.transport.bluetooth.adapters.linux_att import _LinuxAttClient


class WriteResponseTests(unittest.IsolatedAsyncioTestCase):
    def session(self, response=True, properties=("write", "write-without-response"), bulk=False):
        profile = replace(get_ble_transport_profile("instaprint_core"), standard_write_response=response,
                          standard_write_delay_ms=0, standard_chunk_cap=48,
                          bulk_write=BleBulkWriteProfile("bulk", chunk_cap=48, write_delay_ms=0) if bulk else None)
        session = _BleakTransportSession(profile, _BleWriteEndpointResolver(), reporting.DUMMY_REPORTER)
        char = SimpleNamespace(uuid="49535343-8841-43f4-a8d4-ecbe34729bb3", properties=properties,
                               max_write_without_response_size=20)
        session.bindings.write_char = char
        session.bindings.write_selection_strategy = "preferred_uuid"
        session.bindings.write_response_preference = False
        return session, char

    async def test_standard_chunks_wait_for_each_response_before_sending_next(self):
        session, char = self.session()
        permits = asyncio.Semaphore(0)
        calls = []
        async def write(characteristic, chunk, response=False):
            calls.append((bytes(chunk), response))
            await permits.acquire()
        client = SimpleNamespace(write_gatt_char=write)
        task = asyncio.create_task(session.send(client, bytes(range(100)), mtu_size=64, timeout=1))
        try:
            for expected_count in (1, 2, 3):
                await asyncio.sleep(0)
                self.assertEqual(len(calls), expected_count)
                self.assertFalse(task.done())
                permits.release()
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(b"".join(chunk for chunk, _ in calls), bytes(range(100)))
        self.assertTrue(all(response for _, response in calls))
        self.assertEqual([len(chunk) for chunk, _ in calls], [48, 48, 4])

    async def test_acknowledged_writes_respect_negotiated_payload_limit(self):
        session, _ = self.session()
        client = SimpleNamespace(write_gatt_char=AsyncMock())
        await session.send(client, b"x" * 85, mtu_size=20, timeout=1)
        self.assertTrue(all(len(call.args[1]) <= 20 for call in client.write_gatt_char.await_args_list))
        self.assertTrue(all(call.kwargs["response"] for call in client.write_gatt_char.await_args_list))

    async def test_rejected_or_timed_out_write_is_not_retried(self):
        for error in (RuntimeError("ATT rejection"), TimeoutError("No ATT response")):
            session, _ = self.session()
            client = SimpleNamespace(write_gatt_char=AsyncMock(side_effect=error))
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                await session.send(client, b"x" * 200, mtu_size=64, timeout=1)
            client.write_gatt_char.assert_awaited_once()

    async def test_unsupported_response_mode_fails_before_any_bytes(self):
        for mode, properties in ((True, ("write-without-response",)), (False, ("write",))):
            session, _ = self.session(response=mode, properties=properties)
            client = SimpleNamespace(write_gatt_char=AsyncMock())
            with self.subTest(mode=mode), self.assertRaises(RuntimeError):
                await session.send(client, b"data", mtu_size=64, timeout=1)
            client.write_gatt_char.assert_not_awaited()

    async def test_unset_override_preserves_preferred_uuid_behavior(self):
        session, _ = self.session(response=None)
        client = SimpleNamespace(write_gatt_char=AsyncMock())
        await session.send(client, b"data", mtu_size=64, timeout=1)
        self.assertFalse(client.write_gatt_char.await_args.kwargs["response"])

    async def test_control_uses_standard_override_and_bulk_keeps_its_policy(self):
        session, char = self.session(bulk=True)
        session.bindings.bulk_write_char = char
        client = SimpleNamespace(write_gatt_char=AsyncMock())
        session._client = client
        await session.send_control_packet(b"control", timeout=1)
        self.assertTrue(client.write_gatt_char.await_args.kwargs["response"])
        client.write_gatt_char.reset_mock()
        await session.send_bulk_payload(client, b"bulk", mtu_size=64, timeout=1)
        self.assertFalse(client.write_gatt_char.await_args.kwargs["response"])

    async def test_linux_acknowledged_write_uses_att_request_not_command(self):
        client = _LinuxAttClient.__new__(_LinuxAttClient)
        client._timeout = 1
        client._resolve_characteristic = Mock(return_value=SimpleNamespace(handle=4))
        client._request = Mock(return_value=b"\x13")
        client._send_pdu = Mock()
        await client.write_gatt_char("uart", b"abc", response=True)
        self.assertEqual(client._request.call_args.args[0], b"\x12\x04\x00abc")
        self.assertEqual(client._request.call_args.kwargs["expected_opcodes"], {0x13})
        client._send_pdu.assert_not_called()
