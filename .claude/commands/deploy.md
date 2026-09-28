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
