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
- **Bật 1 cờ env trên prod: đọc MỌI nơi dùng cờ đó (`grep -rn TÊN_CỜ`) trước, không chỉ nơi mình đang nghĩ tới.** `METRICS_AUTO_CLEANUP` gate cả `cleanup_old_metrics` (90 ngày) lẫn `rollup_hourly_metrics`→`cleanup_rolled_up_raw_data` (raw >48h); bật cờ mà chỉ mô tả cơ chế đầu → suýt xoá ~635k InterfaceStats raw ngoài dự kiến. Đo số dòng sẽ bị xoá + báo user đủ cả 2 tác động rồi mới bật.
- **Dashboard tổng hợp trạng thái từ snapshot có field nullable**: field None = "không đọc được", KHÔNG phải "khoẻ". Không để 1 metric OK làm tổng thành OK khi nhóm từng lỗi không đọc được — dùng giá trị đọc được gần nhất (kèm thời điểm) như alert engine (`apps/dashboard/hardware.py` `latest_hardware` carry-forward 24h). Field luôn None (iLO4: BIOS/Network) không bị coi là thiếu.
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
- **WinRM `run_ps` (HyperV collector) giới hạn dòng lệnh ~8191 ký tự** (base64-encode UTF-16LE
  script rồi truyền qua cmd.exe). `PS_SCRIPT` trong `apps/collectors/hyperv.py` thêm counter/logic mới
  mà viết đầy đủ comment + tên biến dài (vd `Avg-List`, match theo `$cs.Path -like`) dễ vượt giới hạn
  → lỗi **"The command line is too long"** (exit 1), mất luôn cả phần VM/CPU/mem cũ trong cùng poll.
  Đã dính thật 2026-07-07 khi thêm 7 host-perf counter. Fix: nén script (bỏ comment, tên biến 1-2 ký
  tự, dùng positional index `$c[0..7]` thay vì string-match `Path` — đã verify `Get-Counter` giữ
  nguyên thứ tự request trên 2 host thật). Trước khi deploy thay đổi `PS_SCRIPT`, đo lại:
  `python -c "from apps.collectors.hyperv import PS_SCRIPT; import base64; print(len(base64.b64encode(PS_SCRIPT.encode('utf-16-le'))))"`
  — phải < ~8000 (có margin). Bản đầy đủ dễ đọc (comment, tên biến rõ nghĩa) lưu ở
  `scratchpad/plan_hyperv.md` để tham khảo logic, KHÔNG paste thẳng vào `PS_SCRIPT`.
  ⚠️ **Nếu vẫn vượt giới hạn dù đã nén hết mức** (dính thật 2026-07-07 khi thêm per-volume —
  cardinality động N volume/host buộc match theo `Path`/`InstanceName` tốn ký tự, không nén nổi
  bằng positional index): tách thành **2 script riêng, gọi `_run_ps()` 2 lần** (2 phiên WinRM/NTLM
  handshake) thay vì cố nhét vào 1 script — chấp nhận thêm ~1 handshake/host, cô lập lỗi ở Python
  (`try/except` quanh lệnh gọi script thứ 2) để script 1 không bị ảnh hưởng nếu script 2 lỗi. Đo lại
  timing tổng sau khi tách (đã tăng ~32.8s→~52.5s cho 2 host khi thêm per-volume, vẫn dưới ngưỡng
  cảnh báo 50%×`POLL_HYPERV_INTERVAL_SECS` nhưng margin mỏng đi nhiều — theo dõi log cảnh báo sau deploy).
  Đo tương tự cho `PS_SCRIPT_VOLUME`:
  `python -c "from apps.collectors.hyperv import PS_SCRIPT_VOLUME; import base64; print(len(base64.b64encode(PS_SCRIPT_VOLUME.encode('utf-16-le'))))"`.
  Khi thêm 4 counter `LogicalDisk` nữa (2026-07-07, đợt 3): script vượt ngay 8764/8191 → nén bằng 3 cách
  (thứ tự ưu tiên, đã áp dụng thật): (1) **suy ra counter thay vì query** khi có công thức chuẩn Windows
  đã tài liệu hoá — `Disk Transfers/sec = Disk Reads/sec + Disk Writes/sec`, tính cộng trong PS thay vì
  thêm 1 Get-Counter path mới (bớt hẳn 1 path + 1 regex branch); (2) biến prefix `$dp='\LogicalDisk(*)\'`
  rồi nối chuỗi `$dp+'...'` thay vì lặp lại `\LogicalDisk(*)\` nguyên văn mỗi path (bỏ luôn dấu ngoặc
  bọc ngoài `($dp+'x')`→`$dp+'x'` trong `@(...)`, dấu phẩy vẫn tách đúng phần tử); (3) regex match rút
  về substring tối thiểu vẫn unique (`'disk reads/sec'`→`'reads/sec'`, `'avg\. disk queue length'`→
  `'avg\. disk queue'` để còn phân biệt với `'current disk queue'` mới thêm) — PHẢI soát lại từng cặp
  path không cho substring rút gọn của path này vô tình khớp path khác (đã soát thủ công toàn bộ 11
  path trong `PS_SCRIPT_VOLUME`, xem comment tại khai báo). Field JSON mới (`current_queue_length`,
  `transfers_per_sec`, `split_io_per_sec`, `idle_time_percent`) cũng rút gọn thành `cql/tps/sio/idt`
  trong PS (cùng kiểu rtm/wtm/dql/aio ở `PS_SCRIPT` host) — map lại tên đầy đủ ở
  `HyperVCollector._normalize_volumes()` (Python, không tốn base64). Kết quả cuối: 7880/8191 (margin
  ~3.8%, tương đương margin ~3.5% của `PS_SCRIPT` host đã chạy ổn định production).
- **`CELERY_BEAT_SCHEDULE[...]["options"]` phải có key `expire_seconds`, KHÔNG chỉ `expires`.**
  django-celery-beat (`ModelEntry._unpack_options`) chỉ đọc `expire_seconds` từ `options` — key
  `expires` (celery gốc) bị nó bỏ qua qua `**kwargs`. `DatabaseScheduler.setup_schedule()` gọi
  `update_from_dict(beat_schedule)` **mỗi lần `beat` process khởi động** (không chỉ lần đầu) →
  nếu thiếu `expire_seconds`, mỗi lần `beat` restart sẽ RESET `PeriodicTask.expire_seconds` về
  `None` ngay sau khi entrypoint/`sync_beat_expires` vừa set đúng — verify bằng cách đọc source
  + query DB prod thấy `expire_seconds=NULL` dù log entrypoint in "None -> 120" vài phút trước
  (dính thật 2026-07-07, xem CLAUDE.md "Celery Beat — expire_seconds bị reset"). Thêm entry mới
  có `options.expires` → PHẢI thêm luôn `options.expire_seconds` cùng giá trị.
- **`poll_all_hyperv` (và mọi task chạy inline nhiều thiết bị trong 1 lời gọi, không qua
  `poll_device`) phải có `soft_time_limit`/`time_limit` riêng ở tầng `@shared_task`.** Không có
  thì 1 thiết bị treo (WinRM/SNMP/SSH) có thể chiếm toàn bộ task tới hàng trăm giây, y hệt cơ chế
  "poll queue snowball" — `poll_device` đã có từ commit `fe1dac1`, `poll_all_hyperv` thiếu tới
  2026-07-07 mới fix (100s/110s, bắt `SoftTimeLimitExceeded` để dừng batch sạch).
  ⚠️ **Con số soft/hard này PHẢI tính lại mỗi khi fleet HyperV đổi quy mô hoặc có host đang bệnh —
  không phải hằng số cố định.** Dính thật 2026-09-28: 100s/110s (tính cho 2 host healthy) không đủ
  khi thêm host thứ 3 + 1 host đang có sự cố phần cứng (RAID) khiến WinRM tự treo tới hết timeout
  socket riêng (60-70s) BẤT KỂ soft_time_limit đã bắn — `SoftTimeLimitExceeded` là Python exception,
  chỉ raise được khi interpreter quay lại bytecode; kẹt trong 1 call blocking lâu hơn khoảng
  (hard−soft) còn lại → Celery SIGKILL thẳng tiến trình, mất trắng KHÔNG chỉ host xấu mà CẢ host
  đang khoẻ trong cùng vòng → offline giả cho host không liên quan. Fix: nâng hẳn soft/hard lên
  250s/270s + `POLL_HYPERV_INTERVAL_SECS` 120s→300s (xem CLAUDE.md "HyperV Host Performance
  Counters" ⚠️ Batch, memory `poll-queue-snowball-slow-device.md`). Quy tắc: thêm host HyperV mới
  → đo timing thật trước, tính theo host XẤU NHẤT trong fleet (không phải trung bình), rồi mới
  quyết định giữ nguyên hay tách kiến trúc (mỗi host 1 task riêng qua `poll_device`).
- **Vendor/enum choice thêm vào UI (`Device.VENDORS`...) mà KHÔNG có nhánh detect + OID đã verify
  trên thiết bị thật đứng sau nó → silent-fail âm thầm, không phải lỗi ồn ào.** Dính thật 2026-07-11
  khi audit CPU/RAM: `VENDORS` có `("hp", "HP/Aruba")` nhưng code chỉ implement HP-Comware (H3C
  rebrand, `display cpu-usage` VRP-style) — KHÔNG phải ArubaOS thật (CLI/MIB hoàn toàn khác). Vì
  `detect_os_family()` tự dò qua sysObjectID/sysDescr chứ KHÔNG đọc field `vendor` (trừ Synology),
  nếu ai chọn "HP/Aruba" cho 1 switch Aruba thật, auto-detect không khớp nhánh nào → rơi về mặc định
  `cisco_ios` → cpu/mem đọc lặng lẽ **0/0**, không log warning (khác nhánh Huawei có log rõ khi rỗng).
  Fix: xoá thẳng choice + nhánh detect H3C/Comware + Netmiko driver/commands (không có thiết bị thật
  nào dùng, verify qua `Device.objects.filter(vendor="hp").count()` trên DB thật = 0) thay vì sửa
  nhãn — theo nguyên tắc "thiết bị chưa có thật thì bỏ, có thiết bị thật rồi code sau" chứ không giữ
  code chạy được nhưng không ai verify. **Quy tắc chung**: mỗi lựa chọn trong `VENDORS`/`DEVICE_TYPES`
  phải có 1 trong 2: (a) nhánh detect + OID/CLI đã verify trên thiết bị thật, hoặc (b) không tồn tại
  trong UI — không giữ lựa chọn "trông như hoạt động" mà chưa ai chạy thử.
- **Gỡ 1 vendor chưa verify KHÔNG dừng ở vendor/collector — phải lần theo mọi feature ăn theo
  dữ liệu riêng của vendor đó, nếu không sẽ tạo ra 1 "silent-fail trap" MỚI y hệt bẫy vừa xoá.**
  Dính thật 2026-07-11 (cùng đợt gỡ HP/Aruba ở trên, sau đó gỡ tiếp MikroTik/Fortinet): Fortinet là
  vendor DUY NHẤT từng ghi `extra["session_count"]` (SNMP OID `fgSysSesCount`) — nhưng field này lại
  được đặt tên + expose thành 1 **alert metric độc lập** `fw_session_count` trong
  `METRIC_CHOICES`/`AlertRule.metric_label` (UI dropdown chọn được), cộng thêm 1 chuỗi hiển thị
  (`session_count` trong `dashboard/views.py` → `firewall_detail.html` thẻ "Sessions" + Chart.js
  dataset, `_attach_session_count()` trong `metrics/api.py`, write-side `sc` key trong
  `writer.py::_device_scalar_sample`). Nếu chỉ xoá adapter/collector Fortinet mà quên các chỗ này:
  `fw_session_count` vẫn còn trong dropdown AlertRule nhưng **vĩnh viễn không có dữ liệu** (không
  hãng nào khác ghi `extra["session_count"]`) → rule tạo ra sẽ không bao giờ fire, không lỗi, không
  cảnh báo gì — chính xác kiểu bug mà bẫy "vendor/enum choice không có nhánh backing" ở trên mô tả,
  chỉ khác là lần này nó nằm ở tầng feature/metric chứ không phải tầng vendor. Quy trình đúng: sau
  khi quyết định xoá 1 vendor, `grep -ri` toàn repo theo os_family key (vd `fortinet_fortios`) VÀ
  theo tên field đặc thù nó tạo ra trong `extra`/JSON (vd `session_count`) — không chỉ theo tên
  hãng — để bắt hết các tầng: model choices → collector/adapter → OID profile → alert engine
  (metric/getter/format) → dashboard view/template/JS → metrics API → seed data (management
  command). ⚠️ **Seed command (`seed_alert_rules.py`) không tự dọn DB** — nó chỉ create/update
  theo tên rule, không xoá rule đã bị bỏ khỏi `DEFAULT_RULES`; nếu prod từng chạy seed trước khi
  xoá, row `AlertRule` cũ (vd "Firewall Sessions High") vẫn còn trong DB thật và cần xoá tay qua
  `/alerts/rules/` — không phải lỗi code, chỉ là seed script không có nhánh "reconcile/delete".
- **Field "percent" sẵn có trong MIB chuẩn (vd `ssCpuIdle` UCD-SNMP-MIB) không chắc đúng chuẩn
  trên mọi firmware — phải verify tổng User+System+Idle ≈100 trên thiết bị thật trước khi tin.**
  Dính thật 2026-07-11: Synology DSM trả `ssCpuUser(.9)+ssCpuSystem(.10)+ssCpuIdle(.11)=48`
  (phải ≈100) → công thức chuẩn `100-idle` báo CPU 53-54% giả trong khi Resource Monitor DSM
  thật ~1-4% (phát hiện qua ảnh chụp UI thật của user, không phải chủ động test). Fix: chuyển
  sang **RAW counter** (`ssCpuRawUser/Nice/System/Idle` — jiffies cộng dồn từ boot, KHÔNG phải
  %) + **delta giữa 2 lần poll liên tiếp** (`cpu%=100×Δbusy/Δtotal`, chuẩn Cacti/Zabbix/Munin).
  Cần baseline (poll trước) → thêm state Redis riêng `apps/collectors/cpu_state.py` (DB/1, TTL
  600s, **độc lập `METRICS_WRITE_MODE`** vì đây là scratch tính delta chứ không phải metrics).
  Counter giảm (reboot) → bỏ mẫu thay vì suy đoán. Quy tắc chung: bất kỳ OID "đã tính sẵn %"
  nào chưa có trong mục "OID đã xác minh runtime" CLAUDE.md đều PHẢI coi là chưa chắc — verify
  bằng cách walk cả nhánh counter thô liên quan rồi đối chiếu UI/tool chính hãng của thiết bị,
  không chỉ tin tên OID nghe "chuẩn".
- **Getter "sustained" cho rule `duration_min>0` trả `None` khi hết đúng điều kiện == trả `None`
  khi ĐÃ HỒI PHỤC — nếu code chỉ `if value is None: continue` thì alert KHÔNG BAO GIỜ tự resolve.**
  Dính thật 2026-09-28: `_sustained_verdict()` (dùng chung bởi `_sustained_cpu_mem`/
  `_sustained_host_perf`/`_sustained_vm_metric`/`_sustained_wifi_*`/`_sustained_uplink_traffic_max`
  trong `apps/alerts/engine.py`) trả `None` cho cả 2 case "chưa đủ sustain để fire" VÀ "đã hồi phục"
  — không phân biệt được — nên `check_device_alerts` cũ bỏ qua thẳng, không bao giờ tới nhánh resolve.
  Verify runtime: 11/13 alert active loại này đã hồi phục thật từ 1 ngày tới 95 ngày trước nhưng
  chưa từng gửi Recovered. Miễn nhiễm: `device_online`/`if_status` (2 hàm sustained riêng tự trả
  `0.0`/`1.0` rõ ràng thay vì `None`) — đây là lý do bug tồn tại lâu mà không lộ ra, vì rule
  online/offline (test nhiều nhất) lại không dính. Fix: khi sustained=`None` mà có alert active →
  fallback đọc giá trị TỨC THỜI (hàm `getter` plain, không sustain) để xét resolve qua hysteresis —
  resolve không cần sustain, chỉ FIRE mới cần lọc nhiễu qua window. Không có dữ liệu tức thời (mất
  tín hiệu hoàn toàn) → vẫn giữ active, không đoán. Quy tắc chung: **bất kỳ getter nào trả `None`
  để nói "điều kiện fire không đúng" đều PHẢI phân biệt rõ với "đã hồi phục" nếu logic gọi nó có
  nhánh resolve dựa trên `is None`** — 2 khái niệm khác nhau, gộp chung 1 sentinel là nguồn bug.
  Chi tiết + số liệu verify: memory `alert-sustained-never-resolve.md`. Commit `d258bec`.
  ⚠️ **Verify timing của task `@shared_task` PHẢI đọc log Celery thật (`docker compose logs worker
  | grep "Task ... succeeded"`), KHÔNG gọi trực tiếp function trong `manage.py shell` rồi tự đo
  `time.time()`** — gọi trực tiếp bỏ qua hoàn toàn `soft_time_limit`/`time_limit` (chỉ Celery worker
  enforce khi task chạy qua queue thật) VÀ có thể trùng lúc Beat cũng tự trigger cùng task → 2 tiến
  trình cùng tải 1 host (dính thật: gọi tay `poll_all_hyperv()` đo được 500.9s do trùng lúc Beat,
  tưởng regression, nhưng log Celery thật cùng lúc cho thấy task qua queue chạy sạch 98.17s).
- **PowerShell helper function 1 ký tự có thể trùng alias built-in** (`r`=`Invoke-History`,
  `h`=`Get-History`, v.v.) — PowerShell resolve alias TRƯỚC function cùng tên trong 1 số trường hợp,
  khiến hàm tự định nghĩa `function R(...)` bị gọi nhầm thành `Invoke-History`, ném lỗi mơ hồ
  **"Cannot convert 'System.Object[]' to the type 'System.String' required by parameter 'Id'"** —
  bị `try/catch` nuốt im lặng nếu không có debug output riêng, rất khó dò (dính thật 2026-07-07 khi
  nén `PS_SCRIPT`). Trước khi đặt tên helper function ngắn, tránh dùng 1 ký tự đơn lẻ hay kiểm tra
  `Get-Alias <tên>` trên máy Windows thật; ưu tiên tên 2+ ký tự (`RA`, `RS`, `RX`) dù tốn thêm vài
  chục ký tự base64.
- **`GenericIPAddressField` (Postgres `inet`) KHÔNG lưu được chuỗi rỗng `""` — Django tự coi `""`
  là `None` khi build query.** `.exclude(field="")` trên field này sinh SQL
  `NOT (x = %s AND x IS NOT NULL)` với param `None` → `x = NULL` luôn là SQL `NULL` (không phải
  `TRUE`/`FALSE`) → biểu thức `AND`/`NOT` cả dòng thành `NULL` → **WHERE loại bỏ HẾT mọi row, kể cả
  row có giá trị hợp lệ** (SQL chỉ giữ row khi điều kiện `TRUE`, `NULL` bị coi như `FALSE`). Dính
  thật 2026-09-28 khi build `poll_all_ilo`: `.exclude(ilo_ip_address__isnull=True).exclude(
  ilo_ip_address="")` trả **0 devices** dù DB có đủ 3 IP hợp lệ (verify bằng
  `qs.query.sql_with_params()` in ra đúng SQL trên + đếm tay qua `psql`). Fix: field IP/inet chỉ
  cần `.exclude(field__isnull=True)` — KHÔNG thêm `.exclude(field="")` (không bao giờ có "" thật
  trong DB để loại). Quy tắc chung: **filter/exclude bằng `""` trên field không-string thật sự
  (`GenericIPAddressField`, `DateField`, `IntegerField`...) luôn đáng ngờ** — field đó thường coerce
  `""` → `None` ở `to_python`/`get_prep_value`, khiến so sánh `=""` âm thầm thành so sánh `=NULL`
  (luôn `NULL`, không lỗi) — không phải bug hiếm, các field `blank=True, null=True` non-string khác
  trong model (vd `remote_mgmt_ip` ở `TopologyLink`) có nguy cơ y hệt nếu ai lỡ viết
  `.exclude(remote_mgmt_ip="")`.
- **BMC/iLO embedded webserver (HPE iLO 4/5 xác nhận thật) đóng TCP connection sau ĐÚNG 1 request
  dù client gửi HTTP keep-alive.** `requests.Session()` tái dùng pooled connection → request thứ 2
  trên cùng session luôn `ConnectionError: Remote end closed connection without response` — lỗi
  xen kẽ đều đặn OK/ERR/OK/ERR qua nhiều request liên tiếp (dính thật 2026-09-28 khi build
  `apps/collectors/ilo_redfish.py`, xem CLAUDE.md mục "iLO Redfish"). Endpoint/URL hoàn toàn ĐÚNG —
  đừng nhầm sang "sai path" khi thấy lỗi này. Fix: header `Connection: close` mỗi request + retry
  đúng 1 lần bằng session mới (`session.close()` rồi để `requests` tự mở connection mới) khi gặp
  `ConnectionError`. Áp dụng cho bất kỳ tích hợp BMC/thiết bị nhúng nào khác sau này (Dell iDRAC...)
  nếu thấy đúng triệu chứng xen kẽ này — verify lại trên thiết bị đó trước khi copy nguyên fix.
- **Fetch lỗi ở 1 sub-endpoint (không phải fetch gốc) mà chỉ log-and-skip, KHÔNG đánh dấu incomplete
  → counter "đếm số lượng vấn đề" (đếm bằng cách cộng dồn, khởi tạo 0) âm thầm trả 0 GIẢ trông y hệt
  "đã verify sạch".** Dính thật 2026-09-29 (review code, `apps/collectors/ilo_redfish.py`
  `_collect_controller`/`normalize`): `collect_raw()` chỉ coi là lỗi toàn phần khi fetch endpoint
  **gốc** (`ArrayControllers/`) fail (401/404/connection error → trả `None`, `poll_all_ilo` bỏ qua
  hẳn, không lưu). Nhưng fetch lỗi ở **sub-endpoint** (`/LogicalDrives/`, `/DataDrives/`,
  `/StorageEnclosures/`) chỉ log warning rồi đi tiếp với list rỗng — `normalize()` khởi tạo
  `missing_count = 0`/`enclosure_mismatch = 0` rồi CHỈ CỘNG DỒN (không bao giờ trừ), nên 1 sub-fetch
  lỗi giữa chừng cho ra kết quả **giống hệt** "đã verify và đúng là 0 vấn đề" — không có tín hiệu
  nào phân biệt được 2 case. Hệ quả nguy hiểm hơn cả case đơn giản "dữ liệu thiếu": nếu trước đó có
  alert đang active dựa trên field này (`raid_missing_disk_count`), 1 poll không đầy đủ đủ để engine
  đọc thấy "0" và tưởng RAID đã hồi phục → tự resolve, gửi RECOVERED giả, trong khi RAID thật có thể
  vẫn đang lỗi (chỉ là lần này không lấy được dữ liệu). Fix: track cờ hoàn chỉnh riêng
  (`disks_complete`/`enclosures_complete`) qua từng tầng gọi, `normalize()` trả **`None`** (không
  phải `0`) khi cờ False — field `HardwareHealth` liên quan vốn đã nullable, và `_latest_ilo`/
  `_sustained_ilo` (`apps/alerts/engine.py`) vốn đã filter `{field}__isnull=False` per-field (viết
  từ đầu để chọn "giá trị KHÔNG-null gần nhất", không phải để né bug này) nên chỉ cần `normalize()`
  ngưng nói dối là "0", toàn bộ chuỗi tự đọc đúng giá trị lần cuối thật sự đã verify — không cần
  sửa gì ở tầng alert engine. **Quy tắc chung**: bất kỳ hàm nào "đếm số lượng X bất thường" bằng
  cách cộng dồn qua nhiều sub-fetch đều PHẢI phân biệt rõ "đã duyệt hết, đếm được 0" với "duyệt dở
  dang vì 1 sub-fetch lỗi" — im lặng coi 2 case là một luôn thiên về hướng nguy hiểm hơn (báo cáo
  "sạch" khi thực ra chưa biết), nhất là khi con số đó nuôi logic resolve alert.
  ⚠️ **Vòng 2 cùng ngày (review lại đúng bản fix vòng 1 ở trên, phát hiện fix chưa đủ)**: vòng 1
  chỉ che lỗi ở tầng **list** (`/LogicalDrives/`, `/DataDrives/`, `/StorageEnclosures/` — endpoint
  trả *danh sách member*). Lỗi fetch **detail của TỪNG MEMBER riêng lẻ** sau khi đã có list đúng
  (`LogicalDrives/{ld}/`, `StorageEnclosures/{n}/`) vẫn bị `if detail:` nuốt im lặng — chỉ bỏ qua
  member đó khỏi list kết quả, KHÔNG hạ cờ complete. Nguy hiểm hơn cả vòng 1 vì tinh vi hơn: nếu
  đúng cái member fetch lỗi là cái đang Critical/mismatch, phép tính trên PHẦN CÒN LẠI (đã fetch
  được) có thể ra kết quả **tốt hơn thực tế** (vd LD đang Critical fetch lỗi, LD khác OK →
  `logical_drive_worst_code` tính max() trên phần còn lại ra 0 thay vì phải None) — không chỉ là
  "thiếu dữ liệu" mà là "nhầm sang tốt". Bài học áp dụng chung, không chỉ riêng iLO: **fix 1 lớp
  "thiếu dữ liệu" (list rỗng do lỗi) không tự động che luôn lớp sâu hơn (member rỗng do lỗi bên
  trong 1 danh sách vốn đã fetch đúng)** — phải rà TỪNG bước gọi mạng trong hàm, không chỉ bước gọi
  đầu tiên/rõ ràng nhất. Quy trình bắt: đọc lại chính diff vừa fix, hỏi "còn `_get()`/network call
  nào trong hàm này chưa có cặp `else:` hạ cờ complete không?" — không tự tin "đã fix xong" chỉ vì
  test cũ pass, vì test cũ (viết TRƯỚC khi biết bug vòng 2) không cover được case chưa biết tới.
  ⚠️ **Vòng 3 cùng ngày (đúng bug vẫn còn, lần này ở tầng GỐC chứ không phải tầng per-controller)**:
  vòng 1+2 fix hết mọi chỗ "1 sub-fetch BÊN TRONG 1 controller đã có" lỗi. Nhưng nếu chính endpoint
  liệt kê controller (`ArrayControllers/` root) trả HTTP 200 với `Members` rỗng/thiếu, `ac_root`
  vẫn là dict TRUTHY (`{"Members": []}` không rỗng như `{}`) nên KHÔNG trúng check `not ac_root` đã
  có sẵn cho các lỗi root khác (401/404/connection error) → `controllers=[]` lọt xuống
  `normalize()`, vòng for KHÔNG chạy lần nào → mọi cờ complete/counter giữ nguyên giá trị khởi tạo
  (`True`/`0`) → SAI giống hệt 2 vòng trước nhưng ở tầng cao hơn: thiếu HẲN controller, không phải
  thiếu sub-data bên trong 1 controller đã có. Bài học: **"0 phần tử sau khi lọc/liệt kê" và "lỗi
  fetch 1 phần tử cụ thể" là 2 lớp lỗi khác nhau của cùng 1 họ bug ("dữ liệu thiếu trông giống dữ
  liệu sạch") — sửa xong lớp trong (per-item) không có nghĩa lớp ngoài (per-list, khi cả list rỗng)
  đã được che.** Với danh sách mà nghiệp vụ NGẦM ĐỊNH luôn ≥1 phần tử (ở đây: host đã cấu hình
  `ilo_ip_address` thì ngầm định có RAID controller), danh sách rỗng dù HTTP 200 vẫn phải coi là
  thất bại, không phải "đúng là rỗng thật". Fix 2 lớp: (1) chặn tại nguồn — `collect_raw()` tự trả
  `None` khi `controllers` rỗng sau vòng lặp Members, y hệt các nhánh lỗi root khác; (2) phòng thủ
  độc lập ở `normalize()` — `controllers_list` rỗng thì tự hạ cả 3 cờ complete, đề phòng raw dựng
  tay/gọi trực tiếp không qua `collect_raw()`. Quy tắc chung mở rộng: khi audit 1 hàm "đếm/tổng hợp
  qua danh sách con", phải tự hỏi CẢ 2 câu — "1 phần tử trong danh sách lỗi fetch thì sao" (đã hỏi
  ở vòng 2) VÀ "cả danh sách rỗng/không lấy được thì sao, danh sách đó có được phép rỗng thật theo
  nghiệp vụ không" (câu thứ 2 dễ bị bỏ sót hơn vì "rỗng" trông giống 1 kết quả hợp lệ, không giống
  1 lỗi).
  ⚠️ **Vòng 4 cùng ngày (rủi ro suy luận từ đọc code, CHƯA quan sát trên iLO thật)**: các collection
  CON (`/LogicalDrives/`, `/DataDrives/`, `/StorageEnclosures/` — khác collection GỐC
  `/ArrayControllers/` đã fix ở vòng 3) vẫn dùng `payload.get("Members", [])` trực tiếp — pattern
  này gộp chung 2 trường hợp KHÁC NHAU thành cùng 1 kết quả `[]`: "JSON đúng cấu trúc Redfish,
  `Members: []` rỗng THẬT" (hợp lệ) và "JSON 200 nhưng THIẾU HẲN key `Members`, hoặc `Members`
  không phải list" (bất thường/glitch, KHÔNG phải "0 phần tử đã xác nhận"). `StorageEnclosures`
  còn có biến thể tệ hơn: check cũ `encl_status == 200 and encl_list is not None` coi
  `encl_list={}` (dict rỗng, thiếu Members) là `enclosures_complete=True` (SAI), rồi
  `elif encl_list:` (truthy check) lại bỏ qua vòng lặp vì `{}` falsy — kết quả: không log, không hạ
  cờ, im lặng hoàn toàn. Fix: helper `IloRedfishClient._members(payload)` — trả `list["Members"]`
  nếu là list hợp lệ, `None` nếu thiếu key/sai kiểu; áp dụng thống nhất cho cả 3 sub-collection,
  thay hẳn pattern `x.get("Members", [])` + check `is not None`/truthy rời rạc trước đó. Case trả
  `None` phải hạ cờ complete tương ứng (`disks_complete`/`logical_drives_complete`/
  `enclosures_complete`), case trả `[]` thật thì vẫn coi hợp lệ (giữ nguyên hành vi cũ cho case này
  — KHÔNG áp dụng lý luận "nghiệp vụ ngầm định ≥1 phần tử" của vòng 3 xuống tầng sub-collection,
  vì chưa có bằng chứng 1 LD/enclosure/DataDrive thật sự luôn ≥1 — chỉ fix đúng phạm vi user chỉ ra
  là "JSON thiếu Members", không mở rộng suy luận thêm). Quy tắc chung: `dict.get("Members", [])`
  hay bất kỳ pattern `.get(key, <default rỗng>)` nào dùng để build 1 list-để-lặp đều nên tự hỏi
  "default rỗng này có đang che giấu 1 case lỗi/bất thường không, hay chỉ đang xử lý đúng 1 case
  hợp lệ" — 2 lần default che-lỗi đã tìm thấy hôm nay (vòng 3 ở root, vòng 4 ở 3 sub-collection)
  đều đúng dạng này.
- **`${VAR}` trong `docker-compose.yml` KHÔNG đọc qua `env_file:` của service — chỉ Compose nội
  suy từ file tên đúng `.env` cạnh compose file (hoặc `--env-file`/shell env).** `env_file:` chỉ bơm
  biến vào **container lúc chạy**, không tham gia bước nội suy `${...}` trong chính file YAML (bước
  đó chạy client-side, TRƯỚC khi container tồn tại). Dính thật 2026-09-28 (audit security từ báo cáo
  ngoài): service `db` khai cả `env_file: .env.production` LẪN
  `environment: { POSTGRES_USER: ${DB_USER:-monitor_user}, POSTGRES_PASSWORD: ${DB_PASSWORD:-change_me_db_password} }`
  — trong khi server **chỉ có `.env.production`, không có `.env`** (xác nhận qua SSH `ls -la`) nên
  `${DB_USER}`/`${DB_PASSWORD}` luôn rơi về default, còn `app`/`worker`/`beat` (chỉ dùng `env_file`,
  không có `environment: ${...}`) nhận đúng giá trị thật → 2 bộ credential khác nhau cho cùng 1 DB.
  Verify runtime `docker compose config` trên server thật: `POSTGRES_PASSWORD` == default
  `change_me_db_password`, KHÁC `DB_PASSWORD` thật app đang dùng — bug **đang tồn tại**, chỉ "vô hại
  tạm thời" vì Postgres image chỉ áp `POSTGRES_*` lúc **init data dir rỗng lần đầu** (container đang
  chạy dùng credential đã ghi sẵn trong `postgres_data` volume, không đọc lại env mỗi lần start) —
  sẽ nổ ngay khi ai `docker compose down -v`/tạo volume mới (disaster recovery, migrate host) vì lúc
  đó Postgres init thật sự bằng giá trị default sai. Fix: bỏ hẳn `environment: ${...}` ở service cần
  secret thật, chỉ dùng `env_file:` trỏ đúng file — và file đó phải tự có sẵn đúng tên biến mà image
  cần (image `postgres` cần `POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD`, không đọc `DB_*` của
  Django) → thêm 3 khoá `POSTGRES_*` mirror `DB_*` thẳng vào `.env.production`/`.env`/`.env.example`
  (xem CLAUDE.md). Healthcheck cùng lỗi (`pg_isready -U ${DB_USER}`) — sửa bằng `$$VAR` (2 dấu `$`)
  để Compose KHÔNG nội suy, để nguyên cho shell **trong container** tự thay bằng biến thật lúc chạy
  (`test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]`). Quy tắc chung: **service
  nào cần secret/giá trị thật khớp `env_file` của chính nó thì đọc thẳng qua `env_file`, đừng đi
  vòng qua `${VAR}` nội suy YAML** — 2 cơ chế độc lập, dễ tưởng đã "dùng chung 1 nguồn" nhưng thực
  ra không. Kèm theo: `Dockerfile` `COPY . .` không có `.dockerignore` từng đóng gói cả `.env*`,
  `db.sqlite3`, `backups/`, `.git/`, `venv/`, `scratchpad/` vào build context — thêm `.dockerignore`
  loại trừ secret/dữ liệu vận hành/VCS/venv (không đổi phần code đóng gói vào image, chỉ đổi
  context).
- **`ENTRYPOINT` dùng chung cho nhiều service (app/worker/beat) mà chạy `migrate`/`collectstatic`
  vô điều kiện → N container cùng migrate/collectstatic đồng thời khi deploy.** Dính thật
  2026-09-28 (security review đợt 2, người ngoài phát hiện): [entrypoint.sh](../../entrypoint.sh)
  cũ chạy `migrate --noinput` → `sync_beat_expires` → `collectstatic --noinput --clear` **trước
  cả khi kiểm tra `$# -gt 0`** (có `command:` riêng hay không) — `app`/`worker`/`beat` đều dùng
  chung `ENTRYPOINT` này ([Dockerfile](../../Dockerfile)) nên `deploy.sh`/`docker compose up -d
  --build app worker beat` khởi động gần như đồng thời khiến 2-3 container cùng gọi `migrate`
  (đua tranh áp schema) + cùng `collectstatic --clear` (xoá rồi ghi static trong khi container
  khác cũng đang xoá/ghi → nginx đọc `static_volume` có thể trúng khoảng trống). Fix: đảo thứ tự
  — kiểm tra `$# -gt 0` (worker/beat luôn có `command: celery ...`) **TRƯỚC**, có thì `exec "$@"`
  ngay (không migrate); chỉ nhánh KHÔNG có command riêng (= container `app`, dùng gunicorn mặc
  định ở cuối script) mới migrate/collectstatic — 1 lần duy nhất mỗi lần deploy. Kèm theo: thêm
  `depends_on: app: condition: service_healthy` cho `worker`/`beat` trong `docker-compose.yml`
  (app healthcheck `/health/` chỉ pass sau khi gunicorn lên = migrate đã xong) — đảm bảo
  worker/beat không chạy task Celery trước khi schema kịp migrate; migrate lỗi → `app` exit →
  compose từ chối start worker/beat, deploy fail rõ ràng thay vì âm thầm chạy trên schema cũ.
  Quy tắc chung: **`ENTRYPOINT`/`command` dùng chung giữa nhiều service phải tự hỏi "việc chạy 1
  lần" (migrate, seed data, sync schedule…) có đang bị N container cùng làm không** — nếu deploy
  script start nhiều service cùng lúc (không tuần tự), câu trả lời mặc định là CÓ trừ khi tách
  nhánh rõ ràng như trên.
