"""Regression checks for provenance, page boundaries, and source drift."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / '.agents/skills/tpks-kozyrev/scripts/course.py'
spec = importlib.util.spec_from_file_location('course', SCRIPT)
course = importlib.util.module_from_spec(spec)
spec.loader.exec_module(course)


class CourseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder in ['materials/kozyrev_slides', 'notes', 'knowledge']:
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.source = self.root / 'materials/kozyrev_slides/test.pdf.txt'
        self.source.write_text('Первая страница\nБазовая\nверсия согласована\n\fВторая страница\nОтдельная схема\n\f', encoding='utf-8')
        (self.root / 'notes/example.md').write_text('Конспект: ёмкость системы\n', encoding='utf-8')
        (self.root / 'knowledge/project-profile.json').write_text(
            json.dumps({'status': 'unconfirmed', 'topic': None}), encoding='utf-8')
        (self.root / 'knowledge/rules.json').write_text('[]', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()):
            course.catalog(self.root)

    def lookup(self, query, **overrides):
        options = dict(query=query, regex=False, kind='slides', limit=10, json=True)
        options.update(overrides)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = course.search(self.root, SimpleNamespace(**options))
        return code, json.loads(out.getvalue())

    def test_multiline_query_keeps_pdf_page_and_physical_line(self):
        code, hits = self.lookup('базовая версия')
        self.assertEqual(code, 0)
        self.assertEqual((hits[0]['page'], hits[0]['line']), (1, 2))
        self.assertEqual(hits[0]['kind'], 'slides')

    def test_query_cannot_cross_page_boundary(self):
        code, hits = self.lookup('согласована отдельная')
        self.assertEqual((code, hits), (1, []))

    def test_second_page_line_number_does_not_count_formfeed_as_newline(self):
        _, hits = self.lookup('отдельная')
        self.assertEqual((hits[0]['page'], hits[0]['line']), (2, 5))

    def test_yo_and_case_and_kind_filter(self):
        _, hits = self.lookup('ЕМКОСТЬ', kind='analysis')
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['path'], 'notes/example.md')

    def test_source_changes_require_rebuild(self):
        self.source.write_text('Новый текст\n', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(course.check(self.root))

    def test_rebuilding_catalog_does_not_reapprove_old_rule(self):
        rule = [{'id': 'R1', 'evidence': [{'path': self.source.relative_to(self.root).as_posix(),
                  'page': 1, 'source_sha256': course.fingerprint(self.source)[1]}]}]
        (self.root / 'knowledge/rules.json').write_text(json.dumps(rule), encoding='utf-8')
        self.source.write_text('Изменённый источник\n\f', encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            course.catalog(self.root)
            self.assertTrue(course.check(self.root))

    def test_catalog_is_portable_across_newline_conversions(self):
        before = course.inventory(self.root)
        self.source.write_bytes(self.source.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))
        self.assertEqual(course.inventory(self.root), before)

    def test_show_rejects_reading_outside_repository(self):
        with self.assertRaises(ValueError):
            course.show(self.root, SimpleNamespace(path='../outside.txt', page=None, start=1, count=10))

    def test_invalid_regex_is_detected(self):
        result = subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.root),
                                 'search', '[', '--regex'], capture_output=True, encoding='utf-8')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Ошибка:', result.stderr)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
