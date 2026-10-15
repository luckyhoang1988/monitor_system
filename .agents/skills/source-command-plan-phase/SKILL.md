---
name: "source-command-plan-phase"
description: "Migrated source command `plan-phase`"
---

# source-command-plan-phase

Use this skill when the user asks to run the migrated source command `plan-phase`.

## Command Template

# /plan-phase

Lập kế hoạch chi tiết cho một hạng mục hoặc giai đoạn công việc.

## Cách dùng
```
/plan-phase "Giai đoạn 2: dọn nợ kỹ thuật"
/plan-phase "Thêm metric X cho HyperV"
```

## Hành động
1. Đọc CLAUDE.md (nguyên tắc làm việc, kiến trúc, bẫy đã dính) và `/deploy` skill
2. Liệt kê tất cả tasks cần hoàn thành
3. Ước tính độ phức tạp mỗi task (S/M/L)
4. Xác định dependencies giữa các tasks và thứ tự implement
5. Với mỗi task: nêu cách đo/probe dữ liệu thật TRƯỚC khi sửa, test hồi quy phải đỏ trước, và tiêu chí verify sau deploy
6. Tạo checklist dạng markdown; ghi rõ những bước cần người dùng duyệt (commit/push/deploy)

## Hiện trạng
Các phase gốc (setup/models → collector → Celery + HyperV → dashboard → alert → Docker/prod → RBAC) đã hoàn thành — xem CLAUDE.md mục "Trạng thái". Công việc hiện nay là bảo trì: độ tin cậy của cảnh báo và dọn nợ kỹ thuật; từng đợt thay đổi được ghi ở CLAUDE.md mục "Thay đổi quan trọng".
