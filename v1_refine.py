from rich.console import Console
from rich.progress import (
    Progress, BarColumn, TextColumn,
    TaskProgressColumn,
)
from rich.panel import Panel
import datetime
import json
import msvcrt
import os
import sys
import threading
import time
import traceback
import yt_dlp

console     = Console()
DEBUG       = False             # toggled at runtime with 'D'
stop_flag   = False             # set True to stop playlist after current video
pause_event = threading.Event() # set = paused, clear = running
settings    = {}


# ── Settings (persisted to settings.json next to exe/script) ──────────────

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


# ── Helpers ────────────────────────────────────────────────────────────────

class _SilentLogger:
    """Redirects all yt-dlp log output so nothing bleeds to stderr."""
    def debug(self, msg):
        pass
    def info(self, msg):
        pass
    def warning(self, msg):
        if DEBUG:
            console.print(f'[dim]yt-dlp: {msg}[/dim]')
    def error(self, msg):
        if DEBUG:
            console.print(f'[dim red]yt-dlp error: {msg}[/dim red]')


def _clean_error(e):
    """Strip noisy 'ERROR: [youtube] ID: ' prefix from yt-dlp exceptions."""
    import re
    msg = str(e)
    # Remove "ERROR: [extractor] videoId: " prefix
    msg = re.sub(r'^ERROR:\s+\[[^\]]+\]\s+\S+:\s+', '', msg)
    # Remove leading "ERROR: " if still present
    msg = re.sub(r'^ERROR:\s+', '', msg)
    return msg.strip()


def _is_auth_error(e):
    """Return True if the error is likely an authentication/access issue."""
    msg = str(e).lower()
    return any(k in msg for k in (
        'format is not available', 'requested format', 'sign in',
        'age-restricted', 'private video', 'members only',
        'this video is not available', 'confirm your age',
    ))


def _ydl_base():
    """Return yt_dlp base options depending on debug mode and settings."""
    # 'node' is the correct runtime name (not 'nodejs') — yt-dlp will use Node.js
    # for n-challenge solving, which is required for web/authenticated downloads.
    # No explicit player_client — let yt-dlp pick the right one based on context
    # (it skips android when cookies are present and uses web instead).
    base = {
        'js_runtimes':      {'node': {}},
        'remote_components': {'ejs:github'},  # download EJS n-challenge solver via Node.js
        'format':            'bestaudio*+bestvideo*/best',
    }
    if not DEBUG:
        base.update({'quiet': True, 'no_warnings': True, 'noprogress': True,
                     'logger': _SilentLogger()})
    cf = settings.get('cookies_file', '')
    if cf and os.path.isfile(cf):
        base['cookiefile'] = cf
    return base


# ── Key listener (pause/resume with P key) ────────────────────────────────

def _key_listener(stop_evt):
    """Background thread during downloads: press P to toggle pause."""
    while not stop_evt.is_set():
        try:
            if msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch == 'p':
                    if pause_event.is_set():
                        pause_event.clear()
                    else:
                        pause_event.set()
        except Exception:
            pass
        time.sleep(0.05)


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


def _make_progress():
    return Progress(
        TextColumn('  {task.description:<32}'),
        TextColumn('['),
        BarColumn(bar_width=28, complete_style='bold green', finished_style='dim green',
                  pulse_style='cyan'),
        TextColumn(']'),
        TaskProgressColumn(),
        TextColumn('[dim]{task.fields[info]}[/dim]'),
        console=console,
        expand=False,
        transient=False,
    )


# ── Quality selection ──────────────────────────────────────────────────────

