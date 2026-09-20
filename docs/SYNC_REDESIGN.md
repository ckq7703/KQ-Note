# Thiết kế lại lưu trữ & đồng bộ note (nhiều note / user)

Trạng thái: **Phase 0 (đã có trong code), Phase 1 (server v2, đã deploy) và Phase 2 (lưu trữ local SQLite, chưa phát hành) đã làm.** Phase 3–4 chờ thực hiện.

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
| 2 | Client: SQLite, UUID, thùng rác, gắn account | **Xong** (chưa phát hành; sync vẫn là bản v1, xem bên dưới) |
| 3 | Client: engine sync v2 (outbox, cursor, bản sao xung đột, UI trạng thái) | Chưa |
| 4 | Auto-merge 3-way theo dòng, dọn ảnh, deprecate API cũ, min-version gate | Chưa |

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

### Chưa đổi ở Phase 2

- Luồng đăng nhập/đồng bộ vẫn là bản v1 (một slot trên server, `sync_state.json`, cache `notes.cloud.<id>.txt`) và **chưa gọi `set_scope`**, nên các rủi ro ghi đè giữa các note khi bật cloud vẫn còn cho đến Phase 3 (bản hotfix Phase 0 chỉ giảm nhẹ). Vì vậy không nên phát hành riêng Phase 2 mà nên gộp với Phase 3.
- Local "xoá vĩnh viễn" chưa báo cho server: note đó ở lại thùng rác server tới khi server tự purge sau 60 ngày.

### Lưu ý khi build (bẫy đã gặp)

`xor_obfuscate.py` chèn helper sau dòng `import` cuối của **mọi** file `.py` bằng một regex mà `\s+` nuốt luôn xuống dòng. Hệ quả: dòng import kết thúc bằng chữ "import" (ví dụ module tên `legacy_import`), hoặc `from x import (` nhiều dòng ở cuối, sẽ làm hỏng file và hỏng bản build. `tests/test_build_compat.py` mô phỏng bước này và biên dịch lại mọi module; hãy giữ nó trong CI.

### Chạy test

`python -m unittest discover -s tests -t .` (Linux không màn hình: thêm `xvfb-run -a`; test dialog tự bỏ qua nếu không có display).
