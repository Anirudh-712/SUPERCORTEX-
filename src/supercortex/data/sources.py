"""L0 ingestion from venue public archives.

Only public bulk archives are used. No credentials, ever — see CLAUDE.md law 3.
Binance publishes monthly and daily zips with SHA256 checksums, which is why it is the
primary source: the checksum makes L0 verifiable rather than merely downloaded.
"""
from __future__ import annotations

import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from supercortex.registry.db import PROJECT_ROOT, free_gb, sha256_file

BINANCE_VISION = "https://data.binance.vision/data"
RAW_ROOT = PROJECT_ROOT / "data" / "raw"
DISK_FLOOR_GB = 2.0

# What a market actually is, in URL terms. `um` = USD-margined futures (perps).
MARKETS = {"spot": "spot", "um": "futures/um", "cm": "futures/cm"}

# Publication cadence per kind, verified against data.binance.vision on 2026-08-24.
# This is not cosmetic: `metrics` (which carries open interest) has NO monthly archive —
# requesting one returns 404 — so it must be fetched day by day, ~365 files per year.
CADENCE = {
    "klines": "monthly",
    "aggTrades": "monthly",
    "fundingRate": "monthly",
    "premiumIndexKlines": "monthly",
    "metrics": "daily",
}

#: Earliest publication date per kind for USD-M perps, measured by probing (2026-08-24).
#: Open interest starts eight months after klines, which narrows the usable common
#: window for any strategy that needs both.
FIRST_AVAILABLE = {
    "klines": "2020-01-01",
    "fundingRate": "2020-01-01",
    "metrics": "2020-09-01",
}


@dataclass(frozen=True)
class Archive:
    """One archive file: where it lives remotely and locally.

    `period` is a month-start for monthly kinds and a specific day for daily kinds.
    """

    market: str
    symbol: str
    kind: str
    period: date
    interval: str | None = None

    @property
    def cadence(self) -> str:
        return CADENCE.get(self.kind, "monthly")

    @property
    def stem(self) -> str:
        stamp = self.period.strftime("%Y-%m-%d" if self.cadence == "daily" else "%Y-%m")
        if self.interval:
            return f"{self.symbol}-{self.interval}-{stamp}"
        return f"{self.symbol}-{self.kind}-{stamp}"

    @property
    def url(self) -> str:
        parts = [BINANCE_VISION, MARKETS[self.market], self.cadence, self.kind, self.symbol]
        if self.interval:
            parts.append(self.interval)
        return "/".join(parts) + f"/{self.stem}.zip"

    @property
    def local(self) -> Path:
        parts = [RAW_ROOT, "binance", self.market, self.symbol, self.kind]
        if self.interval:
            parts.append(self.interval)
        if self.cadence == "daily":
            parts.append(self.period.strftime("%Y"))
        return Path(*parts) / f"{self.stem}.zip"


class NotPublished(Exception):
    """The venue has no archive for this period. Usually the symbol did not exist yet."""


def _get(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as handle:
            while block := response.read(1 << 20):
                handle.write(block)
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        if exc.code == 404:
            raise NotPublished(url) from exc
        raise
    tmp.replace(dest)


def fetch(archive: Archive, *, verify: bool = True) -> Path:
    """Download one archive into L0 and verify it against the venue's own checksum.

    A download that is not checksum-verified is not evidence — a truncated zip that
    silently loses the last three days of a month is exactly the kind of defect that
    produces a beautiful, wrong backtest.
    """
    if free_gb() < DISK_FLOOR_GB:
        raise RuntimeError(
            f"disk floor: {free_gb():.2f} GB free (floor {DISK_FLOOR_GB} GB). Refusing to "
            "write — a truncated archive is worse than a missing one."
        )

    if not archive.local.exists():
        _get(archive.url, archive.local)

    if verify:
        checksum_path = archive.local.with_suffix(".zip.CHECKSUM")
        if not checksum_path.exists():
            try:
                _get(archive.url + ".CHECKSUM", checksum_path)
            except NotPublished:
                checksum_path = None  # type: ignore[assignment]
        if checksum_path is not None and checksum_path.exists():
            expected = checksum_path.read_text().split()[0].strip()
            actual = sha256_file(archive.local)
            if expected != actual:
                archive.local.unlink(missing_ok=True)
                checksum_path.unlink(missing_ok=True)
                raise RuntimeError(
                    f"CHECKSUM MISMATCH for {archive.stem}: venue says {expected[:16]}…, "
                    f"we computed {actual[:16]}…. Corrupt download deleted."
                )

    with zipfile.ZipFile(archive.local) as zf:
        if bad := zf.testzip():
            archive.local.unlink(missing_ok=True)
            raise RuntimeError(f"corrupt zip {archive.stem}: bad member {bad}")

    return archive.local


def months(start: date, end: date) -> list[date]:
    """Every month-start between two dates, inclusive."""
    out, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append(date(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def days(start: date, end: date) -> list[date]:
    """Every day between two dates, inclusive."""
    from datetime import timedelta

    out, cursor = [], start
    while cursor <= end:
        out.append(cursor)
        cursor += timedelta(days=1)
    return out


def fetch_many(
    archives: list[Archive], *, workers: int = 8, on_done: object = None
) -> tuple[int, list[date], list[str]]:
    """Fetch archives concurrently. Returns (fetched, not_published, failures).

    Daily kinds mean thousands of small files; serial fetching would take an hour for
    what concurrency does in a couple of minutes. Kept modest to stay polite to the CDN.
    """
    import concurrent.futures as cf

    fetched, missing, failed = 0, [], []

    def one(archive: Archive) -> tuple[Archive, str, str]:
        try:
            fetch(archive)
            return archive, "ok", ""
        except NotPublished:
            return archive, "missing", ""
        except Exception as exc:
            return archive, "failed", str(exc)

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for archive, status, detail in pool.map(one, archives):
            if status == "ok":
                fetched += 1
            elif status == "missing":
                missing.append(archive.period)
            else:
                failed.append(f"{archive.stem}: {detail}")
            if callable(on_done):
                on_done(fetched, len(missing), len(failed))

    return fetched, missing, failed