def select_quality(choice, info):
    if choice == 'mp3':
        options = [
            ('320', '320 kbps  (MAX)'), ('256', '256 kbps'),
            ('192', '192 kbps'),        ('128', '128 kbps'),
            ('64',  ' 64 kbps  (MIN)'),
        ]
        console.print('\n[cyan]Audio Quality:[/cyan]')
    else:
        formats = info.get('formats', [])
        heights = sorted(set(
            f['height'] for f in formats
            if f.get('height') and f.get('vcodec') not in (None, 'none')
        ), reverse=True)
        options  = [('max', 'MAX  (best available)')]
        options += [(str(h), f'{h}p') for h in heights]
        options += [('min', 'MIN  (lowest available)')]
        console.print('\n[cyan]Video Resolution:[/cyan]')

    for i, (_, label) in enumerate(options, 1):
        console.print(f'  [green]\\[{i}][/green] {label}')

    while True:
        sel = console.input(f'\n> Pick quality (1-{len(options)}): ').strip()
        if sel.isdigit() and 1 <= int(sel) <= len(options):
            return options[int(sel) - 1][0]
        console.print('[red]  Invalid choice, try again.[/red]')


# ── Core download ──────────────────────────────────────────────────────────

def run_download(url, choice, quality, save_path, p, remove_when_done=False):
    """
    Adds stage tasks to an existing live Progress `p` and runs the download.

    Returns (status, result, elapsed):
      status : True  = downloaded successfully
               None  = skipped (too long / already exists)
               False = error
      result : title on success/skip, error message on failure
      elapsed: seconds taken (0.0 if skipped/error)

    Re-raises KeyboardInterrupt so callers can offer skip/stop choice.
    All other exceptions are caught.
    """
    task_ids = []

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

    # ── Stage 1: Fetch info ────────────────────────────────────
    t_fetch = _add(description='[cyan]① Fetching info[/cyan]', total=1, info='')
    try:
        with yt_dlp.YoutubeDL(_ydl_base()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        msg = _clean_error(e)
        p.update(t_fetch, completed=1, description='[red]① Fetch failed[/red]', info=msg[:60])
        if DEBUG:
            console.print(f'\n[dim]{traceback.format_exc()}[/dim]')
        _cleanup()
        return False, msg, 0.0

    title    = info.get('title', 'Unknown')
    duration = info.get('duration', 0) or 0

    # ── Skip videos over 1 hour ────────────────────────────────
    if duration > 3600:
        p.update(t_fetch, completed=1, description='[yellow]① Skipped (>1hr)[/yellow]',
                 info=f'{_fmt_duration(duration)}')
        _cleanup()
        return None, 'too_long', 0.0

    p.update(t_fetch, completed=1, description='[green]① Info fetched[/green]',
             info=title[:35] + ('…' if len(title) > 35 else ''))

    # ── Already exists check ───────────────────────────────────
    ext = 'mp3' if choice == 'mp3' else 'mp4'
    if os.path.exists(os.path.join(save_path, f'{title}.{ext}')):
        p.update(t_fetch, description='[yellow]① Already exists[/yellow]', info='skipped')
        _cleanup()
        return None, 'exists', 0.0

    # ── Stage 2: Metadata ──────────────────────────────────────
    _add(description='[green]② Metadata ready[/green]', total=1, info='formats loaded', completed=1)

    # ── Stage 3 & 4: Download streams ─────────────────────────
    dl1_label = '③ Downloading audio' if choice == 'mp3' else '③ Downloading video'
    t_dl1  = _add(description=f'[cyan]{dl1_label}[/cyan]', total=None, info='starting…', start=False)
    t_dl2  = _add(description='[cyan]④ Downloading audio[/cyan]', total=None, info='waiting…',
                  start=False, visible=(choice == 'mp4'))

    # ── Stage 5: Processing ────────────────────────────────────
    t_proc  = _add(description='[cyan]⑤ Processing[/cyan]',  total=1, info='waiting…', start=False)

    # ── Stage 6: Cleanup ───────────────────────────────────────
    t_clean = _add(description='[cyan]⑥ Cleanup[/cyan]',     total=1, info='waiting…', start=False)

    started    = set()
    last_fname = [None]
    cur_task   = [t_dl1]
    cur_label  = [f'[cyan]{dl1_label}[/cyan]']  # tracks active label for pause restore

    def _start(tid):
        if tid not in started:
            p.start_task(tid)
            started.add(tid)

    def prog_hook(d):
        fname = d.get('tmpfilename') or d.get('filename', '')
        if fname and fname != last_fname[0]:
            if last_fname[0] is not None and choice == 'mp4':
                p.update(t_dl1, description='[green]③ Video downloaded[/green]', info='')
                cur_task[0]  = t_dl2
                cur_label[0] = '[cyan]④ Downloading audio[/cyan]'
            last_fname[0] = fname

        task = cur_task[0]
        _start(task)

        # ── Pause support ──────────────────────────────────────
        if pause_event.is_set():
            p.update(task, description='[yellow]⏸ Paused[/yellow]', info='press P to resume')
            while pause_event.is_set():
                time.sleep(0.2)
            p.update(task, description=cur_label[0], info='resuming…')
            return

        if d['status'] == 'downloading':
            dl  = d.get('downloaded_bytes', 0)
            tot = d.get('total_bytes') or d.get('total_bytes_estimate')
            p.update(task, completed=dl or 0, total=tot,
                     info=f'{_fmt_speed(d.get("speed"))} · ETA {_fmt_eta(d.get("eta"))}')
        elif d['status'] == 'finished':
            tot = d.get('total_bytes', 0)
            if task == t_dl2 or choice == 'mp3':
                label = '[green]④ Audio downloaded[/green]' if choice == 'mp4' else '[green]③ Audio downloaded[/green]'
            else:
                label = '[green]③ Video downloaded[/green]'
            cur_label[0] = label
            p.update(task, completed=tot or 100, total=tot or 100, description=label, info='')

    def pp_hook(d):
        if d['status'] == 'started':
            _start(t_proc)
            p.update(t_proc, description='[yellow]⑤ Processing (FFmpeg)[/yellow]', info='')
        elif d['status'] == 'finished':
            p.update(t_proc, completed=1, description='[green]⑤ Processed[/green]', info='done')

    opts = {
        **_ydl_base(),
        'format':              build_format(choice, quality),
        'outtmpl':             os.path.join(save_path, '%(title)s.%(ext)s'),
        'progress_hooks':      [prog_hook],
        'postprocessor_hooks': [pp_hook],
    }
    if choice == 'mp3':
        opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality,
        }]
    else:
        opts['merge_output_format'] = 'mp4'

    # Start key listener for pause support (P key)
    pause_event.clear()
    stop_listener = threading.Event()
    listener = threading.Thread(target=_key_listener, args=(stop_listener,), daemon=True)
    listener.start()

    start = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        elapsed = time.time() - start
        _start(t_clean)
        p.update(t_clean, completed=1, description='[green]⑥ Cleanup[/green]', info='done')
        _cleanup()
        return True, title, elapsed

    except KeyboardInterrupt:
        p.update(t_dl1, description='[yellow]Interrupted[/yellow]', info='')
        _cleanup()
        raise   # caller handles skip vs stop

    except Exception as e:
        msg = _clean_error(e)
        p.update(t_dl1, description='[red]Download failed[/red]', info=msg[:60])
        if DEBUG:
            console.print(f'\n[dim]{traceback.format_exc()}[/dim]')
        _cleanup()
        return False, msg, 0.0

    finally:
        stop_listener.set()
        pause_event.clear()


