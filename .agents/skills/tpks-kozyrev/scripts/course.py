#!/usr/bin/env python3
"""Offline source inventory and evidence lookup. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCHEMA = 1
TEXT_SUFFIXES = {'.txt', '.md'}
NORMALIZED_SUFFIXES = TEXT_SUFFIXES | {'.tex', '.cls', '.json', '.yaml', '.yml', '.sh'}
KINDS = ('slides', 'lecture-notes', 'student-guide', 'past-project',
         'analysis', 'domain-reference', 'inherited-project')


def root_dir(explicit: str | None = None) -> Path:
    candidates = [Path(explicit).resolve()] if explicit else list(Path(__file__).resolve().parents)
    for path in candidates:
        if (path / 'materials').is_dir() and (path / 'notes').is_dir():
            return path
    raise ValueError('Нет материалов репозитория. Передайте --root PATH к guide_tpks.')


def read(path: Path) -> str:
    return path.read_text(encoding='utf-8-sig')


def fingerprint(path: Path) -> tuple[bytes, str]:
    """Keep catalogue stable across Git's Windows/Linux newline conversions."""
    data = (read(path).encode('utf-8') if path.suffix.lower() in NORMALIZED_SUFFIXES
            else path.read_bytes())
    return data, hashlib.sha256(data).hexdigest()


def category(path: str) -> str:
    if path.startswith('materials/kozyrev_slides/'):
        return 'slides'
    if path.startswith('materials/lectures/'):
        return 'lecture-notes'
    if path.startswith(('materials/past_projects/', 'materials/example_2021/')):
        return 'past-project'
    if path == 'notes/00_guide_from_seniors.md':
        return 'student-guide'
    if path == 'notes/04_dpm_platform_v.md':
        return 'domain-reference'
    if path.startswith('notes/'):
        return 'analysis'
    return 'inherited-project'


def inventory(root: Path) -> list[dict]:
    paths = [p for folder in ('materials', 'notes', 'docs', 'pdf')
             for p in (root / folder).rglob('*')
             if p.is_file() and 'build' not in p.relative_to(root).parts
             and not p.name.startswith('~$')]
    entries = []
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        data, digest = fingerprint(path)
        entry = {'path': rel, 'kind': category(rel), 'bytes': len(data),
                 'sha256': digest,
                 'hash_mode': 'utf8-lf' if path.suffix.lower() in NORMALIZED_SUFFIXES else 'raw',
                 'searchable': path.suffix.lower() in TEXT_SUFFIXES,
                 'approval': 'not-established'}
        if entry['searchable']:
            text = read(path)
            # Physical newline numbers: form feed is a page boundary, not a newline.
            entry['lines'] = len(text.split('\n'))
            if '\f' in text:
                pages = text.split('\f')
                if not pages[-1].strip():
                    pages.pop()
                entry['pdf_pages'] = len(pages)
            original = path.with_suffix('')
            if path.suffix == '.txt' and original.is_file():
                entry['original'] = original.relative_to(root).as_posix()
            elif rel == 'materials/example_2021/example_students_system.txt':
                entry['original'] = 'materials/example_2021/Programmnaya_sistema_dlya_studentov.pdf'
        else:
            companion = Path(str(path) + '.txt')
            if companion.is_file():
                entry['text'] = companion.relative_to(root).as_posix()
            elif rel == 'materials/example_2021/Programmnaya_sistema_dlya_studentov.pdf':
                entry['text'] = 'materials/example_2021/example_students_system.txt'
        entries.append(entry)
    return entries


