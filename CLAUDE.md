# Monitor System — CLAUDE.md

## ⓿ Nguyên tắc làm việc (BẮT BUỘC — đọc trước mọi việc)
> Rút ra từ thực chiến trên fleet + prod. Vi phạm là lặp lại lỗi cũ. Chi tiết trong `/deploy` §0.
1. **KHÔNG suy luận linh tinh, KHÔNG đoán mò.** Mọi kết luận (OID, root cause, mapping) phải có bằng chứng. Chưa chứng minh được thì nói "chưa chắc" + đi verify, KHÔNG viết vào code/doc như sự thật.
2. **Test bằng KẾT QUẢ THẬT trước khi thay đổi.** Đo/probe trên thiết bị/DB/shell thật (vd walk OID trên `docker compose exec worker`) → xác nhận số liệu → rồi mới sửa code. Sửa xong verify live lại (§4 /deploy).
3. **Đọc KỸ tài liệu hãng/OS của từng thiết bị** (Cisco IOS/IOS-XE/Business-CISCOSB, Huawei VRP/YunShan, HyperV/WinRM) — MIB/enum/OID khác nhau theo firmware. Không đồng nhất "Cisco" hay "Huawei" là một. Tài liệu chung mâu thuẫn thiết bị thật → tin thiết bị thật (đã verify), ghi lại điểm lệch.
4. **Đổi code = đọc `/deploy` skill TRƯỚC, học được gì mới thì UPDATE NGAY skill + CLAUDE.md + memory.** Skill là nguồn sự thật sống; giữ nó đúng hiện trạng cho lần sau.

## Mục tiêu
Giám sát hạ tầng mạng + ảo hoá: **Switch** Cisco (IOS/IOS-XE) & Huawei (VRP — S5700/S6700/S9300); **HyperV** (VM health, host resources, replication, snapshot).

## Tech Stack
Django 5.x + Bootstrap 5 · PostgreSQL (prod) / SQLite (dev) · Celery + Redis + django-celery-beat · Netmiko (cisco_ios/huawei_vrp) · pysnmp + easysnmp · pywinrm + PowerShell · Chart.js (AJAX) · **Realtime: SSE (async Django view) qua ASGI/uvicorn + Redis pub/sub** · Alert: Email SMTP + Telegram.

## Luồng dữ liệu
```
Celery Beat (120s) → CollectorFactory → SNMP/SSH/WinRM
  → Adapter (normalize theo vendor) → MetricWriter → DB
  → evaluate_alert_rules → Email/Telegram
  → Django Views + Chart.js → Dashboard
  → publish_device_event → Redis pub/sub → SSE async view → EventSource (cập nhật realtime, không reload)
```

## Cấu trúc Apps
```
apps/
├── devices/      # Device, Interface CRUD + test connection
├── collectors/   # SNMP/SSH/WinRM collector + adapter Cisco/Huawei + tasks
├── metrics/      # InterfaceStats, SystemHealth, VMStats + writer + Chart.js API
├── alerts/       # AlertRule CRUD + engine + dedup + Email/Telegram
├── dashboard/    # index, switch/hyperv/wlan/firewall_detail
├── accounts/     # RBAC 2 cấp (Admin/Review) + UI quản lý user (không có model)
└── realtime/     # SSE push: publisher (Redis pub/sub) + async stream view (không có model)
```

## Nguyên tắc & convention
- `vendor` (cisco/huawei) trong Device; `os_family` tự detect khi poll đầu qua sysObjectID + sysDescr.
- OID profiles: `oids/{cisco_ios,cisco_iosxe,huawei_vrp}.yaml`. Interface metrics dùng MIB-II chuẩn, không phụ thuộc vendor/model.
- Adapter pattern: `collect_raw()` → `normalize()` → `MetricWriter.save_metrics()`.
- Timestamps UTC (`USE_TZ=True`, display `Asia/Ho_Chi_Minh`). Credentials trong `Device.ssh_password`/`snmp_community`.
- **Không hard-code** IP/password/community. Type hints bắt buộc cho collector/adapter.
- Log: `logger.info("Device %s: CPU %.1f%%", device.name, value)`.
- ⚠️ **Số Django nhúng vào JS phải `{{ x|unlocalize }}`** (`{% load l10n %}`). Locale `vi` đổi dấu thập phân thành **phẩy** → `var x = 1782380079,836022;` là **SyntaxError làm chết CẢ `<script>` inline** (nút, poller, SSE, reload đều ngừng → dashboard treo). Test phía server KHÔNG bắt được — chỉ trình duyệt parse JS. Đã áp dụng cho `poll_fresh`, `device.pk`.

