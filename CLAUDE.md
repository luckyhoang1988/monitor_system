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
Celery Beat (60s) → CollectorFactory → SNMP/SSH/WinRM
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
- Scalar `.0` thường trống → walk table. ⚠️ **Đính chính 2026-07-11**: code (`_collect_cpu_mem_huawei`,
  [switch_snmp.py](apps/collectors/switch_snmp.py)) KHÔNG lọc theo tên entity "MPU Board"/mainboard
  như mô tả trước đây (không hề walk cột tên entity) — thực tế chọn **entry có CPU cao nhất** trong
  toàn bảng, rồi lấy Mem cùng index đó. Hội tụ đúng trên switch 1-MPU (fleet 16 thiết bị đã verify),
  nhưng **chưa thử trên chassis nhiều MPU/line card thật** (S9300/S12700, có trong "Mục tiêu") — trên
  chassis đó, "CPU cao nhất" có thể rơi vào 1 line card/MPU standby đang bận thay vì MPU active chính.
  Không phải bug đã xác nhận (chưa có thiết bị để verify) — chỉ là điểm cần walk lại + đối chiếu
  `display cpu-usage` CLI khi có S9300/S12700 thật vào fleet.
- Dùng chung cho VRP V5 (S5735 V200R021), YunShan (CloudEngine S5735-L-V2 V600R023/024), **và firewall USG6525E** (VRP V600R007C20SPC600, entity MPU 67108873) — collector `huawei_vrp` chạy nguyên.
- ⚠️ **USG từ chối PTY** (chỉ exec-channel) → netmiko `huawei_vrp` fail "Channel closed" → firewall phải poll **SNMP** (hoặc exec-channel paramiko gửi `system-view\n…\nquit` trong 1 phiên).

**Cisco**:
- IOS classic (C2960X): CPU `1.3.6.1.4.1.9.2.1.58.0` (OLD-CISCO-CPU 5min), Mem pool `.1`.
- Business/SMB (Catalyst 1200/1300, CBS250/350): CPU `rlCpuUtil 1.3.6.1.4.1.9.6.1.101.1.9.0`. **Mem KHÔNG expose SNMP → mem=0** (giới hạn HW, không phải bug).
- IOS-XE — ⚠️ **CHƯA kiểm chứng** (không có thiết bị): CPU/mem hard-code index `.1`; cần walk/verify khi có thiết bị thật (index khác trên stack/multi-RP).
  - **Research 2026-07-11 (chưa code, chờ thiết bị thật)**: Mem hiện dùng `CISCO-MEMORY-POOL-MIB`
    (`ciscoMemoryPoolTable`, `1.3.6.1.4.1.9.9.48.1.1.1.5.1`/`.6.1`) — Used/Free là **Gauge32
    (32-bit)**, tài liệu Cisco chính hãng (SNMP config guide xe-16-9/xe-17-x) ghi nhận overflow/sai
    số trên platform RAM lớn (Catalyst 9300/9500/9600, ISR/ASR4000 — phổ biến trên IOS-XE hiện đại
    hơn IOS classic). MIB thay thế đúng là `CISCO-ENHANCED-MEMPOOL-MIB` (`cempMemPoolTable`
    `1.3.6.1.4.1.9.9.221.1.1.1.1`, cột `.18`=cempMemPoolHCUsed/`.20`=cempMemPoolHCFree, bản 64-bit).
    ⚠️ **KHÔNG thể hardcode 1 index như MIB cũ** — đã đọc thẳng file `.my` gốc: table này INDEX
    **KÉP** `{entPhysicalIndex, cempMemPoolIndex}` (khác `ciscoMemoryPoolTable` chỉ index 1 cột
    `ciscoMemoryPoolType`), nên phải walk + chọn đúng entry (thường lọc theo `cempMemPoolName`
    chứa "Processor"/"System memory") — CHƯA biết entPhysicalIndex nào là RP chính trên platform
    thật (nhất là stack/multi-RP) nếu không có thiết bị để walk đối chiếu. Quyết định: **không viết
    code đoán** (giống lý do bỏ HP/Aruba) — giữ nguyên `CISCO-MEMORY-POOL-MIB` hiện tại (đơn giản,
    đủ dùng cho platform RAM nhỏ, đã ghi rõ "chưa kiểm chứng" nên không ai bị đánh lừa) cho tới khi
    có IOS-XE thật để walk `cempMemPoolTable` + đối chiếu `show processes memory platform`.

**Synology DSM (NAS)** — UCD-SNMP-MIB (`1.3.6.1.4.1.2021`), `oids/synology_dsm.yaml`:
- ❌ **KHÔNG dùng `ssCpuUser/.9`+`ssCpuSystem/.10`+`ssCpuIdle/.11` (chuẩn UCD-SNMP-MIB, `cpu=100-idle`)** — verify runtime NAS-Pfvn 2026-07-11: DSM trả User(0)+System(1)+Idle(47)=**48**, phải ≈100 theo chuẩn. Field percent này bị lỗi scale trên firmware DSM (root cause chưa rõ, chỉ biết KHÔNG tin được) → `100-idle` từng báo CPU **53-54%** giả trong khi Resource Monitor thật DSM chỉ **~1-4%**.
- ✅ **Dùng RAW counter** `ssCpuRawUser/.50` + `RawNice/.51` + `RawSystem/.52` + `RawIdle/.53` (jiffies cộng dồn từ boot, KHÔNG phải % — chuẩn Cacti/Zabbix/Munin cho host net-snmp) — **delta giữa 2 lần poll liên tiếp**: `cpu% = 100 × Δ(user+nice+system) / Δ(user+nice+system+idle)`. Cần baseline (poll trước, lưu Redis DB/1 qua `apps/collectors/cpu_state.py`, TTL 600s, độc lập `METRICS_WRITE_MODE`) → poll đầu tiên (hoặc sau khi mất state) trả `cpu=0.0`, tự lành ở poll kế tiếp. Counter giảm (NAS reboot) → bỏ mẫu, không suy đoán bừa.
- ✅ Verify runtime NAS-Pfvn 2026-07-11 (3 poll cách nhau ~20-25s qua `manage.py poll_device`, đối chiếu DSM Resource Monitor UI): cpu trả 4.3% rồi 3.7% — khớp DSM thật (nền ~1%, spike ~12-15%), thay vì 53-54% cũ.
- `cpu_idle` (`.11`) vẫn giữ trong profile làm **fallback** khi thiếu OID raw trong profile hoặc SNMP không trả đủ 4 giá trị raw — KHÔNG xoá.
- Mem: `mem% = (memTotalReal - memAvailReal - memBuffer - memCached)/total×100` (loại cache/buffer để khớp DSM Resource Monitor) — **đã verify đúng từ trước** (commit `ee2b47f`), không đổi lần này.

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
- ⚠️ **AC SNMP phản hồi CHẬM đều** (verify id=23 ACL_Wlan 2026-07-02): poll ~28-33s (interfaces 9s + wifi walk 9s + vlan 4s + port_mode 3s) → sát `POLL_DEVICE_SOFT_LIMIT=45s`; khi 4 worker bận vượt 45s → `soft_timeout_sighandler` giết task → coi offline → xoá `last_seen` → **badge Off giả** dù ICMP+SNMP thật vẫn OK (`wlan_controller` KHÔNG trong `ICMP_DEVICE_TYPES` nên online = collect-thành-công; ping tốt vô nghĩa với online status). Fix (commit 836d543): `collect_raw` **bỏ `_collect_access_vlans`+`_collect_port_modes` cho `device_type=wlan_controller`** (AC giám sát AP/client, không phải switchport VLAN) → poll còn ~22s, biên an toàn dưới 45s.

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
- **Chống spam khác** ([apps/alerts/engine.py](apps/alerts/engine.py)): (1) `_resolve_alert` chỉ gửi ✅ RECOVERED nếu fire đã từng có `AlertNotification` status `sent` → fire bị flapping-suppress thì resolve im lặng (không dội recovery). ⚠️ Nếu fire vẫn `pending`/`processing` TẠI THỜI ĐIỂM resolve chạy (worker chết giữa lúc `_fire_alert` commit outbox và lúc kịp dispatch) → `_resolve_alert` không queue gì; `_finalize_fire_sent` (gọi khi fire đó CUỐI CÙNG cũng chuyển "sent", dù từ dispatch gốc hay sweep) gộp "chuyển sent" + "queue recovery trễ nếu alert đã resolve" (`_queue_late_recovery_if_resolved`) vào CHUNG 1 transaction có khoá `Device` (giống `_fire_alert`/`_resolve_alert`) — tránh cả mất recovery khi crash giữa 2 bước lẫn race đọc `is_active` với `_resolve_alert`. Quyết định recovery theo TỪNG (alert, channel), không theo alert (rule nhiều channel, channel chậm không được nhận RECOVERED trước khi từng nhận ALERT) — xem memory `notification-outbox-late-recovery-claim-token.md`. (2) `mem_percent==0` coi là sentinel "không đo được" (Cisco Business/SMB không expose mem) → `_latest_mem`/`_sustained_cpu_mem` bỏ qua → rule `lt/lte` mem không fire giả.
- ⚠️ **Rule `duration_min>0` — resolve KHÔNG cần sustain, chỉ FIRE mới cần** (fix 2026-09-28,
  commit `d258bec`): các hàm `_sustained_*` (`_sustained_cpu_mem`/`_sustained_host_perf`/
  `_sustained_vm_metric`/`_sustained_wifi_client_count`/`_sustained_wifi_ap_offline_count`/
  `_sustained_uplink_traffic_max`) dùng chung `_sustained_verdict()` — hàm này trả `None` bất cứ
  khi nào điều kiện KHÔNG còn đúng suốt window, dùng chung cho cả "chưa đủ sustain để fire" VÀ "đã
  hồi phục". `check_device_alerts` cũ `if value is None: continue` → alert `duration_min>0` đã fire
  **KHÔNG BAO GIỜ tự resolve được** (verify runtime: 11/13 alert active loại này đã hồi phục thật
  từ nhiều ngày/tháng trước — cũ nhất 95 ngày — nhưng chưa từng gửi Recovered). Miễn nhiễm: metric
  nhị phân `device_online`/`if_status` (2 hàm sustained riêng tự trả `0.0`/`1.0` rõ ràng, không bao
  giờ `None`-khi-hồi-phục, nên offline/recovered luôn hoạt động đúng — đây là lý do bug tồn tại lâu
  mà không ai phát hiện, vì rule online/offline test nhiều nhất lại miễn nhiễm). Fix: khi sustained
  trả `None` mà đang có alert active → fallback đọc giá trị TỨC THỜI (`getter` plain, cùng hàm dùng
  cho `duration_min=0`) rồi xét resolve qua hysteresis; không có dữ liệu tức thời (mất tín hiệu hoàn
  toàn, khác "đã hồi phục") → vẫn giữ active, không đoán bừa. Chi tiết + số liệu verify đầy đủ: memory
  `alert-sustained-never-resolve.md`.

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
- **Key** (đều có TTL): `m:latest:<device_id>` (STRING JSON, snapshot mới nhất) · `m:series:sys:<device_id>` (LIST scalar cấp device `{ts,cpu,mem,sc?,vmr?,vmu?,wc?,wao?}`) · `m:series:if:<interface_id>` (LIST `{ts,in_mbps,out_mbps,status,in_errors,out_errors}`). Cap `METRICS_SERIES_MAX_SAMPLES` (mặc định 1500 ≈ 25h @60s → phủ chart raw-tier 24h). TTL: latest 30min, series ~25h.
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

