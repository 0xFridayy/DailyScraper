"""Experiment #1F: the PROPOSED manifest (v3). Builds it; never establishes it.

Three ideas the v2 proposal did not separate cleanly.

EXECUTION vs PROVENANCE
-----------------------
An execution input is data the gate actually consumes. A provenance input is
evidence about where that data came from. Pinning the full-market
broker_daily.parquet as an EXECUTION input would let an out-of-universe refresh
invalidate a frozen 297-name experiment while changing nothing the gate reads:
load_frozen_broker() filters to `set(universe) & present` on its first
statement, and every broker computation downstream runs on that filtered frame.
So the 297-row broker_daily_v2.parquet is the execution input, and the
full-market table is pinned as provenance together with the proof that v2
reproduces its in-universe subset exactly.

OHLC is the deliberate exception and stays full-market: detect()'s
cross_ticker_dup compares OHLCV ACROSS tickers, so narrowing it would blind the
contamination detector that #1F depends on.

CODE IDENTITY vs RULE VERSION
-----------------------------
These are independent, not redundant, and neither subsumes the other:

  a rule_version is a human claim about meaning. It catches a deliberate
  semantic change, and it is worthless when someone edits the implementation
  and forgets to bump it.

  a file sha256 is a mechanical fact about bytes. It catches every edit,
  including the forgotten one, and it cannot tell a whitespace change from a
  semantic one.

  a git_commit_sha binds the whole tree at once and is checkable against
  history, but it says nothing when the working tree is dirty -- which is
  exactly when a file hash still tells the truth.

So the manifest carries all three, and establishment additionally requires a
clean working tree, because a dirty tree means the commit does not describe the
code that produced the artifacts.

PARENTAGE
---------
Filenames prove nothing. Every derived artifact is bound to its parent by
identity, so the chain source -> ingest -> detection -> authorisation can be
verified rather than assumed.
"""
import datetime as _dt
import json
import os
import subprocess
import tempfile

import experiment_1f_candidate as cand
import experiment_1f_normalization as norm
import experiment_1f_universe_gate as gate
import experiment_1f_validity as validity

HERE = os.path.dirname(os.path.abspath(__file__))
SHARED = os.environ.get("NEOBDM_SHARED_ROOT", os.path.join(os.path.dirname(HERE), "Claude"))

MANIFEST_VERSION = "experiment_1f/3"
PROPOSAL_NAME = "PROPOSED_manifest_schema_v3.json"

#: Files whose bytes can change the derived Gate-A panel or the artifacts it
#: consumes. Curated from the actual import graph, not globbed: pinning an
#: unrelated file would make the manifest fail for reasons that cannot affect
#: the experiment, which trains people to re-establish it without reading why.
#: Two planes, kept distinct because they fail differently.
#:
#: DATA PLANE code computes the panel: change it and the numbers change.
#: CONTROL PLANE code decides what a valid manifest IS: change it and the
#: numbers are untouched while the meaning of "verified" moves underneath them.
#: A verifier that quietly stopped checking code identity would leave every
#: data-plane hash intact and every guarantee gone, so the checker must be
#: pinned by the thing it checks.
DATA_PLANE = "DATA PLANE"
CONTROL_PLANE = "CONTROL PLANE"

CODE_IDENTITY = (
    ("experiment_1f_universe_gate.py", DATA_PLANE,
     "Gate A itself: universe resolution, panel construction, repair "
     "application, basis quarantine and harmonisation"),
    ("price_audit.py", DATA_PLANE,
     "detect() and the forward/lagged return builders the panel is made of; "
     "its semantics are frozen for #1E and must not drift under #1F"),
    ("build_inventory_db.py", DATA_PLANE,
     "strict candidate broker builder; broker_daily_v2.parquet is rebuildable "
     "from it, so its bytes determine the execution input"),
    ("normalize_market_data.py", DATA_PLANE,
     "semantically ACTIVE: supplies every basis threshold and the exact "
     "stage-A wrap detector that experiment_1f_normalization imports"),
    ("experiment_1f_candidate.py", DATA_PLANE,
     "source snapshot, source manifest and aggregate source identity"),
    ("experiment_1f_normalization.py", DATA_PLANE,
     "segmented basis regimes, repair candidates and the authorisation builder"),
    ("experiment_1f_validity.py", DATA_PLANE,
     "PRIMARY/SECONDARY validity policy and the feature-domain masks"),
    ("ml_v2_experiment_1.py", DATA_PLANE,
     "_historical_net_lots, used by the gate's net-lot recovery audit, and "
     "FLOW_WINDOWS, the actual broker feature lookbacks"),
    ("experiment_1f_manifest.py", CONTROL_PLANE,
     "constructs and verifies the manifest, checks code identity and enforces "
     "the establishment preconditions; a change here can redefine what a valid "
     "manifest means without touching a single computed number"),
)