# ── Single video ───────────────────────────────────────────────────────────

def download_single(url, choice, quality=None):
    save_path = 'audio' if choice == 'mp3' else 'video'
    os.makedirs(save_path, exist_ok=True)

    if quality is None:
        with console.status('[cyan]Fetching info for quality selection…[/cyan]'):
            try:
                with yt_dlp.YoutubeDL(_ydl_base()) as ydl:
                    pre_info = ydl.extract_info(url, download=False)
            except Exception as e:
                msg = _clean_error(e)
                console.print(f'[red]  Could not fetch video info:[/red] {msg}')
                if _is_auth_error(e):
                    console.print(
                        '[yellow]  ↳ This video may require authentication.'
                        ' Set a cookies file with [bold]C[/bold] from the main menu.[/yellow]'
                    )
                if DEBUG:
                    console.print_exception()
                return False

        title    = pre_info.get('title', 'Unknown')
        duration = pre_info.get('duration', 0) or 0
        console.print(f'\n  [bold]{title}[/bold]')

        if duration > 3600:
            console.print(
                f'  [yellow]⚠ This video is {_fmt_duration(duration)} long (over 1 hour) — skipped.[/yellow]'
            )
            return None

        quality = select_quality(choice, pre_info)
        console.print()

    console.print('[dim]  Tip: Press P during download to pause/resume.[/dim]')

    with _make_progress() as p:
        try:
            status, result, elapsed = run_download(url, choice, quality, save_path, p)
        except KeyboardInterrupt:
            console.print('\n[yellow]  Interrupted.[/yellow]')
            return False

    finished_at = datetime.datetime.now().strftime('%H:%M:%S')

    if status is True:
        console.print(f'  [green]✓ Finished in {elapsed:.1f}s  (at {finished_at})[/green]')
    elif status is None:
        if result == 'too_long':
            console.print(f'  [yellow]⏭ Skipped — video is over 1 hour.[/yellow]')
        else:
            console.print(f'  [dim]= Already exists, skipped.[/dim]')
    else:
        console.print(f'  [red]✗ Failed: {result}[/red]')

    return status


