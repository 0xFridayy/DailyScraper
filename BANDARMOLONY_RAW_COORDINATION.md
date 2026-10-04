# Shared raw-root coordination

Independent `TradeCaptureStore` databases can share one `raw_root`. Each writer
uses `raw_root/.raw-root-lock.sqlite3` to serialize canonical raw ownership.
The file holds no capture data or raw evidence. It stays outside the
`sha256/<prefix>/<digest>.parquet` evidence layout, has POSIX mode `0600`, and
passes the private-output guard together with its SQLite sidecar paths.

The coordination connection holds `BEGIN IMMEDIATE` before the capture database
starts its body transaction. The lock covers canonical inspection, publication
or reuse, exact-byte verification, body INSERT, COMMIT or rollback, and failed
creator cleanup. Successful body COMMIT permanently relinquishes deletion
ownership before releasing coordination. A reuser never owns deletion.

`resume()` acquires the same root lock before its acceptance transaction. It
verifies the committed body, repairs pending hardlink residue, and then accepts.
Repair failure leaves the body and canonical bytes intact without an acceptance;
resume can retry without the original source file. Every path uses the lock order
raw root, then capture database, to avoid lock-order deadlocks.

SQLite supplies native process locks on Windows and Linux. The coordination file
is a valid empty SQLite database, requires no table writes, and cannot produce a
hot journal through these operations. Closing its connection or terminating its
process releases the lock. The persistent file is not a stale ownership marker.
SQLite contention uses a 30-second timeout and coordination errors fail closed.
Nested calls share coordination only within the same store
and thread. Independent stores and threads acquire their own SQLite connections.

Never delete, replace, or independently open and close the coordination file
while stores are active. On POSIX, closing an unrelated descriptor to that inode
can release process-wide SQLite locks. SQLite manages all handles internally.
Read-only verification does not create or acquire the coordination artifact.

Abrupt termination before body COMMIT can leave canonical or pending raw residue,
as with the existing publication protocol. The next successful capture of those
bytes can adopt and repair it under the root lock. Crash recovery does not infer
ownership by scanning sibling databases or deleting objects that they may use.

Run `python test_bandarmolony_trade_capture.py` for deterministic competing-body,
accepted-raw deletion, repair recovery, process exclusion and termination tests.
On native Windows the equivalent command is
`py -3 test_bandarmolony_trade_capture.py`.
