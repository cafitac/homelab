"""trading 백업 — lab.sqlite · market_data.sqlite 를 SQLite backup API 로 일관되게 복사하고 reports/ 를 묶어 둔다.

trading-lab 이미지(표준 라이브러리만 사용)에서 CronJob `backup` 이 돌린다. 설계 05 UC5 · 07.
  * 원본은 file:...?mode=ro 로만 연다 — 쓰기 자리(tradelab-writer)를 잡지 않고 원본에 쓰지 않는다.
    WAL 읽기라 쓰는 쪽의 커밋을 막지 않고, 복사본은 한 시점의 일관된 스냅샷이다(backup API 를 한 번에 끝까지 돈다).
  * 복사본은 <이름>.part 로 만들어 integrity_check 를 통과한 뒤에만 이름을 붙인다. 깨진 파일이 최신 백업 행세를 하지 않게
  * 복사본은 journal_mode=DELETE 로 바꿔 파일 하나로 닫는다(되살릴 때 -wal 이 따라다니지 않게)
  * 이번 실행이 모두 성공한 뒤에만 오래된 파일을 지운다. 종류마다 최신 KEEP_MIN 개는 나이와 상관없이 남긴다

환경변수: BACKUP_SRC(/home/app/.tradelab), BACKUP_DST(/backups), BACKUP_KEEP_DAYS(14), BACKUP_KEEP_MIN(3)
종료 코드: 0 성공(원본이 아직 없어 건너뛴 것만 있어도 0), 1 하나라도 실패.
"""

import os
import re
import sqlite3
import sys
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path

DATABASES = ("lab", "market_data")
KINDS = {"lab": ".sqlite", "market_data": ".sqlite", "reports": ".tar.gz"}
NAME = re.compile(r"^(?P<kind>lab|market_data|reports)-(?P<ts>\d{8}T\d{6}Z)(?P<ext>\.sqlite|\.tar\.gz)$")
STALE_PART_SECONDS = 24 * 3600


def backup_database(source: Path, target: Path) -> None:
    """source 를 읽기 전용으로 열어 target 에 한 시점의 복사본을 만든다(target 은 새 파일)."""
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        src.execute("PRAGMA query_only = ON")
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)  # pages=-1: 한 번에 끝까지 — 하나의 읽기 스냅샷
            dst.execute("PRAGMA journal_mode = DELETE")
            result = dst.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise RuntimeError(f"integrity_check: {result[:3]}")
            if dst.execute("PRAGMA user_version").fetchone() != src.execute("PRAGMA user_version").fetchone():
                raise RuntimeError("user_version differs between source and copy")
        finally:
            dst.close()
    finally:
        src.close()


def backup_reports(source: Path, target: Path) -> None:
    with tarfile.open(target, "w:gz") as tar:
        tar.add(source, arcname="reports")
    with tarfile.open(target, "r:gz") as tar:  # 끝까지 읽혀야 온전하다
        for _ in tar:
            pass


def publish(part: Path, final: Path) -> None:
    fd = os.open(part, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, final)


# 중간에 죽은 복사가 남기는 파일: .part 와 그 옆의 rollback journal(-journal)·WAL·SHM
STALE_SUFFIXES = (".part", ".part-journal", ".part-wal", ".part-shm")


def prune(dst: Path, keep_days: int, keep_min: int, now: float) -> list[Path]:
    removed: list[Path] = []
    by_kind: dict[str, list[Path]] = {kind: [] for kind in KINDS}
    for path in dst.iterdir():
        match = NAME.match(path.name)
        if match and path.is_file():
            by_kind[match["kind"]].append(path)
        elif path.name.endswith(STALE_SUFFIXES) and path.is_file() and now - path.stat().st_mtime > STALE_PART_SECONDS:
            path.unlink()
            removed.append(path)
    for files in by_kind.values():
        files.sort(key=lambda p: p.name, reverse=True)  # 이름의 타임스탬프가 곧 시각
        for path in files[keep_min:]:
            if now - path.stat().st_mtime > keep_days * 86400:
                path.unlink()
                removed.append(path)
    return removed


def run(src: Path, dst: Path, keep_days: int, keep_min: int, now: float | None = None) -> int:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    failures = 0
    made: list[Path] = []
    for name in DATABASES:
        source = src / f"{name}.sqlite"
        if not source.is_file():
            print(f"건너뜀 {name}: {source} 가 아직 없다")
            continue
        part = dst / f"{name}-{stamp}.sqlite.part"
        try:
            part.unlink(missing_ok=True)
            backup_database(source, part)
            final = dst / f"{name}-{stamp}.sqlite"
            publish(part, final)
            made.append(final)
        except Exception as error:  # noqa: BLE001 — 하나가 실패해도 나머지는 계속, 끝에 종료 코드로 알린다
            failures += 1
            part.unlink(missing_ok=True)
            print(f"실패 {name}: {type(error).__name__}: {error}", file=sys.stderr)
    reports = src / "reports"
    if reports.is_dir():
        part = dst / f"reports-{stamp}.tar.gz.part"
        try:
            part.unlink(missing_ok=True)
            backup_reports(reports, part)
            final = dst / f"reports-{stamp}.tar.gz"
            publish(part, final)
            made.append(final)
        except Exception as error:  # noqa: BLE001
            failures += 1
            part.unlink(missing_ok=True)
            print(f"실패 reports: {type(error).__name__}: {error}", file=sys.stderr)
    for path in made:
        print(f"백업 {path.name} ({path.stat().st_size} bytes)")
    if failures:
        print(f"{failures} 건 실패 — 오래된 백업은 지우지 않았다", file=sys.stderr)
        return 1
    for path in prune(dst, keep_days, keep_min, time.time() if now is None else now):
        print(f"삭제 {path.name}")
    return 0


def main() -> int:
    src = Path(os.environ.get("BACKUP_SRC", "/home/app/.tradelab"))
    dst = Path(os.environ.get("BACKUP_DST", "/backups"))
    return run(src, dst, int(os.environ.get("BACKUP_KEEP_DAYS", "14")), int(os.environ.get("BACKUP_KEEP_MIN", "3")))


if __name__ == "__main__":
    sys.exit(main())
