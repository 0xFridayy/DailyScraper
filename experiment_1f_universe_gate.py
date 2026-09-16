"""Experiment #1F Gate A — data-integration gate for the approved 298 universe.

WHY THIS FILE EXISTS
--------------------
Experiment #1/#1E ran on the 45-ticker conglomerate watchlist in neobdm.db. The
approved research universe is a different, broader set (298 names). Only 30 of
those 298 have any row in neobdm.db, so #1F cannot source prices from the
production database. The data does exist -- in the ohlc/broker_daily parquet
harvest -- but that harvest has never been through the validation stack that
Experiment #1E spent an entire review cycle hardening.

Running #1F straight off raw parquet would therefore discard the executable
contract. This module is the gate that prevents that: it puts the harvest
through the SAME checks, reusing price_audit's real functions rather than
reimplementing them, and refuses to hand Gate B a panel that has not passed.

THE DETECTOR SEES THE WHOLE MARKET, THE PANEL SEES THE UNIVERSE
---------------------------------------------------------------
cross_ticker_dup is a CROSS-SECTIONAL detector: it finds one stock's OHLCV
copied onto another, which is what a chart that had not re-rendered yet
produces. Subsetting to the approved names before running it would hide exactly
the contamination it exists to catch -- an approved ticker cloned against a
ticker OUTSIDE the universe looks perfectly unique once the other half of the
pair has been filtered away. So detect() runs on the FULL frozen harvest, and
only afterwards are the suspect keys and the model panel narrowed to the
approved names. The trading calendar likewise comes from the full harvest.

FROZEN, NOT REFRESHED
---------------------
The harvest is deliberately NOT re-run before validation. The harvest/backfill
path writes price history with INSERT OR REPLACE across the whole chart range,
so a refresh restates months of history rather than appending to it -- a
point-in-time hazard (today's revision silently replacing what was observable
then) and a determinism hazard (a frozen experiment whose inputs move). The
reviewed snapshot is pinned by a SHA-256 input manifest that must already exist:
a missing manifest is a failure, never an invitation to adopt whatever bytes
happen to be on disk.

WHAT RAISES AND WHAT DOES NOT
-----------------------------
Structural invariants that no downstream code could survive -- duplicate keys,
impossible calendar dates, non-finite prices, cross-ticker contamination
surviving quarantine, a label leaking across a ticker boundary -- RAISE. So do
changed input fingerprints, changed coverage, and a changed universe.

An invalid OPEN anchor does NOT raise. That is a per-row data-quality fact the
executable contract already handles by withholding the label: the row survives,
its fwd_oo_* go NaN. Deleting such rows would be a repair, and this module
repairs nothing.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not train, score, rank, or simulate anything -- that is Gate B. It does
not write to neobdm.db or touch any Experiment #1/#1E artifact. It does not
backfill or fabricate a single price.
"""

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re

import numpy as np
import pandas as pd

from price_audit import (
    OHLCV, add_forward_returns, add_lagged_returns, detect, _open_anchor_valid,
)

HERE = os.path.dirname(os.path.abspath(__file__))

#: The approved universe as supplied by the user. Read-only: this module never
#: writes to it. Once frozen into FROZEN_UNIVERSE_JSON that artifact becomes the
#: reproducible source of record, so #1F does not depend on a Desktop path
#: staying put -- see resolve_universe().
#: The workbook lives OUTSIDE the repo, so its location is machine-local and is
#: read from the environment rather than hardcoded. It is consulted only to
#: CREATE the freeze; once FROZEN_UNIVERSE_JSON exists that artifact is
#: authoritative and the workbook is never reread, so an unset variable is not
#: an error path for any normal run.
UNIVERSE_XLSX = os.environ.get(
    "IDX_UNIVERSE_XLSX",
    os.path.join(os.path.expanduser("~"), "Desktop",
                 "Inventory + Broker Summary ML", "Extract Data dulu",
                 "Stock Universe Set.xlsx"),
)
UNIVERSE_SHEET = "Universe for ML"
UNIVERSE_HEADER = "symbol"
FROZEN_UNIVERSE_JSON = os.path.join(HERE, "experiment_1f_universe.json")
INPUT_MANIFEST_JSON = os.path.join(HERE, "experiment_1f_input_manifest.json")
#: The reviewed manifest-v3, the only manifest the candidate contract verifies
#: against. It does not exist yet and this module never writes it: establishment
#: belongs to experiment_1f_manifest.py, after commit, on a clean semantic tree.
MANIFEST_V3_VERSION = "experiment_1f/3"
REVIEWED_MANIFEST_V3_JSON = os.path.join(
    HERE, "backtest_out", "experiment_1f_candidate", "experiment_1f_manifest_v3.json")

#: The COMPLETE execution-input contract, as (manifest name, candidate_inputs key).
#:
#: One tuple, two consumers. experiment_1f_manifest.build() constructs
#: A_execution_inputs by iterating this, and verify_reviewed_manifest_v3()
#: verifies against it, so the produced manifest and the verified manifest cannot
#: structurally diverge. They already had: the manifest pinned seven inputs while
#: the gate verified three, which meant a REAL established manifest would have
#: failed Gate A on the other four -- and the orchestration test could not see it
#: because its synthetic manifest pinned only the three the gate happened to hash.
#:
#: A pin that is never verified is decoration; an input that is consumed but not
#: pinned is unaudited. Both directions are failures, so the sets must be equal.
#: Two identity rules, because two kinds of file are being pinned.
#:
#: RAW_BYTES     generated, experiment-owned artifacts. Their exact reviewed
#:               bytes ARE the thing under review, they are gitignored, and no
#:               filter ever touches them, so literal sha256 is correct.
#: GIT_CONTENT   tracked repository text. The authoritative identity is the
#:               content Git stores at the pinned commit, NOT the checkout's
#:               newline bytes -- see git_content_sha256(). Worktree binding is
#:               kept by a separate, mandatory cleanliness check.
IDENTITY_RAW_BYTES = "RAW_BYTES"
IDENTITY_GIT_CONTENT = "GIT_CONTENT"
UNAVAILABLE_UNTIL_COMMIT = "UNAVAILABLE_UNTIL_COMMIT"

EXECUTION_INPUTS = (
    ("ohlc_full_market_parquet", "ohlc_parquet", IDENTITY_RAW_BYTES),
    ("broker_daily_v2_parquet", "broker_parquet", IDENTITY_RAW_BYTES),
    # tracked repository text, and an execution input: it must not become
    # machine-bound merely because its JSON whitespace is CRLF rather than LF
    ("universe_json", "universe_json", IDENTITY_GIT_CONTENT),
    ("source_manifest_json", "source_manifest", IDENTITY_RAW_BYTES),
    ("volume_repair_candidates_json", "repair_candidates", IDENTITY_RAW_BYTES),
    ("volume_repair_authorization_json", "repair_authorization", IDENTITY_RAW_BYTES),
    ("observed_basis_factor_candidate_json", "basis_artifact", IDENTITY_RAW_BYTES),
)

OHLC_PARQUET = os.path.join(HERE, "ohlc.parquet")
BROKER_PARQUET = os.path.join(HERE, "broker_daily.parquet")

#: Normalized-layer artifacts, produced by normalize_market_data.py from the
#: immutable raw cache. The gate CONSUMES these; it never derives a repair of
#: its own. A wrap-shaped observation absent from the ledger is a hard failure,
#: not something to fix in place.
VOLUME_REPAIR_LEDGER_JSON = os.path.join(HERE, "volume_repair_ledger.json")
OBSERVED_BASIS_FACTOR_JSON = os.path.join(HERE, "observed_basis_factor.json")

#: Experiment #1F candidate contract (Phase 2 / 2.5). Segmented regimes, and a
#: repair authorisation kept in its own artifact so review is a separate act
#: from detection.
CANDIDATE_DIR = os.path.join(HERE, "backtest_out", "experiment_1f_candidate")
VOLUME_REPAIR_CANDIDATES_JSON = os.path.join(CANDIDATE_DIR,
                                             "volume_repair_candidates.json")
VOLUME_REPAIR_AUTHORIZATION_JSON = os.path.join(CANDIDATE_DIR,
                                                "volume_repair_authorization.json")
OBSERVED_BASIS_FACTOR_CANDIDATE_JSON = os.path.join(
    CANDIDATE_DIR, "observed_basis_factor_candidate.json")

AUTHORIZATION_SCOPE = "EXACT KEY SET ONLY"

#: The shared checkout the experiment copied its frozen inputs FROM. Only ever
#: used to name provenance; nothing at execution time reads it.
SHARED_ROOT = os.environ.get("NEOBDM_SHARED_ROOT",
                             os.path.join(os.path.dirname(HERE), "Claude"))

#: Validity/normalisation mode. PRIMARY is the default in every signature; no
#: argument default may select the retrospective mode implicitly.
#: Named CONSERVATIVE, not SAFE: the policy refuses retrospective harmonisation,
#: but the basis-invalid mask is itself reconstructed from the full frozen
#: window, so point-in-time observability of the classification is UNESTABLISHED.
PRIMARY_MODE = "PRIMARY_PIT_CONSERVATIVE"
PIT_OBSERVABILITY = "UNESTABLISHED"
SECONDARY_MODE = "RETROSPECTIVE_ECONOMIC_NORMALIZATION_SENSITIVITY"

#: Classifications a regime may carry and still be economically harmonised.
#: This is NOT the same question as whether harmonisation may be applied to the
#: primary path -- see PRIMARY_PIT_APPLIES_BASIS_HARMONISATION.
CERTIFIED_CLASSIFICATIONS = ("RECONSTRUCTIBLE", "CANDIDATE_HARMONIZABLE")

#: The primary, tradable #1F path does NOT apply retrospective basis
#: harmonisation. The measured factors are economically coherent, but the source
#: carries no corporate-action metadata, no publication date and no available_at
#: field, and each regime's boundary was inferred from the whole frozen window.
#: An economically harmonisable regime is therefore broker-basis INVALID for
#: primary use; the factors survive as a clearly labelled retrospective
#: sensitivity, never as headline tradable evidence.
PRIMARY_PIT_APPLIES_BASIS_HARMONISATION = False
SECONDARY_ANALYSIS_LABEL = "RETROSPECTIVE ECONOMIC-NORMALIZATION SENSITIVITY"

#: A ticker is four uppercase letters. Enforced as a real pattern, not implied
#: by a digest: duplicates, blanks and type corruption can all leave the
#: DISTINCT set -- and therefore the digest -- completely unchanged.
TICKER_PATTERN = re.compile(r"^[A-Z]{4}$")
#: Shape check only. Shape is NOT validity: 2026-99-77 matches this pattern and
#: is not a date, so every date check pairs it with strict parsing.
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_FORMAT = "%Y-%m-%d"

#: Hard expectations for the approved freeze. Any deviation stops the gate.
EXPECTED_UNIVERSE_SIZE = 298
ACCEPTED_UNIVERSE_DIGEST = "7a4ce50ffb5a7ebe"
EXPECTED_PRICE_COVERED = 297
EXPECTED_PRICE_MISSING = ("WIKA",)
#: Broker coverage is frozen independently of price coverage: a broker harvest
#: that quietly covered fewer names would silently shrink variants B/C/D without
#: shrinking A, making the variants incomparable.
EXPECTED_BROKER_COVERED = 297
EXPECTED_BROKER_MISSING = ("WIKA",)

#: The same pins, gathered so they travel with the execution contract instead of
#: being read out of module globals deep inside three separate loaders. Making
#: them an explicit input is what lets the full run_gate orchestration be
#: exercised end-to-end against a synthetic fixture: the pins are the only thing
#: in the pipeline that is specific to the production snapshot, and testing the
#: orchestration is not the same act as relaxing them. Production callers get
#: PRODUCTION_EXPECTATIONS and nothing else.
PRODUCTION_EXPECTATIONS = {
    "universe_size": EXPECTED_UNIVERSE_SIZE,
    "universe_digest": ACCEPTED_UNIVERSE_DIGEST,
    "price_covered": EXPECTED_PRICE_COVERED,
    "price_missing": EXPECTED_PRICE_MISSING,
    "broker_covered": EXPECTED_BROKER_COVERED,
    "broker_missing": EXPECTED_BROKER_MISSING,
}


def _expect(expectations):
    """Fill any unspecified pin from the production set."""
    merged = dict(PRODUCTION_EXPECTATIONS)
    merged.update(expectations or {})
    return merged

#: Where the harvest's broker values can be checked against independently
#: observed data. neobdm.db holds live-scraped rows (bval IS NOT NULL) from this
#: date onward; earlier harvest rows have nothing to check against.
#:
#: This window establishes VALUE FIDELITY, NOT point-in-time availability. See
#: broker_provenance() for why the distinction matters.
VALUE_RECONCILED_FROM = "2026-07-05"

#: #1E's feature code (_historical_net_lots) reads netval/bval/sval in BILLIONS
#: of rupiah; the harvest stores plain rupiah. One conversion, applied once, and
#: proven against the live rows rather than assumed.
RUPIAH_PER_BILLION = 1e9

#: Lots are 100 shares on IDX. Used only to recover the average execution price
#: implied by (value, lots).
SHARES_PER_LOT = 100.0

#: Rupiah amounts are whole numbers in practice, and float64 carries ~15-16
#: significant digits, so a value near 1e12 still resolves far below 1 rupiah.
#: A 1-rupiah tolerance is therefore below the smallest meaningful unit while
#: absorbing any parquet round-trip representation error.
NVAL_TOLERANCE_RUPIAH = 1.0
#: Lots are integers; this only absorbs float representation noise.
NLOT_TOLERANCE = 1e-6

# A vendor row can carry a rupiah value while reporting ZERO lots. Measured on the
# frozen harvest: 733 such rows (0.027% of 2,689,458) across exactly three tickers
# -- CTRA, BRPT, TINS -- whose implied quantity, value / that session's close,
# clusters on exactly one lot: median 100.0 shares, sd 2.0, max 109.7. The reverse
# direction, lots with no value, does not occur at all.
#
# Our ingest is not the cause and must not be "fixed": exact_lots() RAISES on a
# non-integral lot rather than flooring, and the raw cache carries a literal 0.0
# beside the correct rupiah value, so the zero is what the vendor sent.
#
# The rule is therefore BOUNDED, not dropped -- the same shape as BASIS_MIN_LOT_GAP,
# which kept the basis rule and added an absolute discriminator rather than relaxing
# the ratio. A value-without-lots row is tolerated only when its implied quantity is
# under two lots: 1.82x the largest observed, and still under the smallest quantity
# that could not be a one-lot artifact. At or above that bound it stays FATAL, and so
# does any row whose price is missing, so an unadjudicable claim is never waved
# through as tolerated.
SUBLOT_MAX_SHARES = 2.0 * SHARES_PER_LOT

REQUIRED_BROKER_NUMERIC = ("nlot", "blot", "slot", "nval", "bval", "sval")

#: Unsigned 32-bit modulus, the observed vendor volume wrap. Matched exactly, in
#: integer arithmetic: the nearest non-wrap observation in the whole cache sits
#: 3.5% of the modulus away, so a tolerance would add risk and buy nothing.
VOLUME_WRAP_MODULUS = 2 ** 32

#: A cross-source ratio this far from 1 is a BASIS mismatch (the deficit scales
#: with volume); anything closer is source rounding (a small absolute lot count,
#: 2-23 lots against millions traded). Measured separation inside the universe is
#: 15.9x -- every basis regime is at least 1.35e-2 from 1, every rounding deficit
#: at most 8.5e-4. Market-wide there is a middle band with no gap, which this
#: threshold routes to QUARANTINE: the conservative direction.
BASIS_RATIO_TOLERANCE = 1e-3

#: ...but a ratio alone misclassifies thin stocks. MTDL loses 3-8 lots on 14
#: scattered sessions; because it trades only 1,003-6,087 lots a day that reads
#: as |r-1| up to 4.8e-3 and would be called a basis break, which it is not.
#:
#: The two populations separate on ABSOLUTE size far more cleanly than on ratio:
#: every measured basis regime moves at least 343 lots (WINS) and typically
#: ~434,000 (RAJA moves 1,647,440), while the worst coverage deficit anywhere in
#: the cache is 23 lots. A basis break must therefore be BOTH ratio-significant
#: AND absolutely large -- the conjunction is what encodes the finding that these
#: are two different phenomena, not two ends of one scale.
BASIS_MIN_LOT_GAP = 100.0

#: Lot conservation is NOT source-guaranteed, so it can never be FATAL. Buy- and
#: sell-side agreement with volume is symmetric (96.0986% vs 96.1081%), all 101
#: declared broker codes appear, and the residual is a handful of lots. Making
#: exact equality fatal would reject 15 in-universe tickers on ~100% of days.
LOT_COVERAGE_DEFICIT_IS_FATAL = False

#: A broker's implied average execution price, bval / (blot * SHARES_PER_LOT),
#: must lie inside that session's [low, high]. A VWAP is an average of executed
#: prices, so it cannot escape the day's range unless the two sides are on
#: different bases or one of them is wrong.
#:
#: This is a ROW-LEVEL, independent confirmation that basis harmonisation
#: worked: before harmonisation RAJA's implied price reads ~4641 against a
#: [low, high] near 920; after scaling lots by the factor it reads ~928 and
#: falls inside. Unlike the aggregate ratio checks, it does not use volume at
#: all, so it cannot be satisfied by the same error that satisfies I1.
#:
#: REPORTED, not FATAL. 90.1% of the raw violations are downstream of the basis
#: mismatch and clear once harmonisation and quarantine run; the remainder sits
#: on otherwise-clean tickers and has no established cause yet. Failing the gate
#: on an unexplained residual would be a guess dressed as a rule.
IMPLIED_PRICE_TOLERANCE = 1e-3

HORIZONS = (1, 2, 3, 4, 5)
LAGS = (1, 3, 5, 10, 20)


class GateFailure(AssertionError):
    """Raised when an input violates a frozen expectation of the gate."""


def _digest(values):
    """SHA-256[:16] over a '|'-joined payload.

    Same construction as ml_v2_experiment_1.prediction_digest so digests across
    experiments are read the same way.
    """
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:16]


def frame_digest(df, columns=None):
    """Content digest of a frame, float-formatted at .12g.

    .12g is what #1E pins its predictions with: stable across platforms, but
    sensitive to any real numeric change.
    """
    cols = list(columns or df.columns)
    values = ["|".join(cols)]
    for col in cols:
        series = df[col]
        if pd.api.types.is_float_dtype(series):
            values.extend("" if pd.isna(v) else f"{v:.12g}" for v in series.to_numpy())
        else:
            values.extend("" if pd.isna(v) else str(v) for v in series.to_numpy())
    return _digest(values)