- **Alert engine: hàm resolve thiếu lock trong khi hàm fire đã có → gửi trùng notification khi 2
  đường eval (inline sau poll + `evaluate_alert_rules` định kỳ) chạy gần như đồng thời trên các
  Celery worker khác nhau.** Dính thật 2026-09-28: `_fire_alert`
  ([apps/alerts/engine.py](../../apps/alerts/engine.py)) đã dùng `transaction.atomic()` +
  `select_for_update()` khoá `Device` để serialize fire, nhưng `_resolve_alert` cạnh đó lại đọc
  `alerts_to_resolve` KHÔNG lock → gửi RECOVERED → mới update `is_active=False`: 2 lời gọi trùng
  thời điểm cùng đọc thấy `is_active=True`, cùng gửi trùng. Fix: cùng pattern lock — "claim"
  (lock `Device` + update `is_active=False`) xong trong transaction rồi MỚI gửi notification
  ngoài transaction (không giữ DB lock trong lúc chờ HTTP Telegram/email chậm). Quy tắc chung:
  **bất kỳ cặp hàm fire/resolve (hay create/update/delete) nào thao tác chung 1 state đổi được từ
  nhiều nơi đồng thời — nếu 1 hàm trong cặp đã có lock mà hàm còn lại không, đó gần như luôn là
  thiếu sót chứ không phải cố ý** — kiểm tra tính đối xứng khi audit.
