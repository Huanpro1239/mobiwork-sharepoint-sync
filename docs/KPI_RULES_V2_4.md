# KPI Sales V2.4 — ghép M-1 và M

## Nguyên tắc

KPI chỉ xét khách hàng có viếng thăm trong tháng hiện tại **M**. Doanh số, ghi tồn, ghi chú và bằng chứng ảnh được phép tích lũy từ **M-1 + M** theo **Mã KH**.

## Bước 1 — Mới / Cũ

- **Mới**: chưa có đơn hàng hoặc lượt viếng thăm trước kỳ M trong phạm vi lịch sử nguồn đã cấu hình.
- **Cũ**: đã có ít nhất một hoạt động trước kỳ M.

`KPI_HISTORY_FROM_DATE` phải trỏ tới mốc lịch sử đầy đủ nếu cần phân loại Mới/Cũ tuyệt đối. Nếu lịch sử chưa đủ, workbook ghi cảnh báo thay vì im lặng coi kết quả là chính xác tuyệt đối.

## Bước 2 — Doanh số 2 tháng

Dòng sản phẩm được gom theo mã đơn trước khi xét threshold.

- **KHTC**: đơn lớn nhất trong M-1/M `>=` ngưỡng KHTC.
- **KHĐĐK**: nếu chưa đạt KHTC, tổng KTB trong M-1/M `>=` ngưỡng KHĐĐK.
- Mặc định KTB chỉ tính Két / Thùng / Bình; đơn vị Chai không cộng.

Ngưỡng mặc định:

- KHTC: `3.0 KTB` / đơn.
- KHĐĐK: `5.0 KTB` / 2 tháng.

Các ngưỡng được ghi vào sheet `Tham_so` và công thức tham chiếu ô tham số.

## Bước 3 — Ghi tồn

`ghi_ton_2m = TRUE` khi có ít nhất một lượt ghi tồn hợp lệ trong M-1 hoặc M.

Không có ghi tồn → khách hàng không đạt KPI, bất kể doanh số.

## Bước 4 — Ảnh / ghi chú

Điều kiện ảnh:

```text
(Bien_hieu >= 1 OR ghi_chu_bien_hieu_hop_le)
AND
Trung_bay >= 1
```

Ghi chú chỉ được thay bằng chứng **Biển hiệu**, không được thay ảnh **Trưng bày**.

Các ghi chú phủ định rõ như `Không biển bảng`, `Không có biển hiệu` không được đặc cách.

## Kết quả cuối khách hàng

```text
Có visit M
AND đạt doanh số
AND ghi_ton_2m
AND đủ bằng chứng ảnh
→ KHTC hoặc KHĐĐK
```

Nếu một Mã KH xuất hiện dưới nhiều nhân viên trong tháng M, hệ thống giữ từng dòng người viếng thăm và phát cảnh báo dữ liệu. Bằng chứng 2 tháng vẫn ghép theo Mã KH.

## Ngày công

```text
Tỷ lệ = (KHTC + KHĐĐK) / 50
Ngày công = MIN(Tỷ lệ, 1) × ngày công chuẩn
```

Ngày công chuẩn được tính bằng `NETWORKDAYS.INTL`, loại Chủ nhật.

## Thưởng

- KH Mới đạt: `MIN(Số KH Mới Đạt, 50) × 30.000` VNĐ.
- KH Cũ đạt: `MIN(Số KH Cũ Đạt, 50) × 10.000` VNĐ.
- Tổng thưởng: tối đa `4.000.000` VNĐ / nhân viên / tháng.

## Công thức sống / sửa tay

`Chi_tiet_Anh_Checkin` có:

- `Phân Loại AI`;
- `Nhãn Sửa Tay`;
- `Nhãn Dùng Thực Tế = IF(Nhãn Sửa Tay<>"", Nhãn Sửa Tay, Nhãn AI)`.

Các sheet khách hàng và tổng hợp đếm từ **Nhãn Dùng Thực Tế**, nên sửa nhãn thủ công làm KPI, ngày công và thưởng tính lại trong Excel mà không cần chạy model lại.