## OID đã xác minh runtime (fleet thật 16 thiết bị, 2026-06)
> Ghi lại để không lặp lỗi gán nhầm OID.

**Huawei VRP / YunShan** — `hwEntityResourceTable` `1.3.6.1.4.1.2011.5.25.31.1.1.1.1.X`:
- `.5` = **hwEntityCpuUsage** (CPU% ✅) · `.6` = CpuUsageThreshold (NGƯỠNG 90/95, ❌ không phải CPU) · `.7` = **hwEntityMemUsage** (Mem% ✅).
- ⚠️ **Từng gán nhầm CPU→`.6`** (mọi switch báo CPU 90-95% giả) và Mem→`.5`. Đã fix.
- Scalar `.0` thường trống → walk table, lấy entity "MPU Board"/mainboard (giá trị > 0).
- Dùng chung cho VRP V5 (S5735 V200R021), YunShan (CloudEngine S5735-L-V2 V600R023/024), **và firewall USG6525E** (VRP V600R007C20SPC600, entity MPU 67108873) — collector `huawei_vrp` chạy nguyên.
- ⚠️ **USG từ chối PTY** (chỉ exec-channel) → netmiko `huawei_vrp` fail "Channel closed" → firewall phải poll **SNMP** (hoặc exec-channel paramiko gửi `system-view\n…\nquit` trong 1 phiên).

**Cisco**:
- IOS classic (C2960X): CPU `1.3.6.1.4.1.9.2.1.58.0` (OLD-CISCO-CPU 5min), Mem pool `.1`.
- Business/SMB (Catalyst 1200/1300, CBS250/350): CPU `rlCpuUtil 1.3.6.1.4.1.9.6.1.101.1.9.0`. **Mem KHÔNG expose SNMP → mem=0** (giới hạn HW, không phải bug).
- IOS-XE — ⚠️ **CHƯA kiểm chứng** (không có thiết bị): CPU/mem hard-code index `.1`; cần walk/verify khi có thiết bị thật (index khác trên stack/multi-RP).

**Interface (mọi vendor)** — MIB-II, dùng 64-bit HC counters `ifHCInOctets/Out` = `.31.1.1.1.6/.10`.

**Access VLAN / PVID per port** (`Interface.access_vlan`, collector `_collect_access_vlans`, OID trong `oids/*.yaml` `vlan:`):
- **Cisco IOS/IOS-XE**: `vmVlan` CISCO-VLAN-MEMBERSHIP-MIB `1.3.6.1.4.1.9.9.68.1.2.2.1.2`, **index = ifIndex trực tiếp**. Chỉ access port có entry → trunk/uplink trống (đúng ý, UI hiện badge "Trunk"). (Cisco Business dùng CISCOSB — xem bên dưới.)
- **Huawei + fallback chuẩn**: `dot1qPvid` Q-BRIDGE-MIB `1.3.6.1.2.1.17.7.1.4.5.1.1` **index = dot1dBasePort** → phải map qua `dot1dBasePortIfIndex` `1.3.6.1.2.1.17.1.4.1.2`.
- ✅ **Cisco Business (CBS250/350, Catalyst 1200/1300)**: MIB riêng **CISCOSB** `vlanAccessPortModeVlanId` `1.3.6.1.4.1.9.6.1.101.48.62.1.1` (**index = ifIndex trực tiếp**). ⚠️ Q-BRIDGE trên CBS vô dụng: `dot1qPvid` trả **1 cho mọi cổng** (kể cả access VLAN thật ≠1) → KHÔNG tin `dot1qPvid` cho CBS. Verify runtime 2026-07-02 (CBS250 gi1=VLAN5, Catalyst1200 VLAN8/10). Nhánh này ưu tiên trước vmVlan/dot1qPvid trong `_collect_access_vlans`.
- Chỉ lấy **access VLAN (1 số/port)**, KHÔNG lấy allowed-list trên trunk (phạm vi cố ý). UI: cột VLAN ở `switch_detail`.
- ✅ **Verify runtime fleet thật 2026-06-26** (Huawei CORE 10.0.193.1): vmVlan/dot1qPvid trả đúng. Nếu Huawei/Business trống bất thường → mở SNMP view nhánh `1.3.6.1.2.1.17` (Q-BRIDGE).