#: Generated candidate artifacts. These are gitignored, so they never reach
#: porcelain -- but the classification is explicit so that establishing a
#: manifest into the candidate directory can never be mistaken for dirtying the
#: semantic code tree.
GENERATED_PREFIXES = ("backtest_out/",)

#: Pure documentation: the only thing that may be dirty at establishment time.
#:
#: The classification below is deliberately fail-CLOSED. An earlier version asked
#: "does the path end in .py?" and called everything else non-semantic, which was
#: fail-OPEN and wrong: experiment_1f_universe.json is the approved 298-name
#: universe and an execution input, and a modified-but-uncommitted copy of it
#: would have been pinned at its dirty value while semantic_code_tree_clean
#: stayed true. neobdm.db and broker_codes.json had the same hole. Enumerating
#: what is safe to ignore is checkable; enumerating what matters is not, because
#: the next result-affecting file added to the repo would silently escape.
NON_SEMANTIC_SUFFIXES = (".md", ".txt", ".rst")


def parse_porcelain(text):
    """Parse `git status --porcelain` v1 without destroying its column layout.

    The previous implementation ran .strip() over the WHOLE output and then took
    line[3:]. Porcelain v1 is a fixed 2-column status followed by a space, and an
    unstaged modification's status is " M" -- leading space significant. Stripping
    the whole output removed that space from the FIRST line only, so the first
    entry silently lost a character (" M build_inventory_db.py" -> "M build_..."
    -> "uild_inventory_db.py") while every later line parsed correctly. A
    one-character corruption on exactly one path is precisely the kind of defect
    a clean-tree gate must not have, so the fix is to stop using offsets on
    normalised text rather than to patch the symptom.
    """
    entries = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        status, path = line[:2], line[3:]
        if " -> " in path:                      # rename/copy: R  ORIG -> DEST
            path = path.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]                   # git quotes unusual paths
        entries.append({"status": status, "path": path})
    return entries


def classify_dirty(entries):
    """Split working-tree dirt into semantic, generated and documentation.

    Fail-closed: everything outside the generated candidate directory counts as
    semantic unless it is pure documentation. A .py-only rule would let a dirty
    experiment_1f_universe.json -- the approved universe, an execution input --
    pass the establishment gate, and would let every future non-.py input do the
    same.
    """
    semantic, generated, documentation = [], [], []
    for entry in entries:
        path = entry["path"]
        if any(path.startswith(prefix) for prefix in GENERATED_PREFIXES):
            generated.append(path)
        elif path.lower().endswith(NON_SEMANTIC_SUFFIXES):
            documentation.append(path)
        else:
            semantic.append(path)
    return sorted(semantic), sorted(generated), sorted(documentation)


def git_identity(root=HERE):
    def run(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True).stdout
    entries = parse_porcelain(run("status", "--porcelain", "--untracked-files=all"))
    semantic, generated, documentation = classify_dirty(entries)
    return {
        "git_commit_sha": run("rev-parse", "HEAD").strip(),
        "git_branch": run("rev-parse", "--abbrev-ref", "HEAD").strip(),
        "working_tree_clean": not entries,
        "semantic_code_tree_clean": not semantic,
        "dirty_paths": sorted(e["path"] for e in entries),
        "dirty_semantic_code": semantic,
        "dirty_generated_artifacts": generated,
        "dirty_documentation": documentation,
        "_cleanliness_note": (
            "establishment requires semantic_code_tree_clean. Generated "
            "candidate artifacts live under backtest_out/ and are gitignored, "
            "so writing the established manifest there cannot make the semantic "
            "code tree dirty -- the paradox that would otherwise make the "
            "precondition unsatisfiable."),
    }


