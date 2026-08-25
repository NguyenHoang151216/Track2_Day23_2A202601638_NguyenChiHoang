"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append one timestamped event to the failover JSONL log."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
    print("FAILOVER", json.dumps(record, ensure_ascii=False))
    return record


def state_of(region: str) -> dict:
    """Return the serving state advertised by a region."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    if response.status_code != 200:
        raise RuntimeError(f"region-{region} /v1/state returned HTTP {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"region-{region} returned a non-object state payload")
    return body


def _failure(target: str, backend: str, failed_step: str, reason: str, **kw) -> dict:
    """Build a stable abort result for the runbook without performing a cutover."""
    return {
        "ok": False,
        "target": target,
        "backend": backend,
        "failed_step": failed_step,
        "reason": reason,
        "state": None,
        "cutover": {"ok": False, "performed": False, "active_region": None},
        **kw,
    }


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and cut traffic over to *target* in the required five-step order."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target!r}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unknown snapshot backend: {backend!r}")
    if wait < 0:
        raise ValueError("wait must be non-negative")

    # 1. Verify the target process and capture its pre-restore state. Calling
    # /v1/state also makes the service observe the current pool state, so the
    # later warm -> full transition starts its warm-up timer.
    try:
        state_before = state_of(target)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="1_verify_target", target=target, ok=False, error=reason)
        return _failure(target, backend, "1_verify_target", reason)
    emit(step="1_verify_target", target=target, ok=True, state=state_before)

    # 2. Restore vector state and model weights, then measure actual data loss
    # against the still-readable primary database.
    try:
        restore_meta = snapshot.get(target, backend)
        source_region = restore_meta.get("source_region")
        if source_region not in URL:
            source_region = "a" if target == "b" else "b"
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{source_region}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        restore_result = {**restore_meta, **rpo}
        restore_result.setdefault("rpo_seconds", None)
        restore_result.setdefault("docs_lost", None)
        restore_result.setdefault("embed_model_version", None)
    except (Exception, SystemExit) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="2_restore_snapshot", target=target, backend=backend,
             ok=False, error=reason, rpo_seconds=None, docs_lost=None,
             embed_model_version=None)
        return _failure(
            target, backend, "2_restore_snapshot", reason,
            state_before=state_before, restore=None,
            rpo_seconds=None, docs_lost=None, embed_model_version=None,
        )
    emit(step="2_restore_snapshot", target=target, backend=backend, ok=True,
         **restore_result)

    # 3. Scale the target pool. The first readiness poll makes the running
    # serving process observe this transition and begin its configured warm-up.
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    previous_pool_state = state_before.get("pool_state")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full\n", encoding="utf-8")
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="3_scale_pool", target=target, ok=False, error=reason,
             from_pool_state=previous_pool_state, to_pool_state="full")
        return _failure(
            target, backend, "3_scale_pool", reason,
            state_before=state_before, restore=restore_result,
            rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
            embed_model_version=restore_meta.get("embed_model_version"),
        )
    emit(step="3_scale_pool", target=target, ok=True,
         from_pool_state=previous_pool_state, to_pool_state="full",
         pool_state_file=str(pool_file))

    # 4. Only /readyz=200 proves that compute, weights and vector state are all
    # ready together. Log one summary event rather than one event per poll.
    started = time.monotonic()
    deadline = started + wait
    attempts = 0
    ready_body = None
    last_error = None
    last_status = None
    while True:
        attempts += 1
        remaining = max(0.0, deadline - time.monotonic())
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=min(2.0, max(0.05, remaining)),
            )
            last_status = response.status_code
            try:
                body = response.json()
                ready_body = body if isinstance(body, dict) else {"body": body}
            except Exception:
                ready_body = None
            if response.status_code == 200:
                break
            if ready_body:
                last_error = ", ".join(str(x) for x in ready_body.get("reasons", []))
            if not last_error:
                last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            waited_s = round(time.monotonic() - started, 3)
            reason = last_error or f"target did not become ready within {wait}s"
            emit(step="4_wait_ready", target=target, ok=False, ready=False,
                 waited_s=waited_s, wait_timeout_s=wait, attempts=attempts,
                 status_code=last_status, error=reason)
            return _failure(
                target, backend, "4_wait_ready", reason,
                state_before=state_before, restore=restore_result,
                ready=ready_body, waited_s=waited_s,
                rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
                embed_model_version=restore_meta.get("embed_model_version"),
            )
        time.sleep(min(0.5, remaining))

    waited_s = round(time.monotonic() - started, 3)
    emit(step="4_wait_ready", target=target, ok=True, ready=True,
         waited_s=waited_s, wait_timeout_s=wait, attempts=attempts,
         status_code=last_status, readiness=ready_body)

    # Fetch final state for the runbook. Readiness is already established; if
    # this informational request fails, retain equivalent readiness information.
    try:
        final_state = state_of(target)
    except Exception as exc:
        vectors = (ready_body or {}).get("vectors", {})
        final_state = {
            "region": target,
            "pool_state": "full",
            "weights": True,
            "count": vectors.get("count") if isinstance(vectors, dict) else None,
            "state_read_error": f"{type(exc).__name__}: {exc}",
        }

    # 5. This is deliberately the final write. Every earlier return leaves
    # edge/active_region byte-for-byte unchanged.
    active_file = pathlib.Path("edge/active_region")
    try:
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(target + "\n", encoding="utf-8")
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="5_dns_cutover", target=target, ok=False, error=reason,
             active_region_file=str(active_file))
        return _failure(
            target, backend, "5_dns_cutover", reason,
            state_before=state_before, restore=restore_result, state=final_state,
            ready=ready_body, waited_s=waited_s,
            rpo_seconds=rpo.get("rpo_seconds"), docs_lost=rpo.get("docs_lost"),
            embed_model_version=restore_meta.get("embed_model_version"),
        )
    cutover = {
        "performed": True,
        "active_region": target,
        "active_region_file": str(active_file),
    }
    emit(step="5_dns_cutover", target=target, ok=True, **cutover)

    return {
        "ok": True,
        "target": target,
        "backend": backend,
        "state_before": state_before,
        "restore": restore_result,
        "state": final_state,
        "ready": ready_body,
        "waited_s": waited_s,
        "rpo_seconds": rpo.get("rpo_seconds"),
        "docs_lost": rpo.get("docs_lost"),
        "embed_model_version": restore_meta.get("embed_model_version"),
        "cutover": {"ok": True, **cutover},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
