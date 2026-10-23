# Bundled code and fonts

`src/timiniprint` contains TiMini-Print from
https://github.com/Dejniel/TiMini-Print at commit
`95beec7ba0f6422c29fd480ca262e369caf5e375`, under Apache-2.0.
The unrelated upstream application entry points are omitted. Its license,
NOTICE, and dependency notices are preserved under `licenses/TiMini-Print`.

The snapshot includes these local changes, retained from the working printer:

- Correct 128-bit ATT UUID byte order on Linux.
- Subscribe to the CorePrint Microchip UART notification characteristic.
- Support an explicit GATT write-response policy for standard writes.
- Recover the negotiated Linux payload limit from the public Bleak
  characteristic property when BlueZ's client MTU reports its default.
- Expose a passive connection-health check, pump idle Bleak disconnect
  callbacks, and mark direct ATT EOF/errors disconnected so response waiters
  fail promptly rather than retaining a stale connection.

Regression tests for these changes are in `tests/vendor`. Keep the snapshot
and its fixes together when updating upstream; this repository needs no
submodules, second checkout, or upstream runtime update check.

`src/linear_receiptd/assets/fonts` contains Liberation Sans (regular and bold)
and Liberation Mono (regular), under the SIL Open Font License 1.1. The full
copyright and license text accompanies the font files. These fonts make the
receipt layout independent of Linux fontconfig and installed system fonts.

Python dependencies are installed separately by pip under their own licenses.
The third-party licenses above apply to their respective components; they do
not assign a license to the original application code.