def code_identity(root=HERE):
    """Pin every implementation file by its COMMITTED Git content.

    Not by its checkout bytes. With core.autocrlf=true the two differ on every
    line, so a raw-byte pin made the manifest reproducible only on the machine
    that wrote it: same HEAD, clean status, identical Git content, different
    hash, verification refused.
    """
    files, unavailable = [], []
    for name, plane, role in CODE_IDENTITY:
        path = os.path.join(root, name)
        entry = dict(gate.tracked_text_identity(path, root=root), path=name,
                     plane=plane,
                     role=role)
        if entry["canonical_identity_status"] != "AVAILABLE":
            unavailable.append(name)
        files.append(entry)
    return {
        "canonical_identity": gate.IDENTITY_GIT_CONTENT,
        "canonical_identity_rule": (
            "sha256 of the bytes Git stores at the pinned commit, plus a "
            "mandatory proof that the working tree carries no semantic Git "
            "difference from it. Git's own clean filter decides CRLF "
            "equivalence, so a CRLF-only checkout difference does not "
            "invalidate the experiment and a real edit does."),
        "identity_unavailable_until_commit": sorted(unavailable),
        "why_both_commit_and_hashes": (
            "a commit binds the whole tree but says nothing about WHICH files "
            "were meant to matter; a per-file content hash names them and "
            "catches the edit whose rule_version was never bumped. They are "
            "independent checks, not redundant ones."),
        "why_the_control_plane_is_pinned": (
            "the manifest verifier decides what 'valid' means. A change there "
            "leaves every data-plane hash intact while altering every guarantee, "
            "so it is pinned by the same mechanism it enforces."),
        "establishment_requires_semantic_code_tree_clean": True,
        **git_identity(root),
        "files": files,
        "data_plane_files": [f["path"] for f in files if f["plane"] == DATA_PLANE],
        "control_plane_files": [f["path"] for f in files if f["plane"] == CONTROL_PLANE],
    }


def _fp(path, role, extra=None, identity=gate.IDENTITY_RAW_BYTES,
        root=HERE):
    """Pin one file under the identity rule that is correct FOR THAT FILE.

    RAW_BYTES for generated, experiment-owned artifacts: their exact reviewed
    bytes are the thing under review and no Git filter ever touches them.

    GIT_CONTENT for tracked repository text, where the checkout's newline bytes
    are a local rendering rather than an identity -- see
    gate.git_content_sha256(). `size_bytes` is still recorded for such a file
    but is FORENSIC only; comparing it would reintroduce exactly the
    machine-binding this rule removes, because CRLF and LF renderings of
    identical Git content differ in length.
    """
    entry = {"path": os.path.relpath(path, HERE).replace(os.sep, "/"),
             "role": role, "present": os.path.exists(path),
             "identity": identity}
    if identity == gate.IDENTITY_GIT_CONTENT:
        entry.update(gate.tracked_text_identity(path, root=root))
        entry["path"] = os.path.relpath(path, HERE).replace(os.sep, "/")
        entry["role"] = role
        entry["size_bytes"] = (os.path.getsize(path) if os.path.exists(path)
                               else None)
        entry["size_bytes_role"] = "FORENSIC ONLY -- CRLF/LF changes it"
    elif os.path.exists(path):
        entry["sha256"] = cand.sha256_file(path)
        entry["size_bytes"] = os.path.getsize(path)
    entry.update(extra or {})
    return entry


#: Prose for each execution input, keyed by the SAME manifest names the gate
#: verifies. The name set itself comes from gate.EXECUTION_INPUTS, so the
#: manifest cannot pin an input the gate does not verify, or omit one it does.
EXECUTION_INPUT_ROLES = {
    "ohlc_full_market_parquet": (
        "FULL-MARKET price harvest, experiment-owned. Deliberately not "
        "narrowed: detect()'s cross_ticker_dup compares OHLCV across tickers, "
        "so an approved name cloned against an out-of-universe name would look "
        "unique if the other half were filtered away. The candidate copy "
        "changes custody, not content.",
        {"tickers": 936, "rows": 210216, "byte_identical_to_frozen_source": True}),
    "broker_daily_v2_parquet": (
        "EXACT-297 broker execution input. load_frozen_broker() filters to "
        "set(universe) & present immediately and no broker invariant reads an "
        "out-of-universe row, so the isolated table is the semantically "
        "complete input.",
        {"rows": 2689458, "tickers": 297}),
    "universe_json": ("the approved 298, consumed via "
                      "resolve_universe(require_frozen=True)", None),
    "source_manifest_json": ("the 297-file source population", None),
    "volume_repair_candidates_json": ("DETECTION; authorises nothing", None),
    "volume_repair_authorization_json": (
        "AUTHORISATION; a separate artifact from a separate act", None),
    "observed_basis_factor_candidate_json": (
        "SEGMENTED regimes: a list per ticker, each an inclusive "
        "start_date..end_date interval", None),
}


