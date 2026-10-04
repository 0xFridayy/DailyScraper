"""Offline process and failure checks for shared raw-root coordination."""

from contextlib import closing, contextmanager
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

import bandarmolony_trade_capture as capture
import bandarmolony_trade_contract as contract


LOCK_NAME = ".raw-root-lock.sqlite3"
INHERITED_OPERATIONS = ("raw-root lock", "connection", "ingest", "absence", "resume",
                        "verify", "read rows", "inspect", "enter", "close")
real_fork = unittest.skipUnless(hasattr(os, "fork"), "real os.fork() ownership checks need POSIX")


class _NoWaitConnection(sqlite3.Connection):
    """Make a contention assertion immediate without changing the real lock."""

    def execute(self, statement, *args, **kwargs):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            super().execute("PRAGMA busy_timeout = 0")
        return super().execute(statement, *args, **kwargs)


class _FailBeginConnection(sqlite3.Connection):
    def execute(self, statement, *args, **kwargs):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            raise sqlite3.OperationalError("synthetic private lock acquisition failure")
        return super().execute(statement, *args, **kwargs)


@contextmanager
def _no_wait_coordination(raw_root):
    """Only change the coordinator connection, preserving the store database."""
    original = sqlite3.connect
    lock_path = (Path(raw_root) / LOCK_NAME).resolve()

    def connect(database, *args, **kwargs):
        if Path(database).resolve() == lock_path:
            kwargs["factory"] = _NoWaitConnection
        return original(database, *args, **kwargs)

    with patch.object(capture.sqlite3, "connect", side_effect=connect):
        yield


def _process_hold_lock(db, raw_root, channel):
    try:
        with capture.TradeCaptureStore(db, raw_root=raw_root) as store:
            with store._raw_root_lock():
                channel.send("held")
                if channel.recv() != "release":
                    raise AssertionError("holder did not receive release")
            channel.send("released")
    except BaseException as error:
        channel.send(("error", repr(error)))
    finally:
        channel.close()


def _process_contend(db, raw_root, channel):
    try:
        with capture.TradeCaptureStore(db, raw_root=raw_root) as store:
            channel.send("ready")
            if channel.recv() != "try":
                raise AssertionError("contender did not receive try")
            try:
                with _no_wait_coordination(raw_root), store._raw_root_lock():
                    channel.send("unexpected acquisition")
            except contract.TradeContractError:
                channel.send("blocked")
            if channel.recv() != "acquire":
                raise AssertionError("contender did not receive acquire")
            with store._raw_root_lock():
                channel.send("held")
                if channel.recv() != "release":
                    raise AssertionError("contender did not receive release")
            channel.send("released")
    except BaseException as error:
        channel.send(("error", repr(error)))
    finally:
        channel.close()


def _process_probe_lock(lock_path, channel):
    """An independent process observes the native operating-system lock."""
    try:
        with closing(sqlite3.connect(lock_path, timeout=0)) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
                channel.send("blocked")
            else:
                connection.rollback()
                channel.send("available")
    except BaseException as error:
        channel.send(("error", repr(error)))
    finally:
        channel.close()


def _process_crash_after_publication(db, raw_root, source, channel):
    """Exit with a published hard link and an uncommitted capture body."""
    from test_bandarmolony_trade_capture import envelope

    original_unlink = os.unlink

    def crash_on_pending_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(".pending-"):
            channel.send("published")
            os._exit(73)
        return original_unlink(path, *args, **kwargs)

    try:
        with capture.TradeCaptureStore(db, raw_root=raw_root) as store:
            with patch.object(capture.os, "unlink", side_effect=crash_on_pending_unlink):
                store.ingest(source, envelope("crashed-publisher"))
        channel.send("unexpected completion")
    except BaseException as error:
        channel.send(("error", repr(error)))
    finally:
        channel.close()


