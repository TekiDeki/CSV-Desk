# CSV-Desk

A local CSV and TSV editor and cleaner. One HTML file, no runtime dependencies, no account and no upload.

**Live:** [tekideki.github.io/CSV-Desk](https://tekideki.github.io/CSV-Desk/). The source and local pre-publication checks are complete.

## Features

- Open CSV, TSV or TXT files up to 8 MiB, encoded as UTF-8 or UTF-16 with a BOM.
- Detect comma, semicolon, tab or pipe delimiters, including quoted delimiters, escaped quotes and newlines inside quoted fields.
- Edit cells, add or delete rows, rename, reorder, hide, add or delete columns.
- Search all columns, sort with numeric-aware collation and paginate the editable table.
- Trim whitespace, remove empty rows, remove exact duplicate rows and apply literal find and replace.
- Undo and redo up to twenty operations; restore the loaded original without touching the source file.
- Export all data or the filtered and sorted view as CSV, TSV or JSON. Optional UTF-8 BOM for spreadsheet compatibility.
- Save up to three small sessions manually in localStorage. Files are not persisted automatically.
- English, Romanian, Serbian Latin and Serbian Cyrillic; light and dark themes; offline app shell and honest installation help.

## Privacy and limits

Files are read and exported locally. No data is sent to a server. CSV values remain text and spreadsheet formulas are not executed. JSON export makes duplicate or empty headers unique and keeps values as strings. Very large files are intentionally rejected so the editable table remains responsive. XLSX is outside the first version.

## Run locally

Serve this directory over HTTP, for example:

```sh
python3 -m http.server 8080
```

Then open `http://localhost:8080/`. The main editor also works from `index.html` directly, but the service worker and installation require HTTP localhost or HTTPS.

## Tests

The Playwright suite covers parsing, malformed input, delimiter detection, UTF encodings, editing, cleanup, history, export bytes, sessions, four languages, mobile layout, offline reload and zero unexpected network hosts. With Python, Playwright and Chromium installed, run:

```sh
python3 tests/test_csv_desk.py
```

The suite starts its own local server, writes `tests/report.json` and refreshes the desktop and mobile screenshots.

## License

Apache 2.0 - see [LICENSE](LICENSE).