def execution_inputs_section(candidate_dir, inputs=None, root=None):
    """Build A_execution_inputs from the gate's own contract, never by hand.

    `inputs` lets a caller supply an already-built execution contract (a test
    fixture pointing at a synthetic directory) so that the fixture's manifest is
    produced by THIS function rather than by a hand-written imitation. That is
    what makes structural divergence between the produced and the verified
    manifest detectable instead of silently tolerated.

    The manifest pinned seven inputs while run_gate hashed three, so a real
    established manifest would have failed Gate A on the other four -- and no
    test could see it, because the orchestration fixture wrote its own reduced
    manifest instead of using this builder. Deriving both sides from
    gate.EXECUTION_INPUTS makes that divergence unrepresentable.
    """
    inputs = inputs or gate.candidate_inputs(candidate_dir=candidate_dir)
    root = root or inputs.get("git_root", HERE)
    section = {"_definition": "data Gate A actually consumes; a change here "
                              "changes the experiment and must invalidate the "
                              "manifest",
               "_source_of_truth": "experiment_1f_universe_gate.EXECUTION_INPUTS"}
    for name, key, identity in gate.EXECUTION_INPUTS:
        role, extra = EXECUTION_INPUT_ROLES.get(name, ("execution input", None))
        section[name] = _fp(inputs[key], role, extra, identity=identity,
                            root=root)
    return section


def parentage(candidate_dir):
    """Bind every derived artifact to its parent by identity, not by filename."""
    covered, manifest = cand.manifest_tickers(candidate_dir)
    aggregate = manifest["source_aggregate_sha256"]
    repairs_path = os.path.join(candidate_dir, norm.CANDIDATE_REPAIRS)
    auth_path = os.path.join(candidate_dir, "volume_repair_authorization.json")

    with open(repairs_path, encoding="utf-8") as fh:
        repairs = json.load(fh)
    with open(auth_path, encoding="utf-8") as fh:
        auth = json.load(fh)

    repairs_physical = cand.sha256_file(repairs_path)
    bound = (auth["parent_candidate_sha256"] == repairs_physical)

    # Every repair candidate names the sha256 of the source file it was measured
    # from, so the detection layer is bound to the source population row by row
    # rather than only in aggregate.
    source_shas = {f["ticker"]: f["sha256"] for f in manifest["files"]}
    per_hit = [{"key": f"{c['ticker']} {c['date']}",
                "source_sha256_matches_manifest":
                    c["source_sha256"] == source_shas.get(c["ticker"])}
               for c in repairs["candidates"]]

    return {
        "chain": [
            {"parent": "source_manifest.source_aggregate_sha256",
             "child": "broker_daily_v2.parquet",
             "binding": "the strict builder consumes exactly the manifest's 297 "
                        "tickers from the experiment-owned snapshot",
             "parent_identity": aggregate},
            {"parent": "source_manifest + rule/code identity",
             "child": norm.CANDIDATE_REPAIRS,
             "binding": "each candidate carries the full sha256 of the source "
                        "file it was measured from",
             "per_candidate_source_binding_ok": all(
                 h["source_sha256_matches_manifest"] for h in per_hit),
             "detail": per_hit},
            {"parent": norm.CANDIDATE_REPAIRS,
             "child": "volume_repair_authorization.json",
             "binding": "physical sha256 + semantic digest of the reviewed bytes",
             "parent_sha256": repairs_physical,
             "recorded_parent_sha256": auth["parent_candidate_sha256"],
             "bound": bound},
            {"parent": "source_manifest + rule/code identity",
             "child": norm.CANDIDATE_BASIS,
             "binding": "derived from the same snapshot by the same rule "
                        "version; pinned by content hash in this manifest",
             "parent_identity": aggregate},
        ],
        "all_bindings_ok": bool(bound and all(
            h["source_sha256_matches_manifest"] for h in per_hit)),
    }


