"""Admission of a human-captured venue API response bundle into immutable L0.

The desk's bulk archives are checksum-verified against a digest the venue itself
publishes, which is what makes them evidence rather than a download. An API response has
no such published digest. A capture is therefore *weaker evidence by construction*, and
this module's job is to be precise about how much weaker.

What a capture proves
---------------------
`SHA256SUMS` was produced on the operator's machine, from the same bytes, at the same
moment as the response. It detects truncation and post-capture corruption. It is
**self-referential**: it cannot establish that the venue sent those bytes, and no check in
this module can. Every row that reaches L1 from here carries `VENUE_API_CAPTURE` and
keeps it — see `settlement.py` for what corroboration against a checksum-authenticated
archive does and does not add.

What this module never does
---------------------------
Fetch. There is no network client here and there must never be one: the operator carries
the bytes across, and the desk's WebFetch denies, Bash venue guard and sandbox deny-list
all exist to keep it that way. `urllib.parse` is used for URL *string* parsing only; the
network half of urllib is absent and a test asserts it stays absent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

#: Every row imported through here is this, permanently. Corroboration never upgrades it.
PROVENANCE = "VENUE_API_CAPTURE"

#: There is nothing to corroborate a mark price against: the checksum-authenticated
#: funding archive carries no mark price. Stated as a value so it appears in the stored
#: metadata rather than living only in prose.
MARK_PRICE_CORROBORATION = "NONE"

#: The bundle is indivisible. A capture missing any one of these is not a capture.
EXPECTED_FILES = ("body.json", "headers.txt", "meta.txt", "SHA256SUMS")

#: What `SHA256SUMS` must account for — itself excluded, since a manifest cannot digest
#: itself. This asymmetry is the reason the manifest is an integrity check and not proof.
MANIFEST_FILES = ("body.json", "headers.txt", "meta.txt")

#: Written by us, alongside the operator's files, never merged into them.
IMPORT_METADATA = "import.json"

#: The files admission reads as text. `body.json` is absent deliberately: it is parsed as
#: JSON downstream, where `json.loads` on bytes enforces UTF-8 strictly of its own accord.
TEXT_FILES = ("SHA256SUMS", "headers.txt", "meta.txt")

EXPECTED_SCHEME = "https"
EXPECTED_HOST = "fapi.binance.com"
EXPECTED_PATH = "/fapi/v1/fundingRate"

#: The public endpoint takes these and nothing else. An unrecognised parameter means the
#: request was not the one this importer knows how to reason about.
ALLOWED_QUERY = frozenset({"symbol", "limit", "startTime", "endTime"})

EXPECTED_META_KEYS = ("url", "captured_at", "curl")

#: This endpoint is the USD-margined futures API, so a capture stored under any other
#: market label would be mislabelled at rest. One supported market, checked below the CLI
#: so a direct library call cannot route around it.
SUPPORTED_MARKETS = ("um",)

#: A symbol becomes a directory name. It must therefore be exactly one safe path
#: component: no separators, no dots (so no `.` or `..`), no control characters, not empty.
SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9_]{1,32}$")

#: The trust position, written into every `import.json` and required to still be there.
#: Part of the verified payload precisely so it cannot be quietly deleted from stored
#: evidence.
TRUST_NOTE = (
    "The SHA-256 digests fix these bytes as received on the operator's machine. They do "
    "not authenticate the publisher: the venue publishes no digest for an API response, "
    "unlike the bulk archives. Provenance stays VENUE_API_CAPTURE."
)

#: Anything shaped like a credential is a refusal wherever it appears. This capture path
#: requires no credential, so the presence of one means something other than the intended
#: public request was performed.
CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|apikey|secret|signature|passwd|password|authorization|bearer"
    r"|x-mbx-apikey|access[_-]?token)\b"
)

_MANIFEST_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+(\S.*)$")
_STATUS_LINE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})")
_UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class CaptureRefused(ValueError):
    """The bundle is not admissible. Nothing is written when this is raised."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_text(data: bytes, name: str) -> str:
    """Strict UTF-8, or a refusal naming the file.

    `errors="replace"` would substitute U+FFFD for an undecodable byte and carry on. On an
    evidence file that is not leniency, it is quiet repair: the admitted facts would then
    describe bytes nobody sent, and every digest would still agree because the manifest was
    computed over the damaged file. Evidence is never silently repaired.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptureRefused(
            f"{name} is not valid UTF-8 (byte {exc.object[exc.start]:#04x} at offset "
            f"{exc.start}). A capture file that cannot be decoded is not repaired here."
        ) from exc


# ── manifest ────────────────────────────────────────────────────────────────────
def parse_manifest(text: str) -> dict[str, str]:
    """Digests keyed by expected basename.

    The manifest in a real capture holds absolute paths from the operator's machine.
    Those paths are *labels*, not instructions: this parser keys on basename and the
    caller reads from the bundle directory. Following a path out of a manifest would let
    a crafted bundle point the importer at any file on disk.
    """
    digests: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _MANIFEST_LINE.match(line)
        if not match:
            raise CaptureRefused(
                f"malformed SHA256SUMS line (expected '<64 hex>  <path>'): {raw_line!r}"
            )
        digest, path = match.group(1).lower(), match.group(2).strip()

        if ".." in Path(path).parts or ".." in path.split("/"):
            raise CaptureRefused(f"path traversal in SHA256SUMS entry: {path!r}")

        name = path.rsplit("/", 1)[-1]
        if name not in MANIFEST_FILES:
            raise CaptureRefused(
                f"unexpected SHA256SUMS entry {name!r}; the manifest must cover exactly "
                f"{', '.join(MANIFEST_FILES)}"
            )
        if name in digests:
            raise CaptureRefused(f"duplicate SHA256SUMS entry for {name}")
        digests[name] = digest

    if missing := [n for n in MANIFEST_FILES if n not in digests]:
        raise CaptureRefused(f"SHA256SUMS has no digest for {', '.join(missing)}")
    return digests


# ── response headers ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class HeaderFacts:
    status_code: int
    content_type: str
    content_length: int | None
    server_date: str | None


def parse_headers(text: str) -> HeaderFacts:
    """The **final** response block: what we actually received.

    `curl -D` appends a block per response, so a redirect chain leaves earlier blocks in
    the file. Reading the first would let a 200 that preceded a 500 vouch for a failure.
    """
    blocks = [b for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if b.strip()]
    if not blocks:
        raise CaptureRefused("headers.txt is empty: no status line to check")

    lines = blocks[-1].strip().splitlines()
    status_match = _STATUS_LINE.match(lines[0].strip())
    if not status_match:
        raise CaptureRefused(f"no HTTP status line in the final header block: {lines[0]!r}")

    status = int(status_match.group(1))
    if not 200 <= status < 300:
        raise CaptureRefused(f"capture records an unsuccessful response: HTTP {status}")

    fields: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            fields.setdefault(key.strip().lower(), value.strip())

    content_type = fields.get("content-type", "")
    if "json" not in content_type.lower():
        raise CaptureRefused(
            f"capture content type is {content_type!r}, expected JSON"
        )

    length: int | None = None
    if raw_length := fields.get("content-length"):
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise CaptureRefused(f"malformed content-length {raw_length!r}") from exc

    return HeaderFacts(status, content_type, length, fields.get("date"))


# ── operator metadata ───────────────────────────────────────────────────────────
def parse_meta(text: str) -> dict[str, str]:
    """`meta.txt` as written by the capture command, parsed fail-closed."""
    if found := CREDENTIAL_PATTERN.search(text):
        raise CaptureRefused(
            f"meta.txt carries credential-shaped material ({found.group(0)!r}). This "
            f"capture path is public and requires none."
        )

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise CaptureRefused(f"malformed meta.txt line (expected key=value): {line!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in EXPECTED_META_KEYS:
            raise CaptureRefused(
                f"unknown meta.txt key {key!r}; expected exactly "
                f"{', '.join(EXPECTED_META_KEYS)}"
            )
        if key in values:
            raise CaptureRefused(f"duplicate meta.txt key {key!r}: each must appear once")
        values[key] = value.strip()

    if missing := [k for k in EXPECTED_META_KEYS if k not in values]:
        raise CaptureRefused(f"meta.txt is missing {', '.join(missing)}")

    stamp = values["captured_at"]
    if not _UTC_STAMP.match(stamp):
        raise CaptureRefused(
            f"captured_at {stamp!r} is not a UTC timestamp of the form "
            f"YYYY-MM-DDTHH:MM:SSZ"
        )
    try:
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CaptureRefused(f"captured_at {stamp!r} is not a valid timestamp") from exc

    return values


# ── URL ─────────────────────────────────────────────────────────────────────────
def validate_url(url: str, *, symbol: str) -> None:
    """Confirm the recorded request was the expected public endpoint. Purely textual."""
    parts = urlparse(url)
    if parts.scheme != EXPECTED_SCHEME:
        raise CaptureRefused(f"URL scheme is {parts.scheme!r}, expected https")
    if parts.netloc != EXPECTED_HOST:
        raise CaptureRefused(f"URL host is {parts.netloc!r}, expected {EXPECTED_HOST}")
    if parts.path != EXPECTED_PATH:
        raise CaptureRefused(f"URL path is {parts.path!r}, expected {EXPECTED_PATH}")

    query = parse_qs(parts.query, keep_blank_values=True)
    if found := [k for k in query if CREDENTIAL_PATTERN.search(k)]:
        raise CaptureRefused(f"credential-shaped query parameter(s): {', '.join(found)}")
    if unknown := [k for k in query if k not in ALLOWED_QUERY]:
        raise CaptureRefused(
            f"query parameter(s) not allowed: {', '.join(sorted(unknown))}"
        )
    if query.get("symbol", []) != [symbol]:
        raise CaptureRefused(
            f"URL symbol is {query.get('symbol', ['<absent>'])} but this import is for "
            f"{symbol}"
        )


# ── admission ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CaptureFacts:
    """Everything established about a bundle without trusting anything it asserts."""

    symbol: str
    body_sha256: str
    headers_sha256: str
    meta_sha256: str
    #: The manifest cannot digest itself, so we digest it here.
    #: Without this, reordering SHA256SUMS leaves no trace.
    manifest_sha256: str
    body_bytes: int
    url: str
    #: The operator's clock, from meta.txt.
    captured_at_local: str
    #: The venue's clock, from the response `date` header. Kept apart from the local
    #: stamp deliberately: neither authenticates the other and agreement is coincidence.
    server_date: str | None
    status_code: int
    content_type: str
    content_length: int | None
    curl_version: str
    provenance: str = PROVENANCE
    mark_price_corroboration: str = MARK_PRICE_CORROBORATION

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _read_bundle(directory: Path, names: tuple[str, ...]) -> dict[str, bytes]:
    """Read every named file **once**, into memory.

    One read, one snapshot. Verifying from disk and then reading again for consumption
    leaves a window in which the bytes can change between the two, which is exactly how a
    forged mark price reached L1 carrying the previously verified capture's hash.
    """
    if not directory.is_dir():
        raise CaptureRefused(f"capture directory does not exist: {directory}")
    files: dict[str, bytes] = {}
    for name in names:
        path = directory / name
        if not path.is_file():
            raise CaptureRefused(f"capture bundle is missing {name}")
        files[name] = path.read_bytes()
    return files


def admit_snapshot(files: Mapping[str, bytes], *, symbol: str) -> CaptureFacts:
    """Full admission against an in-memory snapshot. Touches no filesystem."""
    for name in EXPECTED_FILES:
        if name not in files:
            raise CaptureRefused(f"capture bundle is missing {name}")

    digests = parse_manifest(decode_text(files["SHA256SUMS"], "SHA256SUMS"))
    actual = {name: sha256_bytes(files[name]) for name in MANIFEST_FILES}
    for name in MANIFEST_FILES:
        if actual[name] != digests[name]:
            raise CaptureRefused(
                f"{name} does not match its SHA256SUMS digest "
                f"(manifest {digests[name][:16]}…, actual {actual[name][:16]}…)"
            )

    headers = parse_headers(decode_text(files["headers.txt"], "headers.txt"))
    body_bytes = len(files["body.json"])
    if headers.content_length is not None and headers.content_length != body_bytes:
        raise CaptureRefused(
            f"content-length {headers.content_length} does not match body.json "
            f"({body_bytes} bytes) — the capture is truncated or altered"
        )

    meta = parse_meta(decode_text(files["meta.txt"], "meta.txt"))
    validate_url(meta["url"], symbol=symbol)

    return CaptureFacts(
        symbol=symbol,
        body_sha256=actual["body.json"],
        headers_sha256=actual["headers.txt"],
        meta_sha256=actual["meta.txt"],
        manifest_sha256=sha256_bytes(files["SHA256SUMS"]),
        body_bytes=body_bytes,
        url=meta["url"],
        captured_at_local=meta["captured_at"],
        server_date=headers.server_date,
        status_code=headers.status_code,
        content_type=headers.content_type,
        content_length=headers.content_length,
        curl_version=meta["curl"],
    )


def admit(capture_dir: Path, *, symbol: str) -> CaptureFacts:
    """Verify a bundle end to end. Raises `CaptureRefused` rather than returning a verdict."""
    return admit_snapshot(_read_bundle(capture_dir, EXPECTED_FILES), symbol=symbol)


# ── generated metadata ──────────────────────────────────────────────────────────
def import_metadata_payload(facts: CaptureFacts) -> dict[str, object]:
    """The single definition of what `import.json` contains.

    Used by the writer *and* the verifier, so the two cannot drift. Verification demands
    equality with this whole object — not a subset — which is what stops an extra key or a
    quietly deleted trust note surviving inside stored evidence.
    """
    payload: dict[str, object] = dict(facts.as_dict())
    payload["note"] = TRUST_NOTE
    return payload


def _render_import_metadata(facts: CaptureFacts) -> bytes:
    return (
        json.dumps(import_metadata_payload(facts), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_import_metadata(staging: Path, facts: CaptureFacts) -> None:
    """Our metadata, in our own file. `meta.txt` belongs to the operator and is untouched."""
    (staging / IMPORT_METADATA).write_bytes(_render_import_metadata(facts))


# ── L0 storage ──────────────────────────────────────────────────────────────────
def validate_location(market: str, symbol: str) -> None:
    """A capture's path is built from these, so they are validated before anything else."""
    if market not in SUPPORTED_MARKETS:
        raise CaptureRefused(
            f"market {market!r} is not supported by this capability: the funding-rate "
            f"endpoint is USD-margined futures, so only {SUPPORTED_MARKETS} is valid."
        )
    if not SAFE_SYMBOL.match(symbol):
        raise CaptureRefused(
            f"symbol {symbol!r} is not a single safe path component. A symbol becomes a "
            f"directory name, so separators, dots, control characters and empty values "
            f"are refused."
        )