- **View ghi dữ liệu chỉ `@login_required` mà thiếu check quyền ghi (`_can_write`/`is_admin`) —
  dễ lọt qua review vì trông giống các view CRUD khác trong cùng file đã check đúng.** Dính thật
  2026-09-28: `alert_acknowledge` ([apps/alerts/views.py](../../apps/alerts/views.py)) chỉ
  `@login_required`, thiếu `_can_write` mà `rule_create`/`rule_edit`/`storage` NGAY TRONG CÙNG
  FILE đã có — Read-Only Operators acknowledge được, trái RBAC "chỉ xem" (xem CLAUDE.md mục
  RBAC). Quy tắc chung: khi thêm/audit 1 view ghi dữ liệu, **grep các view khác trong cùng
  app/file trước** — nếu chúng đều gọi `_can_write`/`is_admin`, view mới/view đang xét thiếu là
  dấu hiệu bug rõ ràng, không phải khác biệt có chủ đích.
- **"Xoá rồi tạo lại" (xoá bản ghi cũ trước khi validate + tạo bản ghi mới) làm mất dữ liệu khi
  validate fail, vì hàm tạo mới trả lỗi bằng `JsonResponse` (không raise exception).** Dính thật
  2026-09-28: `_update_link` ([apps/dashboard/topology_links_api.py](../../apps/dashboard/topology_links_api.py))
  xoá `existing` NGAY rồi mới gọi `_create_link(request)` — hàm đó tự validate và trả lỗi qua
  `return _bad(...)` (JsonResponse status 400), không exception, nên transaction (nếu có) sẽ
  KHÔNG tự rollback theo cơ chế "exception propagate" thông thường của Django. Request sai (thiếu
  field, switch đích không tồn tại...) → mất link cũ vĩnh viễn, không tạo được link thay thế. Fix:
  bọc xoá+tạo trong `transaction.atomic()`, kiểm tra `response.status_code != 200` rồi tự gọi
  `transaction.set_rollback(True)` — Django hỗ trợ ép rollback trong atomic block mà không cần
  raise. Quy tắc chung: pattern "xoá cũ → tạo mới từ input chưa validate" luôn nguy hiểm; nếu hàm
  tạo mới báo lỗi bằng return value (không exception) thì `transaction.atomic()` không tự cứu —
  phải chủ động `set_rollback(True)` khi thấy response lỗi.
