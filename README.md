# Weekly News

Инструкции еженедельного отчёта находятся в [`news/head.md`](news/head.md), постоянный реестр просмотренных материалов — в [`news/seen.md`](news/seen.md).

## Загрузчик

[`scripts/newsfetch.py`](scripts/newsfetch.py) загружает RSS/Atom/RDF-ленты и страницы статей сырыми байтами, а наружу выводит компактный JSONL. Скрипт не выбирает новости и не изменяет `seen.md`.

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
