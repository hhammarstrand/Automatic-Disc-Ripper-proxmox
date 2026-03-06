# Automatic Disc Ripper for Windows

**Automatic Disc Ripper for Windows** — automated DVD ripping with MakeMKV, transcoding with HandBrake, and a web UI for monitoring and control.

Insert a DVD. ADR automatically rips it with MakeMKV, ejects the disc so the next one can be inserted, and transcodes to MP4 with HandBrake in the background. The entire workflow is monitored and controlled from a web interface accessible from any device on the network.

> Inspired by [Automatic Disc Ripper](https://github.com/automatic-ripping-machine/automatic-ripping-machine) for Linux, but written from scratch for Windows.

---

## Features

- **Automatic disc detection** — WMI-based monitoring of optical drives. Insert a DVD and ripping starts automatically.
- **MakeMKV ripping** — Rips titles from DVD to MKV using `makemkvcon` in robot mode.
- **HandBrake transcoding** — Transcodes ripped MKV files to MP4 with a configurable preset.
- **Automatic eject** — The disc is ejected when ripping completes so the next disc can be inserted while transcoding continues.
- **TMDb identification** — Looks up movie title, year, and poster via The Movie Database API.
- **Plex-compatible folder structure** — Finished files are saved as `Movie (Year)/Movie (Year).mp4`.
- **LAN web interface** — Dashboard with real-time progress, history, and settings. Accessible from any device on the network.
- **Multiple drives** — Support for parallel ripping from multiple DVD drives.
- **Encode queue** — Encoding jobs are queued and run independently of ripping.
- **Watch folder** — Monitor a folder and automatically transcode any video files that appear.
- **Custom HandBrake presets** — Place JSON preset files in `presets/` and they are discovered automatically.

---

## Quick Start

Everything below is done on **the machine with the DVD drive**.

### 1. Install MakeMKV and HandBrakeCLI

These two programs do the actual ripping and transcoding. ADR controls them automatically, but they must be installed first.

| Software | Link | Notes |
|----------|------|-------|
| **MakeMKV** | https://www.makemkv.com/download/ | Install normally. The default path works. |
| **HandBrakeCLI** | https://handbrake.fr/downloads2.php | Download the **CLI version** (not the GUI). Extract `HandBrakeCLI.exe` to `C:\Program Files\HandBrake\`. |

### 2. Download and install ADR

**Option A — Download ZIP (no Git required):**

1. Click the green **Code** button at the top of this page, then **Download ZIP**
2. Extract the ZIP to a folder, e.g. `C:\ADR`
3. Run `install.bat`

**Option B — Clone with Git:**

```powershell
git clone https://github.com/hhammarstrand/Automatic-Disc-Ripper-for-Windows.git
cd Automatic-Disc-Ripper-for-Windows
install.bat
```

The installation script handles everything else automatically:
- Installs **Python** if not already present (downloads and runs the installer)
- Creates a virtual environment and installs all Python dependencies
- Creates `config\adr.yaml` from the example configuration
- Creates output directories (`C:\ADR\raw`, `C:\ADR\completed`)
- Checks that MakeMKV and HandBrakeCLI are found
- Optionally launches a setup GUI for easy configuration

<details>
<summary>Manual installation (without install.bat)</summary>

Install Python 3.11+ from https://www.python.org/downloads/ (check **"Add python.exe to PATH"**), then:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config\adr.yaml.example config\adr.yaml
```

If `Activate.ps1` is blocked by Execution Policy:
```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```
</details>

### 3. Configure

If you skipped the setup GUI during installation, edit `config\adr.yaml` to adjust paths and add your TMDb API key:

```yaml
# Directories (make sure there is plenty of disk space)
raw_path: "C:\\ADR\\raw"
completed_path: "C:\\ADR\\completed"

# HandBrake preset (run HandBrakeCLI --preset-list for all options)
handbrake_preset: "Fast 1080p30"

# TMDb API key (optional — enables automatic title identification + movie posters)
# Get one for free: https://www.themoviedb.org/settings/api
tmdb_api_key: ""
```

You can also set the TMDb key as the environment variable `ADR_TMDB_API_KEY` instead of putting it in the config file.

> All settings can also be changed later via the web interface (Settings page).

### 4. Start

```powershell
start.bat
```

Or manually:
```powershell
.venv\Scripts\Activate.ps1
python run.py
```

On startup you will see:
```
Web UI (local):  http://localhost:8080
Web UI (LAN):    http://192.168.1.42:8080
Waiting for disc insertion...
```

### 5. Open the web interface

- **On the ripping machine:** http://localhost:8080
- **From another device on LAN:** Use the LAN address shown in the terminal.

> **If you cannot reach it from the network:** Open port 8080 in the Windows firewall:
> ```powershell
> New-NetFirewallRule -DisplayName "Automatic Disc Ripper for Windows" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
> ```

---

## Usage

### DVD Ripping

1. Insert a DVD into any monitored drive
2. ADR detects the disc and creates a job automatically
3. The movie title is identified via TMDb (if an API key is configured)
4. MakeMKV rips titles to MKV
5. The disc is ejected — **insert the next disc right away!**
6. HandBrake transcodes MKV to MP4 in the background
7. The finished file is saved in `completed_path` under `Movie (Year)/Movie (Year).mp4`

### Watch Folder

Configure `watch_path` in `config/adr.yaml` (or via the web Settings page):

```yaml
watch_path: "C:\\ADR\\watch"
watch_output_path: "C:\\ADR\\encoded"
```

- Copy any video file (`.mkv`, `.avi`, `.mp4`, `.mov`, `.ts`, etc.) to the watch folder
- ADR picks up the file, transcodes it with HandBrake, and saves it to the output folder
- Files that are still being copied are left alone until they stop growing

### Custom HandBrake Presets

Place one or more `.json` preset files in the `presets/` folder in the project root. ADR discovers them automatically.

To export a preset from HandBrake:
1. Open HandBrake GUI
2. Go to Presets
3. Right-click your preset and choose *Export to file*
4. Save the `.json` file in `presets/`

Then set the preset name in the configuration:
```yaml
handbrake_preset: "My Preset Name"
```

> The preset name must match the `PresetName` inside the JSON file exactly.

### Command-line Arguments

```powershell
python run.py                  # Default start
python run.py --port 9090      # Different port
python run.py --host 127.0.0.1 # Local access only (no LAN)
python run.py --config C:\my\config.yaml  # Custom config file
```

---

## Web Interface

| Page | Description |
|------|-------------|
| **Dashboard** (`/`) | Drive overview, active jobs with progress bars, watch folder status, recent completed jobs |
| **History** (`/history`) | All jobs with status filter |
| **Settings** (`/settings`) | Edit all settings via a web form |

The web UI auto-refreshes every 3 seconds.

### REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/jobs` | GET | List all jobs (filterable with `?status=...`) |
| `/api/jobs/<id>` | GET | Details for a single job |
| `/api/jobs/<id>/cancel` | POST | Cancel a job |
| `/api/jobs/<id>/rematch` | POST | Re-run TMDb search for a job |
| `/api/drives` | GET | Status of all monitored drives |
| `/api/status` | GET | System status (queue, workers, watch folder) |
| `/api/settings` | GET/POST | Read/write configuration |
| `/api/presets` | GET | List available HandBrake presets |
| `/api/preset-check` | GET | Verify configured preset |

---

## Run Automatically at Startup

### Option A — Scheduled Task (recommended)

```powershell
$action = New-ScheduledTaskAction `
    -Execute "$PWD\.venv\Scripts\python.exe" `
    -Argument "$PWD\run.py" `
    -WorkingDirectory "$PWD"

$trigger = New-ScheduledTaskTrigger -AtLogOn

Register-ScheduledTask -TaskName "Automatic Disc Ripper for Windows" -Action $action -Trigger $trigger `
    -Description "Automatic Disc Ripper" -RunLevel Highest
```

### Option B — Shortcut in the Startup folder

1. Press `Win+R`, type `shell:startup`, press Enter
2. Create a shortcut with target: `.venv\Scripts\python.exe run.py`
3. Set "Start in" to the project folder

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| **"No optical drives detected"** | Make sure the DVD drive is visible in File Explorer. Try unplugging and re-plugging USB drives. |
| **"MakeMKV not found"** | Check that `makemkv_path` points to the correct file. Default: `C:\Program Files (x86)\MakeMKV\makemkvcon64.exe` |
| **"HandBrakeCLI not found"** | Download the CLI version from handbrake.fr and update `handbrake_path`. |
| **Web UI not reachable from LAN** | Open port 8080 in the firewall (see above). Check that `web_host` is `0.0.0.0`. |
| **File not picked up from watch folder** | Check that the file has a video file extension. Wait at least 10 seconds after copying. |
| **Execution Policy blocks scripts** | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

---

## Development

### Project Structure

```
automatic-disc-ripper/
├── adr/                    # Application logic
│   ├── config.py           # YAML configuration with fallbacks
│   ├── models.py           # SQLAlchemy models (Job, Track)
│   ├── disc.py             # WMI-based disc detection + eject
│   ├── ripper.py           # MakeMKV subprocess wrapper
│   ├── encoder.py          # HandBrakeCLI subprocess wrapper
│   ├── identify.py         # TMDb API lookup
│   ├── pipeline.py         # Orchestrator: detect -> rip -> eject -> encode
│   ├── watcher.py          # Watch folder: file monitoring + auto-encode
│   └── utils.py            # Shared helper functions and constants
├── web/
│   ├── app.py              # Flask app, routes, and REST API
│   ├── templates/          # Jinja2 HTML templates
│   └── static/             # CSS, JS, favicon
├── config/
│   ├── adr.yaml.example    # Example configuration
│   └── adr.yaml            # Local configuration (gitignored)
├── presets/                # Custom HandBrake preset files (.json)
├── tests/                  # Unit tests (pytest)
├── install.bat             # Installation script
├── start.bat               # Start script
├── run.py                  # Entry point
├── requirements.txt        # Python dependencies
└── pyproject.toml          # Project metadata, pytest/ruff configuration
```

### Running Tests

```powershell
pip install pytest
python -m pytest tests/ -v
```

### Tech Stack

- **Python 3.11+** with Flask, SQLAlchemy, PyYAML
- **SQLite** (WAL mode) for the job database
- **WMI + pywin32** for disc detection and eject (Windows-specific)
- **MakeMKV** (`makemkvcon64.exe`) for ripping
- **HandBrakeCLI** for transcoding
- **TMDb API** for movie identification
- **Vanilla JS + CSS** in the web interface (no heavy frameworks)

---

## License

MIT — see [LICENSE](LICENSE).
