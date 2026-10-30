#!/usr/bin/env python3
"""vault-curator — deterministic reviewer for untrusted inbox material.

The contract (`contracts/tracking-schema.json` `inbox_dispatch`,
`contracts/provenance-protocol.md`) says an item from a peer or an authorised
email sender is reviewed and sanitized before anything downstream reads it. This
is the reviewer for a peer; the Master's is `.opencode/agents/evidence-reviewer`.

It is deliberately **not** an LLM. A model reviewing a prompt-injection payload
is itself a target, and the result would not be reproducible. This reviewer does
the part a program can do exactly:

* refuse anything outside `01_Inbox/`, a symlink, a non-regular file, an
  oversized file, invalid UTF-8, or a note that is already signed;
* remove active HTML (elements, comments, tags), orchestrator embeds, images and
  unsafe link schemes outside fenced code blocks;
* remove bidi/zero-width control characters that can disguise text;
* stop on high-confidence instruction-override or secret-exfiltration language
  unless the operator explicitly passes `--allow-instructions`;
* prefix a visible warning that the material is untrusted;
* on `--apply`, set `tracking.validity` to `sanitized` with the reviewer and
  evidence, then **sign** the result with the user key so `verify-note` still
  checks freshness. The signature attests the reviewed bytes, not that the
  content is true or harmless.

Scope boundary: this removes active content, not every semantic risk. The banner
and the evidence record say so, and the note stays attributed as untrusted
ingress. A reviewer that claimed more than that would be the unsafe part.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import stat
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

REVIEWER = "agents/vault-curator"
MODEL = "deterministic"
MAX_BYTES = 2 * 1024 * 1024
BANNER_FIRST_LINE = "> [!warning] Untrusted ingress — sanitized by `agents/vault-curator`"
BANNER_BODY = (
    BANNER_FIRST_LINE
    + "\n"
    + "> Active HTML, embeds and images were removed. This is a structural review, "
    + "not a guarantee that the text is true or free of instruction-like language.\n\n"
)
ROLE_ROOT = Path(__file__).resolve().parents[2]


class CuratorError(Exception):
    """A refusal or an unreadable input, both of which are terminal for one item."""


# --- load the one provenance implementation, by path --------------------------


def _find_shared(start: Path) -> Path:
    for root in (start, *start.parents):
        candidate = root / "shared" / "sparknet-provenance.py"
        if candidate.is_file():
            return candidate
    raise CuratorError("cannot find shared/sparknet-provenance.py from the reviewer's location")


_PROVENANCE: ModuleType | None = None


def _provenance() -> ModuleType:
    global _PROVENANCE
    if _PROVENANCE is not None:
        return _PROVENANCE
    path = _find_shared(Path(__file__).resolve().parent)
    spec = importlib.util.spec_from_file_location("sparknet_provenance_curator", path)
    if spec is None or spec.loader is None:
        raise CuratorError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _PROVENANCE = module
    return module


# --- path and file safety -----------------------------------------------------


def resolve_vault(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("SPARKNET_VAULT_ROOT", "").strip()
    if env:
        candidates.append(Path(env))
    candidates.append(ROLE_ROOT)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "00_System" / "user.md").is_file() and (resolved / "01_Inbox").is_dir():
            return resolved
    raise CuratorError("no vault found: pass --vault, set SPARKNET_VAULT_ROOT, or run from a peer role tree")


def resolve_inbox_item(vault: Path, item: str) -> Path:
    raw = Path(item).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    if raw.is_symlink():
        raise CuratorError(f"refusing a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise CuratorError(f"cannot resolve {raw}: {exc}") from exc
    inbox = (vault / "01_Inbox").resolve()
    try:
        resolved.relative_to(inbox)
    except ValueError as exc:
        raise CuratorError(f"only 01_Inbox/ items may be reviewed, got {item!r}") from exc
    if resolved.suffix.lower() != ".md":
        raise CuratorError(f"only Markdown inbox items are reviewed, got {resolved.name!r}")
    if not resolved.is_file():
        raise CuratorError(f"not a regular file: {resolved}")
    return resolved


def read_item(path: Path) -> tuple[bytes, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CuratorError(f"cannot open {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CuratorError(f"not a regular file: {path}")
        if info.st_size > MAX_BYTES:
            raise CuratorError(f"{path} is {info.st_size} bytes; the review limit is {MAX_BYTES}")
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_BYTES:
        raise CuratorError(f"{path} changed while being read or exceeds the review limit")
    return raw, info


# --- sanitisation -------------------------------------------------------------


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_ACTIVE_ELEMENTS = re.compile(
    r"<(script|style|iframe|object|embed|form|svg|math|template|noscript)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ACTIVE_OPEN = re.compile(
    r"<(script|style|iframe|object|embed|form|svg|math|template|noscript)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_VOID_TAGS = re.compile(
    r"<(?:meta|link|base|input|source|track|img)\b[^>]*?/?>",
    re.IGNORECASE,
)
_ANY_TAG = re.compile(r"<[^>]{1,500}>")
_TAG_LIKE = re.compile(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s|/?>)")
_AUTOLINK = re.compile(r"<(?:https?://|mailto:)[^>\s]{1,500}>", re.IGNORECASE)
_ORCHESTRATOR_EMBED = re.compile(r"!\[\[[^\]]{1,500}\]\]")
_MD_IMAGE = re.compile(r"!\[([^\]]{0,500})\]\([^)\n]{0,1000}\)")
_MD_REF_IMAGE = re.compile(r"!\[([^\]]{0,500})\]\[[^\]]{0,500}\]")
_UNSAFE_INLINE_LINK = re.compile(
    r"\[([^\]]{0,500})\]\(\s*(?:javascript|vbscript|data|file):[^)\n]*\)",
    re.IGNORECASE,
)
_UNSAFE_REF_LINK = re.compile(
    r"^[ \t]*\[[^\]]{1,500}\]:[ \t]*(?:javascript|vbscript|data|file):.*$",
    re.IGNORECASE | re.MULTILINE,
)
_BIDI_AND_CONTROL = re.compile(
    "[\u061c\u200b\u200c\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]"
)
_FENCE_START = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")

_INSTRUCTION_PATTERNS = (
    (
        "instruction-override",
        re.compile(
            r"\b(?:ignore|disregard|forget)\b.{0,40}\b(?:previous|prior|above|earlier)\b"
            r".{0,25}\b(?:instruction|prompt|message)s?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role-override",
        re.compile(
            r"\b(?:you are now|act as|pretend to be)\b.{0,40}\b(?:system|developer|admin|root)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt-exfiltration",
        re.compile(
            r"\b(?:reveal|show|print|repeat|dump)\b.{0,35}\b"
            r"(?:system prompt|developer message|hidden instruction|api key|private key)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "secret-exfiltration",
        re.compile(
            r"\b(?:send|upload|post|exfiltrate)\b.{0,40}\b(?:api key|private key|token|secret)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "remote-shell-pipe",
        re.compile(r"\b(?:curl|wget)\b[^\n|]{0,160}\|\s*(?:ba|z)?sh\b", re.IGNORECASE),
    ),
    ("destructive-root", re.compile(r"\brm\s+(?:-[A-Za-z]*r[A-Za-z]*f|-[A-Za-z]*f[A-Za-z]*r)\s+/(?:\s|$)")),
)


def _mask_fenced_code(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace fenced code blocks so a code example is not rewritten as active HTML."""
    lines = text.splitlines(keepends=True)
    normal: list[str] = []
    blocks: list[tuple[str, str]] = []
    block: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            match = _FENCE_START.match(line)
            if match:
                fence = match.group(1)
                block = [line]
            else:
                normal.append(line)
            continue
        block.append(line)
        closing = re.match(rf"^[ \t]{{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$", line)
        if closing:
            marker = f"\x00SPARKNET_CODE_{len(blocks)}\x00"
            blocks.append((marker, "".join(block)))
            normal.append(marker)
            fence = None
            block = []
    if fence is not None:
        marker = f"\x00SPARKNET_CODE_{len(blocks)}\x00"
        blocks.append((marker, "".join(block)))
        normal.append(marker)
    return "".join(normal), blocks


