"""Pure text/JSON parsers for the NeoBDM Stock Detail ownership panes.

No I/O, no Playwright, no DB here -- every function takes a string or dict
already captured by ownership_capture.py and returns plain dicts/lists so
this module can be unit-tested against fixed fixture strings (see
test_ownership_ingest.py) independent of the live site.

Interpretation rules encoded here (per STOCK_DETAIL_OWNERSHIP_DISCOVERY.md
and this turn's instructions -- do not relax without re-reading both):

  - KDA5%/PKDA5% eligibility is NOT "foreign holder". We don't gate on it
    here at all -- we just parse whatever the page shows. The scripless-
    >=5% explanation lives in the discovery doc, not in code that would
    filter data based on it.
  - custodian_pct_of_holder is % of the HOLDER's own position (confirmed,
    not assumed) -- the column name in the schema says so; this module
    just extracts the number, it does not renormalize it against company
    shares or free float.
  - Participant labels/codes (custodian names AND 2-letter codes) are
    kept exactly as rendered. Nothing here maps them to execution brokers.
  - No entity aliasing. normalize_entity_name() exists only to detect
    CANDIDATE variants for a human to review -- it is never used to merge
    rows or rewrite investor_name_raw.
"""
import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
    "jul": 7, "agu": 8, "ags": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
    "nov": 11, "des": 12, "dec": 12,
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PCT_TOKEN_RE = re.compile(r"^(<\s*1%|\d+(?:\.\d+)?%)$")
PURE_PCT_RE = re.compile(r"^(\d+(?:\.\d+)?)%$")
LABEL_PCT_RE = re.compile(r"^(.+?)\s+(\d+(?:\.\d+)?)%$")
SCRIP_FIELD_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?[KM]?)\s*lot\s+(<\s*1|\d+(?:\.\d+)?)%$")
LOT_VAL_RE = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)\s*([KM]?)\s*lot$")
LOT_MOVE_RE = re.compile(
    r"^(?:(Cust\.)\s*)?(?:([A-Z]{1,3})\s+)?([+-]?\d+(?:\.\d+)?)\s*([KM])?\s*lot$"
)

_LEGAL_SUFFIX_STOPWORDS = {
    "PT", "TBK", "PTE", "LTD", "LIMITED", "PUBLIC", "COMPANY", "CO",
    "CORP", "CORPORATION", "INC", "HOLDING", "HOLDINGS",
}


def _lines(text):
    return [ln.strip() for ln in (text or "").split("\n") if ln.strip()]


def parse_data_per_date(text):
    """'Data per 31 jul 2026' / 'Data per 27 aug 2026' -> '2026-07-31' etc."""
    m = re.search(r"Data per (\d{1,2}) (\w{3,4}) (\d{4})", text or "", re.IGNORECASE)
    if not m:
        return None
    d, mon, y = m.groups()
    mon_num = MONTHS.get(mon.lower())
    if not mon_num:
        return None
    return f"{int(y):04d}-{mon_num:02d}-{int(d):02d}"