- **`.dockerignore` loại đúng secret CỦA APP rồi vẫn có thể sót secret CỦA SERVICE KHÁC nằm
  cùng thư mục con trong repo (vd cert của service dùng bind-mount, không build từ Dockerfile
  này).** Dính thật 2026-09-28 (security review đợt 3): đợt trước đã thêm `.dockerignore` loại
  `.env*`/DB/backup, nhưng CHƯA loại `nginx/certs/` — service `nginx`
  ([docker-compose.yml](../../docker-compose.yml)) dùng `image: nginx:alpine` +
  bind-mount `./nginx/certs:/etc/nginx/certs:ro` (KHÔNG build từ [Dockerfile](../../Dockerfile)),
  nhưng `app`/`worker`/`beat` build từ Dockerfile đó với `COPY . .` lại vô tình gom luôn thư mục
  `nginx/` (cùng cấp trong repo) → TLS **private key thật** (`server.key`, mode 600) bị đóng gói
  vào image của 3 service không hề dùng tới nó. Verify SSH: `docker compose exec app ls
  /app/nginx/certs/` xác nhận key đã nằm SẴN trong container `app` đang chạy trước khi fix — bất
  kỳ ai có quyền `docker compose exec` vào app/worker/beat (rộng hơn nhóm quản lý cert) đọc được
  thẳng. Quy tắc chung: audit `.dockerignore` không chỉ hỏi "secret của service ĐANG build có bị
  lọt không" mà phải hỏi thêm **"repo có thư mục nào chứa secret CỦA SERVICE KHÁC (bind-mount,
  không build từ Dockerfile này) nằm trong cùng build context không"** — build context luôn là
  TOÀN BỘ thư mục gửi cho Docker daemon, không tự giới hạn theo service.
- **JS: hàm `esc()` kiểu `div.textContent = s; return div.innerHTML;` chỉ an toàn khi chèn vào
  TEXT CONTENT — KHÔNG an toàn khi chèn vào thuộc tính HTML (`title="..."`, `href="..."`...).**
  Dính thật 2026-09-28: trick này escape đúng `&`/`<`/`>` (browser tự làm khi serialize text
  node) nhưng KHÔNG escape `"`/`'` (2 ký tự này vô nghĩa trong ngữ cảnh text content nên không bị
  encode) — verify bằng Node: payload `x" onmouseover="alert(1)` qua hàm này giữ nguyên dấu `"`.
  `discovery.html` chèn kết quả vào `title="${esc(...)}"` với dữ liệu SNMP sysDescr (thiết bị
  ngoài mạng, không tin được) → payload trên thoát khỏi `title=`, tự thêm thuộc tính
  `onmouseover=` mới → XSS thật. Fix: đổi sang escape đầy đủ `&<>"'` bằng regex (không dùng DOM
  round-trip) — đúng pattern đã có sẵn ở `wlan_detail.html`, áp cho MỌI usage của `esc()` bất kể
  đang chèn vào text hay attribute (không cần 2 hàm riêng — bản full-encode an toàn cho cả 2 ngữ
  cảnh, không có usage nào bị "quá tay"). Quy tắc chung: khi audit 1 hàm tên `esc`/`escape`, phải
  xác định rõ nó an toàn cho ngữ cảnh nào (text content / attribute value / URL / JS string...) —
  3 ngữ cảnh HTML đầu có luật escape KHÁC NHAU, hàm chỉ đúng cho 1 ngữ cảnh mà dùng ở chỗ khác là
  bug, không phải "escape rồi thì luôn an toàn".
- **"Commit trạng thái TRƯỚC, gửi notification SAU" (đúng để chặn race gửi trùng — xem bẫy
  `_resolve_alert` thiếu lock ở trên) có tác dụng phụ: worker chết ĐÚNG lúc giữa 2 bước làm mất
  trắng notification, không có gì để retry (trạng thái đã commit nên vòng eval sau coi là "đã xử
  lý xong").** Dính thật 2026-09-28 (security review đợt 3, đúng như review dự đoán khi tôi tự
  sửa `_resolve_alert` ở đợt trước): fix "lock để chặn gửi trùng" tự nó tạo ra 1 cửa sổ mất dữ
  liệu mới nếu không kèm theo cơ chế durable-intent. `_fire_alert` vốn đã có cùng cấu trúc "commit
  trước, gửi sau" từ trước (không phải do fix trước gây ra) nên mang cùng rủi ro — sửa đối xứng cả
  2 (xem bẫy "hàm fire/resolve thiếu đối xứng" ở trên — cùng nguyên tắc, ở khía cạnh khác). Fix:
  transactional-outbox-lite — ghi `AlertNotification(status="pending")` **CÙNG transaction** với
  lúc commit trạng thái Alert (durable "ý định gửi" tồn tại dù crash ngay sau commit); gửi thật
  xong UPDATE row đó thành sent/failed (không tạo row mới); task định kỳ quét `pending` cũ hơn
  `grace_secs` (tránh đua với 1 lần gửi đang chạy hợp lệ) và gửi lại đúng loại đã lưu tường minh
  (field `kind`, KHÔNG suy đoán lại từ trạng thái hiện tại — trạng thái có thể đã đổi lần nữa giữa
  lúc ghi pending và lúc retry). Quy tắc chung: **bất kỳ đâu áp dụng pattern "commit rồi mới làm
  side-effect không-transactional" (gửi email, gọi webhook, ghi file...) để chặn race/gửi trùng,
  đều PHẢI đi kèm 1 dạng "durable intent" (outbox row/queue message) — nếu không, đã đổi 1 bug
  (gửi trùng) lấy 1 bug khác (mất tin) chứ không thực sự giải quyết được cả 2.**
- **"Durable intent" (outbox row) tự nó KHÔNG đủ — phải CLAIM nguyên tử row đó trước khi dùng,
  nếu không outbox chỉ dời bug "gửi trùng" từ chỗ cũ sang chỗ mới (task retry).** Dính thật
  2026-09-28: bản outbox-lite đầu tiên (ghi ở bẫy ngay trên) tạo row `status="pending"` đúng,
  nhưng `retry_pending_alert_notifications` lại `list()` toàn bộ row pending rồi gửi tuần tự —
  KHÔNG khoá/claim gì cả. 2 lần gọi hàm này chồng nhau (task trước >120s chu kỳ beat, nhiều beat
  process, gọi tay trùng lúc sweep chạy) đọc thấy CÙNG row → cùng gửi → trùng — y hệt lớp bug mà
  outbox pattern định giải quyết, chỉ chuyển từ `_resolve_alert` sang task retry. Phát hiện thêm
  khi sửa: không chỉ 2 lần sweep tranh nhau — sweep còn có thể tranh với chính lệnh gọi GỐC
  (`_dispatch_notifications`) nếu nó gửi chậm hơn ngưỡng "coi là kẹt" của sweep (channel `email`
  qua Django `send_mail` không có `EMAIL_TIMEOUT` nên có thể treo lâu thật). Fix: **UPDATE có
  điều kiện làm compare-and-swap** — `AlertNotification.objects.filter(pk=pk,
  status="pending").update(status="processing")`: DB đảm bảo chỉ 1 trong N lệnh UPDATE đồng thời
  trên CÙNG row nhận được số dòng thay đổi > 0 (row lock cấp DB), N-1 lệnh còn lại nhận 0 → tự bỏ
  qua. Áp dụng claim này ở MỌI nơi có thể chạm vào row outbox (cả lệnh gọi gốc lẫn task retry),
  không chỉ ở retry. Thu hồi row `processing` bị bỏ rơi (claim thành công nhưng chết trước khi
  gửi xong) cần 1 field timestamp riêng cập nhật MỖI LẦN đổi status (`updated_at`, `auto_now=True`
  — KHÁC field `auto_now_add` chỉ set lúc tạo, dễ nhầm) + ngưỡng "coi là kẹt" dài hơn ngưỡng ban
  đầu (`stale_processing_secs` > `grace_secs`, vì 1 tiến trình ĐANG gửi thật hợp lệ cần thời gian
  dài hơn "vừa tạo intent"). ⚠️ Claim lại row `processing` cũ PHẢI re-check cutoff NGAY TRONG
  WHERE của chính câu UPDATE claim (không chỉ ở bước SELECT chọn candidate trước đó) — nếu chỉ
  lọc candidate 1 lần rồi loop claim từng row bằng điều kiện lỏng hơn (vd chỉ `status IN
  (pending, processing)` không kèm lại mốc thời gian), 2 lần claim chồng nhau trên row processing
  vừa được claim xong (updated_at=NOW rất mới) vẫn cùng khớp điều kiện lỏng đó → double-claim.
  Quy tắc chung: **outbox pattern có 2 nửa bắt buộc — (a) ghi durable intent CÙNG transaction với
  đổi trạng thái nguồn, (b) claim nguyên tử trước khi hành động trên intent đó — thiếu nửa (b) là
  outbox nửa vời, tự nó tạo lại đúng bug mà nửa (a) định giải quyết ở 1 tầng khác.** Migration
  `0007` (field `updated_at` + `models.Index(fields=["status","sent_at"])`/
  `["status","updated_at"]` — không có index này thì sweep định kỳ mỗi 120s là full-table scan
  khi lịch sử `AlertNotification` lớn dần, phát hiện Low riêng nhưng sửa cùng lúc vì đụng chung
  model).
- **"Claim nguyên tử" (bẫy ngay trên) chặn 2 lệnh UPDATE THEO ĐÚNG NGHĨA ĐEN chạy đồng thời trên
  CÙNG row — nhưng KHÔNG chặn được việc chủ CŨ (đã claim, chưa chết, chỉ đang chạy CHẬM hơn
  ngưỡng "coi là kẹt") vẫn tự gửi thật RỒI ghi đè kết quả của chủ MỚI (đã reclaim + gửi + finalize
  xong trước nó).** Dính thật 2026-09-29 (review tiếp theo, soi đúng code outbox mới viết): nếu
  `stale_processing_secs` (300s) không thực sự là giới hạn TRÊN của "1 lần gửi hợp lệ mất bao
  lâu" — mà là ước lượng, vì channel `email` qua Django `send_mail` chưa set `EMAIL_TIMEOUT` nên
  smtplib có thể treo VÔ THỜI HẠN — thì 1 lần gửi chậm thật (không chết) có thể bị sweep coi nhầm
  là kẹt, reclaim, gửi lại. Khi đó CẢ 2 bên đều gọi hàm gửi thật (gửi trùng không tránh được lúc
  này, phải chặn TỪ GỐC bằng timeout — xem ý 2 dưới), nhưng nguy hiểm hơn: UPDATE finalize của 2
  bên đều chỉ filter theo `status="processing"` (không có gì phân biệt "processing của TÔI" vs
  "processing của người khác đã reclaim") → bên finalize SAU (dù là chủ cũ tới muộn) ghi đè thẳng
  lên kết quả ĐÚNG mà chủ mới đã ghi trước đó (`sent`→`failed` hoặc ngược lại), không có gì phát
  hiện được. Fix 2 lớp, PHẢI làm cả 2 (1 lớp không đủ): (1) **Đặt timeout hữu hạn cho MỌI kênh
  gửi** — bound "1 lần gửi hợp lệ" thật sự ngắn hơn hẳn `stale_processing_secs`, biến ước lượng
  thành giới hạn cứng (Django `EMAIL_TIMEOUT` setting — webhook/telegram đã có `timeout=10` từ
  trước, chỉ email thiếu). (2) **Claim token** — field `claim_token` (random, vd `uuid4().hex`)
  ghi lại MỖI LẦN claim (kể cả reclaim); câu UPDATE finalize phải match ĐÚNG token đã ghi lúc
  MÌNH claim (`.filter(status="processing", claim_token=token_cua_minh).update(...)`) — chủ cũ
  tới muộn có token đã lỗi thời (bị ghi đè lúc reclaim) nên UPDATE của nó khớp 0 dòng → tự bỏ qua,
  KHÔNG tạo row mới thay thế (khác hẳn nhánh "không có row nào tồn tại" — phải phân biệt 2
  trường hợp `update()==0` này bằng biến `claimed` đã có sẵn từ lúc đầu hàm, không suy đoán lại).
  Quy tắc chung: **outbox pattern có claim atomically đúng vẫn CHƯA đủ nếu thời gian giữa "claim"
  và "finalize" không có giới hạn trên thật sự (do side-effect bên ngoài không timeout) — phải
  bound thời gian đó bằng timeout TRƯỚC, rồi mới thêm claim token làm phòng thủ thứ 2 cho các
  nguyên nhân trễ khác (GC pause, network buffering...) mà timeout không bound hết được.**
