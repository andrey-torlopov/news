<p align="center">
  <img src="Docs/banner.png" alt="Letopis Logo" width="600"/>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/Python-3.9%2B-3776AB.svg?logo=python&amp;logoColor=white" alt="Python 3.9+"/>
  </a>
  <a href="scripts/newsfetch.py">
    <img src="https://img.shields.io/badge/feeds-RSS%20%7C%20Atom%20%7C%20RDF-orange.svg" alt="RSS, Atom and RDF feeds"/>
  </a>
  <a href="news_result/">
    <img src="https://img.shields.io/badge/reports-Markdown-000000.svg?logo=markdown&amp;logoColor=white" alt="Markdown reports"/>
  </a>
</p>

# Weekly News

Репозиторий содержит воспроизводимый процесс подготовки еженедельного научно-технического дайджеста. Он нужен, чтобы запускать один и тот же сценарий локально или через подключённый GitHub-репозиторий, исключать уже просмотренные материалы и сохранять готовые Markdown-отчёты, которыми можно делиться по ссылке.

Инструкции еженедельного отчёта находятся в [`news/head.md`](news/head.md), постоянный реестр просмотренных материалов — в [`news/history.md`](news/history.md), история изменений — в [`Docs/CHANGELOG.md`](Docs/CHANGELOG.md).

## Загрузчик

[`scripts/newsfetch.py`](scripts/newsfetch.py) загружает RSS/Atom/RDF-ленты и страницы статей сырыми байтами, а наружу выводит компактный JSONL. Скрипт не выбирает новости и не изменяет `history.md`.

Требования: Python 3.9+ и `curl` в `PATH`.

```bash
python3 scripts/newsfetch.py --version
python3 scripts/newsfetch.py feeds --date 2026-08-08 --period 7 < feeds.tsv
python3 scripts/newsfetch.py pages --text 1500 < urls.tsv
```

Вход `feeds`: `<slug>\t<name>\t<url>\t<tier>`. Вход `pages`: `<key>\t<url>`. Каждая выходная строка содержит JSON-объект с полем `kind`: `DIAG`, `ITEM`, `PAGE` или `ERROR`.

## Проверка

Тесты не обращаются к сети:

```bash
python3 -m unittest discover -s tests -v
```
