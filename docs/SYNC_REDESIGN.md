# Thiết kế lại lưu trữ & đồng bộ note (nhiều note / user)

Trạng thái: **Phase 0–4 đã làm trong code.** Phase 1 đã deploy lên server; Phase 4 có thay đổi server **chưa deploy**; các phần client (Phase 2–4) chưa phát hành.

## Vấn đề hiện tại

Backend chỉ có 1 dòng `notes` cho mỗi user, trong khi app desktop có nhiều note. Sync đẩy note *đang mở* vào slot duy nhất đó và pull thì ghi slot đó lên note *đang mở*, nên các note đè lẫn nhau giữa các máy. Chi tiết các đường dẫn mất dữ liệu:

1. Cloud 1 slot bị mọi note ghi đè (`flush_save`, `_apply_remote_update`, `_apply_initial_pull` trong `app/notes_widget.py`).
2. Conflict: local ghi đè server, bản server chỉ lưu file trên máy, UI không báo (`_update_cloud_icon` rỗng, sự kiện `error` bị bỏ qua).
3. `last_synced_hash` toàn cục: đổi note xong thì update từ xa bị bỏ qua âm thầm.
4. `store.py` ghi file không atomic; index hỏng thì khởi tạo lại và ghi đè `note_default.txt`.
5. Xoá là `os.remove` vĩnh viễn, không thùng rác, không tombstone.
6. ID `note_default` trùng giữa các máy; ID note mới chỉ 32 bit.
7. `sync_state` không theo account/note.
8. Server: không lịch sử, không giới hạn kích thước, không migration, không backup DB, ảnh không được dọn.
9. Khi đã đăng nhập, khởi động mà chưa có cache cloud cho note đang mở thì editor trống, autosave sau đó ghi đè note thật bằng nội dung trống.

## Quyết định đã chốt

| Câu hỏi | Quyết định |
|---|---|
| Xung đột | **Bản sao xung đột** (kiểu OneNote), không tự merge ở giai đoạn đầu. Auto-merge theo dòng để Phase 4 |
| Đăng nhập khi đã có note local | **Upload lên tài khoản như note mới**, không ghi đè gì |
| Lưu trữ local | **SQLite (WAL)** thay cho `index.json` + file `.txt` |

## Tham khảo Microsoft

- OneNote sync ngầm từ bản local; sửa cùng một đối tượng thì tạo bản sao xung đột và báo người dùng, không bỏ nội dung nào. Lưu theo revision (MS-ONESTORE).
- Thùng rác 60 ngày cho page/section đã xoá.
- Graph delta query: cursor không trong suốt, tombstone `@removed`, phải chịu được replay (idempotent), `410 Gone` thì full resync.
- Sticky Notes: mỗi note là một item riêng gắn với tài khoản.

## Kiến trúc đích

Nguyên tắc: local là nguồn để làm việc, server là nguồn để đồng bộ. Không thao tác sync nào được ghi đè nội dung mà không giữ lại bản cũ.

### Server (Postgres)

- `notes(id UUID do client sinh, user_id, content, title, position, rev, seq, deleted_at, updated_by_device, created_at, updated_at)`
- `note_revisions(note_id, rev, content, created_at)`: giữ 30 ngày hoặc N bản gần nhất, gộp các lần autosave sát nhau.
- `user_sync(user_id, seq)`: tăng `seq` trong cùng transaction với mỗi lần ghi; đây là cursor delta.
- API v2:
  - `GET /v2/notes/changes?cursor=`: delta kèm tombstone, cursor quá cũ trả 410.
  - `PUT /v2/notes/{id}` với `base_rev`: sai rev trả **409 kèm bản server, không bao giờ ghi đè**. Tạo mới/retry idempotent qua UUID + `mutation_id`.
  - `DELETE` xoá mềm, `POST /restore`, job purge sau 60 ngày, `GET /revisions`.
