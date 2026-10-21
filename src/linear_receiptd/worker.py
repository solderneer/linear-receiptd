"""One BLE owner. Transferred bytes are never treated as a completion ACK."""
import asyncio
import json
import logging
import time

from .linear import LinearError
from .receipt import render_receipt

log = logging.getLogger(__name__)


class TransferUncertain(Exception):
    pass


class Printer:
    def __init__(self, settings, *, reporter=None):
        # Deferred imports allow preview and queue commands without Bluetooth.
        from timiniprint.devices import PrinterCatalog
        from timiniprint.printing.builder import PrintJobBuilder
        from timiniprint.printing.settings import PrintSettings
        from timiniprint.transport.bluetooth import BleakBluetoothConnector

        config = json.loads(settings.printer_config.read_text())
        self.device = PrinterCatalog.load().device_from_printer_config(config)
        if self.device.protocol_family.value != "instaprint_core":
            raise ValueError("Only the tested InstaPrint/CorePrint sender is supported")
        if settings.transfer_mode == "unpaced":
            from .device import without_ble_delays
            self.device = without_ble_delays(self.device)
        elif settings.transfer_mode == "chunked":
            from .device import with_ble_pacing
            self.device = with_ble_pacing(self.device, chunk_size=settings.transfer_chunk_size,
                                         delay_ms=settings.transfer_delay_ms,
                                         write_response=settings.transfer_write_response)
        self.builder = PrintJobBuilder(self.device, settings=PrintSettings(
            trim_side_margins=False, trim_top_bottom_margins=False))
        self.connector = BleakBluetoothConnector() if reporter is None else BleakBluetoothConnector(reporter)
        self.connection = None
        self.settings = settings

    def prepare(self, path):
        job = self.builder.build_from_file(str(path))
        if job.steps or not job.payload.startswith(bytes.fromhex("1b401d49f0")):
            raise ValueError("Expected a stream-only CorePrint raster job")
        return job.payload

    async def connect(self):
        if self.connection is None:
            self.connection = await self.connector.connect(self.device)

    async def send(self, payload):
        from .sender import send_job_slowly

        stream = self.device.profile.stream
        unpaced = self.settings.transfer_mode == "unpaced"
        chunked = self.settings.transfer_mode == "chunked"
        await send_job_slowly(self.connection, payload,
                              chunk_size=len(payload) if unpaced else self.settings.transfer_chunk_size if chunked else stream.chunk_size,
                              delay_ms=0 if unpaced or chunked else stream.delay_ms,
                              hold_open_seconds=self.settings.drain_seconds)
        # No disconnect here: allow the printer to finish and reuse this link.

    async def close(self):
        if self.connection is not None:
            connection, self.connection = self.connection, None
            try:
                await connection.disconnect()
            except Exception:
                log.warning("BLE disconnect failed")


async def process_one(queue, settings, linear, printer, stop):
    job = queue.claim()
    if job is None:
        return False
    job_id = job["id"]
    try:
        issue = await linear.issue(job["issue_id"])
        if issue is None or issue.get("archivedAt"):
            queue.update(job_id, status="skipped", error="Issue removed or archived")
            return True
        if job["source"] == "webhook" and (issue.get("assignee") or {}).get("id") != settings.user_id:
            queue.update(job_id, status="skipped", error="Issue is no longer assigned to you")
            return True
        path = settings.receipt_dir / f"job-{job_id}.png"
        assigned = issue.get("assignee") or {}
        assignee_name = settings.assignee_name if assigned.get("id") == settings.user_id else assigned.get("name", "Unassigned")
        await asyncio.to_thread(render_receipt, issue, path, assignee=assignee_name, header=settings.receipt_header)
        payload = await asyncio.to_thread(printer.prepare, path)
        queue.update(job_id, receipt_path=str(path), snapshot=json.dumps(issue))
    except LinearError as error:
        if error.permanent:
            queue.update(job_id, status="failed", error=str(error))
            queue.pause()
        else:
            queue.retry(job, str(error))
        return True
    except Exception:
        log.exception("Could not render or prepare job %s", job_id)
        queue.update(job_id, status="failed", error="Receipt preparation failed; see service log")
        return True

    try:
        await printer.connect()
    except Exception:
        # No print bytes attempted: retry is safe if the printer is offline.
        log.warning("Printer unavailable for job %s; queued for retry", job_id)
        queue.retry(job, "BLE connection unavailable")
        return True

    if stop.is_set() or queue.paused():
        queue.update(job_id, status="queued")
        return True

    # Must commit before invoking the sender: a crash can now mean partial paper.
    queue.update(job_id, status="sending", error=None)
    try:
        await printer.send(payload)
    except BaseException as error:
        queue.update(job_id, status="uncertain", error="Transfer interrupted; inspect paper before reprinting")
        queue.pause()
        await printer.close()
        log.error("Job %s may be partially printed; queue paused", job_id)
        if isinstance(error, Exception):
            raise TransferUncertain(f"Interrupted job {job_id}") from error
        raise
    queue.update(job_id, status="sent", sent_at=time.time())
    log.info("Job %s transferred; physical completion is unconfirmed", job_id)
    return True


async def run_worker(queue, settings, linear, printer, stop):
    queue.recover()
    try:
        while not stop.is_set():
            try:
                processed = await process_one(queue, settings, linear, printer, stop)
            except TransferUncertain:
                # process_one already persisted uncertain and paused the queue.
                processed = True
            if not processed:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
    finally:
        # Shutdown waits for the active transfer and passive drain before here.
        await printer.close()
