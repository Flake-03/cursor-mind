# CursorMind

Trợ lý giọng nói tiếng Việt điều khiển các cửa sổ Chromium riêng. Mỗi agent có profile, tab và thư mục tải xuống riêng. Input AI được gửi trực tiếp vào tab qua Chrome DevTools Protocol; chuột/phím desktop không bị đổi routing, focus MPX hoặc keymap.

## Cài đặt và chạy

Cần Python 3.11+, Poetry 2, Chrome/Chromium và phiên desktop đồ họa trên máy ảo. Không cần chạy script setup MPX, cấp quyền uinput hay thêm group input.

Khi chuyển từ source cũ, thoát các tiến trình CursorMind cũ, copy toàn bộ source mới cùng `pyproject.toml` và giữ file `.env` của bạn. Nếu dự án đã có lockfile cũ, cập nhật dependency:

```bash
poetry lock
poetry install
poetry run cursormind
```

Các lệnh trên để bạn chạy trên máy ảo. Bản thay đổi này chưa được chạy app hoặc kiểm thử.

Điền `GEMINI_API_KEY`, `LIVE_MODEL`, `BROWSER_MODEL` trong `.env`; dùng model mà key của bạn có quyền truy cập. `CHROME_PATH` chỉ cần khi không tự tìm được binary. `HOME_URL` là trang để mở lại browser khi resume mà cửa sổ đã đóng. `MAX_AGENTS`, `MAX_STEPS`, `TASK_TIMEOUT` giới hạn tài nguyên và thời gian.

Chỉ dùng text:

```bash
poetry run cursormind --text
```

## Giao việc trên nhiều website

Sau thông báo **CursorMind đã kết nối**, nói hoặc nhập yêu cầu tự nhiên:

```text
Mở GitHub cho tôi.
Mở wikipedia.org và tìm bài về Hà Nội.
Tạo agent research, tìm thông tin trên hai website rồi so sánh kết quả.
Agent research đến đâu rồi?
```

Mặc định mỗi lệnh dùng lại agent và tab đang mở. Worker nhìn screenshot/DOM hiện tại để bấm, gõ, cuộn hoặc đọc ngay trên trang đó; giao task mới không tự chuyển về trang chủ hay công cụ tìm kiếm. Không tự điều hướng chỉ vì câu lệnh có nhắc tên website hoặc URL.

Chỉ khi bạn yêu cầu chuyển website, trợ lý mới truyền URL đích. Với yêu cầu mở tab mới, worker mở tab riêng và giữ tab đang có. Tìm kiếm trên một website dùng ô tìm kiếm của website đó. Browser chưa được mở thì khởi động bằng `about:blank` (hoặc `HOME_URL` do bạn cấu hình), rồi worker quan sát và thực hiện yêu cầu. Nếu `.env` cũ có `HOME_URL=https://duckduckgo.com/`, đổi thành `HOME_URL=about:blank` nếu bạn không muốn trang đó lúc khởi động browser mới.

Khi bắt đầu task, cửa sổ browser được mở/khôi phục và đưa lên. Với agent đã có browser, ứng dụng dùng lại cửa sổ và profile; không tạo cửa sổ thừa cho mỗi câu nói. Resume không tự điều hướng lại URL ban đầu hay phát lại các thao tác đã gửi.

## Chuột/phím thật và thao tác AI

Bạn có thể chuyển sang ứng dụng khác, di chuyển cửa sổ hoặc dùng chuột/phím thật trên browser. AI dùng tọa độ CSS trong tab, không cần khớp ảnh desktop, không chiếm con trỏ hệ điều hành. Chấm tím trong trang là con trỏ AI; nó không chặn click của bạn.

Di chuyển con trỏ được chia thành các bước ngắn với tốc độ tăng/giảm; scroll được chia nhỏ. Chữ Unicode được chèn trực tiếp, không đổi layout bàn phím. Click, chọn nội dung và nhập text được thực hiện trong cùng một lượt rồi đọc lại giá trị. Thời gian suy luận của model và tải website vẫn phụ thuộc model, mạng và máy ảo.

Khi nhận thấy click/gõ/scroll của bạn trên trang giữa các lượt input AI, agent nhường và chuyển sang chờ người dùng. Không có thao tác khóa thiết bị thật. Hai bên vẫn dùng chung nội dung và ô đang focus của cùng một tab: cơ chế nhận biết chỉ mang tính phối hợp, không thể tách thành hai tài liệu độc lập. Để đăng nhập hoặc sửa cùng ô nhập một cách chắc chắn, dùng `/pause <agent>` trước, xử lý xong rồi `/resume <agent>`. Không cần Alt+Tab để AI được phép chạy. Hộp thoại native, CAPTCHA, 2FA và một số iframe có thể cần bạn hỗ trợ.

## Lệnh trực tiếp

| Lệnh | Công dụng |
| --- | --- |
| `/create research` | Tạo agent/profile |
| `/task research <mục tiêu>` | Thao tác trên tab hiện tại |
| `/task research https://example.com <mục tiêu>` | Mở URL chỉ định |
| `/update research <mục tiêu đầy đủ>` | Đổi yêu cầu, giữ tiến độ |
| `/agents` hoặc `/status research` | Xem trạng thái/kết quả |
| `/pause research` / `/resume research` | Dừng / tiếp tục |
| `/pause` / `/resume all` | Dừng / tiếp tục tất cả |
| `/cancel research` | Hủy task, giữ browser |
| `/close research` | Đóng browser, giữ profile đăng nhập |
| `/rename research <tên mới>` | Đổi tên agent |
| `/mute` | Bật/tắt microphone |
| `/help` | Xem lệnh |
| `/exit` hoặc Ctrl+C | Thoát và đóng browser |

Tên có khoảng trắng đặt trong dấu nháy; có thể dùng ID thay tên. Mất kết nối Live làm các task pause; cần lệnh tiếp tục sau khi nối lại. Thao tác đã xảy ra trên website không được hoàn tác hay tự gửi lại khi chưa xác minh.

Tiến độ được in khi thay đổi, không chỉ khi đổi trạng thái. Muốn nghe kết quả, hỏi trạng thái agent. Dữ liệu profile, downloads, task journal và `runtime.log` nằm trong `.data/`. Nội dung phục vụ suy luận được gửi tới Gemini.

Nên dùng tai nghe: mic hiện tạm chặn khi trợ lý đang nói và 350 ms sau âm thanh cuối để tránh vọng loa. Chưa hỗ trợ ngắt lời bằng giọng nói trong chế độ này.

## Dọn thiết bị còn sót từ bản cũ

Chỉ khi phiên X11 vẫn còn seat từ bản MPX trước: thoát tiến trình cũ, rồi chạy bằng tài khoản desktop với đúng `DISPLAY`/`XAUTHORITY`:

```bash
poetry run cursormind --recover-input
```

Lệnh chỉ gỡ master `cursormind-*` cũ và trả thiết bị về master thật; không cần Gemini API key hay uinput. Bản mới không tạo lại các master này. Không cần cài lại cấu hình Xorg/udev cũ.

Dừng AI từ Terminal khác nếu cần:

```bash
kill -USR1 PID_CURSOR_MIND
```