**Trunk/Access mode per port** (`Interface.port_mode` ∈ access/trunk/hybrid, collector `_collect_port_modes`, từ 2026-06-26):
- Đọc **mode switchport THẬT** thay vì đoán theo tên/tốc độ, **2 nguồn theo hãng**:
  - **Cisco IOS/IOS-XE** (ưu tiên): CISCO-VTP-MIB `vlanTrunkPortDynamicStatus` `1.3.6.1.4.1.9.9.46.1.6.1.1.14`, **index = ifIndex trực tiếp**, trunking(1)⇒trunk / notTrunking(2)⇒access. ✅ verify IOS classic 2026-06-26 (28 cổng). Cisco KHÔNG expose Q-BRIDGE static table chuẩn nên phải đi đường này.
  - **Huawei + chuẩn** (fallback khi VTP rỗng): Q-BRIDGE `dot1qVlanStaticTable`. Mỗi VLAN có 2 PortList bitmap (index=VLAN id): `dot1qVlanStaticEgressPorts` `1.3.6.1.2.1.17.7.1.4.3.1.2` + `dot1qVlanStaticUntaggedPorts` `…1.4` → `tagged = egress \ untagged`. Gom theo `dot1dBasePort`: tagged≥1 ⇒ **trunk**, untagged đúng 1 ⇒ **access**, untagged≥2 ⇒ **hybrid**; map qua `dot1dBasePortIfIndex`.
  - ✅ **Cisco Business (CBS250/350, Catalyst 1200/1300)** (từ 2026-07-02): MIB riêng **CISCOSB** `vlanPortModeState` `1.3.6.1.4.1.9.6.1.101.48.22.1.1` (**index = ifIndex trực tiếp**): **11⇒access, 12⇒trunk** (giá trị khác general/customer → để rỗng, rơi heuristic). ⚠️ CBS KHÔNG expose CISCO-VTP-MIB, và Q-BRIDGE `dot1qVlanStaticEgress` trả bitmap **TOÀN 0x00** (verify 2026-07-02 CBS250/Catalyst1200) → phải đọc CISCOSB. Enum 11/12 KHÔNG khớp tài liệu chung general(1)/access(2)/trunk(3) — firmware này offset riêng, chỉ tin số đã verify (gi9 trunk-link=12). Nhánh này ưu tiên trước VTP/Q-BRIDGE trong `_collect_port_modes`.
- ⚠️ **TÁCH** khỏi `is_uplink`: `port_mode` = mode switchport (điều khiển cột LOẠI/VLAN ở UI); `is_uplink` = vai trò topology cho **cảnh báo băng thông** (`uplink_*_mbps`). Writer: `port_mode==trunk` ⇒ ép `is_uplink=True`; `==access` ⇒ chặn heuristic tên/speed. Lý do đổi: heuristic cũ gán cổng 1G nối switch khác (PFVN-SW03) nhầm "Access VLAN 1", ép mọi XGE thành Trunk.
- `_parse_portlist` xử lý OCTET STRING (bytes / "0x80.." / "80 00.." / latin-1). ⚠️ easysnmp có thể cắt tại null byte — prod đang chạy **pysnmp** nên OK; `--raw` in giá trị thô để soi.
- Cổng KHÔNG có entry Q-BRIDGE (routed/L3, Vlanif, member Eth-Trunk) → `port_mode` rỗng → fallback `is_uplink` + access_vlan. UI vẫn hiện VLAN N qua `dot1qPvid`.
- ✅ **Verify 2026-06-26**: Huawei CORE (Eth-Trunk→trunk, Gi0/0/31→access VLAN3, Gi0/0/32→access VLAN10); Cisco IOS classic id5 (VTP 28 cổng). Tool: `python manage.py verify_vlan_oids <device_id> [--raw]` — in cả VTP (Cisco) lẫn Q-BRIDGE (Huawei) để đối chiếu `show interfaces switchport` / `display port vlan`.
- ⚠️ Population **eventually-consistent**: walk bảng VLAN chập chờn khi nhiều thiết bị poll đồng thời → 1 vài Huawei có thể tạm rỗng port_mode; preserve-on-empty giữ giá trị cũ → **tự lành** ở poll thành công kế tiếp (không kẹt).