- **"Recovery chỉ queue 1 lần tại thời điểm resolve" (dựa trên trạng thái fire NGAY LÚC ĐÓ) bỏ
  sót trường hợp fire vẫn `pending` lúc resolve chạy rồi mới được gửi trễ sau đó — quyết định
  "có nợ recovery hay không" chỉ được đánh giá ĐÚNG 1 LẦN, không có gì kích hoạt lại nếu điều
  kiện đổi sau đó.** Dính thật 2026-09-29: `_resolve_alert` filter fire `status="sent"` để quyết
  định channel nào cần recovery — đúng cho ca thường (fire gửi xong TRƯỚC khi resolve chạy), sai
  cho ca hiếm nhưng có thật (worker chết giữa lúc `_fire_alert` commit `pending` và lúc kịp
  dispatch, alert hồi phục rồi resolve chạy TRƯỚC khi fire kịp gửi) — fire đó sau này vẫn được
  `retry_pending_alert_notifications` gửi thành công (trễ), nhưng KHÔNG BAO GIỜ có recovery theo
  sau vì thời điểm duy nhất từng xét "cần recovery không" đã qua rồi. Verify bằng test dựng đúng
  thứ tự sự kiện (tạo fire pending → gọi thẳng `_resolve_alert` → mới cho sweep gửi fire trễ) —
  fail thật trên code cũ (`AlertNotification.DoesNotExist`, không route nào tạo recovery). Fix:
  coi "có nợ recovery không" là điều kiện phải re-check ở CẢ 2 nơi có thể làm nó đúng — nơi cũ
  (`_resolve_alert`, khi resolve chạy SAU khi fire đã sent) VÀ nơi mới
  (`_queue_late_recovery_if_resolved`, gọi ngay khi 1 fire row CHUYỂN "sent" thật, dù từ dispatch
  gốc hay sweep — khi resolve đã chạy TRƯỚC) — bên nào xảy ra sau cùng sẽ là bên phát hiện. Quy
  tắc chung: **bất kỳ quyết định "X có cần làm Y không" dựa trên 2 sự kiện độc lập có thể xảy ra
  theo THỨ TỰ BẤT KỲ (ở đây: "fire đã gửi" và "đã resolve") mà chỉ được đánh giá tại 1 trong 2 nơi
  — phải re-check ở CẢ nơi còn lại, nếu không nhánh "sự kiện kia xảy ra trước" sẽ luôn bị bỏ sót.**
- **Fix cho 1 bug outbox bằng cách "gọi thêm 1 hàm phụ SAU KHI câu UPDATE chính đã commit" tự nó
  tái tạo ĐÚNG root cause đang sửa, chỉ ở 1 cặp thao tác khác.** Dính thật 2026-09-29 (review vòng
  2, cùng ngày, soi đúng bản fix "recovery chỉ queue 1 lần tại thời điểm resolve" ở bẫy ngay
  trên): bản fix đó gọi `_queue_late_recovery_if_resolved(...)` bằng 1 CÂU DB RIÊNG, ngay sau khi
  `.update(status="sent")` đã tự COMMIT (Django autocommit ngoài `transaction.atomic()`) — worker
  chết ĐÚNG giữa 2 câu này thì fire đã "sent" (trạng thái CUỐI, không gì kích hoạt lại) nhưng
  recovery chưa kịp tạo → mất vĩnh viễn, y hệt bug đang sửa nhưng chuyển sang 1 cặp thao tác khác.
  Verify: mock hàm phụ ném exception, assert câu UPDATE trước đó cũng phải rollback — fail thật
  (status vẫn "sent" dù bước sau lỗi) khi 2 thao tác KHÔNG cùng 1 `transaction.atomic()`. Fix: gộp
  cả 2 (`.update(status="sent")` + gọi hàm phụ) vào CHUNG 1 transaction (`_finalize_fire_sent`) —
  crash giữa chừng thì TOÀN BỘ rollback, sweep sau reclaim (row vẫn "processing") và làm lại
  nguyên vẹn cả 2 bước. Quy tắc chung: **bất cứ khi nào 1 thao tác ghi trạng thái xong rồi "tiện
  thể" gọi thêm 1 hàm phụ để quyết định/ghi thêm state khác (dù hàm phụ đó nhỏ, tưởng như best-
  effort) — nếu hàm phụ đó có thể thất bại/bị ngắt giữa chừng (crash, exception, timeout), 2 thao
  tác PHẢI chung 1 transaction, nếu không sẽ luôn có 1 khoảng hở "đã ghi A nhưng chưa ghi B" không
  gì retry được vì A đã là trạng thái cuối.**
- **Khoá "cùng ĐỐI TƯỢNG" (Device) ở 1 đường ghi (vd `_resolve_alert`) mà đường ghi CẠNH TRANH
  khác (đọc/ghi state phụ thuộc CÙNG object đó) lại không khoá gì — 2 SELECT độc lập đọc phải giá
  trị "cũ" của nhau, không bên nào phát hiện được thay đổi của bên kia.** Dính thật 2026-09-29
  (review vòng 2): `_resolve_alert` đọc "fire nào đã sent" và
  `_queue_late_recovery_if_resolved` đọc "alert đã `is_active=False` chưa" là 2 câu SELECT không
  khoá gì chung — nếu fire chuyển "sent" đúng lúc nằm GIỮA lúc `_resolve_alert` đọc xong danh sách
  "đã gửi" và lúc nó COMMIT `is_active=False`, cả 2 bên đều đọc phải giá trị CŨ của phía kia → cả
  2 đều bỏ qua, không bên nào tạo recovery. Fix theo đúng gợi ý review: bên ghi mới
  (`_finalize_fire_sent`) khoá `Device` (`select_for_update()`) giống hệt `_fire_alert`/
  `_resolve_alert` TRƯỚC KHI đọc/ghi — 2 giao dịch cạnh tranh cùng device luôn serialize trên
  Postgres, bên nào commit trước thì bên sau LUÔN đọc thấy state MỚI (không còn đọc phải giá trị
  cũ). ⚠️ **Đính chính cùng ngày — đã ghi nhầm "SQLite bỏ qua select_for_update" ở lần viết đầu
  của bẫy này mà KHÔNG kiểm chứng dev/test project này thực sự chạy trên engine nào** (vi phạm
  chính nguyên tắc §0 "không suy luận linh tinh"). Verify lại bằng kết nối trực tiếp:
  `config/settings/development.py` hard-code `ENGINE: postgresql` (không có nhánh sqlite), `.env`
  có `DB_HOST=localhost` trỏ 1 Postgres 18.4 thật đang chạy trên máy dev, `pytest.ini` trỏ
  `config.settings.development` → **toàn bộ test suite của project này luôn chạy trên Postgres
  thật, kể cả trước khi review vòng 2 này** — KHÔNG có SQLite ở đâu trong project. `select_for_update`
  do đó CÓ lấy khoá row Postgres thật ngay cả khi chạy test. Giới hạn thật sự (vẫn đúng, chỉ khác
  lý do): test hiện tại chạy TUẦN TỰ 1 thread — không có 2 giao dịch nào thực sự cạnh tranh CÙNG
  LÚC để khoá phải phát huy tác dụng chặn nhau, nên vẫn KHÔNG chứng minh được serialization thật
  dưới tải đồng thời (muốn chứng minh cần viết test đa luồng/đa tiến trình thật, dùng
  `pytest.mark.django_db(transaction=True)` để cho phép nhiều connection thật) — ghi rõ giới hạn
  này trong test/docstring, không overclaim đã "test được race". Quy tắc chung áp dụng ngay cho
  chính lỗi này: **trước khi viết bất kỳ kết luận "X không chứng minh được vì backend Y" vào skill/
  docstring, phải TỰ KIỂM TRA project này thực sự dùng backend gì (đọc settings + thử kết nối),
  không suy luận từ hiểu biết chung ("dự án Django/pytest hay dùng SQLite cho test") — đúng y hệt
  bẫy "không đoán mò OID/enum" đã áp dụng cho phần cứng mạng, giờ áp dụng luôn cho hạ tầng test.**
  Quy tắc chung (phần gốc của bẫy, vẫn đúng): **khi 2+
  đường ghi độc lập cùng đọc/ghi 1 field (vd `Alert.is_active`) để quyết định 1 hành động không
  thể làm lại (gửi notification), TẤT CẢ các đường đó phải khoá CHUNG 1 đối tượng — khoá ở 1 đường
  mà đường còn lại không khoá thì khoá đó vô nghĩa, chỉ tự chặn được chính nó với chính nó.**
- **Quyết định "gửi thông báo cho rule nhiều channel" theo cấp ALERT (any channel đã gửi → gửi
  cho MỌI channel) thay vì theo cấp (alert, channel) — channel chậm có thể nhận thông báo giai
  đoạn SAU (recovery) trước khi từng nhận thông báo giai đoạn TRƯỚC (fire) của cùng channel đó.**
  Dính thật 2026-09-29 (review vòng 2): `_resolve_alert` chỉ cần 1 channel có fire "sent" là tạo
  recovery cho MỌI channel trong `rule.channels`, kể cả channel khác còn "pending" (SMTP treo,
  hàng đợi chậm) — recipient trên channel đó nhận "✅ RECOVERED" (do outbox recovery mới hơn được
  sweep ưu tiên nhờ `Meta.ordering=["-sent_at"]`) TRƯỚC KHI từng nhận "🔴 ALERT" gốc, gây hiểu
  lầm nghiêm trọng hơn cả việc chậm trễ đơn thuần. Fix: đổi tập hợp quyết định từ `set(alert_id)`
  sang `set((alert_id, channel))`, filter + dispatch theo từng cặp. Quy tắc chung: **rule/alert có
  nhiều channel độc lập (khác tốc độ gửi, khác khả năng lỗi) — mọi quyết định "đã thông báo đủ
  chưa"/"cần thông báo tiếp không" phải tính theo TỪNG channel, không suy rộng từ 1 channel đại
  diện sang cả nhóm.**
- **2 bản sao của CÙNG 1 danh sách `choices` (vd metric key → label) ở 2 file khác nhau sẽ lệch
  nhau theo thời gian — khi lệch, form Django dùng `forms.Select(choices=...)` không báo lỗi gì,
  chỉ âm thầm đổi sai dữ liệu lúc Lưu.** Dính thật 2026-09-29 (tự phát hiện khi user nhờ review
  lại phần cảnh báo iLO, không phải review ngoài): `apps/alerts/forms.py::METRIC_CHOICES` (dùng
  cho `AlertRuleForm`, dropdown ở `/alerts/rules/<id>/edit/`) là 1 list tự chép tay, tách biệt
  hoàn toàn với `AlertRule.metric_label` (`apps/alerts/models.py`) — cả 2 lẽ ra phải cùng 1 tập
  metric key. `METRIC_CHOICES` không được cập nhật khi thêm 11 metric host-perf HyperV (2026-07-07)
  và 4 metric `raid_*` iLO (2026-09-28) — 3 rule iLO tạo bằng `seed_alert_rules.py`
  (`AlertRule.objects.create(**dict)`, không qua `ModelForm` nên không bị chặn) có `metric` hợp lệ
  trong DB nhưng KHÔNG nằm trong `METRIC_CHOICES`. Cơ chế lỗi: HTML `<select>` không có `<option>`
  nào mang `selected` khi giá trị hiện tại của field không khớp option nào trong `choices` → trình
  duyệt mặc định hiển thị VÀ SUBMIT option ĐẦU TIÊN trong list — mở sửa 1 rule như vậy qua UI (kể
  cả chỉ để tắt/bật, không đụng ô Metric) rồi bấm Lưu sẽ âm thầm đổi `rule.metric` sang giá trị
  sai, không có lỗi/cảnh báo nào. Đây là cùng HỌ bug với "seed script không tự dọn DB" và
  "gỡ vendor phải lần theo mọi feature ăn theo" đã ghi ở trên — khác ở chỗ lần này bug nằm ở
  chính cơ chế Django Form/HTML `<select>` chứ không phải ở tầng feature. Fix root cause (không vá
  bằng cách thêm thủ công các entry thiếu — sẽ lại lệch lần sau): gộp về 1 nguồn — thêm
  `AlertRule.METRIC_LABELS` (dict, class-level, dùng bởi `metric_label`) rồi
  `forms.METRIC_CHOICES = list(AlertRule.METRIC_LABELS.items())`. Quy tắc chung: **bất kỳ dict/list
  "metric/field key → label hiển thị" nào tồn tại song song ở model (cho hiển thị) VÀ ở form (cho
  dropdown chọn) đều phải xuất phát từ 1 nguồn duy nhất — không có ngoại lệ "chỉ thêm khi cần",
  vì đúng thời điểm quên thêm chính là lúc bug này xảy ra.** Tiện thể cũng gộp
  `RAID_HEALTH_NAMES = {0:"OK",1:"Warning",2:"Critical"}` (từng lặp lại y hệt ở
  `AlertRule.threshold_label` VÀ `_fmt_metric()` trong `apps/alerts/engine.py::_fire_alert`) —
  cùng nguyên tắc, phát hiện cùng lúc: `_fmt_metric()` không có nhánh `raid_*` nên tin nhắn
  Telegram/email cho 4 metric RAID trước fix là dạng thô `"raid_controller_health = 2.00 (ngưỡng
  gte 2.00)"` (dùng `rule.metric` thay vì `rule.metric_label`) thay vì đọc được
  `"RAID Controller Health (iLO) = Critical (ngưỡng ≥ Warning)"`.