def capture_root(raw_root: Path, market: str, symbol: str) -> Path:
    """The directory every capture for one symbol lives under."""
    validate_location(market, symbol)
    return raw_root / "venue_api" / "binance" / market / symbol / "fundingRate"


def capture_dir(raw_root: Path, market: str, symbol: str, body_sha256: str) -> Path:
    """Where a capture lives, addressed by the content it holds.

    The **full** digest, not a prefix. A truncated address is a collision waiting to be
    someone else's problem, and it also weakens verification: the directory name is one of
    the independent facts a forged bundle has to reproduce.
    """
    return capture_root(raw_root, market, symbol) / body_sha256


# ── verification ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VerifiedCapture:
    """Verified facts and the exact bytes they were verified against.

    The body travels with the facts so a caller cannot re-read the file and consume
    something else. `settlement.build` parses `body` and never reopens `body.json`.
    """

    facts: CaptureFacts
    body: bytes


def _verify_snapshot(files: Mapping[str, bytes], expected_address: str) -> CaptureFacts:
    """Everything a stored capture must satisfy, judged from one snapshot.

    Four independent facts have to agree, and a forgery must reproduce all of them:

      1. full admission (manifest vs body/headers/meta, headers vs body length, URL)
      2. the content address, which is the digest of the admitted body
      3. `manifest_sha256`, closing the manifest's inability to digest itself
      4. `import.json`, equal to the payload this module would write for those facts
    """
    if IMPORT_METADATA not in files:
        raise CaptureRefused(
            f"stored capture has no {IMPORT_METADATA}: it was not written by this "
            f"importer, or the write did not complete"
        )
    try:
        recorded = json.loads(decode_text(files[IMPORT_METADATA], IMPORT_METADATA))
    except ValueError as exc:
        raise CaptureRefused(f"{IMPORT_METADATA} is not valid JSON: {exc}") from exc
    if not isinstance(recorded, dict) or not isinstance(recorded.get("symbol"), str):
        raise CaptureRefused(f"{IMPORT_METADATA} does not record a symbol")

    # The symbol comes from our own metadata, then admission re-derives everything from
    # the operator's bytes. A tampered symbol fails the URL check inside admission.
    facts = admit_snapshot(files, symbol=recorded["symbol"])

    if expected_address != facts.body_sha256:
        raise CaptureRefused(
            f"capture content address {expected_address!r} is not the digest of the body "
            f"it contains ({facts.body_sha256}). The content address and the content "
            f"disagree, so these bytes are not the capture that was admitted."
        )

    expected_payload = import_metadata_payload(facts)
    if recorded != expected_payload:
        missing = sorted(set(expected_payload) - set(recorded))
        extra = sorted(set(recorded) - set(expected_payload))
        changed = sorted(
            key for key in set(recorded) & set(expected_payload)
            if recorded[key] != expected_payload[key]
        )
        raise CaptureRefused(
            f"{IMPORT_METADATA} is not the metadata this importer would write for these "
            f"bytes — missing {missing}, unexpected {extra}, changed {changed}"
        )
    return facts