## HyperV Host Performance Counters (Phase 1 MVP, từ 2026-07-07)
> 7 metric bổ sung ngoài CPU/mem cũ để phát hiện host quá tải sớm hơn: `cpu_hv_percent`,
> `mem_available_mb`, `disk_read_iops`, `disk_write_iops`, `disk_read_latency_ms`,
> `disk_write_latency_ms`, `net_mbps_total` — cột riêng `SystemHealth`/`Hourly`/`Daily` (nullable),
> KHÔNG dùng JSON `extra`.

- **Thu thập**: `Get-Counter -SampleInterval 2 -MaxSamples 5` (~10-12s/host, verify runtime 2 host thật)
  trong **cùng phiên WinRM** với `Get-VM`/CPU/mem WMI cũ (không tách phiên thứ 2), cô lập lỗi bằng
  try/catch riêng — hỏng Get-Counter không ảnh hưởng phần VM/CPU/mem đã chạy ổn định.
- ⚠️ **WinRM/cmd.exe giới hạn độ dài dòng lệnh ~8191 ký tự** (`run_ps` base64-encode UTF-16LE rồi
  truyền qua cmd.exe). Bản PS_SCRIPT đầy đủ comment + tên biến dài (`cpuUtil`, `Avg-List`, match theo
  `$cs.Path -like '*...*'`) vượt giới hạn → lỗi **"The command line is too long"** (exit 1), toàn bộ
  poll host đó fail (kể cả VM/CPU/mem cũ). Fix: nén script (không comment, tên biến 1-2 ký tự) +
  **positional index thay vì string-match Path** — đã verify runtime trên cả 2 host thật rằng
  `Get-Counter -Counter $paths` trả `CounterSamples` **đúng thứ tự request**, nên `$c[0..7]` là 8
  counter scalar theo đúng thứ tự khai báo trong `$paths`, `$c[8..]` luôn là các instance
  `Network Interface(*)` (do path đó xếp cuối mảng). ⚠️ Nếu sau này thêm counter mới vào `PS_SCRIPT`
  của `hyperv.py`, phải đo lại kích thước base64 trước khi deploy (`len(base64.b64encode(PS_SCRIPT.encode('utf-16-le')))`
  phải < ~8000 để có margin) — script đầy đủ có comment/tên biến rõ nghĩa xem `scratchpad/plan_hyperv.md`.
- **Network throughput**: `\Network Interface(*)\Bytes Total/sec`, loại isatap/teredo/loopback/pseudo-
  interface/qos/wfp/kernel-debug bằng regex. Verify runtime: NIC vật lý (kể cả bị teaming) đã có traffic
  thật, **không cần fallback** `Hyper-V Virtual Switch(*)\Bytes/sec`.
- **Aggregation trong PS trước khi trả JSON**: CPU/hypervisor%/mem committed%/disk IOPS/network Mbps →
  **average** 5 mẫu; disk latency (read/write) → **max** 5 mẫu (spike-sensitive).
