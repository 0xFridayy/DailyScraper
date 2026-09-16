"""Experiment #1F Gate-B Stage-1 runner.

Loads only the immutable Gate-B exports, verifies the frozen contract and its
ledgers, enumerates the pre-registered Stage-1 schedule, builds FIT / EVAL / TEST
rows, and (only when allowed) fits, predicts, evaluates and assembles the
pre-registered graduation report.

FAIL-CLOSED FIT BOUNDARY. No XGBoost object is constructed and nothing is fitted
unless the pre-fit execution manifest exists and verifies: exact committed HEAD,
clean tracked tree, execution-code Git-content identities, closure of loaded
repo-local modules, contract canonical hash, READY contract document,
preparation pins, export raw hashes, semantic digests, ledgers, Stage-1 plan
digest, package versions, and no pending contract decisions. HEAD, tree,
manifest bytes and module closure are re-checked before every fit. Any mismatch
is a HARD STOP with no repair, no re-establishment and no fallback dataset.

Modes:
  --dry-run            verify contract + ledgers, build every job's rows, apply one
                       draw of each placebo transform, count fits; fits nothing
  --establish-manifest after committing the execution code (confirmation phrase required)
  --verify-manifest    report manifest verification without fitting
  --run                the real Stage-1 execution (refuses without a valid manifest)
"""

import argparse
import datetime
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import experiment_1f_evaluation as evaluation  # noqa: E402
import experiment_1f_features as features  # noqa: E402
import experiment_1f_gate_b_contract as contract  # noqa: E402

KEY = ["ticker", "date"]
MANIFEST_VERSION = "gate_b_execution_manifest_v1"
MANIFEST_PATH = os.path.join(contract.CONTRACT_DIR, f"{MANIFEST_VERSION}.json")
CONFIRMATION_PHRASE = "I CONFIRM THE GATE-B EXECUTION CODE IS COMMITTED AND FROZEN"
EXECUTION_FILES = ("experiment_1f_features.py", "experiment_1f_evaluation.py", "experiment_1f_gate_b_contract.py",
                   "experiment_1f_gate_b.py", "experiment_1f_gate_b_preparation_pins.json")
HELPER_FILES = ("walk_forward_backtest.py", "price_audit.py", "normalize_market_data.py",
                "experiment_1f_universe_gate.py", "signal_metrics.py")
IDENTITY_FILES = EXECUTION_FILES + HELPER_FILES
LAZY_LOCAL_IMPORTS = ("walk_forward_backtest", "price_audit", "normalize_market_data", "experiment_1f_universe_gate")
PACKAGES = ("numpy", "pandas", "pyarrow", "xgboost")
PREDICTION_COLUMNS = ("job_id", "category", "arm", "sample", "variant", "placebo_family", "placebo_draw", "seed",
                      "horizon", "fold", "best_iteration", "fit_rows", "eval_rows", "test_rows", "feature_count",
                      "date", "ticker", "prediction")
FIT_COUNTER = {"models_constructed": 0, "fits": 0, "predictions": 0}
PAIRS = (("A", "B", "AB", "B_alignment", 0), ("B", "C", "BC", "C_identity", 1), ("C", "D", "CD", "D_state", 2))
PRICE_CORE_BOOTSTRAP_INDEX = 3
GRADUATION_ARM = "lag1"
PLACEBO_SEED = features.PLACEBO_SEED


class HardStop(RuntimeError):
    pass


# ── git / code identity ────────────────────────────────────────────────────
def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True)


def git_head(root=HERE):
    out = _git(root, "rev-parse", "HEAD")
    return out.stdout.decode().strip() if out.returncode == 0 else None


def tracked_tree_clean(root=HERE):
    out = _git(root, "status", "--porcelain", "--untracked-files=no")
    return out.returncode == 0 and not out.stdout.strip()


def git_content_sha256(path, root=HERE, ref="HEAD"):
    out = _git(root, "show", f"{ref}:{path}")
    return hashlib.sha256(out.stdout).hexdigest() if out.returncode == 0 else None


def worktree_matches_head(path, root=HERE):
    tracked = _git(root, "ls-files", "--error-unmatch", path).returncode == 0
    return tracked and _git(root, "diff", "--quiet", "HEAD", "--", path).returncode == 0


def code_identity(paths, root=HERE):
    return {p: {"git_content_sha256": git_content_sha256(p, root),
                "worktree_matches_head": worktree_matches_head(p, root)} for p in paths}


def package_versions():
    return {p: importlib.metadata.version(p) for p in PACKAGES}


