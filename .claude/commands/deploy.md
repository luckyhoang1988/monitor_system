# /deploy

Quy trình thay đổi code + deploy an toàn cho Monitor System.

> ⚠️ **ĐỌC SKILL NÀY TRƯỚC mỗi lần thay đổi code rồi mới làm.** Nó ghi lại các bẫy đã
> dính thật ngoài production — bỏ qua là lặp lại lỗi (504, deploy code cũ, vỡ JS).

## ⓿ Kỹ năng cốt lõi: SUY LUẬN + DEBUG trước, ÁP DỤNG sau

### 4 nguyên tắc bất di bất dịch (BẮT BUỘC)
1. **KHÔNG suy luận linh tinh, KHÔNG đoán mò.** Mọi kết luận (OID, enum, root cause, mapping) phải có bằng chứng thật. Chưa chứng minh được → nói "chưa chắc" + đi verify, tuyệt đối KHÔNG viết vào code/doc như sự thật. (Vd: enum CISCOSB 11/12 chỉ khẳng định sau khi có anchor gi9=trunk-link=12.)
2. **Test bằng KẾT QUẢ THẬT trước khi thay đổi.** Probe/đo trên thiết bị/DB/shell thật rồi mới sửa code:
   - SNMP OID: walk trên `docker compose exec -T worker python manage.py shell` (thiết bị có SNMP ACL chỉ cho monitorsrv → KHÔNG walk được từ máy local).
   - Query chậm/logic: đo thời gian, `EXPLAIN`, đếm rows trước.
   - Sửa xong → verify LIVE lại (§4), không tin "chắc là xong".
3. **Đọc KỸ tài liệu hãng/OS của từng thiết bị.** Cisco IOS ≠ IOS-XE ≠ Business(CISCOSB/RADLAN); Huawei VRP V5 ≠ YunShan; HyperV/WinRM. MIB/OID/enum khác theo firmware → KHÔNG gộp "Cisco"/"Huawei" làm một. Tài liệu chung mâu thuẫn thiết bị thật đã verify → tin thiết bị thật + ghi lại điểm lệch (vd enum CISCOSB không khớp general(1)/access(2)/trunk(3)).
4. **Đổi code = đọc skill này TRƯỚC; học được gì mới → UPDATE NGAY** skill (§5) + [CLAUDE.md](../../CLAUDE.md) + memory. Đừng để lần sau dò lại.

### Quy trình chuẩn
**Không đoán mò, không "fix" theo cảm tính.** Chứng minh nguyên nhân bằng dữ liệu thật rồi mới sửa.
Quy trình chuẩn (đã dùng để bắt bug 504 phiên đầu):
1. **Quan sát triệu chứng** — đọc log, error thật, tái hiện (vd: 504 = view > nginx 120s).
2. **Đặt giả thuyết** nguyên nhân, có thể có vài cái → xếp theo khả năng.
3. **Đo / chứng minh trên dữ liệu thật** trước khi đụng code: `manage.py shell` đo thời gian
   query, `psql \d` xem index, đếm rows, `EXPLAIN`… (vd: đo query cũ = 242s, xác nhận index tồn tại).
4. **Thử nghiệm fix ở chỗ an toàn** (shell/query/scratchpad) → đo lại, so sánh
   (vd: thử `DISTINCT ON` = 0.038s) → CHỈ khi chứng minh được mới viết vào code.
5. **Sửa tối thiểu** đúng root cause, không vá vòng ngoài che triệu chứng.
6. **Verify fix live** sau deploy (§4) — đo lại trên container prod, không tin "chắc là xong".
7. Bí thì xem rộng (đọc nhiều file/`Explore`) thay vì sửa liều rồi deploy thử.

## 0. Trước khi sửa
1. Đọc mục liên quan trong [CLAUDE.md](../../CLAUDE.md) (OID đã verify, online/offline, realtime SSE…).
2. Đọc skill này tới hết.
3. Xác định thay đổi chạm tầng nào → ảnh hưởng container nào (xem §3).

## 1. Bẫy code đã dính (kiểm tra trước khi viết)
- **DB "latest per group" trên bảng time-series** (VMStats/InterfaceStats/Wifi*): KHÔNG dùng
  `pk__in=Subquery(OuterRef(...))` → Postgres bỏ index, quét lặp → 504 (đã xảy ra: 242s).
  Dùng Postgres `DISTINCT ON`:
  `.filter(device=d).order_by("vm_name","-timestamp").distinct("vm_name")` + fallback
  Python-dedup khi `connection.vendor != "postgresql"` (SQLite dev).
- **Số float Django nhúng vào JS phải `{{ x|unlocalize }}`** (`{% load l10n %}`). Locale `vi`
  đổi `.`→`,` → SyntaxError giết cả `<script>` inline (poller/SSE/nút chết → dashboard treo).
  Test server KHÔNG bắt được, chỉ trình duyệt parse.