- **Poll interval**: hạ `POLL_HYPERV_INTERVAL_SECS` **300s → 120s** sau khi đo timing thật (~10-12s
  burst + ~4s WMI cũ ≈ 30s/tick cho 2 host, duty cycle ~25%, margin ~4x — tránh lặp "poll queue
  snowball", xem memory `poll-queue-snowball-slow-device.md`). `poll_all_hyperv()` tự log cảnh báo nếu
  1 tick vượt 50% interval.
- ⚠️ **Batch KHÔNG có timeout tổng cho tới 2026-07-07** ([apps/collectors/tasks.py](apps/collectors/tasks.py)
  `poll_all_hyperv`): task này chạy inline nhiều host trong 1 lời gọi (lý do: tránh mất task
  `poll_device` trên môi trường Celery/Windows không ổn định), KHÔNG qua `poll_device` nên không có
  soft/hard `time_limit` per-device như switch/router. 1 host WinRM treo có thể chiếm ~280s (2 script
  host+volume × 2 endpoint http/https × operation 60s/read 70s timeout mỗi cái) — đủ nuốt hết chu kỳ,
  y hệt cơ chế "poll queue snowball" đã fix cho `poll_device` (commit `fe1dac1`) nhưng CHƯA từng áp
  cho nhánh hyperv. **Đã fix**: thêm `soft_time_limit`/`time_limit` thẳng lên `poll_all_hyperv` (bắt
  `SoftTimeLimitExceeded`, log rồi return — host chưa poll kịp chờ chu kỳ sau, host đã poll xong
  trong vòng vẫn giữ kết quả vì `save_metrics` chạy ngay trong từng host, không đợi hết loop).
  ⚠️ **Giá trị 100s/110s (2026-07-07) chỉ tính cho 2 host healthy — KHÔNG đủ khi fleet tăng lên 3
  host (2026-09-28, thêm Hyprver03) cộng với 1 host đang có sự cố phần cứng** (Hyperv-02, xem memory
  `hyperv02-winrm-instability.md`): 1 lệnh WinRM có thể tự treo tới hết timeout socket riêng (60-70s)
  BẤT KỂ soft_time_limit đã bắn, vì `SoftTimeLimitExceeded` chỉ raise được khi interpreter quay lại
  bytecode — kẹt trong 1 call blocking lâu hơn khoảng (hard−soft) còn lại thì Celery SIGKILL thẳng
  cả task, mất trắng toàn bộ host trong vòng đó (kể cả host đang khoẻ) → offline giả. Đã nâng lên
  **`soft=250s`/`hard=270s`** + `POLL_HYPERV_INTERVAL_SECS` **120s→300s** (khớp
  `Device.collect_interval=300` đã set sẵn), deploy commit `14b8f7a`, verify 2 vòng sau deploy
  99.6s/109.5s không hard-kill. Quy tắc: thêm host HyperV mới → đo lại timing thật trước, tính theo
  host XẤU NHẤT trong fleet, không ngoại suy tuyến tính. Chi tiết: memory
  `poll-queue-snowball-slow-device.md` mục "Recurrence 2026-09-28".
- **Alert engine**: không tái dùng `_sustained_cpu_mem` (hardcode field cpu/mem) — dict riêng
  `_HOST_PERF_FIELD_MAP` (metric → short-key ring-buffer + field SystemHealth) + `_latest_host_perf`/
  `_sustained_host_perf` dùng chung `_sustained_verdict`. 4 `AlertRule` (`device_type=hyperv`,
  tạo qua UI `/alerts/rules/` — KHÔNG có trong `seed_alert_rules.py`, chỉ tồn tại dạng data trên
  DB prod): CPU hypervisor >80%, RAM available <2048MB, disk read/write latency >20ms.
- ✅ Verify runtime 2026-07-07 (Hyperv-01/02 thật, cả local dev DB-mode lẫn prod cache-mode Redis):
  dữ liệu non-null hợp lý cả 7 field, alert fire đúng trên spike latency thật (33ms/549ms), Telegram
  gửi thành công trên prod.
- ⚠️ **2026-09-28: nâng `duration_min` disk read/write latency 5→15 phút** (rule id 19/20, prod)
  — noise thật: lúc backup server chạy, latency vượt 20ms đều đặn, cửa sổ 5 phút (≈1-2 mẫu ở
  poll cadence 300s) quá ngắn nên hầu như backup nào cũng bắn Telegram. Đối chiếu lịch sử fire 7
  ngày (37 lần fire riêng ngày 2026-09-28): đa số fire→resolve trong 4-15 phút (khớp thời lượng 1
  job backup), vài trường hợp kéo dài 58/108 phút và 1 alert vẫn active >4 giờ (Hyperv-02, rule
  20 — khớp sự cố RAID thật đang có, xem [[hyperv02-winrm-instability]]) — đây mới là loại cần
  báo, không phải backup blip. Đặt `duration_min=15` (yêu cầu MỌI mẫu trong cửa sổ 15 phút đều
  vượt ngưỡng, logic có sẵn ở `_sustained_verdict`, không cần đổi code) lọc gần hết backup noise
  mà vẫn giữ báo đúng cho sự cố kéo dài thật. Đổi qua Django shell (`AlertRule.objects.filter(id__in=[19,20]).update(duration_min=15)`)
  trên prod trực tiếp — thuần data, không rebuild/deploy.

### HyperV — Disk Throughput/Queue/IO-size + Per-Volume mapped theo VM (từ 2026-07-07, cùng ngày)
> Vòng 2 cùng ngày: thêm 4 metric host-level (`disk_read_throughput_mbps`, `disk_write_throughput_mbps`,
> `disk_queue_length`, `avg_io_size_kb` — cùng pipeline cột `SystemHealth`/Hourly/Daily như 7 metric
> trên) **và** bảng **Per-Volume Disk Stats** mapped theo VM (model mới `VolumeStats`) để trả lời "volume
> nào đang bị latency cao, VM nào bị ảnh hưởng".

- **PS_SCRIPT phải TÁCH THÀNH 2 SCRIPT RIÊNG** (`PS_SCRIPT` host + `PS_SCRIPT_VOLUME`, `apps/collectors/hyperv.py`):
  gộp chung vào 1 script đo được **15408 ký tự base64** (gần gấp đôi giới hạn ~8191) vì per-volume
  cardinality động (N volume/host) buộc match theo `Path`/`InstanceName` (tốn ký tự) thay vì positional
  index như khối scalar host cố định. `collect_raw()` gọi `_run_ps()` **2 lần** (2 phiên WinRM/NTLM
  handshake riêng, cô lập lỗi ở Python — hỏng volume script không ảnh hưởng host script đã chạy được).
  Đây là ngoại lệ so với nguyên tắc cũ "không tách phiên WinRM thứ 2" — chấp nhận đổi lấy việc tránh
  vượt giới hạn hoàn toàn. Sau khi nén helper function (xem bẫy `R`/alias bên dưới) + rút gọn `$bd`
  path-prefix: `PS_SCRIPT` (host) 7908/8191 base64, `PS_SCRIPT_VOLUME` 6672/8191.
- ⚠️ **BẪY MỚI: helper function PowerShell tên `R` bị PowerShell resolve nhầm thành alias built-in
  `r` (= `Invoke-History`, có tham số positional `-Id`)** — gọi `R $arr 1` không gọi function của mình
  mà gọi `Invoke-History` với `$arr` bind vào `-Id`, ném lỗi runtime **"Cannot convert 'System.Object[]'
  to the type 'System.String' required by parameter 'Id'. Specified method is not supported."**. Lỗi
  này bị `try/catch` trong `PS_SCRIPT` nuốt im lặng → `$hp=$null` → toàn bộ 13 host-perf field trả
  `None` mà KHÔNG có exception nào lộ ra ngoài (verify: bug xảy ra thật khi đổi từ if/else dài dòng
  sang helper `function R(...)` để nén script, phát hiện bằng cách bisect từng biến thể qua
  `manage.py shell` + thêm `$err=$_.Exception.Message` debug tạm). Fix: đổi tên hàm thành `RA` (không
  trùng alias). **Quy tắc chung**: đặt tên helper function PowerShell 1 ký tự phải kiểm tra trước
  bằng `Get-Alias <tên>` (alias built-in phổ biến 1 ký tự: `r`=Invoke-History, `h`=Get-History,
  `d`=Get-ChildItem, `l`=Get-ChildItem, `p`=Set-Location trên 1 số profile...) — hàm dài ≥2 ký tự
  không mô tả rõ (`RA`, `RS`, `RX`) an toàn hơn hàm 1 ký tự dù tốn thêm vài chục ký tự base64.
- **Per-volume**: `Get-VM|Get-VMHardDiskDrive|Select VMName,Path` (2 host thật: toàn ổ cục bộ
  `D:\...`/`E:\...`, KHÔNG CSV/cluster) map sang `LogicalDisk(*)` bằng cách rút drive-letter từ Path
  (`^([A-Za-z]):\\`) rồi lowercase — **verify runtime**: `InstanceName` của `LogicalDisk(*)` trả về
  **lowercase** (`"c:"`, `"d:"`, `"harddiskvolume1"`, `"_total"`), phải chuẩn hoá 2 bên khi join. Loại
  instance `_total` (tổng host, trùng khối scalar). Instance `harddiskvolumeN` (system reserved/không
  gắn VM) vẫn hiện trong bảng với `vm_names=[]`. `MaxSamples=3` (không phải 5 như host) — chấp nhận độ
  mượt thấp hơn vì mục đích là "xác định VM bị ảnh hưởng" chứ không phải alert chính xác cao.
- **Model mới `VolumeStats`** (mirror `VMStats`, index `[device,volume_name,-timestamp]` — tái dùng
  pattern DISTINCT ON đã fix bug 504 cho VMStats). Ghi **mỗi poll** ở DB-mode (số volume/host thấp,
  rẻ) — KHÁC `VMStats` (event-driven, chỉ ghi khi state đổi) vì đây là scalar liên tục cần lịch sử như
  `SystemHealth`, không phải state cần dedup. Cache-mode: KHÔNG ghi DB mỗi poll (theo triết lý
  cache-first) — chỉ snapshot "hiện tại" trong `m:latest:<id>["volumes"]` cho dashboard, DB chỉ ghi khi
  alert fire (`_persist_incident_snapshot` mở rộng bulk_create `VolumeStats` cùng lúc với
  `SystemHealth`). **KHÔNG rollup Hourly/Daily cho VolumeStats** (out of scope MVP — bảng hiện trạng
  để soi nhanh, không phải chart lịch sử; N volume động khiến rollup phức tạp hơn nhiều).
- **Per-volume mở rộng (2026-07-07, đợt 3, cùng ngày)**: đối chiếu với danh sách 8 counter chuẩn
  Windows `LogicalDisk` (`Disk Read/Write Bytes/sec`, `Avg. Disk sec/Read/Write` đã có sẵn từ đợt 2;
  bổ sung `Current Disk Queue Length`, `Disk Transfers/sec`, `Split IO/sec`, `% Idle Time`).
  `Disk Transfers/sec` **KHÔNG query counter riêng** — suy ra bằng `read_iops + write_iops` (đúng công
  thức chuẩn Windows, không cần verify runtime vì là identity toán học cố định chứ không phải giá trị
  đo riêng theo thiết bị). 3 counter còn lại query thật qua `Get-Counter`. ⚠️ `Current Disk Queue
  Length` (raw/instantaneous, lấy **mẫu cuối** trong `MaxSamples=3`) là counter **khác**
  `Avg. Disk Queue Length` đã có (avg theo sample interval) — 2 field riêng biệt (`current_queue_length`
  vs `queue_length`), không gộp/thay thế nhau. Field JSON rút gọn `cql/tps/sio/idt` trong
  `PS_SCRIPT_VOLUME` (margin base64 8191/8191), map lại tên đầy đủ ở Python
  `HyperVCollector._normalize_volumes()` — cùng kiểu rtm/wtm/dql/aio ở `PS_SCRIPT` host. Base64 cuối
  7880/8191 (margin ~3.8%). Migration `0008_volumestats_current_queue_length_and_more`.
- **KHÔNG có alerting per-volume** trong lần này (out of scope, quyết định có chủ đích) — `AlertRule`
  hiện là scalar-per-device, alert theo "volume tệ nhất/host" cần thiết kế riêng (đề xuất: alert theo
  max latency across volumes + kèm tên VM trong message, không đổi kiến trúc `AlertRule`). Bảng
  dashboard "Per-Volume Disk Stats" (`templates/dashboard/hyperv_detail.html`) đã đáp ứng đúng nhu cầu
  gốc: nhìn thấy ngay volume nào latency cao + VM nào đang nằm trên đó.
- ⚠️ **Timing tăng đáng kể do 2 WinRM session/host**: elapsed đo runtime 2 host thật tăng từ
  ~32.8s → **~52.5s tổng cho 2 host** (2nd WinRM handshake + burst thêm ~15-20s/host). Vẫn dưới
  ngưỡng cảnh báo 50%×120s=60s trong `poll_all_hyperv()` nhưng margin mỏng hơn nhiều so với trước
  (trước ~2.6x margin, nay ~1.15x) — nếu fleet HyperV tăng số host, cân nhắc tăng
  `POLL_HYPERV_INTERVAL_SECS` hoặc giảm `MaxSamples` volume script trước khi thêm host mới.
- Migration `0007_systemhealth_avg_io_size_kb_and_more` (4 cột `SystemHealth` + 8 cột mỗi
  Hourly/Daily + model `VolumeStats`). Commit `21da8e0`.

## iLO Redfish — RAID/disk health cho HyperV host (từ 2026-09-28)
> Nguồn phát sinh: sự cố Hyperv-02 (RAID P440ar mất 2/4 disk RAID5, xem memory
> `hyperv02-winrm-instability.md`) cho thấy WinRM collector KHÔNG có cách nào chẩn đoán RAID/đĩa —
> chỉ thấy "poll chập chờn". Thêm nguồn dữ liệu **độc lập hoàn toàn** với WinRM: gọi trực tiếp iLO
> Redfish API (đọc-only, HTTPS) — model/task/collector riêng, không qua `CollectorFactory`, không
> đụng `device.last_seen`/`is_online`. Chỉ hỗ trợ **HPE iLO 4/5 + Redfish SmartStorage extension**
> (OEM-specific) — loại BMC duy nhất đã verify thật, theo đúng tiền lệ "không build cho hãng chưa
> verify" (giống lý do gỡ HP/Aruba/MikroTik/Fortinet). Cấu hình theo từng Device
> (`ilo_ip_address`/`ilo_username`/`ilo_password`, optional — để trống = bỏ qua khi poll), nhập qua
> UI thêm/sửa thiết bị (section "iLO / Remote Management", hiện khi `device_type=HyperV Host`).

**Endpoint đã verify runtime thật (Hyperv-02 P440ar fw 5.04 đang RAID Critical thật, Hyprver03
P440ar fw 4.52 khoẻ mạnh, 2026-09-28)** — root `/redfish/v1/Systems/1/SmartStorage/ArrayControllers/`:
- `ArrayControllers/{id}/` — `Status.Health` của **controller**.
- `ArrayControllers/{id}/LogicalDrives/{ld}/` — `Status.Health`, `Raid`, `CapacityMiB` của từng LD.
- `ArrayControllers/{id}/LogicalDrives/{ld}/DataDrives/` — danh sách URI đĩa **khai báo thuộc LD đó**
  (dùng để biết "đĩa nào LẼ RA phải có", KHÔNG đổi theo tình trạng detect).
- `ArrayControllers/{id}/DiskDrives/{n}/` — detail 1 đĩa vật lý (`Status.Health`, `Location`
  `"1I:3:4"` dạng ControllerPort:Box:Bay, `Model`, `CapacityGB`).
- `ArrayControllers/{id}/StorageEnclosures/{n}/` — `DriveBayCount` + `Location` `"1I:3"`
  (ControllerPort:Box, tiền tố khớp `Location` của đĩa thuộc enclosure đó).

**Đã xác minh runtime (KHÔNG suy luận)**:
- `Status.Health` chỉ 3 giá trị thật đã thấy: `"OK"` / `"Warning"` / `"Critical"` (controller +
  logical drive). Map `OK=0, Warning/Degraded=1, Critical/Failed=2` — enum lạ chưa từng thấy thì
  **coi Critical (an toàn, không bỏ sót)** + log warning rõ ràng để verify sau, KHÔNG bug im lặng.
- **Đĩa "mất" (RAID member không detect) = HTTP 404 THẬT** tại `DiskDrives/{id}/`
  (`MessageID: Base.0.10.ResourceMissingAtURI`) — verify trực tiếp trên đúng 2 đĩa đang mất thật
  của Hyperv-02 (id 4, 5 thuộc LD2/RAID5). KHÔNG phải chỉ "biến mất khỏi `/DiskDrives/` collection"
  — phải đi qua đúng tập URI từ `DataDrives/` của từng LD (khai báo cấu hình) rồi GET từng cái, vì
  `/DiskDrives/` collection tự nó chỉ liệt kê đĩa CÒN detect (không có entry 404 sẵn trong đó).
- `enclosure_mismatch_count` (mất cả 1 cage đĩa): nhóm đĩa **đang hiện diện** theo tiền tố
  `Location` (bỏ phần `:Bay` cuối) rồi so với `DriveBayCount>0` của từng enclosure — enclosure có
  bay nhưng 0 đĩa present khớp tiền tố ⇒ mismatch. Heuristic dựa trên field đã verify thật nhưng
  CHƯA verify exhaustive mọi đĩa present đều đúng tiền tố enclosure (raw JSON giữ đủ để soi tay).
- ⚠️ **HPE iLO4/5 embedded webserver đóng TCP connection sau ĐÚNG 1 request dù client gửi
  keep-alive** — `requests.Session()` tái dùng connection cũ bị đóng → `ConnectionError` xen kẽ đều
  đặn OK/ERR/OK/ERR qua nhiều request liên tiếp trên cùng session (không phải sai URL — mọi endpoint
  ở trên đều đúng, chỉ lỗi khi request thứ 2+ tái dùng connection). Fix: header `Connection: close`
  + retry 1 lần bằng session mới khi `ConnectionError` — xem `/deploy` skill mục bẫy BMC.
- ⚠️ **Bug ORM bắt được ở bước verify (trước khi vào code thật)**: `GenericIPAddressField` (Postgres
  `inet`) không lưu được `""` → Django coi `""` là `None` → `.exclude(ilo_ip_address="")` sinh SQL
  luôn `NULL` cho mọi row (kể cả IP hợp lệ) → `poll_all_ilo` từng trả **0 devices** dù DB có đủ 3 IP
  đúng. Fix: chỉ `.exclude(ilo_ip_address__isnull=True)`. Xem `/deploy` skill mục bẫy
  `GenericIPAddressField` (áp dụng chung, không riêng iLO).

**Kiến trúc** (quyết định "độc lập hoàn toàn" — không gộp vào `poll_all_hyperv`, xem lý do trong
git log commit thêm feature): model `HardwareHealth` (`apps/metrics/models.py`, ghi thẳng DB mỗi
poll, KHÔNG qua `METRICS_WRITE_MODE`/cache-mode, KHÔNG rollup Hourly/Daily — giống quyết định MVP
đã áp cho `VolumeStats`) · collector `apps/collectors/ilo_redfish.py` (`IloRedfishClient`, không kế
thừa `BaseCollector`) · task `poll_all_ilo` (`apps/collectors/tasks.py`, soft/hard time_limit riêng
60s/70s — Redfish REST nhanh hơn WinRM PowerShell nhiều nên giới hạn thấp hơn `poll_all_hyperv`
nhiều) · `POLL_ILO_INTERVAL_SECS=300s` mặc định, `CELERY_BEAT_SCHEDULE["poll-all-ilo"]` (nhớ cả
`expires`+`expire_seconds`, xem mục Celery Beat bên dưới) · `ILO_CERT_VALIDATE` (bool, mặc định
`False` — iLO tự ký cert). Alert: `_ILO_FIELD_MAP`/`_latest_ilo`/`_sustained_ilo`
(`apps/alerts/engine.py`), 3 rule seed `duration_min=0` (sự cố phần cứng cần báo NGAY, không chờ
sustain): `raid_controller_health >= 2` (Critical), `raid_logical_drive_health >= 1` (Warning+),
`raid_missing_disk_count >= 1` (Critical). UI: card "Storage Health (iLO)" trên
`templates/dashboard/hyperv_detail.html`, chỉ hiện khi `device.ilo_ip_address` có giá trị.

**Verify sống 2026-09-28 (live-fire test tự nhiên trên RAID Hyperv-02 đang lỗi thật)**: deploy xong
→ vòng `poll_all_ilo` đầu tiên (20.3s/2-3 host) ghi đúng `HardwareHealth` (Hyperv-02:
`controller=2,ld_worst=1,missing=2,enclosure_mismatch=1`; Hyprver03: toàn `0` — khớp Redfish dump
thủ công trước đó) → `evaluate_alert_rules` (safety net, 90s) fire đúng cả 3 alert ngay vòng đầu →
Telegram gửi thành công cả 3 (`AlertNotification.status="sent"`). ⚠️ **Hyperv-01 (`10.0.198.253`)
trả 401 Unauthorized** — user cần nhập lại đúng username/password iLO qua UI (mỗi iLO có local
admin credential riêng, không nhất thiết giống Hyperv-02/Hyprver03).
- ⚠️ Chưa test qua `_poll_device_once`/inline alert eval — `poll_all_ilo` KHÔNG gọi
  `check_device_alerts` ngay sau khi ghi (khác pattern `_poll_device_once`), dựa hoàn toàn vào
  `evaluate_alert_rules` (safety net, 90s) để phát hiện — vẫn đủ nhanh cho ngưỡng "sự cố phần cứng"
  (không phải mili-giây) nhưng khác 1 chút so với pattern "eval inline sau mỗi poll" mô tả ở mục
  "Online/offline" bên dưới (pattern đó áp cho `_poll_device_once`, không áp cho `poll_all_ilo`).

## Celery Beat — `expire_seconds` bị reset mỗi lần `beat` restart (fix gốc 2026-07-07)
> Phát hiện khi audit lại điều kiện poll HyperV — không phải bug riêng HyperV, ảnh hưởng
> **mọi** `PeriodicTask` có khai báo `options.expires` trong `CELERY_BEAT_SCHEDULE`
> ([config/settings/base.py](config/settings/base.py)): `poll-all-hyperv`,
> `poll-all-network-devices`, `poll-all-ping-devices`, `evaluate-alert-rules`,
> `discover-topology-links`.

- **ROOT CAUSE** (đọc source `django_celery_beat.schedulers`, không đoán): `ModelEntry._unpack_options()`
  chỉ đọc key **`expire_seconds`** trong `options` — KHÔNG đọc `expires` (đó là key celery gốc
  dùng cho `apply_async`, django-celery-beat không biết tới). `DatabaseScheduler.setup_schedule()`
  gọi `update_from_dict(beat_schedule)` **mỗi lần process `beat` khởi động** (không chỉ lần tạo
  đầu) → `update_or_create(defaults=_unpack_fields(...))` ghi `expire_seconds=None` vào DB vì
  `options` code cũ chỉ có `expires`. Hệ quả: management command `sync_beat_expires` (thêm
  2026-07-07 sau 1 regression trước đó, xem memory `poll-queue-snowball-slow-device.md`) set đúng
  `expire_seconds` lúc container boot (trong `entrypoint.sh`), nhưng **NGAY SAU ĐÓ** trong CÙNG
  container `beat` tự `exec celery beat` → `setup_schedule()` ghi đè lại `None` — verify runtime
  prod: log entrypoint in "None -> 120" rồi `SELECT expire_seconds FROM
  django_celery_beat_periodictask` vẫn NULL vài phút sau khi container đã "healthy". `every`/
  `period` (khoảng lặp thật) KHÔNG bị ảnh hưởng (đường riêng, đã đúng lúc verify).
- **Đã fix tại gốc**: thêm key `expire_seconds` (song song `expires`) vào từng `options` trong
  `CELERY_BEAT_SCHEDULE` → `update_from_dict` giờ tự ghi đúng giá trị mỗi lần `beat` boot, không
  còn phụ thuộc thứ tự chạy `sync_beat_expires` vs `exec celery beat` trong entrypoint. Command
  `sync_beat_expires` giữ lại làm lớp phòng thủ idempotent, không còn là cơ chế chính.
- ⚠️ Ai đổi/thêm entry mới vào `CELERY_BEAT_SCHEDULE` có `options.expires` → nhớ thêm luôn
  `expire_seconds` cùng giá trị, nếu không entry đó lặp lại đúng bug này.

### Thay đổi quan trọng
- **2026-09-29 (cùng ngày, review vòng 2)**: Bản fix vòng 1 ngay dưới đây (`_queue_late_recovery_if_resolved`)
  tự nó tái tạo đúng loại bug mà dự án đã gặp nhiều lần ("commit trạng thái TRƯỚC, side-effect
  SAU, không durable/không đồng bộ khoá") — user dán tiếp review vòng 2, 2 điểm, cả 2 verify đúng
  bằng cách sửa tay lùi logic (chưa có commit boundary sạch để `git stash`) rồi xác nhận test mới
  FAIL trước khi khẳng định fix đúng:
  1. **Cao — `_dispatch_notifications`/`retry_pending_alert_notifications` chuyển fire sang
     "sent" ở 1 câu DB, rồi mới gọi `_queue_late_recovery_if_resolved` ở 1 câu RIÊNG sau khi câu
     trước đã commit — worker chết giữa 2 câu vẫn mất recovery vĩnh viễn** (fire đã "sent" là
     trạng thái cuối, không gì kích hoạt lại). Fix: hàm mới `_finalize_fire_sent(alert, channel,
     token)` gộp CẢ 2 thao tác vào CHUNG 1 `transaction.atomic()` — crash giữa chừng thì toàn bộ
     rollback (row vẫn "processing"), sweep sau reclaim + làm lại nguyên vẹn. Verify: test mock
     `_queue_late_recovery_if_resolved` ném exception, assert fire rollback về "processing" — xác
     nhận FAIL (kẹt ở "sent") khi tạm bỏ `transaction.atomic()`.
  2. **Cao — đọc "fire đã sent" (`_resolve_alert`) và đọc "alert đã resolve" (helper late-recovery)
     là 2 SELECT độc lập không khoá gì chung — fire chuyển sent đúng lúc nằm GIỮA lúc
     `_resolve_alert` đọc xong danh sách "đã gửi" và lúc nó COMMIT `is_active=False` thì CẢ 2 bên
     đều đọc phải giá trị CŨ của phía kia → không bên nào tạo recovery.** Fix theo đúng gợi ý
     review: `_finalize_fire_sent` khoá `Device` (`select_for_update()`) giống hệt
     `_fire_alert`/`_resolve_alert` TRƯỚC KHI đọc/ghi — 2 giao dịch cạnh tranh cùng device luôn
     serialize trên Postgres. ⚠️ **Đính chính cùng ngày**: ban đầu tưởng nhầm dev/test dùng SQLite
     (giả định KHÔNG kiểm chứng) nên ghi "SQLite bỏ qua select_for_update, test không chứng minh
     được serialization thật" — verify lại bằng kết nối trực tiếp: `.env`/`development.py` dùng
     **Postgres thật** (localhost, Postgres 18.4) cho cả dev lẫn test (`pytest.ini` trỏ
     `config.settings.development`), KHÔNG có nhánh SQLite nào trong project này. `select_for_update`
     do đó CÓ lấy khoá row thật kể cả khi chạy test. Giới hạn thật sự không phải "SQLite bỏ qua
     lock" mà là "test hiện tại chạy tuần tự 1 thread, không có 2 giao dịch nào thực sự cạnh tranh
     cùng lúc để khoá phải phát huy tác dụng". ✅ **Verify thêm cùng ngày (sau đó)**: thêm
     `TestSelectForUpdateRealPostgresLock` (`tests/alerts/test_notification_outbox.py`) — 2 thread
     thật + `django_db(transaction=True)` (2 connection Postgres thật), thread A giữ khoá `Device`
     trong `_finalize_fire_sent`, thread B gọi `_resolve_alert` cùng device đo thời gian chờ. Tự
     chứng minh test có ý nghĩa: tạm xoá `select_for_update()` khỏi `_resolve_alert` → test FAIL
     đúng dự đoán (`_resolve_alert` chạy xong 0.052s, không bị chặn); khôi phục lại → pass (≥0.4s).
     Kết luận: khoá THẬT trên Postgres, không còn là suy luận. 464 test pass, không đổi code sản
     xuất (chỉ thêm test).
  3. **Điểm bổ sung — rule nhiều channel: 1 channel đã "sent" fire là đủ để `_resolve_alert` tạo
     recovery cho MỌI channel của rule, kể cả channel khác chưa từng gửi fire (SMTP treo) → channel
     chậm nhận RECOVERED trước khi từng nhận ALERT.** Fix: `sent_fire_channels` đổi từ
     `set(alert_id)` sang `set((alert_id, channel))`, quyết định + dispatch theo từng cặp.
  3 test mới (`test_crash_between_finalize_and_recovery_rolls_back_both`,
  `test_success_path_still_finalizes_and_queues_recovery_together`,
  `test_resolve_only_queues_recovery_for_channels_whose_fire_was_sent`) — 463 test pass (460+3
  mới), 2 skip như cũ, không migration mới (chỉ đổi logic). Chi tiết: memory
  `notification-outbox-late-recovery-claim-token.md` mục "Vòng 2".
- **2026-09-29**: Review tiếp theo (người dùng dán từ ngoài, không phải security-review 4 đợt
  ngày hôm trước) soi đúng vào outbox `AlertNotification` vừa viết — 2 điểm, cả 2 verify đúng
  bằng cách đọc code thật rồi TỰ CHỨNG MINH bằng test fail trên code cũ trước khi sửa (đúng quy
  trình §0 — không sửa mù theo báo cáo):
  1. **Cao — có thể gửi FIRE trễ mà không bao giờ có RECOVERED theo sau, nếu worker chết ngay
     sau khi `_fire_alert` commit outbox `pending` nhưng trước khi kịp dispatch, RỒI alert mới
     resolve.** `_resolve_alert` chỉ queue recovery cho alert có fire `status="sent"` **TẠI THỜI
     ĐIỂM resolve chạy** — quyết định chỉ đưa ra ĐÚNG 1 LẦN, không có gì kích hoạt lại. Nếu fire
     vẫn `pending` lúc đó (chưa kịp gửi), resolve bỏ qua vĩnh viễn; sau đó
     `retry_pending_alert_notifications` vẫn gửi fire trễ thành công → người nhận thấy "sự cố
     mới" dù nó đã hồi phục từ trước, và KHÔNG BAO GIỜ nhận RECOVERED. Verify bằng test mô phỏng
     đúng thứ tự "fire pending → resolve chạy trước → fire mới được gửi trễ" — fail thật trên
     code cũ (`AlertNotification.DoesNotExist` vì không có row recovery nào được tạo), pass sau
     fix. Fix: hàm mới `_queue_late_recovery_if_resolved(alert_id, channel)` — gọi ngay sau khi 1
     fire notification CHUYỂN "sent" thật (ở cả `_dispatch_notifications` lẫn
     `retry_pending_alert_notifications`), tự kiểm tra alert đã `is_active=False` chưa; nếu rồi
     và chưa có recovery row cho channel đó → tạo `pending`. Coi "recovery còn nợ" là điều kiện
     re-check ở CẢ 2 nơi có thể làm nó đúng (resolve chạy sau HOẶC fire gửi xong sau) — bên nào
     xảy ra sau sẽ là bên phát hiện. An toàn không cần lock thêm vì 1 fire-notification row chỉ
     chuyển "sent" đúng 1 lần (nhờ fix #2 ngay dưới).
  2. **Trung bình — có thể gửi trùng thật khi channel `email` treo lâu hơn
     `stale_processing_secs` (300s), vì trước đó KHÔNG có `EMAIL_TIMEOUT` (smtplib treo vô thời
     hạn nếu SMTP server không phản hồi) và row `AlertNotification` không có "mã sở hữu" để phân
     biệt chủ cũ (bị coi nhầm là kẹt) với chủ mới (vừa reclaim).** Sweep coi 1 row `processing`
     quá `stale_processing_secs` là "kẹt" và reclaim — nhưng nếu chủ cũ thực ra vẫn đang gửi hợp
     lệ (chỉ chậm), CẢ 2 bên đều gọi hàm gửi thật (gửi trùng không tránh được nếu SMTP đã treo
     thật lâu), và bên finalize SAU có thể ghi đè kết quả ĐÚNG mà bên finalize TRƯỚC (chủ mới) đã
     ghi. Fix 2 lớp: (a) **`EMAIL_TIMEOUT=20`** (`config/settings/base.py`, mới,
     `.env.example`) — bound thời gian gửi email dưới hẳn `stale_processing_secs`, chặn gốc rễ
     nguyên nhân treo vô thời hạn; (b) **field mới `AlertNotification.claim_token`** (CharField,
     migration `0008`) — ghi token ngẫu nhiên MỖI LẦN claim (pending/processing→processing);
     finalize (sent/failed) PHẢI match đúng token đó, nếu không (đã bị reclaim, token đổi) → tự
     bỏ qua, KHÔNG ghi đè. Verify bằng test mô phỏng trực tiếp "chủ cũ cầm token cũ cố finalize
     sau khi đã bị reclaim" — `UPDATE ... WHERE claim_token=<token cũ>` khớp 0 dòng trên code có
     fix (fail với `TypeError` trên code cũ vì field còn chưa tồn tại — xác nhận test đúng là
     regression test, không phải tautology).
  2 test mới (`test_stale_owner_cannot_overwrite_reclaimed_result`,
  `test_fire_confirmed_sent_after_resolve_still_gets_recovery`,
  `tests/alerts/test_notification_outbox.py`) — 460 test pass (458+2 mới), 2 skip như cũ. Chi
  tiết đầy đủ: memory `notification-outbox-late-recovery-claim-token.md`.
- **2026-09-28 (cùng ngày, mới nhất — sau đợt 3)**: Security review đợt 4 (2 điểm), soi thẳng
  vào code `retry_pending_alert_notifications` mới viết ở đợt 3 cùng ngày — cả 2 đúng:
  1. **Medium — 2 lần gọi `retry_pending_alert_notifications` chồng nhau có thể gửi trùng**:
     bản đợt 3 `list()` toàn bộ row `pending` rồi gửi từng row tuần tự, KHÔNG claim nguyên tử —
     task trước chạy quá 120s (chu kỳ beat), gọi tay trùng lúc sweep định kỳ, hoặc nhiều beat
     process đều có thể khiến 2 lần gọi cùng đọc thấy 1 row và cùng gửi. Review còn chỉ ra thêm
     1 khe hở tôi tự phát hiện khi sửa: KHÔNG chỉ 2 lần sweep tranh nhau — chính lệnh gọi GỐC
     (`_dispatch_notifications` từ `_fire_alert`/`_resolve_alert`) cũng có thể bị sweep tranh
     mất cùng row nếu gửi CHẬM hơn `grace_secs` (90s) — xác nhận có thật với channel `email`:
     Django `send_mail` KHÔNG set `EMAIL_TIMEOUT` (verify: không có key này trong
     `config/settings/*`) nên SMTP server treo có thể giữ lâu hơn 90s; `telegram`/`webhook` có
     `timeout=10` nên rủi ro thấp hơn. Fix: **claim bằng UPDATE có điều kiện**
     (`.filter(status="pending").update(status="processing", ...)` — chỉ 1 caller nhận số dòng
     >0) áp dụng ở CẢ 2 nơi (`_dispatch_notifications` lẫn `retry_pending_alert_notifications`),
     không chỉ ở sweep. Thêm field `AlertNotification.updated_at` (`auto_now=True`, migration
     `0007`) để track "lúc claim" — cần vì `sent_at` (`auto_now_add`) chỉ set lúc tạo, không đổi
     khi claim. Thu hồi row `processing` bị bỏ rơi (worker chết SAU khi claim, TRƯỚC khi kịp
     gửi): sweep còn quét thêm `status="processing"` cũ hơn `stale_processing_secs=300s` (dài
     hơn hẳn `grace_secs` vì 1 tiến trình đang gửi thật hợp lệ có thể mất vài chục giây, nhất là
     email không timeout) — claim lại bằng UPDATE re-check `stale_cutoff` CỐ ĐỊNH (tính 1 lần
     trước vòng lặp) NGAY TRONG WHERE của chính câu UPDATE, không chỉ ở bước chọn candidate —
     đây là phần mấu chốt: 2 lần claim chồng nhau trên cùng 1 row `processing` cũ, lần đến sau
     (dù bị DB khoá row chờ lần đầu commit) re-evaluate điều kiện `updated_at < stale_cutoff`
     trên dữ liệu MỚI COMMIT (đã update updated_at=NOW()) → không còn khớp → tự bỏ qua.
  2. **Low — query sweep định kỳ (mỗi 120s) chưa có index cho `(status, sent_at)`**: đúng,
     full-table scan khi lịch sử `AlertNotification` lớn dần. Fix: thêm 2
     `models.Index` — `(status, sent_at)` (đúng đề xuất, phục vụ nhánh quét "pending") và
     `(status, updated_at)` (phục vụ nhánh quét "processing" bị kẹt, cũng mới thêm ở fix #1).
  6 test mới (`test_double_claim_second_caller_skips`, `test_fresh_processing_row_not_touched`,
  `test_stale_processing_row_gets_reclaimed` + 3 test cũ vẫn giữ nguyên hành vi) — 458 test pass
  (455+3 mới), 2 skip như cũ. Bài học (đã ghi `/deploy` skill): tự viết fix cho 1 báo cáo review
  không có nghĩa fix đó miễn nhiễm — code mới viết trong ngày đã bị review tiếp lần nữa và bắt
  đúng 1 bug cùng loại với bug đang sửa (đọc-sửa-ghi không khoá), cho thấy giá trị của việc để
  review độc lập soi lại code mới thay vì tự tin là đã xong.
- **2026-09-28 (cùng ngày, sau đợt Medium)**: Security review đợt 3 (3 điểm), cả 3
  verify đúng bằng bằng chứng thật:
  1. **High — TLS private key lọt vào Docker image (ĐANG XẢY RA THẬT trên prod, không phải lý
     thuyết)**: `.dockerignore` đợt trước loại `.env`/DB/backup nhưng CHƯA loại `nginx/certs/`
     — `Dockerfile` `COPY . .` vẫn gom `server.key` (nginx dùng bind-mount riêng, image
     `nginx:alpine`, KHÔNG build từ `Dockerfile` này, nhưng `app`/`worker`/`beat` build từ
     Dockerfile đó lại vô tình COPY luôn thư mục `nginx/` cùng cấp). Verify SSH trên
     `monitorsrv`: `docker compose exec app ls /app/nginx/certs/` **xác nhận `server.key` (mode
     600, key thật) đã nằm sẵn trong container `app` đang chạy** trước khi fix — ai có quyền
     `docker compose exec` vào app/worker/beat (rộng hơn nhóm quản lý cert) đọc được thẳng key.
     Fix: thêm `nginx/certs/`, `*.key`, `*.pem` vào `.dockerignore`; rebuild xoá khỏi image mới,
     verify lại bằng đúng lệnh trên (rỗng sau fix).
  2. **High — DOM XSS tại thuộc tính `title` chưa đóng hết**: `esc()` (discovery.html +
     topology.js) dùng trick `div.textContent` → đọc lại `div.innerHTML` — chỉ escape
     `&`/`<`/`>` (đủ an toàn khi chèn vào TEXT CONTENT) nhưng KHÔNG escape `"`/`'` (2 ký tự này
     không có ý nghĩa đặc biệt trong text content nên trình duyệt không encode khi serialize
     lại — đã verify bằng Node: payload `x" onmouseover="alert(1)` qua `esc()` cũ giữ nguyên
     dấu `"`). `discovery.html` chèn `esc(dev.sys_descr)` vào `title="${...}"` (dữ liệu SNMP từ
     thiết bị quét — ngoài tầm kiểm soát server) → payload trên thoát khỏi `title=`, tự thêm
     thuộc tính `onmouseover="alert(1)"` mới trên `<td>` → XSS thật khi rê chuột. `topology.js`
     có cùng lỗi ở `href="${esc(d.detail_url)}"` nhưng `detail_url` hiện do server sinh qua
     `reverse()` nên chưa khai thác được — vẫn sửa vì cùng root cause. Fix: đổi `esc()` sang
     escape đầy đủ `&<>"'` bằng regex (không dùng DOM round-trip nữa) — đúng pattern đã dùng ở
     `wlan_detail.html`. Verify lại bằng Node: payload trên qua `esc()` mới ra
     `x&quot; onmouseover=&quot;alert(1)` — không thoát được attribute nữa.
  3. **Medium — có thể mất thông báo RECOVERED (và về lý thuyết cả FIRE) nếu worker chết giữa
     lúc commit trạng thái Alert và lúc gửi notification thật**: `_resolve_alert` (đợt fix
     Medium trước) cố ý đổi thứ tự "commit is_active=False TRƯỚC, gửi SAU" để chặn race gửi
     trùng — nhưng tác dụng phụ là nếu worker bị kill đúng lúc giữa 2 bước, alert đã
     `is_active=False` nên vòng eval sau coi như "đã xử lý xong", không bao giờ tự gửi lại →
     mất RECOVERED vĩnh viễn, không có gì để retry. `_fire_alert` vốn đã có cùng cấu trúc
     "commit trước, gửi sau" từ trước (không phải do đợt fix trước gây ra) nên mang cùng rủi ro
     với thông báo FIRE — sửa đối xứng cả 2 theo nguyên tắc đã ghi trong `/deploy` skill. Fix:
     **transactional-outbox-lite** — thêm field `AlertNotification.kind` (`fire`/`recovery`,
     migration `0006_alertnotification_kind`); `_fire_alert`/`_resolve_alert` ghi
     `AlertNotification(status="pending")` cho từng channel **CÙNG transaction** với lúc commit
     trạng thái Alert (bằng chứng "còn nợ gửi" luôn tồn tại ngay cả khi crash ngay sau commit);
     gửi thật xong UPDATE row đó thành `sent`/`failed` (không tạo row mới, tránh trùng). Task
     Celery mới `retry_pending_alert_notifications` (`apps/alerts/tasks.py`, beat mỗi 120s, nhớ
     cả `expires` lẫn `expire_seconds` — đúng bẫy đã ghi ở mục "Celery Beat") quét row
     `status="pending"` cũ hơn `grace_secs=90s` (tránh đua với 1 lần gửi đang chạy hợp lệ) và
     gửi lại — retry đúng loại (`kind`) đã lưu tường minh, không suy đoán lại từ `is_active` tại
     thời điểm retry (có thể đã đổi lần nữa). 6 test mới (`tests/alerts/test_notification_outbox.py`)
     mô phỏng trực tiếp "row pending bị kẹt" (tạo tay, không qua fire/resolve — đúng như DB sẽ
     trông thế nào sau SIGKILL) chứ không cố dựng lại kịch bản crash thật.
  455 test pass (451+6 mới — 2 TestFireLeavesNoLeftoverPending/TestResolveLeavesNoLeftoverPending
  + 4 TestRetryPendingAlertNotifications), 2 skip như cũ.
- **2026-09-28 (cùng ngày, sau iLO)**: Security review theo báo cáo ngoài (4 điểm), cả
  4 đều verify đúng bằng bằng chứng thật rồi mới fix (không sửa mù):
  1. **Critical — secret/dữ liệu vận hành đóng gói vào Docker image**: `Dockerfile` `COPY . .`
     không có `.dockerignore` → build context chứa `.env`/`.env.production`/`db.sqlite3`/
     `backups/`/`.git/`/`venv/`/`scratchpad/`. Thêm `.dockerignore` loại trừ secret + VCS + venv +
     dữ liệu vận hành (không đổi `Dockerfile`).
  2. **High — service `db` và `app` nhận 2 bộ credential Postgres khác nhau**: xác nhận **đang
     tồn tại thật trên prod** qua SSH + `docker compose config` (không chỉ đọc code đoán) —
     `POSTGRES_PASSWORD` resolve về default `change_me_db_password`, khác hẳn `DB_PASSWORD` thật.
     Root cause: `${DB_USER}`/`${DB_PASSWORD}` trong `docker-compose.yml` là nội suy cấp Compose
     (chỉ đọc file `.env`, server không có), không đọc qua `env_file: .env.production`. Hiện chưa
     lộ triệu chứng vì Postgres chỉ áp `POSTGRES_*` lúc init volume rỗng lần đầu — sẽ vỡ khi tái
     tạo volume (disaster recovery/migrate host). Fix: bỏ `environment: ${...}`, service `db` đọc
     thẳng `POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD` qua `env_file` (3 khoá mới, mirror
     `DB_NAME/DB_USER/DB_PASSWORD`, thêm vào `.env`/`.env.production`/`.env.example`); healthcheck
     đổi sang `$$POSTGRES_USER` (nội suy trong container, không phải Compose). Chi tiết đầy đủ +
     cách verify: `/deploy` skill mục bẫy `${VAR}` vs `env_file`. ✅ **Đã deploy prod + verify
     sống**: `db-1` tự recreate (đổi `env_file`), lên `Healthy`; SSH xác nhận
     `POSTGRES_PASSWORD == DB_PASSWORD thật` (trước đó `False`); `Device.objects.count()` qua
     container `app` mới trả đúng 23 — chứng minh app thật sự query được DB vừa recreate, không
     chỉ postgres process lên. Commit `4e1447e`.
  3. **High — DOM XSS**: `discovery.html` (kết quả AJAX scan: hostname/sys_descr từ reverse
     DNS+SNMP của thiết bị quét được) và `topology.js` (`showPanel`: mac/ip/switch_name/location từ
     LLDP/FDB/SNMP) ghép thẳng vào `innerHTML` không escape — dữ liệu này đến từ thiết bị ngoài
     mạng, không phải server sinh ra, nên thiết bị/host độc hại có thể chèn HTML/script chạy trong
     phiên admin. Fix: thêm hàm `esc()` (tạo `<div>`, gán `textContent`, đọc lại `innerHTML`) —
     đúng pattern đã có sẵn ở `topology_links.js`/`wlan_detail.html`, áp cho mọi field nghi vấn;
     `discovery.html` thêm `encodeURIComponent()` cho query string link Import.
  4. **High — CIDR lớn có thể OOM/treo worker**: `device_discovery_scan`
     ([apps/devices/views.py](apps/devices/views.py)) từng `list(network.hosts())` rồi mới so
     `len(ips) > max_ips` → nhập nhầm `/8` hoặc IPv6 `/64` tạo hàng triệu/tỷ string trước khi kịp
     từ chối. Fix: so `network.num_addresses` (O(1), không duyệt) TRƯỚC khi materialize danh sách.
     Test regression thêm (`test_scan_huge_subnet_rejected_fast`/`test_scan_ipv6_subnet_rejected_fast`,
     assert trả lời <2s) — 448 test pass (446+2 mới), 2 skip như cũ.
- **2026-09-28 (cùng ngày, mới nhất — sau đợt security review đầu)**: User dán tiếp báo cáo review
  đợt 2 (4 điểm mức Medium), verify từng cái bằng đọc code thật (không đoán) — cả 4 đều đúng:
  1. **Migrate/collectstatic chạy đồng thời ở cả 3 container**: [entrypoint.sh](entrypoint.sh)
     chạy `migrate`/`sync_beat_expires`/`collectstatic --clear` **vô điều kiện** trước khi mới xét
     có `command:` riêng hay không — app/worker/beat dùng chung `ENTRYPOINT` này
     ([Dockerfile](Dockerfile) dòng `ENTRYPOINT`) nên `deploy.sh`/`docker compose up -d --build app
     worker beat` khiến 2-3 container cùng migrate + cùng `collectstatic --clear` gần như đồng
     thời (đua tranh schema; nginx đọc `static_volume` có thể trúng khoảng trống giữa lúc 1
     container đang `--clear` còn container khác đang ghi lại). Fix: chuyển migrate/collectstatic
     vào nhánh **KHÔNG có `command:` riêng** (chỉ container `app` chạy — worker/beat luôn có
     `command: celery ...` trong `docker-compose.yml` nên đi thẳng `exec "$@"`, không migrate
     nữa). Kèm theo: thêm `depends_on: app: condition: service_healthy` cho `worker`/`beat` trong
     `docker-compose.yml` — đảm bảo chúng chỉ khởi động SAU khi `app` đã migrate xong + gunicorn
     lên (healthcheck `/health/` pass), tránh worker/beat chạy task trước khi schema kịp migrate.
     Nếu migrate lỗi → container `app` exit → `worker`/`beat` không bao giờ start (compose báo lỗi
     rõ ràng ở bước deploy thay vì âm thầm cho worker chạy trên schema cũ).
  2. **`_resolve_alert` có thể gửi trùng thông báo RECOVERED**: alert được eval từ CẢ inline sau
     mỗi poll (`_poll_device_once`) LẪN `evaluate_alert_rules` định kỳ (safety net) — 2 đường có
     thể chạy gần như đồng thời trên các Celery worker khác nhau (`--concurrency=4`).
     [_resolve_alert](apps/alerts/engine.py) bản cũ đọc `alerts_to_resolve` (không lock) → gửi
     notification → MỚI update `is_active=False` — 2 lời gọi trùng thời điểm cùng đọc thấy
     `is_active=True`, cùng gửi Telegram/email trùng lặp (bất đối xứng với `_fire_alert` — hàm đó
     ĐÃ có `select_for_update()` khoá `Device` từ trước). Fix: áp cùng pattern khoá — "claim"
     atomically (lock `Device` + update `is_active=False` TRƯỚC trong `transaction.atomic()`) rồi
     MỚI gửi notification ngoài transaction (không giữ lock trong lúc chờ HTTP Telegram/email);
     lời gọi thứ 2 tới sau sẽ thấy `is_active` đã `False` (đã commit) → tự return, không gửi lại.
  3. **`alert_acknowledge` thiếu kiểm tra RBAC**: [alert_acknowledge](apps/alerts/views.py) chỉ
     `@login_required`, không gọi `_can_write` như `rule_create`/`rule_edit`/`storage` cùng file
     — trái mô hình RBAC "Read-Only Operators chỉ xem" (xem mục "RBAC" ở trên). Fix: thêm check
     `_can_write` → 403 nếu không có quyền, cùng pattern đã dùng ở các view khác trong file. Test
     regression `test_alert_acknowledge_forbidden_for_readonly`.
  4. **`_update_link` xoá link cũ TRƯỚC khi validate link mới**:
     [_update_link](apps/dashboard/topology_links_api.py) xoá `existing` (dòng cũ) rồi mới gọi
     `_create_link(request)` — hàm này tự validate (thiếu field, switch đích không tồn tại, nối
     chính nó...) và trả `JsonResponse` lỗi (không raise exception) nên request sai làm **mất
     link cũ vĩnh viễn** mà không tạo được link thay thế. Fix: bọc xoá+tạo trong
     `transaction.atomic()`, nếu `_create_link` trả `status_code != 200` thì
     `transaction.set_rollback(True)` — khôi phục nguyên link cũ. Test regression
     `test_update_with_invalid_body_keeps_existing_link` (file mới
     `tests/dashboard/test_topology_links_api.py`).
  449 test pass, 2 skip như cũ (con số chạy thật qua `pytest`, không cộng nhẩm — 3 test mới thêm
  đợt này nhưng chưa đối chiếu chính xác baseline trước đó là bao nhiêu).
- **2026-09-28 (cùng ngày, mới nhất)**: Thêm iLO Redfish — RAID/disk health cho HyperV host, độc
  lập hoàn toàn WinRM (model `HardwareHealth`, collector `ilo_redfish.py`, task `poll_all_ilo`, 3
  alert rule RAID). Xem mục "iLO Redfish" ở trên + memory `ilo-raid-monitoring.md`. Verify sống
  live-fire trên RAID Hyperv-02 đang lỗi thật: alert fire + Telegram gửi thành công ngay vòng poll
  đầu. Bắt được 2 bug ở bước verify trước khi code thật (ghi vào `/deploy` skill để dùng chung):
  (1) `GenericIPAddressField.exclude(field="")` sinh SQL luôn `NULL`, loại bỏ mọi row; (2) HPE
  iLO4/5 đóng TCP connection sau đúng 1 request dù keep-alive. Commit `2fddc44` (+ `78d71e5` bước 1
  thêm field nhập liệu). ⚠️ Hyperv-01 còn 401 Unauthorized (credential iLO sai) — chưa fix.
- **2026-09-28 (cùng ngày, sau đó)**: Fix alert `duration_min>0` không tự resolve dù metric đã hồi
  phục (11/13 alert active kẹt tới 95 ngày) — xem mục "Online/offline" ⚠️ Rule `duration_min>0` ở
  trên + memory `alert-sustained-never-resolve.md`. Commit `d258bec`. Verify runtime: 11 alert tự
  resolve đúng (gửi Recovered trễ), 2 alert Hyperv-02 latency đúng vẫn active (RAID vẫn còn thật).
- **2026-09-28**: Fix `poll_all_hyperv` hard-kill liên tục sau khi fleet HyperV tăng lên 3 host
  (thêm Hyprver03) trong khi Hyperv-02 đang có sự cố RAID thật ([[hyperv02-winrm-instability]] —
  không phải memory link, xem file cùng tên) khiến batch vượt hard time_limit cũ (110s, tính cho
  2 host) → SIGKILL cả task → Hyprver03 báo Offline giả (10:08-10:09). Nâng
  `POLL_HYPERV_BATCH_SOFT_LIMIT/HARD_LIMIT` 100/110→250/270, `POLL_HYPERV_INTERVAL_SECS` 120→300.
  Deploy commit `14b8f7a`, verify 2 vòng poll sau deploy không hard-kill. Xem mục "HyperV Host
  Performance Counters" ⚠️ Batch ở trên + memory `poll-queue-snowball-slow-device.md`.
- **2026-07-11**: Audit cách lấy CPU/RAM toàn bộ switch (yêu cầu user "review lại"). Verified-đúng:
  Cisco IOS classic, Cisco Business/SMB, Huawei VRP (khớp mọi note đã có). Đã sửa phần verify được
  ngay (không cần thiết bị thật): (1) **gỡ bỏ vendor "HP/Aruba"** khỏi `Device.VENDORS` + nhánh
  detect H3C/Comware (`switch_snmp.py`) + `NETMIKO_DRIVER`/`COMMANDS["hp"]` (`switch_ssh.py`,
  migration `0019_alter_device_vendor`) — code chỉ từng support HP-Comware (H3C rebrand), KHÔNG
  phải ArubaOS thật; nhãn gộp 2 hãng khác nhau dưới 1 lựa chọn dễ khiến ai thêm switch Aruba thật
  rơi vào silent-fail (auto-detect không dùng field `vendor` nên fallback `cisco_ios` → cpu=mem=0,
  không log warning). Không có thiết bị thật nào dùng vendor này (verify DB: 0 record) → xoá thẳng
  thay vì sửa nhãn, đúng theo hướng dẫn "thiết bị chưa có thật thì bỏ, có thiết bị thật code sau".
  (2) Sửa `tests/collectors/test_switch_snmp.py` — test Huawei entity-table dùng đúng số cột thật
  (`.5`=CPU/`.7`=Mem) thay vì `.6`/`.5` (vô tình mô phỏng lại đúng cặp số đã biết là SAI trong lịch
  sử dự án, dễ gây hiểu lầm cho người đọc sau dù không phải bug chức năng). (3) Đính chính mô tả
  thuật toán chọn entity Huawei (xem "OID đã xác minh runtime" → Huawei) — code chọn theo CPU cao
  nhất, không lọc theo tên "MPU Board" như comment cũ mô tả. (4) Research (chưa code, chưa có IOS-XE
  thật): `CISCO-ENHANCED-MEMPOOL-MIB` là MIB đúng thay cho `CISCO-MEMORY-POOL-MIB` 32-bit trên
  IOS-XE RAM lớn, nhưng INDEX kép `{entPhysicalIndex, cempMemPoolIndex}` nên không thể hardcode như
  MIB cũ — chờ thiết bị thật để walk+chọn entry đúng cách (xem mục Cisco IOS-XE). (5) Thêm comment
  giải thích hành vi ngầm định pysnmp `NoSuchInstance`/`NoSuchObject` → `str()` ra chuỗi rỗng (đã
  verify runtime pysnmp 7.1.27) tại [snmp_client.py](apps/collectors/snmp_client.py) — toàn bộ
  pattern `float(x or 0)` trong collector dựa vào hành vi này, không phải bug nhưng dễ vỡ nếu ai đổi
  sang `.prettyPrint()`. Chưa đụng: SSH-path `_parse_cisco_mem` (regex nghi ngờ không khớp output
  thật `show processes memory` — 0 thiết bị hiện dùng `protocol=ssh` trong DB nên chưa có real data
  để verify, để lại chờ có thiết bị SSH thật).
- **2026-07-11 (cùng ngày, tiếp audit trên)**: User xác nhận gỡ luôn **MikroTik** và **Fortinet**
  theo đúng cách đã làm với HP/Aruba — 2 vendor này có code + OID profile đầy đủ
  (`oids/mikrotik_routeros.yaml`, `oids/fortinet_fortios.yaml`, adapter riêng) nhưng chưa từng xuất
  hiện trong mục "OID đã xác minh runtime" ở trên (chưa có thiết bị thật để verify), và không có
  device nào dùng 2 vendor này trong DB (local: 0 record; không kết luận được cho prod nhưng user đã
  xác nhận xoá). Phạm vi xoá **rộng hơn HP/Aruba nhiều** vì đây là support đầy đủ (không chỉ 1 nhánh
  detect):
  - `Device.VENDORS` (migration `0020_alter_device_vendor`), nhánh `detect_os_family` +
    `_collect_cpu_mem_mikrotik`/`_collect_cpu_mem_fortinet` + dispatch trong `collect_raw()`
    ([switch_snmp.py](apps/collectors/switch_snmp.py)).
  - `NETMIKO_DRIVER`, `COMMANDS`, `detect_os_family` shortcut, toàn bộ parser
    (`_parse_mikrotik_resource/_interfaces`, `_parse_fortinet_perf/_uptime/_interfaces`) + dispatch
    trong `collect_raw()` ([switch_ssh.py](apps/collectors/switch_ssh.py), −184 dòng).
  - Xoá hẳn `apps/collectors/adapters/mikrotik_routeros.py`, `fortinet_fortios.py` +
    registry trong `adapters/__init__.py`, `oids/mikrotik_routeros.yaml`, `oids/fortinet_fortios.yaml`.
  - **`fw_session_count`** (alert metric) xoá theo dây chuyền — field này **chỉ Fortinet từng ghi**
    (`extra["session_count"]`, không hãng nào khác đụng tới), nên sau khi bỏ Fortinet, giữ lại
    `fw_session_count` trong `METRIC_CHOICES`/`AlertRule.metric_label` sẽ tạo ra **dropdown option
    không bao giờ có dữ liệu** — đúng loại "silent-fail trap" mà bẫy HP/Aruba đã cảnh báo, nên xoá
    chứ không giữ. Xoá `_fw_session_count`/`_sustained_fw_session_count` +
    `METRIC_GETTERS`/dispatch/format trong [engine.py](apps/alerts/engine.py); entry
    "Firewall Sessions High" trong `seed_alert_rules.py` (⚠️ nếu prod đã từng chạy lệnh seed này,
    AlertRule row cùng tên có thể vẫn còn trong DB thật — seed command không tự xoá rule cũ, cần
    kiểm tra/xoá tay qua `/alerts/rules/` nếu tồn tại). Kéo theo xoá luôn UI/API hiển thị
    `session_count` (thẻ "Sessions" + Chart.js dataset trong `firewall_detail.html`,
    `_attach_session_count()` trong `metrics/api.py`, extraction trong `dashboard/views.py`,
    write-side `sc` key trong `writer.py::_device_scalar_sample` — giữ lại sẽ là field ghi ra mà
    không ai đọc, tương tự lý do xoá `fw_session_count`). `SystemHealth.extra` (JSONField) **giữ
    nguyên** — đây là kho generic dùng chung cho `vms`/`wifi_aps`/`wifi_clients`, không phải code
    riêng Fortinet.
  - Test: xoá `MikroTik*/Fortinet*DeviceFactory` + fixtures (`conftest.py`), test adapter/factory
    riêng 2 vendor; thay device_type=router/firewall trong test bằng Cisco/Huawei (Huawei mirror
    đúng firewall thật USG6525E trong fleet) để không mất coverage dispatch theo `device_type`.
    424 passed, 2 skipped (không đổi so với trước, cùng lý do skip easysnmp).
- **2026-07-11**: Fix CPU Synology NAS báo sai (53-54% giả, DSM thật ~1-4%) — phát hiện qua
  đối chiếu ảnh chụp DSM Resource Monitor thật của user. Root cause: `ssCpuIdle` trên DSM
  không theo chuẩn UCD-SNMP-MIB (verify runtime User+System+Idle=48, phải ≈100). Fix: chuyển
  sang RAW counter delta 2 lần poll (xem mục "OID đã xác minh runtime" → Synology DSM). Module
  mới `apps/collectors/cpu_state.py` (Redis scratch state). `cpu_idle` giữ làm fallback.
- **2026-07-07**: Audit lại poll HyperV: fix `poll_all_hyperv` thiếu soft/hard `time_limit` batch
  (thêm 100s/110s, xem mục "HyperV Host Performance Counters" ⚠️ Batch); fix root cause
  `expire_seconds` bị `beat` reset mỗi lần restart (xem mục "Celery Beat" ở trên) — thêm key
  `expire_seconds` vào `CELERY_BEAT_SCHEDULE` cho 5 periodic task (poll-all-hyperv/network/ping,
  evaluate-alert-rules, discover-topology-links).
- **2026-07-07**: Bổ sung 4 chỉ số per-volume (`Current Disk Queue Length`, `Disk Transfers/sec` suy ra bằng cộng thay vì query, `Split IO/sec`, `% Idle Time`) sau khi đối chiếu danh sách counter chuẩn Windows `LogicalDisk` với code hiện có. `PS_SCRIPT_VOLUME` nén thêm (biến prefix `$dp`, regex rút gọn, field JSON viết tắt `cql/tps/sio/idt`) để giữ dưới giới hạn base64 8191. Migration `0008_volumestats_current_queue_length_and_more`.
- **2026-07-07**: Thêm disk throughput/queue/io-size (4 metric) + per-volume disk stats mapped theo VM (model `VolumeStats`). PS_SCRIPT tách 2 script (host+volume) do vượt giới hạn base64 khi gộp chung. Fix bug helper function `R` trùng alias PowerShell `Invoke-History`. Migration `0007_systemhealth_avg_io_size_kb_and_more`. Commit `21da8e0`.
- **2026-07-07**: Thêm 7 HyperV host performance counter (xem mục "HyperV Host Performance Counters" ở trên). Migration `0006_systemhealth_cpu_hv_percent_and_more`. `POLL_HYPERV_INTERVAL_SECS` 300→120. Commit `3bc46a4`.
- **2026-07-02**: **SNMP walk chuyển sang getBulk** (pysnmp `bulk_cmd`, `max_repetitions=25`) thay vì getNext tuần tự. PFVN_Router giảm **40s → 8s** (−80%), CORE 13s→6s, ACL_Wlan 20s→3s. Wall-clock cả cycle 20 thiết bị: **56.5s → ~30s**. SNMPv1 fallback getNext. Commit `bab0aa8`.
- **2026-07-02**: Nâng SNMP polling interval **60s → 90s** (trước getBulk, wall-clock ~56.5s/60s quá sát). Kèm theo: `ALERT_EVAL_INTERVAL_SECS` 60→90, `ALERT_GRACE_PERIOD_SECS` 90→135, `METRICS_SERIES_MAX_SAMPLES` 1500→1000. Sau getBulk wall-clock ~30s — có thể hạ lại 60s nếu cần.
- **2026-07-02 (trước đó)**: Hạ SNMP polling interval **120s → 60s** (fleet ≤30 thiết bị, 4 Celery workers đủ throughput). Kèm theo: `ALERT_EVAL_INTERVAL_SECS` 120→60, `ALERT_GRACE_PERIOD_SECS` 120→90 (1.5× interval), `METRICS_SERIES_MAX_SAMPLES` 800→1500 (giữ 24h chart @60s). Các setting đọc từ env, override qua `.env` nếu cần rollback. Commit `0e4258c`.

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
| [apps/collectors/tasks.py](apps/collectors/tasks.py) | `_poll_device_once`, online/ICMP, clear `last_seen` khi offline, publish SSE, `poll_all_ilo` |
| [apps/collectors/ilo_redfish.py](apps/collectors/ilo_redfish.py) | `IloRedfishClient` — RAID/disk health qua iLO Redfish (HyperV, độc lập WinRM) |
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