**Huawei WLAN/AC — AC6508** (`device_type=wlan_controller`, HUAWEI-WLAN MIB `…2011.6.139`, OID đầy đủ trong `oids/huawei_vrp.yaml` `wlan:`):
- Bảng AP `hwWlanApInfoTable` `…6.139.13.3.3.1.X` (index=MAC AP): `.4` name · `.5` group · `.6` run_state (`8`=online) · `.44` = **client đang kết nối/AP** (cả 2 band, ✅).
- ⚠️ **client/AP đúng là `.44`, KHÔNG phải `.41`** (`.41`≈số khác; `.17/.33/.34` bất biến = config). Cách dò: poll 2 lần lọc cột dao động + đối chiếu Total Web UI (`/research-oids`).
- Bảng STA chi tiết **KHÔNG expose** SNMP → chỉ lấy được **số lượng** client/AP, không liệt kê từng client/MAC. Lệch nhẹ vs Web UI từng thời điểm là bình thường.
- Tool dò: `python manage.py verify_wlan_oids <device_id> --parent <oid>`.

**Topology — map AP vào switch (`apps/collectors/topology_*`, `apps/dashboard/topology_api.py`)**:
- Badge "(n AP)" trên node switch = số `TopologyLink(link_kind='ap', is_stale=False)` của switch đó.
- Ưu tiên LLDP; switch **không expose LLDP** (cisco_business, một số cisco_ios) → fallback **FDB** (dò MAC bảng forwarding khớp danh sách AP từ AC). FDB không phân biệt "AP cắm trực tiếp" vs "MAC học vọng qua uplink" → lọc bằng `is_uplink_port` (port_mode trunk/hybrid, `FDB_UPLINK_TOTAL_MAC_THRESHOLD=25` tổng MAC, `AP_MAC_FLOOD_THRESHOLD=3`).
- ⚠️ **AP link unique theo `(local_device, local_port)`** (update_or_create) → "last-MAC-wins"/cổng. Đường FDB phải chỉ trả entry **đã khớp AP**, nếu trả mọi MAC sẽ đẻ AP ma 1/cổng (đã từng: `filter_fdb_ap_entries` trả `entries` thay vì `[]` khi không match → ma trên uplink Gi9 + `port-0`, MAC đổi mỗi vòng, fix 2026-06-29 commit 6409b73).
- **Soi AP ma:** AP link `is_stale=False` có MAC **không** thuộc snapshot AC (`load_ac_ap_snapshot`) = giả. Lệnh: `diagnose_ap_mapping`. Link sẽ tự `is_stale` sau `STALE_MISS_THRESHOLD=3` vòng miss, nhưng nếu gốc còn đẻ thì phải fix collector chứ xoá vô ích.

## RBAC — 2 cấp (app `apps.accounts`, không có model riêng)
- **Admin** = group `Network Admins` (hoặc superuser): full + quản lý user. **Review** = `Read-Only Operators`: chỉ xem, write → 403.
- Nguồn sự thật: [apps/accounts/roles.py](apps/accounts/roles.py) (`is_admin/get_role/set_role`) — dùng chung với `_can_write` (devices/alerts) và `IsAdminOrReadOnly` (DRF).
- UI: `/users/` (admin-only), đổi mật khẩu `/users/password/`. Group tạo sẵn ở migration `devices/0007_create_rbac_groups`.