# ── Playlist ───────────────────────────────────────────────────────────────

def download_playlist(url, choice):
    global stop_flag
    stop_flag = False

    console.print()
    with console.status('[cyan]Fetching playlist info…[/cyan]'):
        try:
            with yt_dlp.YoutubeDL({**_ydl_base(), 'extract_flat': True}) as ydl:
                playlist_info = ydl.extract_info(url, download=False)
        except Exception as e:
            console.print(f'[red]  Could not fetch playlist: {e}[/red]')
            if DEBUG:
                console.print_exception()
            return

    entries        = playlist_info.get('entries') or []
    total          = len(entries)
    playlist_title = playlist_info.get('title', 'Playlist')
    console.print(Panel(f'[bold]{playlist_title}[/bold]  —  {total} video(s)', expand=False))

    if total == 0:
        console.print('[yellow]  Playlist is empty or unavailable.[/yellow]')
        return

    # quality selection — fetch first reachable video for mp4 format list
    if choice == 'mp4':
        first_info = {}
        for entry in entries[:3]:
            try:
                first_url = (entry.get('webpage_url')
                             or f"https://www.youtube.com/watch?v={entry['id']}")
                with console.status('[cyan]Fetching first video info…[/cyan]'):
                    with yt_dlp.YoutubeDL(_ydl_base()) as ydl:
                        first_info = ydl.extract_info(first_url, download=False)
                break
            except Exception:
                continue
        quality = select_quality(choice, first_info)
    else:
        quality = select_quality(choice, {})

    save_path = 'audio' if choice == 'mp3' else 'video'
    os.makedirs(save_path, exist_ok=True)

    total_start   = time.time()
    completed     = 0
    failed_list   = []
    long_videos   = []
    elapsed_times = []   # track per-video times for ETA

    console.print('\n[dim]  Tip: Ctrl+C to skip/stop  ·  P to pause/resume[/dim]')

    with _make_progress() as p:
        t_overall = p.add_task(
            '[bold cyan]Overall playlist[/bold cyan]',
            total=total, info=f'0/{total}',
        )

        for i, entry in enumerate(entries, 1):
            if stop_flag:
                console.print('[yellow]  Playlist stopped.[/yellow]')
                break

            video_url   = (entry.get('webpage_url')
                           or f"https://www.youtube.com/watch?v={entry['id']}")
            video_title = entry.get('title', 'Unknown')
            remaining   = total - i

            console.print(
                f'\n[cyan]\\[{i}/{total}][/cyan] [bold]{video_title}[/bold]',
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
                    failed_list.append((video_title, 'Interrupted by user'))
                    p.update(t_overall, completed=i, info=f'{i}/{total}')
                    console.print(f'  [yellow]⏭ [{i}/{total}] Skipped (interrupted)[/yellow]')
                    continue
                else:
                    stop_flag = True
                    console.print('[yellow]  Stopping playlist.[/yellow]')
                    break

            p.update(t_overall, completed=i, info=f'{i}/{total}')

            # ETA based on average elapsed so far
            if status is True:
                elapsed_times.append(elapsed)
            avg_time = sum(elapsed_times) / len(elapsed_times) if elapsed_times else 0
            eta_str  = _fmt_eta(avg_time * remaining) if remaining > 0 and avg_time > 0 else '—'

            if status is True:
                completed += 1
                console.print(
                    f'  [green]✓[/green] [{i}/{total}] {video_title[:50]}'
                    f'  [dim]{elapsed:.1f}s · at {finished_at}'
                    + (f' · ETA ~{eta_str}' if remaining > 0 else '') + '[/dim]'
                )
            elif status is None and result == 'too_long':
                long_videos.append((video_title, entry.get('duration', 0) or 0))
                console.print(
                    f'  [yellow]⏭[/yellow] [{i}/{total}] {video_title[:50]}'
                    f'  [yellow]skipped (>1hr)[/yellow]'
                )
            elif status is None:   # already exists
                completed += 1
                console.print(
                    f'  [dim]=[/dim] [{i}/{total}] {video_title[:50]}'
                    f'  [dim]already exists[/dim]'
                )
            else:
                failed_list.append((video_title, result))
                console.print(
                    f'  [red]✗[/red] [{i}/{total}] {video_title[:50]}'
                    f'  [red]{result[:60]}[/red]'
                )

    total_elapsed = round(time.time() - total_start, 1)
    console.print(f'\n[green]Done! {completed}/{total} downloaded in {total_elapsed}s[/green]')

    if long_videos:
        console.print(f'\n[yellow]Skipped {len(long_videos)} video(s) over 1 hour:[/yellow]')
        for t, dur in long_videos:
            console.print(f'  [yellow]⏭[/yellow] {t}  [dim]({_fmt_duration(dur)})[/dim]')

    if failed_list:
        console.print(f'\n[red]{len(failed_list)} failed:[/red]')
        for t, err in failed_list:
            console.print(f'  [red]✗[/red] {t}')
            console.print(f'    [dim]{err[:80]}[/dim]')



# ── Cookies settings ───────────────────────────────────────────────────────

def manage_cookies():
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


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    global DEBUG

    load_settings()

    while True:
        try:
            tags = []
            if DEBUG:
                tags.append('[yellow][DEBUG][/yellow]')
            cf = settings.get('cookies_file', '')
            if cf and os.path.isfile(cf):
                tags.append('[green][🍪 cookies][/green]')
            tag_str = '  ' + '  '.join(tags) if tags else ''
            console.print(f'\n{"─" * 50}{tag_str}')
            mode = console.input(
                'Download a [green](V)ideo[/green], [green](P)laylist[/green],'
                ' [cyan](C)[/cyan]ookies, or [yellow](D)[/yellow]ebug toggle? > '
            ).strip().upper()

            if mode == 'D':
                DEBUG = not DEBUG
                state = '[yellow]ON[/yellow]' if DEBUG else '[dim]OFF[/dim]'
                console.print(f'  Debug mode: {state}')
                continue

            if mode == 'C':
                manage_cookies()
                continue

            if mode in ('V', 'VIDEO'):
                fmt    = console.input('Format — [green](1)[/green] MP4  [green](2)[/green] MP3 > ').strip()
                choice = 'mp3' if fmt in ('2', 'mp3') else 'mp4'
                url    = console.input('URL > ').strip()
                if url:
                    download_single(url, choice)

            elif mode in ('P', 'PLAYLIST'):
                fmt    = console.input('Format — [green](1)[/green] MP4  [green](2)[/green] MP3 > ').strip()
                choice = 'mp3' if fmt in ('2', 'mp3') else 'mp4'
                url    = console.input('URL > ').strip()
                if url:
                    download_playlist(url, choice)

            else:
                console.print('[red]  Invalid choice, enter V, P, C, or D.[/red]')
                continue

        except KeyboardInterrupt:
            console.print('\n[yellow]  Interrupted. Press Enter to go back to menu.[/yellow]')
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
        