- Giới hạn 1 MB/note, số note tối đa/user, rate limit.
- Migration: đổi tên `notes` thành `notes_legacy` (không xoá), mỗi dòng cũ thành 1 note mới. `/notes/me` cũ tiếp tục chạy như "hộp thư legacy" cho client 1.4.x; client mới import nó thành một note thường rồi ngừng dùng.

### Client

- SQLite: `notes`, `outbox`, `sync_meta` (cursor, device_id), `base_content`. Import một lần từ thư mục cũ và **giữ nguyên thư mục cũ làm backup**.
- ID là UUID đầy đủ, mỗi note gắn `account_id`.
- Engine sync theo từng note: ghi local trước, push từng note với `base_rev`, pull theo cursor, áp dụng idempotent.
- Conflict: bản server ở lại note gốc, sửa đổi local thành **note bản sao "(xung đột – máy – giờ)"** có badge và thông báo trên UI.
- Không hot-swap nội dung khi editor còn thay đổi chưa lưu. Xoá bên này mà bên kia đã sửa thì giữ lại (sửa thắng xoá).
- Thùng rác 60 ngày; thứ tự note bằng fractional index.
- Ảnh đặt ID theo SHA-256 (bất biến, idempotent); marker `kqnote-image:` cũ vẫn dùng được; dọn ảnh mồ côi khi purge.
- Hiển thị trạng thái sync/lỗi; nút xuất tất cả ra `.md`/zip.

## Các phase

| Phase | Nội dung | Trạng thái |
|---|---|---|
| 0 | Hotfix an toàn: ghi atomic, khôi phục index hỏng, backup trước khi cloud ghi đè hoặc xoá note, vá editor trống khi khởi động, backup Postgres định kỳ | **Xong** (chưa release/deploy) |
| 1 | Server v2: schema, migration, endpoint, revisions, shim `/notes/me` | **Xong, đã deploy** (rate limit chuyển sang Phase 4) |
| 2 | Client: SQLite, UUID, thùng rác, gắn account | **Xong** (chưa phát hành riêng; phát hành cùng Phase 3) |
| 3 | Client: engine sync v2 (cursor, bản sao xung đột, UI trạng thái) | **Xong** (chưa phát hành) |
| 4 | Gộp 3-way theo dòng, xoá vĩnh viễn báo server, dọn ảnh mồ côi, rate limit, deprecate API cũ, cổng phiên bản | **Xong** (server chưa deploy; client chưa phát hành) |

## Kiểm thử

- Backend: pytest trên Postgres thật (concurrent PUT, retry idempotent, phân trang delta, tombstone, 410).
- Client: engine với server giả mô phỏng offline, replay, 2 thiết bị, crash giữa lúc ghi.
- Migration: dry-run trên bản restore của DB prod trong container tạm, không đụng container đang chạy.

## Phase 0: chi tiết đã làm

- `app/store.py`: `atomic_write_text/bytes`; `_load_index` cách ly index hỏng thành `index.corrupt-<ts>.json` rồi dựng lại từ các `note_*.txt` còn trên đĩa (giữ nguyên key khác như Gemini); không ghi đè `note_default.txt` đang tồn tại; `backup_note_content` lưu vào `backups/` (giữ 200 bản gần nhất); xoá note giờ để lại bản backup.
- `app/notes_widget.py`: backup note local trước khi pull/đăng nhập ghi đè; khởi động khi đã đăng nhập mà chưa có cache cloud thì dùng nội dung note local thay vì editor trống.
- `app/sync/state.py`, `app/config.py`: ghi atomic.
- `backend/docker-compose.yml`: dịch vụ `db_backup` chạy `pg_dump -Fc` mỗi 24 giờ vào volume `db_backups`, giữ 14 bản.
- Test: `python -m unittest tests.test_store_safety`.

## Phase 1: chi tiết đã làm (server v2)

Code: `backend/app/{models,note_service,migrations,maintenance}.py`, `backend/app/routers/notes_v2.py`. Test: `backend/tests` (32 test, chạy được trên SQLite; đặt `TEST_DATABASE_URL` trỏ tới một Postgres **dùng riêng để test** để chạy thêm các test đồng thời; test tự `drop_all`, tuyệt đối không trỏ vào DB thật).

