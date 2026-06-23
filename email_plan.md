# Kế hoạch: Cấu hình email nhận cảnh báo qua UI + rule thiết bị offline

## Context (Vì sao)
Hệ thống **đã có đủ pipeline gửi email**: `evaluate_alert_rules` (Celery beat) duyệt mọi device → `check_device_alerts` → khi điều kiện rule khớp thì `_fire_alert` → `send_email_alert` (SMTP) → log `AlertNotification`. Metric `device_online` (1=ONLINE / 0=OFFLINE) đã có sẵn, dựa trên `Device.is_online` nên fire được **kể cả khi thiết bị mất kết nối**.

**Khoảng trống duy nhất:** người nhận email hiện cứng trong `.env` (`ALERT_EMAIL_RECIPIENTS`), không có ô nhập trên UI. User muốn một ô nhập "mail được chỉ định" để nhận cảnh báo khi thiết bị lỗi.

**Quyết định đã chốt với user:**
1. Email người nhận = **cấu hình chung toàn hệ thống** (singleton, không per-rule).
2. Tài khoản Gmail GỬI đi (SMTP host/user/app-password) **vẫn giữ trong `.env`** — UI chỉ nhập email NGƯỜI NHẬN.
3. **Tự tạo sẵn rule "thiết bị offline → email"** để cảnh báo lỗi thiết bị chạy ngay.

## Phạm vi & thiết kế
Thêm 1 model singleton `AlertConfig` lưu danh sách email người nhận (chỉnh qua trang UI admin-only), cho `email_channel.py` ưu tiên đọc từ DB và fallback `.env`. Seed sẵn rule offline qua data migration.

---

## Các thay đổi

### 1. Model singleton — apps/alerts/models.py
Thêm class `AlertConfig` (pk luôn = 1):
```python
import re
class AlertConfig(models.Model):
    """Singleton (pk=1): cấu hình kênh thông báo chỉnh qua UI."""
    email_enabled    = models.BooleanField(default=True, verbose_name="Bật gửi email")
    email_recipients = models.TextField(blank=True, default="",
        verbose_name="Email nhận cảnh báo",
        help_text="Mỗi địa chỉ một dòng hoặc cách nhau bằng dấu phẩy.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Cấu hình cảnh báo"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def recipient_list(self) -> list[str]:
        parts = re.split(r"[,\n;]+", self.email_recipients or "")
        return [p.strip() for p in parts if p.strip()]
```

### 2. Resolve recipient — apps/alerts/channels/email_channel.py
Thêm helper, thay 2 chỗ lấy `recipients` (trong `send_email_alert` dòng 11 và `send_email_recovery` dòng 43):
```python
def _resolve_recipients() -> list[str]:
    try:
        from apps.alerts.models import AlertConfig
        cfg = AlertConfig.load()
        if not cfg.email_enabled:
            return []
        if cfg.recipient_list():
            return cfg.recipient_list()
    except Exception:
        logger.exception("Đọc AlertConfig lỗi — fallback .env")
    return list(getattr(settings, "ALERT_EMAIL_RECIPIENTS", []))
```
- `send_email_alert`: `recipients = _resolve_recipients()` (giữ nguyên check rỗng + log warning).
- `send_email_recovery`: tương tự.
- Giữ nguyên `from_email=settings.SMTP_FROM` (tài khoản gửi vẫn từ `.env`).

### 3. Form — apps/alerts/forms.py
Thêm `AlertConfigForm(forms.ModelForm)` theo pattern `AlertRuleForm` (widget Bootstrap `form-control`/`form-check-input`):
- fields = `["email_enabled", "email_recipients"]`, `email_recipients` dùng `Textarea`.
- `clean_email_recipients`: split bằng logic `recipient_list()` rồi validate từng địa chỉ qua `django.core.validators.validate_email`, gom lỗi địa chỉ sai.

### 4. View — apps/alerts/views.py
Thêm `notification_config(request)` theo đúng pattern `rule_create`/`device_add` (POST-Redirect-Get + `messages.success`):
- Chặn quyền: dùng decorator `@admin_required` từ apps/accounts/decorators.py (đồng bộ với trang `/users/`), hoặc `_can_write` nếu file đã import sẵn — kiểm tra import hiện có rồi chọn cho nhất quán.
- `cfg = AlertConfig.load()`; `form = AlertConfigForm(request.POST or None, instance=cfg)`; valid → `save()` + message + redirect lại chính trang.

### 5. URL — apps/alerts/urls.py
Thêm: `path("config/", views.notification_config, name="config")`.

### 6. Template — templates/alerts/config.html (mới)
Theo layout card của templates/alerts/rules/form.html: extends `base.html`, breadcrumb, `{% csrf_token %}`, hiển thị `form.errors`, ô `email_recipients` (textarea) + checkbox `email_enabled`, nút Lưu (`btn-primary`). Ghi chú nhỏ: "Tài khoản gửi (SMTP Gmail) cấu hình trong `.env`".

### 7. Sidebar — templates/base.html
Thêm link trong mục **Cảnh báo**, bọc `{% if is_admin_user %}`:
`<a href="{% url 'alerts:config' %}">Cấu hình Email</a>`.

### 8. Admin (tùy chọn) — apps/alerts/admin.py
Đăng ký `AlertConfig` để admin Django cũng sửa được.

### 9. Migration — apps/alerts/migrations/000X_alertconfig_offline_rule.py (mới)
Một migration gồm:
- `CreateModel` cho `AlertConfig`.
- `RunPython` seed rule offline **idempotent**:
  ```python
  AlertRule.objects.get_or_create(
      name="Thiết bị offline",
      defaults=dict(device_type="all", metric="device_online",
                    condition="eq", threshold=0, severity="CRITICAL",
                    duration_min=0, channels=["email"], enabled=True))
  ```
  (reverse = no-op). Dùng `apps.get_model` trong RunPython.

---

## Lưu ý
- **Không** đụng SMTP/`.env` — tài khoản gửi giữ nguyên. Nếu `.env` chưa có `SMTP_*`/Gmail app-password thì email vẫn không gửi được; đây là điều kiện hạ tầng, ngoài phạm vi code.
- Rule offline gửi `email` — nếu danh sách người nhận trong UI rỗng thì `email_channel` tự fallback `.env`; nếu cả hai rỗng → log warning, bỏ qua (đúng hành vi hiện tại).

## Verification
1. `python manage.py makemigrations alerts` (xác nhận migration sinh đúng) → `python manage.py migrate`.
2. `python manage.py check`.
3. `python manage.py runserver`, đăng nhập admin → vào sidebar **Cảnh báo → Cấu hình Email** (`/alerts/config/`), nhập email (vd `hoangtruonghd88@gmail.com`), lưu → thấy message thành công; mở lại trang thấy giá trị đã lưu. Thử nhập email sai định dạng → báo lỗi validate.
4. Xác nhận rule offline tồn tại: `python manage.py shell -c "from apps.alerts.models import AlertRule; print(AlertRule.objects.filter(metric='device_online').values('name','channels','enabled'))"`.
5. Test gửi mail end-to-end bằng console backend (không cần SMTP thật):
   `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend python manage.py shell` rồi gọi `send_email_alert` trên một Alert giả / hoặc tạm set một device `last_seen` cũ → chạy `from apps.alerts.tasks import evaluate_alert_rules; evaluate_alert_rules()` và quan sát mail in ra console + bản ghi `AlertNotification`.
6. (Tùy chọn) RBAC: đăng nhập tài khoản Review → `/alerts/config/` phải trả 403.
