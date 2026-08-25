# Runbook — Region chính down

**Phạm vi:** bare mode trên localhost; primary A, target B, snapshot backend
filesystem. **Incident Commander (IC)** là người duy nhất cho phép cutover hoặc
rollback. Không chạy hai lệnh failover song song.

## Checklist vận hành

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage, đồng thời bảo đảm B còn sống | `for i in 1 2 3; do date -u; curl --max-time 2 -sS http://127.0.0.1:8001/readyz; curl --max-time 2 -sf http://127.0.0.1:8002/healthz; sleep 5; done` | A fail 3 lần liên tiếp; B vẫn trả HTTP 200 ở `/healthz`. Nếu B cũng không trả lời thì dừng, không tạo double outage. | On-call SRE |
| 2 | Mở incident, bắt đầu đồng hồ và yêu cầu xác nhận | `python3 dr/runbook.py --primary a --target b --backend fs` | Có step `thong_bao_incident`; terminal hỏi `y/N`. Chỉ nhập `y` sau khi IC phê duyệt. Lệnh này chỉ được chạy **một lần**. | On-call thực thi; IC phê duyệt |
| 3 | Restore state ở B | `tail -n 5 reports/failover-events.jsonl` | Event `2_restore_snapshot` có `ok:true`, `rpo_seconds`, `docs_lost` và `embed_model_version`. Nếu thiếu snapshot, automation phải abort trước cutover. | Data/ML platform |
| 4 | Scale GPU pool và chờ readiness | `curl -sf http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, `pool_state:"full"`, vector count > 0 và không có reason lỗi. | AI serving on-call |
| 5 | Xác minh DNS/LB cutover sau TTL | `sleep 5; curl -sf http://127.0.0.1:8080/edge/state` | `active_region:"b"`; failover log có đủ thứ tự `1_verify_target` đến `5_dns_cutover`. | Network/SRE |
| 6 | Verify golden signals | `python3 -c "import json; e=[json.loads(x) for x in open('reports/runbook-run.jsonl')]; print([x for x in e if x.get('name')=='verify_golden_signals'][-1])"` | 10 request, 0 lỗi, error rate < 1% và p95 < 500ms. Nếu vi phạm, giữ incident mở và đánh giá rollback. | Service owner |
| 7 | Đo RTO/RPO và mở postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, `warnings:[]`, `rto_verdict:"PASS"`, recovery do B phục vụ, RPO seconds/docs đều khác null. | Incident analyst |

## Guardrails và điều kiện abort

- Không dùng `--i-really-want-both`; nếu B không alive thì dừng ở bước 1.
- Không ghi tay `edge/active_region`. Cutover chỉ do bước 5 của
  `dr/failover.py` thực hiện sau khi B ready.
- Nếu restore, model version hoặc readiness thất bại, không retry mù quáng và
  không chạy lại runbook; giữ traffic chưa cutover, báo IC và điều tra snapshot.
- Ghi lại mọi quyết định trong incident; không xóa log trong khi incident mở.

## Rollback về Region A

Rollback chỉ được cân nhắc khi **cả hai** điều kiện sau đúng:

1. B vi phạm golden signals trong 3 cửa sổ liên tiếp (error rate ≥ 1%, p95 ≥
   500ms), có lỗi integrity/model version, hoặc không thể duy trì service.
2. A đã được restore, đồng bộ state, `/readyz` trả 200 ba lần liên tiếp và không
   còn nguyên nhân outage.

**Thẩm quyền:** IC quyết định; on-call SRE thực thi. Áp dụng circuit breaker tối
thiểu 15 phút từ lần cutover trước để tránh hai region flap qua lại.

```bash
python3 dr/failover.py --target a --backend fs --wait 60
sleep 5
curl -sf http://127.0.0.1:8080/edge/state
curl -sf http://127.0.0.1:8080/v1/infer
```

Rollback hoàn tất khi edge báo `active_region:"a"`, inference trả câu trả lời
`[a]`, và 10 request xác minh đáp ứng cùng ngưỡng golden signals. Nếu A không
ready trong 60 giây, lệnh phải abort mà không đổi DNS; tiếp tục phục vụ trên B và
escalate cho IC.