### Hợp đồng API `/v2/notes` (cần Bearer token như các API khác)

| Method | Đường dẫn | Ý nghĩa |
|---|---|---|
| GET | `/changes?cursor=&limit=` | Delta feed theo `seq` tăng dần: `{changes, cursor, has_more}`. `cursor=0` = từ đầu. Trả **410** nếu cursor lớn hơn seq hiện tại của server (DB đã bị restore) hoặc nhỏ hơn `tombstone_floor` (có thể đã lỡ một lần xoá) → client phải resync từ `cursor=0` |
| GET | `/{id}` | Một note (kể cả trong thùng rác) |
| PUT | `/{id}` | Tạo (`base_rev=0`) hoặc sửa. `base_rev` sai → **409** `{error: "conflict", note: <bản server>}`, **không bao giờ ghi đè**. Sửa note đang trong thùng rác cần `restore: true`. Note không tồn tại mà `base_rev>0` → 404 |
| POST | `/{id}/trash`, `/{id}/restore` | Xoá mềm / khôi phục, cũng yêu cầu `base_rev` (thiết bị cũ không thể xoá mất bản mới sửa) |
| PATCH | `/{id}` | Đổi `position` (last-write-wins, không đổi `rev`, không xung đột với sửa nội dung) |
| GET | `/{id}/revisions`, `/{id}/revisions/{rid}` | Lịch sử; khôi phục bằng cách PUT lại nội dung cũ |

- `id` là UUID do client sinh, duy nhất theo từng user. `mutation_id` cho phép retry an toàn khi mất response.
- `rev` = phiên bản nội dung (dùng làm `base_rev`); `seq` = vị trí trong feed (đổi cả khi chỉ đổi thứ tự).
- Trạng thái note: live → trashed (giữ nội dung) → purged (mất nội dung, giữ dòng làm tombstone).
- Ghi: mỗi lần ghi khoá dòng `user_sync` của user rồi mới đọc/kiểm tra `rev`, nên `seq` luôn theo thứ tự commit và client không bao giờ bỏ sót thay đổi.
- Giới hạn (cấu hình được trong `config.py`): 1 MB/note (413), 5000 note/user (403).
- Lịch sử: tối đa 1 bản/phút/note, nhưng **luôn** lưu khi nội dung bị co lại quá 50% (chống xoá nhầm); giữ 50 bản gần nhất và 30 ngày (luôn giữ 5 bản mới nhất).
- Job dọn (`maintenance.py`, chạy mỗi 6 giờ trong tiến trình API): thùng rác > 60 ngày → purged; tombstone > 90 ngày → xoá hẳn và nâng `tombstone_floor`.

### Migration

Khi API khởi động và thấy bảng `notes` dạng v1: trong **một transaction** đổi tên thành `notes_legacy` (không xoá), tạo bảng mới, copy mỗi blob không rỗng thành 1 note (UUID mới, `rev=1`). Chạy lại là no-op. Đã dry-run trên bản restore của DB production trong container tạm: 2 user → 2 note, `notes_legacy` và 9 ảnh còn nguyên.

`/notes/me` (v1) vẫn chạy, đọc/ghi `notes_legacy`, để client 1.4.x không hỏng. Lưu ý: sau migration, note v2 chỉ là **ảnh chụp** của blob lúc đó; nếu người dùng còn dùng client cũ thì blob legacy tiếp tục thay đổi mà không đẩy sang v2. **Việc cho Phase 3:** client mới đọc `/notes/me` một lần và import thành note thường nếu nội dung chưa có trong các note v2.

### Deploy và rollback

1. Chạy `pg_dump` thủ công trước khi deploy (dịch vụ `db_backup` chỉ có sau khi `docker compose up -d` bản compose mới).
2. Deploy image mới; migration tự chạy khi khởi động.
3. Rollback: image cũ **không** chạy được trên DB đã migrate (bảng `notes` đã đổi hình). Muốn quay lại: dừng API, chạy trong Postgres
   `DROP TABLE note_revisions, notes, user_sync; ALTER TABLE notes_legacy RENAME TO notes; ALTER INDEX notes_legacy_pkey RENAME TO notes_pkey;`
   rồi chạy lại image cũ. Cách này mất các note v2 tạo sau migration, nên chỉ dùng khi chưa có client v2 nào ghi dữ liệu.

