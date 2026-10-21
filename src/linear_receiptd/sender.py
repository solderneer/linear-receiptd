"""Print through CorePrint's BLE UART with explicit per-device pacing."""
import asyncio
import os
import sys
import time

async def wait_for_disconnect(input_fd=None):
    """Wait for Enter, a GUI disconnect request, or the owner closing stdin."""
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno() if input_fd is None else input_fd
    request = loop.create_future()

    def readable():
        try:
            os.read(fd, 4096)
        except OSError as error:
            if not request.done():
                request.set_exception(error)
        else:
            if not request.done():
                request.set_result(None)
        loop.remove_reader(fd)

    loop.add_reader(fd, readable)
    try:
        await request
    finally:
        loop.remove_reader(fd)


async def send_job_slowly(connection, payload, *, chunk_size, delay_ms, hold_open_seconds=None):
    if chunk_size < 1 or delay_ms < 0 or (hold_open_seconds is not None and hold_open_seconds < 0):
        raise ValueError("Invalid transfer pacing")
    if not payload.startswith(bytes.fromhex("1b401d49f0")):
        raise ValueError("Expected an InstaPrint/CorePrint raster job")
    # Keep initialization and density separate from raster transfer, as in the
    # reference client. Each send is bounded even if the backend ignores pacing.
    for offset in range(0, 6, chunk_size):
        await connection.send_standard_payload(payload[offset:min(offset + chunk_size, 6)])
        await asyncio.sleep(delay_ms / 1000)
    await asyncio.sleep(0.5)
    started = time.monotonic()
    last_reported = -1
    for offset in range(6, len(payload), chunk_size):
        chunk = payload[offset:offset + chunk_size]
        await connection.send_standard_payload(chunk)
        await asyncio.sleep(delay_ms / 1000)
        sent = min(offset + len(chunk), len(payload))
        progress = sent * 10 // len(payload)
        if progress != last_reported:
            print(f"Transfer: {sent}/{len(payload)} bytes", flush=True)
            last_reported = progress
    print(f"Transfer complete in {time.monotonic() - started:.1f}s.", flush=True)
    # Keeping the link open is passive; it sends no commands or retries and is
    # not a printer-completion acknowledgement.
    if hold_open_seconds is None:
        print("BLE link remains open. Press Enter or use Disconnect when the paper has finished.", flush=True)
        await wait_for_disconnect()
    else:
        print(f"Keeping the BLE link open for {hold_open_seconds:g}s.", flush=True)
        await asyncio.sleep(hold_open_seconds)
