# Thiết kế B — cảnh báo uplink theo từng cổng

Trạng thái: **ĐÃ CHỐT HƯỚNG (2026-10-16, 5 quyết định của người vận hành) — ĐÃ TRIỂN KHAI VÀ TEST LOCAL
(working tree, CHƯA commit/push/deploy).** Hướng A (phạm vi giám sát `MonitoredUplink`) đã deploy prod (HEAD `35f164f`).
Chỉnh sửa bắt buộc khi duyệt triển khai: `duration_min` phải đòi cổng KHÔNG up liên tục đủ cửa sổ (bản A báo DOWN khi
có BẤT KỲ mẫu không-up nào) và hồi phục dùng trạng thái mới nhất — xem 3.2 và test `TestDurationMinMeansDownForTheWholeWindow`
(DB-mode + cache-mode).

Phạm vi: chỉ rule `if_status` ("Switch Uplink Down"). **Hai rule băng thông (`uplink_in_mbps_max`,
`uplink_out_mbps_max`) là một thay đổi RIÊNG, không nằm trong B** — chúng vẫn đọc `Interface.is_uplink`.

Khác bản 1 (sửa lỗi của chính bản 1): bản 1 ghi "RECOVERED dùng lại `alert.message` nên cũng có tên cổng" — **sai**.
Đọc lại mẫu thông báo: RECOVERED của Telegram/email/Slack/Teams chỉ in thiết bị + rule + giờ, KHÔNG in `alert.message`. Nên B phải
sửa cả mẫu RECOVERED (xem 3.4), nếu không tin hồi phục không nói cổng nào.

## 1. Vấn đề (đo/đọc code, không suy đoán)

| # | Hiện trạng sau A | Bằng chứng |
|---|---|---|
| 1 | Chỉ MỘT alert cho mỗi (thiết bị, rule). Cổng thứ hai rớt khi alert đang active chỉ được ghi thêm vào `Alert.down_interface_ids`, **không có thông báo** | `_fire_alert` thấy `active` thì `return`; `_extend_if_status_causes` chỉ `UPDATE` cột JSON |
| 2 | Hồi phục chỉ khi MỌI cổng giám sát up (`_reduce_if_status` trả 0.0 nếu có bất kỳ cổng down). Cổng 1 hồi phục mà cổng 2 còn down: **không có tin nào** | `_reduce_if_status`, `_evaluate_if_status` |
| 3 | Tin nhắn **không nêu cổng nào**. 4 mẫu thật trên prod: `X3_SW1_GiuaX3_CUT: if_status = DOWN (ngưỡng eq DOWN)` | `Alert.message`, đọc 2026-10-16 |
| 4 | Chống flapping gộp mọi cổng của thiết bị: 1 cổng nhấp nháy làm chặn thông báo cổng khác | `_is_flapping(device, rule)` đếm theo (device, rule) |
| 5 | `down_interface_ids` chỉ **lớn lên** trong đời alert, không cho biết cổng nào đang down *lúc này* | `_extend_if_status_causes` chỉ hợp tập |

Mức độ ảnh hưởng (đo prod 2026-10-16): chỉ 4/17 switch có >1 cổng giám sát — `CORE` 8, `X1_SW1_CuoiX1_ServerU_ServerRoom` 2,
`X3_SW1_GiuaX3_CUT` 2, `X4_SW1_DauX4_20U` 2. Lịch sử `if_status`: 20 alert / ~3,5 tháng (28 fire `sent`, 1 `failed`).
**Lợi ích thực của B là chất lượng thông tin (biết cổng nào, cổng thứ hai không bị im, hồi phục từng phần), không phải
giảm số alert.**

## 2. Mục tiêu / không phải mục tiêu

Mục tiêu: (a) mỗi cổng giám sát có vòng đời ALERT → RECOVERED riêng; (b) mọi tin (cả ALERT lẫn RECOVERED) nêu tên cổng,
kèm tên thiết bị đối diện khi biết CHẮC; (c) flapping, nhắc-lại, đóng do đổi phạm vi đều tính theo từng cổng; (d) không
thể có 2 alert active cho cùng (thiết bị, rule, cổng) — được DB bảo đảm, không chỉ nhờ khoá.

