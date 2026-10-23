# linear-receiptd

A local daemon that prints a receipt whenever a Linear issue is newly assigned
to you. It renders the full ticket, metadata, Markdown description, and a QR
code on a 384-pixel InstaPrint/CorePrint BLE receipt printer.

The default heading is **OPAL ELECTRONICS** and can be changed in configuration.
Receipts are queued in SQLite before acknowledging Linear, deduplicated across
retries, and sent over one persistent Bluetooth connection. A failure during
printing pauses the queue for inspection rather than automatically printing a
possibly duplicated receipt.

## Platforms and printer support

| Platform | Service manager | Status |
| --- | --- | --- |
| Linux with BlueZ and systemd | User systemd units | Implementation extracted from the working CTP520WH setup; automated tests pass on Linux |
| Other Linux distributions with BlueZ | Run foreground commands in your supervisor | Python application is portable; automatic installation requires systemd |
| macOS 12.3+ | Per-user LaunchAgents | Implemented and covered by generated-plist tests; Bluetooth printing on a Mac has not been hardware tested |

The supported printer is the **Core Innovations CTP520WH**, advertised as
**Stone Printer**, using the InstaPrint/CorePrint raster protocol over the
Microchip BLE UART. Other printers need their own verified profile; the
bundled catalog does not make every printer supported by this daemon.

TiMini-Print's source, catalog, and the transport fixes used by the working
printer are bundled. There is no submodule or separate checkout to download.
The repository contains no account credentials, real tickets, printer address,
queue database, or installed ngrok binary. Third-party notices are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Install

Requirements: Python **3.11+**, a BLE-capable adapter, and internet access for
Linear and your public HTTPS tunnel. Install BlueZ and enable Bluetooth on
Linux. On Debian/Ubuntu, also install `python3-venv`; on other distributions,
use their Python venv and Bluetooth packages. Run the application as your
normal user with permission to access the Bluetooth adapter.

```sh
cd linear-receiptd
./scripts/bootstrap
. .venv/bin/activate
linear-receiptd --help
```

Alternatively, install into an existing virtual environment:

```sh
python -m pip install .
```

Keep this virtual environment at its installed location after generating
services. Their commands use its absolute Python path and do not depend on
shell activation, the current directory, or `PYTHONPATH`.

For an offline receipt preview before configuring an account:

```sh
linear-receiptd preview --issue-json examples/issue.json --output /tmp/receipt.png
```

### macOS Bluetooth permission

Install a current Python, enable Bluetooth, and run `scan` interactively from
Terminal first. Grant Bluetooth permission when macOS requests it. If denied,
change it in **System Settings → Privacy & Security → Bluetooth**. A background
LaunchAgent may need permission for its Python process as well; verify that it
can connect before relying on automatic printing.

