"""Deterministic entity canonicalization for the ownership dataset.

Establishes stable GLOBAL ownership entity IDs. This module decides identity
only; it creates no ML feature, maps no owner to a broker, and touches no
production ML path.

WHAT IT NEVER DOES
------------------
  - It never rewrites source data. investor_name_raw stays exactly the bytes
    that were captured. Case folding here is a COMPARISON device only.
  - It never silently merges an ambiguous pair. Anything the rules below do
    not confirm keeps its own separate global entity_id and is flagged for a
    human. Separate-plus-review is the default, not the exception.

MATCHING SEMANTICS (ordered, R0 -> R4, first match wins)
--------------------------------------------------------
  R0  byte-identical raw name.
  R1  normalize case + punctuation + whitespace ONLY, PRESERVING TOKEN ORDER.
      Identical token SEQUENCE -> merge.
  R2  R1, then remove only the PT and TBK tokens, at any position, then
      compare the remaining token sequence IN ORDER.
  R3  R1/R2 plus an explicitly curated spacing substitution
      (HONGKONG <-> HONG KONG), then ordered comparison.
  R4  everything else: stays separate, flagged for review.

Comparison is over an ORDERED token sequence, never an unordered multiset.
A multiset would make 'ALPHA BETA PT' and 'PT BETA ALPHA' the same entity;
ordered comparison correctly refuses them. Sequence order is why R1 and R2 are
genuinely distinct rules rather than one rule with an option flag: a leading
'PT ...' versus a trailing '..., PT' is a positional difference, which only R2's
token removal can reconcile.

WHY THE AUTO-MERGE SET IS THIS SMALL
------------------------------------
Entity IDs are global, so a false-positive merge is expensive and effectively
irreversible once downstream work joins on it. Only PT and TBK are removable.
They are Indonesian company-form markers (Perseroan Terbatas; Terbuka) that
appear in free position and carry no distinguishing identity.

Every other legal/entity suffix is DELIBERATELY NOT removable, because the
difference may be a real difference in legal identity:
  EFG BANK          vs EFG BANK AG                  -- distinct legal persons
  GREEN ERA ENERGY  vs GREEN ERA ENERGY PTE. LTD.
  GOLDWAVE CAPITAL  vs GOLDWAVE CAPITAL LIMITED
  GLAS TRUST (S)    vs GLAS TRUST SINGAPORE         -- jurisdiction qualifier
  ... SEKURITAS     vs ... SEKURITAS - REGISTRAR    -- role qualifier
  DBS BANK          vs DBS BANK S/A <FUND>          -- account designation
  PRIME HILL FUND   vs ZHAOCAI PRIME HILL FUND      -- containment is not identity
Branch, S/A, nominee, custody, account-designator and fund-containment cases
all stay separate for the same reason. Note that this module does NOT reuse
ownership_parse._LEGAL_SUFFIX_STOPWORDS: that set is intentionally broad
because it only FLAGS pairs for review, whereas these rules MERGE, and merging
demands the stricter set.

R3 substitutions are a curated allowlist, not a heuristic. Each entry is a
confirmed spacing variant of one name. Adding one is a review decision; bump
RULE_VERSION when the list changes so existing provenance stays interpretable.
"""
import hashlib
import re

# Bump on ANY change to the rules, the removable-token set, or the R3 list.
# Stored per row so a later reader can tell which regime produced a mapping.
RULE_VERSION = "entity-canon-v1"

# The ONLY tokens removable for matching. See module docstring before adding.
FORMATTING_ONLY_TOKENS = frozenset({"PT", "TBK"})

# Curated confirmed spacing variants, applied to the upper-cased string.
CONFIRMED_SPACING = (
    ("HONGKONG", "HONG KONG"),
)

_PUNCT_RE = re.compile(r"[.,()]")

AUTO_RULES = ("R0", "R1", "R2", "R3")

_CONFIDENCE = {
    "R0": "exact",
    "R1": "high",
    "R2": "high",
    "R3": "confirmed",
    "R4": "unresolved",
}


def normalize_tokens(name, apply_spacing=False):
    """Case/punctuation/whitespace normalization, PRESERVING TOKEN ORDER.

    Returns a list, never a set or Counter -- order is part of identity.
    Upper-casing is for comparison only; the caller keeps the raw name.
    """
    if not name:
        return []
    s = name.upper()
    if apply_spacing:
        for src, dst in CONFIRMED_SPACING:
            s = s.replace(src, dst)
    s = _PUNCT_RE.sub(" ", s)
    return [t for t in s.split() if t]


def _strip_form_tokens(tokens):
    return [t for t in tokens if t not in FORMATTING_ONLY_TOKENS]