def build(candidate_dir=cand.CANDIDATE_DIR):
    covered, manifest = cand.manifest_tickers(candidate_dir)
    join = lambda name: os.path.join(candidate_dir, name)
    basis = json.load(open(join(norm.CANDIDATE_BASIS), encoding="utf-8"))
    auth = json.load(open(join("volume_repair_authorization.json"), encoding="utf-8"))

    return {
        "_status": ("PROPOSAL FOR REVIEW -- NOT ESTABLISHED. No manifest exists "
                    "in this worktree and none is written by this module."),
        "manifest_version": MANIFEST_VERSION,
        "established_utc": None,
        "established_commit_sha": None,

        "A_execution_inputs": execution_inputs_section(candidate_dir),

        "B_provenance_inputs": {
            "_definition": ("evidence about where execution inputs came from. A "
                            "change here is investigated, but it does not by "
                            "itself invalidate a frozen 297-name experiment."),
            "shared_checkout_ohlc_parquet": _fp(
                os.path.join(SHARED, "ohlc.parquet"),
                "PROVENANCE: the frozen source the experiment-owned OHLC copy "
                "was taken from. Not read at execution time.",
                {"reproduced_byte_for_byte_by": cand.CANDIDATE_OHLC}),
            "full_market_broker_daily_parquet": _fp(
                os.path.join(SHARED, "broker_daily.parquet"),
                "PROVENANCE / REGRESSION REFERENCE, not an execution input",
                {"in_universe_subset_reproduced_by_v2_exactly": True,
                 "measured_value_mismatches": 0,
                 "_evidence": "validation_report.ingest."
                              "agreement_with_frozen_broker_daily"}),
            "superseded_broker_daily_float32": {
                "sha256": cand.SUPERSEDED_BROKER_DAILY["sha256"],
                "size_bytes": cand.SUPERSEDED_BROKER_DAILY["size_bytes"],
                "role": "SUPERSEDED PRE-PR38 LOSSY REFERENCE -- never an input",
            },
            "legacy_root_artifacts": {
                "observed_basis_factor.json": "LEGACY one-regime/prefix contract. "
                    "Left untouched as production state.",
                "volume_repair_ledger.json": "LEGACY detection-as-authorisation "
                    "ledger. Superseded by the candidates + authorisation pair.",
                "experiment_1f_input_manifest.json": "LEGACY two-file manifest. "
                    "Superseded by this v3 schema and no longer verified against "
                    "or written by the gate.",
                "_not_consumed_claim": (
                    "run_gate reads its normalized layer through "
                    "load_candidate_artifacts(inputs), which takes every path "
                    "from candidate_inputs() and has no default that could "
                    "reach these files. Verified behaviourally by tracing "
                    "builtins.open across a COMPLETE run_gate() call against a "
                    "synthetic contract, not by declaration and not by tracing a "
                    "loader in isolation: an earlier run_gate declared the "
                    "candidate inputs, passed the declaration guard, and still "
                    "opened both of these because load_normalized_artifacts() "
                    "was called with no arguments -- a defect that lived in the "
                    "orchestration, where no loader-level test could see it. "
                    "See test_full_run_gate_orchestration_consumes_only_the_"
                    "candidate_contract."),
            },
            "source_raw_snapshot": {
                "path": "backtest_out/experiment_1f_candidate/source_raw/",
                "n_files": len(covered),
                "aggregate_sha256": manifest["source_aggregate_sha256"],
                "role": "experiment-owned byte copy of the 297 covered sources",
            },
        },

        "C_normalization_policy": {
            "mode": validity.PRIMARY,
            "pit_observability": validity.PIT_OBSERVABILITY,
            "pit_observability_meaning": (
                "the policy refuses retrospective basis harmonisation and "
                "excludes unresolved regimes conservatively, but the historical "
                "availability of the DQ classification itself is NOT proven: "
                "the basis-invalid mask is reconstructed from the full frozen "
                "window. The mode is named CONSERVATIVE, not SAFE, for exactly "
                "this reason."),
            "primary_applies_basis_harmonisation": False,
            "price_broker_domain_separation": (
                "a basis regime is a statement about the broker LOT basis, not "
                "about the price series -- the vendor's price and volume stay "
                "mutually consistent throughout. So a basis-invalid session "
                "withholds BROKER features (NaN) and keeps its price row. "
                "Price-row validity has exactly one authority, "
                "price_audit.detect, and Gate A deletes ZERO price rows for a "
                "broker-basis fact. Routing the price frame through "
                "quarantine_basis_regimes() previously made a broker-domain "
                "defect shrink variant A's panel for a reason no price detector "
                "ever objected to."),
            "certified_but_pit_ineligible": ["MLPT", "RAJA", "RMKE"],
            "secondary_mode": validity.SECONDARY,
            "secondary_is_not_tradable_evidence": True,
            "observable_inventory_resets_at_a_hole": False,
            "observable_inventory_reanchored": False,
            "validity_artifacts": {
                "primary": _fp(join(validity.ARTIFACT_PRIMARY),
                               "DERIVED REPORT, not an execution input: it is a "
                               "function of the basis artifact and the policy "
                               "constants, both already pinned above"),
                "secondary": _fp(join(validity.ARTIFACT_SECONDARY),
                                 "DERIVED REPORT, retrospective sensitivity only"),
            },
        },

        "D_authorization": {
            "scope": auth["scope"],
            "n_authorized": auth["n_authorized"],
            "authorized_keys": [f"{a['ticker']} {a['date']}"
                                for a in auth["authorized"]],
            "parent_candidate_sha256": auth["parent_candidate_sha256"],
            "parent_candidate_semantic_digest":
                auth["parent_candidate_semantic_digest"],
            "stage_b_structurally_ineligible": ["BNBR 2026-02-27",
                                                "BNBR 2026-03-02"],
            "invalidation": ("any detector hit added, removed or changed "
                             "invalidates this authorisation in full"),
        },

        "E_code_identity": code_identity(),

        "F_universe": {
            "n_tickers": manifest["universe"]["n_tickers"],
            "universe_digest": manifest["universe"]["universe_digest"],
            "covered": len(covered),
            "uncovered": sorted(manifest["population"]["uncovered"]),
        },

        "G_rule_versions": {
            "volume_wrap": norm.RULE_VERSION_WRAP,
            "basis_regime": norm.RULE_VERSION_BASIS,
            "ingest": "build_inventory_db.build_candidate_broker",
            "n_regimes": basis["n_regimes"],
            "_note": "a rule_version is a claim about meaning; section E pins "
                     "the bytes that implement it. Both are required.",
        },

        "H_parentage": parentage(candidate_dir),

        "_establishment": {
            "established": False,
            # Taken from the gate's own constant so the file this module would
            # write and the file the gate verifies cannot drift apart. The name
            # is deliberately NOT experiment_1f_input_manifest.json: that is the
            # legacy two-file manifest, and a v3 manifest wearing a v2 filename
            # is exactly the confusion the version field exists to prevent.
            "target_path": os.path.relpath(
                gate.REVIEWED_MANIFEST_V3_JSON, HERE).replace(os.sep, "/"),
            "verified_by": ("experiment_1f_universe_gate."
                            "verify_reviewed_manifest_v3 -- a verifier with no "
                            "establish parameter and no write path, so a failing "
                            "verification can never be resolved by re-pinning"),
            "why_not_repo_root": (
                "the candidate directory is gitignored, so writing the "
                "established manifest there cannot dirty the semantic code "
                "tree. Establishing into the repo root would make the act of "
                "establishing violate its own clean-tree precondition -- a "
                "paradox that would force everyone to bypass the check."),
            "order": [
                "finish code", "tests green", "HUMAN CODE REVIEW",
                "commit approved semantic + control-plane code",
                "verify semantic code tree clean",
                "regenerate this proposal against the FINAL commit sha and file "
                "hashes",
                "human review of those exact hashes",
                "explicit manifest establishment",
                "first real Gate A",
            ],
        },

        "_establishment_rules": [
            "establishment is an explicit, separately reviewed act, never a side "
            "effect of running the gate",
            "semantic_code_tree_clean must be true; generated candidate "
            "artifacts under backtest_out/ are gitignored and never count "
            "against it",
            "every hash is full sha256",
            "any execution-input or code-identity mismatch is a HARD FAIL",
            "a provenance mismatch is investigated, not silently accepted, but "
            "does not by itself invalidate the frozen experiment",
            "a new stage-A detector hit requires fresh human review",
        ],
    }


