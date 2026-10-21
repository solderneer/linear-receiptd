"""Local transport settings verified with the CTP520WH / Stone Printer."""
from dataclasses import dataclass, fields, replace
import math

from timiniprint.devices import PrinterDevice


@dataclass(frozen=True)
class CorePrintTransportDevice(PrinterDevice):
    ble_chunk_cap: int = 180
    ble_delay_ms: float = 0
    ble_write_response: bool | None = None

    @property
    def ble_transport_profile(self):
        # Keep UART endpoints, notifications, and negotiated ATT payload limits.
        return replace(super().ble_transport_profile,
                       standard_chunk_cap=self.ble_chunk_cap,
                       standard_write_delay_ms=self.ble_delay_ms,
                       standard_write_response=self.ble_write_response)


def with_ble_pacing(device, *, chunk_size=180, delay_ms=10, write_response=None):
    if device.protocol_family.value != "instaprint_core":
        raise ValueError("These local transport settings are for the CorePrint printer only")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or not 1 <= chunk_size <= 512:
        raise ValueError("BLE chunk size must be an integer from 1 to 512")
    if isinstance(delay_ms, bool) or not isinstance(delay_ms, (int, float)) or not math.isfinite(delay_ms) or not 0 <= delay_ms <= 1000:
        raise ValueError("BLE write delay must be from 0 to 1000 ms")
    if write_response is not None and not isinstance(write_response, bool):
        raise ValueError("BLE write response must be a boolean or None")
    values = {field.name: getattr(device, field.name) for field in fields(PrinterDevice)}
    return CorePrintTransportDevice(**values, ble_chunk_cap=chunk_size, ble_delay_ms=delay_ms,
                                   ble_write_response=write_response)


def without_ble_delays(device):
    return with_ble_pacing(device, chunk_size=180, delay_ms=0)