def invalid_date_mask(series):
    """Rows whose date is missing, misshapen, or not a real calendar date.

    Shape and validity are different questions. '2026-99-77' satisfies
    ^\\d{4}-\\d{2}-\\d{2}$ and is not a date, so the regex is paired with strict
    parsing; coerce turns anything unparseable into NaT rather than guessing at
    a different format.
    """
    shape_ok = series.astype(str).str.match(DATE_PATTERN)
    parsed = pd.to_datetime(series, format=DATE_FORMAT, errors="coerce")
    return series.isna() | ~shape_ok.fillna(False) | parsed.isna()


def file_fingerprint_full(path):
    """FULL sha256. Semantic provenance never travels on a truncated hash."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _semantic_digest(payload):
    """Content identity of a JSON artifact, independent of formatting."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode()
    ).hexdigest()


def regime_factor(regime):
    """The regime's factor, preferring the exact rational over its float shadow.

    `candidate_factor` is a float rendering kept for display. When the exact
    numerator/denominator is present it is authoritative, because the float is
    a lossy view of it and never the other way round.
    """
    exact = regime.get("candidate_factor_exact")
    if exact:
        return exact["numerator"] / exact["denominator"]
    for key in ("candidate_factor", "factor"):
        if key in regime:
            return float(regime[key])
    raise GateFailure(f"{regime.get('ticker', '?')}: regime carries no factor")


def file_fingerprint(path):
    """Full SHA-256 + size of a raw input snapshot."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = os.stat(path)
    return {
        # `path` stays the BASENAME: the legacy two-file manifest is keyed on it
        # and re-keying that artifact would invalidate a pin for a reason that
        # has nothing to do with content. `abspath` is additive, and is what
        # manifest-v3 matches on -- v3 pins several files that can share a
        # basename across directories, so a basename is not an identity there.
        "path": os.path.basename(path),
        "abspath": os.path.abspath(path),
        "sha256": digest.hexdigest(),
        "size_bytes": stat.st_size,
        "mtime_utc": _dt.datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
    }


def verify_input_manifest(fingerprints, path=INPUT_MANIFEST_JSON, establish=False):
    """Pin the reviewed raw snapshot so a refreshed harvest cannot slip in.

    LEGACY two-file manifest. Retained as a primitive because the older
    production entry points still verify against it, but it is NO LONGER
    REACHABLE FROM run_gate: see verify_reviewed_manifest_v3(). In particular
    `establish=True` can no longer be reached from the Gate A CLI at all.

    A MISSING manifest is a failure, not an invitation. Auto-establishing one
    would reopen the exact hole this closes: delete the manifest, refresh the
    parquet, and whatever bytes happen to be on disk quietly become the
    "reviewed" snapshot.

    `mtime` is recorded but never compared: touching a file does not change its
    content.
    """
    current = {fp["path"]: {"sha256": fp["sha256"], "size_bytes": fp["size_bytes"]}
               for fp in fingerprints}

    if establish:
        payload = {
            "established_at_utc": _dt.datetime.utcnow().isoformat() + "Z",
            "inputs": current,
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        return {"status": "ESTABLISHED (explicit, reviewed)",
                "manifest_path": path, "inputs": current}

    if not os.path.exists(path):
        raise GateFailure(
            f"frozen input manifest {os.path.basename(path)} is missing. Gate A "
            "will not adopt whatever parquet bytes are on disk as the reviewed "
            "snapshot -- that is how a silently refreshed harvest becomes the "
            "baseline. Review the hashes, then re-run with --establish-manifest.")

    with open(path, encoding="utf-8") as handle:
        stored = json.load(handle)["inputs"]

    failures = []
    for name, values in current.items():
        if name not in stored:
            failures.append(f"{name} is absent from the reviewed manifest")
            continue
        if stored[name]["sha256"] != values["sha256"]:
            failures.append(
                f"{name} sha256 {values['sha256'][:16]}... != reviewed "
                f"{stored[name]['sha256'][:16]}... (the snapshot changed)")
        if stored[name]["size_bytes"] != values["size_bytes"]:
            failures.append(
                f"{name} size {values['size_bytes']} != reviewed "
                f"{stored[name]['size_bytes']}")
    for name in stored:
        if name not in current:
            failures.append(f"{name} is in the reviewed manifest but was not loaded")
    if failures:
        raise GateFailure(
            "frozen input snapshot no longer matches the reviewed manifest. A new "
            "hash is never silently accepted -- re-establish deliberately with "
            "--establish-manifest after review:\n  - " + "\n  - ".join(failures))
    return {"status": "verified", "manifest_path": path, "inputs": current}


class ManifestEstablishmentRefused(GateFailure):
    """Gate A tried to write its own manifest. That is never its job."""


def execution_input_fingerprints(inputs, root=HERE):
    """Fingerprint EVERY execution input the manifest contract pins. No subset.

    Each input is fingerprinted under ITS OWN identity rule: generated artifacts
    by raw bytes, tracked repository text by the content Git stores at HEAD.
    See EXECUTION_INPUTS and git_content_sha256().

    The exact bytes, taken BEFORE any semantic application: a JSON artifact is
    hashed as it sits on disk, not after parsing, so the pin covers what was
    reviewed rather than what the parser happened to reconstruct.

    run_gate previously handed the verifier three of the seven, and the verifier
    fails any pinned input it was not given -- so a real established manifest
    would have failed Gate A on source_manifest, the two repair artifacts and
    the basis artifact. That defect was invisible to a test whose synthetic
    manifest pinned the same three the gate hashed; the fix belongs here, in the
    production contract, not in a test expectation.
    """
    missing, out = [], {}
    for name, key, identity in EXECUTION_INPUTS:
        path = inputs.get(key)
        if not path:
            raise GateFailure(
                f"execution input {key!r} is not declared by candidate_inputs(); "
                f"the manifest pins it as {name}")
        if not os.path.exists(path):
            missing.append(f"{name} -> {path}")
            continue
        fingerprint = file_fingerprint(path)
        fingerprint["identity"] = identity
        if identity == IDENTITY_GIT_CONTENT:
            fingerprint.update(tracked_text_identity(
                path, root=inputs.get("git_root", root)))
            fingerprint["abspath"] = os.path.abspath(path)
        out[name] = fingerprint
    if missing:
        raise GateFailure(
            "declared execution inputs are missing on disk; absence is never an "
            "empty default:\n  - " + "\n  - ".join(missing))
    return out


class ManifestEstablishmentRefused(GateFailure):
    """Gate A tried to write its own manifest. That is never its job."""


def _git(root, *args, text=True):
    """Run one git command. Returns (ok, output). Never raises on a git error."""
    import subprocess
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True,
                                text=text)
    except OSError as exc:                       # git absent
        return False, f"git is not runnable here ({exc})"
    if result.returncode != 0:
        if text:
            return False, (result.stderr or "").strip() or f"git {args[0]} failed"
        return False, (result.stderr or b"").decode("utf-8", "replace").strip()
    return True, result.stdout


def _git_head(root=HERE):
    """The commit the code being executed actually sits on."""
    ok, out = _git(root, "rev-parse", "HEAD")
    if not ok:
        return None, out
    return out.strip() or None, None


def git_path_is_tracked(path, ref="HEAD", root=HERE):
    """Is this path present in the commit `ref`?"""
    ok, _ = _git(root, "cat-file", "-e", f"{ref}:{_git_rel(path, root)}")
    return ok


def _git_rel(path, root):
    return os.path.relpath(os.path.abspath(path), root).replace(os.sep, "/")


def git_content_sha256(path, ref="HEAD", root=HERE):
    """SHA256 of the bytes GIT HAS STORED for this path at `ref`.

    THE AUTHORITATIVE IDENTITY FOR TRACKED REPOSITORY TEXT, and deliberately not
    the sha256 of the file as it sits in the working tree.

    With core.autocrlf=true and no .gitattributes -- this repository's actual
    configuration -- a clean checkout of the pinned commit holds CRLF while the
    committed blob holds LF. Hashing the checkout bytes therefore produced this:

        same HEAD, git status clean, identical Git content,
        different raw sha256  ->  manifest verification FAILS

    That makes the pin machine-bound rather than commit-bound, which is the
    opposite of what a reproducibility contract is for. Git's own object store
    already holds one canonical byte sequence per commit, so that is what is
    hashed.

    This is NOT a licence to run edited code: on its own, a git-content hash
    would still verify while the working tree carried local modifications. The
    worktree binding is restored separately by git_worktree_matches_ref(), which
    lets Git's clean/filter semantics decide what counts as a difference -- a
    CRLF-only checkout difference is clean, a real edit is not.

    Returns None for a path that is not tracked at `ref`; the caller decides
    whether that is fatal (it is, at establishment).
    """
    if not git_path_is_tracked(path, ref=ref, root=root):
        return None
    ok, blob = _git(root, "show", f"{ref}:{_git_rel(path, root)}", text=False)
    if not ok:
        return None
    return hashlib.sha256(blob).hexdigest()


def git_worktree_matches_ref(path, ref="HEAD", root=HERE):
    """Does the file on disk carry no semantic Git difference against `ref`?

    Covers staged AND unstaged differences, because `git diff <ref> -- <path>`
    compares the working tree against the commit rather than against the index.

    Git applies the repository's own clean filter here, which is exactly the
    behaviour required: a checkout whose only difference is CRLF vs LF is clean
    and must not invalidate the experiment, while a one-character content edit
    is dirty and must.
    """
    if not git_path_is_tracked(path, ref=ref, root=root):
        return False
    ok, _ = _git(root, "diff", "--quiet", ref, "--", _git_rel(path, root))
    return ok


def tracked_text_identity(path, ref="HEAD", root=HERE):
    """The full identity record for one TRACKED pinned path.

    `worktree_raw_sha256` is FORENSIC ONLY and gates nothing: it records which
    literal bytes were on disk, which is useful when diagnosing a failure and
    meaningless as an acceptance criterion.
    """
    tracked = git_path_is_tracked(path, ref=ref, root=root)
    exists = os.path.exists(path)
    return {
        "identity": IDENTITY_GIT_CONTENT,
        "path": _git_rel(path, root),
        "present": exists,
        "tracked_at_head": tracked,
        "git_content_sha256": git_content_sha256(path, ref=ref, root=root),
        "worktree_matches_head": git_worktree_matches_ref(path, ref=ref, root=root),
        "worktree_raw_sha256": file_fingerprint_full(path) if exists else None,
        "worktree_raw_sha256_role": "FORENSIC ONLY -- never gates acceptance",
        "canonical_identity_status": (
            "AVAILABLE" if tracked else UNAVAILABLE_UNTIL_COMMIT),
    }


def _git_content_failures(label, entry, actual):
    """The four conditions a GIT_CONTENT pin must satisfy. All are mandatory.

    Hashing Git content instead of checkout bytes fixes portability, and on its
    own it would open a worse hole: a locally edited implementation would verify
    because HEAD had not moved. The tracked + clean checks close it, and they
    delegate the CRLF question to Git rather than re-deciding it here.
    """
    failures = []
    if not actual.get("tracked_at_head"):
        failures.append(
            f"{label} is pinned as tracked repository text but is NOT tracked at "
            f"HEAD ({actual.get('canonical_identity_status')}); a canonical "
            "identity cannot be invented for a file the commit does not contain")
        return failures
    if not actual.get("present"):
        failures.append(f"{label} is tracked at HEAD but absent from the worktree")
    if not actual.get("worktree_matches_head"):
        failures.append(
            f"{label} carries a staged or unstaged difference against HEAD. Git "
            "itself reports this file as modified, so the pinned commit does not "
            "describe the bytes about to run. (A CRLF-only checkout difference "
            "is clean to Git and would NOT appear here.)")
    pinned = entry.get("git_content_sha256")
    if len(pinned or "") != 64:
        failures.append(
            f"{label} carries no full git_content_sha256 in the manifest "
            f"({pinned!r}); an unestablishable proposal was pinned as a manifest")
    elif pinned != actual.get("git_content_sha256"):
        failures.append(
            f"{label} git content {str(actual.get('git_content_sha256'))[:16]}... "
            f"!= reviewed {pinned[:16]}... (the committed content changed)")
    return failures


def _verify_code_identity(manifest, root, failures):
    """Re-hash every pinned implementation file. The manifest is a claim only.

    Identity is the GIT CONTENT at HEAD, plus a mandatory proof that the working
    tree carries no semantic difference from it. See git_content_sha256().
    """
    code = manifest.get("E_code_identity")
    if not isinstance(code, dict):
        failures.append("E_code_identity is absent; the manifest pins no code")
        return {}
    files = code.get("files") or []
    if not files:
        failures.append("E_code_identity pins no files")
        return {}
    checked, planes = {}, set()
    for entry in files:
        name = entry.get("path", "")
        path = os.path.join(root, name)
        planes.add(entry.get("plane"))
        actual = tracked_text_identity(path, root=root)
        checked[name] = {"plane": entry.get("plane"),
                         "pinned_git_content_sha256": entry.get("git_content_sha256"),
                         "actual_git_content_sha256": actual["git_content_sha256"],
                         "worktree_matches_head": actual["worktree_matches_head"],
                         "worktree_raw_sha256": actual["worktree_raw_sha256"]}
        failures.extend(_git_content_failures(f"pinned code file {name}",
                                              entry, actual))
    if "CONTROL PLANE" not in planes:
        failures.append(
            "no CONTROL PLANE file is pinned; the manifest verifier itself must "
            "be part of the identity it enforces, or the control can be rewritten "
            "without invalidating anything")
    if "DATA PLANE" not in planes:
        failures.append("no DATA PLANE file is pinned")
    return checked


def _verify_policy(manifest, mode, failures):
    """The normalization policy must be the reviewed one, not merely present."""
    policy = manifest.get("C_normalization_policy")
    if not isinstance(policy, dict):
        failures.append("C_normalization_policy is absent")
        return {}
    if policy.get("mode") != PRIMARY_MODE:
        failures.append(
            f"manifest primary mode is {policy.get('mode')!r}, expected "
            f"{PRIMARY_MODE!r}")
    if policy.get("pit_observability") != PIT_OBSERVABILITY:
        failures.append(
            f"manifest pit_observability is {policy.get('pit_observability')!r}, "
            f"expected {PIT_OBSERVABILITY!r}. A manifest claiming PIT "
            "observability had been established would authorise a claim no "
            "evidence in this cache supports.")
    if policy.get("primary_applies_basis_harmonisation") is not False:
        failures.append(
            "manifest says PRIMARY applies basis harmonisation; the reviewed "
            "policy is that it applies none")
    if policy.get("observable_inventory_resets_at_a_hole") is not False:
        failures.append(
            "manifest says observable inventory resets at a hole; the reviewed "
            "policy is that it never resets and never re-anchors")

    # SECONDARY is a DIFFERENT run under the same data pins, and it must say so
    # out loud. Silently running a retrospective sensitivity mode against a
    # manifest that only authorises PRIMARY would let a non-tradable result
    # inherit a reviewed manifest's authority.
    if mode == SECONDARY_MODE:
        if policy.get("secondary_mode") != SECONDARY_MODE:
            failures.append(
                f"this run requested {SECONDARY_MODE!r} but the manifest "
                f"declares secondary_mode {policy.get('secondary_mode')!r}")
        if policy.get("secondary_is_not_tradable_evidence") is not True:
            failures.append(
                "the manifest does not record that SECONDARY is not tradable "
                "evidence; a sensitivity run may not borrow PRIMARY's standing")
    return {
        "mode": policy.get("mode"),
        "pit_observability": policy.get("pit_observability"),
        "run_mode": mode,
        "run_is_sensitivity_only": mode == SECONDARY_MODE,
    }


def _verify_authorization(manifest, inputs, artifacts, failures):
    """Cross-check the manifest's authorisation against the loaded artifact.

    The manifest records the reviewed key set and the parent identity; the gate
    has already loaded the same authorisation through the fail-closed contract.
    If the two disagree, one of them is describing a different review.
    """
    auth = manifest.get("D_authorization")
    if not isinstance(auth, dict):
        failures.append("D_authorization is absent")
        return {}
    if auth.get("scope") != AUTHORIZATION_SCOPE:
        failures.append(
            f"manifest authorization scope is {auth.get('scope')!r}, expected "
            f"{AUTHORIZATION_SCOPE!r}")
    if artifacts is None:
        return {"scope": auth.get("scope"), "cross_checked_with_artifact": False}

    physical = file_fingerprint_full(inputs["repair_candidates"])
    if auth.get("parent_candidate_sha256") != physical:
        failures.append(
            f"manifest authorisation parent {str(auth.get('parent_candidate_sha256'))[:16]}"
            f"... != candidate artifact on disk {physical[:16]}...")
    if auth.get("parent_candidate_sha256") != artifacts["parent_candidate_sha256"]:
        failures.append(
            "the manifest and the loaded authorisation disagree about which "
            "candidate artifact was reviewed")
    with open(inputs["repair_candidates"], encoding="utf-8") as handle:
        semantic = _semantic_digest(json.load(handle))
    if auth.get("parent_candidate_semantic_digest") != semantic:
        failures.append(
            "manifest parent_candidate_semantic_digest does not match the "
            "candidate artifact's recomputed semantic digest")

    manifest_keys = sorted(auth.get("authorized_keys") or [])
    loaded_keys = sorted(f"{t} {d}" for t, d in artifacts["authorised_repairs"])
    if manifest_keys != loaded_keys:
        failures.append(
            f"manifest authorises {manifest_keys} but the loaded authorisation "
            f"carries {loaded_keys}; an EXACT key set cannot differ by one entry")

    stage_b = sorted(f"{h['ticker']} {h['date']}"
                     for h in artifacts.get("diagnostic_only", []))
    if sorted(auth.get("stage_b_structurally_ineligible") or []) != stage_b:
        failures.append(
            "the manifest's stage-B ineligible set does not match the artifact's")
    if set(auth.get("authorized_keys") or []) & set(stage_b):
        failures.append(
            "a stage-B diagnostic appears in the manifest's authorised keys; it "
            "has no proposed volume and can never be authorised")
    return {"scope": auth.get("scope"), "authorized_keys": manifest_keys,
            "stage_b_structurally_ineligible": stage_b,
            "cross_checked_with_artifact": True}


def _verify_rule_versions(manifest, artifacts, failures):
    """The rule versions the manifest names must be the ones actually imported."""
    # Imported lazily: the gate needs two string constants, not a dependency on
    # the producer, and a module-level import would make the consumer of the
    # contract depend on the module that writes it.
    import experiment_1f_normalization as _norm

    rules = manifest.get("G_rule_versions")
    if not isinstance(rules, dict):
        failures.append("G_rule_versions is absent")
        return {}
    actual = {"volume_wrap": _norm.RULE_VERSION_WRAP,
              "basis_regime": _norm.RULE_VERSION_BASIS}
    for name, value in actual.items():
        if rules.get(name) != value:
            failures.append(
                f"rule version {name} is {rules.get(name)!r} in the manifest but "
                f"{value!r} in the imported implementation")
    if artifacts is not None:
        pinned_n = rules.get("n_regimes")
        actual_n = sum(len(v) for v in artifacts["regimes"].values())
        if pinned_n is not None and int(pinned_n) != actual_n:
            failures.append(
                f"manifest pins {pinned_n} basis regimes but the artifact "
                f"carries {actual_n}")
    return dict(actual, n_regimes=rules.get("n_regimes"))


def _verify_parentage(manifest, inputs, failures):
    """RECOMPUTE the critical bindings. A stored boolean proves nothing.

    `H_parentage["all_bindings_ok"] = true` is a claim written by the producer
    into a file the producer also wrote. Trusting it would make the parentage
    check self-certifying, so every binding that can be recomputed from files on
    disk is recomputed here instead.
    """
    import experiment_1f_candidate as _cand

    chain = (manifest.get("H_parentage") or {}).get("chain")
    if not isinstance(chain, list) or not chain:
        failures.append("H_parentage carries no chain")
        return {}
    if (manifest.get("H_parentage") or {}).get("all_bindings_ok") is not True:
        failures.append("the reviewed manifest itself records a broken parentage")

    with open(inputs["source_manifest"], encoding="utf-8") as handle:
        source_manifest = json.load(handle)
    recorded_aggregate = source_manifest.get("source_aggregate_sha256")
    recomputed = _cand.aggregate_digest(
        (entry["ticker"], entry["sha256"])
        for entry in source_manifest.get("files", []))
    if recomputed != recorded_aggregate:
        failures.append(
            f"source_manifest aggregate {str(recorded_aggregate)[:16]}... does not "
            f"match the digest recomputed from its own file list {recomputed[:16]}...")

    for link in chain:
        pinned = link.get("parent_identity")
        if pinned is not None and pinned != recomputed:
            failures.append(
                f"parentage link for {link.get('child')} names parent identity "
                f"{str(pinned)[:16]}... which is not the recomputed source "
                f"aggregate {recomputed[:16]}...")

    # every stage-A candidate must name the source file the snapshot actually holds
    by_ticker = {entry["ticker"]: entry["sha256"]
                 for entry in source_manifest.get("files", [])}
    with open(inputs["repair_candidates"], encoding="utf-8") as handle:
        candidates = json.load(handle)
    unbound = []
    for hit in candidates.get("candidates", []):
        expected = by_ticker.get(hit["ticker"])
        if expected is None or hit.get("source_sha256") != expected:
            unbound.append(f"{hit['ticker']} {hit['date']}")
    if unbound:
        failures.append(
            "these repair candidates do not bind to the snapshot's source files: "
            + ", ".join(unbound))

    authorization_link = next(
        (l for l in chain if l.get("child") == "volume_repair_authorization.json"),
        None)
    if authorization_link is not None:
        physical = file_fingerprint_full(inputs["repair_candidates"])
        if authorization_link.get("parent_sha256") != physical:
            failures.append(
                "the parentage chain's authorisation parent is not the candidate "
                "artifact on disk")

    return {
        "source_aggregate_recomputed": recomputed,
        "source_aggregate_matches_manifest": recomputed == recorded_aggregate,
        "candidate_source_bindings_recomputed": len(candidates.get("candidates", [])),
        "recomputed_not_trusted_from_json": True,
    }


def verify_reviewed_manifest_v3(inputs, fingerprints, artifacts=None,
                                mode=None, path=None,
                                expected_version=MANIFEST_V3_VERSION, root=HERE):
    """VERIFY the reviewed manifest-v3. It cannot establish, by construction.

    There is no `establish` parameter and no write path in this function, which
    is the point. A verifier that can also establish is not a control: the
    moment verification fails, the cheapest way out is to re-establish, and the
    pin becomes a record of the last run rather than of the last review. The old
    two-file verifier had exactly that shape and Gate A's CLI exposed it as
    `--establish-manifest`, so a Gate A run could silently re-pin its own inputs.

    Establishment lives in experiment_1f_manifest.establish_reviewed_manifest(),
    is a separate act, and is only legitimate after the code is committed and
    the semantic tree is clean -- conditions Gate A cannot check about itself
    while it is running.

    A REVIEWED MANIFEST IS A CLAIM; THIS FUNCTION PROVES IT. Everything it can
    recompute from files on disk it recomputes, rather than reading a boolean
    the producer wrote into its own output:

      A  every one of the seven execution inputs, by name AND by resolved path
      B  established_commit_sha == the actual git HEAD
      C  E_code_identity re-hashed, data plane and control plane both present
      D  the normalization policy, including the SECONDARY authorisation rule
      E  the authorisation, cross-checked against the loaded artifact
      F  the rule versions actually imported
      G  the parentage bindings, recomputed from the snapshot

    Manifest-v3 separates EXECUTION inputs from PROVENANCE inputs, so section A
    covers A_execution_inputs only: a provenance file changing is an
    investigation, not an automatic invalidation of a frozen 297-name experiment.
    """
    mode = mode or PRIMARY_MODE
    # TWO roots, deliberately distinct. `root` resolves the manifest's relative
    # paths, which _fp() writes relative to this module's directory. `code_root`
    # is the REPOSITORY whose object store defines committed identity, and it is
    # what the HEAD and code-identity checks run against. In production they are
    # the same directory; keeping them separate is what lets the pinned
    # implementation be verified against a repository other than the one the
    # generated artifacts happen to sit under.
    code_root = inputs.get("code_root", root)
    path = path or inputs.get("reviewed_manifest") or REVIEWED_MANIFEST_V3_JSON
    if not os.path.exists(path):
        raise GateFailure(
            f"the reviewed manifest {os.path.basename(path)} does not exist, so "
            "there is nothing for Gate A to verify against. Gate A will NOT "
            "establish one: a run that pins its own inputs records what it just "
            "read, not what a human reviewed. Establishment is a separate act, "
            "performed by experiment_1f_manifest.establish_reviewed_manifest(), "
            "and only after the implementation is committed and the semantic "
            "tree is clean.")

    with open(path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    version = manifest.get("manifest_version")
    if version != expected_version:
        raise GateFailure(
            f"reviewed manifest is version {version!r}, expected "
            f"{expected_version!r}. A v2 (two-file) manifest cannot express the "
            "execution/provenance split this gate depends on, so it is refused "
            "rather than reinterpreted.")
    established = manifest.get("established_commit_sha")
    if not established:
        raise GateFailure(
            "the reviewed manifest carries no established_commit_sha; it is a "
            "PROPOSAL, not a manifest. A proposal pins no code identity, so it "
            "cannot certify that the implementation which produced the "
            "artifacts is the implementation about to consume them.")

    failures = []

    # ── A: the COMPLETE execution-input set, both directions ──
    pinned = {name: entry
              for name, entry in (manifest.get("A_execution_inputs") or {}).items()
              if not name.startswith("_")}
    verified = {}
    for name, fingerprint in sorted(fingerprints.items()):
        entry = pinned.get(name)
        if entry is None:
            failures.append(
                f"{name} was CONSUMED but is not pinned as an execution input")
            continue
        identity = fingerprint["identity"]
        if entry.get("identity") != identity:
            failures.append(
                f"{name} is pinned under identity {entry.get('identity')!r} but "
                f"the contract requires {identity!r}; the identity rule decides "
                "what a matching hash even means")
        elif identity == IDENTITY_RAW_BYTES:
            if entry.get("sha256") != fingerprint["sha256"]:
                failures.append(
                    f"{name} sha256 {fingerprint['sha256'][:16]}... != reviewed "
                    f"{str(entry.get('sha256'))[:16]}... (the snapshot changed)")
            if entry.get("size_bytes") != fingerprint["size_bytes"]:
                failures.append(
                    f"{name} size {fingerprint['size_bytes']} != reviewed "
                    f"{entry.get('size_bytes')}")
        else:
            # GIT_CONTENT. size_bytes is deliberately NOT compared: CRLF vs LF
            # changes the byte count of identical Git content, and comparing it
            # would reintroduce the machine-binding this rule removes.
            failures.extend(_git_content_failures(name, entry, fingerprint))

        pinned_path = os.path.normcase(os.path.normpath(
            os.path.join(root, entry.get("path", ""))))
        actual_path = os.path.normcase(os.path.normpath(fingerprint["abspath"]))
        if pinned_path != actual_path:
            failures.append(
                f"{name} is pinned at {entry.get('path')} but the file consumed "
                f"was {fingerprint['abspath']}; a pin that names a different file "
                "certifies nothing about the one that was read")
        verified[name] = {"identity": identity,
                          "sha256": fingerprint.get("sha256"),
                          "git_content_sha256": fingerprint.get("git_content_sha256"),
                          "size_bytes": fingerprint["size_bytes"],
                          "path": fingerprint["abspath"]}
    for name in pinned:
        if name not in verified:
            failures.append(
                f"{name} is pinned as an EXECUTION input but was not consumed by "
                "this run; a pinned input the gate never opens is decoration, not "
                "a contract")

    # ── B: the commit the manifest names must be the commit being run ──
    head, git_error = _git_head(code_root)
    if git_error:
        failures.append(
            f"the commit the manifest pins cannot be checked: {git_error}. A "
            "manifest names a commit precisely so the running code can be tied "
            "to it, and an uncheckable pin is not a pin.")
    elif head != established:
        failures.append(
            f"manifest established_commit_sha {established[:12]}... != current "
            f"HEAD {str(head)[:12]}...; the pinned commit does not describe the "
            "code about to run")

    code = _verify_code_identity(manifest, code_root, failures)
    policy = _verify_policy(manifest, mode, failures)
    authorization = _verify_authorization(manifest, inputs, artifacts, failures)
    rules = _verify_rule_versions(manifest, artifacts, failures)
    parentage = _verify_parentage(manifest, inputs, failures)

    if failures:
        raise GateFailure(
            "the reviewed manifest-v3 does not verify. Gate A cannot re-pin "
            "itself out of this; re-review and re-establish deliberately:\n  - "
            + "\n  - ".join(failures))

    return {
        "status": "verified against reviewed manifest-v3",
        "manifest_version": version,
        "manifest_path": path,
        "established_commit_sha": established,
        "head_commit_sha": head,
        "establishment_reachable_from_gate": False,
        "checks_performed": ["execution_inputs_complete", "commit_is_head",
                             "code_identity_rehashed", "normalization_policy",
                             "authorization_cross_checked", "rule_versions",
                             "parentage_recomputed"],
        "inputs": verified,
        "code_identity": {"files_rehashed": len(code)},
        "policy": policy,
        "authorization": authorization,
        "rule_versions": rules,
        "parentage": parentage,
    }



# ── universe ingestion ────────────────────────

def validate_universe_cells(cells, source_path=UNIVERSE_XLSX, sheet=UNIVERSE_SHEET):
    """HARD-VALIDATE a column of universe cells. Raises GateFailure on any defect.

    The audit is not ceremonial. A spreadsheet is exactly where a legitimate
    ticker turns into something else: IDX lists a real stock called TRUE, which
    Excel will happily store as the boolean TRUE, and a ticker that looks
    numeric or date-like can be retyped the same way.

    The checks do NOT rely on the digest alone, because the digest covers the
    sorted DISTINCT set: a duplicated row, a blank cell, or a coerced type can
    leave that set -- and therefore the digest -- byte-identical while the file
    has genuinely changed.

    Split out from the workbook reader so the validation is testable without a
    spreadsheet on disk.
    """
    header, body = (cells[0] if cells else None), list(cells[1:])
    non_string = [{"row": i + 2, "value": repr(v), "type": type(v).__name__}
                  for i, v in enumerate(body)
                  if v is not None and not isinstance(v, str)]
    blanks = [i + 2 for i, v in enumerate(body)
              if v is None or (isinstance(v, str) and not v.strip())]
    strings = [v for v in body if isinstance(v, str) and v.strip()]
    malformed = [{"row": i + 2, "value": repr(v)} for i, v in enumerate(body)
                 if isinstance(v, str) and v.strip() and not TICKER_PATTERN.match(v)]

    counts = pd.Series(strings, dtype=object).value_counts() if strings else pd.Series(dtype=int)
    duplicates = sorted(counts[counts > 1].index.tolist())
    tickers = sorted(set(strings))
    digest = _digest(tickers)

    audit = {
        "source_path": source_path,
        "sheet": sheet,
        "header_cell": header,
        "rows_after_header": len(body),
        "non_string_cells": non_string,
        "blank_cells": blanks,
        "malformed_tickers": malformed,
        "duplicates": duplicates,
        "distinct_tickers": len(tickers),
        "universe_digest": digest,
    }

    failures = []
    if header != UNIVERSE_HEADER:
        failures.append(f"header cell is {header!r}, expected {UNIVERSE_HEADER!r}")
    if non_string:
        failures.append(f"{len(non_string)} non-string cell(s) (Excel type coercion): {non_string[:5]}")
    if blanks:
        failures.append(f"{len(blanks)} blank cell(s) at rows {blanks[:10]}")
    if malformed:
        failures.append(f"{len(malformed)} malformed ticker(s) failing ^[A-Z]{{4}}$: {malformed[:5]}")
    if duplicates:
        failures.append(f"{len(duplicates)} duplicate ticker(s): {duplicates[:10]}")
    if len(tickers) != EXPECTED_UNIVERSE_SIZE:
        failures.append(f"{len(tickers)} distinct tickers, expected {EXPECTED_UNIVERSE_SIZE}")
    if digest != ACCEPTED_UNIVERSE_DIGEST:
        failures.append(f"digest {digest} != accepted {ACCEPTED_UNIVERSE_DIGEST}")
    if failures:
        raise GateFailure(
            "approved universe failed validation - re-freezing is a reviewed "
            "decision, never automatic:\n  - " + "\n  - ".join(failures))

    return tickers, audit


def read_universe_xlsx(path=UNIVERSE_XLSX, sheet=UNIVERSE_SHEET):
    """Read the approved workbook column and hand it to validate_universe_cells."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if sheet not in workbook.sheetnames:
        workbook.close()
        raise GateFailure(
            f"sheet {sheet!r} not found in {path} (sheets: {workbook.sheetnames})")
    cells = [row[0].value for row in workbook[sheet].iter_rows(min_col=1, max_col=1)]
    workbook.close()
    return validate_universe_cells(cells, source_path=path, sheet=sheet)