class NotEstablishable(RuntimeError):
    """The proposal cannot become a manifest in its current state."""


def _identity_failures(label, entry):
    """The tracked-content preconditions for one pinned path, at establishment."""
    failures = []
    if not entry.get("present"):
        failures.append(f"{label}: missing from the worktree")
    if not entry.get("tracked_at_head"):
        failures.append(
            f"{label}: NOT TRACKED AT HEAD "
            f"({entry.get('canonical_identity_status')}); its committed content "
            "does not exist yet, so it has no canonical identity to pin")
        return failures
    if len(entry.get("git_content_sha256") or "") != 64:
        failures.append(f"{label}: no full git_content_sha256")
    if not entry.get("worktree_matches_head"):
        failures.append(
            f"{label}: carries a staged or unstaged difference against HEAD")
    return failures


def assert_establishable(proposal):
    """Every precondition establishment would require. Does NOT establish.

    Kept as a pure check so the preconditions are testable long before anyone
    is allowed to run the establishing act.
    """
    failures = []
    code = proposal["E_code_identity"]
    if not code["semantic_code_tree_clean"]:
        failures.append(
            f"semantic code tree is dirty "
            f"({len(code['dirty_semantic_code'])} path(s): "
            f"{', '.join(code['dirty_semantic_code'][:4])}); the pinned commit "
            "would not describe the code that produced the artifacts")
    if not code["git_commit_sha"]:
        failures.append("no git commit sha")

    # Every pinned implementation file must be TRACKED AT HEAD. Before the
    # approved commit the Phase-2 modules are untracked and have no committed
    # content, so no canonical identity exists for them -- and inventing one
    # (from checkout bytes, say) is precisely the machine-bound pin this
    # contract replaces. The proposal records UNAVAILABLE_UNTIL_COMMIT and
    # establishment refuses until the commit resolves it.
    for entry in code["files"]:
        failures.extend(_identity_failures(f"code file {entry['path']}", entry))

    for name, entry in proposal["A_execution_inputs"].items():
        if name.startswith("_"):
            continue
        if not entry.get("present"):
            failures.append(f"execution input missing: {entry['path']}")
        elif entry.get("identity") == gate.IDENTITY_GIT_CONTENT:
            failures.extend(_identity_failures(f"execution input {name}", entry))
        elif len(entry.get("sha256", "")) != 64:
            failures.append(f"execution input lacks a full sha256: {name}")

    if not proposal["H_parentage"]["all_bindings_ok"]:
        failures.append("artifact parentage does not verify")
    if proposal["D_authorization"]["scope"] != "EXACT KEY SET ONLY":
        failures.append("authorization scope is not an exact key set")
    if proposal["C_normalization_policy"]["mode"] != validity.PRIMARY:
        failures.append(f"manifest mode is not {validity.PRIMARY}")

    if failures:
        raise NotEstablishable("; ".join(failures))
    return True