## Phase 2: chi tiết đã làm (lưu trữ local)

Code: `app/store.py` (viết lại phần lưu note; giữ nguyên tên hàm để UI ít phải sửa), `app/legacy_storage.py` (đọc layout cũ), `app/fracindex.py` (khoá thứ tự), `app/trash_dialog.py` + `app/theme.py` (UI thùng rác). Test: `tests/` (58 test).

### Lưu trữ

- Một file SQLite `kqnote.sqlite3` trong `%APPDATA%\NoteCheatsheet` (WAL, `synchronous=FULL`; sẽ có thêm file `-wal`/`-shm` cạnh nó, nên khi tự sao chép hãy chép cả ba). Không dùng tên `notes.db` vì tên đó thuộc layout cũ hơn.
- Bảng `notes(id UUID, account_id, content, title, snippet, position, created_at, updated_at, deleted_at, legacy_id, server_rev, base_content, dirty)`, `kv` (note đang mở, cài đặt Gemini), `adoptions`. Mọi thao tác ghi là một transaction; lỗi giữa chừng thì rollback toàn bộ.
- `position` là fractional index (`app.fracindex`): kéo thả một note chỉ đổi khoá của đúng note đó. Khi khoá dài quá `MAX_KEY_LEN` hoặc gặp khoá lạ/trùng (ví dụ `a0` do migration phía server tạo ra), store tự đánh số lại cả danh sách, vẫn giữ thứ tự.

### Migration lần chạy đầu

- Tự chạy khi mở app: đọc `notes_store/index.json` + `note_*.txt` (nếu index hỏng thì copy sang `index.corrupt-<ts>.json` và dựng lại từ các file note; file note không có trong index vẫn được nhập), rồi `notes.txt`, rồi `notes.db` cũ. Cài mới thì tạo note mặc định.
- Toàn bộ trong **một transaction**, có kiểm tra lại số note và tổng số ký tự trước khi ghi `user_version`. Lỗi thì không thay đổi gì và lần mở sau thử lại; `main.py` hiện hộp thoại nêu rõ thư mục dữ liệu và ghi `startup_error.log`.
- **Không sửa hay xoá file cũ** (`notes_store/`, `notes.txt`, ...): chúng là bản backup. Lưu ý: nếu ai đó chạy lại bản app cũ trên cùng thư mục, bản đó chỉ thấy dữ liệu tại thời điểm migrate. Có thể tự xoá chúng sau khi yên tâm.
- ID cũ (`note_default`, `note_a1b2c3d4`) được đổi thành UUID; ID cũ giữ trong cột `legacy_id`. Ghi chú đang mở, thứ tự, thời gian tạo/sửa và cài đặt Gemini đều được giữ.

### Thùng rác

- Xoá note giờ là chuyển vào thùng rác (60 ngày, `TRASH_RETENTION_DAYS`, khớp với server). Mục **🗑️ Thùng rác** trong menu ba chấm mở hộp thoại: khôi phục, xoá vĩnh viễn, dọn sạch.
- Xoá vĩnh viễn (và tự dọn khi quá hạn lúc khởi động) luôn để lại một bản `.purged.txt` trong `backups/` (giữ 200 bản gần nhất).

### Phạm vi tài khoản (chuẩn bị cho Phase 3)

- `store.set_scope(account_id)` chọn danh sách nào đang hiển thị (`None` = note chỉ có ở máy). `store.copy_local_notes_to_account(account_id)` **sao chép** các note local (bỏ qua note trống và note trong thùng rác) thành note mới của tài khoản, đánh dấu `dirty` để Phase 3 upload; note local gốc vẫn còn (đăng xuất là thấy lại) và bảng `adoptions` đảm bảo đăng nhập lại không nhân đôi note.
- Các cột `server_rev`, `base_content`, `dirty` đã có nhưng **chưa có nơi nào đọc chúng** ngoài `dirty`; Phase 3 sẽ dùng.

