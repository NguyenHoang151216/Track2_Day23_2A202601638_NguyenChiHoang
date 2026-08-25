# RTO/RPO Evidence — Lab 23

Kết quả đo tổng hợp có `valid: true`, không có warning và
verdict `PASS` tại `reports/measure-drill-2.json:2`,
`reports/measure-drill-2.json:4` và `reports/measure-drill-2.json:22`.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-08-25T09:36:53Z | Sự kiện `kill`, Region A, `netblock` | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.1s | Dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Tổng request lỗi sau outage | 16 | Đếm các dòng `ok:false` trong cửa sổ drill | `reports/measure-drill-1.json:28` |
| Request thành công sau đó | Không có | Không tìm thấy `ok:true` sau request lỗi đầu tiên | `reports/measure-drill-1.json:16` |
| RTO | `NO_RECOVERY` | Không có timestamp phục hồi trong cửa sổ loadgen | `reports/measure-drill-1.json:25` |

Baseline chứng minh Region A không tự phục hồi và hệ thống chưa có cơ chế đổi
traffic sang B.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | Sự kiện `kill`, Region A, `netblock` | `chaos/chaos-events.jsonl:5` |
| User thấy lỗi đầu tiên | 0.2s | Dòng `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:19` |
| Health checker phát hiện A unhealthy | 16.5s | Transition sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore hoàn tất | 17.9s | Bước `2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | 24.6s | `/readyz` trả 200 sau warm-up | `reports/failover-events.jsonl:4` |
| DNS/LB cutover | 24.7s | Bước `5_dns_cutover`, active region là B | `reports/failover-events.jsonl:5` |
| Request thành công đầu tiên từ B | **27.6s** | Dòng `ok:true`, `served_by:"b"` đầu tiên sau lỗi | `reports/drill-2-withdr.jsonl:32` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---:|---:|---|---|
| RTO — Inference API | **27.6s** | 300s | PASS | `reports/measure-drill-2.json:20` |
| RPO — Vector DB | **4.08s / 2 docs** | 300s | PASS | `reports/failover-events.jsonl:2` |

Model version được khôi phục cùng snapshot là
`embed-model=vi-e5-base@v3`, cũng được ghi tại
`reports/failover-events.jsonl:2`; do đó vector index và embedding model không
bị lệch phiên bản.

## 3. Phân rã RTO

Cấu hình health check là interval 5.0s, threshold 3, nên detection floor cấu
hình là **15.0s** (`5.0 × 3`). Giá trị và cấu hình thực tế nằm tại
`reports/health-events.jsonl:2` và `reports/measure-drill-2.json:16`.

Để bốn pha cộng đúng bằng RTO trải nghiệm người dùng, ranh giới pha được lấy từ
các timestamp thật như sau:

| Thành phần | Giây | Ranh giới đo | Evidence | Cách giảm |
|---|---:|---|---|---|
| Health-check detection | 16.5s | t_outage → A `UNHEALTHY`; floor cấu hình là 15.0s | `chaos/chaos-events.jsonl:5`, `reports/health-events.jsonl:2` | Giảm interval nhưng giữ threshold để vẫn chống flapping |
| Xác nhận + verify + restore snapshot | 1.5s | t_detect → `3_scale_pool`; riêng snapshot có RPO 4.08s / 2 docs | `reports/health-events.jsonl:2`, `reports/failover-events.jsonl:3` | Alert/one-click sớm hơn; giữ snapshot gần target |
| GPU warm-up + readiness/cutover | 6.7s | `3_scale_pool` → `5_dns_cutover`; `waited_s` thực tế là 6.622s | `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:5` | Duy trì warm standby hoặc pre-warm có kiểm soát |
| DNS/LB cache + nhịp request kế tiếp | 2.9s | t_cutover → request thành công đầu tiên từ B | `reports/failover-events.jsonl:5`, `reports/drill-2-withdr.jsonl:32` | Giảm TTL và kiểm tra ảnh hưởng lên edge |
| **Tổng** | **27.6s** | 16.5 + 1.5 + 6.7 + 2.9 | `reports/measure-drill-2.json:20` | Mục tiêu 300s còn 272.4s headroom |

## 4. Kết luận

- Drill hợp lệ, không có warning và không có double outage.
- Region phục hồi là B, khác với Region A bị kill.
- RTO và RPO đều được đo từ timestamp/log thật, không lấy từ ước lượng hay số mẫu
  trong tài liệu hướng dẫn.
- Runbook đã gửi 10 request trực tiếp tới B: 0 lỗi, p95 148.9ms tại
  `reports/runbook-run.jsonl:6`.
