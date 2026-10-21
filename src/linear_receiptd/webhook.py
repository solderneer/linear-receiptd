"""Only the signed webhook and an uninformative health check are public."""
import hashlib
import hmac
import json
import math
import re
import time

from aiohttp import web


def verify(raw, signature, secret, *, now=None):
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", signature) or not hmac.compare_digest(expected, signature.lower()):
        raise PermissionError("Invalid signature")
    try:
        payload = json.loads(raw)
        timestamp = payload["webhookTimestamp"]
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError
        if abs((time.time() if now is None else now) * 1000 - timestamp) > 60_000:
            raise PermissionError("Expired timestamp")
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("Invalid webhook body") from error
    return payload


def create_app(queue, user_id, secret):
    app = web.Application(client_max_size=2 * 1024 * 1024)

    async def health(request):
        return web.json_response({"ok": True})

    async def receive(request):
        raw = await request.read()
        try:
            payload = verify(raw, request.headers.get("Linear-Signature"), secret)
        except PermissionError:
            raise web.HTTPUnauthorized()
        except ValueError:
            raise web.HTTPBadRequest()
        delivery = request.headers.get("Linear-Delivery", "")
        if not delivery or len(delivery) > 200:
            raise web.HTTPBadRequest(reason="Missing delivery ID")
        try:
            result = queue.accept(delivery, payload, user_id)
        except ValueError:
            raise web.HTTPBadRequest(reason="Invalid assignment")
        # Committed to disk before replying; no Linear or BLE work in this path.
        return web.json_response({"accepted": result})

    app.router.add_get("/health", health)
    app.router.add_post("/webhooks/linear", receive)
    return app