Không làm trong B: gộp tin nhắn, suppress theo topology, giám sát thành viên Eth-Trunk, rule băng thông, đổi cơ chế
outbox/retry, xoay log, release Pha A.

## 3. Thiết kế

### 3.1 Dữ liệu (migration chỉ thêm, không đụng dữ liệu cũ)

Bảng `Alert`, 3 cột mới + 1 ràng buộc:

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `interface` | `ForeignKey(Interface, null, blank, on_delete=SET_NULL, related_name="+")` | cổng của alert; `NULL` = alert cấp thiết bị (rule khác, hoặc `if_status` kiểu cũ) |
| `interface_name` | `CharField(100, blank, default="", db_default="")` | tên cổng chụp lúc nổ (lịch sử vẫn đọc được khi interface bị xoá) |
| `peer_name` | `CharField(200, blank, default="", db_default="")` | tên thiết bị đối diện chụp **lúc tạo alert**; rỗng = không biết chắc |

- **Ràng buộc** `UniqueConstraint(fields=[device, rule, interface], condition=Q(is_active=True, interface__isnull=False),
  name="uniq_active_alert_per_port")`. Điều kiện `interface IS NOT NULL` nghĩa là: **không áp cho alert cấp thiết bị** —
  mọi rule khác (interface luôn NULL) giữ NGUYÊN ngữ nghĩa hiện tại (1 alert active/(thiết bị, rule) do khoá `Device`).
  Chỉ mục một phần này cũng phục vụ truy vấn "alert active của cổng này"; đếm flapping quét bảng nhỏ nên không thêm chỉ mục.
- `closed_reason` thêm giá trị `superseded` (`Alert.CLOSED_SUPERSEDED`, 10 ký tự < 20).
- **Vẫn ghi `down_interface_ids=[interface.pk]`** trên alert theo cổng ⇒ engine bản A (khi rollback) vẫn hiểu.
- Migration: `ADD COLUMN` nullable/`db_default` + 1 chỉ mục một phần trên ≈1.000 dòng, mọi dòng cũ `interface IS NULL` nên
  không thể vi phạm ràng buộc. Cùng độ nhẹ với `alerts/0011`, `0012`.

### 3.2 Engine — `_evaluate_if_status` đánh giá TỪNG cổng

Giữ nguyên: `_sync_uplink_scope`, phạm vi rỗng ⇒ WARNING và KHÔNG làm gì (kể cả không supersede), các WARNING
không-khớp/mơ-hồ/thiếu-một-phần, cache-mode/DB-mode, lược đồ khoá Device → Alert.

```
scope = _sync_uplink_scope(device); ids = scope.interface_ids
if not ids: WARNING; return                       # alert cũ giữ nguyên, như A
_supersede_device_level_alerts(device, rule)      # 3.3 — một lần, dưới khoá Device
observed = {alert.pk: alert.interface_id  for active per-port alerts}    # chụp TRƯỚC khi đọc trạng thái
statuses = _uplink_statuses(device, ids, since)
for port in ids:                                  # mỗi cổng trong try/except riêng (cô lập như 1.8)
    s = statuses[port]
    if s is None: continue                        # thiếu dữ liệu: không fire, không resolve CỔNG NÀY
    sustain: duration_min>0 → _sustained_if_status(device, window_since, [port]) (0.0/1.0/None)
    action = _decide_transition(rule, value, has_active_for(port))
    fire    → _fire_alert(device, rule, 0.0, interface=port)
    resolve → _resolve_alert(device, rule, interface=port, only_if=pk ∈ observed)
alert per-port active có interface ∉ ids (và pk ∈ observed) → đóng scope_changed, không RECOVERED
```

Hệ quả: cổng thứ hai rớt = alert + thông báo riêng; cổng 1 hồi phục = RECOVERED riêng dù cổng 2 còn down; cổng thiếu dữ
liệu không còn che cổng down khác; lỗi (exception) khi đánh giá 1 cổng không chặn cổng còn lại
(`SoftTimeLimitExceeded` vẫn nổi lên). `only_if` giữ nguyên bài học review ngoài của A: quyết định resolve chỉ áp cho
alert đã thấy trước khi đọc trạng thái.

