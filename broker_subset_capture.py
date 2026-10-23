"""Durable all-ticker OBSERVED_BROKER_SUBSET acquisition, never full101 metrics.

The explicit subset is part of the contract. Original provider response bytes are
content-addressed and compressed without changing their uncompressed hash.
Existing broker-learning caches, databases and full-universe guards are untouched.
"""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import time
from urllib.parse import parse_qs

import broker_collect as bc
import inventory_capture as ic
from ticker_registry import load_registry

HERE = Path(__file__).resolve().parent
DEFAULT_SUBSET = HERE / "broker_subset.json"
RECEIPT_NAME = "broker-subset-capture-receipt.json"

def stamp():
    return datetime.now(timezone.utc).isoformat()

def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def atomic(path, raw):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+".tmp")
    with temp.open("wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp,path)

def immutable(path, raw):
    path=Path(path)
    if path.exists():
        if path.read_bytes()!=raw:
            raise ValueError("Immutable source evidence differs: "+path.name)
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open("xb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError:
        if path.read_bytes()!=raw:
            raise ValueError("Immutable source evidence collision")

def subset_contract(path=DEFAULT_SUBSET):
    obj=json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(obj,dict) or not isinstance(obj.get("name"),str) or not obj["name"]:
        raise ValueError("Named explicit broker subset required")
    codes=tuple(bc.cg.check_explicit_codes(obj.get("codes",())))
    return dict(name=obj["name"],codes=list(codes),sha256=digest(encoded(list(codes))),
                coverage_scope="OBSERVED_BROKER_SUBSET",full_broker_universe=False,
                client_classification="UNKNOWN")

def year_before(session):
    day=date.fromisoformat(session)
    try:
        return day.replace(year=day.year-1).isoformat()
    except ValueError:
        return day.replace(year=day.year-1,day=28).isoformat()

@contextmanager
def store_lock(root):
    root=Path(root)
    root.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(root/".capture-lock.sqlite3",timeout=0)
    try:
        conn.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        conn.rollback()
        conn.close()

class BudgetExhausted(RuntimeError):
    pass

class SourcePaused(RuntimeError):
    pass

class EvidenceTransport:
    def __init__(self,get,root,max_requests,sleep=time.sleep):
        self.get=get
        self.root=Path(root)
        self.max_requests=max_requests
        self.sleep=sleep
        self.calls=0
        self.evidence={}
        self.paused=None
    def __call__(self,query):
        if self.paused:
            raise SourcePaused(self.paused)
        if self.calls>=self.max_requests:
            raise BudgetExhausted("bounded request budget exhausted")
        if self.calls:
            if self.calls % bc.REST_EVERY==0:
                self.sleep(bc.REST_FOR)
            self.sleep(bc.PACE+random.random()*bc.JITTER)
        self.calls+=1
        response=self.get(query)
        raw=ic.raw_body(response)
        basis="ORIGINAL_RESPONSE_BYTES"
        if raw is None:
            raw=response.text().encode("utf-8")
            basis="DECODED_UTF8_TEXT_NOT_ORIGINAL_BYTES"
        body_sha=digest(raw)
        compressed=gzip.compress(raw,mtime=0)
        rel=Path("objects")/body_sha[:2]/(body_sha+".response.gz")
        immutable(self.root/rel,compressed)
        record=dict(path=rel.as_posix(),sha256=digest(compressed),
                    source_sha256=body_sha,hash_basis=basis,bytes=len(raw),
                    query_sha256=digest(query.encode()),captured_at=stamp(),
                    http_status=int(response.status))
        ticker=parse_qs(query).get("symbol",[""])[0]
        self.evidence.setdefault(ticker,[]).append(record)
        if int(response.status) in (401,403,429):
            self.paused="PROVIDER_HTTP_"+str(response.status)
        elif b"abnormal" in raw.lower():
            self.paused="PROVIDER_ABNORMAL_USAGE_RESPONSE"
        return response

def validate_stored(root,item,subset,expected,ticker=None):
    if item.get("state")!="COMPLETE" or item.get("subset_sha256")!=subset["sha256"]:
        return False
    artifacts=item.get("artifacts") or []
    if not artifacts or item.get("last_session")!=expected:
        return False
    for artifact in artifacts:
        path=(Path(root)/artifact["path"]).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
            return False
        if digest(path.read_bytes())!=artifact["sha256"]:
            return False
        try:
            if digest(gzip.decompress(path.read_bytes()))!=artifact.get("source_sha256"):
                return False
        except (OSError,EOFError):
            return False
    try:
        final=artifacts[-1]
        body=json.loads(gzip.decompress((Path(root)/final["path"]).read_bytes()))
        identity=ticker or item.get("source_identity")
        if not identity or (body.get("meta") or {}).get("symbol")!=identity:
            return False
        data=bc.validate_envelope(body,identity,subset["codes"])
        if data["date"][-1]!=expected or digest(encoded(data["ohlc"]))!=item.get("ohlc_sha256"):
            return False
    except (ValueError,KeyError,TypeError,AttributeError,OSError,EOFError,bc.FetchRejected):
        return False
    return True

def observation_item(data,ticker,subset,expected,artifacts):
    """Record usable source history without certifying the requested session."""
    dates=data["date"]
    if not artifacts:
        raise ValueError("Successful capture lacks durable source bytes")
    if not dates:
        raise ValueError("Nonempty validated source history required")
    last=dates[-1]
    state="COMPLETE" if last==expected else "SOURCE_STALE" if last<expected else "SOURCE_AHEAD"
    item=dict(state=state,last_session=last,first_session=dates[0],
        expected_session=expected,observed_sessions=len(dates),artifacts=artifacts,
        ohlc_sha256=digest(encoded(data["ohlc"])),subset_sha256=subset["sha256"],
        successful_source_answer=True,requested_session_available=last==expected,
        history_certification="SOURCE_SPAN_RECORDED_SESSION_COMPLETENESS_UNVERIFIED",
        price_certification="RAW_OBSERVATION_NOT_FINANCIAL_ADMISSION",source_identity=ticker)
    if state!="COMPLETE":
        item["reason"]=("SOURCE_HISTORY_ENDS_BEFORE_REQUESTED_SESSION" if last<expected
                        else "SOURCE_HISTORY_EXCEEDS_REQUESTED_CUTOFF")
    return item

def replay_observation(root,ticker,item,subset,expected):
    """Reclassify only after rehashing the saved original and parsing it again."""
    artifacts=item.get("artifacts") or []
    if not artifacts:
        raise ValueError("No preserved source response")
    root=Path(root).resolve()
    raw=None
    for artifact in artifacts:
        path=(root/artifact["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Source object escapes archive")
        compressed=path.read_bytes()
        if digest(compressed)!=artifact["sha256"]:
            raise ValueError("Compressed source hash mismatch")
        raw=gzip.decompress(compressed)
        if digest(raw)!=artifact["source_sha256"]:
            raise ValueError("Original source hash mismatch")
    if artifacts[-1].get("http_status")!=200 or artifacts[-1].get("hash_basis")!="ORIGINAL_RESPONSE_BYTES":
        raise ValueError("Original successful source response required")
    env=json.loads(raw)
    if (env.get("meta") or {}).get("symbol")!=ticker:
        raise ValueError("Source ticker identity not verified")
    try:
        data=bc.validate_envelope(env,ticker,subset["codes"])
    except bc.EmptyResponse:
        return dict(state="NO_DATA",reason="SOURCE_RETURNED_EMPTY_DATE_AXIS",
                    successful_source_answer=True,requested_session_available=False,
                    source_identity=ticker,subset_sha256=subset["sha256"],artifacts=artifacts,
                    price_certification="RAW_OBSERVATION_NOT_FINANCIAL_ADMISSION")
    return observation_item(data,ticker,subset,expected,artifacts)

def coverage_summary(receipt,members):
    states=[receipt["per_ticker"][t]["state"] for t in members]
    eligible=[t for t in members if t not in bc.NOT_A_TICKER]
    receipt["counts"]={s:states.count(s) for s in sorted(set(states))}
    receipt["eligible_scope_complete"]=all(receipt["per_ticker"][t]["state"]=="COMPLETE" for t in eligible)
    receipt["acquisition_status"]="COMPLETE" if all(s=="COMPLETE" for s in states) else "PARTIAL"
    receipt["eligibility"]=dict(registry_entries=len(members),source_eligible=len(eligible),
        explicitly_ineligible=sorted(set(members)&bc.NOT_A_TICKER),
        scope_evidence="broker_collect.NOT_A_TICKER")
    receipt["source_response_counts"]={
        "validated_nonempty":sum(receipt["per_ticker"][t]["state"] in
            {"COMPLETE","SOURCE_STALE","SOURCE_AHEAD"} for t in eligible),
        "empty":states.count("NO_DATA"),"current":states.count("COMPLETE"),
        "stale":states.count("SOURCE_STALE"),"ahead":states.count("SOURCE_AHEAD")}

def audit_archive(root,registry,subset,expected,checkpoint=None):
    """Offline receipt repair. Original responses and old receipts are immutable."""
    root=Path(root).resolve()
    run_dir=root/"runs"/(expected+"_"+registry.sha256[:16]+"_"+subset["sha256"][:16])
    with store_lock(root):
        original=(run_dir/RECEIPT_NAME).read_bytes()
        receipt=json.loads(original)
        if (receipt.get("registry_sha256")!=registry.sha256 or receipt.get("subset")!=subset
                or receipt.get("trade_date")!=expected
                or set(receipt.get("per_ticker",{}))!=set(registry.tickers)):
            raise ValueError("Archive receipt contract mismatch")
        signatures={}
        changed={}
        for ticker,item in list(receipt["per_ticker"].items()):
            if ticker in bc.NOT_A_TICKER:
                continue
            if item.get("state")=="MISSING" and not item.get("artifacts"):
                continue
            try:
                observed=replay_observation(root,ticker,item,subset,expected)
                signature=observed.get("ohlc_sha256")
                if signature and signature in signatures:
                    raise ValueError("Full OHLCV identical to "+signatures[signature])
                if signature:
                    signatures[signature]=ticker
            except (ValueError,KeyError,TypeError,OSError,EOFError,bc.FetchRejected) as exc:
                observed=dict(item,state="FAILED",reason=bc._safe_error_local(exc))
            if observed["state"]!=item.get("state"):
                changed[ticker]=dict(before=item.get("state"),after=observed["state"],
                                     previous_reason=item.get("reason"))
            receipt["per_ticker"][ticker]=observed
        coverage_summary(receipt,registry.tickers)
        receipt["processing_status"]="COMPLETE"
        receipt["archive_audit"]=dict(source_receipt_sha256=digest(original),changes=changed,
            audited_at=stamp(),provider_requests=0,original_bytes_modified=False)
        publish_receipt(root,run_dir,receipt)
        if checkpoint:
            checkpoint(root,run_dir,receipt)
        return receipt

def publish_receipt(root,run_dir,receipt):
    raw=encoded(receipt)
    immutable(Path(run_dir)/"receipt_versions"/(digest(raw)+".json"),raw)
    atomic(Path(run_dir)/RECEIPT_NAME,raw)
    atomic(Path(root)/RECEIPT_NAME,raw)

def capture(root,registry,subset,expected_session,request_get,*,max_requests=100,
            requested_tickers=None,live=False,sleep=time.sleep,collector=None,
            checkpoint=None,checkpoint_every=20):
    if max_requests<1:
        raise ValueError("Positive request budget required")
    expected=date.fromisoformat(expected_session).isoformat()
    if expected!=expected_session:
        raise ValueError("Canonical session date required")
    start=year_before(expected)
    members=tuple(registry.tickers)
    wanted=set(members if requested_tickers is None else requested_tickers)
    if not wanted <= set(members):
        raise ValueError("Requested ticker outside authoritative registry")
    run_key=expected+"_"+registry.sha256[:16]+"_"+subset["sha256"][:16]
    root=Path(root).resolve()
    run_dir=root/"runs"/run_key
    with store_lock(root):
        path=run_dir/RECEIPT_NAME
        if path.exists():
            receipt=json.loads(path.read_text(encoding="utf-8"))
            if receipt.get("registry_sha256")!=registry.sha256 or receipt.get("subset")!=subset:
                raise ValueError("Resume contract mismatch")
        else:
            receipt=dict(schema="spectra-collector-receipt.v1",
                source="neobdm_inventory_explicit_subset",registry_sha256=registry.sha256,
                registry=registry.metadata(),trade_date=expected,
                requested_start_date=start,requested_end_date=expected,subset=subset,
                coverage_scope="OBSERVED_BROKER_SUBSET",full_broker_universe=False,
                source_scope=dict(investor_type="A",board_scope="UNKNOWN"),
                acquisition_status="PARTIAL",per_ticker={t:dict(state="MISSING",reason="NOT_ATTEMPTED") for t in members},
                history_certification="NOT_CERTIFIED_BY_SPAN_OR_ROW_COUNT")
        transport=EvidenceTransport(request_get,root,max_requests,sleep)
        receipt["source_certification"]="VERIFIED_REQUESTED_SCOPE" if live else "OFFLINE_TEST_ONLY"
        receipt["persistence"]=dict(status="LOCAL_ONLY",retrieval_status="NOT_TESTED")
        receipt["processing_status"]="IN_PROGRESS"
        receipt["captured_at"]=stamp()
        publish_receipt(root,run_dir,receipt)
        collect=collector or bc.collect
        # Unknown members get a first opportunity before prior failures retry.
        # A permanently unavailable first ticker cannot starve later batches.
        for ticker in members:
            if ticker in bc.NOT_A_TICKER:
                receipt["per_ticker"][ticker]=dict(state="SOURCE_UNAVAILABLE",
                    reason="INDEX_SYMBOL_NOT_EQUITY",subset_sha256=subset["sha256"])
            elif receipt["per_ticker"][ticker].get("state")=="COMPLETE" and not validate_stored(
                    root,receipt["per_ticker"][ticker],subset,expected,ticker):
                receipt["per_ticker"][ticker]["state"]="FAILED"
                receipt["per_ticker"][ticker]["reason"]="STORED_ARTIFACT_INVALID"
        planned=sorted(members,key=lambda t:receipt["per_ticker"][t].get("state")!="MISSING")
        processed=0
        for ticker in planned:
            if ticker not in wanted:
                continue
            prior=receipt["per_ticker"][ticker]
            if validate_stored(root,prior,subset,expected,ticker):
                continue
            if ticker in bc.NOT_A_TICKER:
                continue
            if transport.calls>=max_requests:
                break
            cache_root=run_dir/"cache"
            receipt["persistence"]=dict(status="LOCAL_ONLY",retrieval_status="NOT_TESTED")
            try:
                result=collect([ticker],bc.MODE_DAILY,raw_dir=str(cache_root),
                    now=datetime.combine(date.fromisoformat(expected),datetime.min.time(),
                                         tzinfo=timezone.utc),
                    request_get=transport,sleep=sleep,codes=subset["codes"],
                    pipeline_run_id=run_key,start_date_override=start,end_date_override=expected,
                    relogin=getattr(request_get,"relogin",None))
                if ticker in result.get("empty",{}):
                    item=dict(state="NO_DATA",reason="SOURCE_RETURNED_EMPTY_DATE_AXIS",
                              successful_source_answer=True)
                elif ticker not in result["ok"]:
                    item=dict(state="FAILED",reason=result.get("failed",{}).get(ticker,"FETCH_FAILED"))
                else:
                    item=replay_observation(root,ticker,
                        dict(artifacts=transport.evidence.get(ticker,[])),subset,expected)
                    ohlc_sha=item["ohlc_sha256"]
                    clones=[t for t,row in receipt["per_ticker"].items()
                            if t!=ticker and row.get("state") in {"COMPLETE","SOURCE_STALE","SOURCE_AHEAD"}
                            and row.get("ohlc_sha256")==ohlc_sha]
                    if clones:
                        raise ValueError("Full OHLCV identical to "+clones[0]+"; identity review required")
            except Exception as exc:
                item=dict(state="FAILED",reason=bc._safe_error_local(exc))
            item["subset_sha256"]=subset["sha256"]
            item.setdefault("artifacts",transport.evidence.get(ticker,[]))
            receipt["per_ticker"][ticker]=item
            processed+=1
            receipt["captured_at"]=stamp()
            publish_receipt(root,run_dir,receipt)
            if checkpoint and processed % checkpoint_every==0:
                checkpoint(root,run_dir,receipt)
            if transport.paused:
                receipt["halt_reason"]=transport.paused
                break
        coverage_summary(receipt,members)
        receipt["processing_status"]="COMPLETE"
        receipt["request_budget"]=dict(allowed=max_requests,performed=transport.calls,
                                      retry_limit_per_ticker=bc.MAX_RETRY)
        receipt["captured_at"]=stamp()
        publish_receipt(root,run_dir,receipt)
        if checkpoint:
            checkpoint(root,run_dir,receipt)
        return receipt

@contextmanager
def authenticated_get():
    # This acquisition path has no Telegram import or messaging capability.
    username=os.environ.get("NEOBDM_USERNAME")
    password=os.environ.get("NEOBDM_PASSWORD")
    if not username or not password:
        raise RuntimeError("Existing NeoBDM username/password secrets are required")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            context=browser.new_context(viewport={"width":1400,"height":900})
            page=context.new_page()
            def sign_in():
                page.goto("https://neobdm.tech/accounts/login/?next=/home/",
                          wait_until="domcontentloaded",timeout=60000)
                page.locator('input[name="username"],#id_username,input[type="text"]').first.fill(username)
                page.locator('input[name="password"],#id_password,input[type="password"]').first.fill(password)
                submit=page.locator('button[type="submit"],input[type="submit"]')
                if submit.count():
                    submit.first.click()
                else:
                    page.keyboard.press("Enter")
                page.wait_for_url(lambda url:"login" not in url.lower(),timeout=30000)
                page.goto("https://neobdm.tech/inventory-chart/",wait_until="domcontentloaded",timeout=60000)
            try:
                sign_in()
            except Exception as exc:
                raise RuntimeError("NeoBDM login failed: "+bc._safe_error_local(exc)) from None
            def get(query):
                return context.request.get("https://neobdm.tech/api/inventory?"+query,
                                           timeout=bc.REQUEST_TIMEOUT_MS)
            get.relogin=sign_in
            yield get
        finally:
            browser.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["plan","capture"])
    parser.add_argument("--registry",default=str(HERE/"all_tickers.json"))
    parser.add_argument("--subset",default=str(DEFAULT_SUBSET))
    parser.add_argument("--store",required=True)
    parser.add_argument("--expected-session",required=True)
    parser.add_argument("--max-requests",type=int,default=100)
    parser.add_argument("--tickers",default="")
    args=parser.parse_args()
    registry=load_registry(args.registry)
    subset=subset_contract(args.subset)
    if args.mode=="plan":
        print(json.dumps(dict(registry=registry.metadata(),subset=subset,
              expected_session=args.expected_session,max_requests=args.max_requests,
              minimum_requests_for_registry=sum(t not in bc.NOT_A_TICKER for t in registry.tickers),
              acquisition_status="NOT_ATTEMPTED")))
        return 0
    requested=[t.strip().upper() for t in args.tickers.split(",") if t.strip()] or None
    with authenticated_get() as get:
        receipt=capture(args.store,registry,subset,args.expected_session,get,
                        max_requests=args.max_requests,requested_tickers=requested,live=True)
    print(json.dumps({k:receipt[k] for k in ["acquisition_status","counts","request_budget"]}))
    return 0 if receipt["acquisition_status"]=="COMPLETE" else 1

if __name__=="__main__":
    raise SystemExit(main())
