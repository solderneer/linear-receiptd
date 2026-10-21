"""Explicit BLE discovery and authenticated HTTP checks that never queue a job."""
import hashlib
import hmac
import json
import time
import uuid

import aiohttp


async def scan(seconds=10):
    from bleak import BleakScanner
    devices = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for device, advertisement in sorted(devices.values(), key=lambda item: item[0].address):
        name = advertisement.local_name or device.name or "(unnamed)"
        print(f"{device.address}  {name}  RSSI {advertisement.rssi}")
    print("Use this machine's address/UUID in setup. This scan sends no printer commands.")


async def check_endpoint(session, settings):
    origin = settings.public_url
    try:
        async with session.get(origin + "/health", timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status != 200 or await response.json() != {"ok": True}:
                raise ValueError("Public receiver health check failed")
        endpoint = origin + "/webhooks/linear"
        async with session.post(endpoint, data=b"{}", timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status != 401:
                raise ValueError("Public receiver did not reject an unsigned webhook")
        # Unknown event types are durably acknowledged as ignored. No issue ID
        # or assignee is present, so this cannot create a print job.
        raw = json.dumps({"type": "ReceiptConnectivityCheck", "action": "noop",
                          "webhookTimestamp": int(time.time() * 1000)}).encode()
        secret = settings.secrets("linear_webhook_secret")["linear_webhook_secret"]
        headers = {"Linear-Signature": hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest(),
                   "Linear-Delivery": "receipt-check-" + str(uuid.uuid4()),
                   "Content-Type": "application/json"}
        async with session.post(endpoint, data=raw, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status != 200 or (await response.json()).get("accepted") != "ignored":
                raise ValueError("Public receiver did not authenticate the harmless signed check")
    except (aiohttp.ClientError, TimeoutError) as error:
        raise ValueError("Could not reach the public receiver; check the running service and tunnel") from error