def write_frozen_universe(tickers, audit, path=FROZEN_UNIVERSE_JSON):
    """Persist the validated universe as the reproducible source of record.

    `frozen_at_utc` is provenance metadata only. It is deliberately NOT part of
    the universe digest, so re-freezing identical content yields an identical
    digest and the artifact stays deterministic.
    """
    payload = {
        "universe_digest": audit["universe_digest"],
        "n_tickers": len(tickers),
        # Basename only: this artifact is committed, and the directory the
        # workbook happened to sit in identifies nothing. The filename plus
        # source_sheet is what identifies it.
        "source_path": os.path.basename(audit["source_path"] or ""),
        "source_sheet": audit["sheet"],
        "frozen_at_utc": _dt.datetime.utcnow().isoformat() + "Z",
        "tickers": tickers,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def load_frozen_universe(path=FROZEN_UNIVERSE_JSON, expectations=None):
    """Load and re-verify the frozen artifact, independent of the workbook."""
    expect = _expect(expectations)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    tickers = payload["tickers"]

    failures = []
    if sorted(set(tickers)) != sorted(tickers):
        failures.append("frozen artifact contains duplicate tickers")
    bad = [t for t in tickers if not isinstance(t, str) or not TICKER_PATTERN.match(t)]
    if bad:
        failures.append(f"frozen artifact contains malformed tickers: {bad[:5]}")
    if len(tickers) != expect["universe_size"]:
        failures.append(f"{len(tickers)} tickers, expected {expect['universe_size']}")
    recomputed = _digest(sorted(tickers))
    if recomputed != expect["universe_digest"]:
        failures.append(
            f"recomputed digest {recomputed} != accepted {expect['universe_digest']}")
    if payload.get("universe_digest") != recomputed:
        failures.append(
            f"stored digest {payload.get('universe_digest')} != recomputed {recomputed}")
    if failures:
        raise GateFailure("frozen universe artifact failed validation:\n  - "
                          + "\n  - ".join(failures))

    audit = {
        "source_path": payload.get("source_path"),
        "sheet": payload.get("source_sheet"),
        "header_cell": UNIVERSE_HEADER,
        "rows_after_header": None,
        "non_string_cells": [],
        "blank_cells": [],
        "malformed_tickers": [],
        "duplicates": [],
        "distinct_tickers": len(tickers),
        "universe_digest": recomputed,
        "loaded_from": "frozen artifact",
        "consumed_path": os.path.abspath(path),
        "consumed_fingerprint": file_fingerprint(path),
        "frozen_at_utc": payload.get("frozen_at_utc"),
    }
    return sorted(tickers), audit


def resolve_universe(xlsx_path=UNIVERSE_XLSX, frozen_path=FROZEN_UNIVERSE_JSON,
                     refreeze=False, require_frozen=False, expectations=None):
    """Prefer the frozen artifact; fall back to the workbook only to create it.

    Gate B must never reread a mutable Desktop workbook, so once the freeze
    exists it is authoritative. `refreeze=True` re-reads the workbook
    deliberately -- and still hard-fails if its content no longer matches the
    accepted freeze, so a changed workbook can never be picked up silently.

    `require_frozen=True` is the candidate contract's setting and removes BOTH
    escapes. Falling back to the workbook would mean the universe actually
    consumed is not the file the manifest pins, and re-reading the workbook
    would let a mutable Desktop file re-enter a frozen experiment. Under it the
    declared `universe_json` is the only file that can be read: a missing one is
    a stop, not an invitation to regenerate it.
    """
    if require_frozen:
        if refreeze:
            raise GateFailure(
                "the candidate contract will not re-read the universe workbook: "
                "refreeze is an act on production state, and the pinned "
                f"{os.path.basename(frozen_path)} is the only universe an "
                "experiment-owned run may consume")
        if not os.path.exists(frozen_path):
            raise GateFailure(
                f"the declared universe artifact {frozen_path} does not exist. "
                "The candidate contract will not silently fall back to the "
                "repo-root default or to the workbook -- a declared input that "
                "is not the file consumed is exactly the drift this pins shut.")
        return load_frozen_universe(frozen_path, expectations=expectations)
    if os.path.exists(frozen_path) and not refreeze:
        return load_frozen_universe(frozen_path, expectations=expectations)
    tickers, audit = read_universe_xlsx(xlsx_path)
    payload = write_frozen_universe(tickers, audit, frozen_path)
    audit["loaded_from"] = "workbook (frozen now)"
    audit["consumed_path"] = os.path.abspath(frozen_path)
    audit["consumed_fingerprint"] = file_fingerprint(frozen_path)
    audit["frozen_at_utc"] = payload["frozen_at_utc"]
    return tickers, audit


# ── raw source integrity ──────────────────────

def audit_raw_ohlc(frame, calendar, scope="full harvest"):
    """Structural audit of the RAW price harvest, BEFORE detect() runs.

    Runs on the FULL harvest, because detect() runs on the full harvest: its
    cross-sectional duplicate test is only meaningful over the whole
    cross-section, and it assumes the frame is already structurally sound --
    unique keys, real dates, finite numbers. If that assumption is wrong its
    output is meaningless, so those properties are established first, here.

    Non-positive OPEN is reported but does NOT fail: an unusable open is exactly
    what the open-anchor contract exists to catch, and it withholds the label
    rather than deleting the row. Non-positive high/low/close DO fail -- nothing
    downstream can form a return from them and no contract absorbs it.
    """
    report = {"scope": scope, "rows": int(len(frame))}
    failures = []

    report["duplicate_date_ticker"] = int(frame.duplicated(["date", "ticker"]).sum())
    if report["duplicate_date_ticker"]:
        failures.append(f"{report['duplicate_date_ticker']} duplicate (date,ticker) rows")

    bad_date = invalid_date_mask(frame["date"])
    report["invalid_dates"] = int(bad_date.sum())
    if report["invalid_dates"]:
        examples = frame.loc[bad_date, "date"].astype(str).unique()[:5].tolist()
        failures.append(
            f"{report['invalid_dates']} missing/unparseable/impossible dates, "
            f"e.g. {examples}")

    missing_ticker = frame["ticker"].isna() | (frame["ticker"].astype(str).str.strip() == "")
    report["missing_ticker"] = int(missing_ticker.sum())
    if report["missing_ticker"]:
        failures.append(f"{report['missing_ticker']} rows with a missing ticker")

    non_finite = {}
    for col in OHLCV:
        count = int((~np.isfinite(frame[col].to_numpy(dtype=float))).sum())
        non_finite[col] = count
        if count:
            failures.append(f"{count} non-finite values in {col}")
    report["non_finite"] = non_finite

    nonpositive = {col: int((frame[col] <= 0).sum()) for col in ("open", "high", "low", "close")}
    report["nonpositive"] = nonpositive
    for col in ("high", "low", "close"):
        if nonpositive[col]:
            failures.append(f"{nonpositive[col]} non-positive {col}")
    report["nonpositive_open_note"] = (
        "non-positive open is reported, not fatal: the open-anchor contract "
        "withholds the label for such a row rather than deleting it")

    calendar_series = pd.Series(list(calendar), dtype=object)
    bad_calendar = calendar_series[invalid_date_mask(calendar_series)]
    report["invalid_calendar_dates"] = int(len(bad_calendar))
    report["calendar_dates"] = len(calendar)
    if len(bad_calendar):
        failures.append(
            f"{len(bad_calendar)} invalid calendar dates: {bad_calendar.tolist()[:5]}")
    if list(calendar) != sorted(calendar):
        failures.append("calendar is not sorted ascending")

    report["failures"] = failures
    report["passed"] = not failures
    if failures:
        raise GateFailure(
            "raw OHLC source integrity failed - detect() would be meaningless on "
            "a structurally broken frame:\n  - " + "\n  - ".join(failures))
    return report


def adjudicate_value_without_lots(frame, prices=None):
    """Separate a one-lot vendor rounding artifact from a real coherence defect.

    A row reporting rupiah value with zero lots is not self-describing: the same
    shape covers a trade too small for the vendor's lot field to represent, and a
    genuinely corrupt row that lost a large quantity. Counting them together, which
    is what a pure sign test does, cannot tell those apart -- so it either rejects
    the whole harvest over the market's smallest possible trade, or it waves a real
    defect through. Neither is a measurement.

    The discriminator is the implied quantity, value / that session's close, in
    shares. It uses no lot field at all, so it cannot be satisfied by the same defect
    it is adjudicating.

    Adjudication is deliberately conservative in both unresolved directions. A row
    whose price is missing or non-positive is FATAL, not tolerated: an unadjudicable
    claim must never be silently accepted. And passing no price frame at all makes
    every such row fatal, so a caller that forgets to supply prices gets a loud
    failure rather than a quietly weaker audit.
    """
    result = {
        "rows": 0, "tolerated": 0, "fatal": 0,
        "max_implied_shares": 0.0, "bound_shares": SUBLOT_MAX_SHARES,
        "tickers": [], "failures": [],
    }

    sides = []
    for value_col, lot_col in (("bval", "blot"), ("sval", "slot")):
        mask = (frame[value_col] > 0) & (frame[lot_col] <= 0)
        if mask.any():
            part = frame.loc[mask, ["date", "ticker", value_col]].copy()
            part = part.rename(columns={value_col: "value"})
            part["side"] = lot_col
            sides.append(part)
    if not sides:
        return result

    offending = pd.concat(sides, ignore_index=True)
    result["rows"] = int(len(offending))
    result["tickers"] = sorted(offending["ticker"].unique().tolist())

    if prices is None:
        result["fatal"] = int(len(offending))
        result["failures"].append(
            f"{len(offending)} rows carry value with zero lots and no price frame was "
            "supplied to adjudicate them; refusing to tolerate unadjudicated rows")
        return result

    close = prices[["date", "ticker", "close"]].drop_duplicates(["date", "ticker"])
    merged = offending.merge(close, on=["date", "ticker"], how="left")

    close_values = merged["close"].to_numpy(dtype=float)
    unpriced = ~np.isfinite(close_values) | (close_values <= 0)
    implied = np.full(len(merged), np.nan)
    np.divide(merged["value"].to_numpy(dtype=float), close_values,
              out=implied, where=~unpriced)

    over = ~unpriced & (implied >= SUBLOT_MAX_SHARES)
    result["max_implied_shares"] = float(np.nanmax(implied)) if (~unpriced).any() else 0.0
    result["tolerated"] = int((~unpriced & ~over).sum())
    result["fatal"] = int(unpriced.sum() + over.sum())

    if unpriced.any():
        result["failures"].append(
            f"{int(unpriced.sum())} rows carry value with zero lots and no usable close "
            "price, so the implied quantity cannot be adjudicated")
    if over.any():
        worst = float(np.nanmax(np.where(over, implied, np.nan)))
        result["failures"].append(
            f"{int(over.sum())} rows carry value with zero lots at an implied quantity "
            f"of {SUBLOT_MAX_SHARES:g} shares or more (worst {worst:.1f}); that is too "
            "large to be a one-lot rounding artifact")
    return result


def audit_broker_source(frame, prices=None):
    """Source-integrity audit of the RAW broker harvest, before any conversion.

    Normalization must not run on incoherent inputs: converting units or
    deriving an average price from fields that do not add up would launder a
    data defect into a plausible-looking feature.

    NaN is checked EXPLICITLY rather than inferred from comparisons. `NaN < 0`
    is False and `NaN != x` is True, so a purely comparison-based audit would
    wave non-finite values straight through.

    Sign conventions: blot/slot/bval/sval are gross buy/sell quantities and can
    never be negative. nlot/nval are NET and legitimately can be. Value and lots
    must agree about whether a side traded at all -- in BOTH directions, since
    positive lots with no value is exactly as incoherent as value with no lots.
    """
    report = {"rows": int(len(frame))}
    failures = []

    non_finite = {}
    for col in REQUIRED_BROKER_NUMERIC:
        count = int((~np.isfinite(frame[col].to_numpy(dtype=float))).sum())
        non_finite[col] = count
        if count:
            failures.append(f"{count} non-finite (NaN/inf) values in required field {col}")
    report["non_finite"] = non_finite

    report["duplicate_date_ticker_broker"] = int(
        frame.duplicated(["date", "ticker", "broker"]).sum())
    if report["duplicate_date_ticker_broker"]:
        failures.append(
            f"{report['duplicate_date_ticker_broker']} duplicate (date,ticker,broker) rows")

    bad_date = invalid_date_mask(frame["date"])
    report["invalid_dates"] = int(bad_date.sum())
    if report["invalid_dates"]:
        examples = frame.loc[bad_date, "date"].astype(str).unique()[:5].tolist()
        failures.append(
            f"{report['invalid_dates']} invalid/impossible dates, e.g. {examples}")

    missing_ids = frame["ticker"].isna() | (frame["ticker"].astype(str).str.strip() == "") \
        | frame["broker"].isna() | (frame["broker"].astype(str).str.strip() == "")
    report["missing_identifiers"] = int(missing_ids.sum())
    if report["missing_identifiers"]:
        failures.append(f"{report['missing_identifiers']} rows with missing ticker/broker")

    for col in ("blot", "slot", "bval", "sval"):
        count = int((frame[col] < 0).sum())
        report[f"negative_{col}"] = count
        if count:
            failures.append(f"{count} rows with negative {col} (gross field cannot be negative)")

    nlot_gap = (frame["nlot"] - (frame["blot"] - frame["slot"])).abs()
    report["nlot_inconsistent"] = int((nlot_gap > NLOT_TOLERANCE).sum())
    report["nlot_max_gap"] = float(nlot_gap.max()) if len(frame) else 0.0
    if report["nlot_inconsistent"]:
        failures.append(f"{report['nlot_inconsistent']} rows where nlot != blot - slot")

    nval_gap = (frame["nval"] - (frame["bval"] - frame["sval"])).abs()
    report["nval_inconsistent"] = int((nval_gap > NVAL_TOLERANCE_RUPIAH).sum())
    report["nval_max_gap_rupiah"] = float(nval_gap.max()) if len(frame) else 0.0
    report["nval_tolerance_rupiah"] = NVAL_TOLERANCE_RUPIAH
    if report["nval_inconsistent"]:
        failures.append(
            f"{report['nval_inconsistent']} rows where nval != bval - sval beyond "
            f"{NVAL_TOLERANCE_RUPIAH} rupiah")

    coherence = {
        "bval_positive_blot_nonpositive": int(((frame["bval"] > 0) & (frame["blot"] <= 0)).sum()),
        "sval_positive_slot_nonpositive": int(((frame["sval"] > 0) & (frame["slot"] <= 0)).sum()),
        "blot_positive_bval_nonpositive": int(((frame["blot"] > 0) & (frame["bval"] <= 0)).sum()),
        "slot_positive_sval_nonpositive": int(((frame["slot"] > 0) & (frame["sval"] <= 0)).sum()),
    }
    report.update(coherence)

    # Lots without value has no rounding story -- a trade cannot move a lot for no
    # money -- so it stays unconditionally fatal.
    for name in ("blot_positive_bval_nonpositive", "slot_positive_sval_nonpositive"):
        if coherence[name]:
            failures.append(f"{coherence[name]} rows failing value/lot coherence: {name}")

    # Value without lots is adjudicated per row against the implied quantity.
    sublot = adjudicate_value_without_lots(frame, prices)
    report["value_without_lots"] = sublot
    failures.extend(sublot["failures"])

    report["failures"] = failures
    report["passed"] = not failures
    return report


# ── harvest ingestion ─────────────────────────

def load_full_harvest(path=OHLC_PARQUET):
    """Load the ENTIRE frozen price harvest plus its trading calendar.

    Returns the full cross-section deliberately. Two things need it:

      - detect()'s cross_ticker_dup, which compares OHLCV ACROSS tickers and is
        blinded by any prior filtering;
      - the trading calendar, which must be every date the market traded, so
        add_forward_returns() can tell a genuine suspension apart from a date
        nobody traded.
    """
    frame = pd.read_parquet(path)
    calendar = sorted(frame["date"].unique())
    return frame, calendar, file_fingerprint(path)


def price_coverage(full_frame, universe, fingerprint, calendar, expectations=None):
    """Which approved names the price harvest carries, asserted against the freeze."""
    expect = _expect(expectations)
    present = set(full_frame["ticker"].unique())
    covered = sorted(set(universe) & present)
    missing = sorted(set(universe) - present)

    coverage = {
        "fingerprint": fingerprint,
        "harvest_rows_total": int(len(full_frame)),
        "harvest_tickers_total": int(full_frame["ticker"].nunique()),
        "calendar_dates": len(calendar),
        "calendar_first": calendar[0] if calendar else None,
        "calendar_last": calendar[-1] if calendar else None,
        "price_covered_n": len(covered),
        "price_missing": missing,
    }

    failures = []
    if len(covered) != expect["price_covered"]:
        failures.append(
            f"{len(covered)} tickers with prices, expected {expect['price_covered']}")
    if tuple(missing) != tuple(expect["price_missing"]):
        failures.append(
            f"price-missing set is {missing}, expected exactly "
            f"{list(expect['price_missing'])}")
    if failures:
        raise GateFailure(
            "price coverage no longer matches the frozen snapshot - stop rather "
            "than silently proceed:\n  - " + "\n  - ".join(failures))
    return covered, coverage


def load_frozen_broker(universe, path=BROKER_PARQUET, prices=None, expectations=None):
    """Audit the raw broker harvest, then normalize into #1E's unit convention.

    The harvest stores plain rupiah with an explicit buy/sell split
    (blot/bval/slot/sval). #1E's frozen feature code reads value columns in
    BILLIONS and recovers lots from (value, average price), so this converts once
    and derives the average execution price implied by (value, lots).

    Nothing is invented: where a side has zero lots its implied average price is
    undefined and left NaN. That is faithful, but it has a consequence for
    feature fidelity that audit_net_lot_recovery() measures rather than assumes.

    Broker coverage is frozen separately from price coverage, because a broker
    harvest that quietly covered fewer names would shrink variants B/C/D while
    leaving A untouched -- making the variants incomparable without anything
    looking wrong.

    Raises before normalizing if the source audit fails.
    """
    expect = _expect(expectations)
    frame = pd.read_parquet(path)
    present = set(frame["ticker"].unique())
    covered = sorted(set(universe) & present)
    missing = sorted(set(universe) - present)

    coverage = {
        "fingerprint": file_fingerprint(path),
        "harvest_rows_total": int(len(frame)),
        "harvest_tickers_total": int(frame["ticker"].nunique()),
        "broker_covered_n": len(covered),
        "broker_missing": missing,
    }
    failures = []
    if len(covered) != expect["broker_covered"]:
        failures.append(
            f"{len(covered)} tickers with broker flow, expected {expect['broker_covered']}")
    if tuple(missing) != tuple(expect["broker_missing"]):
        failures.append(
            f"broker-missing set is {missing}, expected exactly "
            f"{list(expect['broker_missing'])}")
    if failures:
        raise GateFailure(
            "broker coverage no longer matches the frozen snapshot - variants "
            "B/C/D would silently shrink:\n  - " + "\n  - ".join(failures))

    rows = frame[frame["ticker"].isin(covered)].copy()
    source_audit = audit_broker_source(rows, prices)
    if not source_audit["passed"]:
        raise GateFailure(
            "broker source integrity audit failed - refusing to normalize "
            "incoherent inputs:\n  - " + "\n  - ".join(source_audit["failures"]))

    rows = rows.rename(columns={"broker": "broker_code"})
    rows["netval"] = rows["nval"] / RUPIAH_PER_BILLION
    rows["bval_b"] = rows["bval"] / RUPIAH_PER_BILLION
    rows["sval_b"] = rows["sval"] / RUPIAH_PER_BILLION

    buy_shares = rows["blot"] * SHARES_PER_LOT
    sell_shares = rows["slot"] * SHARES_PER_LOT
    rows["bavg"] = np.where(buy_shares > 0, rows["bval"] / buy_shares, np.nan)
    rows["savg"] = np.where(sell_shares > 0, rows["sval"] / sell_shares, np.nan)
    rows = rows.drop(columns=["bval", "sval"]).rename(
        columns={"bval_b": "bval", "sval_b": "sval"})

    keep = ["date", "ticker", "broker_code", "nlot", "blot", "slot",
            "netval", "bval", "sval", "bavg", "savg"]
    out = rows[keep].sort_values(["ticker", "date", "broker_code"]).reset_index(drop=True)
    return out, source_audit, coverage


def audit_net_lot_recovery(broker, panel, sample=None):
    """Measure how faithfully #1E's frozen feature code recovers source lots.

    This corrects a claim that would otherwise be wrong. #1E's
    _historical_net_lots() takes its exact buy/sell path ONLY when bval, sval,
    bavg and savg are all non-null AND both averages are > 0. A ONE-SIDED broker
    row -- bought but never sold that day, or the reverse -- has an undefined
    average on the empty side, so it falls through to the netval/close fallback
    and does NOT reproduce the source nlot exactly.

    Gate A does not fix that: changing _historical_net_lots would alter frozen
    #1E behaviour. It measures it instead, so the fidelity limit travels with
    the experiment as a stated caveat rather than an unexamined assumption.

    One-sided means exactly one side traded -- an XOR. A row where NEITHER side
    traded is zero-sided and is counted separately; folding it into "one-sided"
    would misdescribe a broker that simply did not trade that name that day.

    The frozen function is imported and actually executed here -- not
    paraphrased -- so what is measured is precisely what Gate B will compute.
    """
    from ml_v2_experiment_1 import _historical_net_lots

    merged = broker.merge(panel[["date", "ticker", "close"]],
                          on=["date", "ticker"], how="inner")
    if sample is not None and len(merged) > sample:
        merged = merged.sample(n=sample, random_state=17).reset_index(drop=True)

    recovered = _historical_net_lots(merged)
    live_mask = (
        recovered[["bval", "sval", "bavg", "savg"]].notna().all(axis=1)
        & recovered["bavg"].gt(0) & recovered["savg"].gt(0)
    )
    bought = recovered["blot"] > 0
    sold = recovered["slot"] > 0
    one_sided = bought ^ sold
    zero_sided = ~bought & ~sold
    gap = (recovered["net_lots"] - recovered["nlot"]).abs()

    exact_gap = gap[live_mask]
    fallback_gap = gap[~live_mask]
    return {
        "rows_audited": int(len(recovered)),
        "exact_split_path_rows": int(live_mask.sum()),
        "fallback_path_rows": int((~live_mask).sum()),
        "fallback_share": round(float((~live_mask).mean()), 6) if len(recovered) else 0.0,
        "one_sided_rows": int(one_sided.sum()),
        "one_sided_and_fallback": int((one_sided & ~live_mask).sum()),
        "zero_sided_rows": int(zero_sided.sum()),
        "zero_sided_and_fallback": int((zero_sided & ~live_mask).sum()),
        "exact_path_max_lot_gap": float(exact_gap.max()) if len(exact_gap) else 0.0,
        "exact_path_median_lot_gap": float(exact_gap.median()) if len(exact_gap) else 0.0,
        "fallback_path_max_lot_gap": float(fallback_gap.max()) if len(fallback_gap) else 0.0,
        "fallback_path_median_lot_gap": float(fallback_gap.median()) if len(fallback_gap) else 0.0,
        "caveat": "one-sided and zero-sided rows take the netval/close fallback in "
                  "frozen #1E code and do not reproduce source nlot exactly; this is "
                  "a Gate B feature-fidelity caveat, not a defect introduced here",
    }


def broker_provenance(broker, db_path=os.path.join(HERE, "neobdm.db")):
    """Bound what the harvest's broker values can honestly be said to prove.

    ROLE: DIAGNOSTIC PROVENANCE ONLY, and that is a load-bearing statement, not
    a disclaimer. neobdm.db is NOT a manifest-pinned execution input, so the
    claim "every path Gate A consumes comes from `inputs`" would be false if
    this could influence anything. It cannot, and the boundary is exact:

      - it returns a dict that is stored at report["broker_provenance"] and
        printed; nothing reads it back
      - it raises no GateFailure and feeds no assert_* function
      - it is handed a COPY-free read of `broker` and mutates nothing
      - neither panel_digest nor broker_digest covers any of its output, so the
        determinism check is indifferent to it

    Deleting neobdm.db therefore changes two lines of a printed report and
    nothing else. test_neobdm_is_diagnostic_only_and_cannot_alter_the_gate
    proves that by running the gate with the database present and absent and
    comparing the panel, the broker table and both digests.

    The path is passed explicitly by run_gate through
    `inputs["diagnostic_only"]["neobdm_db"]` rather than left as a default that
    silently resolves next to this file.

    Two DIFFERENT claims are at stake and must not be conflated:

      VALUE FIDELITY   - do the harvest's numbers match independently observed
                         ones? Checkable on the window where neobdm.db holds
                         live-scraped rows, and that is what this measures.

      PIT AVAILABILITY - were those numbers obtainable BEFORE the EOD(T) decision
                         cutoff they are used for? NOT checkable here.
                         broker_flow carries no observation timestamp, only a
                         trade date, so nothing in this data says when a row
                         became visible. Same-day post-session availability
                         remains an ASSUMPTION, in every window.

    Value reconciliation must never be read as evidence of the second claim.
    Matching numbers say the harvest recorded the same trades; they say nothing
    about publication timing.
    """
    import sqlite3

    ledger = {
        "harvest_first": broker["date"].min() if len(broker) else None,
        "harvest_last": broker["date"].max() if len(broker) else None,
        "value_reconciled_from": VALUE_RECONCILED_FROM,
        "pit_availability": "ASSUMED, NOT VERIFIED in any window - broker_flow "
                            "carries no observation timestamp, so availability "
                            "before the EOD(T) cutoff is an assumption",
    }
    if not os.path.exists(db_path):
        ledger["status"] = "neobdm.db absent - no value cross-check possible"
        return ledger

    conn = sqlite3.connect(db_path)
    try:
        live = pd.read_sql(
            "SELECT date, ticker, broker_code, bval, sval FROM broker_flow "
            "WHERE bval IS NOT NULL", conn)
    finally:
        conn.close()

    if live.empty:
        ledger["status"] = "no live-scraped rows in neobdm.db"
        return ledger

    overlap = broker[(broker["date"] >= live["date"].min())
                     & (broker["date"] <= live["date"].max())]
    merged = live.merge(overlap, on=["date", "ticker", "broker_code"],
                        how="inner", suffixes=("_db", "_hv"))

    # The two windows must not share a boundary date, or a reader sees the same
    # day claimed as both reconciled and unverified. The unverified window ends
    # on the last harvest date STRICTLY BEFORE reconciliation begins.
    earlier = broker.loc[broker["date"] < VALUE_RECONCILED_FROM, "date"]
    unverified_last = earlier.max() if len(earlier) else None

    ledger.update({
        "live_rows": int(len(live)),
        "live_window": [live["date"].min(), live["date"].max()],
        "matched_rows": int(len(merged)),
        "VALUE_RECONCILED_WINDOW": [VALUE_RECONCILED_FROM, ledger["harvest_last"]],
        "HISTORICAL_UNVERIFIED_WINDOW": [ledger["harvest_first"], unverified_last],
    })
    if merged.empty:
        ledger["status"] = "no overlapping (date,ticker,broker) rows"
        return ledger

    # neobdm.db rounds billions to 1 decimal place, so exact equality is the
    # wrong test. Agreement means the harvest value rounds to the stored one --
    # which is also the proof that the rupiah -> billions conversion is right.
    for col in ("bval", "sval"):
        db_value = merged[f"{col}_db"]
        harvest_rounded = merged[f"{col}_hv"].round(1)
        agree = np.isclose(harvest_rounded, db_value, atol=0.05, equal_nan=True)
        ledger[f"{col}_agreement_rate"] = round(float(agree.mean()), 6)
        ledger[f"{col}_disagreements"] = int((~agree).sum())

    ledger["status"] = "VALUE fidelity reconciled on the live window only"
    return ledger


# ── validated panel construction ──────────────

def load_normalized_artifacts(ledger_path=VOLUME_REPAIR_LEDGER_JSON,
                              factors_path=OBSERVED_BASIS_FACTOR_JSON):
    """Load the normalized layer. Absence is a failure, never an empty default.

    Defaulting to "no repairs, no regimes" would let the gate pass a harvest whose
    defects simply had not been measured yet, which is the exact failure mode the
    normalized layer exists to prevent.
    """
    missing = [q for q in (ledger_path, factors_path) if not os.path.exists(q)]
    if missing:
        raise GateFailure(
            "normalized-layer artifacts are missing: "
            + ", ".join(os.path.basename(q) for q in missing)
            + "\nRun: py normalize_market_data.py --verify")
    with open(ledger_path, encoding="utf-8") as fh:
        ledger = json.load(fh)
    with open(factors_path, encoding="utf-8") as fh:
        factors = json.load(fh)
    authorised = {(r["ticker"], r["date"]): r
                  for r in ledger.get("authorised_repairs", [])}
    regimes = {}
    legacy = []
    for regime in factors.get("regimes", []):
        if "start_date" not in regime or "end_date" not in regime:
            legacy.append(regime.get("ticker", "?"))
        regimes.setdefault(regime["ticker"], []).append(regime)
    if legacy:
        raise GateFailure(
            f"{os.path.basename(factors_path)} is a LEGACY single-regime "
            f"artifact: {len(legacy)} regime(s) carry no start_date/end_date "
            f"(first: {sorted(legacy)[:3]}). This gate requires the SEGMENTED "
            "artifact. The legacy file is refused rather than converted, "
            "because inventing a start date would silently reimpose the prefix "
            "assumption this contract exists to remove. "
            "Regenerate it: py normalize_market_data.py --verify")
    assert_no_regime_overlap(regimes)
    return {
        "authorised_repairs": authorised,
        "diagnostic_only": ledger.get("diagnostic_only", []),
        "regimes": regimes,
        "ledger_digest": _digest([f"{t}|{d}|{r['normalized_volume']}"
                                  for (t, d), r in sorted(authorised.items())]),
        "factor_digest": _digest(
            [f"{t}|{regime_bounds(r)[0]}|{regime_bounds(r)[1]}"
             f"|{regime_factor(r):.12g}|{r['classification']}"
             for t, entries in sorted(regimes.items()) for r in entries]),
    }


def load_authorized_repairs(candidate_path=VOLUME_REPAIR_CANDIDATES_JSON,
                            authorization_path=VOLUME_REPAIR_AUTHORIZATION_JSON,
                            basis_path=OBSERVED_BASIS_FACTOR_CANDIDATE_JSON):
    """The repair application contract. A file is not trusted for being named one.

    Authorisation is bound to one reviewed artifact by identity and to an EXACT
    key set. Every one of these is a stop, not a warning:

      the candidate artifact is not the one that was reviewed
      the detector found a hit the reviewer never saw
      an approved hit is no longer detected
      an approved hit's source file, raw volume or proposed volume moved

    The asymmetry matters. An extra hit is not "more repairs available", it is
    evidence the source changed under a review that no longer covers it.
    """
    for label, path in (("candidates", candidate_path),
                        ("authorization", authorization_path),
                        ("segmented basis", basis_path)):
        if not os.path.exists(path):
            raise GateFailure(
                f"{label} artifact is missing: {os.path.basename(path)}. "
                "Absence is never an empty default.")

    with open(candidate_path, encoding="utf-8") as fh:
        candidates = json.load(fh)
    with open(authorization_path, encoding="utf-8") as fh:
        auth = json.load(fh)

    physical = file_fingerprint_full(candidate_path)
    if auth.get("parent_candidate_sha256") != physical:
        raise GateFailure(
            "the authorization does not reference this candidate artifact "
            f"(authorised {auth.get('parent_candidate_sha256')}, on disk {physical}). "
            "Review is bound to the exact bytes that were reviewed.")
    semantic = _semantic_digest(candidates)
    if auth.get("parent_candidate_semantic_digest") != semantic:
        raise GateFailure(
            "the candidate artifact's semantic digest does not match the "
            f"authorization ({semantic} vs "
            f"{auth.get('parent_candidate_semantic_digest')})")

    detected = {(c["ticker"], c["date"]): c for c in candidates["candidates"]}
    approved = {(a["ticker"], a["date"]): a for a in auth["authorized"]}
    if auth.get("scope") != AUTHORIZATION_SCOPE:
        raise GateFailure(f"authorization scope must be {AUTHORIZATION_SCOPE!r}")
    if set(detected) != set(approved):
        extra = sorted(f"{t} {d}" for t, d in set(detected) - set(approved))
        missing = sorted(f"{t} {d}" for t, d in set(approved) - set(detected))
        raise GateFailure(
            "detected volume-repair candidates do not match the authorised key "
            f"set exactly. Newly detected and UNREVIEWED: {extra or 'none'}; "
            f"authorised but no longer detected: {missing or 'none'}. "
            "A new detector hit is a human-review event, never an extra repair.")

    for key, record in sorted(approved.items()):
        hit = detected[key]
        for field, ours in (("source_sha256", "source_sha256"),
                            ("raw_volume", "raw_volume"),
                            ("proposed_normalized_volume",
                             "proposed_normalized_volume"),
                            ("delta", "delta")):
            if hit[ours] != record[field]:
                raise GateFailure(
                    f"{key[0]} {key[1]}: {field} is {hit[ours]!r} but the "
                    f"authorization approved {record[field]!r}; the reviewed "
                    "repair is not the repair on disk")
        if record.get("authorised") is not True:
            raise GateFailure(f"{key[0]} {key[1]}: authorization record is not "
                              "marked authorised")

    # Stage-B diagnostics are structurally ineligible: they carry no proposed
    # volume at all, so there is nothing an authorisation could ever approve.
    for hit in candidates.get("stage_b_diagnostics_never_repaired", []):
        if (hit["ticker"], hit["date"]) in approved:
            raise GateFailure(
                f"{hit['ticker']} {hit['date']}: a stage-B diagnostic can never "
                "be authorised; it has no certified factor and no proposed volume")

    return {
        "authorised_repairs": {
            key: {"ticker": key[0], "date": key[1],
                  "raw_volume": record["raw_volume"],
                  "normalized_volume": record["proposed_normalized_volume"]}
            for key, record in approved.items()},
        "diagnostic_only": candidates.get("stage_b_diagnostics_never_repaired", []),
        "authorization_digest": _digest(
            [f"{t}|{d}|{r['proposed_normalized_volume']}|{r['source_sha256']}"
             for (t, d), r in sorted(approved.items())]),
        "parent_candidate_sha256": physical,
    }


def apply_volume_repairs(frame, artifacts):
    """Apply ledger repairs, and refuse any wrap-shaped row the ledger omits.

    The detector proposes; the ledger authorises. A new wrap must fail review
    rather than be silently corrected, so an unlisted candidate stops the gate.
    """
    out = frame.copy()
    authorised = artifacts["authorised_repairs"]
    applied, unlisted = [], []
    volume = np.array(out["volume"], dtype="float64", copy=True)
    keys = list(zip(out["ticker"], out["date"]))
    for i, key in enumerate(keys):
        hit = authorised.get(key)
        if hit is None:
            continue
        if float(volume[i]) != float(hit["raw_volume"]):
            raise GateFailure(
                f"ledger entry {key[0]} {key[1]} expects raw volume "
                f"{hit['raw_volume']:,} but the harvest holds {volume[i]:,.0f}; "
                "the ledger and the parquet disagree about the source")
        volume[i] = float(hit["normalized_volume"])
        applied.append(key)
    out["volume"] = volume

    seen = set(applied)
    for key in authorised:
        if key not in seen:
            unlisted.append(key)
    report = {
        "authorised_in_ledger": len(authorised),
        "applied": len(applied),
        "ledger_entries_not_present_in_harvest": len(unlisted),
        "diagnostic_only_not_repaired": len(artifacts["diagnostic_only"]),
        "ledger_digest": artifacts["ledger_digest"],
        "applied_keys": sorted(f"{t} {d}" for t, d in applied),
    }
    return out, report


def regime_bounds(regime):
    """The regime's inclusive interval on the session axis.

    A regime is an INTERVAL, not a cutoff. The superseded contract selected rows
    with `date <= regime_last_date`, which is only correct when the regime is a
    prefix of history; for a middle or suffix regime it also swept every clean
    session before it. Measured on the frozen 297, 9 of 18 regimes are not
    prefixes -- WINS alone carries six middle regimes -- so the cutoff form
    would have quarantined or rescaled data it was never meant to touch.
    """
    try:
        return regime["start_date"], regime["end_date"]
    except KeyError:
        raise GateFailure(
            f"{regime.get('ticker', '?')}: regime carries no start_date/end_date. "
            "This gate requires the SEGMENTED basis artifact. A legacy "
            "single-regime record is refused rather than converted, because "
            "inventing a start date would silently reimpose the prefix "
            "assumption this contract exists to remove.")


def rows_in_regime(frame, ticker, regime):
    start, end = regime_bounds(regime)
    return ((frame["ticker"] == ticker)
            & (frame["date"] >= start) & (frame["date"] <= end))


def assert_no_regime_overlap(regimes):
    """Overlapping regimes for one ticker are contradictory, never merged."""
    for ticker, entries in regimes.items():
        bounds = sorted(regime_bounds(r) for r in entries)
        for (a_start, a_end), (b_start, b_end) in zip(bounds, bounds[1:]):
            if b_start <= a_end:
                raise GateFailure(
                    f"{ticker}: basis regimes {a_start}..{a_end} and "
                    f"{b_start}..{b_end} overlap. Two factors cannot both hold on "
                    "one session, and silently keeping either one would discard a "
                    "measurement the artifact actually asserts.")


def load_candidate_artifacts(inputs):
    """The candidate contract's normalized layer, read ONLY from candidate paths.

    run_gate previously called load_normalized_artifacts() with no arguments,
    which silently defaulted to the two repo-root LEGACY artifacts that
    candidate_inputs() itself declares provenance-only -- so the whole
    candidates + authorisation contract was unreachable from execution, and
    load_authorized_repairs() had no production caller at all. The guard did not
    catch it because it inspected the returned dict rather than what was opened.
    This function is the single execution path, and it takes its paths from
    `inputs` so there is no default that can point somewhere else.
    """
    repairs = load_authorized_repairs(
        candidate_path=inputs["repair_candidates"],
        authorization_path=inputs["repair_authorization"],
        basis_path=inputs["basis_artifact"])

    with open(inputs["basis_artifact"], encoding="utf-8") as fh:
        factors = json.load(fh)
    regimes, legacy = {}, []
    for regime in factors.get("regimes", []):
        if "start_date" not in regime or "end_date" not in regime:
            legacy.append(regime.get("ticker", "?"))
        regimes.setdefault(regime["ticker"], []).append(regime)
    if legacy:
        raise GateFailure(
            f"{os.path.basename(inputs['basis_artifact'])} carries "
            f"{len(legacy)} legacy single-regime record(s); the candidate "
            "contract requires the SEGMENTED artifact")
    assert_no_regime_overlap(regimes)

    return {
        "mode": inputs["mode"],
        "authorised_repairs": repairs["authorised_repairs"],
        "diagnostic_only": repairs["diagnostic_only"],
        "regimes": regimes,
        "ledger_digest": repairs["authorization_digest"],
        "parent_candidate_sha256": repairs["parent_candidate_sha256"],
        "factor_digest": _digest(
            [f"{t}|{regime_bounds(r)[0]}|{regime_bounds(r)[1]}"
             f"|{regime_factor(r):.12g}|{r['classification']}"
             for t, entries in sorted(regimes.items()) for r in entries]),
        "sources": {
            "repair_candidates": inputs["repair_candidates"],
            "repair_authorization": inputs["repair_authorization"],
            "basis_artifact": inputs["basis_artifact"],
        },
    }


def basis_dispositions(artifacts, universe, mode=PRIMARY_MODE):
    """Split the measured regimes into what may be corrected and what may not.

    Both sides map a ticker to a LIST of regimes. A ticker may carry several,
    and they need not agree: a certified regime and a quarantined one can sit in
    the same ticker's history and must be handled independently.

    In PRIMARY mode NOTHING is harmonisable. A certified factor is an economic
    claim; whether it was knowable on the session it corrects is a
    point-in-time one, and the basis-invalid mask is reconstructed from the full
    frozen window. So PRIMARY quarantines every off-basis regime, certified or
    not, and harmonisation exists only under the explicitly retrospective mode.
    Without this branch PRIMARY_PIT_APPLIES_BASIS_HARMONISATION is decorative:
    run_gate would harmonise MLPT/RAJA/RMKE in the primary path regardless.
    """
    reconstructible, quarantined = {}, {}
    harmonisable = (mode == SECONDARY_MODE)
    for ticker, entries in artifacts["regimes"].items():
        if ticker not in universe:
            continue
        for regime in entries:
            certified = regime["classification"] in CERTIFIED_CLASSIFICATIONS
            target = (reconstructible if (certified and harmonisable)
                      else quarantined)
            target.setdefault(ticker, []).append(regime)
    return reconstructible, quarantined


def broker_basis_invalid_keys(frame, quarantined):
    """The (ticker, date) sessions whose BROKER features are unavailable.

    A basis regime is a statement about the broker LOT basis, not about the
    price series. The vendor's price and volume are mutually consistent
    throughout -- that is precisely why harmonise_broker_basis() scales lots and
    leaves price and volume alone -- so a session inside a regime has a perfectly
    usable price row and an unusable broker row.

    Deleting those price rows, which is what routing the price frame through
    quarantine_basis_regimes() did, made a broker-domain defect silently shrink
    the price panel. It also made the two variant families incomparable for a
    reason that has nothing to do with prices: variant A would lose the 1,425
    PRIMARY basis-invalid sessions even though price_audit.detect() never
    objected to a single one of them.

    So the domains are separated. Price validity is decided by price_audit
    alone; this function returns the broker-side unavailability mask, which
    Gate B applies as NaN on broker-derived features rather than as row
    deletion.
    """
    keys, detail = set(), {}
    if frame.empty or not quarantined:
        return {"keys": keys, "n_sessions": 0, "tickers": 0, "detail": {}}
    for ticker, entries in quarantined.items():
        mask = pd.Series(False, index=frame.index)
        for regime in entries:
            mask |= rows_in_regime(frame, ticker, regime)
        affected = frame.loc[mask, ["ticker", "date"]]
        detail[ticker] = int(len(affected))
        keys.update(zip(affected["ticker"], affected["date"]))
    return {"keys": keys, "n_sessions": len(keys), "tickers": len(quarantined),
            "detail": dict(sorted(detail.items()))}


def quarantine_basis_regimes(frame, quarantined):
    """Drop only the affected regime, never the whole ticker.

    Applied to the BROKER table only. The post-transition tail is verifiably
    clean (the ratio is exactly 1 there), so dropping the ticker outright would
    discard good data.

    Do NOT apply this to the price harvest. A broker-lot basis regime says
    nothing about whether the price row is valid, and price validity has exactly
    one authority -- price_audit.detect(). Use broker_basis_invalid_keys() to
    obtain the broker-side unavailability mask instead.
    """
    if frame.empty or not quarantined:
        return frame.copy(), {"tickers": 0, "rows_dropped": 0, "detail": {}}
    drop = pd.Series(False, index=frame.index)
    detail = {}
    for ticker, entries in quarantined.items():
        mask = pd.Series(False, index=frame.index)
        for regime in entries:
            mask |= rows_in_regime(frame, ticker, regime)
        detail[ticker] = int(mask.sum())
        drop |= mask
    return frame.loc[~drop].copy(), {
        "tickers": len(quarantined),
        "rows_dropped": int(drop.sum()),
        "detail": dict(sorted(detail.items())),
    }


def harmonise_broker_basis(broker, reconstructible):
    """Scale lots onto the basis the price and volume series already use.

    The vendor's price and volume are mutually consistent; only the broker lots
    sit on the other basis. So harmonising means multiplying LOTS by the factor
    and leaving price and volume alone. That keeps the return series continuous
    -- reconstructing as-traded prices instead would reintroduce the very split
    discontinuity the vendor already removed -- and an integer factor keeps lots
    integral. Rupiah values are basis-invariant and are never touched.
    """
    out = broker.copy()
    if not reconstructible:
        return out, {"tickers": 0, "rows_scaled": 0, "detail": {}}
    detail = {}
    for ticker, entries in reconstructible.items():
        rows = 0
        factors = []
        for regime in entries:
            factor = regime_factor(regime)
            mask = rows_in_regime(out, ticker, regime)
            n = int(mask.sum())
            if not n:
                continue
            for column in ("nlot", "blot", "slot"):
                # A DERIVED equivalent quantity, not an exchange lot. Raw lots
                # are integral by contract and stay int64 in the ingest layer;
                # requiring the same of `raw_lots * r` would reject a perfectly
                # valid rational factor -- r = 1/2 on an odd lot count -- for
                # being fractional rather than for being wrong. The float64
                # column carries the derived quantity exactly for the factors
                # this contract can certify.
                out.loc[mask, column] = (
                    out.loc[mask, column].to_numpy(dtype="float64") * factor)
            rows += n
            factors.append({"factor": factor,
                            "factor_exact": regime.get("candidate_factor_exact"),
                            "start_date": regime_bounds(regime)[0],
                            "end_date": regime_bounds(regime)[1],
                            "rows": n})
        if rows:
            detail[ticker] = {"rows": rows, "regimes": factors}
    return out, {
        "tickers": len(detail),
        "rows_scaled": int(sum(d["rows"] for d in detail.values())),
        "detail": dict(sorted(detail.items())),
    }


def implied_price_containment(ohlc, broker, tolerance=IMPLIED_PRICE_TOLERANCE):
    """Row-level check: each broker's implied VWAP must sit inside [low, high].

    Independent of volume, so it confirms the basis correction from a direction
    the aggregate ratio checks cannot. Both sides are checked -- a buy-side-only
    test would miss a sell-side basis error entirely.
    """
    if broker.empty or ohlc.empty:
        return {"rows_checked": 0, "violations": 0, "worst_tickers": []}
    ranges = ohlc[["ticker", "date", "low", "high"]]
    merged = broker.merge(ranges, on=["ticker", "date"], how="inner")
    if merged.empty:
        return {"rows_checked": 0, "violations": 0, "worst_tickers": []}

    low = merged["low"].to_numpy(dtype="float64")
    high = merged["high"].to_numpy(dtype="float64")
    valid_range = np.isfinite(low) & np.isfinite(high) & (low > 0)

    checked = 0
    offending = np.zeros(len(merged), dtype=bool)
    for lots_column, value_column in (("blot", "bval"), ("slot", "sval")):
        lots = merged[lots_column].to_numpy(dtype="float64")
        values = merged[value_column].to_numpy(dtype="float64")
        usable = valid_range & (lots > 0) & np.isfinite(values) & (values > 0)
        with np.errstate(invalid="ignore", divide="ignore"):
            price = np.where(usable, values / (lots * SHARES_PER_LOT), np.nan)
        bad = usable & ((price < low * (1.0 - tolerance))
                        | (price > high * (1.0 + tolerance)))
        checked += int(usable.sum())
        offending |= bad

    worst = []
    if offending.any():
        counts = merged.loc[offending, "ticker"].value_counts()
        worst = [f"{t}:{int(n)}" for t, n in counts.head(10).items()]
    return {
        "rows_checked": checked,
        "violations": int(offending.sum()),
        "worst_tickers": worst,
    }


def rupiah_value_view(broker):
    """Present either broker shape with `nval`/`bval`/`sval` in plain RUPIAH.

    Two shapes exist and they disagree about both names and units. The RAW
    harvest carries nval/bval/sval in rupiah; load_frozen_broker() converts to
    #1E's convention, where the net column is called `netval` and all three are
    in BILLIONS.

    cross_source_invariants() documented rupiah, was unit-tested against the raw
    shape, and was called by run_gate() with the normalized one. That is a
    KeyError on `nval` the moment Gate A actually runs, and no amount of loader
    testing could surface it: the defect is in how run_gate composes two
    functions, not inside either one.

    It cannot be papered over by reading `netval` instead, because the tolerance
    is 1 RUPIAH and comparing billions against it would silently pass anything
    under a billion rupiah. So the unit is restored explicitly here.

    Reconstruction is a float round-trip and therefore not bit-exact. Measured
    on the real 2,689,458-row candidate table the worst residue is 1.2e-4
    rupiah, against a 1 rupiah tolerance -- a margin of ~8,000x.
    test_rupiah_round_trip_stays_far_inside_the_tolerance measures it rather
    than asserting the bound from theory.
    """
    if "nval" in broker.columns:
        return broker
    if "netval" not in broker.columns:
        raise GateFailure(
            "broker frame carries neither `nval` (raw rupiah) nor `netval` "
            f"(#1E-normalized billions); columns are {list(broker.columns)}")
    out = broker.copy()
    out["nval"] = out["netval"].to_numpy(dtype="float64") * RUPIAH_PER_BILLION
    out["bval"] = out["bval"].to_numpy(dtype="float64") * RUPIAH_PER_BILLION
    out["sval"] = out["sval"].to_numpy(dtype="float64") * RUPIAH_PER_BILLION
    return out


def cross_source_invariants(ohlc, broker, artifacts):
    """Measure the cross-source identities the price-only detectors cannot see.

    A back-adjusted price series is internally consistent, so limit_violation,
    cross_ticker_dup and series_break are all silent on it. The mismatch is only
    visible against the broker table -- which is why these live here and not in
    price_audit.detect(), whose behaviour is frozen for Experiment #1E.

    Accepts either broker shape; see rupiah_value_view() for why the unit has to
    be restored rather than reinterpreted.
    """
    # The unit is restored FIRST. implied_price_containment divides a value by
    # lots*SHARES_PER_LOT and compares the result against [low, high] in rupiah;
    # handed the normalized frame it would compute a price 1e-9 too small and
    # report every single row as a containment violation. i6 is reported rather
    # than gated, so that would not have crashed anything -- it would have
    # quietly turned a real cross-source control into noise.
    broker = rupiah_value_view(broker)
    implied = implied_price_containment(ohlc, broker)

    totals = (broker.groupby(["ticker", "date"], sort=True)
              [["blot", "slot", "nlot", "bval", "sval", "nval"]].sum().reset_index())
    merged = totals.merge(ohlc[["ticker", "date", "volume"]], on=["ticker", "date"],
                          how="inner")
    buy_shares = merged["blot"].to_numpy(dtype="float64") * SHARES_PER_LOT
    sell_shares = merged["slot"].to_numpy(dtype="float64") * SHARES_PER_LOT
    volume = merged["volume"].to_numpy(dtype="float64")

    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(buy_shares > 0, volume / buy_shares, np.nan)
    lot_gap = np.abs(buy_shares - volume) / SHARES_PER_LOT
    off = (np.isfinite(ratio)
           & (np.abs(ratio - 1.0) > BASIS_RATIO_TOLERANCE)
           & (lot_gap > BASIS_MIN_LOT_GAP))
    residual = np.isfinite(ratio) & ~off & (buy_shares != volume)

    delta = buy_shares - volume
    with np.errstate(invalid="ignore"):
        wraps = np.isfinite(delta) & (np.abs(np.remainder(delta, VOLUME_WRAP_MODULUS)) < 1.0) \
            & (np.abs(delta) >= VOLUME_WRAP_MODULUS)

    report = {
        "ticker_days": int(len(merged)),
        "i1_buy_vs_volume": int((buy_shares != volume).sum()),
        "i2_sell_vs_volume": int((sell_shares != volume).sum()),
        "i1_basis_scale_breaks": int(off.sum()),
        "i1_rounding_deficits": int(residual.sum()),
        "i3b_sum_nlot_nonzero": int((merged["nlot"].to_numpy() != 0).sum()),
        "i4_value_conservation": int(
            (np.abs(merged["bval"].to_numpy() - merged["sval"].to_numpy())
             > NVAL_TOLERANCE_RUPIAH).sum()),
        "i5_sum_nval_nonzero": int(
            (np.abs(merged["nval"].to_numpy()) > NVAL_TOLERANCE_RUPIAH).sum()),
        "unrepaired_volume_wraps": int(wraps.sum()),
        "basis_scale_break_tickers": sorted(
            set(merged.loc[off, "ticker"].tolist())) if off.any() else [],
        "unrepaired_wrap_keys": sorted(
            f"{t} {d}" for t, d in zip(merged.loc[wraps, "ticker"],
                                       merged.loc[wraps, "date"])) if wraps.any() else [],
        "i6_implied_price_outside_range": implied["violations"],
        "i6_rows_checked": implied["rows_checked"],
        "i6_worst_tickers": implied["worst_tickers"],
        "ledger_digest": artifacts["ledger_digest"],
        "factor_digest": artifacts["factor_digest"],
    }
    return report


def assert_cross_source_integrity(report):
    """Hard gate on the identities the source actually guarantees.

    FATAL   an unrepaired volume wrap, or a surviving basis-scale break. Both
            mean a feature column would mix two scales.
    FATAL   value conservation, the control that proves lots rather than values
            are the broken field.
    REPORTED lot-coverage deficits and sum(nlot) != 0. The source does not
            guarantee complete broker coverage, so these are measured, not fatal.
    """
    failures = []
    if report["unrepaired_volume_wraps"]:
        failures.append(
            f"{report['unrepaired_volume_wraps']} volume wrap(s) not covered by the "
            f"repair ledger: {', '.join(report['unrepaired_wrap_keys'][:5])}")
    if report["i1_basis_scale_breaks"]:
        failures.append(
            f"{report['i1_basis_scale_breaks']} ticker-day(s) on a mismatched basis "
            f"survived quarantine: {', '.join(report['basis_scale_break_tickers'][:8])}")
    if report["i4_value_conservation"]:
        failures.append(
            f"{report['i4_value_conservation']} ticker-day(s) break value conservation "
            "(sum(bval) != sum(sval))")
    if report["i5_sum_nval_nonzero"]:
        failures.append(
            f"{report['i5_sum_nval_nonzero']} ticker-day(s) where sum(nval) != 0")
    if failures:
        raise GateFailure(
            "cross-source integrity failed:\n  - " + "\n  - ".join(failures))
    return True


def build_validated_panel(full_harvest, universe, calendar, horizons=HORIZONS,
                          lags=LAGS):
    """detect() on the FULL harvest, then narrow to the approved universe.

    Order matters and is the point of this function. cross_ticker_dup compares
    OHLCV across tickers, so it must see the whole cross-section: an approved
    ticker cloned against a name OUTSIDE the universe looks unique the moment
    the other half of the pair is filtered away, and the contamination sails
    through. So the detector runs first, on everything, and only then are the
    suspect keys and the model panel restricted to the approved names.

    Out-of-universe tickers contribute detection evidence and calendar dates.
    They never enter the returned panel.

    Returns (panel, flagged_full, universe_rows).
    """
    full = full_harvest.sort_values(["ticker", "date"]).reset_index(drop=True)
    flagged = detect(full)
    quarantined = set(zip(flagged.loc[flagged["suspect"], "date"],
                          flagged.loc[flagged["suspect"], "ticker"]))

    universe_rows = full[full["ticker"].isin(universe)].reset_index(drop=True)
    keep = [(d, t) not in quarantined
            for d, t in zip(universe_rows["date"], universe_rows["ticker"])]
    clean = universe_rows[keep].reset_index(drop=True)

    panel = add_forward_returns(clean, calendar, horizons=horizons, open_anchored=True)
    if lags:
        panel = add_lagged_returns(panel, calendar, lags=lags)
    return panel, flagged, universe_rows


def open_anchor_diagnostics(panel, calendar, horizons=HORIZONS):
    """Separate open-anchor rejections from contiguity/end-of-window attrition.

    A raw count of NaN fwd_oo_* says how much was lost but not why, and the
    causes mean very different things: a failed open anchor is a data-quality
    rejection (a fabricated or out-of-band open), while a contiguity break is a
    calendar fact (a suspension, or simply running off the end of the panel).

    The open-anchor half reuses price_audit._open_anchor_valid directly -- the
    real contract, not a paraphrase. The contiguity half is plain position
    arithmetic on the same unfiltered date axis add_forward_returns uses. The
    remainder is attributed by elimination to the close-step band chain rather
    than re-deriving that chain here, which would be exactly the divergent copy
    this module exists to avoid.
    """
    frame = panel.sort_values(["ticker", "date"]).reset_index(drop=True)
    group = frame.groupby("ticker")
    open_valid = _open_anchor_valid(frame, group).fillna(False).astype(bool)

    positions = {d: i for i, d in enumerate(calendar)}
    pos = frame["date"].map(positions)
    pos_group = pos.groupby(frame["ticker"])

    report = {
        "rows": int(len(frame)),
        "rows_with_invalid_open_anchor": int((~open_valid).sum()),
        "rows_with_valid_open_anchor": int(open_valid.sum()),
        "per_horizon": {},
    }

    entry_ok = open_valid.groupby(frame["ticker"]).shift(-1).fillna(False).astype(bool)
    for h in horizons:
        exit_ok = open_valid.groupby(frame["ticker"]).shift(-(1 + h)).fillna(False).astype(bool)
        contiguous = ((pos_group.shift(-(1 + h)) - pos) == (1 + h)).fillna(False).astype(bool)
        window_exists = pos_group.shift(-(1 + h)).notna()
        invalid = frame[f"fwd_oo_{h}"].isna()

        report["per_horizon"][f"fwd_oo_{h}"] = {
            "non_executable": int(invalid.sum()),
            "entry_open_invalid": int((invalid & ~entry_ok).sum()),
            "exit_open_invalid": int((invalid & ~exit_ok).sum()),
            "window_past_end_of_series": int((invalid & ~window_exists).sum()),
            "non_contiguous_calendar": int((invalid & window_exists & ~contiguous).sum()),
            "close_step_or_other_by_elimination": int(
                (invalid & entry_ok & exit_ok & contiguous).sum()),
        }
    return report


def integrity_checks(universe_rows, panel, flagged_full, universe, horizons=HORIZONS):
    """Post-quarantine structural report. Assertion happens in assert_structural."""
    report = {}

    in_universe = flagged_full["ticker"].isin(universe)
    report["detector_counts_full_harvest"] = {
        name: int(flagged_full[name].sum())
        for name in ("limit_violation", "cross_ticker_dup", "series_break")
    }
    report["detector_counts_universe"] = {
        name: int((flagged_full[name] & in_universe).sum())
        for name in ("limit_violation", "cross_ticker_dup", "series_break")
    }
    report["suspect_rows_full_harvest"] = int(flagged_full["suspect"].sum())
    report["suspect_rows_universe"] = int((flagged_full["suspect"] & in_universe).sum())

    # How many universe rows were quarantined ONLY because their clone lived
    # outside the universe -- the contamination a pre-filtered detector misses.
    dup_only_outside = 0
    dups = flagged_full[flagged_full["cross_ticker_dup"]]
    if len(dups):
        key_cols = ["date"] + OHLCV
        universe_flag = dups["ticker"].isin(universe)
        grouped = dups.groupby(key_cols)["ticker"].transform("nunique")
        inside_count = universe_flag.groupby(
            [dups[c] for c in key_cols]).transform("sum")
        dup_only_outside = int(((universe_flag) & (inside_count == 1) & (grouped > 1)).sum())
    report["universe_rows_dup_against_outside_only"] = dup_only_outside

    scoped = flagged_full[in_universe]
    reason_parts = []
    for name in ("limit_violation", "cross_ticker_dup", "series_break"):
        reason_parts.append(np.where(scoped[name], name, ""))
    reasons = pd.Series(
        ["+".join(p for p in parts if p) for parts in zip(*reason_parts)],
        index=scoped.index, dtype=object) if len(scoped) else pd.Series(dtype=object)
    report["suspect_reason_breakdown_universe"] = (
        reasons[scoped["suspect"].to_numpy()].value_counts().to_dict()
        if len(scoped) else {})

    report["rows_in"] = int(len(universe_rows))
    report["rows_after_quarantine"] = int(len(panel))
    report["tickers_after_quarantine"] = int(panel["ticker"].nunique())
    report["out_of_universe_rows_in_panel"] = int((~panel["ticker"].isin(universe)).sum())

    report["duplicate_date_ticker"] = int(panel.duplicated(["date", "ticker"]).sum())
    report["blank_dates"] = int(invalid_date_mask(panel["date"]).sum())
    report["nonpositive_close"] = int((panel["close"] <= 0).sum())
    report["open_outside_high_low"] = int(
        ((panel["open"] < panel["low"]) | (panel["open"] > panel["high"])).sum())

    # Cross-ticker contamination must not survive quarantine WITHIN the panel.
    surviving_dup = panel.duplicated(["date"] + OHLCV, keep=False) \
        & panel[OHLCV].notna().all(axis=1)
    report["surviving_cross_ticker_dup"] = int(surviving_dup.sum())

    ordered = panel.sort_values(["ticker", "date"])
    report["dates_monotonic_per_ticker"] = bool(
        ordered.groupby("ticker")["date"].apply(lambda s: s.is_monotonic_increasing).all())

    # Ticker-boundary integrity: a label may never be computed across two
    # different tickers. Every ticker's FIRST row cannot have a lag, and its
    # LAST row cannot have a forward label.
    first_rows = ordered.groupby("ticker").head(1)
    last_rows = ordered.groupby("ticker").tail(1)
    report["first_row_lag_leak"] = (
        int(first_rows["lag_1"].notna().sum()) if "lag_1" in panel else 0)
    report["last_row_forward_leak"] = int(last_rows[f"fwd_oo_{horizons[0]}"].notna().sum())

    label_cols = [f"fwd_oo_{h}" for h in horizons]
    report["label_coverage"] = {
        col: {
            "valid": int(panel[col].notna().sum()),
            "non_executable": int(panel[col].isna().sum()),
            "valid_pct": round(float(panel[col].notna().mean()), 6),
        }
        for col in label_cols
    }
    # DIAGNOSTIC ONLY, never an invariant. Each horizon validates its OWN exit
    # open, so a longer horizon can legitimately be valid where a shorter one is
    # not -- if H2's particular exit open is out of band while H3's is fine.
    report["valid_count_decreases_with_horizon"] = bool(
        all(panel[label_cols[i]].notna().sum() >= panel[label_cols[i + 1]].notna().sum()
            for i in range(len(label_cols) - 1)))

    band = panel[label_cols].abs().max().max()
    report["max_abs_label"] = None if pd.isna(band) else round(float(band), 6)
    return report


def assert_structural_integrity(report):
    """Hard gate on invariants no downstream code could survive.

    Deliberately excluded: open_outside_high_low and any other open-anchor
    condition. Those rows are ALLOWED to remain -- the contract withholds their
    executable labels instead. Deleting them would be a repair.
    """
    failures = []
    if report["duplicate_date_ticker"]:
        failures.append(f"{report['duplicate_date_ticker']} duplicate (date,ticker) rows")
    if report["blank_dates"]:
        failures.append(f"{report['blank_dates']} blank/unparseable/impossible dates")
    if report["nonpositive_close"]:
        failures.append(f"{report['nonpositive_close']} non-positive closes")
    if report["surviving_cross_ticker_dup"]:
        failures.append(
            f"{report['surviving_cross_ticker_dup']} cross-ticker identical OHLCV rows "
            "survived quarantine")
    if report["out_of_universe_rows_in_panel"]:
        failures.append(
            f"{report['out_of_universe_rows_in_panel']} out-of-universe rows leaked "
            "into the model panel")
    if report["first_row_lag_leak"]:
        failures.append(
            f"{report['first_row_lag_leak']} ticker-boundary lag leak(s): a first row "
            "carries a lag it cannot have")
    if report["last_row_forward_leak"]:
        failures.append(
            f"{report['last_row_forward_leak']} ticker-boundary forward leak(s): a last "
            "row carries a forward label it cannot have")
    if not report["dates_monotonic_per_ticker"]:
        failures.append("dates are not monotonically increasing within every ticker")
    if failures:
        raise GateFailure(
            "post-quarantine structural integrity failed:\n  - " + "\n  - ".join(failures))
    return True


def start_of_history(universe_rows, panel):
    """First-appearance statistics from the RAW source, before quarantine.

    Computed on the raw covered rows on purpose: if a ticker's opening row
    happens to be quarantined, the post-quarantine first date would misreport
    when the SOURCE history actually begins, and a reader would mistake a
    dropped bad bar for a later listing. Both views are reported so the
    difference is visible rather than assumed away.

    Neither view is a listing date. No point-in-time membership data exists in
    this repo, so first appearance is the only proxy available -- and for most
    names it records when COLLECTION started, not when the stock listed.
    """
    raw_firsts = universe_rows.groupby("ticker")["date"].min()
    clean_firsts = panel.groupby("ticker")["date"].min()
    floor = raw_firsts.min()
    shifted = int((clean_firsts > raw_firsts.reindex(clean_firsts.index)).sum())
    return {
        "source_harvest_floor": floor,
        "source_tickers_starting_at_floor": int((raw_firsts == floor).sum()),
        "source_tickers_starting_later": int((raw_firsts > floor).sum()),
        "source_first_date_histogram": raw_firsts.value_counts().sort_index().head(12).to_dict(),
        "post_quarantine_first_differs": shifted,
        "post_quarantine_floor": clean_firsts.min(),
        "bars_per_ticker_min": int(panel.groupby("ticker").size().min()),
        "bars_per_ticker_median": int(panel.groupby("ticker").size().median()),
        "bars_per_ticker_max": int(panel.groupby("ticker").size().max()),
    }


def candidate_inputs(candidate_dir=CANDIDATE_DIR, mode=PRIMARY_MODE):
    """The Experiment #1F execution inputs, as explicit paths.

    Asymmetric on purpose, and the asymmetry is measured rather than assumed:

      OHLC is FULL-MARKET, and experiment-owned. detect()'s cross_ticker_dup
      compares OHLCV ACROSS tickers, so an approved name cloned against a name
      outside the universe looks unique the moment the other half is filtered
      away -- narrowing this input would blind the detector. What the candidate
      copy changes is custody, not content: the same 936-ticker cross-section,
      byte-identical, living where no other session can rewrite it.

      BROKER is the isolated 297. load_frozen_broker() filters to
      `set(universe) & present` on its first statement and every downstream
      broker computation -- audit_broker_source, cross_source_invariants,
      implied_price_containment, net-lot recovery -- runs on that filtered
      frame. No broker invariant reads an out-of-universe row. Keeping the
      full-market table as the execution input would therefore let an
      irrelevant out-of-universe refresh invalidate a frozen 297-name
      experiment while changing nothing it actually consumes.

    The full-market broker table remains pinned as PROVENANCE, together with
    the proof that v2 reproduces its in-universe subset exactly.
    """
    if mode not in (PRIMARY_MODE, SECONDARY_MODE):
        raise GateFailure(f"unknown mode {mode!r}")
    return {
        "mode": mode,
        "expectations": dict(PRODUCTION_EXPECTATIONS),
        # The repository the pinned identities are computed against. Declared,
        # not assumed: tracked-content identity is meaningless without saying
        # WHICH repository's object store is authoritative. `code_root` is the
        # repo the pinned implementation lives in; `git_root` the repo a tracked
        # execution input lives in. In production both are this worktree.
        "git_root": HERE,
        "code_root": HERE,
        "ohlc_parquet": os.path.join(candidate_dir, "ohlc_full_market.parquet"),
        "broker_parquet": os.path.join(candidate_dir, "broker_daily_v2.parquet"),
        # CONSUMED, not merely declared. run_gate passes this to
        # resolve_universe(require_frozen=True), so the pinned path is the only
        # universe the gate can read: no repo-root default, no workbook fallback.
        "universe_json": os.path.join(HERE, "experiment_1f_universe.json"),
        "reviewed_manifest": REVIEWED_MANIFEST_V3_JSON,
        "source_manifest": os.path.join(candidate_dir, "source_manifest.json"),
        "repair_candidates": os.path.join(candidate_dir,
                                          "volume_repair_candidates.json"),
        "repair_authorization": os.path.join(candidate_dir,
                                             "volume_repair_authorization.json"),
        "basis_artifact": os.path.join(candidate_dir,
                                       "observed_basis_factor_candidate.json"),
        # DIAGNOSTIC ONLY, and separated from execution inputs for that reason.
        # neobdm.db is read by broker_provenance() to bound a VALUE-fidelity
        # claim; it cannot change pass/fail, the panel, the broker table, or
        # either digest. It is therefore deliberately NOT manifest-pinned -- but
        # it is named here rather than resolved from a default, so "every path
        # is declared" stays literally true.
        "diagnostic_only": {
            "neobdm_db": os.path.join(HERE, "neobdm.db"),
        },
        "provenance_only": {
            # The frozen sources these candidate copies were taken from. They
            # live in the SHARED checkout, not in this worktree -- naming the
            # worktree path here would make the guard compare against a file
            # that does not exist and can never be opened.
            "shared_checkout_ohlc_parquet": os.path.join(SHARED_ROOT, "ohlc.parquet"),
            "shared_checkout_broker_parquet": os.path.join(SHARED_ROOT,
                                                           "broker_daily.parquet"),
            "worktree_ohlc_parquet": OHLC_PARQUET,
            "full_market_broker_parquet": BROKER_PARQUET,
            "legacy_repair_ledger": VOLUME_REPAIR_LEDGER_JSON,
            "legacy_basis_artifact": OBSERVED_BASIS_FACTOR_JSON,
            # The v2 two-file manifest. run_gate now verifies manifest-v3 and
            # never opens this, so listing it here turns "we stopped using it"
            # into something traced_open_paths can actually falsify.
            "legacy_two_file_input_manifest": INPUT_MANIFEST_JSON,
        },
    }


def assert_candidate_mode_reads_no_legacy_artifact(inputs):
    """Declared execution inputs must not name a legacy production artifact.

    This checks the DECLARATION only. It is necessary and not sufficient: an
    earlier version of run_gate declared these inputs, passed this guard, and
    then called load_normalized_artifacts() with no arguments -- reading the
    legacy root artifacts anyway. Use `traced_open_paths` to assert on what a
    call actually opens; a declaration cannot police a hard-coded default.
    """
    legacy = set(inputs["provenance_only"].values())
    for key, value in inputs.items():
        if key in ("mode", "provenance_only", "expectations", "diagnostic_only"):
            continue
        if not isinstance(value, str):
            continue
        if value in legacy:
            raise GateFailure(
                f"candidate input {key} points at the legacy artifact {value}; "
                "legacy root artifacts are provenance, never execution inputs")
    return True


@contextlib.contextmanager
def traced_open_paths(sink):
    """Record every filesystem path opened inside the block.

    The behavioural counterpart to the declaration guard above: it observes what
    code DOES rather than what it says, which is the only way to catch a default
    argument silently reintroducing a legacy path.
    """
    import builtins
    real_open = builtins.open

    def traced(file, *args, **kwargs):
        try:
            sink.append(os.path.abspath(os.fspath(file)))
        except TypeError:
            pass                                  # fd or path-like we cannot resolve
        return real_open(file, *args, **kwargs)

    builtins.open = traced
    try:
        yield sink
    finally:
        builtins.open = real_open


def assert_no_legacy_artifact_was_opened(opened, inputs):
    """Fail if any legacy root artifact was actually read."""
    legacy = {os.path.abspath(p) for p in inputs["provenance_only"].values()}
    touched = sorted({p for p in opened if p in legacy})
    if touched:
        raise GateFailure(
            "the candidate contract OPENED legacy artifacts: "
            + ", ".join(os.path.basename(p) for p in touched)
            + ". Declaring candidate inputs is not enough; a hard-coded default "
              "elsewhere reintroduced a provenance-only path.")
    return True


def run_gate(xlsx_path=UNIVERSE_XLSX, refreeze=False, net_lot_sample=None,
             inputs=None, mode=PRIMARY_MODE, establish_manifest=False):
    """Full Gate A pass. Returns (panel, broker, report).

    `establish_manifest` survives only to REFUSE. It was a CLI flag that let a
    Gate A run re-pin the very inputs it had just read; leaving the parameter in
    place with a hard refusal is louder than deleting it, because any caller
    still passing it gets an explanation instead of a silent no-op.

    Every path this consumes comes from `inputs`. Nothing here falls back to a
    module default: the previous version declared candidate inputs, passed the
    declaration guard, and then read the legacy root artifacts anyway because
    two calls used their own defaults.

    `report["orchestration"]` records the ordered execution stages and the file
    each one actually consumed, so the call path is an audited output rather
    than something a reader has to reconstruct from the source.
    """
    if establish_manifest:
        raise ManifestEstablishmentRefused(
            "Gate A cannot establish or re-establish its own manifest. A run "
            "that pins its own inputs records what it just read, not what a "
            "human reviewed, so the pin stops being a control. Establishment is "
            "a separate act performed by experiment_1f_manifest.py, valid only "
            "after the implementation is committed and the semantic code tree "
            "is clean -- conditions Gate A cannot check about itself mid-run.")

    inputs = inputs or candidate_inputs(mode=mode)
    if inputs["mode"] != mode:
        raise GateFailure(
            f"mode {mode!r} was requested but the execution inputs were built "
            f"for {inputs['mode']!r}; the mode decides whether certified basis "
            "regimes are harmonised, so a silent disagreement would change the "
            "data contract without changing the declaration")
    assert_candidate_mode_reads_no_legacy_artifact(inputs)
    expectations = inputs.get("expectations")
    stages = []

    # 1. Hash the EXACT BYTES of all seven execution inputs, before any semantic
    #    application, so the manifest pin covers what was reviewed rather than
    #    what a parser reconstructed.
    fingerprints = execution_input_fingerprints(inputs)
    stages.append({"stage": "execution_input_fingerprints",
                   "consumed": sorted(fp["abspath"] for fp in fingerprints.values()),
                   "pinned_inputs": len(fingerprints)})

    # 2. The normalized layer, loaded through the single candidate path.
    artifacts = load_candidate_artifacts(inputs)
    stages.append({"stage": "load_candidate_artifacts",
                   "consumed": sorted(artifacts["sources"].values()),
                   "authorised_repairs": len(artifacts["authorised_repairs"])})

    # 3. The manifest, verified in full and BEFORE anything semantic is built.
    #    Everything recomputable is recomputed; nothing is taken on the
    #    manifest's own word.
    manifest = verify_reviewed_manifest_v3(inputs, fingerprints,
                                           artifacts=artifacts, mode=mode)
    stages.append({"stage": "verify_reviewed_manifest_v3",
                   "consumed": manifest["manifest_path"],
                   "checks": manifest["checks_performed"],
                   "can_establish": False})

    tickers, universe_audit = resolve_universe(
        xlsx_path, frozen_path=inputs["universe_json"], refreeze=refreeze,
        require_frozen=True, expectations=expectations)
    stages.append({"stage": "resolve_universe",
                   "consumed": universe_audit.get("consumed_path"),
                   "declared": os.path.abspath(inputs["universe_json"])})

    full_harvest, calendar, ohlc_fingerprint = load_full_harvest(
        inputs["ohlc_parquet"])
    if ohlc_fingerprint["sha256"] != fingerprints["ohlc_full_market_parquet"]["sha256"]:
        raise GateFailure(
            "the OHLC parquet changed between fingerprinting and loading; the "
            "verified manifest describes bytes that are no longer on disk")
    stages.append({"stage": "load_full_harvest",
                   "consumed": os.path.abspath(inputs["ohlc_parquet"]),
                   "sha256": ohlc_fingerprint["sha256"]})
    covered, price_cov = price_coverage(full_harvest, tickers, ohlc_fingerprint,
                                        calendar, expectations=expectations)
    broker, broker_source, broker_cov = load_frozen_broker(
        tickers, path=inputs["broker_parquet"], prices=full_harvest,
        expectations=expectations)
    if broker_cov["fingerprint"]["sha256"] != \
            fingerprints["broker_daily_v2_parquet"]["sha256"]:
        raise GateFailure(
            "the broker parquet changed between fingerprinting and loading; the "
            "verified manifest describes bytes that are no longer on disk")
    stages.append({"stage": "load_frozen_broker",
                   "consumed": os.path.abspath(inputs["broker_parquet"]),
                   "sha256": broker_cov["fingerprint"]["sha256"]})

    full_harvest, repair_report = apply_volume_repairs(full_harvest, artifacts)
    reconstructible, quarantined = basis_dispositions(artifacts, set(covered), mode=mode)
    stages.append({"stage": "basis_dispositions", "mode": mode,
                   "harmonisable_tickers": sorted(reconstructible),
                   "quarantined_tickers": sorted(quarantined)})

    broker, harmonise_report = harmonise_broker_basis(broker, reconstructible)

    # DOMAIN SEPARATION. The price harvest is NOT filtered here: a broker-lot
    # basis regime is not a price defect, and price validity has exactly one
    # authority, price_audit.detect(), which runs below on the untouched frame.
    basis_invalid = broker_basis_invalid_keys(full_harvest, quarantined)
    broker, broker_quarantine = quarantine_basis_regimes(broker, quarantined)
    stages.append({"stage": "broker_basis_invalid_keys",
                   "price_rows_deleted": 0,
                   "broker_invalid_sessions": basis_invalid["n_sessions"],
                   "broker_rows_dropped": broker_quarantine["rows_dropped"]})

    cross_source = cross_source_invariants(full_harvest, broker, artifacts)
    cross_source["volume_repairs"] = repair_report
    cross_source["basis_harmonised"] = harmonise_report
    cross_source["basis_quarantined_broker_rows"] = broker_quarantine["rows_dropped"]
    cross_source["basis_quarantined_broker_detail"] = broker_quarantine["detail"]
    cross_source["broker_basis_invalid_sessions"] = basis_invalid["n_sessions"]
    cross_source["broker_basis_invalid_detail"] = basis_invalid["detail"]
    cross_source["price_rows_deleted_for_broker_basis"] = 0
    assert_cross_source_integrity(cross_source)

    raw_audit = audit_raw_ohlc(full_harvest, calendar, scope="full harvest")
    panel, flagged_full, universe_rows = build_validated_panel(
        full_harvest, covered, calendar)
    stages.append({"stage": "build_validated_panel",
                   "price_validity_authority": "price_audit.detect",
                   "rows": int(len(panel))})
    price_cov["universe_rows"] = int(len(universe_rows))

    integrity = integrity_checks(universe_rows, panel, flagged_full, covered)
    integrity["broker_invalid_price_valid_rows_retained"] = int(
        sum(1 for t, d in zip(panel["ticker"], panel["date"])
            if (t, d) in basis_invalid["keys"]))
    assert_structural_integrity(integrity)

    report = {
        "orchestration": stages,
        "mode": mode,
        "universe": universe_audit,
        "input_manifest": manifest,
        "broker_basis_validity": {
            "policy": ("broker-domain unavailability, applied as NaN on "
                       "broker-derived features by Gate B. Never a price-row "
                       "deletion: price validity is decided by price_audit "
                       "alone."),
            "invalid_sessions": basis_invalid["n_sessions"],
            "detail": basis_invalid["detail"],
            # The mask itself, exported so Gate B can NaN broker features on
            # exactly these keys. A count is not actionable; the key set is.
            "invalid_keys": sorted(f"{t} {d}" for t, d in basis_invalid["keys"]),
            "price_rows_deleted": 0,
            "price_valid_and_broker_invalid_rows_in_panel":
                integrity["broker_invalid_price_valid_rows_retained"],
        },
        "price_coverage": price_cov,
        "broker_coverage": broker_cov,
        "raw_ohlc_audit": raw_audit,
        "cross_source": cross_source,
        "broker_source_audit": broker_source,
        "integrity": integrity,
        "open_anchor": open_anchor_diagnostics(panel, calendar),
        "history": start_of_history(universe_rows, panel),
        "net_lot_recovery": audit_net_lot_recovery(broker, panel, sample=net_lot_sample),
        # DIAGNOSTIC. Explicit path, explicit role; see broker_provenance().
        "broker_provenance": dict(
            broker_provenance(
                broker, db_path=inputs["diagnostic_only"]["neobdm_db"]),
            role="DIAGNOSTIC PROVENANCE ONLY -- not manifest-pinned, cannot "
                 "alter pass/fail, the panel, the broker table or either digest",
            db_path=inputs["diagnostic_only"]["neobdm_db"]),
        "broker_rows": int(len(broker)),
        "broker_tickers": int(broker["ticker"].nunique()),
        "broker_codes": int(broker["broker_code"].nunique()),
        "panel_digest": frame_digest(
            panel, ["date", "ticker", "open", "high", "low", "close", "volume"]
            + [f"fwd_oo_{h}" for h in HORIZONS] + ["gap_1"]),
        "broker_digest": frame_digest(
            broker, ["date", "ticker", "broker_code", "netval", "bval", "sval"]),
    }
    return panel, broker, report


def print_report(report):
    universe = report["universe"]
    price_cov = report["price_coverage"]
    broker_cov = report["broker_coverage"]
    raw = report["raw_ohlc_audit"]
    integrity = report["integrity"]
    anchors = report["open_anchor"]
    history = report["history"]
    provenance = report["broker_provenance"]
    source = report["broker_source_audit"]
    lots = report["net_lot_recovery"]

    print("=" * 78)
    print("EXPERIMENT #1F - GATE A: DATA-INTEGRATION VALIDATION")
    print("=" * 78)

    print("\n[1] APPROVED UNIVERSE")
    print(f"  loaded from       : {universe.get('loaded_from', 'frozen artifact')}")
    print(f"  source            : {universe['source_path']}")
    print(f"  sheet / header    : {universe['sheet']!r} / {universe['header_cell']!r}")
    print(f"  distinct tickers  : {universe['distinct_tickers']} (expected {EXPECTED_UNIVERSE_SIZE})")
    print(f"  digest            : {universe['universe_digest']} (accepted {ACCEPTED_UNIVERSE_DIGEST})")
    print(f"  non-string cells  : {universe['non_string_cells'] or 'none (no Excel type coercion)'}")
    print(f"  blank cells       : {universe['blank_cells'] or 'none'}")
    print(f"  malformed         : {universe['malformed_tickers'] or 'none'}")
    print(f"  duplicates        : {universe['duplicates'] or 'none'}")

    print("\n[2] REVIEWED MANIFEST v3 (missing manifest is a FAILURE, never auto-adopted)")
    print(f"  status   : {report['input_manifest']['status']}")
    print(f"  version  : {report['input_manifest'].get('manifest_version')}")
    print(f"  commit   : {report['input_manifest'].get('established_commit_sha')}")
    print(f"  gate may establish : "
          f"{report['input_manifest'].get('establishment_reachable_from_gate')} "
          f"(establishment lives in experiment_1f_manifest.py, post-commit only)")
    for name, values in sorted(report["input_manifest"]["inputs"].items()):
        print(f"  {name:<20}: {values['sha256']}")
        print(f"  {'':<20}  {values['size_bytes']} bytes")

    print("\n[2b] ORCHESTRATION CALL PATH (what each stage actually consumed)")
    for i, stage in enumerate(report["orchestration"], start=1):
        print(f"  {i}. {stage['stage']}")
        for key, value in sorted(stage.items()):
            if key == "stage":
                continue
            print(f"       {key:<28}: {value}")

    print("\n[3] COVERAGE (price and broker frozen independently)")
    print(f"  full harvest      : {price_cov['harvest_rows_total']} rows / "
          f"{price_cov['harvest_tickers_total']} tickers")
    print(f"  trading calendar  : {price_cov['calendar_dates']} dates "
          f"({price_cov['calendar_first']} -> {price_cov['calendar_last']})")
    print(f"  PRICE covered     : {price_cov['price_covered_n']} / "
          f"{universe['distinct_tickers']} (expected {EXPECTED_PRICE_COVERED})")
    print(f"  PRICE missing     : {price_cov['price_missing']} (excluded, never fabricated)")
    print(f"  BROKER covered    : {broker_cov['broker_covered_n']} / "
          f"{universe['distinct_tickers']} (expected {EXPECTED_BROKER_COVERED})")
    print(f"  BROKER missing    : {broker_cov['broker_missing']}")
    print(f"  universe rows     : {price_cov['universe_rows']}")

    print("\n[4] RAW OHLC SOURCE INTEGRITY (FULL harvest, before detect())")
    print(f"  scope                      : {raw['scope']}")
    print(f"  rows                       : {raw['rows']}")
    print(f"  duplicate (date,ticker)    : {raw['duplicate_date_ticker']}")
    print(f"  invalid/impossible dates   : {raw['invalid_dates']} (strict %Y-%m-%d parse)")
    print(f"  missing ticker             : {raw['missing_ticker']}")
    print(f"  non-finite OHLCV           : {raw['non_finite']}")
    print(f"  non-positive o/h/l/c       : {raw['nonpositive']}")
    print(f"  invalid calendar dates     : {raw['invalid_calendar_dates']} "
          f"of {raw['calendar_dates']}")
    print(f"  passed                     : {raw['passed']}")

    print("\n[5] BROKER SOURCE INTEGRITY (raw rupiah, before normalization)")
    print(f"  rows                          : {source['rows']}")
    print(f"  non-finite required fields    : {source['non_finite']}")
    print(f"  duplicate (date,ticker,broker): {source['duplicate_date_ticker_broker']}")
    print(f"  invalid/impossible dates      : {source['invalid_dates']}")
    print(f"  missing ticker/broker ids     : {source['missing_identifiers']}")
    print(f"  negative gross fields         : blot {source['negative_blot']} | "
          f"slot {source['negative_slot']} | bval {source['negative_bval']} | "
          f"sval {source['negative_sval']}")
    print(f"  nlot != blot-slot             : {source['nlot_inconsistent']} "
          f"(max gap {source['nlot_max_gap']:.6g})")
    print(f"  nval != bval-sval             : {source['nval_inconsistent']} "
          f"(max gap {source['nval_max_gap_rupiah']:.6g} rupiah, tol "
          f"{source['nval_tolerance_rupiah']})")
    print(f"  value/lot coherence           : bval>0&blot<=0 "
          f"{source['bval_positive_blot_nonpositive']} | sval>0&slot<=0 "
          f"{source['sval_positive_slot_nonpositive']} | blot>0&bval<=0 "
          f"{source['blot_positive_bval_nonpositive']} | slot>0&sval<=0 "
          f"{source['slot_positive_sval_nonpositive']}")
    print(f"  passed                        : {source['passed']}")

    print("\n[6] DETECTORS (price_audit.detect on the FULL cross-section)")
    print(f"  {'detector':<20}{'full harvest':>14}{'universe':>11}")
    for name in ("limit_violation", "cross_ticker_dup", "series_break"):
        print(f"  {name:<20}{integrity['detector_counts_full_harvest'][name]:>14}"
              f"{integrity['detector_counts_universe'][name]:>11}")
    print(f"  {'suspect rows':<20}{integrity['suspect_rows_full_harvest']:>14}"
          f"{integrity['suspect_rows_universe']:>11}")
    print(f"  universe rows cloned ONLY against an out-of-universe ticker: "
          f"{integrity['universe_rows_dup_against_outside_only']}")
    print("  (those would have been INVISIBLE had the detector run on the subset)")
    for reason, count in sorted(integrity["suspect_reason_breakdown_universe"].items()):
        print(f"      {reason:<32}: {count}")

    print("\n[7] POST-QUARANTINE STRUCTURAL INTEGRITY (hard gate - all must be 0/True)")
    print(f"  rows in / after quarantine : {integrity['rows_in']} -> "
          f"{integrity['rows_after_quarantine']} "
          f"({integrity['tickers_after_quarantine']} tickers)")
    print(f"  out-of-universe rows leaked: {integrity['out_of_universe_rows_in_panel']}")
    print(f"  duplicate (date,ticker)    : {integrity['duplicate_date_ticker']}")
    print(f"  blank/impossible dates     : {integrity['blank_dates']}")
    print(f"  non-positive close         : {integrity['nonpositive_close']}")
    print(f"  surviving cross-ticker dup : {integrity['surviving_cross_ticker_dup']}")
    print(f"  ticker-boundary lag leak   : {integrity['first_row_lag_leak']}")
    print(f"  ticker-boundary fwd leak   : {integrity['last_row_forward_leak']}")
    print(f"  dates monotonic per ticker : {integrity['dates_monotonic_per_ticker']}")
    print("  --- reported, NOT gated ---")
    print(f"  open outside [low,high]    : {integrity['open_outside_high_low']} "
          f"(anchor contract withholds the label; row is kept)")
    print(f"  max |open-anchored label|  : {integrity['max_abs_label']}")

    basis = report["broker_basis_validity"]
    print("\n[7b] BROKER-BASIS VALIDITY (a BROKER domain fact, not a price fact)")
    print(f"  broker-invalid sessions       : {basis['invalid_sessions']}")
    print(f"  price rows deleted for it     : {basis['price_rows_deleted']} "
          f"(price validity is price_audit.detect's alone)")
    print(f"  price-valid & broker-invalid  : "
          f"{basis['price_valid_and_broker_invalid_rows_in_panel']} row(s) RETAINED "
          f"in the panel and eligible for variant A")
    for ticker, count in sorted(basis["detail"].items()):
        print(f"      {ticker:<8}: {count} session(s) with broker features NaN")

    print("\n[8] OPEN-ANCHOR REJECTION DIAGNOSTICS (Hn = open(T+1) -> open(T+1+n))")
    print(f"  rows with invalid open anchor : {anchors['rows_with_invalid_open_anchor']} "
          f"of {anchors['rows']}")
    print(f"  {'label':<11}{'NON_EXEC':>10}{'entry_bad':>11}{'exit_bad':>10}"
          f"{'past_end':>10}{'non_contig':>12}{'close/other':>13}")
    for col, stats in anchors["per_horizon"].items():
        print(f"  {col:<11}{stats['non_executable']:>10}{stats['entry_open_invalid']:>11}"
              f"{stats['exit_open_invalid']:>10}{stats['window_past_end_of_series']:>10}"
              f"{stats['non_contiguous_calendar']:>12}"
              f"{stats['close_step_or_other_by_elimination']:>13}")
    print("  (categories overlap by construction; 'close/other' is by elimination)")

    print("\n[9] LABEL COVERAGE")
    print(f"  {'label':<12}{'valid':>10}{'NON_EXEC':>12}{'valid %':>10}")
    for col, stats in integrity["label_coverage"].items():
        print(f"  {col:<12}{stats['valid']:>10}{stats['non_executable']:>12}"
              f"{stats['valid_pct']:>10.4f}")
    print(f"  valid count falls with horizon (DIAGNOSTIC, not required): "
          f"{integrity['valid_count_decreases_with_horizon']}")
    print("  Each horizon validates its OWN exit open, so a longer horizon may")
    print("  legitimately be valid where a shorter one is not.")

    print("\n[10] START-OF-HISTORY (from RAW source, pre-quarantine)")
    print(f"  source harvest floor         : {history['source_harvest_floor']}")
    print(f"  tickers starting at floor    : {history['source_tickers_starting_at_floor']}")
    print(f"  tickers starting later       : {history['source_tickers_starting_later']}")
    print(f"  post-quarantine first differs: {history['post_quarantine_first_differs']} ticker(s)")
    print(f"  bars/ticker min/median/max   : {history['bars_per_ticker_min']} / "
          f"{history['bars_per_ticker_median']} / {history['bars_per_ticker_max']}")
    print("  NOTE: first appearance is a COLLECTION date, not a listing date.")

    print("\n[11] NET-LOT RECOVERY FIDELITY (frozen #1E _historical_net_lots)")
    print(f"  rows audited                 : {lots['rows_audited']}")
    print(f"  exact split path             : {lots['exact_split_path_rows']}")
    print(f"  fallback (netval/close) path : {lots['fallback_path_rows']} "
          f"({lots['fallback_share']:.4%})")
    print(f"  one-sided rows (XOR)         : {lots['one_sided_rows']} "
          f"({lots['one_sided_and_fallback']} take the fallback)")
    print(f"  zero-sided rows (neither)    : {lots['zero_sided_rows']} "
          f"({lots['zero_sided_and_fallback']} take the fallback)")
    print(f"  |recovered-nlot| exact path  : median {lots['exact_path_median_lot_gap']:.6g} "
          f"| max {lots['exact_path_max_lot_gap']:.6g}")
    print(f"  |recovered-nlot| fallback    : median {lots['fallback_path_median_lot_gap']:.6g} "
          f"| max {lots['fallback_path_max_lot_gap']:.6g}")
    print(f"  CAVEAT: {lots['caveat']}")

    print("\n[12] BROKER PROVENANCE LEDGER")
    print(f"  rows / tickers / codes       : {report['broker_rows']} / "
          f"{report['broker_tickers']} / {report['broker_codes']}")
    print(f"  harvest window               : {provenance.get('harvest_first')} -> "
          f"{provenance.get('harvest_last')}")
    if "VALUE_RECONCILED_WINDOW" in provenance:
        window = provenance["VALUE_RECONCILED_WINDOW"]
        unverified = provenance["HISTORICAL_UNVERIFIED_WINDOW"]
        print(f"  HISTORICAL_UNVERIFIED_WINDOW : {unverified[0]} -> {unverified[1]}")
        print(f"  VALUE_RECONCILED_WINDOW      : {window[0]} -> {window[1]}")
        print(f"  matched rows for crosscheck  : {provenance['matched_rows']} "
              f"(of {provenance['live_rows']} live rows)")
        print(f"  bval agreement rate          : {provenance.get('bval_agreement_rate')} "
              f"({provenance.get('bval_disagreements')} disagreements)")
        print(f"  sval agreement rate          : {provenance.get('sval_agreement_rate')} "
              f"({provenance.get('sval_disagreements')} disagreements)")
    print(f"  status                       : {provenance.get('status')}")
    print(f"  PIT availability             : {provenance['pit_availability']}")

    print("\n[13] DIGESTS")
    print(f"  universe : {universe['universe_digest']}")
    print(f"  panel    : {report['panel_digest']}")
    print(f"  broker   : {report['broker_digest']}")

    print("\n[14] STANDING CAVEATS")
    print("  - Current-universe retrospective/survivorship bias is NOT eliminated by")
    print("    listing-proxy filtering. No PIT membership data exists in this repo.")
    print("  - Harvest floor is a COLLECTION date, not a listing date.")
    print("  - Broker VALUE fidelity is reconciled only on the live window; broker")
    print("    PIT AVAILABILITY is assumed and unverified in EVERY window.")
    print("  - One-sided/zero-sided broker rows take #1E's fallback path; see [11].")
    print("  - Snapshot is frozen, not refreshed: OOS ends at the calendar last date.")
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", default=UNIVERSE_XLSX)
    parser.add_argument("--json-out", default=None,
                        help="write the machine-readable report here")
    parser.add_argument("--determinism-check", action="store_true",
                        help="build twice and require identical digests")
    parser.add_argument("--refreeze", action="store_true",
                        help="deliberately re-read the workbook and re-freeze "
                             "(still hard-fails if its content changed)")
    # --establish-manifest is GONE, not renamed. It let a Gate A run re-pin the
    # inputs it had just read, which makes the manifest a record of the last run
    # rather than of the last review. Establishment is a separate act:
    #     py experiment_1f_manifest.py
    # and it is only legitimate after commit, on a clean semantic code tree.
    parser.add_argument("--mode", default=PRIMARY_MODE,
                        choices=[PRIMARY_MODE, SECONDARY_MODE],
                        help="PRIMARY is the reviewed policy; SECONDARY is "
                             "retrospective sensitivity only, never tradable "
                             "evidence")
    parser.add_argument("--net-lot-sample", type=int, default=None,
                        help="audit net-lot recovery on a seeded sample of N rows")
    args = parser.parse_args()

    panel, broker, report = run_gate(args.universe, refreeze=args.refreeze,
                                     mode=args.mode,
                                     net_lot_sample=args.net_lot_sample)
    print_report(report)

    if args.determinism_check:
        panel2, broker2, report2 = run_gate(args.universe, mode=args.mode,
                                            net_lot_sample=args.net_lot_sample)
        same_panel = report["panel_digest"] == report2["panel_digest"]
        same_broker = report["broker_digest"] == report2["broker_digest"]
        equal_frames = panel.equals(panel2) and broker.equals(broker2)
        if not (same_panel and same_broker and equal_frames):
            raise GateFailure(
                f"Gate A is not deterministic: panel {report['panel_digest']} vs "
                f"{report2['panel_digest']}, broker {report['broker_digest']} vs "
                f"{report2['broker_digest']}, frames_equal={equal_frames}")
        print(f"\nDeterminism check: PASS (panel {report['panel_digest']}, "
              f"broker {report['broker_digest']}, frames identical)")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True, default=str)
            handle.write("\n")
        print(f"\nMachine-readable report: {args.json_out}")


if __name__ == "__main__":
    main()