def verify_code_identity(proposal, root=HERE):
    """Independently recompute each pinned file's committed identity.

    Recomputed from Git, not from the checkout's bytes, and paired with the
    worktree-cleanliness check so a local edit still refuses.
    """
    drifted = []
    for entry in proposal["E_code_identity"]["files"]:
        path = os.path.join(root, entry["path"])
        actual = gate.tracked_text_identity(path, root=root)
        if (actual["git_content_sha256"] != entry.get("git_content_sha256")
                or not actual["worktree_matches_head"]
                or not actual["tracked_at_head"]):
            drifted.append({
                "path": entry["path"],
                "pinned": entry.get("git_content_sha256"),
                "actual": actual["git_content_sha256"],
                "tracked_at_head": actual["tracked_at_head"],
                "worktree_matches_head": actual["worktree_matches_head"]})
    if drifted:
        raise NotEstablishable(
            "pinned implementation files are not at their reviewed committed "
            "content: " + ", ".join(d["path"] for d in drifted))
    return True


CONFIRMATION_PHRASE = "I HAVE REVIEWED THESE HASHES AND AUTHORISE ESTABLISHMENT"


class EstablishmentRefused(RuntimeError):
    """A precondition of the establishing act does not hold."""


def establish_reviewed_manifest(candidate_dir=None, explicit_confirmation=None,
                                target_path=None, allow_reestablish=False,
                                root=HERE):
    """THE separate act. Implemented and tested; not to be run before commit.

    This is deliberately not reachable from run_gate, from Gate A's CLI, or as a
    side effect of building a proposal. Establishment is the moment a human's
    review becomes machine-enforceable, and a machine that can perform it on its
    own behalf has removed the human from the loop it exists to represent.

    Every precondition is re-checked NOW, against the working tree as it stands,
    rather than trusted from whatever an older proposal recorded:

      1. the proposal is REBUILT from disk, so a stale one cannot be established
      2. assert_establishable() -- dirty semantic tree, missing input, broken
         parentage, wrong scope or wrong mode all refuse
      3. verify_code_identity() re-hashes every pinned file independently
      4. HEAD must equal the commit the freshly built proposal recorded, so the
         code being pinned is the code that is committed
      5. the semantic code tree must be clean AT THIS MOMENT
      6. an existing reviewed manifest is never overwritten without a separate,
         explicit re-establishment decision

    Only then are established_utc and established_commit_sha filled in and the
    file written atomically -- a partial manifest on disk would be a pin that
    describes nothing, and fail-closed verification would read it as drift.

    `explicit_confirmation` must be the exact CONFIRMATION_PHRASE. It is not
    security; it is a speed bump that makes the act impossible to perform by
    reflex or by an argument-less script.
    """
    candidate_dir = candidate_dir or cand.CANDIDATE_DIR
    target_path = target_path or gate.REVIEWED_MANIFEST_V3_JSON

    if explicit_confirmation != CONFIRMATION_PHRASE:
        raise EstablishmentRefused(
            "establishment requires the exact confirmation phrase "
            f"{CONFIRMATION_PHRASE!r}. This act converts a human review into a "
            "machine-enforced pin and is never performed implicitly.")

    if os.path.exists(target_path) and not allow_reestablish:
        raise EstablishmentRefused(
            f"a reviewed manifest already exists at {target_path}. Overwriting "
            "it would silently retire a pin a human accepted; re-establishment "
            "is its own decision (allow_reestablish=True) and requires the same "
            "review as the first one.")

    # 1. a FRESH proposal. A stale one describes a tree that may have moved.
    proposal = build(candidate_dir)
    code = proposal["E_code_identity"]

    # 2 + 3. preconditions, and an independent re-hash of the pinned files
    assert_establishable(proposal)
    verify_code_identity(proposal, root=root)

    # 4. the commit being pinned must be the commit checked out right now
    head, error = gate._git_head(root)
    if error:
        raise EstablishmentRefused(f"cannot determine HEAD: {error}")
    if head != code["git_commit_sha"]:
        raise EstablishmentRefused(
            f"HEAD is {head} but the freshly built proposal recorded "
            f"{code['git_commit_sha']}; the tree moved during establishment")

    # 5. clean NOW, not according to an older record
    if not code["semantic_code_tree_clean"]:
        raise EstablishmentRefused(
            "the semantic code tree is dirty right now: "
            + ", ".join(code["dirty_semantic_code"][:6]))

    established = dict(proposal)
    established["_status"] = (
        "ESTABLISHED. Verified by experiment_1f_universe_gate."
        "verify_reviewed_manifest_v3, which cannot re-establish it.")
    established["established_utc"] = _dt.datetime.utcnow().isoformat() + "Z"
    established["established_commit_sha"] = head
    established["_establishment"] = dict(proposal["_establishment"],
                                         established=True)

    # 6. atomic: a reader never sees a half-written pin
    directory = os.path.dirname(target_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory or ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(established, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp, target_path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return target_path, established

def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-dir", default=cand.CANDIDATE_DIR)
    a = ap.parse_args()
    proposal = build(a.candidate_dir)
    path = os.path.join(a.candidate_dir, PROPOSAL_NAME)
    cand.write_json(path, proposal)
    code = proposal["E_code_identity"]
    print(f"manifest_version   {proposal['manifest_version']}  (NOT established)")
    print(f"commit             {code['git_commit_sha']}")
    print(f"working tree clean {code['working_tree_clean']}")
    print(f"code files pinned  {len(code['files'])}")
    print(f"parentage ok       {proposal['H_parentage']['all_bindings_ok']}")
    print(f"wrote              {path}")
    # Establishment is deliberately NOT wired to a flag here. It is a function
    # with a mandatory confirmation phrase, called deliberately after the final
    # approved commit -- never a switch on the command that builds the proposal.
    print(f"establishment      NOT PERFORMED. Call "
          f"establish_reviewed_manifest(explicit_confirmation=...) after commit.")
    try:
        assert_establishable(proposal)
        print("establishable      YES (preconditions hold right now)")
    except NotEstablishable as exc:
        print(f"establishable      NO -- {exc}")


if __name__ == "__main__":
    main()