## Online/offline — poll + dashboard đếm
> Nguồn sự thật cho badge, thẻ on/off, card Offline: kết quả poll trong worker; dashboard chỉ hiển thị qua SSE + `alerts_summary`.

**Xác định online khi poll** ([apps/collectors/tasks.py](apps/collectors/tasks.py) `_poll_device_once`):
- Thiết bị mạng SNMP/SSH (switch/router/firewall/nas): **ICMP AND SNMP-thật** (`ONLINE_REQUIRE_ICMP=True`). ICMP fail → bỏ qua SNMP, `last_seen=None`, SSE `online=false`.
- HyperV / WLAN AC / ping-only: không bắt buộc ICMP; online = collect thành công + dữ liệu hợp lệ (`_has_valid_data`).
- **Đồng bộ `last_seen`**: poll `online=True` → ghi `last_seen=now()`; `online=False` → **`last_seen=None`** (cả SNMP rỗng/exception, không chỉ ICMP). Tránh lệch: SSE badge **Off** nhưng thẻ đếm vẫn `N on` do grace `is_online`.
- ⚠️ **TÁCH hiển thị vs cảnh báo — 2 mốc thời gian:**
  - `last_seen` (**hiển thị**): bị xoá mỗi lần poll trượt → badge/thẻ đếm Off **tức thì**. `Device.is_online` dựa mốc này.
  - `last_ok_seen` (**cảnh báo**): chỉ ghi khi poll THÀNH CÔNG, **KHÔNG bao giờ bị xoá** khi poll lỗi tạm. `Device.is_online_for_alert` dựa mốc này + grace `max(collect_interval×3, DEVICE_ONLINE_MIN_GRACE_SECS=300)` (dự phòng `created_at` cho thiết bị vừa thêm).
  - **Vì sao**: trước đây xoá `last_seen` làm `is_online`=False **ngay** (grace bị bỏ qua khi `last_seen=None`) → 1 vòng poll trượt (ICMP rớt gói/SNMP chậm/walk rỗng) đủ bắn alert `device_online` **Offline giả** rồi Recovered → **spam Telegram flapping**. Nay alert offline ([_device_online](apps/alerts/engine.py), [_sustained_device_online](apps/alerts/engine.py)) dùng `is_online_for_alert` → chỉ báo khi mất tín hiệu THẬT vượt grace; dashboard vẫn Off tức thì.
- `Device.is_online` ([apps/devices/models.py](apps/devices/models.py)): property từ `last_seen` + grace `max(collect_interval×3, DEVICE_ONLINE_MIN_GRACE_SECS=300)`. Dùng trong `_dashboard_counts()`, render index. **Cảnh báo offline KHÔNG dùng property này** (dùng `is_online_for_alert`).
- **Chống spam khác** ([apps/alerts/engine.py](apps/alerts/engine.py)): (1) `_resolve_alert` chỉ gửi ✅ RECOVERED nếu fire đã từng có `AlertNotification` status `sent` → fire bị flapping-suppress thì resolve im lặng (không dội recovery). (2) `mem_percent==0` coi là sentinel "không đo được" (Cisco Business/SMB không expose mem) → `_latest_mem`/`_sustained_cpu_mem` bỏ qua → rule `lt/lte` mem không fire giả.

**Dashboard index — hiển thị on/off** ([templates/dashboard/index.html](templates/dashboard/index.html)):
- Stat-card mỗi loại: `total` + `X on` + `· Y off` (chỉ hiện `off` khi Y>0).
- Card **Offline** tổng: device offline + AP offline (từ `WifiApStats`).
- Khối **Thiết bị đang Offline**: danh sách tên/IP (partial `_offline_notice.html`).
- Cập nhật realtime: SSE → badge hàng **ngay**; mỗi SSE event → `dashRefreshAlerts()` debounce **1.5s** → `alerts_summary` cập nhật thẻ on/off + Offline; backup poll **25s**. **Không** reload toàn trang định kỳ — chỉ `poll_status` quiet-reload khi SSE hỏng.
- ⚠️ `panel-offline-dot` trên header panel Switch/Router… **chưa** cập nhật qua AJAX (cosmetic); offline vẫn thấy qua badge + stat-card + khối Offline. Thêm/xóa thiết bị khi giữ tab mở → cần F5 (danh sách hàng trong panel không poll).

