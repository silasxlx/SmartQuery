"""扫描仓库中的常见明文凭证；只输出文件和行号，不输出匹配内容。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".runtime",
    ".vector_db",
    ".uv-cache",
    ".venv",
    "__pycache__",
    ".pytest_cache",
}
PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9._-]{20,}\b"),
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*"
        r"['\"]?(?!\$\{)[A-Za-z0-9_+/=-]{16,}"
    ),
]


def iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".xlsx", ".xls"}:
            continue
        yield path


def main() -> int:
    findings: list[tuple[Path, int]] = []
    for path in iter_text_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append((path.relative_to(ROOT), number))
    for path, number in findings:
        print(f"{path}:{number}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