### Ghi chú về Phase 2

- Phase 2 không nên phát hành riêng: luồng đồng bộ v1 (một slot) chỉ được thay ở Phase 3.
- Local "xoá vĩnh viễn" chưa báo cho server: note đó ở lại thùng rác server tới khi server tự purge sau 60 ngày.

### Lưu ý khi build (bẫy đã gặp)

`xor_obfuscate.py` chèn helper sau dòng `import` cuối của **mọi** file `.py` bằng một regex mà `\s+` nuốt luôn xuống dòng. Hệ quả: dòng import kết thúc bằng chữ "import" (ví dụ module tên `legacy_import`), hoặc `from x import (` nhiều dòng ở cuối, sẽ làm hỏng file và hỏng bản build. `tests/test_build_compat.py` mô phỏng bước này và biên dịch lại mọi module; hãy giữ nó trong CI.

### Chạy test

`python -m unittest discover -s tests -t .` (Linux không màn hình: thêm `xvfb-run -a`; test dialog tự bỏ qua nếu không có display).

## Phase 3: chi tiết đã làm (đồng bộ nhiều note)

Code: `app/sync/repo.py` (mọi quyết định, không có mạng), `app/sync/engine.py` (luồng, request, sự kiện), `app/sync/client.py` (gọi `/v2/notes`), `app/store.py` (schema v2, lưu có kiểm tra), `app/notes_widget.py` (nối vào giao diện). Không đổi server.

### Mô hình

Mỗi note của tài khoản nhớ **cả hai phía**: bản trên máy (`content`, `dirty`, `deleted_at`, `position`) và bản server đã biết (`server_rev`, `base_content`, `server_deleted`, `server_position`). Việc cần đẩy lên được **suy ra từ chênh lệch** (`repo.next_op`) chứ không có hàng đợi riêng, nên mất điện giữa chừng chỉ là lần sau tính lại; mọi bước đều lặp lại được (`mutation_id` = hash nội dung + rev, server nhận ra yêu cầu trùng).

Một chu kỳ (`SyncEngine.sync_once`): kéo thay đổi theo cursor → sao chép note local vào tài khoản (`copy_local_notes_to_account(skip_duplicates=True)`) → nhập note "một slot" cũ của bản v1 (một lần) → đẩy từng note. Lỗi ở một note không chặn các note khác. Chạy khi: sau khi lưu/xoá/đổi thứ tự (trễ 2 giây), mỗi 45 giây, và khi bấm "Đồng bộ ngay".

### Khi server có bản mới hơn (`repo._apply_newer`)

`base_content` là nội dung chung lần cuối hai bên thống nhất; so với nó để biết server **có thật sự đổi chữ** hay chỉ đổi trạng thái.

| Máy này | Server | Kết quả |
|---|---|---|
| không sửa gì | đổi bất kỳ | nhận bản server |
| sửa chữ | chữ giống hệt | nhận số revision, không xung đột |
| sửa chữ | revision đổi nhưng chữ không đổi (xoá/khôi phục/đổi thứ tự ở nơi khác) | giữ bản sửa của mình, đẩy tiếp |
| sửa chữ | **chữ khác** (kể cả khi server đã xoá note) | **bản của mình thành ghi chú mới `# [Xung đột] ...`**, note gốc nhận bản server |
| sửa chữ | server xoá, chữ không đổi | sửa thắng xoá: note sống lại (đẩy bằng `restore`) |
| xoá | server sửa chữ | sửa thắng xoá: note hiện lại với bản của server |
| xoá | server chỉ đổi revision | giữ lệnh xoá |
| bất kỳ | server đã purge | note chưa đẩy được giữ lại thành note mới; note sạch thì xoá cục bộ (để lại bản trong `backups/`) |

Bản xung đột là ghi chú bình thường, có biểu ngữ ở đầu và tiêu đề `[Xung đột] ...` nên hiển thị đúng trên mọi thiết bị.