## Realtime — SSE push (app `apps.realtime`, không có model)
> UI cập nhật tại chỗ thay vì full page reload. Producer = Celery worker, consumer = web ASGI; bridge **bắt buộc qua Redis pub/sub** (2 process không chung bộ nhớ).

- **Producer**: [_poll_device_once](apps/collectors/tasks.py) gọi `publish_device_event(device, online, data)` **sau `device.save()`** (cả nhánh success lẫn ICMP-down), **ngoài** `atomic()` của `save_metrics` (tránh phát event cho transaction rollback). Publish **nuốt mọi exception** → Redis chết chỉ mất realtime, KHÔNG fail/retry poll.
- **Kênh** ([apps/realtime/channels.py](apps/realtime/channels.py)): `events:fleet` (index) + `events:device:<id>` (chi tiết). Redis DB **/2** riêng (suy từ `REALTIME_REDIS_URL`, mặc định đổi index từ `REDIS_URL`).
- **Consumer** ([apps/realtime/views.py](apps/realtime/views.py)): 2 **async** view (`redis.asyncio`) `@login_required`, `StreamingHttpResponse` text/event-stream, heartbeat 20s, dọn subscription khi client đóng. URL `/sse/fleet/` + `/sse/device/<id>/` (ngoài `/api/` để né rate-limit).
- **Payload** (compact JSON): `{v,type,device_id,name,device_type,online,last_seen,cpu,mem,if_up,if_total,ts}` + `ap_total/ap_online/ap_offline` khi `device_type=wlan_controller` (để thẻ Access Point cập nhật ngay sau khi AC poll). KHÔNG mang mbps từng port (mbps tính ở writer, không có trong `NormalizedData`) → trang chi tiết re-fetch `/api/.../interfaces/`.
- **Frontend** ([static/js/realtime.js](static/js/realtime.js) `Realtime.connectSSE`): index cập nhật badge On/Off tại chỗ (`tr[data-device-id]`) + thẻ AP khi AC poll; mỗi event SSE kích `dashRefreshAlerts` (~1.5s) để thẻ on/off khớp. Trang chi tiết re-fetch chart khi range 1h/6h/24h. **Fallback**: SSE hỏng 4 lần → `poll_status` quiet-reload (index) / setInterval (chi tiết). Không reload định kỳ 150s.
- **Dashboard cập nhật NGOÀI SSE**: `alerts_summary` (~25s + debounce sau SSE) cập nhật **panel Active Alerts + card Offline + thẻ đếm on/off per-type** qua `_dashboard_counts()`. Alert eval inline sau mỗi poll; beat `evaluate_alert_rules` là safety net.
- **Chống treo/hiển thị cũ**: `@never_cache` cho view `index`; nginx `location /static/js/` đặt `Cache-Control: no-cache` (revalidate — tránh trình duyệt chạy `realtime.js` bản cũ 30d); guard `window.Realtime` + try/catch quanh SSE để lỗi SSE/JS **không làm dừng script** (nếu không poller 25s ngừng → treo). Đổi JS/template → user cần **Empty-Cache-Hard-Reload 1 lần**.
- ⚠️ **Bắt buộc ASGI**: SSE dưới sync WSGI/gunicorn chiếm trọn 1 worker/kết nối → 4 dashboard là treo. [entrypoint.sh](entrypoint.sh) chạy `gunicorn config.asgi:application -k uvicorn.workers.UvicornWorker`. [nginx.conf](nginx/nginx.conf) có `location /sse/` riêng (`proxy_buffering off`, `read_timeout 3600s`). Deploy: đổi runtime web (WSGI→ASGI) + publish trong worker → **rebuild cả `app` lẫn `worker`** + reload nginx.
- Test SSE: `uvicorn config.asgi:application` rồi `curl -N http://127.0.0.1:8000/sse/fleet/ --cookie "sessionid=<valid>"` (thấy `: connected` → `: heartbeat`); trigger poll → bắn `event: metrics`.

