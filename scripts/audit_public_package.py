#!/usr/bin/env python3
"""Fail closed when a public package contains likely private/runtime material."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath


TEXT_SUFFIXES = {
    "",
    ".command",
    ".env",
    ".example",
    ".json",
    ".md",
    ".plist",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
FORBIDDEN_NAMES = {
    "auth.json",
    "config.env",
    "feishu_bot.env",
    "relogin.env",
}
FORBIDDEN_PARTS = {
    ".tmp",
    "__pycache__",
    "codex-home",
    "codex-work",
    "logs",
    "日报文档",
    "日报状态",
    "日报草稿",
    "日报质检",
}
FORBIDDEN_SUFFIXES = {".log", ".png", ".pyc", ".qrcode"}


def content_patterns():
    user_root = "/" + "Users/"
    volume_root = "/" + "Volumes/"
    return {
        "Feishu/Lark identifier": re.compile(r"(?<![A-Za-z0-9])(?:oc|ou|om)_[A-Za-z0-9_-]{12,}"),
        "absolute user path": re.compile(re.escape(user_root) + r"[A-Za-z0-9._-]+"),
        "absolute volume path": re.compile(re.escape(volume_root) + r"[^\s\"'<>]+"),
        "private Feishu document URL": re.compile(r"https?://[^\s)\]>]*feishu[^\s)\]>]*/(?:docx|wiki)/[A-Za-z0-9]+", re.I),
        "secret assignment": re.compile(
            r"(?m)^(?:FEISHU_APP_SECRET|FEISHU_BOT_SECRET|RELOGIN_ADMIN_TOKEN|RELOGIN_TOKEN)"
            r"[ \t]*=[ \t]*(?:"
            r'"(?![$<{])[^"\n]+"|'
            r"'(?![$<{])[^'\n]+'|"
            r'''(?!(?:["'$<{]|os\.getenv|_cfg))[^\s#]+)'''
        ),
    }


def repo_files(root: Path) -> list[Path]:
    if (root / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
        return [root / item.decode() for item in result.stdout.split(b"\0") if item]
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


def path_violations(relative: PurePosixPath) -> list[str]:
    findings = []
    if relative.name in FORBIDDEN_NAMES:
        findings.append("forbidden private filename")
    if any(part in FORBIDDEN_PARTS for part in relative.parts):
        findings.append("forbidden runtime directory")
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES:
        findings.append("forbidden runtime/binary suffix")
    return findings


def scan_text(name: str, data: bytes) -> list[str]:
    suffix = PurePosixPath(name).suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ["non-UTF-8 content in text-like file"]
    return [label for label, pattern in content_patterns().items() if pattern.search(text)]


def scan_root(root: Path) -> list[tuple[str, str]]:
    findings = []
    for path in repo_files(root):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        for reason in path_violations(relative):
            findings.append((str(relative), reason))
        for reason in scan_text(str(relative), path.read_bytes()):
            findings.append((str(relative), reason))
    return findings


def scan_zip(path: Path) -> list[tuple[str, str]]:
    findings = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            relative = PurePosixPath(info.filename)
            for reason in path_violations(relative):
                findings.append((str(relative), reason))
            for reason in scan_text(str(relative), archive.read(info)):
                findings.append((str(relative), reason))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--deny-term", action="append", default=[])
    parser.add_argument("--deny-env-file", action="append", type=Path, default=[])
    parser.add_argument("--deny-local-context", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    findings = scan_root(root)
    if args.archive:
        findings.extend((f"archive:{name}", reason) for name, reason in scan_zip(args.archive))
    denied_terms = list(args.deny_term)
    sensitive_env_keys = {
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_BOT_SECRET",
        "FEISHU_BOT_WEBHOOK",
        "FEISHU_CHAT_ID",
        "FEISHU_REPORT_CHAT_ID",
        "RELOGIN_ADMIN_TOKEN",
        "RELOGIN_TOKEN",
    }
    for env_file in args.deny_env_file:
        try:
            lines = env_file.expanduser().read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            findings.append((str(env_file), f"cannot read deny env file: {exc}"))
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() in sensitive_env_keys and value and "REPLACE" not in value:
                denied_terms.append(value)
    if args.deny_local_context:
        denied_terms.append(Path.home().name)
        volume_parent = Path("/") / "Volumes"
        try:
            denied_terms.append(root.relative_to(volume_parent).parts[0])
        except ValueError:
            pass
    denied_terms = sorted({term for term in denied_terms if len(term) >= 4})
    if denied_terms:
        targets = repo_files(root)
        if args.archive:
            with zipfile.ZipFile(args.archive) as archive:
                archive_text = b"\n".join(
                    archive.read(info)
                    for info in archive.infolist()
                    if not info.is_dir() and PurePosixPath(info.filename).suffix.lower() in TEXT_SUFFIXES
                ).decode("utf-8", errors="ignore")
        else:
            archive_text = ""
        for term in denied_terms:
            for path in targets:
                if term and term in path.read_text(encoding="utf-8", errors="ignore"):
                    findings.append((str(path.relative_to(root)), "explicit denied term"))
            if term and term in archive_text:
                findings.append(("archive", "explicit denied term"))
    if findings:
        print("PUBLIC PACKAGE AUDIT FAILED", file=sys.stderr)
        for name, reason in sorted(set(findings)):
            print(f"- {name}: {reason}", file=sys.stderr)
        return 1
    print(f"PUBLIC PACKAGE AUDIT PASS ({len(repo_files(root))} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