def read_verified_stored(stored: Path) -> VerifiedCapture:
    """Re-authenticate a stored capture and hand back the verified bytes with its facts."""
    files = _read_bundle(stored, (*EXPECTED_FILES, IMPORT_METADATA))
    return VerifiedCapture(_verify_snapshot(files, stored.name), files["body.json"])


def verify_stored(stored: Path) -> CaptureFacts:
    """Re-authenticate a stored capture. Its directory name is its content address."""
    return read_verified_stored(stored).facts


def _operator_files_match(bundle: Path, stored: Path) -> str | None:
    """The first of the four operator files that differs, or None. `SHA256SUMS` included.

    Comparing only body/headers/meta would let two captures with identical content but
    different manifests collapse into one stored directory, quietly discarding one of the
    operator's files.
    """
    for name in EXPECTED_FILES:
        if not (stored / name).is_file():
            return name
        if (bundle / name).read_bytes() != (stored / name).read_bytes():
            return name
    return None


def store(capture_bundle: Path, *, symbol: str, market: str, raw_root: Path) -> Path:
    """Admit a bundle and place it in L0 atomically. Returns the stored directory.

    Everything is verified and written in a staging directory beside the destination,
    then moved with a single `os.replace`. A crash therefore leaves either nothing or a
    complete capture — never a half-written final directory that would block the retry.
    """
    validate_location(market, symbol)
    facts = admit(capture_bundle, symbol=symbol)
    final = capture_dir(raw_root, market, symbol, facts.body_sha256)

    if final.exists():
        verify_stored(final)
        if differing := _operator_files_match(capture_bundle, final):
            raise CaptureRefused(
                f"a capture is already stored at {final.name} but its {differing} differs "
                f"from the bundle offered now. Same body, different operator files, so "
                f"this is not a re-import of the same capture."
            )
        return final          # identical capture, already stored and still intact

    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.parent / f".staging-{final.name}-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir()
        for name in EXPECTED_FILES:
            shutil.copy2(capture_bundle / name, staging / name)
        _write_import_metadata(staging, facts)

        # The whole staged bundle, re-admitted from its own bytes, before the rename.
        # Hashing only body/headers/meta here left `SHA256SUMS` unchecked, so a source
        # bundle mutating mid-copy could produce a final directory that this function
        # returned happily and verification rejected a moment later. Verifying after the
        # rename is not an alternative: that leaves the corrupt directory on disk.
        staged_files = _read_bundle(staging, (*EXPECTED_FILES, IMPORT_METADATA))
        staged_facts = _verify_snapshot(staged_files, facts.body_sha256)
        if staged_facts != facts:
            differing_fields = [
                key for key, value in facts.as_dict().items()
                if staged_facts.as_dict()[key] != value
            ]
            raise CaptureRefused(
                f"the staged copy does not match the bundle that was admitted "
                f"({', '.join(differing_fields)} differ). The source changed while it was "
                f"being copied; nothing is stored."
            )

        os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return final