Mã của A không còn dùng (`_extend_if_status_causes`, `_settle_if_status_alerts`, `_reduce_if_status` cấp thiết bị) bị
**gỡ**, không để 2 đường song song; `_close_alerts_out_of_scope` được viết lại theo từng cổng.

### 3.3 Alert `if_status` cấp thiết bị cũ (quyết định 2)

Trong `_evaluate_if_status`, ngay sau khi phạm vi xác định: dưới khoá Device, mọi alert active của (thiết bị, rule) có
`interface IS NULL` được đóng với `closed_reason="superseded"`:

- `is_active=False`, `resolved_at=now`, ghi lý do vào cuối `message` ("thay bằng alert theo từng cổng — không phải đã hồi
  phục"); **KHÔNG tạo AlertNotification, KHÔNG RECOVERED** (late-recovery đã bỏ qua mọi alert có `closed_reason`).
- Sau đó, **cùng lượt đánh giá**, từng cổng được xét bằng dữ liệu hiện tại và điều kiện sustain của rule: **chỉ cổng thực
  sự lỗi mới tạo alert** (cổng up, cổng thiếu dữ liệu, cổng down chưa đủ `duration_min` ⇒ không alert).
- Hệ quả cần biết: sự cố đang kéo dài sẽ được thông báo lại MỘT lần, theo từng cổng, có tên cổng.
- Đo 2026-10-16: **0 alert `if_status` đang active** ⇒ trên prod hiện tại bước này không làm gì; kiểm lại ngay trước
  deploy. Giới hạn: fire đang `pending` của alert cũ vẫn có thể được gửi sau khi đóng (như khi tắt rule) — chỉ xảy ra nếu
  lúc deploy còn alert cũ active.

### 3.4 Nội dung thông báo (quyết định 4)

- **Tên thiết bị đối diện** (`peer_name`): chỉ khi có `TopologyLink(local_device=thiết bị, link_kind="switch",
  is_stale=False, remote_device IS NOT NULL)` mà `local_interface_id` (hoặc `local_port` trùng tên, không phân biệt
  hoa/thường) **khớp đúng cổng**. Không suy từ link của thiết bị khác, không đoán từ thứ tự/tốc độ.
  Đo prod 2026-10-16: **16/27 cổng giám sát có tên đối diện** (toàn bộ cổng topology phía cha); **11/27 không có** — đúng
  11 cổng thủ công ở switch lá, vì link được lưu phía CHA và `remote_port_id` rỗng (suy từ FDB). Thiếu tên ⇒ vẫn cảnh báo
  đầy đủ, chỉ không có phần "nối tới …".
- **Ghi nhận lúc tạo**: `peer_name` và `interface_name` được tính trong `_fire_alert` và lưu thẳng vào `Alert`, cùng
  transaction với việc tạo alert/thông báo. Outbox/retry chỉ ĐỌC các cột này (không tra topology lúc gửi) ⇒ nội dung
  không đổi dù link đổi/stale trong lúc retry (kể cả sau worker chết, sweep 60s, nhắc lại flapping).
- **ALERT**: `Alert.message` = `{thiết bị}: cổng {tên cổng} DOWN` + ` — nối tới {peer_name}` nếu có.
- **RECOVERED** (4 mẫu: `send_telegram_recovery`, `send_email_recovery`, `send_slack_recovery`, `send_teams_recovery` —
  đã đọc code: cả 4 chỉ in thiết bị + rule + giờ hồi phục): thêm dòng `Cổng: {interface_name}` + `Nối tới: {peer_name}`
  khi alert có `interface_name`; alert cấp thiết bị (rule khác) in y như cũ — kiểm bằng test đặc trưng. Prod chỉ dùng
  Telegram (32 rule đều `["telegram"]`), 3 mẫu còn lại sửa cho đồng nhất, không có bằng chứng sống.
- Trang Alerts/lịch sử: hiện cổng; nhãn mới cho `superseded` cạnh nhãn `rule_disabled`/`scope_changed`; admin
  (`readonly_fields`), `AlertSerializer` (API) thêm `interface_name`, `peer_name`.

### 3.5 Tạo alert, chống trùng, chống spam theo cổng

- `_fire_alert(device, rule, value, interface=None)`: tra alert active theo `(device, rule, interface)` — `interface=None`
  tra `interface__isnull=True` (hành vi cũ cho mọi rule khác). Vẫn dưới khoá `Device`.
- `Alert.objects.create(...)` của nhánh theo cổng nằm trong savepoint; `IntegrityError` do `uniq_active_alert_per_port`
  được coi là "đã có alert active" (log WARNING, không thông báo, không tạo trùng) — lớp bảo vệ thứ hai nếu khoá có sơ hở.
- `_is_flapping` và phép đếm trong `_queue_renotify_if_suppressed` lọc thêm theo cổng ⇒ cổng nhấp nháy không chặn cổng
  khác; nhắc-lại-1-lần (1.1) tính theo từng alert như cũ.
- `_resolve_alert(..., interface=None)` chỉ resolve alert của cổng đó (None = cấp thiết bị như cũ).

### 3.6 Không đổi

`close_alerts_for_disabled_rule` (đóng mọi alert của rule — đã đúng), `_queue_late_recovery_if_resolved`/
`_finalize_fire_sent` (theo từng alert/channel), outbox/retry/cooldown (1.2), `MonitoredUplink`/`_sync_uplink_scope`,
mọi rule không phải `if_status`, rule băng thông, Django admin "Cổng uplink giám sát".

## 4. Migration, rollback

- Rollback = redeploy HEAD trước B. Alert theo cổng còn active có `down_interface_ids=[pk]` nên engine A xử lý như alert
  cấp thiết bị (hồi phục khi mọi cổng giám sát up); cột/ràng buộc mới vô hại với mã cũ (nullable/`db_default`, mã cũ luôn
  `interface IS NULL` nên không chạm ràng buộc).
- Alert đã đóng `superseded` giữ nguyên đã đóng khi rollback (như `scope_changed`).

## 5. Tiêu chí kiểm chứng TRƯỚC khi duyệt deploy (quyết định 5)

Quy trình dự án: test đỏ trên mã A trước, sửa, rồi đột biến. Bắt buộc có:

| # | Kịch bản | Kỳ vọng |
|---|---|---|
| T1 | hai cổng rớt | **2 alert độc lập**, mỗi alert 1 fire theo từng channel, message nêu từng cổng |
| T2 | cổng 1 hồi phục, cổng 2 còn down | RECOVERED **chỉ** cổng 1 (có tên cổng); alert cổng 2 vẫn active; cổng 2 hồi phục sau ⇒ RECOVERED riêng |
| T3 | 2 lượt đánh giá đồng thời (2 connection Postgres thật) cùng cổng | đúng 1 alert active, 1 bộ fire; bỏ khoá (đột biến) ⇒ ràng buộc DB chặn, không có bản trùng |
| T4 | outbox/retry | mỗi (alert, channel): ALERT trước RECOVERED, kể cả khi fire bị 429/retry; cổng B không bị cổng A chặn thứ tự; recovery trễ (resolve khi fire còn pending) vẫn đúng thứ tự |
| T5 | alert cấp thiết bị cũ đang active | đóng `superseded`, 0 AlertNotification, 0 RECOVERED; cổng down đủ sustain ⇒ alert theo cổng; cổng up / chưa đủ `duration_min` / thiếu dữ liệu ⇒ không alert |
| T6 | `peer_name` | link mới khớp đúng cổng ⇒ có tên; link stale / khác cổng / không có link ⇒ không tên mà vẫn cảnh báo; link đổi giữa lúc retry ⇒ nội dung gửi lại giữ NGUYÊN; RECOVERED có cổng + tên |
| T7 | ràng buộc DB | 2 alert active cùng cổng bị từ chối; 1 đã đóng + 1 active được; alert `interface IS NULL` của rule khác không bị ràng buộc (kể cả 2 active cùng (thiết bị, rule) ở tầng DB không đổi) |
| T8 | flapping/nhắc lại theo cổng | cổng A flap không chặn thông báo cổng B; nhắc-lại 1 lần theo alert |
| T9 | đổi phạm vi / interface bị xoá | cổng rời phạm vi ⇒ `scope_changed`, cổng còn lại giữ; interface bị xoá (SET_NULL) ⇒ alert đó bị `superseded` ở lượt sau, không RECOVERED |
| T10 | cô lập lỗi | exception ở 1 cổng không chặn cổng khác; `SoftTimeLimitExceeded` vẫn nổi lên |
| T11 | không đổi rule khác | toàn bộ suite hiện có (978 pass / 2 skip) vẫn xanh; `makemigrations --check` chỉ ra đúng migration mới; ruff không tăng |

Đột biến (mỗi cái phải làm đúng test đỏ): bỏ lọc `interface` ở `_fire_alert`/`_resolve_alert`/`_is_flapping`, bỏ khoá
`Device`, bỏ `only_if`/`observed`, bỏ ràng buộc DB, bỏ savepoint quanh `create`, tra topology lúc GỬI thay vì lúc TẠO, bỏ
`closed_reason=superseded` (⇒ RECOVERED giả), supersede cả khi phạm vi rỗng, sustain toàn thiết bị thay vì theo cổng.

Trước khi duyệt deploy (đợt riêng, **chưa chốt giờ**): đếm alert `if_status` active trên prod, `sqlmigrate` đọc lại SQL,
suite đầy đủ, review diff, CLAUDE.md + `/deploy` skill + memory cập nhật. Sau deploy kiểm theo `/deploy` §4 và bổ sung:
ràng buộc tồn tại (`\d alerts_alert`), `MonitoredUplink` vẫn 27, 0 alert/notification mới nếu mọi cổng up, nếu lúc deploy có
alert cấp thiết bị thì số `superseded` đúng và 0 RECOVERED; nêu rõ những gì vẫn CHƯA quan sát trên prod.

## 6. Giới hạn nói thẳng

1. CORE mất nhiều cổng cùng lúc (line card: tới 8 cổng) ⇒ tối đa 8 tin/vòng (A: 1 tin). Cơ chế cooldown/retry 429 của 1.2
   xử lý giới hạn Telegram; **gộp tin không nằm trong B**.
2. Một cáp đứt vẫn có thể ra 2 alert (hai đầu) + `device_online` của switch lá — B không làm tệ hơn A ở ca này nhưng cũng
   chưa giảm; suppress theo topology là việc riêng.
3. `Eth-Trunk`: chỉ theo dõi cổng logic; thành viên rớt mà trunk còn up thì không báo (giới hạn của A).
4. Cổng topology đã ghim giữ mãi: cáp gỡ hẳn ⇒ alert DOWN cổng đó kéo dài tới khi xoá dòng trong admin.
5. 11/27 cổng (phía lá) không có tên thiết bị đối diện — theo đúng yêu cầu "khớp đúng cổng", không suy luận.
6. A chưa quan sát được ca uplink đứt thật; B được chứng minh bằng test + đột biến + mô phỏng, không bằng sự cố sống.
7. Lần đầu sau deploy, nếu có alert cấp thiết bị đang active, sự cố được thông báo lại theo từng cổng (3.3).

## 7. Quyết định đã chốt (2026-10-16)

| # | Quyết định | Nơi áp dụng |
|---|---|---|
| 1 | B đầy đủ: alert theo từng cổng; băng thông là thay đổi riêng | 2, 3.2 |
| 2 | Alert cấp thiết bị cũ: đóng `superseded`, không RECOVERED; sau đó đánh giá từng cổng theo dữ liệu hiện tại + sustain | 3.3 |
| 3 | Ràng buộc DB duy nhất cho (thiết bị, rule, cổng) đang active; rule cấp thiết bị khác không đổi ngữ nghĩa | 3.1, 3.5 |
| 4 | Tên đối diện khi link còn mới và khớp đúng cổng; ghi lúc tạo, retry giữ nguyên; thiếu vẫn cảnh báo | 3.4 |
| 5 | Deploy là đợt riêng sau review + kiểm chứng (tiêu chí ở mục 5); chưa chốt giờ, chưa duyệt deploy | 5 |

Việc duyệt commit tài liệu, push, deploy: **riêng, từng lần**. Xoay log và release backup: ngoài phạm vi B.
