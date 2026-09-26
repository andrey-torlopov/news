<p align="center">
  <img src="Docs/banner.png" alt="Weekly News" width="600"/>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB.svg?logo=python&amp;logoColor=white" alt="Python 3.9+"/>
  </a>
  <a href="scripts/newsfetch.py">
    <img src="https://img.shields.io/badge/feeds-RSS%20%7C%20Atom%20%7C%20RDF-orange.svg" alt="RSS, Atom and RDF feeds"/>
  </a>
  <a href="results/">
    <img src="https://img.shields.io/badge/reports-Markdown-000000.svg?logo=markdown&amp;logoColor=white" alt="Markdown reports"/>
  </a>
</p>

# Weekly News

Read this in other languages: [Русский](README-ru.md)

This repository provides a reproducible workflow for preparing a weekly science and technology digest. It lets you run the same process locally or through a connected GitHub repository, exclude previously reviewed material, and save finished Markdown reports that can be shared by link.

The weekly report instructions are in [`news/head.md`](news/head.md), the persistent record of reviewed material is in [`news/history.md`](news/history.md), and the changelog is in [`Docs/CHANGELOG.md`](Docs/CHANGELOG.md).

## Fetcher

[`scripts/newsfetch.py`](scripts/newsfetch.py) fetches RSS/Atom/RDF feeds and article pages as raw bytes and outputs compact JSONL. The script does not select news items or modify `history.md`.

Requirements: Python 3.9+ and `curl` in `PATH`.

```bash
python3 scripts/newsfetch.py --version
python3 scripts/newsfetch.py feeds --date 2026-08-08 --period 7 < feeds.tsv
python3 scripts/newsfetch.py pages --text 1500 < urls.tsv
```

Input for `feeds`: `<slug>\t<name>\t<url>\t<tier>`. Input for `pages`: `<key>\t<url>`. Each output line contains a JSON object with a `kind` field: `DIAG`, `ITEM`, `PAGE`, or `ERROR`.

## Testing

The tests do not access the network:

```bash
python3 -m unittest discover -s tests -v
```
