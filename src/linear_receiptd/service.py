import asyncio
import fcntl
import logging
import os
import signal

import aiohttp
from aiohttp import web

from .linear import Linear
from .queue import Queue
from .webhook import create_app
from .worker import Printer, run_worker


async def serve(settings):
    secrets = settings.secrets("linear_api_key", "linear_webhook_secret")
    os.umask(0o077)
    queue = Queue(settings.database)
    lock_path = settings.database.with_suffix(".lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another receipt daemon already owns this queue")
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with aiohttp.ClientSession() as session:
            linear = Linear(session, secrets["linear_api_key"])
            viewer = await linear.viewer()
            if viewer["id"] != settings.user_id:
                raise ValueError("The Linear API key belongs to a different user")
            printer = Printer(settings)
            runner = web.AppRunner(create_app(queue, settings.user_id, secrets["linear_webhook_secret"]),
                                   access_log=None, shutdown_timeout=5)
            await runner.setup()
            try:
                await web.TCPSite(runner, "127.0.0.1", settings.port).start()
                logging.info("Webhook receiver listening on 127.0.0.1:%s; queue %s; transfer %s", settings.port,
                             "paused" if queue.paused() else "active", settings.transfer_mode)
                worker = asyncio.create_task(run_worker(queue, settings, linear, printer, stop))
                stopper = asyncio.create_task(stop.wait())
                try:
                    done, _ = await asyncio.wait((worker, stopper), return_when=asyncio.FIRST_COMPLETED)
                    if worker in done:
                        await worker
                    else:
                        logging.info("Stopping after the active transfer and drain interval")
                        await worker
                finally:
                    stop.set()
                    stopper.cancel()
                    await asyncio.gather(stopper, return_exceptions=True)
            finally:
                await runner.cleanup()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
