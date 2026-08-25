# Postmortem — DR Drill Lab 23

- **Ngày drill:** 2026-08-25
- **Phạm vi:** Region A `netblock --mock`, failover bare mode sang Region B
**Tính chất:** blameless; lỗi được phân tích như đặc tính của hệ thống và quy
trình, không quy trách nhiệm cho cá nhân.

## 1. Tóm tắt tác động

Region A ngừng phản hồi trong khi edge vẫn cache A. Có 13 request lỗi trước khi
traffic phục hồi ở B. Request thành công đầu tiên từ B xuất hiện sau 27.6s. Bản
restore thiếu 2 document mới nhất, tương ứng RPO 4.08s. Drill hợp lệ và không có
warning (`reports/measure-drill-2.json:2`, `reports/measure-drill-2.json:4`).

## 2. Timeline

| UTC | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T09:50:22.877Z | Outage bắt đầu: SIGSTOP Region A | `chaos/chaos-events.jsonl:5` |
| 2026-08-25T09:50:23.123Z | User đầu tiên nhận lỗi từ edge | `reports/drill-2-withdr.jsonl:19` |
| 2026-08-25T09:50:39.374Z | Health checker đổi A sang `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| 2026-08-25T09:50:40.640Z | Operator xác nhận incident/cutover | `reports/runbook-run.jsonl:2` |
| 2026-08-25T09:50:40.823Z | Snapshot restore xong; RPO 4.08s / 2 docs | `reports/failover-events.jsonl:2` |
| 2026-08-25T09:50:47.455Z | Region B ready sau warm-up | `reports/failover-events.jsonl:4` |
| 2026-08-25T09:50:47.578Z | DNS/LB cutover sang B | `reports/failover-events.jsonl:5` |
| 2026-08-25T09:50:50.508Z | Resolved: request đầu tiên thành công từ B | `reports/drill-2-withdr.jsonl:32` |

## 3. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu: 300s; đo được: **27.6s**; gap/headroom: **272.4s** dưới mục
  tiêu (`reports/measure-drill-2.json:20`, `reports/measure-drill-2.json:21`).
- RPO mục tiêu: 300s; đo được: **4.08s / 2 docs**; gap/headroom:
  **295.92s** dưới mục tiêu (`reports/measure-drill-2.json:23`,
  `reports/measure-drill-2.json:24`).
- Bước tốn nhiều thời gian nhất là health-check detection: quan sát 16.5s, trong
  đó floor cấu hình là 15.0s (`reports/measure-drill-2.json:11`,
  `reports/measure-drill-2.json:18`). Đây là khoảng 59.8% RTO quan sát; riêng
  floor cấu hình chiếm 54.3%.
- GPU warm-up đứng thứ hai với `waited_s=6.622s`
  (`reports/failover-events.jsonl:4`). DNS/nhịp request sau cutover cộng thêm
  2.9s trước khi user thấy request thành công (`reports/failover-events.jsonl:5`,
  `reports/drill-2-withdr.jsonl:32`).

## 4. Root cause — 5 Whys

1. **Vì sao user nhận lỗi?** Edge vẫn resolve tới A trong khi A đã ngừng trả lời.
2. **Vì sao không chuyển ngay sang B?** Thiết kế yêu cầu đủ 3 readiness failure
   liên tiếp và xác nhận bán tự động để tránh flapping.
3. **Vì sao B chưa thể nhận traffic ngay khi outage xảy ra?** B khởi đầu ở trạng
   thái warm, không có vector DB và model weights cục bộ.
4. **Vì sao recovery cần thêm hơn 6 giây sau restore?** Pool chỉ bắt đầu GPU
   warm-up khi chuyển `warm → full`; DNS chỉ được đổi sau khi `/readyz` trả 200.
5. **Vì sao state vẫn có data loss?** Replication là snapshot định kỳ 30 giây,
   không phải synchronous replication; document sau snapshot gần nhất không có
   trong bản restore.

Nguyên nhân hệ thống là sự kết hợp của detection theo polling, standby phản ứng
(reactive) và replication bất đồng bộ. Chaos chỉ kích hoạt điều kiện để đo các
trade-off này, không phải root cause.

## 5. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Kết nối health event với alert + one-click runbook, giữ circuit breaker và quyền IC | SRE lead | 2026-09-01 | Loại phần lớn pha xác nhận/verify 1.5s, không chuyển thành full-auto |
| 2 | Duy trì B ở warm standby với weights/vector snapshot được pre-stage và diễn tập readiness hằng ngày | AI Platform | 2026-09-08 | Giảm khoảng 6.6s GPU warm-up/restore; đổi lại có chi phí compute và kiểm tra consistency |
| 3 | Giảm snapshot cadence từ 30s xuống 10s và theo dõi I/O | Data Platform | 2026-09-08 | Giảm RPO worst-case lý thuyết khoảng 20s; cần đánh giá tải snapshot |

## 6. Câu hỏi bắt buộc

1. **`interval × threshold` là bao nhiêu và chiếm bao nhiêu RTO?**

   `5s × 3 = 15s`. Floor này chiếm 54.3% RTO 27.6s; detection quan sát thực tế
   là 16.5s, chiếm 59.8%. Với RTO 5 phút, trần lý thuyết nếu dành toàn bộ ngân
   sách cho detection là interval 100s. Nếu giữ 12.6s cho các pha còn lại như
   lần đo này, interval phải không quá khoảng 95.8s; cấu hình 5s tạo headroom lớn.

2. **Nếu hạ interval xuống 1s thì sao?**

   Floor giảm từ 15s xuống 3s, tức RTO lý thuyết giảm khoảng 12s nếu các pha
   khác giữ nguyên. Đổi lại số probe tăng 5 lần, tăng tải/alert noise và nguy cơ
   đánh dấu outage do lỗi thoáng qua; threshold 3 và circuit breaker vẫn phải giữ.

3. **Nếu outage kéo dài 6 giờ và A mất dữ liệu vĩnh viễn, `docs_lost` có nghĩa gì
   với khách hàng?**

   Hai document trong lần drill là dữ liệu đã có ở primary nhưng chưa có trong
   snapshot được restore. Các truy vấn phụ thuộc vào chúng có thể thiếu hoặc trả
   kết quả cũ; nếu A mất vĩnh viễn và không có WAL/event log khác, chúng không thể
   phục hồi. Con số 2 chỉ mô tả đúng thời điểm drill, không bảo đảm rằng outage 6
   giờ cũng chỉ mất 2 document; cần diễn giải cùng cadence và tốc độ ingest.
