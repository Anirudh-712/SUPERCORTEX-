"""Admission of a human-captured venue API bundle into L0.

The importer is the boundary between bytes a human carried across and anything this desk
will later treat as data. Everything it accepts becomes immutable L0, so every check here
is fail-closed: an unreadable manifest, a header block that does not describe a successful
JSON response, or a URL that is not the expected public endpoint is a refusal, never a
warning.

The one property the whole design rests on: **this module never fetches**. Two tests below
assert that structurally rather than trusting the docstring.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from supercortex.data import capture

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "funding_settlement"
GOOD = FIXTURES / "good"
SYMBOL = "BTCUSDT"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A writable copy of the good bundle. Never the fixture itself — tests mutate."""
    dest = tmp_path / "capture"
    shutil.copytree(GOOD, dest)
    return dest


def rehash(bundle: Path) -> None:
    """Rewrite SHA256SUMS to match current bytes, for tests altering a file legitimately."""
    lines = [
        f"{hashlib.sha256((bundle / n).read_bytes()).hexdigest()}  /elsewhere/{n}"
        for n in capture.MANIFEST_FILES
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── the importer must never reach the network ───────────────────────────────────

def test_capture_module_references_no_network_client() -> None:
    """`urllib.parse` is permitted — it is string parsing. `urllib.request` is not."""
    source = Path(capture.__file__).read_text(encoding="utf-8")
    for banned in ("urllib.request", "urlopen", "requests", "httpx", "socket", "aiohttp"):
        assert banned not in source, banned


def test_admission_does_not_call_urlopen(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt and braces: even if a fetch were added, this would catch it."""
    import urllib.request

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the importer attempted a network fetch")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    capture.admit(bundle, symbol=SYMBOL)


# ── the happy path ──────────────────────────────────────────────────────────────

def test_good_bundle_is_admitted(bundle: Path) -> None:
    facts = capture.admit(bundle, symbol=SYMBOL)
    assert facts.symbol == SYMBOL
    assert facts.status_code == 200
    assert facts.body_sha256 == hashlib.sha256((bundle / "body.json").read_bytes()).hexdigest()
    assert facts.body_bytes == (bundle / "body.json").stat().st_size
    assert facts.provenance == "VENUE_API_CAPTURE"


def test_server_time_and_local_time_are_separate_facts(bundle: Path) -> None:
    """Neither clock authenticates the other, so neither may overwrite the other."""
    facts = capture.admit(bundle, symbol=SYMBOL)
    stored = facts.as_dict()
    # Two clocks, two formats, two fields. Neither is normalised into the other, because
    # normalising them would imply one can stand in for the other.
    assert stored["captured_at_local"].endswith("Z")          # operator's clock, ISO 8601
    assert "GMT" in stored["server_date"]                     # venue's clock, HTTP-date
    assert stored["captured_at_local"] != stored["server_date"]


# ── bundle admission ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", capture.EXPECTED_FILES)
def test_every_expected_file_is_required(bundle: Path, missing: str) -> None:
    (bundle / missing).unlink()
    with pytest.raises(capture.CaptureRefused, match=missing):
        capture.admit(bundle, symbol=SYMBOL)


def test_tampered_body_fails_the_manifest(bundle: Path) -> None:
    (bundle / "body.json").write_bytes(b"[]")
    with pytest.raises(capture.CaptureRefused, match=r"body.json"):
        capture.admit(bundle, symbol=SYMBOL)


def test_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(capture.CaptureRefused):
        capture.admit(tmp_path / "nope", symbol=SYMBOL)


# ── manifest parsing: basename only, never follow a path ────────────────────────

def test_manifest_keys_on_basename_not_path(bundle: Path) -> None:
    """The real manifest holds absolute paths from another machine. They are labels."""
    text = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    assert "/" in text
    assert set(capture.parse_manifest(text)) == set(capture.MANIFEST_FILES)


def test_manifest_path_is_never_read(bundle: Path, tmp_path: Path) -> None:
    """A manifest naming a file outside the bundle must not cause that file to be read."""
    planted = tmp_path / "planted.json"
    planted.write_bytes(b'["planted"]')
    digest = hashlib.sha256(planted.read_bytes()).hexdigest()
    (bundle / "SHA256SUMS").write_text(
        f"{digest}  {planted}\n"
        + "\n".join(
            f"{hashlib.sha256((bundle / n).read_bytes()).hexdigest()}  /x/{n}"
            for n in capture.MANIFEST_FILES
            if n != "body.json"
        )
        + "\n",
        encoding="utf-8",
    )
    # 'planted.json' is not an expected basename, so this is an unexpected entry AND
    # body.json now has no digest. Either way: refused, and planted.json never opened.
    with pytest.raises(capture.CaptureRefused):
        capture.admit(bundle, symbol=SYMBOL)


@pytest.mark.parametrize(
    "line",
    [
        "notahexdigest  body.json",
        "  body.json",
        "deadbeef  body.json",
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ],
    ids=["non-hex", "no-digest", "short-digest", "no-path"],
)
def test_malformed_manifest_lines_are_refused(line: str) -> None:
    with pytest.raises(capture.CaptureRefused):
        capture.parse_manifest(line + "\n")


def test_duplicate_manifest_entry_is_refused(bundle: Path) -> None:
    text = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    first = text.splitlines()[0]
    with pytest.raises(capture.CaptureRefused, match=r"(?i)duplicate"):
        capture.parse_manifest(text + first + "\n")


def test_unexpected_manifest_entry_is_refused(bundle: Path) -> None:
    text = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    extra = f"{'a' * 64}  /somewhere/unexpected.json\n"
    with pytest.raises(capture.CaptureRefused, match=r"(?i)unexpected"):
        capture.parse_manifest(text + extra)


def test_missing_manifest_entry_is_refused(bundle: Path) -> None:
    text = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    trimmed = "\n".join(
        ln for ln in text.splitlines() if not ln.endswith("body.json")
    )
    with pytest.raises(capture.CaptureRefused, match=r"body.json"):
        capture.parse_manifest(trimmed + "\n")


@pytest.mark.parametrize(
    "path", ["../body.json", "../../etc/passwd", "sub/../../body.json"]
)
def test_path_traversal_entries_are_refused(path: str) -> None:
    with pytest.raises(capture.CaptureRefused, match=r"(?i)traversal|unexpected"):
        capture.parse_manifest(f"{'b' * 64}  {path}\n")


# ── header block parsing ────────────────────────────────────────────────────────

def test_final_response_block_is_the_one_used() -> None:
    """curl -D appends every block; a 200 after a 302 is the response we received."""
    text = (
        "HTTP/1.1 302 Found\r\nlocation: /elsewhere\r\n\r\n"
        "HTTP/2 200 \r\ncontent-type: application/json;charset=UTF-8\r\n"
        "content-length: 2\r\ndate: Wed, 26 Aug 2026 05:38:17 GMT\r\n\r\n"
    )
    facts = capture.parse_headers(text)
    assert facts.status_code == 200
    assert facts.content_length == 2


def test_trailing_block_that_failed_is_refused() -> None:
    text = (
        "HTTP/2 200 \r\ncontent-type: application/json\r\n\r\n"
        "HTTP/2 500 \r\ncontent-type: application/json\r\n\r\n"
    )
    with pytest.raises(capture.CaptureRefused, match=r"500"):
        capture.parse_headers(text)


@pytest.mark.parametrize(
    "headers, match",
    [
        ("HTTP/2 404 \r\ncontent-type: application/json\r\n\r\n", "404"),
        ("HTTP/2 200 \r\ncontent-type: text/html\r\n\r\n", "(?i)content type"),
        ("not a status line\r\n\r\n", "(?i)status"),
        ("", "(?i)status|empty"),
    ],
    ids=["error-status", "wrong-content-type", "no-status-line", "empty"],
)
def test_bad_header_blocks_are_refused(headers: str, match: str) -> None:
    with pytest.raises(capture.CaptureRefused, match=match):
        capture.parse_headers(headers)


def test_content_length_must_match_body(bundle: Path) -> None:
    text = (bundle / "headers.txt").read_text(encoding="utf-8")
    (bundle / "headers.txt").write_text(
        text.replace(f"content-length: {(bundle / 'body.json').stat().st_size}",
                     "content-length: 999999"),
        encoding="utf-8",
    )
    rehash(bundle)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)content-length"):
        capture.admit(bundle, symbol=SYMBOL)


def test_absent_content_length_is_allowed(bundle: Path) -> None:
    """Chunked responses omit it. Absent is fine; present-and-wrong is not."""
    import re

    text = (bundle / "headers.txt").read_text(encoding="utf-8")
    (bundle / "headers.txt").write_text(
        re.sub(r"(?im)^content-length:.*\n", "", text), encoding="utf-8"
    )
    rehash(bundle)
    facts = capture.admit(bundle, symbol=SYMBOL)
    assert facts.content_length is None


# ── meta.txt parsing ────────────────────────────────────────────────────────────

def test_meta_requires_each_expected_key_exactly_once(bundle: Path) -> None:
    text = (bundle / "meta.txt").read_text(encoding="utf-8")
    (bundle / "meta.txt").write_text(text + text.splitlines()[0] + "\n", encoding="utf-8")
    rehash(bundle)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)duplicate|once"):
        capture.admit(bundle, symbol=SYMBOL)


def test_meta_rejects_unknown_keys(bundle: Path) -> None:
    text = (bundle / "meta.txt").read_text(encoding="utf-8")
    (bundle / "meta.txt").write_text(text + "surprise=1\n", encoding="utf-8")
    rehash(bundle)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)unknown|unexpected"):
        capture.admit(bundle, symbol=SYMBOL)


@pytest.mark.parametrize(
    "stamp", ["2026-08-26 05:38:17", "not-a-time", "2026-08-26T05:38:17+05:30", ""]
)
def test_meta_capture_time_must_be_valid_utc(bundle: Path, stamp: str) -> None:
    import re

    text = (bundle / "meta.txt").read_text(encoding="utf-8")
    (bundle / "meta.txt").write_text(
        re.sub(r"(?m)^captured_at=.*$", f"captured_at={stamp}", text), encoding="utf-8"
    )
    rehash(bundle)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)captured_at|utc|timestamp"):
        capture.admit(bundle, symbol=SYMBOL)


@pytest.mark.parametrize(
    "secret",
    ["apiKey=abc123", "signature=deadbeef", "X-MBX-APIKEY: abc", "secret=hunter2"],
)
def test_meta_carrying_credential_material_is_refused(bundle: Path, secret: str) -> None:
    text = (bundle / "meta.txt").read_text(encoding="utf-8")
    (bundle / "meta.txt").write_text(text + f"curl_extra={secret}\n", encoding="utf-8")
    rehash(bundle)
    with pytest.raises(capture.CaptureRefused):
        capture.admit(bundle, symbol=SYMBOL)


# ── URL validation, entirely offline ────────────────────────────────────────────

def test_recorded_url_is_the_expected_public_endpoint(bundle: Path) -> None:
    facts = capture.admit(bundle, symbol=SYMBOL)
    capture.validate_url(facts.url, symbol=SYMBOL)


@pytest.mark.parametrize(
    "url, match",
    [
        ("http://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT", "(?i)scheme|https"),
        ("https://evil.example/fapi/v1/fundingRate?symbol=BTCUSDT", "(?i)host"),
        ("https://fapi.binance.com/fapi/v1/account?symbol=BTCUSDT", "(?i)path"),
        ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT", "(?i)symbol"),
        ("https://fapi.binance.com/fapi/v1/fundingRate", "(?i)symbol"),
        ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&apiKey=x",
         "(?i)credential|query"),
        ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&signature=x",
         "(?i)credential|query"),
        ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&weird=1",
         "(?i)query"),
    ],
    ids=["http", "wrong-host", "wrong-path", "wrong-symbol", "no-symbol",
         "apikey", "signature", "unknown-param"],
)
def test_bad_urls_are_refused(url: str, match: str) -> None:
    with pytest.raises(capture.CaptureRefused, match=match):
        capture.validate_url(url, symbol=SYMBOL)


@pytest.mark.parametrize("param", ["limit=1000", "startTime=1", "endTime=2"])
def test_allowed_query_parameters_pass(param: str) -> None:
    capture.validate_url(
        f"https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&{param}",
        symbol=SYMBOL,
    )


# ── L0 storage: staging, atomic rename, idempotent re-import ────────────────────

def test_store_preserves_all_four_files_byte_for_byte(bundle: Path, tmp_path: Path) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    for name in capture.EXPECTED_FILES:
        assert (stored / name).read_bytes() == (GOOD / name).read_bytes(), name


def test_store_writes_generated_metadata_separately(bundle: Path, tmp_path: Path) -> None:
    """import.json is ours. meta.txt is the operator's and is never replaced."""
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    assert (stored / "import.json").exists()
    generated = json.loads((stored / "import.json").read_text(encoding="utf-8"))
    assert generated["provenance"] == "VENUE_API_CAPTURE"
    assert generated["mark_price_corroboration"] == "NONE"
    assert (stored / "meta.txt").read_bytes() == (GOOD / "meta.txt").read_bytes()


def test_final_directory_is_content_addressed(bundle: Path, tmp_path: Path) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    body_hash = hashlib.sha256((GOOD / "body.json").read_bytes()).hexdigest()
    assert stored.name == body_hash


def test_no_staging_directory_survives_success(bundle: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    leftovers = [p for p in stored.parent.iterdir() if p.name != stored.name]
    assert leftovers == []


def test_failure_leaves_no_final_directory_and_retry_still_works(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must block neither the retry nor a later good import."""
    raw = tmp_path / "raw"
    real = capture._write_import_metadata

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(capture, "_write_import_metadata", boom)
    with pytest.raises(RuntimeError):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)

    body_hash = hashlib.sha256((GOOD / "body.json").read_bytes()).hexdigest()
    final = capture.capture_dir(raw, "um", SYMBOL, body_hash)
    assert not final.exists(), "a crash left a final directory that blocks retry"

    monkeypatch.setattr(capture, "_write_import_metadata", real)
    assert capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw) == final


def test_reimport_of_identical_capture_is_a_noop(bundle: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    first = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    second = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    assert second == first
    assert {p.name: p.read_bytes() for p in second.iterdir()} == before


def test_reimport_verifies_stored_bytes_and_refuses_on_corruption(
    bundle: Path, tmp_path: Path
) -> None:
    """A stored capture that no longer matches its own hash is not a no-op — it is a fault."""
    raw = tmp_path / "raw"
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    (stored / "headers.txt").write_text("corrupted", encoding="utf-8")
    with pytest.raises(capture.CaptureRefused,
                       match=r"(?i)corrupt|mismatch|does not match"):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)


def test_store_refuses_a_bundle_that_would_not_be_admitted(
    bundle: Path, tmp_path: Path
) -> None:
    (bundle / "body.json").write_bytes(b"[]")
    with pytest.raises(capture.CaptureRefused):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    assert not (tmp_path / "raw").exists() or not any((tmp_path / "raw").rglob("body.json"))


# ── stored L0 must be re-authenticated, not trusted ─────────────────────────────
# A stored capture is only evidence while its bytes still match what was admitted. The
# builder used to hash whatever `body.json` happened to contain and adopt that hash as
# provenance, which blesses edited bytes instead of authenticating the bundle. These pin
# the fix.

def tamper(stored: Path, name: str, payload: bytes, *, rewrite_manifest: bool) -> None:
    (stored / name).write_bytes(payload)
    if rewrite_manifest:
        lines = [
            f"{hashlib.sha256((stored / n).read_bytes()).hexdigest()}  /x/{n}"
            for n in capture.MANIFEST_FILES
        ]
        (stored / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def stored(bundle: Path, tmp_path: Path) -> Path:
    return capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")


def test_directory_is_named_by_the_full_body_digest(stored: Path) -> None:
    """Truncation invites collision. No real captures exist, so full digest it is."""
    digest = hashlib.sha256((GOOD / "body.json").read_bytes()).hexdigest()
    assert stored.name == digest
    assert len(stored.name) == 64


def test_verify_stored_accepts_an_untouched_capture(stored: Path) -> None:
    facts = capture.verify_stored(stored)
    assert facts.body_sha256 == stored.name
    assert facts.provenance == "VENUE_API_CAPTURE"


def test_manifest_digest_is_recorded_in_import_metadata(stored: Path) -> None:
    recorded = json.loads((stored / "import.json").read_text(encoding="utf-8"))
    assert recorded["manifest_sha256"] == hashlib.sha256(
        (stored / "SHA256SUMS").read_bytes()
    ).hexdigest()


def test_body_changed_alone_is_refused(stored: Path) -> None:
    """The exact blocker: edit the body, leave the manifest, and the build blessed it."""
    tamper(stored, "body.json", b'[{"tampered":true}]', rewrite_manifest=False)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)body.json"):
        capture.verify_stored(stored)


def test_body_changed_with_a_rewritten_manifest_is_refused(stored: Path) -> None:
    """A *fully* self-consistent forgery — body, content-length and manifest all agreeing
    — still cannot reproduce the content-addressed directory name."""
    import re

    forged = b'[{"tampered":true}]'
    (stored / "body.json").write_bytes(forged)
    (stored / "headers.txt").write_text(
        re.sub(r"(?im)^content-length:.*$", f"content-length: {len(forged)}",
               (stored / "headers.txt").read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    lines = [
        f"{hashlib.sha256((stored / n).read_bytes()).hexdigest()}  /x/{n}"
        for n in capture.MANIFEST_FILES
    ]
    (stored / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(capture.CaptureRefused, match=r"(?i)directory name|digest"):
        capture.verify_stored(stored)


@pytest.mark.parametrize("name", ["headers.txt", "meta.txt"])
def test_other_operator_files_changed_are_refused(stored: Path, name: str) -> None:
    tamper(stored, name, b"tampered\n", rewrite_manifest=False)
    with pytest.raises(capture.CaptureRefused):
        capture.verify_stored(stored)


@pytest.mark.parametrize("name", ["headers.txt", "meta.txt"])
def test_other_files_changed_with_rewritten_manifest_are_refused(
    stored: Path, name: str
) -> None:
    tamper(stored, name, b"tampered\n", rewrite_manifest=True)
    with pytest.raises(capture.CaptureRefused):
        capture.verify_stored(stored)


def test_manifest_changed_alone_is_refused(stored: Path) -> None:
    """Digests unchanged, bytes reordered: caught by the recorded manifest digest."""
    text = (stored / "SHA256SUMS").read_text(encoding="utf-8")
    (stored / "SHA256SUMS").write_text(
        "\n".join(reversed(text.strip().splitlines())) + "\n", encoding="utf-8"
    )
    with pytest.raises(capture.CaptureRefused, match=r"(?i)manifest|SHA256SUMS"):
        capture.verify_stored(stored)


def test_import_metadata_changed_is_refused(stored: Path) -> None:
    recorded = json.loads((stored / "import.json").read_text(encoding="utf-8"))
    recorded["captured_at_local"] = "1999-01-01T00:00:00Z"
    (stored / "import.json").write_text(json.dumps(recorded, indent=2), encoding="utf-8")
    with pytest.raises(capture.CaptureRefused, match=r"(?i)import.json|disagree"):
        capture.verify_stored(stored)


def test_directory_renamed_away_from_its_digest_is_refused(stored: Path) -> None:
    moved = stored.parent / ("f" * 64)
    stored.rename(moved)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)directory|digest"):
        capture.verify_stored(moved)


def test_missing_import_metadata_is_refused(stored: Path) -> None:
    (stored / "import.json").unlink()
    with pytest.raises(capture.CaptureRefused, match=r"(?i)import.json"):
        capture.verify_stored(stored)


def test_reimport_compares_the_manifest_too(bundle: Path, tmp_path: Path) -> None:
    """Same body, different SHA256SUMS bytes: not the same capture, so not a no-op."""
    raw = tmp_path / "raw"
    capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    text = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    (bundle / "SHA256SUMS").write_text(
        "\n".join(reversed(text.strip().splitlines())) + "\n", encoding="utf-8"
    )
    with pytest.raises(capture.CaptureRefused, match=r"(?i)SHA256SUMS|differs|manifest"):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)


# ── staging must be verified before the rename, not after ───────────────────────
# `store()` used to hash only body/headers/meta in staging. A source bundle mutating
# between admission and copy could therefore produce a final directory that `store()`
# returned happily and `verify_stored()` would reject on the very next call. Verifying
# after the rename is not an option: that leaves the corrupt directory on disk.

def sabotage_copy(monkeypatch: pytest.MonkeyPatch, target: str, payload: bytes) -> None:
    """Mutate one file *after* it lands in staging, simulating a source changing mid-copy."""
    real = capture.shutil.copy2

    def sneaky(src: object, dst: object, *args: object, **kwargs: object) -> object:
        result = real(src, dst, *args, **kwargs)
        if Path(str(dst)).name == target:
            Path(str(dst)).write_bytes(payload)
        return result

    monkeypatch.setattr(capture.shutil, "copy2", sneaky)


def final_for(raw: Path) -> Path:
    digest = hashlib.sha256((GOOD / "body.json").read_bytes()).hexdigest()
    return capture.capture_dir(raw, "um", SYMBOL, digest)


def assert_no_debris(raw: Path, final: Path) -> None:
    assert not final.exists(), "a failed staging verification left a final directory"
    if final.parent.exists():
        assert not list(final.parent.glob(".staging-*")), "staging debris survived"


@pytest.mark.parametrize(
    "target, payload",
    [
        ("SHA256SUMS", b"tampered\n"),
        ("body.json", b'[{"tampered":true}]'),
        ("headers.txt", b"tampered\n"),
        ("meta.txt", b"tampered\n"),
    ],
)
def test_mutation_during_copy_is_caught_before_the_rename(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    target: str, payload: bytes,
) -> None:
    raw = tmp_path / "raw"
    sabotage_copy(monkeypatch, target, payload)
    with pytest.raises(capture.CaptureRefused):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    assert_no_debris(raw, final_for(raw))


def test_stored_capture_always_passes_verification_immediately(
    bundle: Path, tmp_path: Path
) -> None:
    """The invariant the staging gap broke: whatever `store` returns, `verify_stored`
    accepts. If those two can ever disagree, L0 is not trustworthy."""
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    assert capture.verify_stored(stored).body_sha256 == stored.name


def test_corrupted_generated_metadata_in_staging_is_caught(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`import.json` is ours, so a bad write of it must also fail before the rename."""
    real = capture._write_import_metadata

    def broken(staging: Path, facts: capture.CaptureFacts) -> None:
        real(staging, facts)
        (staging / capture.IMPORT_METADATA).write_text('{"symbol": "WRONG"}', encoding="utf-8")

    monkeypatch.setattr(capture, "_write_import_metadata", broken)
    raw = tmp_path / "raw"
    with pytest.raises(capture.CaptureRefused):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    assert_no_debris(raw, final_for(raw))


# ── capture_root ────────────────────────────────────────────────────────────────

def test_capture_root_is_the_parent_of_every_capture(tmp_path: Path) -> None:
    root = capture.capture_root(tmp_path, "um", SYMBOL)
    assert capture.capture_dir(tmp_path, "um", SYMBOL, "d" * 64).parent == root


# ── verification and consumption must share one snapshot ────────────────────────

def test_read_verified_stored_returns_the_bytes_it_verified(
    bundle: Path, tmp_path: Path
) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    verified = capture.read_verified_stored(stored)
    assert verified.body == (GOOD / "body.json").read_bytes()
    assert hashlib.sha256(verified.body).hexdigest() == verified.facts.body_sha256


def test_bytes_returned_are_immune_to_later_mutation(bundle: Path, tmp_path: Path) -> None:
    """The snapshot is taken once. Editing the file afterwards cannot alter what was
    already verified and returned."""
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    verified = capture.read_verified_stored(stored)
    (stored / "body.json").write_bytes(b'[{"tampered":true}]')
    assert verified.body != (stored / "body.json").read_bytes()
    assert hashlib.sha256(verified.body).hexdigest() == verified.facts.body_sha256


# ── import.json is verified exactly, note included ──────────────────────────────

def test_import_metadata_payload_is_the_single_definition(bundle: Path, tmp_path: Path) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    facts = capture.verify_stored(stored)
    on_disk = json.loads((stored / capture.IMPORT_METADATA).read_text(encoding="utf-8"))
    assert on_disk == capture.import_metadata_payload(facts)


def test_deleting_the_trust_note_is_refused(bundle: Path, tmp_path: Path) -> None:
    """The note is the trust position. It may not be quietly dropped from stored evidence."""
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    recorded = json.loads((stored / capture.IMPORT_METADATA).read_text(encoding="utf-8"))
    del recorded["note"]
    (stored / capture.IMPORT_METADATA).write_text(json.dumps(recorded), encoding="utf-8")
    with pytest.raises(capture.CaptureRefused, match=r"(?i)missing"):
        capture.verify_stored(stored)


def test_altering_the_trust_note_is_refused(bundle: Path, tmp_path: Path) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    recorded = json.loads((stored / capture.IMPORT_METADATA).read_text(encoding="utf-8"))
    recorded["note"] = "these bytes are authenticated by the venue"
    (stored / capture.IMPORT_METADATA).write_text(json.dumps(recorded), encoding="utf-8")
    with pytest.raises(capture.CaptureRefused, match=r"(?i)changed"):
        capture.verify_stored(stored)


def test_extra_metadata_keys_are_refused(bundle: Path, tmp_path: Path) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    recorded = json.loads((stored / capture.IMPORT_METADATA).read_text(encoding="utf-8"))
    recorded["extra"] = "smuggled"
    (stored / capture.IMPORT_METADATA).write_text(json.dumps(recorded), encoding="utf-8")
    with pytest.raises(capture.CaptureRefused, match=r"(?i)unexpected"):
        capture.verify_stored(stored)


# ── path components are validated below the CLI ─────────────────────────────────

@pytest.mark.parametrize(
    "symbol",
    ["", ".", "..", "../evil", "BTC/USDT", "BTC\\USDT", "BTC.USDT", "BTC\x00USDT",
     "a" * 33],
    ids=["empty", "dot", "dotdot", "traversal", "slash", "backslash", "period",
         "nul", "too-long"],
)
def test_unsafe_symbols_are_refused(symbol: str, tmp_path: Path) -> None:
    with pytest.raises(capture.CaptureRefused, match=r"(?i)symbol"):
        capture.capture_root(tmp_path, "um", symbol)


@pytest.mark.parametrize("market", ["cm", "spot", "", "../um", "um/../.."])
def test_unsupported_markets_are_refused(market: str, tmp_path: Path) -> None:
    with pytest.raises(capture.CaptureRefused, match=r"(?i)market"):
        capture.capture_root(tmp_path, market, SYMBOL)


@pytest.mark.parametrize("symbol", ["../../escape", "BTC/USDT"])
def test_store_writes_nothing_outside_the_capture_root(
    bundle: Path, tmp_path: Path, symbol: str
) -> None:
    raw = tmp_path / "raw"
    with pytest.raises(capture.CaptureRefused):
        capture.store(bundle, symbol=symbol, market="um", raw_root=raw)
    assert not raw.exists(), "a refused symbol still created directories"
    assert list(tmp_path.iterdir()) == [bundle]


def test_store_refuses_a_market_this_endpoint_cannot_serve(
    bundle: Path, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    with pytest.raises(capture.CaptureRefused, match=r"(?i)market"):
        capture.store(bundle, symbol=SYMBOL, market="cm", raw_root=raw)
    assert not raw.exists()


# ── evidence is never silently repaired ─────────────────────────────────────────
# `errors="replace"` turns an undecodable byte into U+FFFD and carries on. On an evidence
# file that is not leniency, it is quiet mutation: the admitted facts then describe bytes
# nobody sent, and the digests still agree because the manifest was computed over the
# damaged file. Strict decoding, and a refusal that names the file.

def corrupt_text(bundle: Path, name: str) -> None:
    """Make one file undecodable while leaving every digest consistent.

    `SHA256SUMS` is deliberately not rehashed: a manifest cannot digest itself, so
    corrupting it breaks no checksum and the encoding is the only thing wrong. For the
    other files the manifest is rebuilt afterwards, so the encoding is again the only
    defect — which is exactly the case `errors="replace"` used to wave through.
    """
    (bundle / name).write_bytes((bundle / name).read_bytes().replace(b"c", b"\xff", 1))
    if name != "SHA256SUMS":
        rehash(bundle)


@pytest.mark.parametrize("name", capture.TEXT_FILES)
def test_invalid_utf8_in_any_text_file_is_refused(bundle: Path, name: str) -> None:
    corrupt_text(bundle, name)
    with pytest.raises(capture.CaptureRefused, match=rf"(?i){re.escape(name)}"):
        capture.admit(bundle, symbol=SYMBOL)


@pytest.mark.parametrize("name", capture.TEXT_FILES)
def test_invalid_utf8_leaves_no_l0_directory(
    bundle: Path, tmp_path: Path, name: str
) -> None:
    corrupt_text(bundle, name)
    raw = tmp_path / "raw"
    with pytest.raises(capture.CaptureRefused):
        capture.store(bundle, symbol=SYMBOL, market="um", raw_root=raw)
    assert not raw.exists() or not any(raw.rglob("body.json"))


def test_no_replacement_character_survives_admission(bundle: Path) -> None:
    """The specific failure: U+FFFD reaching `curl_version` and being stored as fact."""
    raw = (bundle / "meta.txt").read_bytes().replace(b"curl=curl", b"curl=cu\xffrl")
    (bundle / "meta.txt").write_bytes(raw)
    rehash(bundle)
    with pytest.raises(capture.CaptureRefused, match=r"(?i)meta.txt"):
        capture.admit(bundle, symbol=SYMBOL)


def test_valid_non_ascii_utf8_is_still_accepted(bundle: Path) -> None:
    """Strict is not ASCII-only. Well-formed UTF-8 must keep working."""
    text = (bundle / "meta.txt").read_text(encoding="utf-8")
    (bundle / "meta.txt").write_text(text.replace("curl=curl", "curl=cürl"), encoding="utf-8")
    rehash(bundle)
    assert "cürl" in capture.admit(bundle, symbol=SYMBOL).curl_version


def test_invalid_utf8_in_import_metadata_is_refused(bundle: Path, tmp_path: Path) -> None:
    stored = capture.store(bundle, symbol=SYMBOL, market="um", raw_root=tmp_path / "raw")
    path = stored / capture.IMPORT_METADATA
    path.write_bytes(path.read_bytes().replace(b"s", b"\xff", 1))
    with pytest.raises(capture.CaptureRefused, match=rf"(?i){capture.IMPORT_METADATA}"):
        capture.verify_stored(stored)
