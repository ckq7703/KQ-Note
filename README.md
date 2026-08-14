# KQ Note

Ứng dụng ghi chú desktop nhỏ gọn cho Windows, luôn sẵn sàng nổi trên màn hình để lưu và tra cứu nhanh các lệnh/cheatsheet thường dùng (nmap, docker, SQL,...) mà không cần mở trình soạn thảo hay trình duyệt.

![platform](https://img.shields.io/badge/platform-Windows-blue) ![python](https://img.shields.io/badge/python-3.10%2B-blue)

## Tính năng

**Giao diện**
- Widget nổi, luôn hiển thị trên cùng, có thể kéo thả vị trí và resize tuỳ ý
- Nền trong suốt nhẹ, bo góc cửa sổ hiện đại, không viền thừa
- Thu gọn xuống khay hệ thống (system tray), không chiếm taskbar

**Ghi chú & định dạng**
- Một tài liệu duy nhất, cuộn liên tục — không cần quản lý danh sách note rời rạc
- Định dạng nhanh bằng toolbar: Heading (H1), **Bold**, danh sách số thứ tự, bullet 2 cấp
- Enter trong danh sách tự nối tiếp số/ký hiệu; Enter trên dòng rỗng sẽ thoát danh sách
- Tự động lưu khi gõ, không cần bấm Save

**Tìm kiếm**
- Tìm theo toàn bộ nội dung (không chỉ tiêu đề), tự nhảy và bôi vàng kết quả
- `Enter` để nhảy tới kết quả tiếp theo

**Hình ảnh & liên kết**
- Dán ảnh trực tiếp từ clipboard (`Ctrl+V`) vào note, ảnh được lưu file riêng để note luôn nhẹ
- Note dài nhiều ảnh sẽ tải dần khi cuộn tới (lazy-load), không load hết một lúc
- Click vào ảnh để xem phóng to; click vào URL trong nội dung để mở bằng trình duyệt

**Hệ thống**
- Phím tắt toàn cục để ẩn/hiện, hoạt động ở bất kỳ đâu kể cả khi app không focus
- Chỉ chạy một tiến trình duy nhất (single-instance), tránh xung đột khi tự khởi động cùng Windows

## Cài đặt

Tải bản cài đặt mới nhất tại trang **[Releases](https://github.com/ckq7703/KQ-Note/releases)** → chạy `KQNoteSetup.exe`.

Installer sẽ tự động:
- Cài ứng dụng vào `%LOCALAPPDATA%\Programs\KQ Note` (không cần quyền admin)
- Tạo shortcut trong Start Menu
- Đăng ký tự khởi động cùng Windows
- Cài kèm uninstaller (gỡ trong Settings → Apps)

## Phím tắt

| Phím tắt | Chức năng |
|---|---|
| `Ctrl+Alt+Space` | Ẩn / hiện cửa sổ |
| `Ctrl+Space` | Ẩn / hiện cửa sổ (hiện lại sẽ luôn nổi trên cùng) |
| `Ctrl+V` | Dán văn bản, hoặc dán ảnh nếu clipboard đang chứa ảnh |
| `Enter` (trong danh sách) | Tạo dòng tiếp theo, tự nối số/bullet |
| `Enter` (trong ô tìm kiếm) | Nhảy tới kết quả tìm kiếm tiếp theo |
| `Esc` (trong ô tìm kiếm) | Xoá tìm kiếm |

## Định dạng nội dung

Chọn dòng cần định dạng rồi bấm nút tương ứng trên toolbar:

| Nút | Ý nghĩa |
|---|---|
| `H1` | Tiêu đề |
| `B` | In đậm |
| `1.` | Danh sách số thứ tự (cấp 1) |
| `—` | Bullet cấp 2 |
| `+` | Bullet cấp 3 |

## Dữ liệu được lưu ở đâu

Toàn bộ dữ liệu lưu cục bộ trên máy, không gửi lên máy chủ nào:

```
%APPDATA%\NoteCheatsheet\
├── notes.txt      # nội dung ghi chú (plain text)
├── config.json    # vị trí cửa sổ, phím tắt, tuỳ chọn
└── images\        # ảnh đã dán vào note
```

## Chạy từ mã nguồn

Yêu cầu Python 3.10+.

```bash
pip install -r requirements.txt
python main.py
```

## Build installer

```bash
python -m PyInstaller --onedir --noconsole --name "KQNote" --icon="assets/logo-kqnote.ico" --add-data "assets;assets" main.py
ISCC installer.iss
```

File cài đặt hoàn chỉnh sẽ nằm trong `installer_output/KQNoteSetup.exe`.

## Công nghệ sử dụng

- **Python + Tkinter** — giao diện, nhẹ và không cần cài runtime thêm
- **Pillow** — xử lý ảnh, icon
- **pystray** — icon khay hệ thống
- **pynput** — phím tắt toàn cục
- **Inno Setup** — đóng gói installer cho Windows
