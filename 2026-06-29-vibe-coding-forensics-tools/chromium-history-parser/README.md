# Chromium History Browser

A Python + Qt desktop viewer for Chromium-family `History` SQLite files, intended for quick review of browsing activity during forensic investigations.

## Disclaimer

This tool is provided as-is, with absolutely no warranty of any kind. Use it at your own risk. The authors and contributors are not liable for any damages, data loss, investigative errors, or other consequences arising from its use.

## Features

- Opens a Chrome/Chromium/Edge `History` SQLite database.
- Opens the default Chrome or Edge profile folder from the File menu.
- Accepts drag-and-drop of `History` files, or profile folders containing a `History` file.
- Shows a histogram timeline of activity over time.
- Zooms the timeline with mouse wheel, drag-to-select, zoom-out, and reset controls.
- Lists visits and downloads together in one chronological table.
- Displays ISO timestamps, event type, title, URL, source/referrer context, visit counts, typed counts, and download metadata where available.
- Opens selected databases directly in SQLite read-only mode.

## Install

```powershell
py -m pip install -r requirements.txt
```

## Run

Double-click:

```text
Launch Chromium History Browser.bat
```

The launcher uses the default Python selected by `py`, falling back to `python` if the Python Launcher is unavailable.
If a file named `History` is beside the script, it opens automatically.

Or run manually:

```powershell
py .\chromium_history_browser.py
```

If no file path is passed and no `History` file is beside the script, the app opens a file picker automatically.

You can also pass the file path directly:

```powershell
py .\chromium_history_browser.py .\History
```
