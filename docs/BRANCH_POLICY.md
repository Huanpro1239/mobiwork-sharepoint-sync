# Branch policy

Mục tiêu là giữ repository dễ hiểu và tránh tích lũy branch lịch sử không còn giá trị vận hành.

## Branch production

```text
main
```

`main` là nguồn production duy nhất. GitHub Actions production chỉ nên lấy code từ `main`.

## Quy ước branch mới

```text
feature/<mo-ta>   tính năng mới
fix/<mo-ta>       sửa lỗi
refactor/<mo-ta>  dọn cấu trúc, không đổi business semantics
chore/<mo-ta>     maintenance/config/docs
ops/<mo-ta>       kiểm tra/vận hành tạm thời
```

Một branch phải có mục tiêu cụ thể và nên được xóa sau khi merge, trừ khi được giữ có chủ ý làm rollback/reference.

## Planning branch audit — 2026-08-28

| Branch | Quan hệ với `main` | Xử lý |
|---|---|---|
| `main` | production | Giữ |
| `feature/planning-engine-v2` | `main` đi trước 12 commit, branch không đi trước | Candidate xóa sau khi cleanup PR hoàn tất |
| `feature/planning-engine-v2-mrp-schedule` | `main` đi trước 7 commit, branch không đi trước | Candidate xóa sau khi cleanup PR hoàn tất |
| `feature/planning-engine-v1` | diverged | **Chưa xóa**; cần rà commit lịch sử riêng trước |
| `refactor/planning-structure-cleanup` | branch cleanup hiện tại | Xóa sau khi PR merge |

## Quy tắc dọn branch

Chỉ xóa branch khi thỏa một trong hai điều kiện:

1. branch là ancestor của `main` và không có commit riêng cần giữ; hoặc
2. PR đã merge và rollback đã được bảo đảm bằng commit/tag/history trên `main`.

Không xóa branch diverged chỉ vì tên cũ.

## Chu kỳ housekeeping đề xuất

Mỗi tháng một lần:

1. list branch;
2. xác định branch đã merge/ancestor của `main`;
3. kiểm tra branch diverged;
4. xóa branch stale an toàn;
5. giữ Dependabot branch theo lifecycle tự động của GitHub;
6. không dùng branch dài hạn như một hệ thống backup — dùng Git history/tag/release thay thế.