### Bẫy: poll lỗi mà không để lại dấu vết trên UI (iLO, 2026-10-01)
Task poll độc lập (`poll_all_ilo`) bỏ qua host lỗi mà không ghi gì → UI chỉ thấy "Chưa có dữ liệu"/dữ
liệu cũ, không biết 401 hay timeout. Mọi poll độc lập cần lưu lý do lỗi gần nhất (`Device.ilo_last_error`)
và UI không được hiện "OK" từ dữ liệu cũ khi poll gần nhất đã lỗi. Khi review "feature chưa hiển thị":
`git status` + `git diff` TRƯỚC, working tree có thể đã chứa bản làm dở.

### Bẫy SQL: tăng tuyến tính theo lịch sử (review ngoài, đo thật 2026-10-01)
Bảng time-series không có retention (`METRICS_AUTO_CLEANUP=False`) chỉ lớn lên → query "chạy nhanh
trên prod hôm nay" (2,5k dòng) có thể chậm dần. Luôn đo ở quy mô 10-40x bằng dữ liệu giả trong test DB
(`bulk_create` + `ANALYZE` + `EXPLAIN (ANALYZE)`) trước khi tin "ổn".
- **`filter(Q(timestamp__gte=since) | Q(pk=Subquery(...)))` = correlated subquery chạy cho MỌI dòng
  ngoài cửa sổ** (`latest_hardware` cũ: 25.920 dòng → 484 ms, 103.680 → 2,1 s; query cửa sổ riêng 17 ms
  không đổi). Tách: 1 query theo cửa sổ thời gian (index `device,timestamp`) + với host không có dòng
  nào trong cửa sổ mới `order_by("-timestamp","-pk").first()` riêng. `DISTINCT ON ... -pk` thì Postgres
  không đi index (đo 104k dòng: 151 ms) — đừng thay `.first()` bằng nó khi chỉ vài host.
- **`Q |= Q(...)` một nhánh cho mỗi cặp (entity, giờ) đã rollup** (`_delete_raw_for_rolled_hours` cũ):
  số cặp = TOÀN BỘ lịch sử hourly (prod: 1.033.679 cặp InterfaceStats cũ hơn 48h → SQL hàng trăm MB,
  ~3 triệu tham số; đo 6.720 cặp: SQL 1,2 MB, 78,6 s). Dùng `Exists(...)` đối chiếu bảng hourly, SQL
  cố định. Lần chạy đầu khi bật `METRICS_AUTO_CLEANUP` là ca xấu nhất — đo TRƯỚC khi bật cờ.
- **Join EXISTS: so ĐẲNG THỨC (hash/index được) chứ không so khoảng.** `h.hour <= s.ts AND h.hour >
  s.ts - 1h` chỉ hash theo entity rồi lọc từng dòng hourly (11,3 s / 201k dòng); `h.hour = đầu giờ(s.ts)`
  → Nested Loop + Memoize + Index Scan `(interface_id, hour)` (1,56 s). Xem plan prod bằng `EXPLAIN`
  (KHÔNG `ANALYZE`, không chạy DELETE).
- ⚠️ **`TruncHour`/`TruncDate` trong `filter()`/`annotate()` rồi so với cột timestamptz = lệch múi giờ,
  khớp 0 dòng, KHÔNG báo lỗi.** Django sinh `DATE_TRUNC('hour', ts AT TIME ZONE 'Asia/Ho_Chi_Minh')` →
  kiểu `timestamp` (không múi giờ), Python mới đổi lại aware khi đọc kết quả; trong WHERE nó bị so với
  timestamptz theo session UTC (lệch 7 giờ). Test cleanup cũ bắt được (`assert 0 == 1`). Dùng
  `timezone(tz, date_trunc('hour', timezone(tz, col)))` (`aggregation._local_hour_floor`), có test múi
  giờ lệch nửa giờ (`Asia/Kolkata`).
- Mọi test "đếm số query" (`django_assert_num_queries`) khi đổi cấu trúc query phải cập nhật CÓ CHỦ ĐÍCH
  kèm comment lý do, không nới số cho qua.

### Bẫy: iLO TLS — bật `ILO_CERT_VALIDATE=True` KHÔNG đủ (đo 3 iLO prod, 2026-10-01)
Collector gọi iLO bằng Basic Auth với `verify=False` mặc định. Cả 3 iLO dùng chứng chỉ HPE mặc định
(issuer "Default Issuer (Do not trust)", SAN chỉ có tên iLO, KHÔNG có IP; `https://<ip>` luôn lệch
hostname) → `verify=True` ra `SSLCertVerificationError` ở cả 3 host, poll iLO sẽ hỏng hoàn toàn.
Hai hướng xác thực thật: (a) ghim fingerprint SHA-256 từng iLO (không đổi gì trên iLO) — **đã chọn và code
xong (2026-10-01)**, hoặc (b) cài chứng chỉ do CA nội bộ ký (có SAN đúng) lên từng iLO + CA bundle vào
container (chưa làm).
- (a) gồm `Device.ilo_cert_sha256` + `_PinnedAdapter` (`apps/collectors/ilo_redfish.py`) + lệnh
  `pin_ilo_certs`. Sau deploy phải ghim thủ công từng host (chưa ghim = vẫn `verify=False`):
  `docker compose exec -T worker python manage.py pin_ilo_certs` (chạy thử, chỉ in) → đối chiếu fingerprint
  với nguồn ĐỘC LẬP (trình duyệt trên máy quản trị khác: mở `https://<ip iLO>` → xem chứng chỉ → SHA-256
  thumbprint; chưa kiểm chứng giao diện iLO tự hiển thị fingerprint) → thêm `--apply`. Giới hạn còn lại
  đã biết: host CHƯA ghim vẫn `verify=False` (rủi ro y như cũ cho tới khi ghim), và ghim lần đầu là TOFU. Cert iLO đổi hợp lệ (cấp lại cert, thay board) → poll báo "KHÔNG khớp
  fingerprint đã ghim" → ghim lại bằng `--device <id> --apply --replace`.
- urllib3 `assert_fingerprint` kiểm lúc bắt tay TLS, trước khi gửi request: cert lạ KHÔNG nhận được Basic
  Auth (test dùng máy chủ HTTPS local thật, `tests/collectors/test_ilo_cert_pin.py`). `requests.exceptions.
  SSLError` là con của `ConnectionError` → `_get` retry 1 lần rồi trả None; muốn phân biệt "mismatch pin" với
  "mất mạng" phải dựa cờ `_PinnedAdapter.fingerprint_mismatch` (không đổi chữ ký `_get`).
- Pin nhập sai định dạng phải FAIL CLOSED (dừng + báo lỗi), không lặng lẽ rơi về "không xác thực".
- ⚠️ **Cấu hình TLS đặt trên `HTTPAdapter` phải phủ CẢ đường proxy.** requests dựng `ProxyManager` riêng qua
  `proxy_manager_for()` (KHÔNG qua `init_poolmanager`), và mặc định đọc `HTTPS_PROXY` từ môi trường
  (`trust_env`). Chỉ truyền `assert_fingerprint` ở `init_poolmanager` thì sai pin qua proxy vẫn HTTP 200 và
  server nhận Basic Auth (review ngoài 2026-10-01, tái hiện bằng proxy CONNECT local). Nay: override cả
  `proxy_manager_for` + session đã ghim đặt `trust_env=False`. Test phải chạy THẬT qua proxy (đếm số
  CONNECT để chắc request có đi qua proxy), không chỉ kiểm tra cấu hình. Mọi kiểm tra bảo mật kiểu này nên
  thử cả đường trực tiếp lẫn đường proxy/biến môi trường.

### Bẫy: lỗi gửi thông báo "tạm thời" bị ghi `failed` vĩnh viễn (đo prod 2026-10-15)
`except Exception` → `status="failed"` biến mọi lỗi tạm thời (429 flood control, mất kết nối, task bị cắt)
thành mất thông báo thật: prod 309/1.843 dòng, 15/38 fire riêng ngày 2026-10-11. Quy tắc cho mọi kênh/outbox:
- **Channel phải phân loại lỗi** (tạm thời / vĩnh viễn / chưa cấu hình) bằng exception riêng; engine chỉ lên
  lịch lại cho loại tạm thời. `return` im lặng khi thiếu cấu hình là bẫy: engine ghi "sent" giả.
- **Lịch gửi lại phải nằm trong DB** (`next_attempt_at`) và số lần thử tăng NGUYÊN TỬ lúc claim — không dùng
  sleep/Celery retry trong bộ nhớ (worker restart là mất), và lần bị SIGKILL giữa chừng cũng phải tính.
- **429 thì tuân theo `parameters.retry_after`**; gửi lại hàng loạt không giãn cách chỉ tái hiện đúng đợt 429.
  Telegram: ≤1 tin/giây/chat, ≤20 tin/phút/nhóm (FAQ Bot API).
- **Cooldown phải DÙNG CHUNG giữa các tiến trình và các đường gửi, lưu trong DB** (`ChannelCooldown`, khoá theo
  bot/chat). Biến cục bộ của 1 vòng quét (`held_until`) không đủ: vòng sau 60s và lần gửi TRỰC TIẾP từ tác vụ
  poll (đường chính) vẫn đâm vào 429 và đốt lượt thử (review ngoài 2026-10-15, P2). Kiểm cooldown TRƯỚC khi
  claim để không tốn lượt thử; ghi cooldown kể cả khi dòng đã bị reclaim/hết lượt (429 là sự thật về kênh).
  Khoá băm token/URL, không lưu bí mật. Test bằng đột biến: bỏ kiểm tra ở từng đường → đúng test đỏ.
- **Phân loại lỗi SMTP bằng NGUỒN thư viện, không đoán theo tên lớp**: `SMTPRecipientsRefused` kế thừa
  `SMTPException` (không phải `SMTPResponseException`) nên nhánh "có `smtp_code`" bỏ sót nó; mã nằm trong
  `exc.recipients`. `SMTPException` ⊂ `OSError` nên phải xét `SMTPException` TRƯỚC `OSError`.
- **Không bao giờ lưu `str(requests.RequestException)`**: nó chứa nguyên URL (`/bot<token>/sendMessage`, URL
  webhook). Channel tự dựng thông điệp; chỗ lưu/log dùng `redact()`.
- **Ghi kết quả lần gửi phải khoá theo `claim_token`** (cả nhánh lỗi tạm thời, không chỉ nhánh sent/failed),
  nếu không chủ cũ gửi chậm sẽ hẹn lại đè kết quả của chủ mới.
- **`SoftTimeLimitExceeded` kế thừa `Exception`**: bắt riêng TRƯỚC `except Exception`, trả row về pending rồi
  raise lại. Test hồi quy bằng ĐỘT BIẾN (tạm phá từng hành vi, xác nhận đúng test đỏ), không chỉ chạy xanh.
- **Regex che bí mật: KHÔNG dùng `\b` trước chữ số và phải thử trên HÌNH DẠNG THẬT.** `\b\d{6,}:...` không khớp
  `/bot123456:AAH...` (giữa `t` và `1` không có ranh giới từ) nên `redact()` bản đầu rò token ở 121/294 dòng thật
  dù test (dùng URL đầy đủ) xanh. Cách làm đúng: (1) quét MỌI cột text/varchar/json bằng `DO $$ ... format('%I')`
  để biết bí mật nằm ở đâu; (2) liệt kê hình dạng đã che (`regexp_replace` rồi `GROUP BY`) và dùng đúng các hình
  dạng đó làm test; (3) chạy hàm che lên dữ liệu thật CHỈ ĐỌC, đếm dòng còn rò (không in nội dung).
- **Migration dọn dữ liệu nhạy cảm**: tự chứa regex (không import code ứng dụng — nghĩa phải cố định), `.update()`
  theo pk để không đổi `auto_now`/mốc dùng cho logic khác, reverse noop, idempotent, và thử khô trên dữ liệu thật
  trước deploy. Che phần bí mật, giữ phần còn lại để vẫn đọc được lỗi. Bản sao DB/backup đã tạo trước đó vẫn chứa
  bí mật → xoay khoá là quyết định riêng, phải nói rõ với người vận hành.
