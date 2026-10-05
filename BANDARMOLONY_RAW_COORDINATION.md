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
Nested calls share coordination only within the same store, thread and creating
process. Independent stores and threads acquire their own SQLite connections.

Each `TradeCaptureStore` belongs to the process that constructed it. It records
`os.getpid()` and a fork generation that `os.register_at_fork` advances in every
child, so a reused PID cannot revive an inherited store. A POSIX `fork()` child
inherits the store object, its SQLite connection and the forking thread's
nested-lock flag, but none of the parent's native locks. In the child, every
operation, `conn`, the raw-root lock, `with`, and `close()` raise
`INHERITED_STORE_REFUSAL` before any private-output inspection, SQLite use or
raw-root access. SQLite forbids using inherited connections and even closing
them, because close cleanup can delete content that the parent still needs. A
child must construct a new store, which opens its own connection and acquires
the real coordinator.

A child should end with `os._exit()`, as multiprocessing does, or `exec`,
without unwinding frames that were inside a store operation at the fork, for
example after a signal handler forks. The store hardens that unwinding but
cannot make arbitrary parent frames safe. In the child, store cleanup never
rolls back the parent's transaction, removes a pending alias, deletes a
published raw object, or closes an inherited connection. Pending raw bytes are
written unbuffered, so no copied file buffer can append to them. An in-flight
exception, such as the child's `SystemExit`, keeps propagating unchanged. A
normal exit from the outermost inherited raw-root lock context, or from an
inherited `with store:` block, raises `INHERITED_STORE_REFUSAL`. The store keeps
the parent's coordination connection referenced instead of closing it.
Interpreter shutdown in a child that exits normally still finalizes inherited
`sqlite3` objects, and the store cannot prevent that.

Construct a child's stores after `os.fork()` returns. A store built inside an
`os.register_at_fork(after_in_child=...)` hook that was registered before this
module was imported records the pre-advance generation, so it is refused even
in its own process.

Fork only while no thread in the process holds a store lock, coordination
connection or store transaction, or use the spawn or forkserver start methods.
SQLite's lock bookkeeping and mutexes are per process, not per thread. A child
forked while any thread held the root lock inherits that bookkeeping for the
coordination file. Its fresh stores then fail closed after the 30-second busy
timeout, for the child's whole life, even after the parent releases the native
lock (observed with SQLite 3.46.1 on Linux). A fork that lands while another
thread is inside SQLite can deadlock the child instead.

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
It also covers inherited-store refusal, with real `os.fork()` children on POSIX
and a simulated process identity everywhere.
On native Windows the equivalent command is
`py -3 test_bandarmolony_trade_capture.py`.