def _inherited_attempts(store, capture_id, source):
    """Try every operation on a store owned by another process."""
    from test_bandarmolony_trade_capture import observation

    def lock():
        with store._raw_root_lock():
            pass

    attempts = {
        "raw-root lock": lock,
        "connection": lambda: store.conn,
        "ingest": lambda: store.ingest(source, observation("inherited-ingest", 50)),
        "absence": lambda: store.observe_absence(observation("inherited-absence", 51, http_status=404)),
        "resume": lambda: store.resume(capture_id),
        "verify": lambda: store.verify(capture_id),
        "read rows": lambda: store.read_rows(capture_id),
        "inspect": lambda: store.inspect(capture_id),
        "enter": store.__enter__,
        "close": store.close,
    }
    outcomes = {}
    for name, attempt in attempts.items():
        try:
            attempt()
        except contract.TradeContractError as error:
            outcomes[name] = str(error)
        except Exception as error:
            outcomes[name] = "UNEXPECTED " + type(error).__name__
        else:
            outcomes[name] = "ALLOWED"
    return outcomes


def _await(channel, message):
    """Synchronize a forked child without unittest assertions.

    The parent may spawn a probe interpreter before answering, so wait longer
    than any single parent-side step.
    """
    if not channel.poll(60) or channel.recv() != message:
        raise RuntimeError("forked child did not receive " + message)


def _fork_inherited_child(stores, capture_id, source, fresh_db, raw_root, creator_pid, channel):
    """Runs in a real fork child: inherited stores refuse, a fresh store does not."""
    # The forking thread's nested-lock flag is copied into the child.
    held_flag = getattr(stores["writer"]._raw_coordination, "held", False)
    inherited = {role: _inherited_attempts(store, capture_id, source)
                 for role, store in stores.items()}
    # A later descendant can reuse the creator's PID; ownership still refuses.
    with patch.object(capture.os, "getpid", return_value=creator_pid):
        reused_pid = {role: _inherited_attempts(store, capture_id, source)
                      for role, store in stores.items()}
    with capture.TradeCaptureStore(fresh_db, raw_root=raw_root) as fresh:
        try:
            with _no_wait_coordination(raw_root), fresh._raw_root_lock():
                fresh_lock = "acquired"
        except contract.TradeContractError:
            fresh_lock = "blocked"
    channel.send({"held flag": held_flag, "inherited": inherited, "reused pid": reused_pid,
                  "fresh lock": fresh_lock})


def _fork_fresh_store_child(db, raw_root, source, channel):
    """Runs in a real fork child: a newly constructed store owns real coordination."""
    from test_bandarmolony_trade_capture import observation

    with capture.TradeCaptureStore(db, raw_root=raw_root) as fresh:
        channel.send("ready")
        _await(channel, "try")
        try:
            with _no_wait_coordination(raw_root), fresh._raw_root_lock():
                channel.send("unexpected acquisition")
        except contract.TradeContractError:
            channel.send("blocked")
        _await(channel, "acquire")
        with fresh._raw_root_lock():
            with fresh._raw_root_lock():
                channel.send("held")
                _await(channel, "release")
        accepted = fresh.ingest(source, observation("fresh-child", 1))
        channel.send(("accepted", accepted["capture_id"], fresh.verify(accepted["capture_id"]) == accepted))


class RawRootLockTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        from test_bandarmolony_trade_capture import make_tree_writable

        self.tmp = tempfile.TemporaryDirectory(prefix="trade-root-lock-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(make_tree_writable, self.root)
        self.db = self.root / "private" / "primary.db"
        self.raw = self.root / "private" / "raw"
        self.lock_path = self.raw / LOCK_NAME
        self.store = capture.TradeCaptureStore(self.db, raw_root=self.raw)
        self.addCleanup(self.store.close)
        self.context = multiprocessing.get_context("spawn")

    def source(self):
        from test_bandarmolony_trade_capture import write_parquet

        return write_parquet(self.root / "synthetic-input.parquet")

    def receive(self, channel):
        self.assertTrue(channel.poll(15), "process synchronization timed out")
        return channel.recv()

    @staticmethod
    def stop_process(process, channel):
        if process.is_alive():
            process.terminate()
        process.join(10)
        channel.close()

    def start_process(self, target, *args):
        parent_channel, child_channel = self.context.Pipe()
        process = self.context.Process(target=target, args=(*args, child_channel))
        process.start()
        child_channel.close()
        self.addCleanup(self.stop_process, process, parent_channel)
        return process, parent_channel

    def finished(self, process, expected_exit=0):
        process.join(15)
        self.assertFalse(process.is_alive(), "child did not finish")
        self.assertEqual(process.exitcode, expected_exit)

    def assert_native_lock(self, expected):
        process, channel = self.start_process(_process_probe_lock, self.lock_path)
        self.assertEqual(self.receive(channel), expected)
        self.finished(process)

    def assert_no_evidence(self):
        for table in ("trade_captures", "trade_acceptances"):
            self.assertEqual(self.store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(list(self.raw.rglob("*.parquet")), [])
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])
        self.assertFalse(self.store.conn.in_transaction)

    def test_root_coordination_serializes_independent_processes(self):
        holder, holder_channel = self.start_process(
            _process_hold_lock, self.root / "private" / "holder.db", self.raw)
        self.assertEqual(self.receive(holder_channel), "held")
        contender, contender_channel = self.start_process(
            _process_contend, self.root / "private" / "contender.db", self.raw)
        self.assertEqual(self.receive(contender_channel), "ready")
        contender_channel.send("try")
        self.assertEqual(self.receive(contender_channel), "blocked")
        self.assert_native_lock("blocked")
        holder_channel.send("release")
        self.assertEqual(self.receive(holder_channel), "released")
        self.finished(holder)
        contender_channel.send("acquire")
        self.assertEqual(self.receive(contender_channel), "held")
        self.assert_native_lock("blocked")
        contender_channel.send("release")
        self.assertEqual(self.receive(contender_channel), "released")
        self.finished(contender)
        self.assert_native_lock("available")

    def test_terminated_holder_releases_native_lock(self):
        holder, channel = self.start_process(
            _process_hold_lock, self.root / "private" / "terminated.db", self.raw)
        self.assertEqual(self.receive(channel), "held")
        self.assert_native_lock("blocked")
        holder.terminate()
        holder.join(15)
        self.assertFalse(holder.is_alive(), "terminated holder did not finish")
        self.assertNotEqual(holder.exitcode, 0)
        self.assert_native_lock("available")
        with self.store._raw_root_lock():
            self.assert_native_lock("blocked")

    def test_crashed_publisher_releases_lock_and_recovery_repairs_alias(self):
        from test_bandarmolony_trade_capture import envelope

        source = self.source()
        data = source.read_bytes()
        digest = contract.sha256_bytes(data)
        destination = self.store.raw_path(digest)
        crash_db = self.root / "private" / "crashed.db"
        publisher, channel = self.start_process(
            _process_crash_after_publication, crash_db, self.raw, source)
        self.assertEqual(self.receive(channel), "published")
        self.finished(publisher, 73)
        self.assertEqual(destination.read_bytes(), data)
        aliases = list(destination.parent.glob(".pending-*"))
        self.assertEqual(len(aliases), 1)
        self.assertTrue(aliases[0].samefile(destination))
        self.assert_native_lock("available")
        with capture.TradeCaptureStore(crash_db, raw_root=self.raw) as recovered:
            self.assertEqual(recovered.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)
            accepted = recovered.ingest(source, envelope("recovered-publisher"))
            self.assertEqual(accepted["raw_response_sha256"], digest)
            self.assertEqual(destination.read_bytes(), data)
            self.assertTrue(recovered.verify(accepted["capture_id"]))
        self.assertEqual(list(destination.parent.glob(".pending-*")), [])
        self.assertEqual(destination.stat().st_mode & 0o222, 0)

    def test_coordination_open_and_begin_fail_closed_before_publication(self):
        from test_bandarmolony_trade_capture import envelope

        source = self.source()
        original_connect = sqlite3.connect
        for failure in ("open", "begin"):
            with self.subTest(failure=failure):
                def connect(database, *args, **kwargs):
                    if Path(database).resolve() == self.lock_path.resolve():
                        if failure == "open":
                            raise sqlite3.OperationalError("synthetic private lock opening failure")
                        kwargs["factory"] = _FailBeginConnection
                    return original_connect(database, *args, **kwargs)

                with patch.object(capture.sqlite3, "connect", side_effect=connect):
                    with patch.object(self.store, "_preserve_raw") as preserve:
                        with self.assertRaises(contract.TradeContractError) as rejected:
                            self.store.ingest(source, envelope("failed-coordination"))
                        preserve.assert_not_called()
                self.assertEqual(str(rejected.exception), "cannot establish shared raw-root coordination")
                self.assertTrue(rejected.exception.__suppress_context__)
                self.assertNotIn("synthetic private lock", str(rejected.exception))
                self.assert_no_evidence()

    def test_coordinator_is_private_and_not_capture_evidence(self):
        from test_bandarmolony_trade_capture import envelope

        accepted = self.store.ingest(self.source(), envelope("private-coordination"))
        self.assertTrue(self.lock_path.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.lock_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.raw.stat().st_mode), 0o700)
        with closing(sqlite3.connect(self.lock_path.as_uri() + "?mode=ro", uri=True)) as coordinator:
            self.assertEqual(coordinator.execute("SELECT name FROM sqlite_master").fetchall(), [])
        self.assertEqual(list(self.raw.rglob("*.parquet")),
                         [self.store.raw_path(accepted["raw_response_sha256"])])
        self.assertNotIn(LOCK_NAME, contract.canonical_json(self.store.inspect(accepted["capture_id"])))
        self.assertFalse(any(Path(str(self.lock_path) + suffix).exists() for suffix in capture.SIDECARS))

    def test_read_only_verification_does_not_create_coordinator(self):
        from test_bandarmolony_trade_capture import envelope

        accepted = self.store.ingest(self.source(), envelope("read-only-coordination"))
        self.store.close()
        self.lock_path.unlink()
        before = {path.relative_to(self.raw): path.read_bytes()
                  for path in self.raw.rglob("*") if path.is_file()}
        with capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True) as reader:
            self.assertTrue(reader.verify(accepted["capture_id"]))
            self.assertTrue(reader.inspect(accepted["capture_id"]))
            self.assertEqual(len(reader.read_rows(accepted["capture_id"])), 3)
        after = {path.relative_to(self.raw): path.read_bytes()
                 for path in self.raw.rglob("*") if path.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(self.lock_path.exists())

    def test_coordinator_and_sidecars_pass_private_output_guard(self):
        from test_bandarmolony_trade_capture import envelope

        source = self.source()
        original_guard = capture.check_private_output
        checked = []

        def refuse_coordinator(path, sidecars=()):
            if Path(path).resolve() == self.lock_path.resolve():
                checked.append(tuple(sidecars))
                raise contract.TradeContractError("synthetic coordination guard refusal")
            return original_guard(path, sidecars)

        with patch.object(capture, "check_private_output", side_effect=refuse_coordinator):
            with self.assertRaises(contract.TradeContractError):
                self.store.ingest(source, envelope("guarded-coordination"))
        self.assertTrue(checked)
        self.assertTrue(all(sidecars == capture.SIDECARS for sidecars in checked))
        self.assert_no_evidence()

    def test_nested_same_store_retains_outer_native_lock(self):
        from test_bandarmolony_trade_capture import envelope

        with self.store._raw_root_lock():
            with self.store._raw_root_lock():
                accepted = self.store.ingest(self.source(), envelope("nested-coordination"))
                self.assertTrue(self.store.verify(accepted["capture_id"]))
            self.assert_native_lock("blocked")
        self.assert_native_lock("available")

    def test_independent_same_thread_stores_do_not_inherit_ownership(self):
        with capture.TradeCaptureStore(self.root / "private" / "independent.db", raw_root=self.raw) as other:
            with self.store._raw_root_lock():
                with _no_wait_coordination(self.raw):
                    with self.assertRaises(contract.TradeContractError):
                        with other._raw_root_lock():
                            self.fail("independent store inherited another store's ownership")
                self.assert_native_lock("blocked")
            with other._raw_root_lock():
                self.assert_native_lock("blocked")
        self.assert_native_lock("available")

    def evidence_snapshot(self):
        """Exact bytes of the parent's database, sidecars and raw evidence.

        Never open the coordination file or its sidecars: on POSIX, closing any
        descriptor to that inode releases this process's native SQLite lock.
        """
        paths = [Path(str(self.db) + suffix) for suffix in ("", *capture.SIDECARS)]
        paths += [path for path in self.raw.rglob("*")
                  if path.is_file() and not path.name.startswith(LOCK_NAME)]
        return {str(path): path.read_bytes() for path in sorted(paths) if path.exists()}

    def fork(self, target, *args):
        """Run target(*args, channel) in a real os.fork() child, never returning there."""
        parent_channel, child_channel = multiprocessing.Pipe()
        pid = os.fork()
        if pid == 0:
            status = 70
            try:
                parent_channel.close()
                target(*args, child_channel)
                status = 0
            except BaseException as error:
                try:
                    child_channel.send(("error", repr(error)))
                except BaseException:
                    pass
            finally:
                # Skip inherited unittest cleanups, atexit handlers and finalizers.
                os._exit(status)
        child_channel.close()
        self.addCleanup(self.reap_fork, pid, parent_channel)
        return pid, parent_channel

    def join_fork(self, pid):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                return os.waitstatus_to_exitcode(status)
            time.sleep(0.01)
        self.fail("forked child did not finish")

    @staticmethod
    def reap_fork(pid, channel):
        channel.close()
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    def test_store_refuses_another_process_identity(self):
        """Portable guard check; the real os.fork() tests below run on POSIX."""
        from test_bandarmolony_trade_capture import observation

        source = self.source()
        accepted = self.store.ingest(source, observation("owner-capture", 0))
        reader = capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True)
        self.addCleanup(reader.close)
        expected = dict.fromkeys(INHERITED_OPERATIONS, capture.INHERITED_STORE_REFUSAL)
        before = self.evidence_snapshot()
        other_pid = os.getpid() + 1
        guard = capture.check_private_output
        checked = []
        with patch.object(capture, "check_private_output",
                          side_effect=lambda *args, **kwargs: checked.append(args) or guard(*args, **kwargs)):
            # Another process, or a later descendant that reuses the creator PID.
            for identity in (patch.object(capture.os, "getpid", return_value=other_pid),
                             patch.object(capture, "_fork_generation", capture._fork_generation + 1)):
                with identity:
                    idle = {role: _inherited_attempts(store, accepted["capture_id"], source)
                            for role, store in (("writer", self.store), ("reader", reader))}
                self.assertEqual(idle, {"writer": expected, "reader": expected})
        # Refusal precedes every private-output Git inspection and file read.
        self.assertEqual(checked, [])
        with self.store._raw_root_lock():
            with patch.object(capture.os, "getpid", return_value=other_pid):
                held = _inherited_attempts(self.store, accepted["capture_id"], source)
            self.assertEqual(held, expected)
            self.assert_native_lock("blocked")
        self.assert_native_lock("available")
        self.assertEqual(self.evidence_snapshot(), before)
        self.assertEqual(self.store.verify(accepted["capture_id"]), accepted)
        self.assertEqual(reader.verify(accepted["capture_id"]), accepted)
        later = self.store.observe_absence(observation("owner-after", 1, http_status=404))
        self.assertEqual(later["previous_observation_id"], accepted["capture_id"])

    def release_inherited_handles(self):
        while capture._inherited_handles:
            capture._inherited_handles.pop().close()

    def test_inherited_cleanup_keeps_parent_coordination_and_publication(self):
        """Frames active at fork never close parent handles or delete raw objects."""
        from test_bandarmolony_trade_capture import observation

        self.addCleanup(self.release_inherited_handles)
        other_pid = os.getpid() + 1
        # A normal exit from an inherited lock context is refused; an exit by
        # exception, such as the child's sys.exit(), keeps that exception.
        for error in (None, SystemExit(0)):
            with self.subTest(error=error):
                context = self.store._raw_root_lock()
                context.__enter__()
                with patch.object(capture.os, "getpid", return_value=other_pid):
                    if error is None:
                        with self.assertRaises(contract.TradeContractError) as refused:
                            context.__exit__(None, None, None)
                        self.assertEqual(str(refused.exception), capture.INHERITED_STORE_REFUSAL)
                    else:
                        self.assertFalse(context.__exit__(SystemExit, error, None))
                        self.assertFalse(self.store.__exit__(SystemExit, error, None))
                # The parent's coordination connection stayed open and still locks.
                self.assertEqual(len(capture._inherited_handles), 1)
                self.assert_native_lock("blocked")
                self.release_inherited_handles()
                self.assert_native_lock("available")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)

        source = self.source()
        data = source.read_bytes()
        destination = self.store.raw_path(contract.sha256_bytes(data))
        identity = patch.object(capture.os, "getpid", return_value=other_pid)
        link = os.link

        def child_exits_after_link(source_path, target, *args, **kwargs):
            # A signal handler forks right after publication; the child calls sys.exit().
            link(source_path, target, *args, **kwargs)
            if Path(target) == destination:
                identity.start()
                raise SystemExit(0)

        try:
            with patch.object(capture.os, "link", side_effect=child_exits_after_link):
                with self.assertRaises(SystemExit):
                    self.store.ingest(source, observation("unwound-child", 0))
        finally:
            identity.stop()
        # The parent's pending alias and published object both survive.
        self.assertEqual(destination.read_bytes(), data)
        aliases = list(destination.parent.glob(".pending-*"))
        self.assertEqual(len(aliases), 1)
        self.assertTrue(aliases[0].samefile(destination))
        # Only the simulation shares the owner's frames: release them as the owner.
        self.assertTrue(self.store.conn.in_transaction)
        self.store.conn.rollback()
        self.release_inherited_handles()
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 0)
        accepted = self.store.ingest(source, observation("owner-after-unwind", 1))
        self.assertEqual(self.store.raw_path(accepted["raw_response_sha256"]), destination)
        self.assertEqual(self.store.verify(accepted["capture_id"]), accepted)
        self.assertEqual(destination.stat().st_mode & 0o222, 0)
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])
        self.assert_native_lock("available")

    @real_fork
    def test_fork_while_lock_held_refuses_inherited_store(self):
        from test_bandarmolony_trade_capture import observation

        source = self.source()
        accepted = self.store.ingest(source, observation("parent-before-fork", 0))
        reader = capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True)
        self.addCleanup(reader.close)
        expected = dict.fromkeys(INHERITED_OPERATIONS, capture.INHERITED_STORE_REFUSAL)
        with self.store._raw_root_lock():
            held = self.evidence_snapshot()
            pid, channel = self.fork(
                _fork_inherited_child, {"writer": self.store, "reader": reader},
                accepted["capture_id"], source, self.root / "private" / "fresh-child.db",
                self.raw, os.getpid())
            self.assertEqual(self.receive(channel), {
                # Pre-fix, this inherited flag let nested acquisition skip the lock.
                "held flag": True,
                "inherited": {"writer": expected, "reader": expected},
                "reused pid": {"writer": expected, "reader": expected},
                # A fresh child store has its own flag, so it never inherits
                # ownership. The parent's lock and SQLite's inherited in-process
                # bookkeeping both refuse it here.
                "fresh lock": "blocked",
            })
            self.assertEqual(self.join_fork(pid), 0)
            # The child changed nothing and the parent still owns the native lock.
            self.assertEqual(self.evidence_snapshot(), held)
            self.assert_native_lock("blocked")
            with self.store._raw_root_lock():
                self.assertEqual(self.store.verify(accepted["capture_id"]), accepted)
            self.assert_native_lock("blocked")
        self.assert_native_lock("available")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM trade_captures").fetchone()[0], 1)
        self.assertEqual(reader.verify(accepted["capture_id"]), accepted)
        later = self.store.observe_absence(observation("parent-after-fork", 1, http_status=404))
        self.assertEqual(later["previous_observation_id"], accepted["capture_id"])

    @real_fork
    def test_fork_while_idle_refuses_inherited_store(self):
        from test_bandarmolony_trade_capture import observation

        source = self.source()
        accepted = self.store.ingest(source, observation("parent-before-fork", 0))
        reader = capture.TradeCaptureStore(self.db, raw_root=self.raw, read_only=True)
        self.addCleanup(reader.close)
        expected = dict.fromkeys(INHERITED_OPERATIONS, capture.INHERITED_STORE_REFUSAL)
        before = self.evidence_snapshot()
        pid, channel = self.fork(
            _fork_inherited_child, {"writer": self.store, "reader": reader},
            accepted["capture_id"], source, self.root / "private" / "fresh-child.db",
            self.raw, os.getpid())
        self.assertEqual(self.receive(channel), {
            "held flag": False,
            "inherited": {"writer": expected, "reader": expected},
            "reused pid": {"writer": expected, "reader": expected},
            "fresh lock": "acquired",
        })
        self.assertEqual(self.join_fork(pid), 0)
        self.assertEqual(self.evidence_snapshot(), before)
        self.assert_native_lock("available")
        self.assertEqual(self.store.verify(accepted["capture_id"]), accepted)
        self.assertEqual(reader.verify(accepted["capture_id"]), accepted)
        with self.store._raw_root_lock():
            with self.store._raw_root_lock():
                later = self.store.observe_absence(observation("parent-after-fork", 1, http_status=404))
            self.assert_native_lock("blocked")
        self.assertEqual(later["previous_observation_id"], accepted["capture_id"])
        self.assert_native_lock("available")

    @real_fork
    def test_fresh_store_in_forked_child_acquires_real_coordination(self):
        from test_bandarmolony_trade_capture import observation

        absent = self.store.observe_absence(observation("parent-before-fork", 0, http_status=404))
        source = self.source()
        pid, channel = self.fork(_fork_fresh_store_child, self.db, self.raw, source)
        self.assertEqual(self.receive(channel), "ready")
        with self.store._raw_root_lock():
            channel.send("try")
            self.assertEqual(self.receive(channel), "blocked")
        channel.send("acquire")
        self.assertEqual(self.receive(channel), "held")
        self.assert_native_lock("blocked")
        with _no_wait_coordination(self.raw):
            with self.assertRaises(contract.TradeContractError):
                with self.store._raw_root_lock():
                    self.fail("parent acquired coordination held by the forked child")
        channel.send("release")
        self.assertEqual(self.receive(channel), ("accepted", "fresh-child", True))
        self.assertEqual(self.join_fork(pid), 0)
        self.assert_native_lock("available")
        accepted = self.store.verify("fresh-child")
        self.assertEqual(accepted["previous_observation_id"], absent["capture_id"])
        self.assertEqual(accepted["observation_state"], "CONTENT_FIRST_SEEN")
        destination = self.store.raw_path(accepted["raw_response_sha256"])
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        self.assertEqual(destination.stat().st_mode & 0o222, 0)
        self.assertEqual(list(self.raw.rglob(".pending-*")), [])


if __name__ == "__main__":
    unittest.main()