### Ô soạn thảo

Cơ sở dữ liệu là nguồn sự thật. Ô soạn thảo lưu bằng so-sánh-và-ghi (`save_note_by_id(..., expected_old=...)`): nếu chữ trong DB đã đổi kể từ lúc ô soạn thảo nạp (một lượt sync vừa áp bản từ máy khác), **chữ đang gõ không ghi đè mà thành bản sao xung đột**, ô soạn thảo hiện lại bản mới và có thông báo. Nếu chưa gõ gì thì bản mới từ máy khác được hiện ngay, giữ nguyên vị trí con trỏ. Nhấn phím mũi tên không gây ghi gì (so với `_editor_baseline`). Note đang mở bị xoá từ máy khác thì phần đang gõ được lưu vào note đó (nằm trong Thùng rác) rồi chuyển sang note khác.

### Đăng nhập / đăng xuất

Đăng nhập: giữ nguyên danh sách local, sync lần đầu kéo note của tài khoản, đưa note local lên (bỏ qua note trống, note mẫu chưa sửa, và note có chữ trùng hệt note đã có trên tài khoản), rồi mới chuyển sang danh sách tài khoản. Đăng xuất: quay về danh sách local; note của tài khoản vẫn nằm trên đĩa (ẩn) nên đăng nhập lại là có ngay. Note mẫu chưa sửa (`# Nmap` mặc định, `# Ghi chú mới`) không bao giờ được đẩy lên.

### Giao diện

Biểu tượng ☁ ở thanh tiêu đề (xanh: đã đồng bộ; xanh dương: đang chạy; vàng: ngoại tuyến hoặc còn thay đổi chưa gửi; đỏ: lỗi; ẩn khi chưa đăng nhập). Menu ba chấm hiện dòng trạng thái ("Đã đồng bộ lúc 14:02", số thay đổi còn chờ, hoặc lý do lỗi).

### Kiểm thử

- `tests/test_sync_repo.py`: từng nhánh của bảng quyết định (không cần mạng).
- `tests/test_sync_engine.py`: **server thật** (uvicorn, DB SQLite tạm) với nhiều "thiết bị"; giả lập mất mạng, mất response, mất điện giữa chừng, server khôi phục từ backup cũ, note bị purge, hàng trăm note qua nhiều trang.
- `FuzzTest`: 3 thiết bị làm việc ngẫu nhiên (tạo/sửa/xoá/khôi phục/đổi thứ tự/ngắt mạng/đồng bộ) rồi kiểm tra: **mọi chữ đã gõ còn nằm đâu đó trên server**, các thiết bị đồng nhất với server (nội dung, trạng thái xoá, thứ tự), và mọi note chưa lên server đều chỉ là chữ mẫu. Chạy dài hơn bằng `FUZZ_FIRST_SEED=200 FUZZ_SEEDS=40 FUZZ_STEPS=80`.
- `tests/test_widget_sync.py`: widget thật dưới Xvfb (stub API Windows): sync áp lên ô soạn thảo sạch/đang gõ, note bị xoá từ máy khác, đăng nhập/đăng xuất, biểu tượng trạng thái.
- Chạy đủ bằng một venv có yêu cầu của backend + `requests`, `Pillow`, `keyring`: `xvfb-run -a python -m unittest discover -s tests -t .` (thiếu phụ thuộc thì các test đó tự bỏ qua).

Lỗi thật do FuzzTest tìm ra và đã sửa (đều là mất chữ): ghi đè bản của máy khác khi cả hai cùng sửa mà note đã bị xoá; chữ đã sửa nhưng note bị xoá rồi thì không bao giờ được đẩy lên; hai máy cùng sửa một note đã ở thùng rác thì máy sau ghi đè máy trước; note tạo rồi xoá khi chưa đồng bộ không lên server (thùng rác lệch nhau). Ngoài ra widget từng hiện lại **bản sao xung đột** thay vì note gốc ngay sau khi tạo bản sao.

### Chưa làm / lưu ý khi phát hành

