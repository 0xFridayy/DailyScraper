"""Experiment #1F Phase 2: the candidate data contract.

Correctness > coverage. Provenance > convenience. Explicit quarantine >
speculative repair.

This module builds CANDIDATE artifacts only. Nothing it writes is an approved
Gate-A input, and it establishes no manifest. Every artifact lands under an
isolated candidate directory that can never be confused with the production
paths Gate A reads.

Layering (unchanged from the merged PR #38-#40 architecture)
------------------------------------------------------------
    RAW          inventory_raw/*.json.gz     immutable, shared, MUTABLE BY OTHERS
    SOURCE SNAP  <candidate>/source_raw/     experiment-owned byte copy  <- new
    INGEST       build_inventory_db.py       faithful transcription, int64 lots
    NORMALIZED   normalize_market_data.py    provenance artifacts (JSON)
    DERIVED      panel construction          applies reviewed artifacts

Why the source snapshot exists
------------------------------
The dedicated git worktree isolates code but NOT gitignored data: inventory_raw/
lives only in the shared checkout, where any other process can rewrite a ticker
in place. The cache is demonstrably not a homogeneous snapshot -- KIOS was
refreshed 2026-09-03, twelve days after the rest -- so "the raw cache" is not a
stable identity. Phase 2 therefore takes an experiment-owned byte-for-byte copy
of exactly the covered approved universe and reads only that copy afterwards.

Copies are independent files, never hardlinks or symlinks: a hardlink would
still let an in-place write in the shared cache alter the experiment.
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))

#: Approved universe, frozen upstream. 298 names, digest 7a4ce50ffb5a7ebe.
UNIVERSE_PATH = os.path.join(HERE, "experiment_1f_universe.json")
UNIVERSE_DIGEST = "7a4ce50ffb5a7ebe"
UNIVERSE_SIZE = 298

#: WIKA is an approved universe name whose raw file is an 83-byte empty shell:
#: zero OHLC rows, zero sessions, zero brokers. It is excluded from the semantic
#: source population by evidence, not by name -- see covered_tickers(), which
#: derives the exclusion and only then asserts it matches this expectation.
UNCOVERED = ("WIKA",)
COVERED_SIZE = UNIVERSE_SIZE - len(UNCOVERED)

CANDIDATE_DIR = os.path.join(HERE, "backtest_out", "experiment_1f_candidate")
SOURCE_RAW = "source_raw"
SOURCE_MANIFEST = "source_manifest.json"

#: Phase-2 baseline expectations for the two frozen artifacts. broker_daily was
#: re-baselined on 2026-09-07 to the post-PR-#38 int64 build; see
#: SUPERSEDED_BROKER_DAILY for the lossy predecessor and the proof.
FROZEN_OHLC_SHA256 = "687127ba5531db3c2b7390c16b7d7603a3986b4b361ed3f14fc63685e5c85b52"
FROZEN_OHLC_SIZE = 4299256
FROZEN_BROKER_SHA256 = "c8d1948f00d99ba96fe17376292f32a9cda2be36e2eb5ce303e680427f05cc32"
FROZEN_BROKER_SIZE = 132881368

#: The pre-PR-#38 float32 build, retained as provenance only. Reproducing it
#: bit-for-bit from the current int64 artifact -- by casting nlot/blot/slot to
#: float32 and changing nothing else -- is what proves the supersession involved
#: no row, ordering or non-lot data drift. It is NOT an accepted #1F input.
SUPERSEDED_BROKER_DAILY = {
    "sha256": "b600ee9dd0c1f8e7f3199b6e7410d0bd2e3bc56b06129cf2110b76fab10c636c",
    "size_bytes": 134574128,
    "classification": "SUPERSEDED PRE-PR38 LOSSY REFERENCE",
    "reason": (
        "Lot columns were pa.float32() before PR #38 (5f521cb) introduced "
        "pa.int64(). float32 holds integers exactly only to 2**24, and the "
        "cache carries lots up to 80,883,692; a safe cast of the current "
        "artifact refuses on 17,168,499. The superseded build therefore stored "
        "provably corrupted lot values."
    ),
    "supersession_proof": (
        "Casting nlot/blot/slot of the current int64 broker_daily.parquet to "
        "float32, preserving row groups, order and every other column, "
        "reproduces sha256 b600ee9d... at exactly 134,574,128 bytes. Identical "
        "bytes prove the two artifacts differ only in lot representation."
    ),
}


class CandidateError(ValueError):
    """A source or artifact fact outside the candidate contract. Always fatal."""


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def sha256_file(path):
    """FULL sha256 hex. Semantic provenance never carries a truncated hash."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def aggregate_digest(pairs):
    """Deterministic aggregate over sorted (ticker, full_sha256).

    NUL-separated so no ticker/hash concatenation can alias another pair, and
    sorted so filesystem enumeration order cannot leak into the identity.
    """
    h = hashlib.sha256()
    for ticker, digest in sorted(pairs):
        h.update(ticker.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def load_universe(path=UNIVERSE_PATH):
    """The approved 298, validated against its frozen digest."""
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    tickers = loaded["tickers"] if isinstance(loaded, dict) else list(loaded)
    if len(tickers) != len(set(tickers)):
        raise SystemExit("universe contains duplicate tickers")
    if len(tickers) != UNIVERSE_SIZE:
        raise SystemExit(f"universe is {len(tickers)} names, expected {UNIVERSE_SIZE}")
    if isinstance(loaded, dict) and loaded.get("universe_digest") != UNIVERSE_DIGEST:
        raise SystemExit(f"universe digest {loaded.get('universe_digest')} "
                         f"!= expected {UNIVERSE_DIGEST}")
    return sorted(tickers)


def raw_path(source_dir, ticker):
    return os.path.join(source_dir, ticker + ".json.gz")


def read_raw(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def coverage(data):
    """Evidence of usable coverage, kept separate from any ticker name."""
    brokers = {b for field in ("nlot", "blot", "slot")
               if isinstance(data.get(field), dict) for b in data[field]}
    return {
        "ohlc_rows": len(data.get("ohlc") or []),
        "sessions": len(data.get("date") or []),
        "brokers": len(brokers),
    }


def covered_tickers(universe, source_dir):
    """Split the universe into covered and uncovered on measured evidence.

    A name is uncovered when its raw file carries no OHLC rows, no sessions and
    no brokers -- an empty shell. The split is derived, then checked against the
    documented expectation, so a second ticker going empty is a hard failure
    rather than a silently smaller population.
    """
    covered, uncovered = [], {}
    for ticker in universe:
        path = raw_path(source_dir, ticker)
        if not os.path.isfile(path):
            raise SystemExit(f"{ticker}: no raw source at {path}")
        try:
            data = read_raw(path)
        except Exception as exc:
            raise SystemExit(f"{ticker}: unreadable raw source ({exc})")
        cov = coverage(data)
        if cov["ohlc_rows"] == 0 and cov["sessions"] == 0 and cov["brokers"] == 0:
            uncovered[ticker] = cov
        else:
            covered.append(ticker)

    if sorted(uncovered) != sorted(UNCOVERED):
        raise SystemExit(
            f"uncovered set {sorted(uncovered)} != expected {sorted(UNCOVERED)}; "
            "the semantic source population changed and must be re-reviewed")
    if len(covered) != COVERED_SIZE:
        raise SystemExit(f"covered population is {len(covered)}, expected {COVERED_SIZE}")
    return covered, uncovered


# --------------------------------------------------------------------------
# source snapshot
# --------------------------------------------------------------------------

def fingerprint_all(source_dir, tickers):
    return {t: (sha256_file(raw_path(source_dir, t)),
                os.path.getsize(raw_path(source_dir, t))) for t in tickers}


def write_json(path, payload):
    """Temp + atomic rename, so a crashed build never leaves a partial artifact."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def snapshot_sources(source_dir, candidate_dir, universe_path=UNIVERSE_PATH):
    """Byte-for-byte experiment-owned copy of the covered source population.

    Fingerprints before the copy, copies, fingerprints again, and requires the
    aggregate digest to be identical on both sides. Any mismatch is fatal: a
    snapshot that does not reproduce its source is not a snapshot.
    """
    universe = load_universe(universe_path)
    covered, uncovered = covered_tickers(universe, source_dir)

    before = fingerprint_all(source_dir, covered)
    digest_before = aggregate_digest((t, s) for t, (s, _) in before.items())

    dest = os.path.join(candidate_dir, SOURCE_RAW)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    snapshot_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for ticker in covered:
        # copyfile, never link(): a hardlink would let an in-place write in the
        # shared cache mutate the experiment's own copy.
        shutil.copyfile(raw_path(source_dir, ticker), raw_path(dest, ticker))

    after = fingerprint_all(dest, covered)
    for ticker in covered:
        if after[ticker] != before[ticker]:
            raise SystemExit(f"{ticker}: snapshot copy does not match source")
    digest_after = aggregate_digest((t, s) for t, (s, _) in after.items())
    if digest_after != digest_before:
        raise SystemExit("snapshot aggregate digest does not match source")

    present = sorted(os.path.basename(p)[:-8]
                     for p in glob.glob(os.path.join(dest, "*.json.gz")))
    if present != sorted(covered):
        extra = sorted(set(present) - set(covered))
        missing = sorted(set(covered) - set(present))
        raise SystemExit(f"snapshot population wrong: extra={extra} missing={missing}")
    if len(present) != COVERED_SIZE:
        raise SystemExit(f"snapshot holds {len(present)} files, expected {COVERED_SIZE}")

    manifest = {
        "experiment": "1F",
        "phase": 2,
        "status": "CANDIDATE -- not an approved Gate-A input, no manifest established",
        "universe": {
            "path": os.path.basename(universe_path),
            "n_tickers": len(universe),
            "universe_digest": UNIVERSE_DIGEST,
        },
        "population": {
            "covered": covered,
            "n_covered": len(covered),
            "uncovered": {t: {"coverage": c, "reason":
                              "raw file present but carries no OHLC rows, no "
                              "sessions and no brokers -- an empty shell"}
                          for t, c in uncovered.items()},
            "n_uncovered": len(uncovered),
        },
        "source_aggregate_sha256": digest_after,
        "snapshot_utc": snapshot_utc,
        "snapshot_source_dir": os.path.abspath(source_dir),
        "time_semantics": {
            "filesystem_mtime": "filesystem metadata only -- NOT market "
                                "observability time and NOT a capture time",
            "capture_timestamp": "absent: the vendor payload carries no capture "
                                 "or fetch timestamp field",
            "snapshot_copy_utc": "when this experiment took its byte copy",
        },
        "files": [
            {
                "ticker": t,
                "sha256": after[t][0],
                "size_bytes": after[t][1],
                "filesystem_mtime_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ",
                    time.gmtime(os.path.getmtime(raw_path(source_dir, t)))),
                "capture_timestamp": None,
            }
            for t in covered
        ],
    }
    path = os.path.join(candidate_dir, SOURCE_MANIFEST)
    write_json(path, manifest)
    return manifest, path


CANDIDATE_OHLC = "ohlc_full_market.parquet"


def snapshot_full_market_ohlc(source_path, candidate_dir,
                              expect_sha256=FROZEN_OHLC_SHA256,
                              expect_size=FROZEN_OHLC_SIZE):
    """Byte-for-byte experiment-owned copy of the FULL-MARKET price harvest.

    Deliberately NOT narrowed to the 297. detect()'s cross_ticker_dup compares
    OHLCV across tickers, so an approved name cloned against an out-of-universe
    name looks unique the moment the other half is filtered away. The isolation
    being bought here is physical, not semantic: the file stops living in a
    shared checkout that other sessions can rewrite, while its contents stay
    exactly the full cross-section the detector needs.

    Copied, never linked. A hardlink would leave the experiment reading bytes
    the shared checkout can still replace in place, which is the dependency this
    copy exists to remove.
    """
    before = sha256_file(source_path)
    size = os.path.getsize(source_path)
    if before != expect_sha256 or size != expect_size:
        raise SystemExit(
            f"refusing to snapshot {source_path}: sha256 {before} size {size}, "
            f"expected {expect_sha256} size {expect_size}")

    os.makedirs(candidate_dir, exist_ok=True)
    dest = os.path.join(candidate_dir, CANDIDATE_OHLC)
    tmp = dest + ".tmp"
    shutil.copyfile(source_path, tmp)
    os.replace(tmp, dest)

    after = sha256_file(dest)
    if after != before or os.path.getsize(dest) != size:
        raise SystemExit("full-market OHLC snapshot does not match its source")
    if os.stat(dest).st_ino == os.stat(source_path).st_ino:
        raise SystemExit("snapshot shares an inode with its source; not an "
                         "independent copy")
    return {"path": dest, "sha256": after, "size_bytes": size,
            "independent_copy": True}


def verify_full_market_ohlc(candidate_dir, expect_sha256=FROZEN_OHLC_SHA256):
    path = os.path.join(candidate_dir, CANDIDATE_OHLC)
    if not os.path.isfile(path):
        raise SystemExit(f"missing experiment-owned OHLC snapshot at {path}")
    actual = sha256_file(path)
    if actual != expect_sha256:
        raise SystemExit(f"OHLC snapshot drifted: {actual} != {expect_sha256}")
    return actual


def manifest_tickers(candidate_dir):
    """The covered population, read back from the snapshot manifest."""
    with open(os.path.join(candidate_dir, SOURCE_MANIFEST), encoding="utf-8") as fh:
        manifest = json.load(fh)
    return manifest["population"]["covered"], manifest


def verify_snapshot(candidate_dir):
    """Re-fingerprint the snapshot and confirm it still matches its manifest."""
    covered, manifest = manifest_tickers(candidate_dir)
    dest = os.path.join(candidate_dir, SOURCE_RAW)
    recorded = {f["ticker"]: (f["sha256"], f["size_bytes"]) for f in manifest["files"]}
    actual = fingerprint_all(dest, covered)
    drifted = sorted(t for t in covered if actual[t] != recorded[t])
    if drifted:
        raise SystemExit(f"snapshot drifted since manifest: {drifted}")
    digest = aggregate_digest((t, s) for t, (s, _) in actual.items())
    if digest != manifest["source_aggregate_sha256"]:
        raise SystemExit("snapshot aggregate digest drifted")
    return digest


def ingest(candidate_dir):
    """Strict candidate broker ingest, reading only the experiment-owned snapshot."""
    import build_inventory_db as bidb

    covered, _ = manifest_tickers(candidate_dir)
    verify_snapshot(candidate_dir)
    return bidb.build_candidate_broker(
        os.path.join(candidate_dir, SOURCE_RAW), covered, candidate_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["snapshot", "verify", "ingest",
                                        "snapshot-ohlc"])
    ap.add_argument("--source-dir",
                    help="shared inventory_raw to copy FROM (read only); "
                         "required for snapshot")
    ap.add_argument("--candidate-dir", default=CANDIDATE_DIR)
    a = ap.parse_args()

    if a.command == "snapshot":
        if not a.source_dir:
            raise SystemExit("snapshot requires --source-dir")
        manifest, path = snapshot_sources(a.source_dir, a.candidate_dir)
        print(f"covered   {manifest['population']['n_covered']}")
        print(f"uncovered {manifest['population']['n_uncovered']} "
              f"{sorted(manifest['population']['uncovered'])}")
        print(f"aggregate {manifest['source_aggregate_sha256']}")
        print(f"wrote     {path}")
    elif a.command == "snapshot-ohlc":
        if not a.source_dir:
            raise SystemExit("snapshot-ohlc requires --source-dir "
                             "(the directory holding the frozen ohlc.parquet)")
        result = snapshot_full_market_ohlc(
            os.path.join(a.source_dir, "ohlc.parquet"), a.candidate_dir)
        print(f"sha256 {result['sha256']}")
        print(f"size   {result['size_bytes']}")
        print(f"wrote  {result['path']}")
    elif a.command == "verify":
        print(f"aggregate {verify_snapshot(a.candidate_dir)}")
        print(f"ohlc      {verify_full_market_ohlc(a.candidate_dir)}")
    else:
        stats = ingest(a.candidate_dir)
        print(f"tickers            {stats['tickers']}")
        print(f"tickers with rows  {stats['tickers_with_rows']}")
        print(f"rows               {stats['rows']:,}")
        print(f"ohlc values parsed {stats['ohlc_values_checked']:,}")
        print(f"no broker activity {len(stats['empty'])} {stats['empty'][:10]}")
        print(f"wrote              {stats['path']}")


if __name__ == "__main__":
    main()