## Cache-first metrics — `METRICS_WRITE_MODE` (app `apps.metrics`, module `cache.py`)
> Giảm tải ghi Postgres: metrics thường xuyên vào **Redis**, Postgres CHỈ ghi khi có
> **sự cố** (alert fire) hoặc **đổi trạng thái quan trọng**. Bật/tắt qua cờ, mặc định TẮT.

- **Cờ** `METRICS_WRITE_MODE ∈ {"db","cache"}` (env, mặc định `"db"`). `"cache"` = bật cache-first. Rollback = đổi về `"db"` + rebuild. Bật dần an toàn.
- **Redis DB /1 riêng** (`CACHE_REDIS_URL`, suy từ `REDIS_URL` `.rsplit("/",1)[0]+"/1"`) — tách Celery **/0** & realtime **/2**. Dùng **redis-py trực tiếp** (không django-redis) qua [apps/metrics/cache.py](apps/metrics/cache.py); mọi thao tác nuốt exception (ghi trả cờ False → caller fallback).
- **Key** (đều có TTL): `m:latest:<device_id>` (STRING JSON, snapshot mới nhất) · `m:series:sys:<device_id>` (LIST scalar cấp device `{ts,cpu,mem,sc?,vmr?,vmu?,wc?,wao?}`) · `m:series:if:<interface_id>` (LIST `{ts,in_mbps,out_mbps,status,in_errors,out_errors}`). Cap `METRICS_SERIES_MAX_SAMPLES` (mặc định 800 ≈ 26h @120s → phủ chart raw-tier 24h). TTL: latest 30min, series ~25h.
- **3 nguồn đọc chuyển sang cache** (mấu chốt — bỏ ghi raw phải kèm chuyển đọc):
  1. **Alert engine** ([apps/alerts/engine.py](apps/alerts/engine.py)): mọi getter có nhánh `if _use_cache()` đọc `get_latest`/`get_sys_series`/`get_if_series`, **giữ nguyên signature + logic hysteresis/sustained** (helper chung `_sustained_verdict`). Sustained cấp-device đọc scalar trong sys-series (`sc/vmr/vmu/wc/wao`), sustained interface đọc if-series. `device_online` KHÔNG đổi (dùng `last_ok_seen`). `_fresh_latest` bỏ snapshot cũ hơn `since` (giữ ngữ nghĩa `timestamp__gte`).
  2. **Tính Mbps** ([apps/metrics/writer.py](apps/metrics/writer.py) `_compute_mbps_core`): prev counter lấy từ `m:latest` (bytes+ts snapshot trước) thay vì row `InterfaceStats`. `_calc_mbps` (DB) & `_calc_mbps_from_snapshot` (cache) cùng gọi core.
  3. **Dashboard/Chart** ([apps/dashboard/views.py](apps/dashboard/views.py) helper `_detail_health`/`_detail_interfaces` dựng SystemHealth/Interface **chưa lưu** từ cache cho template; [apps/metrics/api.py](apps/metrics/api.py) tier **raw → Redis series**, hourly/daily → DB không đổi; AP card + wifi/hyperv/wlan detail đọc snapshot).
- **Ghi Postgres khi nào** (cache-mode): (a) **đổi trạng thái**: interface up↔down / VM state / repl_health đổi → ghi `InterfaceStats`/`VMStats` + 1 `SystemHealth` ngữ cảnh (`_persist_change_events`, so snapshot mới vs prev cache; poll đầu prev rỗng → không nhiễu); (b) **alert fire**: `_fire_alert` → `Alert` (như cũ) + `_persist_incident_snapshot` ghi 1 `SystemHealth` bằng chứng.
- **Rollup từ cache** ([apps/metrics/aggregation.py](apps/metrics/aggregation.py)): `rollup_*_hourly` khi cache-mode gom **ring-buffer Redis** giờ vừa hoàn tất → `*Hourly` (upsert). Daily không đổi. Chart 7d/30d vẫn từ `*Hourly/*Daily`.
- **Interface inventory VẪN ghi DB** ở cả 2 mode (`_sync_interface_inventory`, đổi thưa) — cần PK để khoá if-series + evidence.
- ⚠️ **Rủi ro**: Redis restart/flush → gap chart ngắn hạn + **reset cửa sổ sustained** (alert trễ tối đa `duration_min`; RDB persist giảm nhẹ). Redis down → **fallback ghi DB** (writer trả False → `_save_metrics_db`), alert/dữ liệu không mất; `device_online` vẫn chạy (dùng `last_ok_seen`).
- ⚠️ **Chưa chuyển sang cache**: `api_export.py` (export raw đọc DB → rỗng ở cache-mode) — dùng hourly/daily hoặc tạm bật DB-mode khi cần export raw dài hạn.
- ✅ Verify cục bộ (Redis thật /1): [tests/metrics/test_cache_mode.py](tests/metrics/test_cache_mode.py). Deploy: `METRICS_WRITE_MODE=cache` trong `.env.production` → rebuild `app`+`worker`+`beat`; verify poll thật rồi `redis-cli -n 1 KEYS 'm:*'`.