def _num(raw):
    if raw is None:
        return None
    raw = raw.strip()
    if raw.startswith("<"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_lot(raw):
    """'181M lot' -> 181000000.0, '-1.65M lot' -> -1650000.0, '-' -> None."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "-":
        return None
    m = LOT_VAL_RE.match(raw)
    if not m:
        return None
    sign, num, suf = m.groups()
    val = float(num)
    if suf == "K":
        val *= 1_000
    elif suf == "M":
        val *= 1_000_000
    if sign == "-":
        val = -val
    return val


def parse_scrip_field(raw):
    """'181M lot 21.0%' -> (181000000.0, 21.0, raw). '-' -> (None, None, raw)."""
    if raw is None:
        return None, None, None
    raw = raw.strip()
    if raw == "-":
        return None, None, raw
    m = SCRIP_FIELD_RE.match(raw)
    if not m:
        return None, None, raw
    lot_str, pct_str = m.groups()
    return parse_lot(f"{lot_str} lot"), _num(pct_str), raw


def parse_holder_count(raw):
    """'101K' -> 101000.0, '43.6K' -> 43600.0, '783K' -> 783000.0."""
    if raw is None:
        return None
    m = re.match(r"^([\d.]+)\s*([KM]?)$", raw.strip())
    if not m:
        return None
    num, suf = m.groups()
    val = float(num)
    if suf == "K":
        val *= 1_000
    elif suf == "M":
        val *= 1_000_000
    return val


def normalize_entity_name(name):
    """Conservative normalizer for CANDIDATE alias detection only.

    Strips punctuation, common legal-entity stopwords, and collapses the
    one confirmed spacing variant (HONGKONG/HONG KONG) plus whitespace.
    Deliberately does NOT reorder tokens or fuzzy-match -- it should only
    catch noise-level variants, not genuinely different entities that
    happen to share words (e.g. it must NOT equate "PRIME HILL FUND" and
    "ZHAOCAI PRIME HILL FUND" -- those stay separate, unconfirmed, human
    call only).
    """
    if not name:
        return ""
    s = name.upper()
    s = re.sub(r"[.,()]", " ", s)
    s = s.replace("HONGKONG", "HONG KONG")
    tokens = [t for t in s.split() if t and t not in _LEGAL_SUFFIX_STOPWORDS]
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# KDA 1% -- #insider-current
# ---------------------------------------------------------------------------

def parse_kda1_current(text):
    """Returns {'snapshot_date': str|None, 'rows': [ {..} ]}."""
    text = text or ""
    snapshot_date = parse_data_per_date(text)
    blocks = re.split(r"\n\s*\n", text.strip())
    rows = []
    for block in blocks[1:]:  # blocks[0] is the header (Data per.../Total.../column titles)
        lines = _lines(block)
        if len(lines) < 3:
            continue
        name, category_line, data_line = lines[0], lines[1], lines[2]
        is_foreign = category_line.endswith(" F")
        category = category_line[:-2].strip() if is_foreign else category_line
        # data_line looks like "\t34.6%\t181M lot 21.0%\t118M lot 13.7%"
        # innerText strips the leading tab when .strip()'d per-line above,
        # so split on tab against the *unstripped* segment instead.
        raw_cells = [c for c in block.split("\n") if "\t" in c]
        tab_line = raw_cells[0] if raw_cells else data_line
        parts = [p.strip() for p in tab_line.split("\t") if p.strip()]
        if len(parts) < 3:
            continue
        pct_raw, scrip_raw_field, scripless_raw_field = parts[0], parts[1], parts[2]
        scrip_lot, scrip_pct, scrip_raw = parse_scrip_field(scrip_raw_field)
        scripless_lot, scripless_pct, scripless_raw = parse_scrip_field(scripless_raw_field)
        rows.append({
            "investor_name_raw": name,
            "investor_category": category,
            "is_foreign": is_foreign,
            "ownership_pct_raw": pct_raw,
            "ownership_pct": _num(pct_raw.rstrip("%")) if pct_raw and not pct_raw.startswith("<") else None,
            "scrip_lot": scrip_lot, "scrip_pct": scrip_pct, "scrip_raw": scrip_raw,
            "scripless_lot": scripless_lot, "scripless_pct": scripless_pct,
            "scripless_raw": scripless_raw,
        })
    return {"snapshot_date": snapshot_date, "rows": rows}


# ---------------------------------------------------------------------------
# PKDA 1% -- #insider-moves
# ---------------------------------------------------------------------------

def parse_pkda1_moves(text):
    """Returns {'rows': [ {change_date, investor_name_raw, ...} ]}."""
    tokens = _lines(text)
    date_idxs = [i for i, t in enumerate(tokens) if DATE_RE.match(t)]
    rows = []
    for n, di in enumerate(date_idxs):
        end = date_idxs[n + 1] if n + 1 < len(date_idxs) else len(tokens)
        row_tokens = tokens[di + 1:end]
        if not row_tokens:
            continue
        change_date = tokens[di]
        name = row_tokens[0]
        rest = row_tokens[1:]
        pct_idx = next((i for i, t in enumerate(rest) if PCT_TOKEN_RE.match(t)), None)
        if pct_idx is None:
            continue
        category_tokens = rest[:pct_idx]
        is_foreign = "F" in category_tokens
        category = next((t for t in category_tokens if t != "F"), None)
        pct_raw = rest[pct_idx]
        scrip_raw = rest[pct_idx + 1] if pct_idx + 1 < len(rest) else None
        scripless_raw = rest[pct_idx + 2] if pct_idx + 2 < len(rest) else None
        note = rest[pct_idx + 3] if pct_idx + 3 < len(rest) else None
        rows.append({
            "change_date": change_date,
            "investor_name_raw": name,
            "investor_category": category,
            "is_foreign": is_foreign,
            "resulting_ownership_pct_raw": pct_raw,
            "resulting_ownership_pct": _num(pct_raw.rstrip("%")) if not pct_raw.startswith("<") else None,
            "scrip_lot_change": parse_lot(scrip_raw) if scrip_raw else None,
            "scripless_lot_change": parse_lot(scripless_raw) if scripless_raw else None,
            "note": note if note and ("PKDA" in note) else None,
        })
    return {"rows": rows}


# ---------------------------------------------------------------------------
# KDA 5% -- #insider5p-current
# ---------------------------------------------------------------------------

def parse_kda5_current(text):
    """Returns {'snapshot_date': str|None, 'holders': [ {name, total_pct,
    is_foreign, breakdown: [(label, pct), ...]} ]}."""
    text = text or ""
    snapshot_date = parse_data_per_date(text)
    lines = [ln for ln in _lines(text) if not ln.lower().startswith("data per")]
    holders = []
    cur = None
    for line in lines:
        if line == "F":
            if cur is not None:
                cur["is_foreign"] = True
            continue
        m_pure = PURE_PCT_RE.match(line)
        if m_pure and cur is not None and cur["total_pct"] is None:
            cur["total_pct"] = float(m_pure.group(1))
            continue
        m_label = LABEL_PCT_RE.match(line)
        if m_label and cur is not None and cur["total_pct"] is not None:
            cur["breakdown"].append((m_label.group(1).strip(), float(m_label.group(2))))
            continue
        # otherwise: a new holder name line
        if cur is not None:
            holders.append(cur)
        cur = {"investor_name_raw": line, "total_pct": None, "is_foreign": False, "breakdown": []}
    if cur is not None:
        holders.append(cur)
    return {"snapshot_date": snapshot_date, "holders": holders}


# ---------------------------------------------------------------------------
# PKDA 5% -- #insider5p-moves
# ---------------------------------------------------------------------------

def parse_pkda5_moves(text):
    """Returns {'rows': [ {change_date, investor_name_raw, is_foreign,
    is_custodian_move, custodian_or_code, lot_change} ]}."""
    tokens = [t for t in _lines(text) if t not in ("Tanggal", "Investor", "Perubahan")]
    date_idxs = [i for i, t in enumerate(tokens) if DATE_RE.match(t)]
    rows = []
    for n, di in enumerate(date_idxs):
        end = date_idxs[n + 1] if n + 1 < len(date_idxs) else len(tokens)
        row_tokens = tokens[di + 1:end]
        if not row_tokens:
            continue
        change_date = tokens[di]
        is_foreign = "F" in row_tokens
        name_tokens = [t for t in row_tokens if t != "F" and not LOT_MOVE_RE.match(t)]
        move_token = next((t for t in row_tokens if LOT_MOVE_RE.match(t)), None)
        name = " ".join(name_tokens) if name_tokens else None
        if move_token is None or name is None:
            continue
        m = LOT_MOVE_RE.match(move_token)
        cust_lit, code, num, suf = m.groups()
        val = float(num)
        if suf == "K":
            val *= 1_000
        elif suf == "M":
            val *= 1_000_000
        rows.append({
            "change_date": change_date,
            "investor_name_raw": name,
            "is_foreign": is_foreign,
            "is_custodian_move": bool(cust_lit),
            "custodian_or_code": code,
            "lot_change": val,
        })
    return {"rows": rows}


# ---------------------------------------------------------------------------
# Free float / scripless / holder badge
# ---------------------------------------------------------------------------

def parse_badge(text):
    text = text or ""
    out = {"free_float_pct": None, "scripless_pct": None,
           "holder_count_raw": None, "holder_count_approx": None,
           "date_range_end": None}
    m = re.search(r"Scripless:\s*([\d.]+)%", text)
    if m:
        out["scripless_pct"] = float(m.group(1))
    m = re.search(r"Free Float:\s*([\d.]+)%", text)
    if m:
        out["free_float_pct"] = float(m.group(1))
    m = re.search(r"Holder:\s*([\d.]+[KM]?)", text)
    if m:
        out["holder_count_raw"] = m.group(1)
        out["holder_count_approx"] = parse_holder_count(m.group(1))
    m = re.search(r"Data ranges from [\d-]+ [\d:]+ to (\d{4}-\d{2}-\d{2})", text)
    if m:
        out["date_range_end"] = m.group(1)
    return out


# ---------------------------------------------------------------------------
# Balance Position Chart -- full trace payload
# ---------------------------------------------------------------------------

LINE_SERIES_MAP = {
    "%Retail": "pct_retail", "%Institusi": "pct_institusi",
    "%Foreign": "pct_foreign", "scripless": "pct_scripless",
}


def _category_slug(name):
    parts = name.split()
    if not parts:
        return name.lower()
    prefix = "local" if parts[0].lower() == "lokal" else "foreign"
    rest = "_".join(p.lower() for p in parts[1:])
    return f"{prefix}_{rest}" if rest else prefix


def _decode_y(y):
    """Decode a Dash/Plotly y-value payload.

    The Dash _dash-update-component endpoint returns numeric arrays in one of
    two forms depending on the Plotly version and array dtype:
      - A plain Python list: [1.0, 2.0, ...]  -- returned as-is.
      - A binary-encoded dict: {"dtype": "f8", "bdata": "<base64>"}
        where "f8" means little-endian IEEE 754 float64 (numpy dtype '<f8').

    Any other dict shape (unknown dtype, missing bdata) is treated as an empty
    list so the caller skips the trace gracefully rather than crashing.
    """
    if isinstance(y, list):
        return y
    if not isinstance(y, dict):
        return []
    dtype = y.get("dtype", "")
    bdata = y.get("bdata", "")
    if not bdata:
        return []
    import base64, struct
    raw = base64.b64decode(bdata)
    if dtype == "f8":
        n = len(raw) // 8
        return list(struct.unpack_from(f"<{n}d", raw))
    if dtype == "f4":
        n = len(raw) // 4
        return list(struct.unpack_from(f"<{n}f", raw))
    if dtype in ("i4", "i8"):
        size = 4 if dtype == "i4" else 8
        fmt = "i" if dtype == "i4" else "q"
        n = len(raw) // size
        return list(struct.unpack_from(f"<{n}{fmt}", raw))
    return []  # unknown dtype -- skip gracefully


def parse_balance_position(traces):
    """traces: list of {'name': str, 'x': [...], 'y': list-or-bdata-dict}.

    The Dash payload encodes numeric arrays as {"dtype":"f8","bdata":"<b64>"}
    rather than plain lists; _decode_y() handles both forms transparently.

    Returns {'monthly': [ {period_date, category, lots} ],
             'summary': [ {period_date, pct_retail, pct_institusi,
                            pct_foreign, pct_scripless} ]}.
    """
    monthly = []
    summary_by_date = {}
    for tr in traces or []:
        name = tr.get("name")
        xs = tr.get("x") or []
        ys = _decode_y(tr.get("y"))
        if name in LINE_SERIES_MAP:
            field = LINE_SERIES_MAP[name]
            for x, y in zip(xs, ys):
                d = str(x)[:10]
                summary_by_date.setdefault(d, {"period_date": d})[field] = y
        elif name:
            cat = _category_slug(name)
            for x, y in zip(xs, ys):
                monthly.append({"period_date": str(x)[:10], "category": cat, "lots": y})
    summary = sorted(summary_by_date.values(), key=lambda r: r["period_date"])
    return {"monthly": monthly, "summary": summary}
