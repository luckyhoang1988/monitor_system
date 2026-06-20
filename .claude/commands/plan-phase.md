# /plan-phase

Lập kế hoạch chi tiết cho một phase triển khai dự án.

## Cách dùng
```
/plan-phase 1
/plan-phase 2
```

## Hành động
1. Xem lại CLAUDE.md để hiểu kiến trúc tổng thể
2. Liệt kê tất cả tasks cần hoàn thành trong phase đó
3. Ước tính độ phức tạp mỗi task (S/M/L)
4. Xác định dependencies giữa các tasks
5. Đề xuất thứ tự implement
6. Tạo checklist dạng markdown

## Phase definitions
- **Phase 1 (MVP)**: Collector cơ bản 1 loại switch + 1 HyperV host, alert email, CLI tool xem metrics
- **Phase 2**: Multi-vendor, Grafana dashboard, Teams alert, Docker deploy
- **Phase 3**: Scale, HA, API public, RBAC
