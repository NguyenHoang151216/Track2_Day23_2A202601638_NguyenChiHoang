"""Automate the seven steps of the "primary region down" runbook.

The runbook is deliberately semi-automatic: an operator must confirm the
failover unless ``--auto`` is used for the drill/CI.  Its JSONL output is also
the authoritative incident timeline used by the postmortem.
"""

import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}

OUTAGE_PROBES = 3
OUTAGE_PROBE_INTERVAL_S = 5.0
PROBE_TIMEOUT_S = 2.0
FAILOVER_WAIT_S = 60.0
GOLDEN_REQUESTS = 10
GOLDEN_TIMEOUT_S = 3.0
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")


def step(n, name, **kw):
    """Append exactly one timestamped runbook step to the JSONL timeline."""
    now = time.time()
    rec = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "step": n,
        "name": name,
        **kw,
    }
    # The timestamp of step 2 is specifically the moment the operator learned
    # about the outage.  Keep it explicit as well as in the generic `ts` field.
    if n == 2 and name == "thong_bao_incident":
        rec.setdefault("operator_ts", now)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Return True automatically in CI, otherwise require an explicit y/yes."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        # No stdin must fail safe; never cut traffic over implicitly.
        return False


def _probe(region: str) -> dict:
    """Probe readiness while distinguishing a 503 from no connection at all."""
    started = time.perf_counter()
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=PROBE_TIMEOUT_S)
        detail = None
        try:
            body = response.json()
            if isinstance(body, dict):
                reasons = body.get("reasons")
                detail = ",".join(map(str, reasons)) if isinstance(reasons, list) else reasons
        except Exception:
            pass
        return {
            "reachable": True,
            "ready": response.status_code == 200,
            "status": response.status_code,
            "reason": detail or f"http_{response.status_code}",
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as exc:
        return {
            "reachable": False,
            "ready": False,
            "status": None,
            "reason": type(exc).__name__,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def _latest_outage(primary: str, fallback: float) -> tuple[float, str]:
    """Read the latest real kill timestamp for the primary from the chaos log."""
    if not CHAOS_LOG.exists():
        return fallback, "runbook_start_inferred"

    latest = None
    try:
        with CHAOS_LOG.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                event = json.loads(line)
                if (event.get("action") == "kill" and event.get("region") == primary
                        and isinstance(event.get("ts"), (int, float))):
                    latest = event
    except (OSError, json.JSONDecodeError):
        return fallback, "runbook_start_inferred"

    if latest is None:
        return fallback, "runbook_start_inferred"
    return float(latest["ts"]), "chaos/chaos-events.jsonl"


def _restored_state(result: dict) -> dict:
    """Extract restored state from compatible failover result shapes."""
    for key in ("restored_state", "state", "final_state", "target_state"):
        value = result.get(key)
        if isinstance(value, dict):
            return value

    # Also accept implementations which return the state fields at top level.
    if any(key in result for key in ("count", "vector_count", "weights", "weights_ok")):
        return result
    return {}


def _state_summary(state: dict) -> tuple[int | None, bool | None]:
    count = state.get("count", state.get("vector_count"))
    vectors = state.get("vectors")
    if count is None and isinstance(vectors, dict):
        count = vectors.get("count")
    try:
        count = None if count is None else int(count)
    except (TypeError, ValueError):
        count = None

    weights = state.get("weights", state.get("weights_ok"))
    if weights is not None:
        weights = bool(weights)
    return count, weights


def _cutover_result(result: dict, target: str) -> tuple[bool, object]:
    raw = result.get("cutover")
    if raw is None:
        raw = result.get("cutover_ok", result.get("dns_cutover"))

    if isinstance(raw, dict):
        if "ok" in raw:
            return bool(raw["ok"]), raw
        active = raw.get("active_region", raw.get("target", raw.get("region")))
        return active == target, raw
    if isinstance(raw, str):
        return raw == target, raw
    if raw is not None:
        return bool(raw), raw
    return False, raw


def _percentile(values: list[float], percentile: float) -> float | None:
    """Nearest-rank percentile, suitable for the ten golden-signal samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _golden_signals(target: str) -> dict:
    """Send ten real inference requests directly to the target region."""
    latencies = []
    samples = []
    errors = 0

    for request_no in range(1, GOLDEN_REQUESTS + 1):
        started = time.perf_counter()
        status = None
        served_by = None
        error = None
        try:
            response = httpx.get(
                f"{URL[target]}/v1/infer",
                params={"q": f"dr-golden-signal-{request_no}"},
                timeout=GOLDEN_TIMEOUT_S,
            )
            status = response.status_code
            body = {}
            try:
                parsed = response.json()
                body = parsed if isinstance(parsed, dict) else {}
            except Exception:
                pass
            served_by = body.get("region")
            if status != 200:
                error = body.get("error") or f"http_{status}"
            elif served_by not in (None, target):
                error = f"served_by_{served_by}"
        except Exception as exc:
            error = type(exc).__name__

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        latencies.append(latency_ms)
        if error is not None:
            errors += 1
        samples.append({
            "request": request_no,
            "status": status,
            "served_by": served_by,
            "latency_ms": latency_ms,
            "ok": error is None,
            "error": error,
        })

    successful = GOLDEN_REQUESTS - errors
    return {
        "requests": GOLDEN_REQUESTS,
        "successful": successful,
        "errors": errors,
        "error_rate": round(errors / GOLDEN_REQUESTS, 4),
        "error_rate_pct": round(errors * 100 / GOLDEN_REQUESTS, 1),
        "p95_latency_ms": round(_percentile(latencies, 0.95), 1),
        "samples": samples,
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Run the seven checklist steps in order."""
    if primary not in URL or target not in URL:
        raise ValueError("primary/target must be 'a' or 'b'")
    if primary == target:
        raise ValueError("primary and target must be different regions")
    if backend not in {"fs", "minio"}:
        raise ValueError("backend must be 'fs' or 'minio'")

    run_started = time.time()

    # 1. Three consecutive readiness failures are required. A target 503 still
    # proves the process is reachable and can be restored/scaled by failover.
    checks = []
    consecutive_primary_fails = 0
    for attempt in range(1, OUTAGE_PROBES + 1):
        primary_probe = _probe(primary)
        target_probe = _probe(target)
        if primary_probe["ready"]:
            consecutive_primary_fails = 0
        else:
            consecutive_primary_fails += 1
        checks.append({
            "attempt": attempt,
            "primary": primary_probe,
            "target": target_probe,
            "consecutive_primary_fails": consecutive_primary_fails,
        })
        if attempt < OUTAGE_PROBES:
            time.sleep(OUTAGE_PROBE_INTERVAL_S)

    outage_confirmed = consecutive_primary_fails >= OUTAGE_PROBES
    step(1, "xac_nhan_outage", primary=primary, target=target,
         attempts=OUTAGE_PROBES, consecutive_primary_fails=consecutive_primary_fails,
         outage_confirmed=outage_confirmed,
         target_reachable=checks[-1]["target"]["reachable"], checks=checks)
    if not outage_confirmed:
        return {
            "ok": False,
            "aborted": True,
            "reason": f"region-{primary} did not fail {OUTAGE_PROBES} consecutive probes",
            "primary": primary,
            "target": target,
        }

    # 2. The operator timestamp must be after the real chaos event timestamp.
    outage_ts, outage_source = _latest_outage(primary, run_started)
    incident = step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        t_outage=outage_ts,
        outage_source=outage_source,
        notification_delay_s=round(max(0.0, time.time() - outage_ts), 2),
    )

    if not confirm(auto, f"Region-{primary} outage confirmed. Fail over to region-{target}?"):
        return {
            "ok": False,
            "aborted": True,
            "reason": "operator did not confirm failover",
            "primary": primary,
            "target": target,
            "outage_ts": outage_ts,
            "operator_ts": incident["operator_ts"],
        }

    # 3. This is the one and only failover() call. Never retry implicitly.
    try:
        failover_result = fo.failover(target, backend, FAILOVER_WAIT_S)
        if not isinstance(failover_result, dict):
            failover_result = {
                "ok": False,
                "error": f"failover returned {type(failover_result).__name__}, expected dict",
            }
    except Exception as exc:
        failover_result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    step(3, "scale_gpu_pool", primary=primary, target=target, backend=backend,
         failover_ok=bool(failover_result.get("ok")),
         ready=bool(failover_result.get("ready")),
         error=failover_result.get("error"))

    # 4. Read state only from the returned dict: do not call failover or an API again.
    restored_state = _restored_state(failover_result)
    vector_count, weights = _state_summary(restored_state)
    state_ok = vector_count is not None and vector_count > 0 and weights is True
    step(4, "verify_state_replica", target=target, state=restored_state,
         vector_count=vector_count, weights=weights, ok=state_ok,
         rpo_seconds=failover_result.get("rpo_seconds"),
         docs_lost=failover_result.get("docs_lost"),
         embed_model_version=failover_result.get("embed_model_version"))

    # 5. Likewise, report the cutover result without performing another cutover.
    cutover_ok, cutover_detail = _cutover_result(failover_result, target)
    step(5, "dns_cutover", target=target, ok=cutover_ok, cutover=cutover_detail)

    # 6. Verify the target with real traffic and aggregate its golden signals.
    golden = _golden_signals(target)
    golden_ok = golden["errors"] == 0
    step(6, "verify_golden_signals", target=target, ok=golden_ok, **golden)

    # 7. Finish with reproducible timing and the exact measurement command.
    finished = time.time()
    overall_ok = (bool(failover_result.get("ok")) and state_ok
                  and cutover_ok and golden_ok)
    rto_command = ("python3 tools/measure_rto.py "
                   "--loadgen reports/drill-2-withdr.jsonl --target-rto 300")
    elapsed_s = round(max(0.0, finished - outage_ts), 2)
    runbook_elapsed_s = round(finished - run_started, 2)
    step(7, "post_incident", ok=overall_ok, elapsed_s=elapsed_s,
         runbook_elapsed_s=runbook_elapsed_s, rto_command=rto_command,
         next_action="measure RTO/RPO from logs and complete the postmortem")

    return {
        "ok": overall_ok,
        "primary": primary,
        "target": target,
        "backend": backend,
        "outage_ts": outage_ts,
        "operator_ts": incident["operator_ts"],
        "notification_delay_s": round(incident["operator_ts"] - outage_ts, 2),
        "failover": failover_result,
        "state": {"vector_count": vector_count, "weights": weights, "ok": state_ok},
        "cutover_ok": cutover_ok,
        "golden_signals": golden,
        "elapsed_s": elapsed_s,
        "rto_command": rto_command,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--primary", default="a", choices=["a", "b"])
    parser.add_argument("--target", default="b", choices=["a", "b"])
    parser.add_argument("--backend", default="fs", choices=["fs", "minio"])
    parser.add_argument("--auto", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.primary, args.target, args.backend, args.auto),
                     indent=2, ensure_ascii=False))