- Tình trạng thông báo cũ `failed`: đo xem alert tương ứng còn active không TRƯỚC khi quyết định phát lại —
  cả 309 dòng prod thuộc alert đã hồi phục nên không phát lại gì cả.

### Bẫy: vòng lặp theo phần tử (rule/thiết bị/host) không cô lập lỗi (1.8, 2026-10-15)
`for rule in rules: ...` không có `try` trong thân → 1 phần tử ném exception là cả vòng dừng, mọi phần tử sau bị
bỏ qua IM LẶNG, lặp lại mỗi chu kỳ (`check_device_alerts`: 1 getter lỗi che cả rule offline/RAID phía sau).
- Bọc `try` Ở TRONG vòng lặp, quanh từng phần tử (tách thân thành hàm riêng cho dễ test); log bằng
  `logger.exception` kèm định danh phần tử (tên rule, id, thiết bị) — không chỉ `str(exc)`.
- **`except SoftTimeLimitExceeded: raise` PHẢI đứng TRƯỚC `except Exception`** (nó kế thừa `Exception`); nếu không
  vòng lặp nuốt tín hiệu hết giờ và task chạy tới hard-kill. Test bảo vệ này xanh cả trên code cũ → chứng minh
  bằng đột biến (bỏ nhánh raise → đúng test đỏ), không tin test xanh.
- Test hồi quy: đặt rule hỏng ĐỨNG TRƯỚC rule lành theo `Meta.ordering` (cùng severity, tên `A-…` < `B-…`), phủ cả
  đường fire lẫn resolve, không chỉ getter. Dùng exception Python thuần — lỗi DB trong test (transaction bọc
  sẵn) làm hỏng cả transaction và các rule sau lỗi vì lý do khác.
- Công cụ cục bộ (Windows): `python -I` bỏ qua `PYTHONIOENCODING` → in tiếng Việt ra `cp1252` lỗi
  `UnicodeEncodeError`; dùng `python -X utf8 -I`. Script sửa file CRLF: đọc/ghi `newline=""` và chuẩn hoá `\n`
  rồi trả lại `\r\n`, nếu không diff bị nhiễu toàn file. Thêm `assert` đếm số chỗ sẽ đổi (đã bắt được 1 giả
  định sai về số `continue`) và `git diff -w --stat` để thấy diff logic thật sau khi dedent.
- Log container KHÔNG phải nguồn đo tần suất lịch sử: compose không xoay log, container bị tạo lại mỗi deploy
  (cửa sổ đọc được chỉ ~10 phút sau lần deploy). "0 lần trong log" ≠ "chưa từng xảy ra" — kiểm
  `docker compose logs --timestamps` xem cửa sổ phủ bao lâu rồi mới kết luận.

### Bẫy: "thiết bị trả lời" bị trộn với "hệ thống ghi được" (1.5, 2026-10-15)
Trạng thái online là bằng chứng về THIẾT BỊ (ICMP + dữ liệu thu thập hợp lệ). Lỗi của CHÍNH MÌNH (ghi DB/Redis,
chuỗi quá dài, nghẽn) không được suy ra thành "thiết bị chết".
- Mỗi bước có nguồn lỗi khác nhau phải có `try` riêng: thu thập → quyết định online; ghi → chỉ log. Log phải nói
  đúng bản chất ("ghi metrics thất bại"), không mượn nhãn "Poll collect failed".
- **Postgres từ chối chuỗi quá dài (`DataError`), không tự cắt**, và 1 giá trị hỏng làm hỏng cả `bulk_create`/
  transaction của vòng ghi. Dữ liệu do thiết bị/người vận hành đặt (ifAlias, tên VM) phải cắt theo `max_length`
  của model (`_fit(Model, "field", value)`, không gõ số cứng).
- ⚠️ **Cắt TRƯỚC khi tính khoá tra cứu.** Nếu khoá là `_iface_key(name)` mà chỉ cắt lúc tạo bản ghi, lần poll sau
  tên chưa cắt không khớp tên đã lưu → coi là "mới" → tạo lại → `IntegrityError` (`unique_together`) lặp mãi.
  Chuẩn hoá dữ liệu vào ngay đầu hàm ghi để mọi bước sau dùng chung 1 giá trị. Cắt cũng có thể tạo trùng khoá
  trong cùng lượt → loại trùng trước `bulk_create`.
- **Đo độ dài thực tế trước khi kết luận mức độ**: walk `ifAlias`/`ifDescr` trên worker (script qua stdin:
  `ssh monitorsrv "cd … && docker compose exec -T worker python manage.py shell" < script.py`, in độ dài chứ
  không in giá trị). Đo 2026-10-15: dài nhất 50/28 ký tự → lỗ hổng tiềm ẩn, ghi đúng như vậy.
- Kế hoạch/tài liệu cũ có thể sai về hiện trạng ("các hàm VM đã cắt sẵn" — thực tế `vm_name` không cắt): đọc code
  từng trường, không tin ghi chú.

### Bẫy: batch tự chạy nhiều host dựa vào `SoftTimeLimitExceeded` để dừng (1.7, 2026-10-15)
`poll_all_hyperv`/`poll_all_ilo` chạy inline N host trong 1 task. `SoftTimeLimitExceeded` kế thừa `Exception`
(chạy `SoftTimeLimitExceeded.__mro__` để chắc, đừng tin comment) và chỉ bắn MỘT lần: bị nuốt ở bất kỳ
`except Exception` nào (collect, inline alert eval, chính vòng lặp batch) là batch chạy tiếp tới hard-kill, mất cả
vòng. Nhánh `except SoftTimeLimitExceeded` bên ngoài vòng lặp hầu như không bao giờ chạy tới.
- **Dừng bằng đồng hồ, không bằng exception**: `deadline = bắt đầu + soft_limit − margin`, kiểm trước MỖI phần tử,
  quá hạn → log nêu TÊN phần tử bị bỏ qua + `break`. Kiểm bằng đồng hồ cũng bắt được ca soft limit đã bị nuốt (quay
  lại vòng lặp là thấy đã quá hạn). margin ≥ vài lần thời lượng 1 phần tử khoẻ — ĐO trên prod trước (hyperv 16–19s/host
  → 60s; iLO ~10s/host → 20s), không ước lượng.
- Vẫn đặt `except SoftTimeLimitExceeded: raise` TRƯỚC `except Exception` trong vòng lặp (lưới phía sau), và đừng
  ghi hết giờ của batch thành lỗi của phần tử (iLO từng ghi "Lỗi xử lý: SoftTimeLimitExceeded" lên UI).
- **Thứ tự phần tử quyết định ai bị cắt**: phần tử lần trước lỗi đứng CUỐI. Đừng `order_by("-last_ok_seen")` thuần:
  host bệnh vừa thành công 1 lần có mốc mới nhất → đứng đầu, treo, và bỏ đói các host khoẻ phía sau. Khoá dùng:
  `(failed_last_time, last_ok tăng dần, pk)` — host bị bỏ qua vòng trước (mốc cũ) được poll sớm ở vòng sau.
- Test với đồng hồ giả: patch hàm `_monotonic()` của module (không patch `time.monotonic` toàn cục — Celery/DB driver
  dùng nó), cho mỗi lần poll giả "tốn" N giây. Phải có test đối chứng "chạy bình thường vẫn poll đủ host" để không cắt
  nhầm, và test ca soft limit bị nuốt BÊN TRONG phần tử (poll trả về bình thường nhưng đồng hồ đã quá hạn).
- Số đo từ log phải kèm cửa sổ thời gian: lúc đo 2026-10-15 log worker chỉ phủ ~27 phút (từ lần recreate) và toàn
  khoẻ, nên số "lượt xấu" của kế hoạch (8 soft-limit, 6 hard-kill, 269,8s) không tái đo được — ghi là số của kế hoạch.

### Bẫy: helper SNMP "tiện" nuốt mọi lỗi → walk thất bại bị đọc thành "bảng rỗng" (1.9, 2026-10-15)
`snmp_walk_pairs` có `except Exception → []` (log DEBUG). `SoftTimeLimitExceeded` kế thừa `Exception` nên cũng
thành `[]`, và timeout thật cũng vậy. Nơi nào GHI hoặc SO SÁNH theo "có/không có" mà dùng nó thì "không hỏi được"
= "không có dữ liệu": ifDescr rỗng → tên cổng giả `port-N` → ghi thành `TopologyLink`; link không thấy → đếm trượt.
- **Chứng minh bằng thực nghiệm, không tin comment**: script cục bộ phát tín hiệu vào luồng chính giữa lúc
  `session.walk` (Windows không có SIGUSR1: `signal.signal(SIGINT, handler_ném_SoftTimeLimitExceeded)` +
  `threading.Timer(1.0, lambda: signal.raise_signal(signal.SIGINT))`, đích IP TEST-NET-1 `192.0.2.1` để request treo).
  Kết quả: helper trả `[]`, `session.walk` thô nổi lên exception.
- Quy tắc: nơi ghi dữ liệu dùng `snmp_walk_pairs_strict` — chỉ coi rỗng khi thiết bị TRẢ LỜI error-status
  (`SnmpDeviceError`) hoặc bảng thật sự không hàng; timeout/lỗi mạng/hết giờ task phải nổi lên. Giữ fallback bảng
  dự phòng (Q-BRIDGE → BRIDGE-MIB): chỉ ném khi KHÔNG bảng nào có dữ liệu mà có bảng lỗi. Các collector đang chạy ổn
  (`snmp_walk_pairs`) KHÔNG đổi — đổi helper toàn cục là đổi ngữ nghĩa poll mọi thiết bị.
- **Trước khi siết, đo 1 lượt chỉ-đọc trên worker** xem có walk nào ném lỗi trong vận hành bình thường không
  (`ssh monitorsrv "cd … && docker compose exec -T worker python manage.py shell" < probe.py`, `session.walk` thô từng
  OID, in `loại lỗi + thời gian`, KHÔNG in community). 2026-10-15: 17 switch × ~11 bảng = 0 lỗi, 67,5s.
- **Đo thời gian từng PHA từ mốc log trước khi chọn cách sửa**: lượt 149,8s hoá ra là walk lặp (FDB 2×, LLDP 2×),
  không phải 1 switch chậm — "deadline giữa các switch" thuần tuý sẽ cắt đói switch cuối mỗi lượt. Dùng chung kết quả
  walk bằng `ContextVar` trong `with walk_memo():` (không đổi chữ ký → test mock cũ nguyên), lưu dữ liệu THÔ chứ không
  lưu kết quả đã lọc theo tham số, lỗi không lưu.
- Pha cần dữ liệu toàn fleet (switch↔switch FDB) phải all-or-nothing khi hết giờ: chạy dở thì mọi link không thấy
  bị đếm trượt (3 lượt → stale). Test phải dựng link "sẽ bị đếm trượt nếu pha chạy" — link trùng đúng cạnh mà pha
  tìm lại được thì được làm mới, không bao giờ đỏ (đột biến lộ ra).
- **Loại phần tử lỗi khỏi bước suy luận trên TOÀN TẬP là tạo kết quả sai, không phải "bỏ qua cho an toàn"** (review
  ngoài 2026-10-15, chính tôi viết sai): cạnh trực tiếp = "không switch nào xen giữa" nên bỏ switch trung gian ra
  khỏi registry biến SW1–SW2–SW3 thành SW1–SW3. Lỗi 1 PHẦN (chỉ 1 cột LLDP timeout) khác lỗi TOÀN PHẦN: đừng gộp bằng
  "switch này lỗi → loại". Quy tắc: mỗi bước tự lấy dữ liệu của nó; suy luận toàn tập là ALL-OR-NOTHING — thiếu dữ liệu
  bất kỳ thành viên nào thì bỏ cả pha và giữ nguyên kết quả cũ (không ghi, không đánh stale). Đừng viết "tương đương
  bản cũ" khi chưa thử ca lỗi MỘT PHẦN; dựng kịch bản đó thành test trước.
- Đừng suy mốc tuyệt đối từ "tuổi": lúc đo tôi đã ghi nhầm 2 dòng `port-N` thuộc lượt 03:41 (tuổi 52 phút tại lúc
  probe lúc ~04:05 → 03:13). Lấy `last_seen`/`first_seen` tuyệt đối rồi mới đối chiếu với mốc log.
- Windows/Git Bash: `grep -c $'\r'` báo mọi file là CRLF (kể cả file LF) — kiểm bằng Python `read_bytes().count(b"\r\n")`.
  Repo TRỘN LF/CRLF (vd `snmp_client.py`/`tasks.py` CRLF, `topology_*.py` LF) → script vá phải tự nhận và giữ nguyên.

