import os
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import backup


NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.out = Path(self.tmp.name) / 'out'
        self.data.mkdir()
        self.out.mkdir()
        self.now = NOW

    def database(self):
        conn = sqlite3.connect(self.data / 'skills.db')
        self.addCleanup(conn.close)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE sample (value INTEGER)')
        conn.execute('INSERT INTO sample VALUES (7)')
        conn.commit()
        return conn

    def test_online_copy_includes_uncheckpointed_wal(self):
        # given
        writer = self.database()
        writer.execute('INSERT INTO sample VALUES (8)')
        writer.commit()
        # when
        result = backup.backup(self.data, self.out, 7, NOW)
        copy = sqlite3.connect(result)
        self.addCleanup(copy.close)
        # then
        self.assertEqual(copy.execute('SELECT * FROM sample').fetchall(), [(7,), (8,)])
        self.assertEqual(copy.execute('PRAGMA integrity_check').fetchone(), ('ok',))
        self.assertEqual(copy.execute('PRAGMA journal_mode').fetchone(), ('delete',))
        self.assertEqual(list(self.out.glob('*.part')), [])

    def test_retention_preserves_other_names_and_new_backup_at_zero_days(self):
        # given
        self.database()
        old = self.out / 'skills-old.dump'
        fresh = self.out / 'skills-fresh.dump'
        other = self.out / 'other.dump'
        old.write_text('old')
        fresh.write_text('fresh')
        other.write_text('other')
        (self.out / 'left.part').write_text('partial')
        os.utime(old, (self.now.timestamp() - 86400, self.now.timestamp() - 86400))
        os.utime(other, (self.now.timestamp() - 86400, self.now.timestamp() - 86400))
        os.utime(fresh, (self.now.timestamp() + 60, self.now.timestamp() + 60))
        # when
        result = backup.backup(self.data, self.out, 0, NOW)
        # then
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())
        self.assertTrue(result.exists())
        self.assertEqual(list(self.out.glob('*.part')), [])

    def test_missing_database_fails_without_output(self):
        # given
        args = ['--data', str(self.data), '--out', str(self.out)]
        # when
        result = backup.main(args)
        # then
        self.assertEqual(result, 1)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_same_second_replaces_complete_backup(self):
        # given
        writer = self.database()
        first = backup.backup(self.data, self.out, 7, NOW)
        writer.execute('INSERT INTO sample VALUES (9)')
        writer.commit()
        # when
        second = backup.backup(self.data, self.out, 7, NOW)
        copy = sqlite3.connect(second)
        self.addCleanup(copy.close)
        # then
        self.assertEqual(first, second)
        self.assertEqual(copy.execute('SELECT * FROM sample').fetchall(), [(7,), (9,)])
        self.assertEqual(list(self.out.iterdir()), [second])
