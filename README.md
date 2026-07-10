# yadisk-dump

Download every file from your Yandex.Disk to a local folder. Read-only and resumable.

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![CI](https://github.com/fomin-n/yadisk-dump/actions/workflows/ci.yml/badge.svg)](https://github.com/fomin-n/yadisk-dump/actions/workflows/ci.yml)

[Русская версия](README.ru.md)

## Quickstart

1. Install:

   ```bash
   pipx install git+https://github.com/fomin-n/yadisk-dump
   ```

2. Run:

   ```bash
   yadisk-dump
   ```

3. Paste the OAuth token when asked. The tool opens the correct Yandex page after you press Enter.

<details>
<summary>Other installation methods</summary>

With `uv`:

```bash
uv tool install git+https://github.com/fomin-n/yadisk-dump
```

From a local checkout:

```bash
python -m pip install .
```

Reproducible dependency installation:

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps .
```

</details>

## What it looks like

```text
╭─ Found on disk ──────────────────────────────────────────╮
│   Photos       9,120 files   112.4 GB                    │
│   Videos         840 files    48.1 GB                    │
│   Documents    1,905 files     3.2 GB                    │
│   Other          566 files     4.5 GB                    │
│                                                          │
│   Total       12,431 files   168.2 GB                    │
│                                                          │
│   Destination: ~/YandexDisk   (free: 412 GB)             │
╰──────────────────────────────────────────────────────────╯
  Start download? [Y/n]
```

```text
  ⣷ Photos/2024/IMG_0812.heic      ▕██████████░░░░▏   4.1/6.2 MB
  ⣷ Videos/trip/VID_0553.mp4       ▕███░░░░░░░░░░░▏  38.2/112.0 MB
  ⣷ Documents/thesis_final_v7.pdf  ▕█████████████░▏  11.8/12.1 MB

  Overall ▕█████████░░░░░░░░░░░▏ 62.4/168.2 GB · 7,812/12,431 files
  41.2 MB/s · ETA 43m · ✓ 7,810 downloaded · ↷ 1,204 skipped · ✗ 2 failed
```

Output becomes a plain one-line-per-file log when redirected. `NO_COLOR` and `--quiet` are supported.

## Commands

| Command | Behavior |
|---|---|
| `yadisk-dump` | Wizard if needed, scan, confirmation, and full download |
| `yadisk-dump pull [--to DIR] [--workers N] [--yes]` | Non-interactive scan and download for scripts or cron |
| `yadisk-dump scan` | Scan and print the summary without downloading |
| `yadisk-dump status` | Show saved counters and last-run information |
| `yadisk-dump retry` | Re-download files marked failed |
| `yadisk-dump verify` | Recompute local MD5 checksums; mismatches become retryable failures |
| `yadisk-dump logout` | Delete the saved credential file |

Global options are `--to DIR`, `--workers N` (clamped to 1–5), `--yes`, `--quiet`, and `--version`. The default destination is `~/YandexDisk`; the interactive flow remembers a different choice.

## Security — where your data goes

The runtime network surface is deliberately small:

- `GET https://cloud-api.yandex.net/v1/disk` checks the credential and reads quota data.
- `GET https://cloud-api.yandex.net/v1/disk/resources` lists directories and files.
- `GET https://cloud-api.yandex.net/v1/disk/resources/download` requests one temporary signed URL immediately before each file.
- A plain streaming `GET` reads that signed URL from the Yandex download hostname returned by the API. The OAuth credential is not attached. Cross-host redirects are rejected.
- The OAuth polygon opens in your normal browser only after you press Enter. Package installation may contact PyPI; the running CLI does not.

Proxy and `.netrc` inheritance are disabled, so transfer bytes cannot silently pass through a configured third-party proxy. There is no telemetry, analytics, crash reporting, version check, auto-update, or outbound upload.

The remote side is read-only by construction. The wrapper in [`src/yadisk_dump/api.py`](src/yadisk_dump/api.py) only checks credentials, reads disk information, lists directories, and requests download links. It contains no upload, remove, move, publish, directory-creation, or other mutating Disk call.

The OAuth credential comes from `YADISK_TOKEN` or a local file:

- POSIX: `${XDG_CONFIG_HOME:-~/.config}/yadisk-dump/token`, mode `0600`.
- Windows: `%APPDATA%\yadisk-dump\token`.

It is never printed, logged, included in stored failure reasons, or attached to download-host requests. Signed download URLs are also omitted from errors.

Every remote path component is sanitized. Traversal, symlink escapes, reserved Windows names, forbidden characters, normalized/case-insensitive collisions, and Windows long paths are handled before opening a file. Downloads go to `<name>.part`, are size-checked, flushed, and atomically replaced. SQLite state lives at `<destination>/.yadisk-dump/state.db` in WAL mode.

Audit the static runtime URLs yourself:

```bash
grep -rnE "https?://" src/
```

The result contains only `cloud-api.yandex.net` and the OAuth polygon. Runtime dependencies are limited to `yadisk` and `rich`; `requirements.lock` pins their complete resolved tree.

## FAQ

### How long does the credential last?

Yandex controls OAuth lifetime; tokens commonly last about one year. If it expires, run `yadisk-dump` again and paste a fresh one. An invalid `YADISK_TOKEN` must be replaced or unset because it takes precedence over the saved file.

### What happens on HTTP 429?

The CLI honors `Retry-After`, or waits 60 seconds when it is absent. Rate-limit waits do not consume one of the five normal attempts.

### Can I resume after Ctrl+C or a crash?

Yes. Completed same-size files are skipped on the next run, incomplete `.part` files are removed, and SQLite remembers failures. `yadisk-dump retry` handles failed files directly. The CLI never deletes local files when something disappears remotely.

### What does verification do?

`yadisk-dump verify` hashes completed local files and compares them with the MD5 value reported during the API scan. Missing or mismatched files are marked failed so `yadisk-dump retry` downloads them again. Normal downloads use size checks and do not spend time hashing every existing file.

### Does it support Windows long paths?

Yes. Components are made Windows-safe and absolute paths longer than roughly 240 characters use the extended `\\?\` form, including UNC paths.

### Why not the “unlimited photos” storage?

That mode requires browser cookies and undocumented Photoslice APIs. It is excluded by design. This tool uses only the official [Yandex.Disk REST API](https://yandex.com/dev/disk/rest/).

## License

[MIT](LICENSE) © 2026 fomin-n

