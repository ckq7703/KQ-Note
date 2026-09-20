# KQ Note

KQ Note is a fast desktop note-taking app for Windows. It lives in the system tray, docks flush against a screen edge, stays on top of other windows when you want it to, and keeps every note in sync across your devices through an optional cloud account. It is built for quick reference notes such as command cheat sheets, snippets, checklists and study notes.

The application interface is in Vietnamese.

- [Features](#features)
- [Installation](#installation)
- [Using KQ Note](#using-kq-note)
- [Cloud sync](#cloud-sync)
- [Your data and privacy](#your-data-and-privacy)
- [Troubleshooting](#troubleshooting)
- [Running from source](#running-from-source)
- [Self-hosting the sync server](#self-hosting-the-sync-server)
- [Architecture](#architecture)
- [Releases](#releases)

## Features

**Notes**

- Any number of notes, with instant search across titles and previews.
- Drag and drop to reorder. Reordering changes only the moved note, so it syncs cleanly.
- A trash with 60-day retention: deleted notes can be restored, or deleted permanently.
- Markdown-style formatting rendered in place: headings, bold, italic, inline code, links, block quotes, task lists, numbered and bulleted lists, code blocks, horizontal rules and tables.
- Paste images straight from the clipboard, or capture a screen region from the menu. Web links are highlighted and open in your default browser.
- Optional AI assistant powered by Google Gemini (bring your own API key). Ask questions about the current note, then insert, replace or copy the answer.

**Desktop integration**

- Docks flush against the left or right screen edge as an application bar, so other windows resize instead of sitting underneath it.
- Always-on-top pinning, a system tray icon, and global hotkeys to show or hide the window from any application.
- Scales with display DPI from 100% to 200%.

**Sync and data safety**

- Every note syncs individually, including its order and trash state.
- Works offline. Changes are kept on the device and sent when the network returns.
- Two devices editing the same note never overwrite each other. Edits in different places are merged line by line; edits that collide are kept as a separate conflict note so that no text is lost.
- Notes are stored in a local SQLite database with transactional writes, and the app keeps a safety copy of a note's text before it is permanently deleted.

## Installation

Requirements: Windows 10 or Windows 11 (64-bit).

1. Open the [latest release](https://github.com/ckq7703/KQ-Note/releases/latest).
2. Download one of:
   - `KQNoteSetup-<version>.exe`: installer. Installs per user (no administrator rights needed), with optional desktop shortcut and start-with-Windows.
   - `KQNote-<version>-Portable.zip`: extract anywhere and run `KQNote.exe`.
3. Start KQ Note. It runs in the system tray.

### Upgrading

- Notes from earlier versions are converted automatically on first launch. Your old files are never modified or deleted and remain in `%APPDATA%\NoteCheatsheet` as a backup. Copying that folder before upgrading is still a good habit.
- Upgrade every device you sync. Versions before 1.5 use an older single-note sync protocol and cannot sync with 1.5 and later.
- The first time you sign in, the notes on the device are uploaded to your account as new notes. Nothing already in the account is overwritten, and notes with identical text are not duplicated.

## Using KQ Note

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| Ctrl + Alt + Space | Show or hide the window from anywhere |
| Ctrl + Space | Show the window and pin it on top |
| Ctrl + V | Paste text, or paste an image from the clipboard |
| Ctrl + F | Search within the open note |
| Enter | Jump to the next search match |
| Esc | Clear the search |

The two global hotkeys can be changed in `config.json` (see [Client configuration](#client-configuration)).

### Formatting

Type Markdown syntax directly, or use the toolbar. The syntax is rendered as you type.

| Syntax | Result |
|---|---|
| `# Title`, `## Section` | Headings (levels 1 to 6) |
| `**bold**`, `*italic*`, `` `code` `` | Inline formatting |
| `[text](https://example.com)` | Link |
| `> quote` | Block quote |
| `- [ ] task`, `- [x] done` | Task list |
| `1. item`, `- item`, `+ item` | Numbered list and nested bullet lists |
| Triple backticks | Code block |
| `---` | Horizontal rule |
| Pipe-separated rows | Table |

### Menus

- The list icon at the top left opens the note list. Use the plus button to create a note, the handle on a card to reorder it, and the trash icon to move it to the trash.
- The three-dot menu at the top right pins or unpins the window, captures a screenshot, opens the trash, and holds the account and sync controls.

## Cloud sync

Sync is optional. Without an account, KQ Note is a fully functional local app.

**Signing in.** Choose the sign-in entry in the three-dot menu and authenticate with your Google account in the browser. The desktop app never sees your Google credentials or the OAuth client secret: it only relays a short-lived authorization code to the sync server, which completes the exchange and issues its own tokens.

**What syncs.** Note text, note order, the trash, and pasted images. A cloud icon in the title bar shows the state: synced, syncing, offline (or changes still waiting), or error. The three-dot menu gives details and a manual "sync now".

**When it syncs.** A few seconds after you change something, every 45 seconds in the background, and on demand.

**Conflicts.** The sync server never accepts a write based on an out-of-date copy. When the same note changed on two devices:

1. If the edits touch different parts of the note, they are merged into one note.
2. If they touch the same lines, the version from the other device becomes the note and your version is kept as a new note whose title starts with `[Xung đột]` (Vietnamese for "conflict"). Merge what you need and delete the copy.
3. If one device edited a note that another deleted, the edit wins and the note comes back.

**Deleting.** Deleting a note moves it to the trash on every device. "Delete permanently" removes it from the server as well. Trashed notes are removed automatically after 60 days.

**Signing out** returns you to the local-only notes. The account's notes stay on the device (hidden), so signing back in is instant and does not duplicate anything.

## Your data and privacy

Everything is stored under `%APPDATA%\NoteCheatsheet`:

| Path | Contents |
|---|---|
| `kqnote.sqlite3` (plus `-wal` and `-shm`) | Notes, sync state, settings |
| `images\` | Pasted and captured images |
| `backups\` | Safety copies of notes before they were permanently deleted (newest 200 kept) |
| `config.json` | Window and hotkey settings, sync server URL |
| `startup_error.log` | Written only if the note database cannot be opened |

- Sign-in tokens are kept in the Windows Credential Manager, not in files.
- If you use the AI assistant, the text of your question and the note context is sent to Google's Gemini API using the API key you provide. The key is stored in the local database.
- If you use cloud sync, note text and images are sent to, and stored on, the sync server configured in `sync_server_url` (see [Self-hosting](#self-hosting-the-sync-server) to run your own).
- If you copy the database by hand, copy the `-wal` and `-shm` files with it.

## Troubleshooting

- **"Sign-in failed" with a long block of HTML mentioning Cloudflare and error 502.** The sync server was briefly unreachable. Wait a minute and try again. Your notes are unaffected.
- **The app shows an error about opening the note database.** Your notes have not been changed. Look at `startup_error.log` in `%APPDATA%\NoteCheatsheet`, and keep the folder for support.
- **A note titled `[Xung đột] ...` appeared.** Two devices edited the same lines. Copy what you want to keep into the original note, then delete the conflict note.
- **I deleted a note by mistake.** Open the trash from the three-dot menu. Permanently deleted content is also kept as a text file in `backups\`.
- **Status shows "offline" or "N changes waiting".** The server is not reachable. Changes are safe on the device and are sent automatically when the connection returns.

## Running from source

Requirements: Python 3.11 or newer on Windows (the app uses `pywin32` and Windows APIs).

```
pip install -r requirements.txt
python main.py
```

### Client configuration

`config.json` in the data folder overrides these defaults:

| Key | Default | Meaning |
|---|---|---|
| `hotkey` | `<ctrl>+<alt>+<space>` | Show or hide the window |
| `hotkey_pin` | `<ctrl>+<space>` | Show the window and pin it on top |
| `always_on_top` | `true` | Keep the window above other windows |
| `sync_server_url` | `https://note.smartpro.com.vn` | Sync server to use |

### Tests

The client tests use only the standard library for the storage and sync-decision layers. The sync engine and window tests need the backend's requirements plus `requests`, `Pillow` and `keyring`, and skip themselves when those are missing.

```
python -m unittest discover -s tests -t .
```

For the full suite, run it in an environment that has both the backend requirements and the client requirements. On a machine without a display, prefix the command with `xvfb-run -a`. The engine tests start a real copy of the backend on a scratch SQLite database and simulate several devices, including network loss, lost responses and a randomized multi-device scenario that checks that no typed text is ever lost.

Backend tests:

```
cd backend
pip install -r requirements-dev.txt
pytest
```

Set `TEST_DATABASE_URL` to a disposable PostgreSQL database to also run the concurrency tests. Never point it at a real database: the tests drop and recreate tables.

### Building a release

Releases are built by GitHub Actions when a tag matching `v*` is pushed. The workflow compiles the app with Nuitka, packages the installer with Inno Setup, builds the portable archive and publishes a GitHub release. The version number appears in `app/version.py`, `installer.iss`, `version_info.txt` and `.github/workflows/release.yml`; a test fails if they disagree.

## Self-hosting the sync server

The server is a FastAPI application backed by PostgreSQL, packaged with Docker Compose.

```
cd backend
cp .env.example .env      # then edit the values
docker compose up -d --build
```

Point clients at it by setting `sync_server_url` in their `config.json`. Put the API behind an HTTPS reverse proxy; it does not terminate TLS itself. The Compose file also starts a `db_backup` service that writes a compressed `pg_dump` to a volume every 24 hours and keeps the newest 14. Copy those dumps off the machine for real disaster recovery.

### Server settings

Set these in `backend/.env`. Only the first group is required.

| Variable | Default | Purpose |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | none | Database credentials |
| `JWT_SECRET_KEY` | none | Secret used to sign access and refresh tokens. Use a long random value |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | none | OAuth "Desktop app" client used to verify Google sign-in. The secret lives only on the server |
| `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS` | 15, 30 | Token lifetimes |
| `RATE_LIMIT_ENABLED` | `true` | Enable request rate limiting |
| `RATE_LIMIT_API_PER_MINUTE` | 1200 | Per access token |
| `RATE_LIMIT_AUTH_PER_MINUTE` | 120 | Per client address, for `/auth/*` |
| `TRUSTED_PROXY_COUNT` | 0 | Number of reverse proxies in front of the API. Set it correctly, or every user behind a proxy shares one address and one limit |
| `MIN_CLIENT_VERSION` | empty | Older desktop versions receive HTTP 426 on the note API. Empty allows all |
| `LEGACY_NOTES_ENABLED` | `true` | Serve the single-note API used by versions before 1.5 |
| `LEGACY_SUNSET` | empty | Optional `Sunset` header value for the legacy API |
| `IMAGE_GC_GRACE_DAYS` | 30 | Unreferenced images are deleted after this many days |

Limits and retention (per user): notes up to 1 MB, up to 5000 notes, trash kept 60 days, tombstones kept 90 days, up to 50 saved revisions per note for 30 days.

### API overview

All note endpoints require a bearer token. `GET /health` is public.

| Endpoint | Purpose |
|---|---|
| `POST /auth/google`, `/auth/register`, `/auth/login`, `/auth/refresh`, `GET /auth/me` | Sign-in and account |
| `GET /v2/notes/changes?cursor=` | Change feed since a cursor (returns HTTP 410 when a full resync is needed) |
| `PUT /v2/notes/{id}` | Create or update. A stale `base_rev` returns HTTP 409 with the server copy and never overwrites |
| `POST /v2/notes/{id}/trash`, `/restore`, `/purge` | Trash, restore, delete permanently |
| `PATCH /v2/notes/{id}` | Reorder |
| `GET /v2/notes/{id}/revisions` | Version history |
| `GET`, `PUT /images/{id}` | Image blobs |
| `GET`, `PUT /notes/me` | Deprecated single-note API for versions before 1.5 |

On startup the server migrates an older single-note database automatically, in one transaction, and keeps the old table as `notes_legacy` for rollback.

## Architecture

```
app/
  notes_widget.py    Main window, editor and note list
  store.py           SQLite storage, trash, ordering, account scoping
  markup.py          Markdown-style rendering and serialization
  merge3.py          Conservative line-based three-way merge
  fracindex.py       Fractional ordering keys
  sync/
    repo.py          All sync decisions (no network): what to push, how to apply server changes
    engine.py        Sync cycles, threading, retries, events for the UI
    client.py        HTTP client for the sync API
    google_oauth.py  Loopback OAuth flow with PKCE
backend/app/         FastAPI service: notes, revisions, images, auth, maintenance jobs
```

Design principles:

- The local database is the working copy and the server is the sync point. Each account note remembers both its local state and the last state the server confirmed; what remains to be sent is derived from the difference. A crash between two steps therefore cannot lose work, and every step is safe to repeat.
- A stale write is never applied. The server compares revisions and answers with its own copy; the client keeps the local text as a conflict note rather than picking a winner.
- Design notes and decisions, including the migration and rollback procedures, are in [docs/SYNC_REDESIGN.md](docs/SYNC_REDESIGN.md) (written in Vietnamese).

## Releases

Release notes and downloads are on the [Releases page](https://github.com/ckq7703/KQ-Note/releases).

## License

No license file is included in this repository yet. Until one is added, all rights are reserved by the author.