def catalog(root: Path) -> None:
    entries = inventory(root)
    target = root / 'knowledge'
    target.mkdir(exist_ok=True)
    payload = {'schema': SCHEMA, 'sources': entries}
    (target / 'sources.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# Карта источников', '',
             'Сгенерировано `course.py catalog`. Метка категории описывает происхождение,',
             'а не подтверждает правильность или приёмку. SHA-256 хранится в `sources.json`.',
             'Для текста размер и хеш рассчитаны после нормализации переносов в LF (`utf8-lf`);',
             'для бинарных оригиналов используются исходные байты (`raw`).', '',
             '| Файл | Категория | Поиск |', '|---|---|---|']
    for entry in entries:
        rel = entry['path']
        search = 'текст'
        if not entry['searchable']:
            search = entry.get('text', 'оригинал; искать вручную')
        lines.append(f"| [{rel}](../{rel.replace(' ', '%20')}) | {entry['kind']} | {search} |")
    lines.extend(['', 'Исходные страницы, схемы и таблицы проверять по оригиналу.',
                  'Выжимки в notes/ содержат как пересказ, так и авторские рекомендации.', ''])
    (target / 'sources.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'Учтено файлов: {len(entries)}; доступны для текстового поиска: {sum(e["searchable"] for e in entries)}')


def load_catalog(root: Path) -> list[dict]:
    path = root / 'knowledge/sources.json'
    if not path.is_file():
        raise ValueError('Нет каталога. Выполните course.py catalog.')
    payload = json.loads(read(path))
    if payload['schema'] != SCHEMA:
        raise ValueError('Неизвестная версия каталога; перестройте его.')
    return payload['sources']


def blocks(text: str):
    """Yield page-sized PDF blocks or overlapping windows of newline-numbered text."""
    if '\f' in text:
        start = 1
        for page, body in enumerate(text.split('\f'), 1):
            if body.strip():
                yield page, start, body
            start += body.count('\n')
    else:
        lines = text.split('\n')
        for start in range(0, len(lines), 20):
            body = '\n'.join(lines[start:start + 30])
            if body.strip():
                yield None, start + 1, body


def normalized(text: str) -> str:
    return re.sub(r'\s+', ' ', text.casefold().replace('ё', 'е'))


def search(root: Path, args) -> int:
    patterns = ([re.compile(args.query, re.IGNORECASE)] if args.regex else
                [re.compile(re.escape(t)) for t in normalized(args.query).split()])
    if not patterns:
        raise ValueError('Пустой поисковый запрос.')
    found = []
    for entry in load_catalog(root):
        if not entry['searchable'] or (args.kind and entry['kind'] != args.kind):
            continue
        text = read(root / entry['path'])
        for page, start, body in blocks(text):
            haystack = body if args.regex else normalized(body)
            matches = [p.search(haystack) for p in patterns]
            if not all(matches):
                continue
            raw_lines = body.split('\n')
            # Locate the first query token while preserving original line numbers.
            token = patterns[0]
            offset = next((i for i, line in enumerate(raw_lines)
                           if token.search(line if args.regex else normalized(line))), 0)
            excerpt = '\n'.join(raw_lines[max(0, offset - 2):offset + 7]).strip()
            found.append({'path': entry['path'], 'kind': entry['kind'],
                          'page': page, 'line': start + offset,
                          'original': entry.get('original'),
                          'excerpt': excerpt,
                          'score': sum(len(p.findall(haystack)) for p in patterns)})
    order = {kind: i for i, kind in enumerate(KINDS)}
    found.sort(key=lambda h: (order[h['kind']], -h['score'], h['path'], h['line']))
    # Overlapping windows must not repeat the same line in the result.
    unique = list({(h['path'], h['page'], h['line']): h for h in found}.values())[:args.limit]
    if args.json:
        print(json.dumps(unique, ensure_ascii=False, indent=2))
    else:
        for hit in unique:
            locator = f"PDF стр. {hit['page']}; " if hit['page'] else ''
            print(f"\n{hit['path']} | {hit['kind']} | {locator}строка {hit['line']}")
            print(hit['excerpt'])
        if not unique:
            print('Совпадений нет. Это не доказывает отсутствие правила: проверьте формулировки и схемы PDF.')
    return 0 if unique else 1


def show(root: Path, args) -> None:
    path = (root / args.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError('Укажите существующий файл внутри репозитория.')
    if path.suffix.lower() not in TEXT_SUFFIXES:
        raise ValueError('Для бинарного оригинала используйте просмотрщик; здесь укажите .txt или .md.')
    text = read(path)
    if args.page is not None:
        pages = text.split('\f')
        if '\f' not in text or not 1 <= args.page <= len(pages) or not pages[args.page - 1].strip():
            raise ValueError('Такой текстовой страницы нет. Откройте оригинал PDF.')
        print(f'{args.path} | PDF стр. {args.page}\n{pages[args.page - 1]}')
    else:
        for i, line in enumerate(text.split('\n')[args.start - 1:args.start - 1 + args.count], args.start):
            print(f'{i}: {line}')


def check(root: Path) -> int:
    stored = load_catalog(root)
    current = inventory(root)
    errors = []
    if stored != current:
        errors.append('Каталог устарел: выполните course.py catalog и проверьте изменения источников.')
    profile = json.loads(read(root / 'knowledge/project-profile.json'))
    if profile['status'] == 'unconfirmed' and profile['topic'] is not None:
        errors.append('Неподтверждённый профиль не должен подставлять тему чужой команды.')
    rules = json.loads(read(root / 'knowledge/rules.json'))
    ids = set()
    for rule in rules:
        if rule['id'] in ids:
            errors.append(f"Повтор правила: {rule['id']}")
        ids.add(rule['id'])
        for evidence in rule['evidence']:
            path = root / evidence['path']
            if not path.is_file():
                errors.append(f"Нет источника: {evidence['path']}")
                continue
            text = read(path)
            if evidence.get('source_sha256') != fingerprint(path)[1]:
                errors.append(f"Источник правила {rule['id']} изменён или хеш основания не записан; перепроверьте правило.")
            if 'page' in evidence:
                pages = text.split('\f')
                if '\f' not in text or not 1 <= evidence['page'] <= len(pages) or not pages[evidence['page'] - 1].strip():
                    errors.append(f"Нет страницы в правиле {rule['id']}")
            elif not 1 <= evidence['line'] <= len(text.split('\n')):
                errors.append(f"Нет строки в правиле {rule['id']}")
    for error in errors:
        print(error, file=sys.stderr)
    if not errors:
        print(f'Каталог актуален; ссылки {len(rules)} правил существуют. Это не семантическая проверка правил.')
    return bool(errors)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('Ожидается целое число >= 1.')
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', help='Путь к guide_tpks; по умолчанию определяется по положению скрипта.')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('catalog', help='Обновить knowledge/sources.json и sources.md.')
    sub.add_parser('check', help='Проверить актуальность каталога и адреса доказательств.')
    lookup = sub.add_parser('search', help='Найти все слова запроса в странице или окне текста.')
    lookup.add_argument('query')
    lookup.add_argument('--kind', choices=KINDS)
    lookup.add_argument('--limit', type=positive, default=6)
    lookup.add_argument('--regex', action='store_true')
    lookup.add_argument('--json', action='store_true')
    view = sub.add_parser('show', help='Показать страницу PDF-текста или строки.')
    view.add_argument('path')
    view.add_argument('--page', type=positive)
    view.add_argument('--start', type=positive, default=1)
    view.add_argument('--count', type=positive, default=40)
    args = parser.parse_args()
    try:
        root = root_dir(args.root)
        if args.command == 'catalog':
            catalog(root)
        elif args.command == 'check':
            return int(check(root))
        elif args.command == 'search':
            return search(root, args)
        elif args.command == 'show':
            show(root, args)
        return 0
    except (ValueError, OSError, KeyError, re.error) as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