### Bẫy: giải mã "mềm" trả lại ciphertext → credential sai được dùng và bị ghi đè (1.6, 2026-10-15)
`decrypt_value` từng `except Exception: return ciphertext` (ý định: "plain-text cũ chưa migrate"). Chuỗi `gAAAA…` đi
làm mật khẩu WinRM/iLO/SSH (mỗi lần poll = 1 lần đăng nhập sai → khoá tài khoản), và `get_prep_value` mã hoá lại
chính giá trị vừa đọc nên `save()` đầy đủ ghi `encrypt(token)` đè token gốc.
- **Phân biệt "chưa mã hoá" với "mã hoá nhưng không giải mã được" bằng CẤU TRÚC, không bằng "giải mã có được
  không"**: `is_encrypted` (= giải mã được bằng khoá hiện tại) coi token của khoá khác là plain-text → lệnh migrate
  mã hoá chồng. Cấu trúc Fernet: base64 url-safe, byte đầu `0x80`, ≥73 byte (không dựa tiền tố `gAAAAA`, chỉ đúng khi
  timestamp còn trong 32 bit thấp). Chuỗi không có cấu trúc token VÀ không giống token hỏng → plain-text cũ, trả
  nguyên.
- **"Không đạt kiểm tra cấu trúc" ≠ "là plain-text" (review ngoài P2 sau 1.6)**: token mất `=` cuối (hoặc cắt
  cụt, lẫn khoảng trắng) không có cấu trúc hợp lệ nhưng cũng không phải mật khẩu — bản đầu coi nó là plain-text
  nên collector đăng nhập bằng `gAAAAA…` và `save()`/`encrypt_credentials` mã hoá chồng. Token của plaintext
  ≤15 byte kết thúc `==`, 16–31 byte `=`, nên mất padding là kịch bản thật khi ai đó cắt/dán/chuẩn hoá chuỗi.
  Ba trạng thái: token hợp lệ (cấu trúc) / token hỏng (`looks_damaged_token`: phần đầu `gAAAAA` sau `strip`) /
  plain-text. Token hỏng đi cùng đường `UnreadableCredential` với khoá sai. Nhầm chiều an toàn: plain-text cũ
  bắt đầu bằng `gAAAAA` bị từ chối chứ không gửi rác làm mật khẩu. Quy tắc chung: kiểm tra "đây là X?" trả
  False thì hỏi tiếp "có dấu hiệu là X hỏng không?" trước khi coi là Y; test phải dùng token thật cắt/bỏ `=`,
  không chỉ chuỗi tự bịa.
- **Lỗi ở `from_db_value` KHÔNG được ném**: 1 dòng hỏng làm `Device.objects.all()` ném → sập dashboard, mọi
  `poll_all_*`, `/health/`. Trả sentinel (`UnreadableCredential`: chuỗi RỖNG falsy mang `raw` + `reason`) rồi
  chặn ở nơi DÙNG. Bắt buộc đi kèm 3 việc, thiếu 1 là mất dữ liệu: `get_prep_value` ghi ngược `raw` nguyên vẹn;
  `save_form_data` không để ô trống xoá nó; lệnh bảo trì dùng kiểm tra cấu trúc.
- **Đặt guard ở nơi DÙNG, và đọc nơi TẠO đối tượng xem có nằm trong `try` không**: `CollectorFactory.create` chạy
  ngoài `try` của `_poll_device_once`; guard ở constructor sẽ sập cả lượt poll bằng traceback thay vì "Poll collect
  failed" → Offline kèm lý do. Lưu lỗi lại (`_credential_error`), ném ở `_session`.
- **Giá trị rỗng giả**: sentinel falsy hợp với `if not password`, nhưng các đoạn `(x or "").strip()` / "thiếu
  mật khẩu → hạ cấp no_auth" biến nó thành "chưa đặt" và báo sai/hạ cấp thầm. Kiểm `ensure_credentials_readable`
  TRƯỚC mọi validation kiểu "thiếu mật khẩu".
- **Đo trạng thái cột mã hoá trước khi sửa, chỉ in số đếm**: script đọc cột THÔ (`SELECT … FROM devices_device`),
  phân loại `empty / token_ok / token_FAILS / plain` rồi in đếm + id thiết bị, KHÔNG in giá trị (mẫu:
  `scratchpad/crypto_state_probe.py`, chạy qua `docker compose exec -T worker python manage.py shell < script`).
  2026-10-15: 23 thiết bị, mọi token giải mã được, 0 plain, 0 hỏng → lỗ hổng tiềm ẩn, ghi đúng như vậy.
- **Test "không rò" phải đi qua dữ liệu thật**: đột biến lộ test chỉ kiểm sentinel dựng tay. Nạp từ DB, rồi kiểm
  thông điệp lỗi, `reason` và `caplog.text` không chứa token đọc thẳng từ cột.
- Script đột biến/vá viết `\n` trong heredoc Python bị biến thành xuống dòng thật (đã dính 3 lần): sửa trực tiếp
  file script bằng Edit thay vì sinh nó qua heredoc.

### Bẫy: cơ chế "chặn/giảm thông báo" không có điều kiện thoát → chặn vĩnh viễn sự cố thật (1.1, 2026-10-15)
Flapping-suppress (`_fire_alert`) tạo Alert nhưng không tạo thông báo; vòng sau thấy alert active nên im lặng, và vì fire chưa từng gửi nên `_resolve_alert` cũng không gửi RECOVERED. Prod: alert #662 offline 19,7 ngày, 0 thông báo. Quy tắc chung: mọi cơ chế "không gửi" (flapping, dedup, cooldown, silence) phải trả lời được "khi nào thì ngừng chặn?" — alert còn active mà không có thông báo nào đã gửi/đang chờ là lỗ hổng.
- **Đo trước**: đếm alert bị chặn bằng SQL self-join (≥N alert cùng device+rule trong cửa sổ tính đến nó); khi METRICS_WRITE_MODE=cache không có raw, kiểm "ca dài có thật không" bằng bảng `*Hourly` (`metrics_systemhealthhourly`: thiếu dòng = không có poll thành công) trước khi gọi là sự cố thật.
- **Trần không cần cột mới**: tiêu chí "chưa có dòng fire nào" tự khoá lại sau khi ghi pending (đúng 1 lần/alert). Kiểm "chưa có" và ghi pending PHẢI cùng dưới khoá Device — kiểm riêng bên ngoài rồi ghi bên trong là nhắc đôi khi 2 đường eval chạy đồng thời.
- **Đi qua outbox, không gửi thẳng**: ghi `pending` cùng transaction rồi dispatch sau commit — nhờ vậy crash giữa chừng vẫn được sweep nhặt và late-recovery sẵn có lo thứ tự ALERT → RECOVERED.
- **Phân biệt với 1.2**: alert có dòng fire nhưng tất cả `failed` KHÔNG thuộc nhóm này; nhắc lại ở đó chỉ tái hiện vòng lặp gửi thất bại. Retention tương lai không được xoá dòng thông báo của alert còn active.
- **Mẹo test**: `triggered_at` là `auto_now_add` nên không đặt được lúc create → lùi bằng `Alert.objects.update(triggered_at=F("triggered_at") - timedelta(...))` (giữ khoảng cách tương đối giữa các lần fire); tên rule trùng rule migration seed ("Thiết bị offline") ném IntegrityError và rule seed tự gửi vào mock — dùng tên khác + `AlertRule.objects.exclude(pk=...).delete()` khi cần cô lập; recovery trễ do `_finalize_fire_sent` queue cần sweep lần 2 mới gửi. Test song song: rào chắn đặt ĐẦU hàm kiểm tra + timeout (có khoá thì bên kia chờ ở khoá nên rào hết giờ rồi chạy tuần tự); đột biến bỏ khoá phải làm test đỏ — nếu không, test không đo gì.

### Bẫy: bộ đếm cộng dồn vào cột số nguyên hẹp không có trần (`miss_count`, 2026-10-15)
`PositiveSmallIntegerField` = smallint, tối đa 32767; `save()` không kiểm Python-side nên tràn chỉ lộ ra lúc ghi (`DataError: smallint out of range`) và thường bị chặn theo từng thiết bị → lỗi âm thầm, chỉ thấy là 1 phần dữ liệu không cập nhật. Quy tắc: mọi cột bộ đếm được `+= 1` định kỳ phải (a) có trần hoặc dừng đếm khi đã quyết định xong (stale), (b) không ghi lại khi không đổi, và (c) logic đếm nằm ở 1 hàm — ở đây từng chép nguyên 3 chỗ nên sửa 1 chỗ là bỏ sót 2 chỗ (đột biến "vòng i quay lại mã cũ" kiểm điều đó). Đo trước: `max(cột)` + tốc độ tăng (số đếm / ngày kể từ lần cuối thấy) để biết còn bao lâu, đừng gọi là sự cố khi mới tiềm ẩn.
- **Mẹo test**: dựng 1 "vòng" bằng dữ liệu mạng RỖNG (monkeypatch hàm lấy dữ liệu về `[]`/`{}`) để mọi link đều "không còn thấy"; parametrize theo từng vòng để test chạy đúng 3 hàm; đếm UPDATE bằng `CaptureQueriesContext` thay vì suy ra từ giá trị.

### Bẫy: thao tác "tắt/xoá" 1 thực thể mà engine bỏ qua thực thể đã tắt, hoặc CASCADE xoá lịch sử mà UI nói ngược (1.4, 2026-10-15)
Rule bị tắt thì `check_device_alerts` không xét nữa, nên alert đang active của nó không bao giờ qua `_resolve_alert` → kẹt vĩnh viễn; xoá rule thì `CASCADE` xoá Alert + AlertNotification trong khi trang xác nhận ghi "vẫn còn trong lịch sử". Quy tắc chung: (a) mọi trạng thái "tắt/ẩn/lưu trữ" phải trả lời được "các bản ghi đang ở trạng thái active của nó thì sao?" — engine chỉ xét thực thể bật nghĩa là việc đóng chúng phải làm lúc tắt; (b) cùng trạng thái cuối thì cùng hệ quả cho mọi đường vào (nút Tắt, bỏ tick ở form sửa, admin) — dùng 1 hàm; (c) chữ trên màn hình huỷ-không-hoàn-tác phải sinh từ `on_delete` thật + đếm bằng chính dữ liệu sẽ bị xoá; (d) vòng eval nạp thực thể trước khi người dùng tắt nó → chốt "còn bật không" lại DƯỚI khoá ngay trước khi tạo bản ghi mới; (e) **bên đóng phải đi qua CÙNG khoá với bên tạo, trên tập thực thể mà bên tạo CÓ THỂ chạm tới — không phải tập đã thấy trong DB**: danh sách "Device đã có alert active" bỏ sót đúng alert đang được tạo (chưa commit), nên đóng 0 alert rồi worker tạo tiếp (review sau 30b09aa; tái hiện bằng 2 connection Postgres). **Tập khoá cũng KHÔNG được suy từ thuộc tính phạm vi của chính thực thể đang bị tắt** (`rule.device_type`): thuộc tính đó có thể bị đổi trong cùng lần lưu, nên Device mà worker đang xử lý theo phạm vi CŨ nằm ngoài tập khoá — vá lần hai vẫn thủng (review lần ba). Khoá TOÀN BỘ tập cha (mọi Device, cỡ vài chục dòng, mỗi cái transaction riêng) thay vì thu hẹp; (f) **`is_active=False` ≠ đã hồi phục**: mọi cơ chế suy ra RECOVERED từ trạng thái đóng (late-recovery) phải đọc LÝ DO đóng của từng bản ghi (`Alert.closed_reason`), không suy từ trạng thái của thực thể cha (`rule.enabled`) — bản ghi đã hồi phục thật trước đó vẫn phải có recovery và bật lại rule không được làm bản ghi đã đóng sống lại; (g) **hook của Django admin chạy TRONG `atomic()` của admin**: tác dụng phụ cần dữ liệu đã commit (đóng alert, gửi tin…) → `transaction.on_commit`; test bằng kết nối thứ hai (thread) đọc lại dữ liệu và `django_capture_on_commit_callbacks(execute=True)` cho test thường.
- **Xác nhận xoá tường minh**: gõ lại tên, kiểm Ở SERVER (JS chỉ tiện ích). POST rỗng / Enter / sai chữ hoa-thường / tên của rule khác đều không xoá; hành động chính (tắt) đứng trước, xoá nằm trong `<details>`.
- **Đo trước**: `LEFT JOIN alerts_alert / alerts_alertnotification GROUP BY rule` để biết xoá từng rule mất bao nhiêu (prod: 765 + 1.384 cho "Thiết bị offline"); và kiểm "rule đang tắt có alert active không" trước khi gọi là sự cố đã xảy ra (prod: 0 rule tắt).
- **Test đua với 2 connection thật**: `@pytest.mark.django_db(transaction=True)` + 2 thread; chặn thread tạo ĐÚNG sau bước kiểm và trước `create` (patch `Alert.objects.create` bằng `threading.Event`), chạy thao tác tắt ở thread kia, `join(timeout=1.5)` để mã đúng kịp bị chặn ở khoá, rồi mới nhả. Mã sai: thao tác tắt xong ngay. Phải `connection.close()` cuối mỗi thread.
- **Mẹo test**: `ModelForm.is_valid()` ghi dữ liệu đã làm sạch lên chính instance → đọc giá trị cũ (`was_enabled`) TRƯỚC khi bind form; `Client(enforce_csrf_checks=True)` mới kiểm được CSRF (client mặc định bỏ qua); test "số hiển thị == số thật bị xoá" bằng cách chụp `response.context` rồi đếm trước/sau; test drift `Model._meta.related_objects` để ai thêm quan hệ CASCADE mới phải sửa trang.

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
