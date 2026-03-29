from rich.console import Console
from rich.progress import (
    Progress, BarColumn, TextColumn,
    TaskProgressColumn, DownloadColumn, TransferSpeedColumn,
    SpinnerColumn, TimeElapsedColumn, ProgressColumn,
)
from rich.panel import Panel
from rich.table import Table
from rich.rule import Rule
from rich.text import Text
from rich.live import Live
from rich.columns import Columns
from rich import box
import concurrent.futures
import datetime
import io
import json
import math
import msvcrt
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import yt_dlp

console     = Console()
DEBUG       = False
stop_flag   = False
pause_event = threading.Event()
settings    = {}

VERSION = '2.0'


# ── App directory / settings ───────────────────────────────────────────────

def _app_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(_app_dir(), 'settings.json')

def load_settings():
    global settings
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                settings = json.load(f)
    except Exception:
        settings = {}

def save_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
    except Exception as e:
        console.print(f'[red]  Could not save settings: {e}[/red]')


def _history_path():
    return os.path.join(_app_dir(), 'history.json')

def _load_history():
    try:
        with open(_history_path(), 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []

def _save_history_entry(title, url, fmt, quality, duration_secs):
    history = _load_history()
    history.insert(0, {
        'title': title,
        'url': url,
        'format': fmt,
        'quality': quality,
        'duration': duration_secs,
        'date': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
    })
    history = history[:500]
    try:
        with open(_history_path(), 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ── Silent logger ──────────────────────────────────────────────────────────

class _SilentLogger:
    def debug(self, msg):
        pass
    def info(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        pass


class _DebugLogger:
    """Thread-safe logger that collects messages for deferred display."""
    def __init__(self):
        self._lock = threading.Lock()
        self.messages = []
    def _add(self, level, msg):
        with self._lock:
            self.messages.append((level, str(msg)))
    def debug(self, msg):
        self._add('debug', msg)
    def info(self, msg):
        self._add('info', msg)
    def warning(self, msg):
        self._add('warn', msg)
    def error(self, msg):
        self._add('error', msg)


class _FatBarColumn(ProgressColumn):
    """Block-style progress bar: [████████░░░░░░░░] 80%"""

    def __init__(self, width=35):
        super().__init__()
        self.width = width

    def render(self, task):
        if task.total is None or task.total == 0:
            t = time.time() * 2.5
            pos = int((math.sin(t) + 1) / 2 * (self.width - 4))
            pos = max(0, min(pos, self.width - 4))
            result = Text('[')
            if pos > 0:
                result.append('░' * pos, style='dim')
            result.append('████', style='cyan bold')
            rem = self.width - pos - 4
            if rem > 0:
                result.append('░' * rem, style='dim')
            result.append(']')
            return result

        ratio = min(1.0, max(0.0, task.completed / task.total))
        filled = int(ratio * self.width)

        result = Text('[')
        if filled > 0:
            result.append('█' * filled, style='bold green')
        unfilled = self.width - filled
        if unfilled > 0:
            result.append('░' * unfilled, style='dim')
        result.append(']')
        result.append(f' {ratio * 100:3.0f}%', style='bold white')
        return result


# ── Helpers ────────────────────────────────────────────────────────────────

def _jpeg_dimensions(data):
    """Extract (width, height) from raw JPEG bytes by parsing SOF markers."""
    i = 0
    while i < len(data) - 8:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xD8:          # SOI
            i += 2
            continue
        if marker == 0xD9:          # EOI
            break
        if marker in (0xC0, 0xC1, 0xC2):   # SOF0 / SOF1 / SOF2
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return w, h
        if i + 3 < len(data):
            seg_len = (data[i + 2] << 8) | data[i + 3]
            i += 2 + seg_len
        else:
            break
    return None, None


def _has_cover_art(filepath):
    """Return True if an MP3 has a square embedded cover (APIC tag).

    Non-square covers (e.g. 1280×720 YouTube thumbnails with coloured bars)
    are treated as missing so the file gets re-downloaded with a proper crop.
    """
    try:
        from mutagen.id3 import ID3
        tags = ID3(filepath)
        for k in tags.keys():
            if k.startswith('APIC'):
                w, h = _jpeg_dimensions(tags[k].data)
                if w and h:
                    ratio = max(w, h) / max(min(w, h), 1)
                    return ratio < 1.05      # square within 5 % tolerance
                return True                  # can't parse dims → assume OK
        return False
    except Exception:
        return False


def _clean_error(e):
    msg = str(e)
    msg = re.sub(r'^ERROR:\s+\[[^\]]+\]\s+\S+:\s+', '', msg)
    msg = re.sub(r'^ERROR:\s+', '', msg)
    return msg.strip()


def _is_auth_error(e):
    msg = str(e).lower()
    return any(k in msg for k in (
        'format is not available', 'requested format', 'sign in',
        'age-restricted', 'private video', 'members only',
        'this video is not available', 'confirm your age',
    ))


def _is_fatal_error(e):
    """Return True for errors where an alternative search would also fail."""
    msg = str(e).lower()
    return any(k in msg for k in (
        'private video', 'video unavailable', 'has been removed',
        'account associated', 'age-restricted', 'members only',
        'this video is not available',
    ))


def _fmt_speed(speed):
    if not speed:
        return '—'
    for unit in ('B/s', 'KiB/s', 'MiB/s', 'GiB/s'):
        if speed < 1024:
            return f'{speed:.1f} {unit}'
        speed /= 1024
    return f'{speed:.1f} TiB/s'


def _fmt_eta(eta):
    if eta is None:
        return '?'
    m, s = divmod(int(eta), 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def _fmt_duration(seconds):
    if not seconds:
        return '?'
    h, rem = divmod(int(seconds), 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f'{h}h {m}m'
    if m:
        return f'{m}m {s}s'
    return f'{s}s'


def _fmt_bytes(b):
    if not b:
        return '0 B'
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if abs(b) < 1024:
            return f'{b:.1f} {unit}'
        b /= 1024
    return f'{b:.2f} TiB'


def _notify(title, message, duration=5):
    """Fire a Windows toast notification. Silently skips if unavailable."""
    if not settings.get('notifications', True):
        return
    try:
        from winotify import Notification, audio
        toast = Notification(
            app_id='yt-dlp Downloader',
            title=title,
            msg=message,
            duration='short',
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass


def _musicbrainz_tag(filepath, title, duration_secs):
    """Query MusicBrainz for title metadata and fill missing ID3 tags."""
    if not settings.get('auto_tag', True):
        return
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC
        import urllib.parse

        tags = ID3(filepath)
        has_artist = any(k.startswith('TPE1') for k in tags.keys())
        has_album  = any(k.startswith('TALB') for k in tags.keys())
        if has_artist and has_album:
            return

        query = urllib.parse.quote(title)
        mb_url = (
            f'https://musicbrainz.org/ws/2/recording/?query={query}'
            f'&fmt=json&limit=5'
        )
        req = urllib.request.Request(mb_url, headers={
            'User-Agent': 'yt-dlp-downloader/2.0 ( github.com/user/repo )'
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        recordings = data.get('recordings', [])
        if not recordings:
            return

        best = None
        best_score = 999999
        for rec in recordings[:5]:
            rec_dur = (rec.get('length') or 0) / 1000
            if duration_secs and rec_dur:
                diff = abs(rec_dur - duration_secs)
                if diff < best_score:
                    best_score = diff
                    best = rec
            elif best is None:
                best = rec

        if best is None or (duration_secs and best_score > 30):
            return

        artist = ''
        if best.get('artist-credit'):
            artist = best['artist-credit'][0].get('name', '')

        album = ''
        if best.get('releases'):
            album = best['releases'][0].get('title', '')

        year = ''
        if best.get('releases'):
            date = best['releases'][0].get('date', '')
            year = date[:4] if date else ''

        if artist and not has_artist:
            tags['TPE1'] = TPE1(encoding=3, text=artist)
        if album and not has_album:
            tags['TALB'] = TALB(encoding=3, text=album)
        if year:
            has_year = any(k.startswith('TDRC') for k in tags.keys())
            if not has_year:
                tags['TDRC'] = TDRC(encoding=3, text=year)

        tags.save(filepath)
        if DEBUG:
            console.print(f'  [dim]Tagged: {artist} / {album} ({year})[/dim]')

    except Exception as e:
        if DEBUG:
            console.print(f'  [dim]MusicBrainz tag failed: {e}[/dim]')


# ── Thumbnail renderer ─────────────────────────────────────────────────────

def _fetch_thumbnail_pixels(url, cols=20, rows=10):
    """
    Download thumbnail URL and return a 2D list of (r,g,b) tuples
    sized cols × rows for terminal rendering.
    Returns None on any failure.
    """
    try:
        from PIL import Image
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = resp.read()
        img = Image.open(io.BytesIO(data)).convert('RGB')
        img = img.resize((cols, rows), Image.LANCZOS)
        pixels = []
        for y in range(rows):
            row = []
            for x in range(cols):
                row.append(img.getpixel((x, y)))
            pixels.append(row)
        return pixels
    except Exception:
        return None


def _render_thumbnail(pixels, cols=20, rows=10):
    """
    Render a pixel grid as a Rich Text block using half-block characters (▄).
    Each terminal row represents 2 image rows using ▄ with fg+bg colors.
    Returns a rich Text object.
    """
    text = Text()
    # Process 2 image rows per terminal row using ▄ (lower half block)
    for y in range(0, rows - (rows % 2), 2):
        for x in range(cols):
            top = pixels[y][x]
            bot = pixels[y + 1][x] if y + 1 < rows else (0, 0, 0)
            # top color = background, bot color = foreground (▄ fills lower half)
            fg = f'rgb({bot[0]},{bot[1]},{bot[2]})'
            bg = f'rgb({top[0]},{top[1]},{top[2]})'
            text.append('▄', style=f'{fg} on {bg}')
        text.append('\n')
    return text


def _placeholder_thumbnail(video_id, cols=20, rows=5):
    """Return a colored placeholder block when thumbnail can't be fetched."""
    colors = [
        ('bright_red', 'dark_red'), ('bright_blue', 'navy_blue'),
        ('bright_magenta', 'purple4'), ('bright_cyan', 'dark_cyan'),
        ('bright_green', 'dark_green'), ('bright_yellow', 'dark_goldenrod'),
    ]
    fg_col, bg_col = colors[hash(video_id or '') % len(colors)]
    text = Text()
    for y in range(rows):
        for x in range(cols):
            text.append('▄', style=f'{fg_col} on {bg_col}')
        text.append('\n')
    return text


def _make_thumbnail(entry, cols=20, rows=10):
    """Fetch and render thumbnail for an entry dict, or return placeholder."""
    thumb_url = entry.get('thumbnail') or entry.get('thumbnails', [{}])[-1].get('url', '') if isinstance(entry.get('thumbnails'), list) else ''
    if not thumb_url:
        # Try common YouTube thumbnail URL
        vid = entry.get('id', '')
        if vid:
            thumb_url = f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg'

    if thumb_url:
        pixels = _fetch_thumbnail_pixels(thumb_url, cols=cols, rows=rows)
        if pixels:
            return _render_thumbnail(pixels, cols=cols, rows=rows)

    return _placeholder_thumbnail(entry.get('id', ''), cols=cols, rows=rows)


# ── Arrow-key result picker ────────────────────────────────────────────────

def _arrow_pick(results):
    """
    Interactive arrow-key picker for a list of search results.
    Shows thumbnail on the right when a result is highlighted.
    Returns the selected entry dict, or None if cancelled.
    """
    n = len(results)
    idx = [0]
    # Pre-fetch thumbnail for first item in background
    thumb_cache = {}

    def _prefetch(i):
        if i not in thumb_cache:
            thumb_cache[i] = _make_thumbnail(results[i], cols=22, rows=11)

    threading.Thread(target=_prefetch, args=(0,), daemon=True).start()

    def _make_display():
        sel = idx[0]
        entry = results[sel]

        # ── Thumbnail panel ──────────────────────────────
        thumb = thumb_cache.get(sel) or _placeholder_thumbnail(entry.get('id', ''), cols=22, rows=11)
        thumb_panel = Panel(
            thumb,
            box=box.ROUNDED,
            border_style='bright_blue',
            padding=(0, 0),
            width=26,
        )

        # ── Results list ──────────────────────────────────
        t = Table(
            box=None, show_header=False,
            padding=(0, 1), expand=False,
            min_width=56,
        )
        t.add_column(width=2)
        t.add_column(width=50)
        t.add_column(width=9, justify='right')

        for i, r in enumerate(results):
            is_sel = (i == sel)
            dur = _fmt_duration(r.get('duration'))
            chan = (r.get('channel') or r.get('uploader') or 'Unknown')[:26]
            title = (r.get('title') or 'Unknown')

            source_tag = ''
            url = r.get('webpage_url') or r.get('url') or ''
            if 'music.youtube.com' in url:
                source_tag = ' [bright_magenta]♪[/bright_magenta]'

            if is_sel:
                arrow = '[bold bright_blue]▶[/]'
                title_style = 'bold white'
                meta_style  = 'bright_blue'
                dur_style   = 'bold bright_blue'
            else:
                arrow = ' '
                title_style = 'dim white'
                meta_style  = 'dim'
                dur_style   = 'dim'

            title_line = Text(title[:50], style=title_style)
            meta_line  = Text(f'  {chan}', style=meta_style)

            cell = Text()
            cell.append_text(title_line)
            cell.append('\n')
            cell.append_text(meta_line)
            if source_tag:
                cell.append(' ♪', style='bright_magenta')

            t.add_row(Text(arrow), cell, Text(dur, style=dur_style))

            if i < n - 1:
                t.add_row('', Text('', style=''), '')

        list_panel = Panel(
            t,
            title=f'[bold]Search Results[/bold]  [dim]{sel + 1}/{n}[/dim]',
            subtitle='[dim]↑↓ navigate   Enter select   Esc cancel[/dim]',
            box=box.ROUNDED,
            border_style='bright_blue',
            padding=(0, 1),
        )

        return Columns([list_panel, thumb_panel], padding=(0, 1))

    selected = [None]
    cancelled = [False]

    with Live(_make_display(), console=console, refresh_per_second=15,
              vertical_overflow='visible') as live:
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue

            ch = msvcrt.getwch()

            if ch in ('\x00', '\xe0'):          # extended key prefix
                ch2 = msvcrt.getwch()
                if ch2 == 'H':                  # up arrow
                    idx[0] = (idx[0] - 1) % n
                    threading.Thread(target=_prefetch, args=(idx[0],), daemon=True).start()
                    live.update(_make_display())
                elif ch2 == 'P':                # down arrow
                    idx[0] = (idx[0] + 1) % n
                    threading.Thread(target=_prefetch, args=(idx[0],), daemon=True).start()
                    live.update(_make_display())

            elif ch == '\r' or ch == '\n':      # Enter
                selected[0] = results[idx[0]]
                break

            elif ch == '\x1b':                  # Escape
                cancelled[0] = True
                break

            elif ch == '\x03':                  # Ctrl+C
                cancelled[0] = True
                break

    return None if cancelled[0] else selected[0]


def _save_path(choice):
    """Return the save directory for this format, respecting settings."""
    custom = settings.get('output_folder', '').strip()
    if custom:
        base = custom
    else:
        base = 'audio' if choice == 'mp3' else 'video'
    os.makedirs(base, exist_ok=True)
    return base


def build_format(choice, quality):
    if choice == 'mp3':
        return 'bestaudio/best'
    if quality == 'max':
        return 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    if quality == 'min':
        return 'worstvideo[ext=mp4]+worstaudio[ext=m4a]/worst[ext=mp4]/worst'
    return (
        f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]'
        f'/best[height<={quality}][ext=mp4]/best[ext=mp4]/best'
    )


def _ydl_base(logger=None):
    base = {
        'js_runtimes':       {'node': {}},
        'remote_components': {'ejs:github'},
        'format':            'bestaudio*+bestvideo*/best',
        # Crop thumbnails to 1:1 square during the WebP→JPEG conversion step.
        # crop=ih:ih center-crops to a square using the height; no-op if already square.
        # mjpeg codec with max quality ensures clean JPEG for embedding.
        'postprocessor_args': {
            'thumbnailsconvertor+ffmpeg_o': [
                '-vf', 'crop=ih:ih',
                '-vcodec', 'mjpeg', '-qmin', '1', '-qscale:v', '1',
            ],
        },
        'quiet': True,
        'no_warnings': True,
        'noprogress': True,
        'logger': logger if logger is not None else _SilentLogger(),
    }
    cf = settings.get('cookies_file', '')
    if cf and os.path.isfile(cf):
        base['cookiefile'] = cf
    return base


def _postprocessors(choice, quality):
    """Return postprocessors list including metadata embedding."""
    pp = []
    if choice == 'mp3':
        pp.append({
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality,
        })
    # Convert thumbnail WebP → JPEG and crop to 1:1 square in one pass.
    # The crop filter is injected via postprocessor_args in _ydl_base().
    pp.append({'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'})
    # Embed metadata (title, artist, album, etc.)
    pp.append({'key': 'FFmpegMetadata', 'add_metadata': True})
    pp.append({'key': 'EmbedThumbnail', 'already_have_thumbnail': False})
    return pp


# ── Progress bar ───────────────────────────────────────────────────────────

def _make_progress():
    return Progress(
        TextColumn('    '),
        SpinnerColumn('dots'),
        _FatBarColumn(width=35),
        TextColumn('{task.fields[info]}'),
        console=console,
        expand=False,
        transient=False,
    )


# ── Key listener (P = pause) ───────────────────────────────────────────────

def _key_listener(stop_evt):
    while not stop_evt.is_set():
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch == 'p':
                    if pause_event.is_set():
                        pause_event.clear()
                    else:
                        pause_event.set()
                elif ch == '\x03':  # Ctrl+C
                    import _thread
                    _thread.interrupt_main()
        except Exception:
            pass
        time.sleep(0.05)


# ── yt-dlp version check / update ─────────────────────────────────────────

def _get_ytdlp_version():
    try:
        return yt_dlp.version.__version__
    except Exception:
        return '?'


def check_ytdlp_update(silent=False):
    """Check PyPI for a newer yt-dlp version. Returns (current, latest, is_outdated)."""
    import urllib.request
    try:
        with urllib.request.urlopen(
            'https://pypi.org/pypi/yt-dlp/json', timeout=5
        ) as r:
            data = json.loads(r.read())
        latest  = data['info']['version']
        current = _get_ytdlp_version()
        outdated = latest != current
        if not silent and outdated:
            console.print(
                f'  [yellow]⚡ yt-dlp update available: {current} → {latest}'
                f'  (use [bold]U[/bold] from menu to update)[/yellow]'
            )
        return current, latest, outdated
    except Exception:
        return _get_ytdlp_version(), '?', False


def do_update_ytdlp():
    console.print('\n  [cyan]Updating yt-dlp…[/cyan]')
    try:
        result = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '--upgrade', 'yt-dlp'],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            new_ver = _get_ytdlp_version()
            console.print(f'  [green]✓ yt-dlp updated to {new_ver}[/green]')
        else:
            console.print(f'  [red]Update failed:[/red] {result.stderr[:200]}')
    except Exception as e:
        console.print(f'  [red]Update error: {e}[/red]')


# ── Quality selection ──────────────────────────────────────────────────────

def select_quality(choice, info):
    if choice == 'mp3':
        options = [
            ('320', '320 kbps  MAX'), ('256', '256 kbps'),
            ('192', '192 kbps'),      ('128', '128 kbps'),
            ('64',  ' 64 kbps  MIN'),
        ]
        title = 'Audio Quality'
    else:
        formats = info.get('formats', [])
        heights = sorted(set(
            f['height'] for f in formats
            if f.get('height') and f.get('vcodec') not in (None, 'none')
        ), reverse=True)
        options  = [('max', 'MAX  (best available)')]
        options += [(str(h), f'{h}p') for h in heights]
        options += [('min', 'MIN  (lowest available)')]
        title = 'Video Resolution'

    idx = [0]

    def _make_table():
        t = Table(box=None, show_header=False, padding=(0, 2))
        t.add_column(width=2)
        t.add_column(width=26)
        for i, (val, label) in enumerate(options):
            if i == idx[0]:
                t.add_row('[bold bright_blue]▶[/]', f'[bold white]{label}[/]')
            else:
                t.add_row(' ', f'[dim]{label}[/]')
        return Panel(t, title=f'[bold]{title}[/bold]',
                     subtitle='[dim]↑↓ move   Enter select[/dim]',
                     box=box.ROUNDED, border_style='bright_blue',
                     expand=False, padding=(0, 1))

    with Live(_make_table(), console=console, refresh_per_second=15) as live:
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                ch2 = msvcrt.getwch()
                if ch2 == 'H':   idx[0] = (idx[0] - 1) % len(options)
                elif ch2 == 'P': idx[0] = (idx[0] + 1) % len(options)
                live.update(_make_table())
            elif ch in ('\r', '\n'):
                break
    console.print()
    return options[idx[0]][0]


# ── YouTube search ─────────────────────────────────────────────────────────

def youtube_search(query, max_results=8):
    """
    Search and return result entries.
    Respects the 'search_source' setting:
      'yt'   — YouTube only
      'ytm'  — YouTube Music only
      'both' — YouTube Music first, fill remainder from YouTube (default)
    """
    source = settings.get('search_source', 'both')
    opts = {**_ydl_base(), 'extract_flat': True, 'quiet': True, 'no_warnings': True}

    def _search_yt(n):
        """Standard YouTube search using ytsearch: prefix."""
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f'ytsearch{n}:{query}', download=False)
            entries = info.get('entries', []) or []
            # Ensure webpage_url is set for display/download
            for e in entries:
                if not e.get('webpage_url') and e.get('id'):
                    e['webpage_url'] = f"https://www.youtube.com/watch?v={e['id']}"
            return entries
        except Exception as e:
            if DEBUG:
                console.print(f'[dim]  Search (yt) failed: {_clean_error(e)}[/dim]')
            return []

    def _search_ytm(n):
        """YouTube Music search using music.youtube.com/search URL."""
        try:
            encoded = urllib.parse.quote_plus(query)
            url = f'https://music.youtube.com/search?q={encoded}'
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            entries = info.get('entries', []) or []
            # Keep only proper video entries (filter out albums/artists/playlists)
            entries = [e for e in entries if 'watch?v=' in (e.get('url') or '')]
            # Normalise: use 'url' as 'webpage_url' so downstream code works uniformly
            for e in entries:
                if not e.get('webpage_url'):
                    e['webpage_url'] = e.get('url') or f"https://music.youtube.com/watch?v={e['id']}"
            return entries[:n]
        except Exception as e:
            if DEBUG:
                console.print(f'[dim]  Search (ytm) failed: {_clean_error(e)}[/dim]')
            return []

    if source == 'yt':
        return _search_yt(max_results)

    if source == 'ytm':
        results = _search_ytm(max_results)
        if not results:
            console.print('[dim]  YouTube Music returned no results, falling back to YouTube…[/dim]')
            results = _search_yt(max_results)
        return results

    # 'both': fill from YTM first, top up with YT for any shortfall
    ytm = _search_ytm(max_results)
    if len(ytm) >= max_results:
        return ytm
    need = max_results - len(ytm)
    yt = _search_yt(need + 2)
    # deduplicate by video id
    seen = {e.get('id') for e in ytm if e.get('id')}
    extra = [e for e in yt if e.get('id') not in seen][:need]
    return ytm + extra


def search_and_pick(choice, quality=None):
    """Search mode: arrow-key picker with thumbnail preview."""
    query = console.input('\n[cyan]Search query[/cyan] > ').strip()
    if not query:
        return

    src_label = {'yt': 'YouTube', 'ytm': 'YouTube Music', 'both': 'YT Music + YouTube'}.get(
        settings.get('search_source', 'both'), 'YT Music + YouTube'
    )
    with console.status(f'[cyan]Searching {src_label}…[/cyan]'):
        results = youtube_search(query)

    if not results:
        console.print('[yellow]  No results found.[/yellow]')
        return

    entry = _arrow_pick(results)
    if not entry:
        console.print('[dim]  Cancelled.[/dim]')
        return

    url = entry.get('webpage_url') or f"https://www.youtube.com/watch?v={entry['id']}"
    download_single(url, choice, quality=quality)


# ── Core download ──────────────────────────────────────────────────────────

def run_download(url, choice, quality, save_path, p, remove_when_done=False,
                 lock=None, show_title=True):
    """
    Download one URL. Uses a single task in Progress `p`.

    Returns (status, result, elapsed):
      True  / title / seconds  — success
      None  / reason / 0.0     — skipped (too_long / exists)
      False / error_msg / 0.0  — failed

    Re-raises KeyboardInterrupt for caller to handle.
    `lock` is an optional threading.Lock for thread-safe console output.
    """
    task_ids = []
    debug_logger = _DebugLogger() if DEBUG else None

    def _add(**kwargs):
        tid = p.add_task(**kwargs)
        task_ids.append(tid)
        return tid

    def _cleanup():
        if remove_when_done:
            for tid in task_ids:
                try:
                    p.remove_task(tid)
                except Exception:
                    pass

    def _print_debug():
        if debug_logger and debug_logger.messages:
            lines = []
            for lvl, msg in debug_logger.messages[-50:]:
                lines.append(f'[dim]{lvl}: {msg}[/dim]')
            if lines:
                console.print(Panel(
                    '\n'.join(lines),
                    title='[dim]Debug Log[/dim]',
                    box=box.SIMPLE,
                    style='dim',
                ))

    # ── Fetch info ────────────────────────────────────────────
    t_dl = _add(description='', total=None, info='[cyan]Fetching…[/cyan]')

    try:
        with yt_dlp.YoutubeDL(_ydl_base(logger=debug_logger)) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        msg = _clean_error(e)
        p.update(t_dl, total=1, completed=1, info=f'[red]Failed: {msg[:50]}[/red]')
        _cleanup()
        _print_debug()
        return False, msg, 0.0

    title    = info.get('title', 'Unknown')
    duration = info.get('duration', 0) or 0

    # Show title above the progress bar
    if show_title:
        title_line = f'  ♫ [bold]{title}[/bold]  [dim]{_fmt_duration(duration)}[/dim]'
        if lock:
            with lock:
                console.print(title_line)
        else:
            console.print(title_line)

    if duration > 3600:
        p.update(t_dl, total=1, completed=1, info='[yellow]Skipped (>1hr)[/yellow]')
        _cleanup()
        return None, 'too_long', 0.0

    # ── Already exists? ────────────────────────────────────────
    ext = 'mp3' if choice == 'mp3' else 'mp4'
    out_path = os.path.join(save_path, f'{title}.{ext}')
    if os.path.exists(out_path) and (ext != 'mp3' or _has_cover_art(out_path)):
        p.update(t_dl, total=1, completed=1, info='[dim]Already exists[/dim]')
        _cleanup()
        return None, 'exists', 0.0

    # ── Set up download ───────────────────────────────────────
    p.update(t_dl, total=None, completed=0, info='[cyan]Starting…[/cyan]')

    accumulated = [0]
    last_fname = [None]

    def prog_hook(d):
        fname = d.get('tmpfilename') or d.get('filename', '')
        if fname and fname != last_fname[0]:
            last_fname[0] = fname

        if pause_event.is_set():
            p.update(t_dl, info='[yellow]Paused — P to resume[/yellow]')
            while pause_event.is_set():
                time.sleep(0.2)
            p.update(t_dl, info='[cyan]Resuming…[/cyan]')
            return

        if d['status'] == 'downloading':
            dl = d.get('downloaded_bytes', 0)
            tot = d.get('total_bytes') or d.get('total_bytes_estimate')
            eff_completed = accumulated[0] + dl
            eff_total = (accumulated[0] + tot) if tot else None

            size_str = _fmt_bytes(eff_completed)
            if eff_total:
                size_str += f' / {_fmt_bytes(eff_total)}'

            eta_str = _fmt_eta(d.get('eta'))
            p.update(t_dl, completed=eff_completed, total=eff_total,
                     info=f'[dim]{size_str}[/dim]  ETA [bold]{eta_str}[/bold]')
        elif d['status'] == 'finished':
            size = d.get('total_bytes') or d.get('downloaded_bytes') or 0
            accumulated[0] += size
            p.update(t_dl, completed=accumulated[0], total=accumulated[0],
                     info=f'[dim]{_fmt_bytes(accumulated[0])}[/dim]')

    def pp_hook(d):
        if d['status'] == 'started':
            p.update(t_dl, total=None, completed=0, info='[cyan]Processing…[/cyan]')
        elif d['status'] == 'finished':
            pass

    opts = {
        **_ydl_base(logger=debug_logger),
        'format':              build_format(choice, quality),
        'outtmpl':             os.path.join(save_path, '%(title)s.%(ext)s'),
        'writethumbnail':      True,
        'progress_hooks':      [prog_hook],
        'postprocessor_hooks': [pp_hook],
        'postprocessors':      _postprocessors(choice, quality),
    }
    if choice == 'mp4':
        opts['merge_output_format'] = 'mp4'

    pause_event.clear()
    stop_listener = threading.Event()
    listener = threading.Thread(target=_key_listener, args=(stop_listener,), daemon=True)
    listener.start()

    MAX_RETRIES = 3
    RETRY_DELAYS = [2, 4, 8]

    def _is_retryable(err):
        msg = str(err).lower()
        fatal_keywords = ('private video', 'video unavailable', 'has been removed',
                          'age-restricted', 'members only', 'not available',
                          'copyright', 'format is not available')
        return not any(k in msg for k in fatal_keywords)

    start = time.time()
    last_err = None
    try:
        for attempt in range(MAX_RETRIES + 1):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
                last_err = None
                break  # success
            except KeyboardInterrupt:
                raise
            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES and _is_retryable(e):
                    delay = RETRY_DELAYS[attempt]
                    p.update(t_dl, total=None, completed=0,
                             info=f'[yellow]Retry {attempt+1}/{MAX_RETRIES} in {delay}s…[/yellow]')
                    time.sleep(delay)
                    accumulated[0] = 0  # reset byte counter for retry
                else:
                    break

        if last_err is not None:
            raise last_err
        elapsed = time.time() - start
        p.update(t_dl, total=1, completed=1, info='[green]Done ✓[/green]')
        _cleanup()
        _print_debug()
        if choice == 'mp3':
            mp3_path = os.path.join(save_path, f'{title}.mp3')
            if os.path.exists(mp3_path):
                _musicbrainz_tag(mp3_path, title, duration)
        _save_history_entry(title, url, choice, quality, info.get('duration', 0) or 0)
        return True, title, elapsed

    except KeyboardInterrupt:
        p.update(t_dl, total=1, completed=1, info='[yellow]Stopped[/yellow]')
        _cleanup()
        _print_debug()
        raise

    except Exception as e:
        msg = _clean_error(e)
        p.update(t_dl, total=1, completed=1, info=f'[red]Failed: {msg[:50]}[/red]')
        _cleanup()
        _print_debug()
        return False, msg, 0.0

    finally:
        stop_listener.set()
        pause_event.clear()


# ── Alternative download (search fallback) ─────────────────────────────────

def alt_download(title, choice, quality, save_path, p):
    """Search for `title` on YouTube and download the first result."""
    results = youtube_search(title, max_results=3)
    for r in results:
        if not r.get('id'):
            continue
        alt_url = (r.get('webpage_url')
                   or f"https://www.youtube.com/watch?v={r['id']}")
        if DEBUG:
            console.print(f'  [dim]Alt → {r.get("title", alt_url)}[/dim]')
        status, result, elapsed = run_download(
            alt_url, choice, quality, save_path, p, remove_when_done=True
        )
        if status is True:
            return status, result, elapsed
    return False, 'No alternative found', 0.0


# ── Single video ───────────────────────────────────────────────────────────

def download_single(url, choice, quality=None):
    save_path = _save_path(choice)

    if quality is None:
        with console.status('[cyan]Fetching info…[/cyan]'):
            try:
                with yt_dlp.YoutubeDL(_ydl_base()) as ydl:
                    pre_info = ydl.extract_info(url, download=False)
            except Exception as e:
                msg = _clean_error(e)
                console.print(f'[red]  Could not fetch video info:[/red] {msg}')
                if _is_auth_error(e):
                    console.print(
                        '[yellow]  ↳ This video may need authentication.'
                        ' Press [bold]C[/bold] at the main menu to set a cookies file.[/yellow]'
                    )
                if DEBUG:
                    console.print_exception()
                return False

        title    = pre_info.get('title', 'Unknown')
        duration = pre_info.get('duration', 0) or 0
        console.print(f'\n  [bold]{title}[/bold]  [dim]{_fmt_duration(duration)}[/dim]')

        if duration > 3600:
            console.print(
                f'  [yellow]⚠ This video is {_fmt_duration(duration)} long (over 1 hour) — skipped.[/yellow]'
            )
            return None

        quality = select_quality(choice, pre_info)
        console.print()

    console.print('[dim]  P = pause/resume   Ctrl+C = stop[/dim]\n')

    with _make_progress() as p:
        try:
            status, result, elapsed = run_download(url, choice, quality, save_path, p)
        except KeyboardInterrupt:
            console.print('\n[yellow]  Stopped.[/yellow]')
            return False

    finished_at = datetime.datetime.now().strftime('%H:%M:%S')

    if status is True:
        console.print(f'\n  [green]✓ Done in {elapsed:.1f}s  (at {finished_at})[/green]')
        console.print(f'  [dim]Saved to: {save_path}[/dim]')
        _notify('Download Complete', f'✓ {result}')
    elif status is None:
        if result == 'too_long':
            console.print(f'\n  [yellow]⏭ Skipped — over 1 hour.[/yellow]')
        else:
            console.print(f'\n  [dim]= Already exists.[/dim]')
    else:
        console.print(f'\n  [red]✗ Failed: {result}[/red]')

    return status


# ── Playlist / channel ─────────────────────────────────────────────────────

def _worker(args):
    """Used by concurrent mode — calls run_download in a thread."""
    url, choice, quality, save_path, p, title, idx, total = args
    try:
        status, result, elapsed = run_download(
            url, choice, quality, save_path, p, remove_when_done=True
        )
        return idx, title, status, result, elapsed
    except KeyboardInterrupt:
        return idx, title, False, 'Interrupted', 0.0
    except Exception as e:
        return idx, title, False, _clean_error(e), 0.0


def download_playlist(url, choice, quality=None):
    global stop_flag
    stop_flag = False

    save_path = _save_path(choice)
    state_path = os.path.join(save_path, '.playlist_state.json')

    # ── Check for resume state (Feature 5) ─────────────────────
    resume_ids = set()
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
            if state.get('url') == url:
                done_count = len(state.get('completed_ids', []))
                state_total = state.get('total', '?')
                console.print(
                    f"\n  [yellow]Resume playlist '{state.get('title', '?')}' "
                    f"({done_count}/{state_total})?[/yellow] "
                    f"[green]\\[Y][/green]/[red]\\[N][/red] > ",
                    end='',
                )
                try:
                    ans = console.input('').strip().upper()
                except (KeyboardInterrupt, EOFError):
                    ans = 'N'
                if ans == 'Y':
                    resume_ids = set(state.get('completed_ids', []))
                    choice = state.get('choice', choice)
                    quality = state.get('quality', quality)
                    save_path = _save_path(choice)
                    state_path = os.path.join(save_path, '.playlist_state.json')
        except Exception:
            pass

    console.print()
    with console.status('[cyan]Fetching playlist/channel info…[/cyan]'):
        try:
            with yt_dlp.YoutubeDL({**_ydl_base(), 'extract_flat': True}) as ydl:
                playlist_info = ydl.extract_info(url, download=False)
        except Exception as e:
            console.print(f'[red]  Could not fetch: {_clean_error(e)}[/red]')
            if DEBUG:
                console.print_exception()
            return

    entries        = playlist_info.get('entries') or []
    total          = len(entries)
    playlist_title = playlist_info.get('title', 'Playlist')

    console.print(Panel(
        f'[bold]{playlist_title}[/bold]',
        subtitle=f'[dim]{total} item(s)[/dim]',
        expand=False, box=box.ROUNDED,
    ))

    if total == 0:
        console.print('[yellow]  Empty or unavailable.[/yellow]')
        return

    # Filter out already-completed entries
    if resume_ids:
        original_total = total
        entries = [e for e in entries if e.get('id') not in resume_ids]
        console.print(
            f'  [dim]Resuming: {original_total - len(entries)} already done, '
            f'{len(entries)} remaining[/dim]'
        )

    # Quality selection (skip if provided via defaults or resume)
    if quality is None:
        if choice == 'mp4':
            first_info = {}
            for entry in entries[:3]:
                try:
                    first_url = (entry.get('webpage_url')
                                 or f"https://www.youtube.com/watch?v={entry['id']}")
                    with console.status('[cyan]Fetching first video for quality list…[/cyan]'):
                        with yt_dlp.YoutubeDL(_ydl_base()) as ydl:
                            first_info = ydl.extract_info(first_url, download=False)
                    break
                except Exception:
                    continue
            quality = select_quality(choice, first_info)
        else:
            quality = select_quality(choice, {})
    else:
        console.print(f'  [dim]Using quality: {quality}[/dim]')

    # Concurrent threads setting
    workers = settings.get('concurrent_downloads', 1)
    if workers > 1:
        console.print(f'  [dim]Concurrent downloads: {workers}[/dim]')

    total_start  = time.time()
    completed    = 0
    failed_list  = []   # (title, error)
    long_videos  = []   # (title, duration)
    retry_list   = []   # (url, title) for failed entries
    elapsed_times = []
    completed_ids = set(resume_ids)

    def _save_state():
        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'url': url,
                    'title': playlist_title,
                    'choice': choice,
                    'quality': quality,
                    'completed_ids': list(completed_ids),
                    'total': total,
                }, f)
        except Exception:
            pass

    _save_state()

    console.print('\n[dim]  P = pause/resume   Ctrl+C = skip/stop[/dim]')

    with _make_progress() as p:
        t_overall = p.add_task(
            '',
            total=len(entries), info=f'[cyan]Playlist 0 / {len(entries)}[/cyan]',
        )

        if workers > 1:
            # ── Concurrent mode ────────────────────────────────
            batch_args = []
            for entry in entries:
                video_url = (entry.get('webpage_url')
                             or f"https://www.youtube.com/watch?v={entry['id']}")
                batch_args.append((
                    video_url, choice, quality, save_path, p,
                    entry.get('title', 'Unknown'),
                    entries.index(entry) + 1, len(entries),
                ))

            done_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_worker, a): (a, entries[idx]) for idx, a in enumerate(batch_args)}
                for fut in concurrent.futures.as_completed(futures):
                    arg_tuple, entry = futures[fut]
                    idx, title, status, result, elapsed = fut.result()
                    done_count += 1
                    remaining = len(entries) - done_count
                    p.update(t_overall, completed=done_count,
                             info=f'[cyan]Playlist {done_count} / {len(entries)}[/cyan]')

                    if status is True:
                        completed += 1
                        elapsed_times.append(elapsed)
                        avg  = sum(elapsed_times) / len(elapsed_times)
                        eta  = _fmt_eta(avg * remaining / workers) if remaining else '—'
                        console.print(
                            f'  [green]✓[/green] [{idx}/{len(entries)}] {title[:50]}'
                            f'  [dim]{elapsed:.1f}s · ETA ~{eta}[/dim]'
                        )
                        if entry.get('id'):
                            completed_ids.add(entry['id'])
                            _save_state()
                    elif status is None and result == 'too_long':
                        long_videos.append((title, 0))
                        console.print(f'  [yellow]⏭[/yellow] [{idx}/{len(entries)}] {title[:50]}  [yellow]>1hr[/yellow]')
                    elif status is None:
                        completed += 1
                        console.print(f'  [dim]=[/dim] [{idx}/{len(entries)}] {title[:50]}  already exists')
                        if entry.get('id'):
                            completed_ids.add(entry['id'])
                            _save_state()
                    else:
                        retry_list.append((arg_tuple[0], title))
                        console.print(f'  [red]✗[/red] [{idx}/{len(entries)}] {title[:50]}  [red]{result[:50]}[/red]')

        else:
            # ── Sequential mode ────────────────────────────────
            for i, entry in enumerate(entries, 1):
                if stop_flag:
                    console.print('[yellow]  Stopped.[/yellow]')
                    break

                video_url   = (entry.get('webpage_url')
                               or f"https://www.youtube.com/watch?v={entry['id']}")
                video_title = entry.get('title', 'Unknown')
                remaining   = len(entries) - i

                console.print(
                    f'\n[cyan]\\[{i}/{len(entries)}][/cyan] [bold]{video_title}[/bold]',
                    highlight=False,
                )
                finished_at = datetime.datetime.now().strftime('%H:%M:%S')

                try:
                    status, result, elapsed = run_download(
                        video_url, choice, quality, save_path, p,
                        remove_when_done=True,
                    )
                except KeyboardInterrupt:
                    console.print('\n[yellow]  Interrupted![/yellow]')
                    try:
                        ans = console.input(
                            '  [S]kip this video and continue, or [Q]uit playlist? > '
                        ).strip().upper()
                    except (KeyboardInterrupt, EOFError):
                        ans = 'Q'
                    if ans == 'S':
                        retry_list.append((video_url, video_title))
                        p.update(t_overall, completed=i,
                                 info=f'[cyan]Playlist {i} / {len(entries)}[/cyan]')
                        continue
                    else:
                        stop_flag = True
                        break

                p.update(t_overall, completed=i,
                         info=f'[cyan]Playlist {i} / {len(entries)}[/cyan]')

                if status is True:
                    elapsed_times.append(elapsed)
                avg_time = sum(elapsed_times) / len(elapsed_times) if elapsed_times else 0
                eta_str  = _fmt_eta(avg_time * remaining) if remaining > 0 and avg_time > 0 else '—'

                if status is True:
                    completed += 1
                    console.print(
                        f'  [green]✓[/green] [{i}/{len(entries)}] {video_title[:50]}'
                        f'  [dim]{elapsed:.1f}s · at {finished_at}'
                        + (f' · ETA ~{eta_str}' if remaining else '') + '[/dim]'
                    )
                    if entry.get('id'):
                        completed_ids.add(entry['id'])
                        _save_state()
                elif status is None and result == 'too_long':
                    long_videos.append((video_title, entry.get('duration', 0) or 0))
                    console.print(
                        f'  [yellow]⏭[/yellow] [{i}/{len(entries)}] {video_title[:50]}'
                        f'  [yellow]skipped (>1hr)[/yellow]'
                    )
                elif status is None:
                    completed += 1
                    console.print(
                        f'  [dim]=[/dim] [{i}/{len(entries)}] {video_title[:50]}  [dim]exists[/dim]'
                    )
                    if entry.get('id'):
                        completed_ids.add(entry['id'])
                        _save_state()
                else:
                    retry_list.append((video_url, video_title))
                    failed_list.append((video_title, result))
                    console.print(
                        f'  [red]✗[/red] [{i}/{len(entries)}] {video_title[:50]}'
                        f'  [red]{result[:55]}[/red]'
                    )

    total_elapsed = round(time.time() - total_start, 1)
    console.print(Rule(style='dim'))
    summary_rows = [
        ('[green]✓ Downloaded[/]', str(completed)),
        ('[yellow]⏭ Skipped >1hr[/]', str(len(long_videos))),
        ('[red]✗ Failed[/]',  str(len(failed_list))),
        ('[dim]⏱ Total time[/]', f'{total_elapsed}s'),
    ]
    summary_t = Table(box=None, show_header=False, padding=(0, 2))
    summary_t.add_column(width=18)
    summary_t.add_column(width=8)
    for label, val in summary_rows:
        if val != '0':
            summary_t.add_row(label, f'[bold white]{val}[/]')
    console.print(Panel(summary_t, title='[bold]Playlist Complete[/bold]',
        subtitle=f'[dim]{playlist_title[:50]}[/dim]',
        box=box.ROUNDED, border_style='green', expand=False, padding=(0, 2)))
    console.print(f'[dim]Saved to: {save_path}[/dim]')
    _notify('Playlist Complete', f'{completed}/{total} downloaded — {playlist_title[:40]}')

    if long_videos:
        console.print(f'\n[yellow]⏭ Skipped {len(long_videos)} video(s) over 1 hour:[/yellow]')
        for t, dur in long_videos:
            console.print(f'  • {t}' + (f'  [dim]({_fmt_duration(dur)})[/dim]' if dur else ''))

    if failed_list:
        console.print(f'\n[red]✗ {len(failed_list)} failed:[/red]')
        for t, err in failed_list:
            console.print(f'  • {t}  [dim]{err[:70]}[/dim]')

    # ── Retry failed? ──────────────────────────────────────────
    if retry_list:
        console.print(
            f'\n[yellow]{len(retry_list)} item(s) failed.[/yellow]'
            '  Retry now? [green][Y][/green]/[red][N][/red] > ',
            end='',
        )
        try:
            ans = console.input('').strip().upper()
        except (KeyboardInterrupt, EOFError):
            ans = 'N'

        if ans == 'Y':
            console.print()
            with _make_progress() as p2:
                for r_url, r_title in retry_list:
                    console.print(f'  [cyan]↺[/cyan] [bold]{r_title}[/bold]')
                    try:
                        status, result, elapsed = run_download(
                            r_url, choice, quality, save_path, p2,
                            remove_when_done=True,
                        )
                    except KeyboardInterrupt:
                        break
                    if status is True:
                        completed += 1
                        console.print(f'  [green]✓ Done in {elapsed:.1f}s[/green]')
                    else:
                        console.print(f'  [red]✗ Still failed: {result[:60]}[/red]')

    # All done — remove state file
    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except Exception:
        pass


# ── Settings menu ──────────────────────────────────────────────────────────

def manage_settings():
    while True:
        console.print()
        console.print(Rule('[bold]Settings[/bold]', style='cyan'))

        cf      = settings.get('cookies_file', '')
        out_dir = settings.get('output_folder', '')
        workers = settings.get('concurrent_downloads', 1)
        df      = settings.get('default_format', '')
        dq      = settings.get('default_quality', '')
        src     = settings.get('search_source', 'both')
        src_labels = {'yt': 'YouTube only', 'ytm': 'YouTube Music only', 'both': 'YT Music first, then YT'}
        src_label = src_labels.get(src, src_labels['both'])

        console.print(f'  [green][1][/green] Cookies file     : '
                      + (f'[cyan]{cf}[/cyan]' if cf else '[dim](none)[/dim]'))
        console.print(f'  [green][2][/green] Output folder    : '
                      + (f'[cyan]{out_dir}[/cyan]' if out_dir else '[dim](default: audio / video)[/dim]'))
        console.print(f'  [green][3][/green] Concurrent DLs   : [cyan]{workers}[/cyan]  [dim](1 = sequential)[/dim]')
        default_label = f'[cyan]{df.upper()} {dq}[/cyan]' if df else '[dim](always ask)[/dim]'
        console.print(f'  [green][4][/green] Default format   : {default_label}')
        notifs   = settings.get('notifications', True)
        auto_tag = settings.get('auto_tag', True)
        console.print(f'  [green][5][/green] Search source    : [cyan]{src_label}[/cyan]')
        console.print(f'  [green][6][/green] Notifications    : [cyan]{"ON" if notifs else "OFF"}[/cyan]')
        console.print(f'  [green][7][/green] Auto-tag MP3     : [cyan]{"ON" if auto_tag else "OFF"}[/cyan]')
        console.print(f'  [dim][Enter] Back[/dim]')

        sel = console.input('\n  > ').strip()

        if sel == '1':
            _set_cookies()
        elif sel == '2':
            _set_output_folder()
        elif sel == '3':
            _set_concurrent()
        elif sel == '4':
            _set_default_format()
        elif sel == '5':
            _set_search_source()
        elif sel == '6':
            _set_notifications()
        elif sel == '7':
            _set_auto_tag()
        else:
            break


def _set_cookies():
    cur = settings.get('cookies_file', '')
    if cur:
        exists = os.path.isfile(cur)
        status = '[green]active[/green]' if exists else '[red]file not found[/red]'
        console.print(f'\n  Current: [cyan]{cur}[/cyan]  ({status})')
        action = console.input('  [1] Change  [2] Clear  [Enter] Cancel > ').strip()
        if action == '2':
            settings.pop('cookies_file', None)
            save_settings()
            console.print('  [dim]Cookies cleared.[/dim]')
            return
        elif action != '1':
            return
    path = console.input('\n  Path to cookies.txt (Enter to cancel) > ').strip()
    if not path:
        return
    if os.path.isfile(path):
        settings['cookies_file'] = path
        save_settings()
        console.print(f'  [green]✓ Cookies set:[/green] {path}')
    else:
        console.print(f'  [red]  File not found: {path}[/red]')


def _set_output_folder():
    cur = settings.get('output_folder', '')
    console.print(f'\n  Current: [cyan]{cur if cur else "(default)"}[/cyan]')
    path = console.input('  New folder path (Enter to keep default) > ').strip()
    if not path:
        settings.pop('output_folder', None)
        save_settings()
        console.print('  [dim]Reset to default.[/dim]')
        return
    os.makedirs(path, exist_ok=True)
    settings['output_folder'] = path
    save_settings()
    console.print(f'  [green]✓ Output folder set:[/green] {path}')


def _set_concurrent():
    cur = settings.get('concurrent_downloads', 1)
    console.print(f'\n  Current: [cyan]{cur}[/cyan]')
    console.print('  [dim]Recommended: 1-3. Higher values may trigger YouTube rate limiting.[/dim]')
    val = console.input('  Number of concurrent downloads (1-5) > ').strip()
    if val.isdigit() and 1 <= int(val) <= 5:
        settings['concurrent_downloads'] = int(val)
        save_settings()
        console.print(f'  [green]✓ Set to {val}[/green]')
    else:
        console.print('[red]  Invalid, must be 1-5.[/red]')


def _set_default_format():
    cur_fmt = settings.get('default_format', '')
    cur_q   = settings.get('default_quality', '')
    if cur_fmt:
        console.print(f'\n  Current: [cyan]{cur_fmt.upper()}[/cyan]'
                      + (f' [cyan]{cur_q}[/cyan]' if cur_q else ''))
    else:
        console.print('\n  Current: [dim](always ask)[/dim]')

    console.print('  [green][1][/green] MP3')
    console.print('  [green][2][/green] MP4')
    console.print('  [green][3][/green] Always ask (clear defaults)')

    sel = console.input('\n  > ').strip()

    if sel == '1':
        console.print('\n  [cyan]Audio Quality:[/cyan]')
        q_opts = [
            ('320', '320 kbps  (MAX)'), ('256', '256 kbps'),
            ('192', '192 kbps'),        ('128', '128 kbps'),
            ('64',  ' 64 kbps  (MIN)'),
        ]
        for i, (_, label) in enumerate(q_opts, 1):
            console.print(f'  [green]\\[{i}][/green] {label}')
        q_sel = console.input(f'\n  > Pick quality (1-{len(q_opts)}): ').strip()
        if q_sel.isdigit() and 1 <= int(q_sel) <= len(q_opts):
            settings['default_format'] = 'mp3'
            settings['default_quality'] = q_opts[int(q_sel) - 1][0]
            save_settings()
            console.print(f'  [green]✓ Default: MP3 {settings["default_quality"]}kbps[/green]')
        else:
            console.print('[red]  Invalid choice.[/red]')
    elif sel == '2':
        console.print('\n  [cyan]Video Quality:[/cyan]')
        q_opts = [
            ('max', 'MAX  (best available)'), ('1080', '1080p'),
            ('720', '720p'),                  ('480', '480p'),
            ('min', 'MIN  (lowest available)'),
        ]
        for i, (_, label) in enumerate(q_opts, 1):
            console.print(f'  [green]\\[{i}][/green] {label}')
        q_sel = console.input(f'\n  > Pick quality (1-{len(q_opts)}): ').strip()
        if q_sel.isdigit() and 1 <= int(q_sel) <= len(q_opts):
            settings['default_format'] = 'mp4'
            settings['default_quality'] = q_opts[int(q_sel) - 1][0]
            save_settings()
            console.print(f'  [green]✓ Default: MP4 {settings["default_quality"]}[/green]')
        else:
            console.print('[red]  Invalid choice.[/red]')
    elif sel == '3':
        settings.pop('default_format', None)
        settings.pop('default_quality', None)
        save_settings()
        console.print('  [dim]Defaults cleared.[/dim]')


def _set_search_source():
    cur = settings.get('search_source', 'both')
    labels = {
        'both': 'YT Music first, then YT (default)',
        'ytm':  'YouTube Music only',
        'yt':   'YouTube only',
    }
    console.print(f'\n  Current: [cyan]{labels.get(cur, cur)}[/cyan]')
    console.print('  [green][1][/green] YT Music first, then YouTube  [dim](recommended)[/dim]')
    console.print('  [green][2][/green] YouTube Music only')
    console.print('  [green][3][/green] YouTube only')

    sel = console.input('\n  > ').strip()
    mapping = {'1': 'both', '2': 'ytm', '3': 'yt'}
    if sel in mapping:
        settings['search_source'] = mapping[sel]
        save_settings()
        console.print(f'  [green]✓ Search source set to: {labels[mapping[sel]]}[/green]')
    else:
        console.print('[dim]  No change.[/dim]')


def _set_notifications():
    cur = settings.get('notifications', True)
    console.print(f'\n  Current: [cyan]{"ON" if cur else "OFF"}[/cyan]')
    console.print('  [green][1][/green] ON')
    console.print('  [green][2][/green] OFF')
    sel = console.input('\n  > ').strip()
    if sel == '1':
        settings['notifications'] = True
        save_settings()
        console.print('  [green]✓ Notifications ON[/green]')
    elif sel == '2':
        settings['notifications'] = False
        save_settings()
        console.print('  [dim]Notifications OFF[/dim]')
    else:
        console.print('[dim]  No change.[/dim]')


def _set_auto_tag():
    cur = settings.get('auto_tag', True)
    console.print(f'\n  Current: [cyan]{"ON" if cur else "OFF"}[/cyan]')
    console.print('  [green][1][/green] ON  [dim](query MusicBrainz after MP3 download)[/dim]')
    console.print('  [green][2][/green] OFF')
    sel = console.input('\n  > ').strip()
    if sel == '1':
        settings['auto_tag'] = True
        save_settings()
        console.print('  [green]✓ Auto-tag MP3 ON[/green]')
    elif sel == '2':
        settings['auto_tag'] = False
        save_settings()
        console.print('  [dim]Auto-tag MP3 OFF[/dim]')
    else:
        console.print('[dim]  No change.[/dim]')


# ── History viewer ─────────────────────────────────────────────────────────

def show_history():
    history = _load_history()
    if not history:
        console.print('\n  [dim]No downloads yet.[/dim]')
        return

    idx = [0]
    n = len(history)

    def _make_display():
        sel = idx[0]
        entry = history[sel]

        t = Table(box=None, show_header=False, padding=(0, 1), min_width=56)
        t.add_column(width=2)
        t.add_column(width=50)
        t.add_column(width=12, justify='right')

        for i, h in enumerate(history[:20]):
            is_sel = (i == sel)
            fmt_badge = f"[{'green' if h.get('format') == 'mp3' else 'blue'}]{(h.get('format') or '?').upper()}[/]"
            title_text = (h.get('title') or 'Unknown')[:48]
            date_text  = h.get('date', '')

            if is_sel:
                arrow = '[bold bright_blue]▶[/]'
                style = 'bold white'
                meta  = 'bright_blue'
            else:
                arrow = ' '
                style = 'dim white'
                meta  = 'dim'

            cell = Text()
            cell.append(title_text, style=style)
            cell.append(f'\n  {date_text}  {h.get("quality","")}', style=meta)
            t.add_row(Text(arrow), cell, Text(fmt_badge))

        h = entry
        detail = Table(box=None, show_header=False, padding=(0, 1))
        detail.add_column(width=10, style='dim')
        detail.add_column(width=40)
        detail.add_row('Title',    h.get('title', '')[:40])
        detail.add_row('Format',   f"{(h.get('format') or '').upper()} {h.get('quality', '')}")
        detail.add_row('Date',     h.get('date', ''))
        detail.add_row('Duration', _fmt_duration(h.get('duration', 0)))

        list_panel = Panel(t, title=f'[bold]History[/bold]  [dim]{sel+1}/{n}[/dim]',
            subtitle='[dim]↑↓ navigate   Enter re-download   D delete   Esc back[/dim]',
            box=box.ROUNDED, border_style='bright_blue', padding=(0, 1))
        detail_panel = Panel(detail, title='[dim]Details[/dim]',
            box=box.ROUNDED, border_style='dim', padding=(0, 1), width=46)

        return Columns([list_panel, detail_panel], padding=(0, 1))

    action = [None]
    with Live(_make_display(), console=console, refresh_per_second=15,
              vertical_overflow='visible') as live:
        while True:
            if not msvcrt.kbhit():
                time.sleep(0.03)
                continue
            ch = msvcrt.getwch()
            if ch in ('\x00', '\xe0'):
                ch2 = msvcrt.getwch()
                if ch2 == 'H':   idx[0] = max(0, idx[0] - 1)
                elif ch2 == 'P': idx[0] = min(min(n, 20) - 1, idx[0] + 1)
                live.update(_make_display())
            elif ch in ('\r', '\n'):
                action[0] = 'download'
                break
            elif ch.lower() == 'd':
                action[0] = 'delete'
                break
            elif ch in ('\x1b', '\x03'):
                break

    if action[0] == 'download':
        entry = history[idx[0]]
        url = entry.get('url', '')
        fmt = entry.get('format', 'mp3')
        quality = entry.get('quality')
        if url:
            console.print(f'\n  Re-downloading: [bold]{entry.get("title","")}[/bold]')
            download_single(url, fmt, quality=quality)
    elif action[0] == 'delete':
        entry = history.pop(idx[0])
        try:
            with open(_history_path(), 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        console.print(f'  [dim]Removed: {entry.get("title", "")}[/dim]')


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    global DEBUG

    load_settings()

    # Background startup version check
    threading.Thread(target=check_ytdlp_update, kwargs={'silent': False},
                     daemon=True).start()

    while True:
        try:
            # Build status footer
            status_parts = []
            df = settings.get('default_format')
            dq = settings.get('default_quality')
            if df:
                status_parts.append(f"📁 {df.upper()}" + (f" {dq}" if dq else ""))
            cf = settings.get('cookies_file', '')
            if cf and os.path.isfile(cf):
                status_parts.append("🍪 Cookies")
            w = settings.get('concurrent_downloads', 1)
            if w > 1:
                status_parts.append(f"⚡ {w}x")
            if DEBUG:
                status_parts.append("🐛 Debug")

            footer = " │ ".join(status_parts) if status_parts else ""

            # Build menu
            menu = Table(show_header=False, box=None, padding=(0, 3, 0, 0))
            menu.add_column(min_width=28)
            menu.add_column(min_width=22)
            menu.add_row(
                '[bold green]V[/] ▸ Download video/audio',
                '[bold cyan]O[/] ▸ Options',
            )
            menu.add_row(
                '[bold green]P[/] ▸ Download playlist',
                '[bold yellow]U[/] ▸ Update yt-dlp',
            )
            menu.add_row(
                '[bold green]S[/] ▸ Search YouTube',
                '[bold yellow]D[/] ▸ Toggle debug',
            )
            menu.add_row(
                '[bold green]H[/] ▸ History',
                '[bold green]F[/] ▸ Import from file',
            )

            console.print()
            console.print(Panel(
                menu,
                title=f'[bold white]🎵 yt-dlp Downloader v{VERSION}[/bold white]',
                subtitle=f'[dim]{footer}[/dim]' if footer else None,
                box=box.HEAVY,
                expand=False,
                padding=(1, 2),
                border_style='bright_blue',
            ))
            mode = console.input('  [bold]▸[/bold] ').strip().upper()

            if mode == 'D':
                DEBUG = not DEBUG
                console.print(f'  Debug: [yellow]ON[/yellow]' if DEBUG else '  Debug: [dim]off[/dim]')
                continue

            if mode == 'U':
                do_update_ytdlp()
                continue

            if mode == 'O':
                manage_settings()
                continue

            if mode in ('V', 'VIDEO'):
                df = settings.get('default_format')
                dq = settings.get('default_quality')
                if df:
                    choice = df
                    console.print(f'  [dim]Using default: {df.upper()}'
                                  + (f' {dq}' if dq else '') + '[/dim]')
                else:
                    fmt    = console.input('Format — [green](1)[/green] MP4  [green](2)[/green] MP3 > ').strip()
                    choice = 'mp3' if fmt in ('2', 'mp3', 'MP3') else 'mp4'
                    dq = None
                console.print('Enter URLs (one per line, blank line to start):')
                urls = []
                while True:
                    line = console.input('  ').strip()
                    if not line:
                        break
                    urls.append(line)
                if not urls:
                    continue
                if len(urls) == 1:
                    download_single(urls[0], choice, quality=dq)
                else:
                    console.print(f'\n  [cyan]{len(urls)} URLs queued[/cyan]')
                    total_ok = 0
                    total_fail = 0
                    for i, url in enumerate(urls, 1):
                        console.print(f'\n[cyan]\\[{i}/{len(urls)}][/cyan]')
                        result = download_single(url, choice, quality=dq)
                        if result is True:
                            total_ok += 1
                        elif result is False:
                            total_fail += 1
                    summary = Table(box=None, show_header=False, padding=(0, 2))
                    summary.add_column(width=16)
                    summary.add_column(width=6)
                    summary.add_row('[green]✓ Downloaded[/]', f'[bold white]{total_ok}[/]')
                    if total_fail:
                        summary.add_row('[red]✗ Failed[/]', f'[bold red]{total_fail}[/]')
                    console.print(Panel(summary, title='[bold]Batch Complete[/bold]',
                        box=box.ROUNDED, border_style='green', expand=False, padding=(0, 2)))

            elif mode in ('P', 'PLAYLIST'):
                df = settings.get('default_format')
                dq = settings.get('default_quality')
                if df:
                    choice = df
                    console.print(f'  [dim]Using default: {df.upper()}'
                                  + (f' {dq}' if dq else '') + '[/dim]')
                else:
                    fmt    = console.input('Format — [green](1)[/green] MP4  [green](2)[/green] MP3 > ').strip()
                    choice = 'mp3' if fmt in ('2', 'mp3', 'MP3') else 'mp4'
                    dq = None
                url    = console.input('URL > ').strip()
                if url:
                    download_playlist(url, choice, quality=dq)

            elif mode in ('S', 'SEARCH', '/'):
                df = settings.get('default_format')
                dq = settings.get('default_quality')
                if df:
                    choice = df
                    console.print(f'  [dim]Using default: {df.upper()}'
                                  + (f' {dq}' if dq else '') + '[/dim]')
                else:
                    fmt    = console.input('Format — [green](1)[/green] MP4  [green](2)[/green] MP3 > ').strip()
                    choice = 'mp3' if fmt in ('2', 'mp3', 'MP3') else 'mp4'
                    dq = None
                search_and_pick(choice, quality=dq)

            elif mode == 'H':
                show_history()

            elif mode in ('F', 'FILE'):
                df = settings.get('default_format')
                dq = settings.get('default_quality')
                if df:
                    choice = df
                    console.print(f'  [dim]Using default: {df.upper()}' + (f' {dq}' if dq else '') + '[/dim]')
                else:
                    fmt    = console.input('Format — [green](1)[/green] MP4  [green](2)[/green] MP3 > ').strip()
                    choice = 'mp3' if fmt in ('2', 'mp3', 'MP3') else 'mp4'
                    dq = None
                fpath = console.input('Path to URL file (.txt) > ').strip().strip('"')
                if not os.path.isfile(fpath):
                    console.print(f'[red]  File not found: {fpath}[/red]')
                else:
                    with open(fpath, encoding='utf-8', errors='ignore') as f:
                        file_urls = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                    if not file_urls:
                        console.print('[yellow]  No URLs found in file.[/yellow]')
                    else:
                        console.print(f'  [cyan]{len(file_urls)} URLs loaded from file[/cyan]')
                        total_ok = total_fail = 0
                        for i, url in enumerate(file_urls, 1):
                            console.print(f'\n[cyan]\\[{i}/{len(file_urls)}][/cyan]')
                            result = download_single(url, choice, quality=dq)
                            if result is True:
                                total_ok += 1
                            elif result is False:
                                total_fail += 1
                        summary = Table(box=None, show_header=False, padding=(0, 2))
                        summary.add_column(width=16)
                        summary.add_column(width=6)
                        summary.add_row('[green]✓ Downloaded[/]', f'[bold white]{total_ok}[/]')
                        if total_fail:
                            summary.add_row('[red]✗ Failed[/]', f'[bold red]{total_fail}[/]')
                        skipped = len(file_urls) - total_ok - total_fail
                        if skipped:
                            summary.add_row('[dim]= Skipped[/]', f'[dim]{skipped}[/]')
                        console.print(Panel(summary, title='[bold]File Import Complete[/bold]',
                            box=box.ROUNDED, border_style='green', expand=False, padding=(0, 2)))

            else:
                console.print('[red]  Unknown option.[/red]')
                continue

        except KeyboardInterrupt:
            console.print('\n[yellow]  Interrupted.[/yellow]')
        except Exception as e:
            console.print(f'\n[red]  Unexpected error: {e}[/red]')
            if DEBUG:
                console.print_exception()

        try:
            console.input('\n[dim]Press Enter to continue…[/dim]')
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        console.print('\n[dim]Bye![/dim]')