- **Xóa metrics**: `_purge_metrics` (trang Cảnh báo→Dung lượng) và `cleanup_old_metrics`
  phải đồng bộ danh sách bảng (gồm VMStats + WifiApStats + WifiClientStats).
- **Topology FDB — hàm "lọc AP" phải trả `[]` khi rỗng, KHÔNG trả input.** `filter_fdb_ap_entries`
  từng `if not ap_entries: return entries` (mọi MAC) khi switch không có MAC-AP nào trong FDB →
  caller tạo **1 AP link giả/cổng** (TopologyLink unique theo `(device, port)` → "last-MAC-wins"),
  đẻ AP ma trên uplink + `port-0`, MAC đổi mỗi vòng discovery. Nổ ở switch không có AP thật qua FDB
  (cisco_business/cisco_ios không expose LLDP, hoặc walk ra partial table). Fix: trả `[]`.
  Quy tắc chung: hàm "filter X" mà caller coi output là "danh sách X" thì nhánh rỗng phải trả `[]`.
  Soi link giả: AP link `is_stale=False` có MAC **không** thuộc snapshot AC = giả (xem CLAUDE.md).
- **Cache-first metrics (`METRICS_WRITE_MODE=cache`)**: khi bỏ ghi raw phải chuyển **cả 3 nguồn đọc** sang cache cùng lúc — alert engine, tính Mbps (prev counter), dashboard/chart raw-tier. Bỏ sót 1 → getter trả None (alert im lặng KHÔNG lỗi rõ) / Mbps=0 / chart trống. Redis lỗi phải **fallback ghi DB** (không mất alert). Sustained/latest getter phải giữ nguyên hysteresis + sentinel mem=0. Xem CLAUDE.md "Cache-first metrics". Bật/tắt qua cờ env, mặc định `db` → rollback nhanh.
- **Không hard-code** IP/password/community. Type hints bắt buộc cho collector/adapter.

## 2. Deploy
```
./deploy.sh            # push origin master + pull/build/restart trên monitorsrv
./deploy.sh --no-push  # CHỈ khi code đã push sẵn
```
⚠️ **`--no-push` vẫn chạy `git pull origin master` trên server.** Nếu commit chưa push lên
origin → server pull được code CŨ và build lại bản cũ (HEAD lệch, fix không lên prod).
→ Mặc định dùng `./deploy.sh` (có push). Chỉ `--no-push` khi chắc chắn đã `git push`.

## 3. Thay đổi nào rebuild container nào
- View/template/dashboard/realtime → rebuild **app**.
- Collector/adapter/OID/tasks → rebuild **worker** (collector chạy trong worker).
- Beat schedule (`config/settings/base.py` CELERYBEAT) → rebuild **beat**.
- `METRICS_WRITE_MODE` / cache-first (writer/engine/aggregation/dashboard/api) → chạm cả web + worker + beat → rebuild **app+worker+beat**. Đổi cờ trong `.env.production` cũng phải recreate 3 container.
- `deploy.sh` build cả app+worker+beat nên thường an toàn; nginx chỉ reload.
- Đổi JS/template → user cần **Empty-Cache-Hard-Reload 1 lần**.

## 4. Verify sau deploy (BẮT BUỘC)
1. So commit: `git rev-parse --short HEAD` (local) **==** trên `monitorsrv` (cùng dir).
2. App healthy: `docker inspect -f '{{.State.Health.Status}}' monitor_system-app-1` = `healthy`.
3. Nếu sửa query/logic → chạy `manage.py shell` trên container đo lại / xác nhận fix live.
4. Báo cáo trung thực: nếu HEAD lệch hoặc chưa healthy → CHƯA xong, sửa rồi verify lại.

## 5. Cập nhật skill này (BẮT BUỘC)
Mỗi khi học được điều mới / có gì thay đổi trong lúc làm — bẫy mới, fix mới, đổi quy trình
deploy, đổi hạ tầng server, lệnh/cách verify mới — **cập nhật ngay file này** (`/deploy`) để lần
sau không phải dò lại. Skill này là nguồn sự thật sống về cách thay đổi + deploy an toàn; giữ nó
đúng hiện trạng. Bẫy lớn thì ghi thêm vào CLAUDE.md + memory để khỏi mất.

## Server
- `monitorsrv` = `10.0.193.234`, user `monitorsys`, dir `/home/monitorsys/monitor_system`.
- SSH qua alias `monitorsrv` (key `~/.ssh/monitorsys_ed25519`), KHÔNG dùng `monitorsys@IP` (publickey denied).
- Compose: app(ASGI/uvicorn) + worker + beat + db(pg16) + redis + nginx. Code build vào image.
- DB psql: `docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB ...'`
  (role không phải `monitorsys`).