def canonical_key(name):
    """The fully-reduced ordered key two names must share to be one entity.

    Applies every auto-merge reduction (spacing substitutions, then PT/TBK
    removal) to an ordered token sequence and joins it back with single
    spaces. Two names produce the same key exactly when R1, R2 or R3 would
    merge them, which keeps key equality and rule classification in agreement
    by construction rather than by two parallel implementations.

    Degenerate input (empty, or a name consisting only of PT/TBK) has no
    reducible core, so it falls back to the R1 form and, failing that, to the
    upper-cased original -- never to an empty key that would collapse
    unrelated rows together.
    """
    reduced = _strip_form_tokens(normalize_tokens(name, apply_spacing=True))
    if reduced:
        return " ".join(reduced)
    plain = normalize_tokens(name, apply_spacing=True)
    if plain:
        return " ".join(plain)
    return (name or "").upper().strip()


def entity_id_for_key(key):
    """Derive the global entity ID from the canonical key.

    A pure function of the key, so IDs are stable across reruns, machines and
    insertion order, and adding a member never renumbers an existing group.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"ENT-{digest}"


def classify_pair(name_a, name_b):
    """Return (rule, evidence) for a candidate pair. R4 means DO NOT merge."""
    if name_a == name_b:
        return "R0", "byte-identical raw name"

    ta, tb = normalize_tokens(name_a), normalize_tokens(name_b)
    if ta == tb:
        return "R1", "identical token sequence after case/punctuation/whitespace"

    sa, sb = _strip_form_tokens(ta), _strip_form_tokens(tb)
    if sa == sb:
        removed = sorted(set(ta) - set(sa) | set(tb) - set(sb))
        return "R2", f"identical token sequence after removing {removed}"

    # R3 only claims a pair that the curated substitution is what resolved.
    sub_a = normalize_tokens(name_a, apply_spacing=True)
    sub_b = normalize_tokens(name_b, apply_spacing=True)
    if sub_a == sub_b or _strip_form_tokens(sub_a) == _strip_form_tokens(sub_b):
        applied = [
            f"{src}->{dst}" for src, dst in CONFIRMED_SPACING
            if src in name_a.upper() or src in name_b.upper()
        ]
        return "R3", f"curated spacing substitution {applied}"

    diff_a = [t for t in sub_a if t not in sub_b]
    diff_b = [t for t in sub_b if t not in sub_a]
    detail = sorted(set(diff_a) | set(diff_b))
    if not detail:
        detail = ["token order differs"]
    return "R4", f"unconfirmed, requires review: {detail}"


def classify_name(name):
    """Classify a single raw name by the weakest rule needed to reduce it.

    This is the per-name analogue of classify_pair: it reports how far the
    reductions had to go to reach the name's canonical key, which is what the
    alias row records as provenance.
    """
    key = canonical_key(name)
    if name == key:
        return "R0", "raw name already canonical"
    if " ".join(normalize_tokens(name)) == key:
        return "R1", "case/punctuation/whitespace normalization only"

    spaced = name.upper()
    used_spacing = any(src in spaced for src, _ in CONFIRMED_SPACING)
    if used_spacing:
        applied = [
            f"{src}->{dst}" for src, dst in CONFIRMED_SPACING if src in spaced
        ]
        return "R3", f"curated spacing substitution {applied}"

    tokens = normalize_tokens(name)
    removed = sorted(set(tokens) - set(_strip_form_tokens(tokens)))
    if removed:
        return "R2", f"entity-form token removal {removed}"
    return "R1", "case/punctuation/whitespace normalization only"


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

NAME_SOURCES = (
    ("ownership_snapshot", "investor_name_raw"),
    ("ownership_change", "investor_name_raw"),
    ("custody_breakdown_snapshot", "investor_name_raw"),
)


def distinct_raw_names(conn):
    """Every distinct raw investor name across all name-bearing tables."""
    names = set()
    for table, column in NAME_SOURCES:
        for (value,) in conn.execute(
            f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL"
        ):
            if value:
                names.add(value)
    return names


def unresolved_candidate_names(conn):
    """Names in a candidate pair the deterministic rules did NOT confirm.

    entity_alias_candidate is per-ticker QA bookkeeping. A pair recorded there
    is a HYPOTHESIS of sameness; this asks the rules to adjudicate it. Where
    they refuse, both names are returned so each keeps its own global ID and
    is flagged -- the pair is not merged on the strength of the hypothesis.
    """
    unresolved = set()
    for name_a, name_b in conn.execute(
        "SELECT name_a, name_b FROM entity_alias_candidate"
    ):
        rule, _ = classify_pair(name_a, name_b)
        if rule == "R4":
            unresolved.add(name_a)
            unresolved.add(name_b)
    return unresolved


def backfill(conn, now, rule_version=RULE_VERSION):
    """Populate entity_canonical/entity_alias. Idempotent.

    Rerunning with unchanged inputs is a true no-op: not one byte of the two
    tables changes. IDs derive from the canonical key, first_seen_at is
    preserved on conflict, and each upsert carries a WHERE guard so a row
    whose values all match is not rewritten at all. Without that guard,
    updated_at would be restamped on every run, which would dirty the
    git-committed DB daily and make "nothing changed" indistinguishable
    from "everything was rewritten identically".

    Deliberately does NOT write investor_name_canonical on the source tables.
    That column stays NULL; identity is served by joining entity_alias on
    investor_name_raw, which keeps source rows byte-untouched.
    """
    names = distinct_raw_names(conn)
    needs_review = unresolved_candidate_names(conn)

    groups = {}
    for name in names:
        groups.setdefault(canonical_key(name), []).append(name)

    alias_rows, canon_rows = [], []
    for key, members in groups.items():
        eid = entity_id_for_key(key)
        members_sorted = sorted(members)
        # Representative display label only -- shortest, then alphabetical for
        # determinism. No source row is ever rewritten to this value.
        display = min(members_sorted, key=lambda n: (len(n), n))
        group_review = any(m in needs_review for m in members_sorted)
        canon_rows.append(
            (eid, key, display, len(members_sorted),
             1 if group_review else 0, rule_version, now, now)
        )
        for name in members_sorted:
            if name in needs_review:
                # The rules refused this name's candidate pair. It keeps its
                # own key-derived ID and is flagged; it is NOT merged into the
                # other side of the pair.
                rule, evidence = "R4", (
                    "unconfirmed alias candidate pair; kept separate for review"
                )
                status, confidence = "review", _CONFIDENCE["R4"]
            else:
                rule, evidence = classify_name(name)
                status, confidence = "auto", _CONFIDENCE[rule]
            alias_rows.append(
                (name, eid, rule, status, confidence, evidence,
                 rule_version, now, now)
            )

    conn.executemany(
        """INSERT INTO entity_canonical
             (entity_id, canonical_key, canonical_name, member_count,
              needs_review, rule_version, first_seen_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(entity_id) DO UPDATE SET
             canonical_name = excluded.canonical_name,
             member_count   = excluded.member_count,
             needs_review   = excluded.needs_review,
             rule_version   = excluded.rule_version,
             updated_at     = excluded.updated_at
           WHERE canonical_name IS NOT excluded.canonical_name
              OR member_count   IS NOT excluded.member_count
              OR needs_review   IS NOT excluded.needs_review
              OR rule_version   IS NOT excluded.rule_version""",
        canon_rows,
    )
    conn.executemany(
        """INSERT INTO entity_alias
             (name_raw, entity_id, match_rule, match_status, confidence,
              evidence, rule_version, first_seen_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(name_raw) DO UPDATE SET
             entity_id    = excluded.entity_id,
             match_rule   = excluded.match_rule,
             match_status = excluded.match_status,
             confidence   = excluded.confidence,
             evidence     = excluded.evidence,
             rule_version = excluded.rule_version,
             updated_at   = excluded.updated_at
           WHERE entity_id    IS NOT excluded.entity_id
              OR match_rule   IS NOT excluded.match_rule
              OR match_status IS NOT excluded.match_status
              OR confidence   IS NOT excluded.confidence
              OR evidence     IS NOT excluded.evidence
              OR rule_version IS NOT excluded.rule_version""",
        alias_rows,
    )
    conn.commit()
    return {
        "raw_names": len(names),
        "canonical_entities": len(groups),
        "aliases": len(alias_rows),
        "review": sum(1 for r in alias_rows if r[3] == "review"),
    }


def summarize(conn):
    """Read-only counts for reporting."""
    by_rule = dict(
        conn.execute("SELECT match_rule, COUNT(*) FROM entity_alias GROUP BY match_rule")
    )
    return {
        "raw_names": conn.execute(
            "SELECT COUNT(*) FROM entity_alias").fetchone()[0],
        "canonical_entities": conn.execute(
            "SELECT COUNT(*) FROM entity_canonical").fetchone()[0],
        "by_rule": {r: by_rule.get(r, 0) for r in ("R0", "R1", "R2", "R3", "R4")},
        "review": conn.execute(
            "SELECT COUNT(*) FROM entity_alias WHERE match_status = 'review'"
        ).fetchone()[0],
    }


def cross_ticker_entities(conn):
    """Entities whose raw names appear under more than one ticker.

    The reason IDs are global rather than per-ticker; reported so the effect
    is visible rather than assumed.
    """
    rows = conn.execute(
        """SELECT a.entity_id, c.canonical_name, COUNT(DISTINCT t.ticker) AS n
             FROM entity_alias a
             JOIN entity_canonical c ON c.entity_id = a.entity_id
             JOIN (SELECT ticker, investor_name_raw FROM ownership_snapshot
                   UNION SELECT ticker, investor_name_raw FROM ownership_change
                   UNION SELECT ticker, investor_name_raw
                     FROM custody_breakdown_snapshot) t
               ON t.investor_name_raw = a.name_raw
            GROUP BY a.entity_id
            HAVING n > 1
            ORDER BY n DESC, c.canonical_name"""
    ).fetchall()
    return rows
