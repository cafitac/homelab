"""backup.py 시험 — python3 -I -m unittest apps/trading/scripts/test_backup.py (표준 라이브러리만)."""

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402


def make_wal_db(path: Path, rows: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t (a INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    return conn


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.dst = root / "src", root / "dst"
        self.src.mkdir()
        self.dst.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_copy_is_consistent_while_a_writer_holds_the_database_open(self) -> None:
        writer = make_wal_db(self.src / "lab.sqlite", 50)  # 열린 채 WAL 에 커밋이 남아 있다
        writer.execute("INSERT INTO t VALUES (999)")
        writer.commit()
        before = (sha(self.src / "lab.sqlite"), sha(self.src / "lab.sqlite-wal"))
        self.assertEqual(backup.run(self.src, self.dst, 14, 3), 0)
        files = sorted(p.name for p in self.dst.iterdir())
        self.assertEqual(len(files), 1)
        copy = sqlite3.connect(self.dst / files[0])
        self.assertEqual(copy.execute("SELECT count(*) FROM t").fetchone()[0], 51)  # WAL 안의 커밋 포함
        self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(copy.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        copy.close()
        self.assertEqual(before, (sha(self.src / "lab.sqlite"), sha(self.src / "lab.sqlite-wal")))  # 원본 불변
        writer.execute("INSERT INTO t VALUES (1000)")  # 백업 뒤에도 쓰는 쪽이 막히지 않는다
        writer.commit()
        writer.close()

    def test_source_is_never_written_and_no_part_files_remain(self) -> None:
        make_wal_db(self.src / "lab.sqlite", 5).close()
        make_wal_db(self.src / "market_data.sqlite", 5).close()
        (self.src / "reports").mkdir()
        (self.src / "reports" / "a.html").write_text("x")
        snapshot = {p.name: sha(p) for p in self.src.rglob("*") if p.is_file()}
        self.assertEqual(backup.run(self.src, self.dst, 14, 3), 0)
        after = {p.name: sha(p) for p in self.src.rglob("*") if p.is_file()}
        for name, digest in snapshot.items():
            self.assertEqual(after[name], digest)  # 읽기 연결이 만든 -wal/-shm 은 새 파일일 뿐 기존 파일은 그대로
        names = sorted(p.name.split("-")[0] for p in self.dst.iterdir())
        self.assertEqual(names, ["lab", "market_data", "reports"])
        self.assertFalse([p for p in self.dst.iterdir() if p.name.endswith(".part")])

    def test_missing_sources_are_skipped_not_failed(self) -> None:
        self.assertEqual(backup.run(self.src, self.dst, 14, 3), 0)
        self.assertEqual(list(self.dst.iterdir()), [])

    def test_corrupt_source_fails_and_keeps_old_backups(self) -> None:
        (self.src / "lab.sqlite").write_bytes(b"not a database" * 100)
        old = self.dst / "lab-20200101T000000Z.sqlite"
        old.write_bytes(b"old")
        os.utime(old, (0, 0))
        self.assertEqual(backup.run(self.src, self.dst, 14, 0), 1)
        self.assertTrue(old.exists())  # 실패한 날은 지우지 않는다
        self.assertEqual([p.name for p in self.dst.iterdir()], [old.name])

    def test_prune_removes_old_but_keeps_newest_per_kind(self) -> None:
        now = time.time()
        names = [f"lab-2026100{d}T040000Z.sqlite" for d in range(1, 6)] + ["reports-20261001T040000Z.tar.gz", "unrelated.txt"]
        for n in names:
            p = self.dst / n
            p.write_bytes(b"x")
            os.utime(p, (now - 30 * 86400, now - 30 * 86400))
        for leftover in ("lab-x.sqlite.part", "lab-x.sqlite.part-journal"):
            part = self.dst / leftover
            part.write_bytes(b"x")
            os.utime(part, (now - 2 * 86400, now - 2 * 86400))
        backup.prune(self.dst, 14, 3, now)
        left = sorted(p.name for p in self.dst.iterdir())
        self.assertEqual(
            left,
            ["lab-20261003T040000Z.sqlite", "lab-20261004T040000Z.sqlite", "lab-20261005T040000Z.sqlite",
             "reports-20261001T040000Z.tar.gz", "unrelated.txt"],
        )

    def test_recent_files_are_kept(self) -> None:
        now = time.time()
        for d in range(5):
            p = self.dst / f"lab-2026100{d + 1}T040000Z.sqlite"
            p.write_bytes(b"x")
            os.utime(p, (now - d * 86400, now - d * 86400))
        self.assertEqual(backup.prune(self.dst, 14, 1, now), [])


if __name__ == "__main__":
    unittest.main()
