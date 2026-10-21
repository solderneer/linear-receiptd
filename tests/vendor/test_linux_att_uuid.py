import unittest
from types import SimpleNamespace

from timiniprint import reporting
from timiniprint.devices.bluetooth_profiles import get_ble_transport_profile
from timiniprint.transport.bluetooth.adapters.linux_att import _decode_uuid
from timiniprint.transport.bluetooth.adapters.bleak_adapter_endpoint_resolver import _BleWriteEndpointResolver
from timiniprint.transport.bluetooth.adapters.bleak_adapter_transport import _BleakTransportSession


class AttUuidTests(unittest.TestCase):
    def test_16_bit_uuid(self):
        self.assertEqual(_decode_uuid(bytes.fromhex("0229")), "00002902-0000-1000-8000-00805f9b34fb")

    def test_microchip_uart_128_bit_uuids(self):
        cases = {
            "55e405d2af9fa98fe54a7dfe43535349": "49535343-fe7d-4ae5-8fa9-9fafd205e455",
            "b39b7234beecd4a8f443418843535349": "49535343-8841-43f4-a8d4-ecbe34729bb3",
            "16962447c62361bad94b4d1e43535349": "49535343-1e4d-4bd9-ba61-23c647249616",
        }
        for raw, expected in cases.items():
            with self.subTest(uuid=expected):
                self.assertEqual(_decode_uuid(bytes.fromhex(raw)), expected)

    def test_coreprint_binds_microchip_uart_notification_channel(self):
        write = SimpleNamespace(uuid="49535343-8841-43f4-a8d4-ecbe34729bb3", properties=["write", "write-without-response"])
        notify = SimpleNamespace(uuid="49535343-1e4d-4bd9-ba61-23c647249616", properties=["read", "notify"])
        service = SimpleNamespace(uuid="49535343-fe7d-4ae5-8fa9-9fafd205e455", characteristics=[write, notify])
        resolver = _BleWriteEndpointResolver()
        selection = resolver.resolve([service])
        self.assertIs(selection.char, write)
        session = _BleakTransportSession(get_ble_transport_profile("instaprint_core"), resolver, reporting.DUMMY_REPORTER)
        session.configure_endpoints([service])
        self.assertIs(session.bindings.notify_char, notify)

    def test_coreprint_without_microchip_uart_has_no_extra_subscription(self):
        session = _BleakTransportSession(get_ble_transport_profile("instaprint_core"), _BleWriteEndpointResolver(), reporting.DUMMY_REPORTER)
        session.configure_endpoints([])
        self.assertIsNone(session.bindings.notify_char)
