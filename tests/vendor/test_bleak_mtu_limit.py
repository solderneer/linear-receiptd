from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from timiniprint.devices.bluetooth_profiles import get_ble_transport_profile
from timiniprint.transport.bluetooth.adapters.bleak_adapter import _BleakSocket


class BleakMtuTests(unittest.IsolatedAsyncioTestCase):
    async def open_socket(self, *, payload_limit=509, requested_mtu=512, linux=True):
        char = SimpleNamespace(uuid="49535343-8841-43f4-a8d4-ecbe34729bb3",
                               properties=["write", "write-without-response"],
                               max_write_without_response_size=payload_limit)
        services = [SimpleNamespace(uuid="49535343-fe7d-4ae5-8fa9-9fafd205e455", characteristics=[char])]
        client = SimpleNamespace(connect=AsyncMock(), mtu_size=23, services=services, write_gatt_char=AsyncMock())
        profile = replace(get_ble_transport_profile("instaprint_core"), standard_write_response=True,
                          standard_chunk_cap=180, standard_write_delay_ms=0)
        socket = _BleakSocket(ble_profile=profile, ble_mtu_request=requested_mtu)
        socket._resolve_client_target = AsyncMock(return_value="test")
        with patch("bleak.BleakClient", return_value=client), \
             patch("timiniprint.transport.bluetooth.adapters.bleak_adapter.IS_LINUX", linux), \
             patch("timiniprint.transport.bluetooth.adapters.bleak_adapter.read_device_information", new_callable=AsyncMock) as info:
            info.return_value = SimpleNamespace(log=Mock())
            await socket._open_async("test")
        return socket, client

    async def test_bluez_negotiated_mtu_is_used_for_acknowledged_writes(self):
        socket, client = await self.open_socket()
        self.assertEqual(socket._mtu_size, 509)
        await socket._send_async(b"x" * 360)
        self.assertEqual([len(call.args[1]) for call in client.write_gatt_char.await_args_list], [180, 180])
        self.assertTrue(all(call.kwargs["response"] for call in client.write_gatt_char.await_args_list))

    async def test_requested_mtu_still_bounds_writes(self):
        socket, _ = await self.open_socket(requested_mtu=100)
        self.assertEqual(socket._mtu_size, 97)

    async def test_negotiated_limit_is_available_without_an_explicit_request(self):
        socket, _ = await self.open_socket(requested_mtu=None)
        self.assertEqual(socket._mtu_size, 509)

    async def test_unavailable_or_invalid_limit_retains_safe_default(self):
        for limit in (20, None, 0, True):
            with self.subTest(limit=limit):
                socket, _ = await self.open_socket(payload_limit=limit)
                self.assertEqual(socket._mtu_size, 20)

    async def test_other_platforms_keep_their_existing_mtu_policy(self):
        socket, _ = await self.open_socket(linux=False)
        self.assertEqual(socket._mtu_size, 20)