macOS identifies the printer by a UUID unique to that Mac. Use the UUID from
`linear-receiptd scan` on that machine; a Linux Bluetooth MAC address cannot be
copied over. See [Bleak's macOS documentation](https://bleak.readthedocs.io/en/latest/backends/macos.html).

## Configure Linear, the printer, and HTTPS

1. Turn the printer on nearby and close the phone app so it can connect.
2. Find its address or macOS UUID:

   ```sh
   linear-receiptd scan
   ```

3. Create a Linear personal API key with **Read** permission in
   [Security & access](https://linear.app/settings/account/security).
   Setup discovers your user ID and display name from the key.
4. Choose a public HTTPS tunnel. For the built-in **ngrok** option, install the
   ngrok v3 agent using [its official installation instructions](https://ngrok.com/download),
   obtain an authtoken, and reserve a stable HTTPS domain. Setup saves the
   executable's absolute path so systemd and launchd can find it. For an
   existing reverse proxy or other tunnel, choose **external** and manage it
   separately; forward requests unchanged to `http://127.0.0.1:8765`.
5. Enter credentials locally with hidden input:

   ```sh
   linear-receiptd setup
   ```

6. In Linear workspace **Settings → API → Webhooks**, create an **Issues**
   webhook at the exact URL displayed by setup:

   ```text
   https://YOUR-STABLE-DOMAIN/webhooks/linear
   ```

   Select the desired team coverage, keep it enabled, and paste its signing
   secret into setup. Creating or editing webhooks requires workspace admin
   access. Overlapping team subscriptions are deduplicated. The read-only API
   key does not create or modify the webhook. You can rerun setup to finish
   entering the signing secret while keeping saved values.
7. Validate, inspect the service definitions, and install:

   ```sh
   linear-receiptd doctor
   linear-receiptd prepare-services --output /tmp/receipt-services
   linear-receiptd install
   linear-receiptd doctor --check-endpoint
   linear-receiptd status
   linear-receiptd resume
   ```

   The first queue starts **paused**, and setup does not import existing
   assignments. The endpoint check validates health, rejects unsigned input,
   and sends an authenticated no-op that cannot create a print job. Assign a
   test issue to yourself to check the complete physical print path.

`install` creates `linear-receiptd.service` and, when ngrok is selected,
`linear-receiptd-ngrok.service` on Linux. On macOS, it creates
`io.linear-receiptd.plist` and `io.linear-receiptd.ngrok.plist` in your
`~/Library/LaunchAgents`. Existing differing service definitions are refused,
so installing another checkout cannot silently replace a working deployment.

User services require your user session. On systemd Linux, an administrator
can enable lingering with `loginctl enable-linger "$USER"` for operation after
logout. macOS LaunchAgents run in your logged-in session. The computer must
remain awake and online on either platform.

## Files and configuration

| Data | Linux default | macOS default |
| --- | --- | --- |
| Configuration, signing secret, and printer selection | `~/.config/linear-receiptd/` | `~/Library/Application Support/linear-receiptd/` |
| Queue, rendered receipts, and generated ngrok config | `~/.local/share/linear-receiptd/` | `~/Library/Application Support/linear-receiptd/` |

XDG overrides are respected on Linux. Use `linear-receiptd --config /path/to/config.json COMMAND`
for a custom configuration; relative paths in that JSON resolve beside it.
Credentials are stored in `secrets.json` with mode `0600`, never in Git or
service arguments. Ticket snapshots and receipts are private local data.
Services and CLI commands use umask `0077`.

Setup fills these fields in `config.json`:

```json
{
  "user_id": "YOUR_LINEAR_USER_ID",
  "assignee_name": "Your name",
  "receipt_header": "OPAL ELECTRONICS",
  "public_url": "https://YOUR-STABLE-DOMAIN",
  "tunnel_provider": "ngrok",
  "ngrok_path": "/absolute/path/to/ngrok",
  "port": 8765,
  "transfer_mode": "chunked",
  "transfer_chunk_size": 180,
  "transfer_delay_ms": 10,
  "transfer_write_response": true,
  "drain_seconds": 30
}
```

`printer.json` is generated from the bundled tested CorePrint profile and your
selected address. It forces BLE, requests MTU 512, and uses 384-pixel plain
roll paper at 203 DPI. Keep `secrets.json`, databases, and real issue exports
outside the repository.

## Everyday commands

```sh
linear-receiptd status
linear-receiptd pause
linear-receiptd resume
linear-receiptd preview --issue DEMO-123 --output /tmp/receipt.png
linear-receiptd reprint --job 7
linear-receiptd reprint --issue DEMO-123
```

Only newly created issues assigned to you and actual assignee changes to you
print. Comments and title edits do not. The worker fetches the full current
issue before printing; deleted, archived, or reassigned webhook jobs are
skipped. A later reassignment back to you creates another receipt. A manual
reprint is explicit and can print an issue that is not assigned to you.

For a supervised or non-systemd installation, run these as separate foreground
processes using the same configuration:

```sh
linear-receiptd serve
linear-receiptd tunnel  # ngrok only; external tunnels have their own process
```

Linux logs and graceful stop:

```sh
journalctl --user -u linear-receiptd -u linear-receiptd-ngrok -f
systemctl --user stop linear-receiptd linear-receiptd-ngrok
```

On macOS, logs are under the data directory's `logs/` folder. To stop and unload
an installed LaunchAgent, run `launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/io.linear-receiptd.plist`
and do the same for `io.linear-receiptd.ngrok.plist` when installed. Load them
again with `linear-receiptd install`.

Before using InstaPrint or printing an image manually, pause and gracefully
stop the daemon so it can finish its active receipt. Then:

```sh
linear-receiptd print-image /path/to/image.png
```

The manual command uses the same raster builder, transport, pacing, and drain
as the daemon and refuses a second owner of the same queue lock.

## Delivery and printing guarantees

The receiver binds only to `127.0.0.1`. Public routes are `/webhooks/linear` and
an uninformative `/health`; there are no remote print or administration routes.
Each delivery checks the HMAC-SHA256 signature on the raw body and a 60-second
timestamp window, then commits the event and any print job before returning
HTTP 200. Linear delivery IDs and assignment fingerprints prevent duplicates.
See [Linear's webhook documentation](https://linear.app/developers/webhooks).

Connection failures before any writes remain queued with bounded retries.
Once sending starts, an interrupted transfer becomes **uncertain** and pauses
printing. Inspect the paper, request an explicit reprint if needed, then
resume. The **sent** status records transfer plus passive drain, not verified
physical completion. No automatic replay can guarantee exactly one physical
receipt after a power loss or broken connection.

The retained default uses 180-byte acknowledged BLE writes with 10 ms pacing,
a 0.5-second initialization pause, and a passive 30-second drain. It keeps the
link open between jobs, checks idle transport liveness every five seconds, and
reconnects if the printer drops the link. Connection attempts continue while
the queue is paused; offline attempts retry after 15 seconds. These checks
observe transport state and process OS callbacks without sending printer
payloads. A lost saved connection is replaced before any print writes. This profile produced complete clean full receipts on
the original Linux setup, though paper movement sometimes stuttered. A GATT
response is not a printer completion acknowledgement. Larger 480-byte writes
and sustained unpaced full receipts corrupted content in testing; the small
unpaced sample alone was misleading. The new extraction has not run a new
hardware print or changed those transport settings. Do not remove pacing
based only on short samples.

SIGTERM waits for the active transfer and drain before disconnecting. Generated
systemd units use an unlimited stop timeout; LaunchAgents set `ExitTimeOut=0`
for the receiver. Sleep, forced termination, and power loss can still interrupt
a job. Extended downtime can exhaust Linear's webhook retries and require
re-enabling the subscription.

## Existing installations

Extraction does not move a running installation. Do not run `install`, start
a second receiver on the same port, or copy its live SQLite database while
another daemon is writing to it. A later planned migration should finish any
active receipt, stop the old receiver gracefully, back up its database using
SQLite's backup API, copy the required account/printer configuration privately,
and review new service definitions. Retain that queue to preserve deduplication.
The loader accepts the old `ngrok_url` field as `public_url`; absolute paths in
old configurations still point to the old installation until migrated.

## Development and verification

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -v
python -m build
```

Tests cover signed HTTP ingress, queue recovery and deduplication, held BLE
links, acknowledged writes and MTU limits, long receipts, bundled fonts and
printer configuration, Linux service validation, macOS plist generation, and
refusal to overwrite another installation. They use fake printers and do not
scan or print. CI is configured for Linux and macOS with Python 3.11 and 3.13.
A source distribution and wheel include the printer catalog, fonts, transport
fixes, and third-party notices. Review [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
before updating the bundled upstream snapshot.
