# v1_refine — Feature Roadmap

Ideas and future improvements, roughly ordered by usefulness.

---

## High Priority

- **Resume interrupted playlists** — save playlist progress to a `.json` state file. On next run, detect the file and offer to continue from where you left off instead of restarting.

- **Batch download from .txt file** — add a `(B)atch` option that reads a plain text file with one URL per line and downloads them all in sequence.

- **Configurable output folder** — let the user set a custom save path (stored in `settings.json`) instead of the hardcoded `audio/` and `video/` folders.

- **Retry failed downloads** — after a playlist finishes, offer to retry all failed entries automatically (already have the list, just re-run them).

---

## Quality of Life

- **Download history / log** — append a line to `history.txt` after each successful download: timestamp, title, URL, format, file size. Add a `(H)istory` menu option to view recent entries.

- **Queue system** — add multiple URLs to a queue before starting. Shows "Queue: 4 items" in the header. Download them one after another without re-entering the menu.

- **Default format preference** — save last-used format (MP4/MP3) and quality in `settings.json` so you don't have to pick them every time. Offer a `(S)ettings` menu to configure defaults.

- **Auto quality fallback** — if the chosen resolution isn't available for a video in a playlist, automatically fall back to the next best available instead of failing.

- **Speed / bandwidth limiter** — add a rate limit option (e.g. max 5 MiB/s) so downloads don't choke other internet activity. Stored in settings.

---

## Extra Features

- **Thumbnail download** — option to save the video thumbnail as `.jpg` alongside the downloaded file.

- **Subtitle download** — for MP4 downloads, optionally fetch auto-generated or available subtitles and embed them into the file.

- **Windows toast notification** — pop a Windows notification when a download (or whole playlist) finishes, so you don't have to watch the terminal.

- **Update yt-dlp from within the app** — add a `(U)pdate` menu option that runs `pip install -U yt-dlp` and reports the new version. Useful since yt-dlp updates frequently.

- **Channel download** — support YouTube channel URLs (download all videos from a channel), similar to playlist but with potentially thousands of entries.

- **Search YouTube directly** — add a `(/)` search mode: user types a query, app shows top 5–10 results with titles/durations, user picks one to download.

- **Concurrent downloads** — download 2–3 playlist items in parallel to speed up large playlists (use `ThreadPoolExecutor`). Needs careful progress bar management.

---

## Bugs / Known Issues

- Cookies DPAPI error when running as Administrator — workaround is `cookies.txt` file (already implemented). No fix on our side; upstream yt-dlp issue [#10927](https://github.com/yt-dlp/yt-dlp/issues/10927).

- Pause blocks yt-dlp's download thread in the progress hook — works fine for short pauses but very long pauses (>60s) may cause a socket timeout depending on the server. yt-dlp will retry automatically in most cases.

- Already-exists check uses `%(title)s.%(ext)s` pattern — if yt-dlp sanitizes the filename differently (e.g. replaces special characters), the check may miss the file and re-download it.
