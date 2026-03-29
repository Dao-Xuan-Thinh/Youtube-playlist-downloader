# yt-dlp Downloader v2.0

A feature-rich desktop application for downloading videos and audio from YouTube, YouTube Music, and other sources. Built with Python, yt-dlp, and Rich TUI.

## Features

### Smart Search
- **YouTube Music Priority**: Searches YouTube Music first, with a setting to choose search priority (YT / YTM / Both)
- **Live Thumbnail Preview**: Real-time terminal thumbnails using pixel art rendering
- **Arrow-Key Navigation**: Intuitive interface for browsing search results

### Download Modes
- **Single Video/Audio**: Download one URL at a time
- **Batch Queue**: Download multiple URLs from a comma-separated list
- **Playlist Support**: Download entire YouTube playlists with progress tracking
- **Import from File**: Load URLs from a `.txt` file (one per line, `#` for comments)
- **Download History**: Browse, re-download, or delete past downloads with `(H)` menu

### Smart Download Control
- **Auto-Pause/Resume**: Press `Ctrl+C` to interrupt playlists without losing progress
- **Exponential Backoff Retry**: Network errors auto-retry 3 times (2s, 4s, 8s delays)
- **Resume Interrupted Playlists**: Saves progress to `.json`, detects and resumes on next run
- **Format Selection**:
  - MP3 audio extraction with auto-tagging
  - MP4 video with quality presets (1080p, 720p, 480p, minimum)
  - Arrow-key quality picker instead of typing numbers
- **Default Format Preferences**: Save format/quality in settings to avoid repeated prompts

### Enhanced UI/UX
- **Fat Progress Bars**: Visual progress display with percentage and ETA
- **Song Title Display**: Shows song title and duration above each download
- **Batch/Playlist Summary Panel**: Shows downloaded, failed, skipped counts and total time
- **Heavy-Border Menu**: Clean, organized main interface with status indicators

### Advanced Features
- **MusicBrainz Auto-Tagging**: Automatically fills Artist, Album, Year ID3 tags for MP3s
- **Windows Toast Notifications**: Desktop notifications after each download (can toggle in Settings)
- **Concurrent Downloads**: Configurable worker threads for batch/playlist mode
- **Settings Management**: Persistent configuration for cookies, output folder, defaults
- **Debug Mode**: Verbose logging (toggle with `--debug`)

## Installation

### Requirements
- **Python 3.8+**
- **FFmpeg** (for audio/video post-processing)
- **Windows 10+** (for toast notifications)

### Setup
```bash
pip install -r requirements.txt
```

### Build Standalone EXE
```bash
pyinstaller v1.1_refine.spec --noconfirm
# Output: dist/v1.1_refine/v1.1_refine.exe
```

## Usage

### Run the Application
```bash
python v1.1_refine.py
```

Or launch the built EXE:
```
dist/v1.1_refine/v1.1_refine.exe
```

### Main Menu Options
```
[1] Single URL       Download one video/audio
[2] Search          Find and download from YouTube/YTM
[3] Playlist        Download entire YouTube playlist
[4] Batch Queue     Download multiple URLs
[S] Settings        Configure app options
[H] History         Browse download history
[F] Import File     Load URLs from .txt file
[Q] Quit
```

### Settings Menu `[S]`
```
[1] Output Folder         Where to save downloads
[2] Default Format        MP3 or MP4 (skip format prompt)
[3] Default Quality       Resolution for MP4 (skip quality prompt)
[4] Concurrent Downloads  Worker threads (default: 2)
[5] Search Source         YouTube / YouTube Music / Both
[6] Notifications         Enable/disable toast popups
[7] Auto-tag MP3         Auto-fill ID3 tags from MusicBrainz
```

### Download History `[H]`
- Browse with arrow keys
- Press Enter to re-download selected item
- Press D to delete from history
- Max 500 entries stored in history.json

### Keyboard Shortcuts
```
↑↓          Navigate menu / search results
Enter       Confirm selection
Esc         Cancel current action
Ctrl+C      Pause/interrupt download (playlists can resume)
```

## File Structure

```
v1.1_refine.py              # Main application (1600+ lines)
v1.1_refine.spec            # PyInstaller build configuration
settings.json               # Runtime settings (auto-created)
history.json                # Download history (auto-created)
dist/v1.1_refine/           # Built executable folder
```

## Configuration Files

### `settings.json`
```json
{
  "output_folder": "",
  "default_format": "mp3",
  "default_quality": "720",
  "concurrent_downloads": 2,
  "search_source": "both",
  "notifications": true,
  "auto_tag": true
}
```

### `history.json`
```json
[
  {
    "title": "Never Gonna Give You Up",
    "url": "https://www.youtube.com/watch?v=...",
    "format": "mp3",
    "quality": "best",
    "duration": 213,
    "date": "2026-03-29"
  }
]
```

## Troubleshooting

### YouTube Music search returns no results
**Issue**: ytmsearch: prefix is no longer supported in yt-dlp.
**Fix**: Already patched in v2.0+ - uses direct music.youtube.com/search URLs.

### FFmpeg errors during MP3 conversion
**Issue**: Error selecting an encoder for stream 0:0
**Solution**: Update FFmpeg to latest version, or specify explicit codec in yt-dlp options.

### Playlist resume not working
**Check**: Make sure history.json and .playlist_state files exist in the app folder. Don't delete them between runs.

### Notifications not showing
**Check**: Windows 10+ required. Verify Action Center is enabled in Windows Settings. Toggle [6] Notifications in app settings.

### MusicBrainz tagging fails silently
**Expected**: If no match found within 30s duration tolerance, tags are skipped. Check app folder logs (if --debug enabled).

## Release Notes

### v2.0 - Major Search & Features Overhaul
**Date**: March 2026

#### 🔧 Fixes
- **Fixed YouTube Music search** — replaced defunct `ytmsearch:` prefix with native `music.youtube.com/search` URL support
- **Cover art rendering** — fixed pillarbox/letterbox bars by using correct aspect ratio handling

#### ✨ New Features
- Arrow-key quality selector (replaces number input)
- Batch & playlist summary panels
- Download history browser with re-download & delete
- Import URLs from `.txt` file
- Auto-retry with exponential backoff
- Windows toast notifications
- MusicBrainz auto-tagging for MP3s
- YouTube Music search priority setting

#### 🎨 UI/UX Improvements
- Fat progress bar with block characters (`[████░░░░]  67%`)
- Song title display above each download
- Heavy-border blue menu panel
- Real terminal thumbnails with arrow-key preview
- Better error messaging

#### 🐛 Known Issues
- Narrow terminals (<60 cols) may wrap thumbnail panel awkwardly
- MusicBrainz requires internet (gracefully skips if unavailable)
- Large playlists (1000+) may take 30+ minutes to complete

### v1.1 - Initial Release
- Basic single/playlist/batch download
- yt-dlp integration with format selection
- Settings management
- Simple CLI interface

## Contributing

Bug reports and feature requests welcome! Please test the current EXE before reporting issues.

## License

Personal project. Use freely for non-commercial purposes.

## Credits

Built with:
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Video downloader
- [Rich](https://rich.readthedocs.io) — Terminal UI
- [mutagen](https://mutagen.readthedocs.io) — ID3 tag editing
- [MusicBrainz](https://musicbrainz.org) — Music metadata