def _restore_fenced_code(text: str, blocks: list[tuple[str, str]]) -> str:
    for marker, block in blocks:
        text = text.replace(marker, block)
    return text


def _strip_tags(text: str, counts: Counter[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        if _AUTOLINK.fullmatch(tag):
            return tag
        if _TAG_LIKE.match(tag):
            counts["html_tag"] += 1
            return ""
        return tag

    return _ANY_TAG.sub(replace, text)


def sanitize_body(body: bytes) -> tuple[bytes, list[str], list[str], list[str]]:
    """Return (sanitized body, change labels, instruction warnings, masked text)."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CuratorError(f"body is not valid UTF-8: {exc}") from exc

    counts: Counter[str] = Counter()
    working, code_blocks = _mask_fenced_code(text)

    working, removed = _HTML_COMMENT.subn("", working)
    if removed:
        counts["html_comment"] += removed
    if "<!--" in working:
        working = working.split("<!--", 1)[0]
        counts["html_comment"] += 1

    working, removed = _ACTIVE_ELEMENTS.subn("[active HTML element removed]", working)
    if removed:
        counts["active_element"] += removed
    working, removed = _ACTIVE_OPEN.subn("[active HTML element removed]", working)
    if removed:
        counts["active_element"] += removed

    working, removed = _VOID_TAGS.subn("", working)
    if removed:
        counts["html_tag"] += removed
    working = _strip_tags(working, counts)

    working, removed = _ORCHESTRATOR_EMBED.subn("[embed removed]", working)
    if removed:
        counts["orchestrator_embed"] += removed
    working, removed = _MD_IMAGE.subn(lambda match: f"[image removed: {match.group(1)}]", working)
    if removed:
        counts["image"] += removed
    working, removed = _MD_REF_IMAGE.subn(lambda match: f"[image removed: {match.group(1)}]", working)
    if removed:
        counts["image"] += removed
    working, removed = _UNSAFE_INLINE_LINK.subn(lambda match: match.group(1), working)
    if removed:
        counts["unsafe_link"] += removed
    working, removed = _UNSAFE_REF_LINK.subn("", working)
    if removed:
        counts["unsafe_link"] += removed

    sanitized = _restore_fenced_code(working, code_blocks)
    sanitized, removed = _BIDI_AND_CONTROL.subn("", sanitized)
    if removed:
        counts["bidi_control"] += removed

    warnings = _instruction_warnings(sanitized)
    labels = [f"{name}={value}" for name, value in sorted(counts.items()) if value]
    return sanitized.encode("utf-8"), labels, warnings, sanitized


def _instruction_warnings(text: str) -> list[str]:
    masked, _blocks = _mask_fenced_code(text)
    warnings: list[str] = []
    for number, line in enumerate(masked.splitlines(), start=1):
        for name, pattern in _INSTRUCTION_PATTERNS:
            if pattern.search(line):
                warnings.append(f"{name} at line {number}")
    return warnings


def _ensure_banner(text: str, warnings: list[str]) -> str:
    if text.lstrip().startswith(BANNER_FIRST_LINE):
        return text
    banner = BANNER_BODY
    if warnings:
        banner += "> Instruction-like language was retained under operator override.\n\n"
    return banner + text.lstrip()


# --- review -------------------------------------------------------------------


def _validity(tracking: dict[str, Any]) -> dict[str, Any]:
    validity = tracking.get("validity")
    return validity if isinstance(validity, dict) else {}


def _require_acceptance(provenance: ModuleType, vault: Path, path: Path, source: str) -> None:
    """Untrusted ingress is reviewed only after a recorded, accepted intake."""
    relative = path.relative_to(vault).as_posix()
    ledger_path = vault / "blockchain" / "ledger.json"
    if not ledger_path.is_file():
        raise CuratorError(
            f"{relative} is untrusted {source} ingress but has no acceptance record; "
            "record it with `make accept-inbox` before review"
        )
    try:
        ledger = provenance.load_ledger(ledger_path)
    except provenance.ProvenanceError as exc:
        raise CuratorError(str(exc)) from exc
    ok, detail = provenance.verify_chain(ledger)
    if not ok:
        raise CuratorError(f"ledger does not verify; refusing to review over it: {detail}")
    for block in ledger.get("blocks", []):
        data = block.get("data") if isinstance(block, dict) else None
        if not isinstance(data, dict) or data.get("event") != "inbox_acceptance":
            continue
        if data.get("item") != relative:
            continue
        if data.get("source") != source or data.get("decision") != "accepted":
            continue
        if data.get("sanitized") is not True or not str(data.get("reviewer") or "").strip():
            continue
        return
    raise CuratorError(
        f"{relative} has no accepted, review-marked inbox_acceptance block for {source}; "
        "accept it with a reviewer before sanitising"
    )


def review_item(
    path: Path,
    *,
    vault: Path,
    apply: bool = False,
    allow_instructions: bool = False,
) -> dict[str, Any]:
    provenance = _provenance()
    raw, info = read_item(path)
    try:
        frontmatter, body = provenance.split_note(raw)
        tracking = provenance.tracking_of(frontmatter)
    except provenance.ProvenanceError as exc:
        raise CuratorError(str(exc)) from exc

    if tracking.get("schema") != "sparknet-tracking/1":
        raise CuratorError("note does not carry the sparknet-tracking/1 block")
    if isinstance(tracking.get("signature"), dict):
        raise CuratorError("note is already signed; refusing to re-sanitize signed bytes")
    state = _validity(tracking).get("state")
    if state not in (None, "unsigned", "sanitized"):
        raise CuratorError(f"note validity.state is {state!r}; only unsigned inbox items are reviewed")

    source = str(frontmatter.get("source") or "")
    if source in provenance.INBOX_UNTRUSTED_SOURCES:
        _require_acceptance(provenance, vault, path, source)
    elif source not in provenance.INBOX_SOURCES:
        raise CuratorError(f"note source is {source!r}; the reviewer needs a contracted source before it can decide")

    sanitized, changes, warnings, _scanned = sanitize_body(body)
    if warnings and not allow_instructions:
        raise CuratorError(
            "instruction-like language found ("
            + ", ".join(warnings)
            + "); nothing was changed. Pass --allow-instructions to retain it deliberately"
        )
    sanitized_text = _ensure_banner(sanitized.decode("utf-8"), warnings)
    sanitized = sanitized_text.encode("utf-8")
    changed = sanitized != body
    report: dict[str, Any] = {
        "path": str(path),
        "changed": changed,
        "changes": changes,
        "warnings": warnings,
        "body_sha256_before": hashlib.sha256(body).hexdigest(),
        "body_sha256_after": hashlib.sha256(sanitized).hexdigest(),
        "applied": False,
    }
    if not apply:
        return report

    validity = _validity(tracking)
    validity["state"] = "sanitized"
    validity["verified_by"] = REVIEWER
    validity["verified_at"] = provenance._iso(provenance._now())
    evidence = (
        f"reviewed body {report['body_sha256_before'][:16]} -> {report['body_sha256_after'][:16]}"
        f"; changes={','.join(changes) or 'none'}; warnings={len(warnings)}"
    )
    validity["evidence"] = evidence
    tracking["validity"] = validity
    tracking.pop("signature", None)
    frontmatter["tracking"] = tracking
    unsigned = provenance.emit_note(frontmatter, sanitized)
    try:
        signed = provenance.sign_note(unsigned, vault, agent="vault-curator", model=MODEL)
    except provenance.ProvenanceError as exc:
        raise CuratorError(str(exc)) from exc
    _replace_atomically(path, signed, info)
    report["applied"] = True
    return report


def _replace_atomically(path: Path, data: bytes, original: os.stat_result) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
        current = os.stat(path, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise CuratorError("the inbox item was replaced while it was being reviewed; refusing to overwrite it")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic reviewer for untrusted 01_Inbox material. Dry-run by "
            "default; --apply rewrites the note as sanitized and signs it."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="review one or more inbox items")
    review.add_argument("items", nargs="+", help="vault-relative or absolute paths under 01_Inbox/")
    review.add_argument("--vault", help="vault root (default: SPARKNET_VAULT_ROOT, else the peer role tree)")
    review.add_argument("--apply", action="store_true", help="rewrite and sign each sanitized item")
    review.add_argument(
        "--allow-instructions",
        action="store_true",
        help="retain instruction-like language deliberately (recorded in the note)",
    )
    review.add_argument("--json", action="store_true", help="emit a JSON report")
    args = parser.parse_args(argv)

    try:
        vault = resolve_vault(args.vault)
    except CuratorError as exc:
        print(f"vault-curator: {exc}", file=sys.stderr)
        return 1

    reports: list[dict[str, Any]] = []
    failures = 0
    for item in args.items:
        try:
            path = resolve_inbox_item(vault, item)
            report = review_item(
                path,
                vault=vault,
                apply=args.apply,
                allow_instructions=args.allow_instructions,
            )
            reports.append(report)
            if not args.json:
                state = "applied" if report["applied"] else "would sanitize"
                print(f"vault-curator: {state} {path.relative_to(vault)}")
                for change in report["changes"]:
                    print(f"  {change}")
                for warning in report["warnings"]:
                    print(f"  warning: {warning}")
                if not report["applied"]:
                    print("  dry-run; pass --apply to rewrite and sign")
        except CuratorError as exc:
            failures += 1
            if not args.json:
                print(f"vault-curator: REFUSED {item}: {exc}", file=sys.stderr)
            else:
                reports.append({"path": str(item), "error": str(exc), "applied": False})

    if args.json:
        import json

        print(json.dumps(reports, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
