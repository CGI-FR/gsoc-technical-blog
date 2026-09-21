# Outlook Evidence Viewer

Qt-based viewers for triaging Outlook mail evidence from two common sources:

- PST/OST/PFF archives through `libpff`
- Outlook for Mac SQLite databases such as `Outlook.sqlite`

The tool is intended for forensic workflows where an analyst needs to
quickly browse folders, inspect messages, spot external/personal email
addresses, identify large messages or attachments, and filter activity by time.

## Disclaimer

This tool is provided as-is, with absolutely no warranty of any kind. Use it at your own risk. The authors and contributors are not liable for any damages, data loss, investigative errors, or other consequences arising from its use.

## Features

- Unified launcher that detects supported file types.
- Folder tree with direct and recursive message counts.
- Timeline histogram across the mailbox.
- Click-drag timeline filtering with included/excluded folder counts.
- Selected-folder overlay on the timeline.
- Message list with sortable newest/oldest date order.
- Local text search in the loaded folder.
- Global text search for Outlook for Mac SQLite databases.
- Highlighting for:
  - external addresses
  - likely personal webmail domains
  - messages with attachments
  - large messages by per-folder size heatmap
- Message detail tabs with summary, metadata, JSON, and body preview where available.
- PFF/PST/OST attachment extraction when exposed by the installed `libpff` bindings.
- Outlook for Mac `.olk15Message` path display when the SQLite database references external message files.

## Requirements

- Python 3.10 or newer recommended.
- `PySide6` for the GUI.
- `libpff-python` for PST/OST/PFF archive support.

Install the Python requirements:

```bash
python -m pip install PySide6 libpff-python
```

If you only need the Outlook for Mac SQLite reader:

```bash
python -m pip install PySide6
```

The project uses Python's built-in `sqlite3` module for Outlook for Mac databases.

## Windows Setup

From PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install PySide6 libpff-python
```

Run the unified launcher:

```powershell
python -m ost_pst_parser C:\path\to\Outlook.sqlite
python -m ost_pst_parser C:\path\to\mailbox.ost
python -m ost_pst_parser C:\path\to\mailbox.pst
python -m ost_pst_parser --light-load C:\path\to\large-mailbox.pst
```

For double-click use, run `Launch Outlook Reader.bat` or drag an Outlook
SQLite/PST/OST file onto it. The batch launcher runs `python -m ost_pst_parser`
through `py -3` when available, falls back to `python`, and keeps the console
open if startup fails.

If PowerShell blocks virtualenv activation, run:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Linux Setup

From a shell:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install PySide6 libpff-python
```

Run the unified launcher:

```bash
python -m ost_pst_parser /path/to/Outlook.sqlite
python -m ost_pst_parser /path/to/mailbox.ost
python -m ost_pst_parser /path/to/mailbox.pst
```

For large PST/OST/PFF files on slow storage such as sshfs, start the PFF
viewer in light load mode:

```bash
python -m ost_pst_parser --light-load /path/to/mailbox.pst
python -m ost_pst_parser --low-io /path/to/mailbox.ost
```

On some Linux distributions, Qt may require additional system libraries. If the GUI fails to start because of missing Qt/XCB libraries, install your distribution's Qt/XCB support packages, for example on Debian/Ubuntu:

```bash
sudo apt-get install libxcb-cursor0 libxkbcommon-x11-0
```

## Launchers

Preferred entry point:

```bash
python -m ost_pst_parser <evidence-file>
```

After installing the package, the console entry point is also available:

```bash
outlook-reader <evidence-file>
```

The launcher detects:

- SQLite databases by file magic and the presence of `Mail` and `Folders` tables.
- PST/OST/PFF archives by file extension.

The compatibility launchers still work from a source checkout:

```bash
python mac-outlook-reader.py Outlook.sqlite
python outlook_reader.py mailbox.ost
```

## Typical Evidence Locations

Common Windows Outlook locations:

- `%LOCALAPPDATA%\Microsoft\Outlook\*.ost`
- `%LOCALAPPDATA%\Microsoft\Outlook\*.pst`
- `%USERPROFILE%\Documents\Outlook Files\*.pst`
- Legacy or manually configured PST locations can appear elsewhere on disk, so confirm configured data files from Outlook/account artifacts when possible.

Common Outlook for Mac locations:

- `~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/<profile>/Data/Outlook.sqlite`
- `~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/<profile>/Data/Messages/.../*.olk15Message`
- `~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/<profile>/Data/Folders/.../*.olk15Folder`

Windows Outlook evidence can be collected with KAPE using the `OutlookPSTOST` target, for example:

```powershell
.\kape.exe --tsource C:\ --tdest C:\somefolder\ --target OutlookPSTOST --sim
.\kape.exe --tsource C:\ --tdest C:\somefolder\ --target OutlookPSTOST
```

That target is preferred because KAPE includes handling to collect Outlook PST/OST data even when `outlook.exe` has file locks that would otherwise interfere with straightforward copying.

## Outlook for Mac SQLite Notes

The SQLite reader uses the `Mail`, `Folders`, `Categories`, and account tables directly.

It can show:

- folder hierarchy
- subjects, senders, recipients, dates, preview text
- categories
- message size
- attachment flag
- path to the referenced `.olk15Message` file