## Chạy dev
```bash
cp .env.example .env && python manage.py migrate && python manage.py createsuperuser && python manage.py runserver
# Terminal riêng:
celery -A config worker -l info
celery -A config beat -l info
```

## Trạng thái
Phase 1–7 **đã hoàn thành** (setup/models → collector SNMP/SSH + tests → Celery + HyperV WinRM → dashboard + Chart.js → alert Email/Telegram + Rule CRUD → Docker/prod deploy → RBAC 2 cấp).

### Production (đang chạy)
- Server `monitorsrv` = `10.0.193.234` (SSH sẵn, user `monitorsys`); app tại `/home/monitorsys/monitor_system`.
- Docker Compose: `app` (gunicorn+**UvicornWorker/ASGI** cho SSE) + `worker` + `beat` + `db` (postgres16) + `redis` + `nginx`. Code **build vào image** (`build: .`, không bind-mount).
- Deploy: commit/push → trên server `git pull && docker compose build app worker && docker compose up -d`. Collector chạy trong `worker` → đổi OID/collector phải rebuild `worker`.
- Docker Hub không vào được: tạm `docker cp` file + `docker compose restart` (recreate sẽ mất → rebuild khi registry hồi).

## File quan trọng
| File | Mô tả |
|---|---|
| [apps/collectors/base.py](apps/collectors/base.py) | BaseCollector, BaseAdapter, NormalizedData |
| [apps/collectors/switch_snmp.py](apps/collectors/switch_snmp.py) | SNMP collector + auto-detect os_family |
| [apps/collectors/switch_ssh.py](apps/collectors/switch_ssh.py) | SSH collector (Netmiko) |
| [apps/collectors/factory.py](apps/collectors/factory.py) | CollectorFactory |
| [apps/collectors/tasks.py](apps/collectors/tasks.py) | `_poll_device_once`, online/ICMP, clear `last_seen` khi offline, publish SSE |
| [apps/devices/models.py](apps/devices/models.py) | Device, `is_online` property (grace từ `last_seen`) |
| [apps/metrics/writer.py](apps/metrics/writer.py) | Ghi metrics (DB/cache), tính delta Mbps, evidence khi đổi trạng thái |
| [apps/metrics/cache.py](apps/metrics/cache.py) | Redis cache metrics: latest snapshot + ring-buffer (cache-first) |
| [apps/alerts/engine.py](apps/alerts/engine.py) | Alert rule evaluation + dedup |
| [apps/realtime/publisher.py](apps/realtime/publisher.py) | publish_device_event + build_payload (Redis pub/sub, sync) |
| [apps/realtime/views.py](apps/realtime/views.py) | Async SSE stream view (redis.asyncio) |
| [static/js/realtime.js](static/js/realtime.js) | `Realtime.connectSSE` + cập nhật badge/chart, fallback polling |
| [apps/dashboard/views.py](apps/dashboard/views.py) | index + *_detail + `alerts_summary`/`poll_status` + helper `_dashboard_counts` |
| [oids/](oids/) | OID profiles YAML per vendor |
| [config/settings/production.py](config/settings/production.py) | Production settings |
| [requirements/prod.txt](requirements/prod.txt) | Production dependencies |