- **Mọi thiết bị phải cập nhật.** Client 1.4.x vẫn ghi vào slot cũ; client mới chỉ nhập slot đó một lần khi đăng nhập lần đầu, nên sau đó không sync với client cũ nữa.
- Chưa có: tự gộp 3-way theo dòng, dọn ảnh mồ côi, ảnh đặt ID theo hash, rate limit, chặn phiên bản cũ (Phase 4).
- Xoá vĩnh viễn ở local vẫn chưa báo server (server tự purge sau 60 ngày).
- Chưa chạy trên Windows thật: phần widget được kiểm qua Xvfb với stub `winfx`, chưa kiểm hành vi thanh tiêu đề/dock trên Windows.

## Checklist thử tay trên Windows trước khi phát hành 1.5.0

Phần tự động không kiểm được giao diện thật trên Windows (thanh tiêu đề, dock, con trỏ chuột, keyring). Làm trên một **bản sao** thư mục dữ liệu, không dùng dữ liệu duy nhất:

1. Sao chép `%APPDATA%\NoteCheatsheet` ra chỗ khác. Cài bản mới, mở app: các ghi chú cũ, thứ tự và ghi chú đang mở phải còn nguyên; xuất hiện file `kqnote.sqlite3`; thư mục `notes_store` cũ vẫn còn.
2. Tạo, sửa, kéo thả sắp xếp, xoá một ghi chú; mở Thùng rác (menu ba chấm) và khôi phục; thử xoá vĩnh viễn.
3. Đăng nhập Google. Danh sách chuyển sang danh sách tài khoản, ghi chú local được tải lên (kiểm tra không bị nhân đôi ghi chú đã có trên tài khoản). Biểu tượng ☁ đổi màu.
4. Cài bản 1.5.0 lên máy thứ hai (hoặc thư mục dữ liệu thứ hai), đăng nhập cùng tài khoản: đủ ghi chú, đúng thứ tự, thùng rác khớp.
5. Ngắt mạng trên một máy, sửa vài ghi chú, bật lại: thay đổi tự lên trong vòng ~45 giây (hoặc "Đồng bộ ngay").
6. Xung đột: ngắt mạng ở cả hai máy, sửa **cùng một ghi chú** ở hai nơi, bật mạng lần lượt: mỗi máy phải giữ đủ cả hai đoạn chữ (một ghi chú `[Xung đột] ...` xuất hiện) và có thông báo.
7. Máy A đang mở ghi chú X, máy B xoá X: A phải chuyển sang ghi chú khác kèm thông báo, không văng lỗi.
8. Đăng xuất: quay lại danh sách local; đăng nhập lại: không nhân đôi ghi chú.
9. Dán ảnh vào một ghi chú ở máy A, đồng bộ, mở ở máy B: ảnh hiện ra.

## Phase 4: chi tiết đã làm

### A. Tự gộp theo dòng (`app/merge3.py`)

Khi cả hai phía sửa chữ của cùng một ghi chú, thử gộp ba chiều với `base_content` trước khi tạo bản sao xung đột. Bảo thủ có chủ ý: chỉ gộp khi các chỗ sửa **không chạm nhau**; hai chỗ sửa sát nhau, cùng một dòng, sửa dòng mà bên kia xoá, thiếu `base_content`, hoặc ghi chú quá lớn (> 5000 dòng) đều rơi về bản sao xung đột như trước. Hai bên cùng thêm dòng vào đúng một chỗ (thường là cuối ghi chú) thì giữ cả hai, bản của server trước. Lưới an toàn cuối cùng: kết quả gộp mà thiếu bất kỳ dòng nào một phía đã thêm thì bị bỏ và thành xung đột. Dùng ở hai nơi: khi sync gặp bản mới (`repo._apply_newer`) và khi ô soạn thảo lưu đè lên chữ vừa bị sync đổi (`store.save_note_by_id` trả `SaveOutcome`; gộp được thì ô soạn thảo hiện bản gộp, không có hộp thoại).

### B. Xoá vĩnh viễn báo cho server

