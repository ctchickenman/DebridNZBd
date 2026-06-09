# Dead Code Analysis Report

Generated: 2026-06-04

This report catalogues unused code found across the DebridNZBd codebase. No changes have been made — this is a reference for future cleanup.

## Summary

| Category | Count |
|----------|------:|
| Unused imports | 8 |
| Unused functions/methods | 6 |
| Unused classes/models | 7 |
| Unused variables/constants | 2 |
| Unreachable code paths | 1 |
| Commented-out code | 1 |
| Duplicated code | 1 |
| **Total** | **26** |

---

## 1. Unused Imports

### `debridnzbd/api/history.py` — line 10

`import time` — The `time` module is imported but never referenced in the file.

### `debridnzbd/db/database.py` — line 12

`import os` — The `os` module is imported but never used in runtime code (only appears in comments and docstrings).

### `debridnzbd/db/database.py` — line 14

`from typing import Any` — `Any` is imported but never used in any type annotation.

### `debridnzbd/core/config_store.py` — line 24

`from typing import Any` — `Any` is imported but never used in any type annotation.

### `debridnzbd/torbox/models.py` — line 27

`from typing import Literal` — `Literal` is imported but never used in any type annotation.

### `debridnzbd/torbox/client.py` — line 88

`TorboxDownloadLink` — Imported from `debridnzbd.torbox.models` but never instantiated. The `request_*_dl` methods return plain strings.

### `debridnzbd/torbox/client.py` — line 84

`TorboxControlOperation` — Imported but never used. Control operations are sent as plain dicts.

### `debridnzbd/torbox/client.py` — lines 85–87

`TorboxCreateTorrentRequest`, `TorboxCreateUsenetRequest`, `TorboxCreateWebDownloadRequest` — All three request models are imported but never instantiated. The client uses multipart form data instead.

### `debridnzbd/api/config.py` — line 13

`ConfigResponse` — Imported from `debridnzbd.db.models` but never used. `handle_get_config` returns a plain dict via `JSONResponse`.

---

## 2. Unused Functions/Methods

### `debridnzbd/api/auth.py` — line 56

`redact_config_value(section, keyword, value)` — Defined but never called. The `SENSITIVE_KEYWORDS` constant on line 50 is only used by this dead function. Note: `config_store.py` has its own separate `SENSITIVE_KEYWORDS` that *is* actively used.

### `debridnzbd/api/auth.py` — line 262

`check_api_access(request)` — Defined as a FastAPI dependency but only called from within `check_full_access()` (see next entry). Never used as a route dependency or called from any other file.

### `debridnzbd/api/auth.py` — line 275

`check_full_access(request)` — Defined as a FastAPI dependency but never imported or used anywhere in the codebase. No route handler or middleware calls it.

### `debridnzbd/utils/nzo_id.py` — line 11

`generate_nzf_id()` — Defined but never called. Only `generate_nzo_id()` from the same file is used in production code.

### `debridnzbd/utils/diskspace.py` — line 122

`has_minimum_space(path, minimum_bytes)` — Defined but never called. Production code uses `get_disk_usage()` directly. The only reference to `has_minimum_space` is in its own docstring.

### `debridnzbd/torbox/client.py` — lines 832, 834, 1055, 1235

`check_usenet_cached()`, `check_torrent_cached()`, `check_web_cached()` — These cache-checking methods are defined on `TorboxClient` but never called from production code. They are public API methods on the client class (tested in `test_torbox_client.py`) and may be used by future features like cache-aware download routing. Consider removing if not planned for use.

### `debridnzbd/torbox/client.py` — line 1269

`get_hosters_list()` — Defined on `TorboxClient` but never called from production code. Same status as the cache-checking methods — it's a public API method with test coverage. Consider removing if not planned for use.

---

## 3. Unused Classes/Models

### `debridnzbd/db/models.py` — line 36

`SABnzbdError` — Pydantic model defined but never instantiated. Error responses use plain dicts with `JSONResponse`.

### `debridnzbd/db/models.py` — line 267

`VersionResponse` — Model defined but never used. The version endpoint returns a plain dict via `JSONResponse`.

### `debridnzbd/db/models.py` — line 275

`AuthResponse` — Model defined but never used. The auth endpoint returns a plain dict via `JSONResponse`.

### `debridnzbd/db/models.py` — line 287

`ConfigSection` — Empty model class (body is `pass`) that is never used or referenced anywhere.

### `debridnzbd/torbox/models.py` — line 163

`TorboxDownloadLink` — Model defined and exported but never instantiated. CDN download methods return plain strings.

### `debridnzbd/torbox/models.py` — line 254

`TorboxControlOperation` — Model defined, exported, and imported into `client.py` but never instantiated. Control operations use plain dicts.

### `debridnzbd/torbox/models.py` — lines 265, 277, 290

`TorboxCreateUsenetRequest`, `TorboxCreateTorrentRequest`, `TorboxCreateWebDownloadRequest` — All three request models are defined, exported, and imported into `client.py` but never instantiated. The client uses multipart form data.

---

## 4. Unused Variables/Constants

### `debridnzbd/api/auth.py` — line 50

`SENSITIVE_KEYWORDS` — This constant is only used by the dead `redact_config_value()` function. `config_store.py` has its own separate copy that is actively used. This is a duplicate that could be removed along with `redact_config_value`.

### `debridnzbd/db/database.py` — line 375

Module-level `db` singleton — The variable is documented as being used "throughout the application via `from debridnzbd.db.database import db`" but no production code imports it. All production code accesses the database via `app.state.db`. Only test code references the module. The global variable and its comment are misleading.

---

## 5. Unreachable Code Paths

### `debridnzbd/api/auth.py` — lines 232–244

The `auth_level == ""` branch in `auth_middleware` contains an unreachable else branch. When `validate_api_key()` fails with an empty API key, it returns `(False, "")`. The middleware then checks `if not is_valid:` (True) and `if auth_level == "":` (True), returning "API key required". The second branch at line 241 (checking for a non-empty invalid `auth_level`) can never be reached because `validate_api_key` only ever returns `("", "")` on auth failure, never a non-empty auth level string.

---

## 6. Commented-Out Code

### `debridnzbd/api/router.py` — lines 232–244

Commented-out mode handler entries in the `MODES` dispatch dict:

```python
# "addlocalfile": handle_addlocalfile,
# "change_script": handle_change_script,
# "change_opts": handle_change_opts,
# "rename": handle_rename,
# "get_files": handle_get_files,
# "sort": handle_sort_queue,
# "mark_as_completed": handle_mark_as_completed,
# "set_apikey": handle_set_apikey,
# "set_nzbkey": handle_set_nzbkey,
# "shutdown": handle_shutdown,
# "restart": handle_restart,
```

These represent planned-but-not-yet-implemented API modes. They serve as documentation of future intent but are currently dead code. Note: `addfile` was previously in this list but has since been implemented and is now an active handler.

---

## 7. Duplicated Code

### `debridnzbd/api/queue.py` lines 129–169 ≈ `debridnzbd/core/state_sync.py` lines 56–96

`_extract_download_name()` is defined nearly identically in both files. `web/routes.py` imports it from `state_sync.py`, while `api/queue.py` uses its own local copy. The duplicated logic should be consolidated — either import from a shared utility module or have `queue.py` import from `state_sync.py`.