It cannot currently show attachment names, attachment sizes, or full message bodies unless those details are present in the database. In many Outlook for Mac cases, the richer message content lives in associated `.olk15Message` files referenced by `Mail.PathToDataFile`.

## PST/OST/PFF Notes

The PFF viewer uses `libpff-python`.

It can show:

- folder hierarchy
- message metadata
- body content where exposed by libpff
- PFF properties
- SMTP headers
- attachment list and save buttons where attachment bytes are exposed

The timeline index for PFF archives intentionally reads timestamps only. It does not build a global full-text index, because parsing every message body in a large archive can be expensive on an analyst workstation.

Use `Options -> Low disk I/O mode` or launch with `--light-load`/`--low-io`
when a PST/OST/PFF is on slow remote storage. In this mode the viewer skips
the global recipient/contact-cache scan, does not build or show the timeline,
does not read per-folder message counts while enumerating folders, and only
loads message objects after you select a folder.

The PFF viewer also enables low disk I/O mode automatically when opening the
archive takes more than 10 seconds, or when preliminary recipient/contact-cache
loading or folder-tree loading exceeds 30 seconds. If that happens during
contact-cache loading, the scan stops early and keeps any contacts already
found. Folder-tree parsing runs in the background so the UI can stay responsive
while slow archives are enumerated. Folders appear progressively as they are
discovered, so you can open a known folder such as Inbox before the full archive
tree has finished loading. In low disk I/O mode, folder count columns stay blank
until a folder is loaded or a cached count is available.

## Filtering

Timeline filtering:

- Click and drag across the timeline.
- Release to apply the time window.
- Double-click the timeline to clear it.
- Or use `Filter -> Clear Time Filter`.

Text filtering:

- PFF/PST/OST viewer: searches the currently loaded folder.
- Outlook for Mac SQLite viewer: searches globally and updates included/excluded folder counts.

## Troubleshooting

Missing GUI dependency:

```bash
python -m pip install PySide6
```

Missing PFF support:

```bash
python -m pip install libpff-python
```

If `pypff` imports but is the wrong package, remove it and reinstall the libpff bindings:

```bash
python -m pip uninstall pypff
python -m pip install libpff-python
```

If `libpff-python` is not available as a wheel for your platform/Python version, install libpff from the upstream project and enable its Python bindings.

## Building a Windows Wheelhouse

To distribute to Windows users without requiring local compiler setup, build or
collect the wheels once on a matching Windows/Python version. If colleagues use
64-bit Python 3.14, build the wheelhouse with 64-bit Python 3.14 too.

On the compiler/build machine:

```powershell
.\Build Windows Distribution.ps1
```

The script builds a staged wheelhouse and writes a zip under `release/`, for
example `release/ost-pst-parser-2026.06.30-windows-py3.14.zip`.

If `libpff-python` is not available from PyPI for Python 3.14, build it locally
on the compiler/build machine and copy the generated wheel into `wheels` before
running the script.
Compiled wheels are Python-version-specific: for Python 3.14 receivers, the
libpff wheel must look like `libpff_python-...-cp314-cp314-win_amd64.whl`.

Ship the generated zip to the receiver. After extraction, it contains:

- `wheels/ost_pst_parser-...-py3-none-any.whl`
- `wheels/libpff_python-...-cp314-cp314-win_amd64.whl`
- all dependency wheels downloaded into `wheels`, such as `pyside6`,
  `pyside6_addons`, `pyside6_essentials`, and `shiboken6`
- `Install Outlook Reader.bat`
- `Launch Outlook Reader.bat`
- `README.md`
- `WHEELHOUSE_CONTENTS.txt`

On the receiver machine, install without contacting PyPI by extracting the zip
and double-clicking `Install Outlook Reader.bat`. The manual equivalent is:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install --no-index --find-links wheels ost-pst-parser
.\.venv\Scripts\outlook-reader.exe C:\path\to\mailbox.ost
```

## Development Notes

### Local Configuration

Organization-specific tuning lives in `ost_pst_parser/local_config.py`.

Edit that file to customize:

- internal/corporate email domains
- ignored email domains or exact addresses
- personal mailbox provider domains
- Microsoft Purview/MIP/MSIP label UUIDs, names, enabled flags, and colors
- MAPI property IDs used to discover MSIP label metadata

Shared modules:

- `ost_pst_parser/outlook_reader.py`: unified file-type launcher.
- `ost_pst_parser/timeline_view.py`: reusable timeline widget and time filtering.
- `ost_pst_parser/local_config.py`: organization-local domains and MSIP label mappings.
- `ost_pst_parser/email_domains.py`: shared personal/external email-domain detection using local config.
- `ost_pst_parser/formatting.py`: common display formatting helpers.
- `ost_pst_parser/table_colors.py`: shared table highlight and heatmap colors.
- `ost_pst_parser/mac_outlook_reader.py`: Outlook for Mac SQLite viewer.
- `ost_pst_parser/ost_viewer.py`: PST/OST/PFF viewer.
- `ost_pst_parser/pff_access.py`: libpff access helpers.
- `ost_pst_parser/rendering.py`: body/rendering helpers.