def local_modules_outside_identity(root=HERE, identity=IDENTITY_FILES):
    """Repo-local modules loaded in this process that no code identity covers (test modules excepted).
    Imports the lazily imported helpers first so their transitive closure is visible."""
    import importlib
    for name in LAZY_LOCAL_IMPORTS:
        importlib.import_module(name)
    base = os.path.normcase(os.path.abspath(root))
    outside = []
    for name, module in sorted(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        path = os.path.normcase(os.path.abspath(path))
        if not path.startswith(base + os.sep):
            continue
        rel = os.path.relpath(path, base).replace(os.sep, "/")
        if os.path.basename(rel).startswith("test_"):
            continue
        if rel not in identity:
            outside.append(rel)
    return outside


# ── manifest ───────────────────────────────────────────────────────────────
def _data_identity():
    pins = contract.load_pins()
    body = contract.contract_body()
    ledger_digests = {arm: {"feature_value_digest": pins["arms"][arm]["feature_value_digest"],
                            "samples": {s: {"keys_sha256": v["keys_sha256"],
                                            "splits": {h: r["digest"] for h, r in v["splits"].items()}}
                                        for s, v in pins["arms"][arm]["samples"].items()}}
                      for arm in pins["arms"]}
    return {
        "contract_version": body["contract_version"], "contract_status": body["status"],
        "contract_canonical_sha256": contract.contract_sha256(body),
        "preparation_pins_sha256": contract.file_sha256(contract.PINS_FILE),
        "export_raw_sha256": {n: s["sha256"] for n, s in contract.EXPORT["files"].items()},
        "gate_a_panel_digest": contract.GATE_A["panel_digest"], "gate_a_broker_digest": contract.GATE_A["broker_digest"],
        "lot_content_digest": contract.EXPORT["digests"]["lot_content_canonical"]["value"],
        "feature_ledger_digest": pins["feature_ledger_digest"],
        "ledgers": ledger_digests,
        "timing_intersection_keys": {s: v["keys_sha256"] for s, v in pins["timing_intersection"].items()},
        "rank_label_digests": {h: v["digest"] for h, v in pins["rank_labels"].items()},
        "model_seeds": list(features.MODEL_SEEDS),
    }


def establish_execution_manifest(confirmation, path=MANIFEST_PATH, root=HERE, stage1_plan_digest=None):
    """Write the immutable pre-fit manifest. Refuses unless every precondition holds."""
    if confirmation != CONFIRMATION_PHRASE:
        raise HardStop("confirmation phrase missing or wrong")
    if os.path.exists(path):
        raise HardStop(f"{path} exists; a manifest is never overwritten (new code -> new commit -> new version)")
    if not tracked_tree_clean(root):
        raise HardStop("tracked tree is not clean")
    identity = code_identity(IDENTITY_FILES, root)
    bad = [p for p, v in identity.items() if not v["git_content_sha256"] or not v["worktree_matches_head"]]
    if bad:
        raise HardStop(f"execution files not committed or modified: {bad}")
    uncovered = local_modules_outside_identity(root)
    if uncovered:
        raise HardStop(f"loaded repo-local modules without a code identity: {uncovered}")
    contract.assert_frozen()
    failures = contract.verify_inputs(recompute_logical=True, recompute_ledgers=True, root=root)
    if failures:
        raise HardStop(f"contract input verification failed: {failures}")
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "established_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "committed_head": git_head(root),
        "branch_informational": _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip(),
        "requires_clean_tracked_tree": True,
        "execution_code_git_content_sha256": {p: v["git_content_sha256"] for p, v in identity.items()},
        "contract_document_sha256": contract.file_sha256(contract.write_contract()),
        "python": platform.python_version(), "packages": package_versions(),
        "stage1_plan_digest": stage1_plan_digest,
        **_data_identity(),
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    return path, contract.file_sha256(path)


def verify_git_identity(manifest, root=HERE):
    failures = []
    if git_head(root) != manifest["committed_head"]:
        failures.append("HEAD differs from the manifest's committed HEAD")
    if not manifest.get("requires_clean_tracked_tree") or not tracked_tree_clean(root):
        failures.append("tracked tree is not clean")
    if sorted(manifest["execution_code_git_content_sha256"]) != sorted(IDENTITY_FILES):
        failures.append("manifest code-identity file set differs from the runner's identity set")
    uncovered = local_modules_outside_identity(root)
    if uncovered:
        failures.append(f"loaded repo-local modules without a code identity: {uncovered}")
    for p, sha in sorted(manifest["execution_code_git_content_sha256"].items()):
        if git_content_sha256(p, root) != sha:
            failures.append(f"execution code identity changed: {p}")
        elif not worktree_matches_head(p, root):
            failures.append(f"execution code modified in worktree: {p}")
    return failures


def verify_data_identity(manifest):
    failures = []
    current = _data_identity()
    for key in ("contract_canonical_sha256", "preparation_pins_sha256", "export_raw_sha256", "gate_a_panel_digest",
                "gate_a_broker_digest", "lot_content_digest", "feature_ledger_digest", "ledgers",
                "timing_intersection_keys", "rank_label_digests", "model_seeds"):
        if current[key] != manifest[key]:
            failures.append(f"{key} differs from the manifest")
    if current["contract_status"] != contract.READY or contract.PENDING_DECISIONS:
        failures.append("contract is not READY_FOR_FIRST_FIT or has pending decisions")
    return failures


def verify_execution_manifest(path=MANIFEST_PATH, root=HERE, recompute=True):
    if not os.path.exists(path):
        return ["execution manifest does not exist"]
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    failures = verify_git_identity(manifest, root) + verify_data_identity(manifest)
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        failures.append("manifest version differs")
    if manifest.get("python") != platform.python_version() or manifest.get("packages") != package_versions():
        failures.append("python/package versions differ from the manifest")
    if manifest.get("stage1_plan_digest") != contract.STAGE1_PLAN["digest"]:
        failures.append("Stage-1 plan digest differs from the contract")
    doc = os.path.join(contract.CONTRACT_DIR, f"{contract.CONTRACT_VERSION}_{contract.READY.lower()}.json")
    if not os.path.exists(doc) or contract.file_sha256(doc) != manifest.get("contract_document_sha256"):
        failures.append("READY contract document missing or changed")
    if recompute and not failures:
        failures += contract.verify_inputs(recompute_logical=True, recompute_ledgers=True, root=root)
    return failures


class ExecutionAuthorisation:
    """Issued only by require_execution_manifest after full verification."""

    def __init__(self, manifest_path, manifest_sha256, head):
        self.manifest_path, self.manifest_sha256, self.head = manifest_path, manifest_sha256, head


_ISSUED = set()


def require_execution_manifest(path=MANIFEST_PATH, root=HERE):
    failures = verify_execution_manifest(path, root)
    if failures:
        raise HardStop(f"PRE-FIT EXECUTION MANIFEST INVALID: {failures}")
    token = ExecutionAuthorisation(path, contract.file_sha256(path), git_head(root))
    _ISSUED.add(id(token))
    return token


def _check_authorisation(token, root=HERE):
    if not isinstance(token, ExecutionAuthorisation) or id(token) not in _ISSUED:
        raise HardStop("fit refused: no verified execution authorisation")
    if (git_head(root) != token.head or not tracked_tree_clean(root)
            or not os.path.exists(token.manifest_path)
            or contract.file_sha256(token.manifest_path) != token.manifest_sha256
            or local_modules_outside_identity(root)):
        raise HardStop("fit refused: HEAD, tracked tree, manifest or loaded code changed after verification")


# ── plan ───────────────────────────────────────────────────────────────────
def _folds(pins, arm, sample, horizon, timing_intersection=False):
    record = pins["timing_intersection"][sample] if timing_intersection else pins["arms"][arm]["samples"][sample]
    return record["splits"][f"h{horizon}"]


def stage1_plan(pins=None):
    """Deterministic, pre-registered Stage-1 job schedule."""
    pins = pins or contract.load_pins()
    seeds = list(features.MODEL_SEEDS)
    jobs = []

    def add(category, arm, sample, variants, job_seeds, horizons, placebo=None, draws=(None,), timing=False):
        for h in horizons:
            split = _folds(pins, arm, sample, h, timing)
            for v in variants:
                for s in job_seeds:
                    for d in draws:
                        job = {"category": category, "arm": arm, "sample": sample, "variant": v,
                               "placebo_family": placebo, "placebo_draw": d, "seed": s, "horizon": h,
                               "folds": split["folds"], "split_digest": split["digest"]}
                        job["job_id"] = "|".join(str(job[k]) for k in ("category", "arm", "sample", "variant",
                                                                        "placebo_family", "placebo_draw", "seed", "horizon"))
                        jobs.append(job)

    add("confirmatory", GRADUATION_ARM, "A_full", ["A"], seeds, [1])
    for small, large, sample, family, _ in PAIRS:
        add("confirmatory", GRADUATION_ARM, sample, [small, large], seeds, [1])
    for small, large, sample, family, _ in PAIRS:
        add("placebo", GRADUATION_ARM, sample, [large], [PLACEBO_SEED], [1], family, range(features.PLACEBO_DRAWS))
    for small, large, sample, family, _ in PAIRS:
        add("robust_lag0", "lag0", sample, [small, large], seeds, [1])
    add("robust_all_variant", GRADUATION_ARM, "common_ABCD", ["A", "B", "C", "D"], seeds, [1])
    for arm in ("lag0", "lag1"):
        for small, large, sample, family, _ in PAIRS:
            add("robust_timing_intersection", arm, sample, [small, large], seeds, [1], timing=True)
    add("exploratory", GRADUATION_ARM, "A_full", ["A"], seeds, [2, 3, 4, 5])
    for small, large, sample, family, _ in PAIRS:
        add("exploratory", GRADUATION_ARM, sample, [small, large], seeds, [2, 3, 4, 5])
    summary = {}
    for job in jobs:
        rec = summary.setdefault(job["category"], {"models": 0, "fits": 0})
        rec["models"] += 1
        rec["fits"] += job["folds"]
    return {"jobs": jobs, "digest": features.sha256_json(jobs), "summary": summary,
            "total_models": len(jobs), "total_fits": sum(j["folds"] for j in jobs)}


def job_columns(family, job):
    columns = list(family[job["variant"]])
    features.assert_no_label_features(columns)
    if not columns or len(set(columns)) != len(columns):
        raise HardStop(f"empty or duplicated feature columns for {job['job_id']}")
    return columns


# ── context, rows, placebos ────────────────────────────────────────────────
def load_context():
    inputs = features.load_inputs()
    daily = features.broker_daily(inputs["axis"], inputs["broker"], inputs["calendar"])
    ledger = features.feature_ledger(daily["codes"])
    frames, masks = {}, {}
    for arm in features.TIMING_ARMS:
        frames[arm], _ = features.build_features(inputs, arm, daily=daily)
        masks[arm] = features.sample_masks(frames[arm], ledger)
    timing = features.timing_intersection_masks(masks["lag0"], masks["lag1"], frames["lag0"], frames["lag1"])
    panel = inputs["panel"].sort_values(KEY, kind="mergesort").reset_index(drop=True)
    if not panel[KEY].equals(frames["lag1"][KEY]):
        raise HardStop("panel and feature frames are not row-aligned")
    outcomes = evaluation.slot_outcomes(panel, inputs["calendar"])
    if not outcomes[KEY].equals(panel[KEY]):
        raise HardStop("execution outcomes are not row-aligned")
    return {"inputs": inputs, "daily": daily, "ledger": ledger, "family": features.family_columns(ledger),
            "frames": frames, "masks": masks, "timing": timing, "panel": panel, "calendar": inputs["calendar"],
            "labels": {h: evaluation.rank_label(panel, h).to_numpy() for h in features.HORIZONS},
            "outcomes": outcomes, "pins": contract.load_pins()}


def job_mask(ctx, job):
    if job["category"] == "robust_timing_intersection":
        return ctx["timing"][job["sample"]]
    return ctx["masks"][job["arm"]][job["sample"]]


def fold_rows(ctx, job):
    """FIT / EVAL rows need a label; TEST rows are every sample key on the test dates."""
    frame = ctx["frames"][job["arm"]]
    mask = job_mask(ctx, job)
    dates = frame["date"].to_numpy()
    record, splits = features.split_ledger(sorted(set(dates[mask])), ctx["calendar"], job["horizon"])
    if record["digest"] != job["split_digest"] or record["folds"] != job["folds"]:
        raise HardStop(f"split ledger differs from the pins for {job['job_id']}")
    labelled = ~np.isnan(ctx["labels"][job["horizon"]])
    rows = []
    for number, split in enumerate(splits, 1):
        fit = np.flatnonzero(mask & np.isin(dates, split["fit"]) & labelled)
        eva = np.flatnonzero(mask & np.isin(dates, split["eval"]) & labelled)
        test = np.flatnonzero(mask & np.isin(dates, split["test"]))
        if not len(fit) or not len(eva) or not len(test):
            raise HardStop(f"empty FIT/EVAL/TEST rows in fold {number} of {job['job_id']}")
        rows.append((number, fit, eva, test))
    return rows


def job_frame(ctx, job):
    """Feature frame for a job, with the frozen placebo transformation applied if any."""
    arm, family, draw = job["arm"], job["placebo_family"], job["placebo_draw"]
    frame = ctx["frames"][arm]
    if family is None:
        return frame
    mask = job_mask(ctx, job)
    if family == "B_alignment":
        return features.b_alignment_placebo(frame, mask, draw)
    if family == "D_state":
        return features.d_state_placebo(frame, mask, draw)
    if family == "C_identity":
        placebo, _ = features.build_features(ctx["inputs"], arm, c_placebo_draw=draw, daily=ctx["daily"])
        if not np.array_equal(features.sample_masks(placebo, ctx["ledger"])[job["sample"]], mask):
            raise HardStop("C identity placebo changed the sample key set")
        return placebo
    raise HardStop(f"unknown placebo family {family}")


def model_params(seed):
    """Every registered parameter, explicitly; the seed must be a registered model seed."""
    if int(seed) not in features.MODEL_SEEDS:
        raise HardStop(f"seed {seed} is not registered")
    params = dict(contract.MODEL["fixed_params"])
    params.update(objective=contract.MODEL["xgboost_objective"], eval_metric=contract.MODEL["xgboost_eval_metric"],
                  random_state=int(seed))
    return params


# ── the fit path (fails closed) ────────────────────────────────────────────
def run_job(ctx, job, authorisation):
    _check_authorisation(authorisation)
    contract.assert_frozen()
    import xgboost
    if xgboost.__version__ != contract.MODEL["estimator"].split()[-1]:
        raise HardStop(f"xgboost {xgboost.__version__} differs from the contract")

    frame = job_frame(ctx, job)
    columns = job_columns(ctx["family"], job)
    x = frame[columns].to_numpy(np.float64)
    y = ctx["labels"][job["horizon"]]
    pieces = []
    for number, fit, eva, test in fold_rows(ctx, job):
        _check_authorisation(authorisation)
        model = xgboost.XGBRegressor(**model_params(job["seed"]))
        FIT_COUNTER["models_constructed"] += 1
        model.fit(x[fit], y[fit], eval_set=[(x[eva], y[eva])], verbose=False)
        FIT_COUNTER["fits"] += 1
        prediction = model.predict(x[test])
        FIT_COUNTER["predictions"] += 1
        piece = ctx["panel"].loc[test, KEY].copy()
        piece["prediction"] = prediction.astype(np.float64)
        piece["fold"] = number
        piece["best_iteration"] = int(model.best_iteration)
        piece["fit_rows"], piece["eval_rows"], piece["test_rows"] = len(fit), len(eva), len(test)
        piece["feature_count"] = len(columns)
        for k in ("job_id", "category", "arm", "sample", "variant", "placebo_family", "placebo_draw", "seed", "horizon"):
            piece[k] = job[k]
        pieces.append(piece)
    return pd.concat(pieces, ignore_index=True)[list(PREDICTION_COLUMNS)]


def prediction_ledger_digest(ledger):
    ordered = ledger.sort_values(["job_id", "date", "ticker"], kind="mergesort")
    h = hashlib.sha256()
    h.update("\n".join(ordered["job_id"] + "|" + ordered["date"] + "|" + ordered["ticker"]).encode("utf-8"))
    h.update(np.ascontiguousarray(ordered["prediction"].to_numpy(np.float64)).astype("<f8").tobytes())
    return h.hexdigest()


# ── evaluation and graduation assembly ─────────────────────────────────────
def job_results(ctx, predictions, with_top3=True):
    """Per job: daily IC vs raw fwd_oo_h and daily Top-3 excess per execution view (H1 only)."""
    horizon = int(predictions["horizon"].iloc[0])
    merged = predictions.merge(ctx["panel"][KEY + [f"fwd_oo_{horizon}"]], on=KEY, how="left")
    ic = evaluation.daily_spearman_ic(merged, "prediction", f"fwd_oo_{horizon}")
    top3, execution = {}, {}
    if horizon == 1 and with_top3:
        outcomes = predictions[KEY + ["prediction"]].merge(ctx["outcomes"], on=KEY, how="left")
        days = list(outcomes.groupby("date", sort=True))
        for view in evaluation.VIEWS:
            top3[view] = pd.Series({d: evaluation.daily_top3_excess(day, "prediction", view) for d, day in days},
                                   dtype=float)
        picks = pd.concat([evaluation.select_top_k(day, "prediction") for _, day in days])
        decile = pd.concat([evaluation.select_top_k(day, "prediction", max(1, int(np.ceil(len(day) / 10))))
                            for _, day in days])
        for name, chosen in (("top3", picks), ("top_decile", decile)):
            held = chosen.loc[chosen["status"] == "EXIT_HOLD_THROUGH", "holding_sessions"]
            execution[name] = {"status": {k: int(v) for k, v in chosen["status"].value_counts().items()},
                               "holding_sessions": {str(int(k)): int(v)
                                                    for k, v in held.value_counts().sort_index().items()}}
    return {"daily_ic": ic, "top3": top3, "execution": execution}


def _key(category, sample, variant, seed, horizon=1, arm=GRADUATION_ARM, family=None, draw=None):
    return "|".join(str(v) for v in (category, arm, sample, variant, family, draw, seed, horizon))


def _bootstrap(series, index, block):
    values = series.to_numpy(float)
    theta = float(values.mean())
    boot = evaluation.circular_block_bootstrap_means(values, block, evaluation.BOOTSTRAP["replicates"],
                                                     evaluation.bootstrap_rng(index, block))
    se = float(boot.std(ddof=1))
    return {"theta": theta, "ci": evaluation.basic_bootstrap_ci(theta, boot),
            "p": evaluation.basic_bootstrap_pvalue(theta, boot), "se": se,
            "mde80_alpha_0.05": evaluation.minimum_detectable_effect(se, 0.05),
            "mde80_alpha_0.0167": evaluation.minimum_detectable_effect(se, 0.05 / 3)}


def graduation_report(results):
    """results: {job_id: job_results(...)}. Pre-registered H1 lag-1 assembly."""
    seeds = list(features.MODEL_SEEDS)
    report, pvalues = {"increments": {}}, {}
    for small, large, sample, family, index in PAIRS:
        ic_large = {s: results[_key("confirmatory", sample, large, s)]["daily_ic"] for s in seeds}
        ic_small = {s: results[_key("confirmatory", sample, small, s)]["daily_ic"] for s in seeds}
        primary, per_seed = evaluation.paired_delta_series(ic_large, ic_small)
        stats = {f"block_{b}": _bootstrap(primary, index, b)
                 for b in (evaluation.BOOTSTRAP["block_length"],) + tuple(evaluation.BOOTSTRAP["sensitivity_block_lengths"])}
        theta17 = float(per_seed[PLACEBO_SEED].mean())
        null = []
        for draw in range(features.PLACEBO_DRAWS):
            placebo_ic = results[_key("placebo", sample, large, PLACEBO_SEED, family=family, draw=draw)]["daily_ic"]
            if not placebo_ic.dropna().index.equals(ic_small[PLACEBO_SEED].dropna().index):
                raise HardStop(f"placebo dates differ for {family} draw {draw}")
            null.append(float((placebo_ic - ic_small[PLACEBO_SEED]).mean()))
        first, second = evaluation.period_halves(list(primary.index))
        top3 = {}
        for view in ("HOLD_THROUGH", "CASH"):
            top_large = {s: results[_key("confirmatory", sample, large, s)]["top3"][view] for s in seeds}
            top_small = {s: results[_key("confirmatory", sample, small, s)]["top3"][view] for s in seeds}
            top3[view] = evaluation.pairwise_top3_delta(top_large, top_small)[0]
        name = f"{large}-{small}"
        pvalues[name] = stats["block_10"]["p"]
        report["increments"][name] = {
            "primary": stats, "theta_seed17": theta17, "placebo": evaluation.placebo_statistics(theta17, null),
            "seed_means": [float(per_seed[s].mean()) for s in seeds],
            "half_means": [float(primary.loc[first].mean()), float(primary.loc[second].mean())],
            "top3_delta": top3, "n_ic_dates": int(len(primary))}
    holm = evaluation.holm(pvalues)
    for name, rec in report["increments"].items():
        rec["holm_adjusted_p"], rec["holm_rejected"] = holm[name]
        rec["category"], rec["conditions"] = evaluation.classify_increment(
            rec["primary"]["block_10"]["theta"], rec["holm_rejected"], rec["theta_seed17"], rec["placebo"],
            rec["seed_means"], rec["half_means"], rec["top3_delta"]["HOLD_THROUGH"], rec["top3_delta"]["CASH"])
    ic = {s: results[_key("confirmatory", "A_full", "A", s)]["daily_ic"] for s in seeds}
    top = {s: results[_key("confirmatory", "A_full", "A", s)]["top3"]["HOLD_THROUGH"] for s in seeds}
    ic_mean = pd.concat(ic, axis=1).mean(axis=1)
    top_mean = pd.concat(top, axis=1).mean(axis=1)
    ic_stats = _bootstrap(ic_mean, PRICE_CORE_BOOTSTRAP_INDEX, evaluation.BOOTSTRAP["block_length"])
    top_stats = _bootstrap(top_mean, PRICE_CORE_BOOTSTRAP_INDEX, evaluation.BOOTSTRAP["block_length"])
    ih, th = evaluation.period_halves(list(ic_mean.index)), evaluation.period_halves(list(top_mean.index))
    report["A"] = {"ic": ic_stats, "top3_hold_through": top_stats,
                   "category": evaluation.classify_price_core(
                       ic_stats["theta"], ic_stats["ci"], top_stats["theta"], top_stats["ci"],
                       [float(ic[s].mean()) for s in seeds],
                       [float(ic_mean.loc[ih[0]].mean()), float(ic_mean.loc[ih[1]].mean())],
                       [float(top[s].mean()) for s in seeds],
                       [float(top_mean.loc[th[0]].mean()), float(top_mean.loc[th[1]].mean())])}
    return report


SENSITIVITY_BOOTSTRAP_BASE = 100


def sensitivity_specs():
    """Pre-registered robustness / exploratory summaries, in bootstrap-stream order."""
    specs = [("robust_lag0", "lag0", sample, small, large, 1) for small, large, sample, _, _ in PAIRS]
    specs += [("robust_all_variant", GRADUATION_ARM, "common_ABCD", small, large, 1)
              for small, large in (("A", "B"), ("B", "C"), ("C", "D"))]
    for arm in ("lag0", "lag1"):
        specs += [("robust_timing_intersection", arm, sample, small, large, 1) for small, large, sample, _, _ in PAIRS]
    for h in (2, 3, 4, 5):
        specs.append(("exploratory", GRADUATION_ARM, "A_full", None, "A", h))
        specs += [("exploratory", GRADUATION_ARM, sample, small, large, h) for small, large, sample, _, _ in PAIRS]
    return specs


def sensitivity_report(results):
    """Never decides graduation; no Holm. Each entry: 5-seed mean daily IC delta (or A's mean IC),
    circular block bootstrap at the primary block length, basic 95% interval and basic p; H1 pairs add
    Top-3 deltas for every execution view. Confirmatory pairs add their Top-3 deltas for every view."""
    seeds = list(features.MODEL_SEEDS)
    block = evaluation.BOOTSTRAP["block_length"]
    report = {}
    for index, (category, arm, sample, small, large, horizon) in enumerate(sensitivity_specs()):
        def get(variant, seed):
            return results[_key(category, sample, variant, seed, horizon=horizon, arm=arm)]
        if small is None:
            series = pd.concat({s: get(large, s)["daily_ic"] for s in seeds}, axis=1).mean(axis=1)
            name = f"{category}|{arm}|{sample}|{large}|h{horizon}"
        else:
            series, _ = evaluation.paired_delta_series({s: get(large, s)["daily_ic"] for s in seeds},
                                                       {s: get(small, s)["daily_ic"] for s in seeds})
            name = f"{category}|{arm}|{sample}|{large}-{small}|h{horizon}"
        entry = {"ic": _bootstrap(series, SENSITIVITY_BOOTSTRAP_BASE + index, block), "n_ic_dates": int(len(series))}
        if horizon == 1 and small is not None:
            entry["top3_delta"] = {view: evaluation.pairwise_top3_delta(
                {s: get(large, s)["top3"][view] for s in seeds}, {s: get(small, s)["top3"][view] for s in seeds})[0]
                for view in evaluation.VIEWS}
        report[name] = entry
    for small, large, sample, _, _ in PAIRS:
        report[f"confirmatory_views|{GRADUATION_ARM}|{sample}|{large}-{small}|h1"] = {
            view: evaluation.pairwise_top3_delta(
                {s: results[_key("confirmatory", sample, large, s)]["top3"][view] for s in seeds},
                {s: results[_key("confirmatory", sample, small, s)]["top3"][view] for s in seeds})[0]
            for view in evaluation.VIEWS}
    return report


# ── dry run ────────────────────────────────────────────────────────────────
def dry_run():
    """Verify the plan end to end without constructing, fitting or predicting any model."""
    failures = contract.verify_inputs(recompute_logical=True, recompute_ledgers=True)
    if failures:
        raise HardStop(f"contract verification failed: {failures}")
    before = dict(FIT_COUNTER)
    plan = stage1_plan()
    if plan["digest"] != contract.STAGE1_PLAN["digest"] or plan["total_fits"] != contract.STAGE1_PLAN["totals"]["fits"]:
        raise HardStop("Stage-1 plan differs from the contract")
    ctx = load_context()
    job_ids = {job["job_id"] for job in plan["jobs"]}
    if len(job_ids) != len(plan["jobs"]):
        raise HardStop("duplicate Stage-1 job ids")
    for job in plan["jobs"]:
        model_params(job["seed"])
        job_columns(ctx["family"], job)
        if job["placebo_family"] is not None and job["seed"] != PLACEBO_SEED:
            raise HardStop(f"placebo job with an unregistered seed: {job['job_id']}")
    for category, arm, sample, small, large, horizon in sensitivity_specs():
        for variant in (small, large):
            for seed in (features.MODEL_SEEDS if variant else ()):
                if _key(category, sample, variant, seed, horizon=horizon, arm=arm) not in job_ids:
                    raise HardStop(f"sensitivity summary needs an unplanned job: {category} {sample} {variant}")
    checked, row_counts = set(), {}
    for job in plan["jobs"]:
        signature = (job["category"] == "robust_timing_intersection", job["arm"], job["sample"], job["horizon"])
        if signature in checked:
            continue
        checked.add(signature)
        rows = fold_rows(ctx, job)
        row_counts["|".join(map(str, signature))] = {
            "folds": len(rows), "fit_rows": int(sum(len(r[1]) for r in rows)),
            "eval_rows": int(sum(len(r[2]) for r in rows)), "test_rows": int(sum(len(r[3]) for r in rows))}
    placebo_digests = {}
    for small, large, sample, family, _ in PAIRS:
        job = {"category": "placebo", "arm": GRADUATION_ARM, "sample": sample, "placebo_family": family,
               "placebo_draw": 0}
        frame = job_frame(ctx, job)
        columns = [c for c in ctx["family"][large] if c not in ctx["family"][small]]
        placebo_digests[family] = features.frame_value_digest(frame, columns)
    if FIT_COUNTER != before:
        raise HardStop("dry run touched the fit path")
    placebo_schedule = {}
    for job in plan["jobs"]:
        if job["placebo_family"] is not None:
            rec = placebo_schedule.setdefault(job["placebo_family"], {"sample": job["sample"], "variant": job["variant"],
                                                                      "seed": job["seed"], "draws": 0})
            rec["draws"] += 1
    return {"contract_status": contract.contract_body()["status"], "plan_digest": plan["digest"],
            "summary": plan["summary"], "total_models": plan["total_models"], "total_fits": plan["total_fits"],
            "jobs_resolved": len(plan["jobs"]),
            "feature_columns_per_variant": {v: len(c) for v, c in ctx["family"].items()},
            "row_signatures_checked": len(checked), "row_counts": row_counts,
            "placebo_schedule": placebo_schedule, "placebo_draw0_block_digests": placebo_digests,
            "model_params": {s: model_params(s) for s in features.MODEL_SEEDS},
            "prediction_ledger_schema": list(PREDICTION_COLUMNS),
            "evaluation_plan": {"graduation": "graduation_report: B-A, C-B, D-C (lag1, h1) and family A",
                                "sensitivity": [list(map(str, spec)) for spec in sensitivity_specs()],
                                "views": list(evaluation.VIEWS)},
            "execution_diagnostics": evaluation.execution_diagnostics(ctx["outcomes"], ctx["calendar"]),
            "models_constructed": FIT_COUNTER["models_constructed"] - before["models_constructed"],
            "fits": FIT_COUNTER["fits"] - before["fits"],
            "predictions": FIT_COUNTER["predictions"] - before["predictions"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", "--verify-plan", dest="dry_run", action="store_true")
    mode.add_argument("--establish-manifest", metavar="CONFIRMATION")
    mode.add_argument("--verify-manifest", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(json.dumps(dry_run(), indent=1, sort_keys=True, default=str))
        return 0
    if args.establish_manifest is not None:
        path, digest = establish_execution_manifest(args.establish_manifest,
                                                    stage1_plan_digest=stage1_plan()["digest"])
        print(f"established {path} sha256={digest}")
        return 0
    if args.verify_manifest:
        failures = verify_execution_manifest()
        print("execution manifest:", "VALID" if not failures else "INVALID")
        for failure in failures:
            print("  -", failure)
        return 0 if not failures else 1
    print(json.dumps(execute_stage1(), indent=1, sort_keys=True, default=str))
    return 0


def execute_stage1(manifest_path=MANIFEST_PATH):
    """The real Stage-1 execution. Verifies the manifest first; writes new, never-overwritten outputs."""
    authorisation = require_execution_manifest(manifest_path)
    contract.assert_frozen()
    with open(manifest_path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    plan = stage1_plan()
    if plan["digest"] != manifest["stage1_plan_digest"]:
        raise HardStop("Stage-1 plan differs from the manifest")
    out_dir = os.path.join(contract.CONTRACT_DIR, f"stage1_{manifest['committed_head'][:12]}")
    if os.path.exists(out_dir):
        raise HardStop(f"{out_dir} exists; Stage-1 outputs are never overwritten")
    os.makedirs(out_dir)
    ctx = load_context()
    results, ledger_digests = {}, {}
    for category in dict.fromkeys(job["category"] for job in plan["jobs"]):
        pieces = []
        for job in (j for j in plan["jobs"] if j["category"] == category):
            predictions = run_job(ctx, job, authorisation)
            results[job["job_id"]] = job_results(ctx, predictions, with_top3=category != "placebo")
            pieces.append(predictions)
        ledger = pd.concat(pieces, ignore_index=True)
        ledger.to_parquet(os.path.join(out_dir, f"predictions_{category}.parquet"), index=False)
        ledger_digests[category] = prediction_ledger_digest(ledger)
    report = graduation_report(results)
    sensitivity = sensitivity_report(results)
    daily = {job_id: {"daily_ic": r["daily_ic"].to_dict(), "top3": {v: s.to_dict() for v, s in r["top3"].items()},
                      "execution": r["execution"]}
             for job_id, r in results.items()}
    with open(os.path.join(out_dir, "result_ledger.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"manifest_sha256": authorisation.manifest_sha256, "prediction_ledger_digests": ledger_digests,
                   "jobs": daily}, fh, sort_keys=True, default=str)
    with open(os.path.join(out_dir, "graduation_report.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"manifest_sha256": authorisation.manifest_sha256, "report": report,
                   "sensitivity_never_decides_graduation": sensitivity}, fh, indent=1, sort_keys=True, default=str)
    return {"out_dir": out_dir, "prediction_ledger_digests": ledger_digests}


if __name__ == "__main__":
    raise SystemExit(main())