- Server: `POST /v2/notes/{id}/purge` xoá nội dung và lịch sử ngay. Chỉ purge được note **đang trong thùng rác** và **đúng revision** người gọi thấy (409 nếu không), nên note vừa được máy khác sửa hoặc khôi phục không bao giờ bị huỷ; gọi lại là no-op.
- Client (schema v3, bảng `pending_purges`): xoá vĩnh viễn note mà server đã biết thì được ghi nhớ đến khi server xác nhận (trash trước nếu server còn coi là sống, rồi purge). Trong lúc đó, pull không nhét note trở lại. Nếu máy khác đã đổi note thì bản của họ quay lại thay vì bị phá. Tự dọn sau 60 ngày ở local không ghi gì (server tự purge theo lịch).

### C. Dọn ảnh mồ côi (server)

Job bảo trì xoá ảnh không còn được tham chiếu bởi note (sống hoặc trong thùng rác), bản lịch sử nào, hay slot v1 của **cùng người dùng**, và đã quá `IMAGE_GC_GRACE_DAYS` (30). Ảnh lỡ bị xoá sẽ được client tự tải lại ở lần đẩy sau (nó luôn so với danh sách ảnh trên server). Đặt ID ảnh theo hash **không làm**: việc dọn ảnh đã giải quyết vấn đề lưu trữ, còn đổi cách đặt tên ảnh chạm vào luồng dán/chụp màn hình mà chưa có lợi ích tương xứng.

### D. Lớp bảo vệ request (server, `app/guard.py`)

| Tính năng | Mặc định | Cấu hình |
|---|---|---|
| Rate limit theo access token (API) | 1200/phút | `RATE_LIMIT_API_PER_MINUTE`, tắt bằng `RATE_LIMIT_ENABLED=false` |
| Rate limit `/auth/*` theo IP | 120/phút | `RATE_LIMIT_AUTH_PER_MINUTE`, `TRUSTED_PROXY_COUNT` |
| Cổng phiên bản (`X-Client-Version` thấp hơn → HTTP 426 trên `/v2`) | tắt | `MIN_CLIENT_VERSION` |
| API cũ `/notes/me`: header `Deprecation` (và `Sunset` nếu đặt) | bật | `LEGACY_SUNSET`; `LEGACY_NOTES_ENABLED=false` để tắt hẳn (HTTP 410) |

Lưu ý triển khai: domain công khai đi qua **Cloudflare tunnel** nên mọi request đến từ địa chỉ của connector; giới hạn `/auth` theo IP đang được dùng chung cho tất cả người dùng cho đến khi đặt `TRUSTED_PROXY_COUNT` (thường là 1, sau khi kiểm `X-Forwarded-For` có IP thật). Máy trạm gửi `X-Client-Version` (từ `app/version.py`) và hiểu 426 ("Cần cập nhật KQ Note...") và 429 (dừng cả chu kỳ, thử lại sau, không đập vào từng note).

### Deploy Phase 4 lên server

Không có thay đổi schema server; tương thích ngược với client 1.4.x (chỉ thêm header `Deprecation` và các giới hạn rộng). Vì có thay đổi hành vi (rate limit bật sẵn), nên: chạy `pg_dump` thủ công, build và `docker compose up -d`, kiểm `/health`, thử đăng nhập và một lượt sync từ client hiện có, xem log có 429 bất thường không. Đặt `MIN_CLIENT_VERSION` hoặc tắt API cũ **chỉ sau khi** mọi thiết bị đã lên 1.5.0.

### Kiểm thử thêm ở Phase 4

Property test cho `merge3` (4000 ca, không mất dòng nào); test ngẫu nhiên nhiều thiết bị giờ chèn ở vị trí bất kỳ (để việc gộp thật sự xảy ra); test purge (endpoint, sổ ghi nhớ, nâng schema, kịch bản engine: offline, máy khác sửa, khởi động lại); test dọn ảnh và lớp bảo vệ ở server; `tests/test_version.py` canh lệch số phiên bản giữa `app/version.py`, `installer.iss`, `version_info.txt` và workflow.
