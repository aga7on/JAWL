"""Token-efficient, cross-language repository context tools."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.polls.utils import is_ignored
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils.logger import main_logger


class HostOSCodingContext:
    """Builds compact file/symbol maps without a persistent indexing prerequisite."""

    _LANGUAGES = {
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cs": "csharp",
        ".cxx": "cpp",
        ".go": "go",
        ".h": "c",
        ".hpp": "cpp",
        ".java": "java",
        ".js": "javascript",
        ".jsx": "javascript",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".php": "php",
        ".py": "python",
        ".rb": "ruby",
        ".rs": "rust",
        ".swift": "swift",
        ".ts": "typescript",
        ".tsx": "typescript",
    }
    _SPECIAL_FILES = {
        "CMakeLists.txt": "cmake",
        "Dockerfile": "dockerfile",
        "Makefile": "make",
    }
    _GENERIC_PATTERNS: Dict[str, Tuple[Tuple[str, re.Pattern[str]], ...]] = {
        "javascript": (
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")),
        ),
        "typescript": (
            ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
            ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=")),
            ("enum", re.compile(r"^\s*(?:export\s+)?enum\s+([A-Za-z_$][\w$]*)")),
            ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")),
        ),
        "rust": (
            ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)")),
            ("enum", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_]\w*)")),
            ("trait", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(([^)]*)")),
        ),
        "go": (
            ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b")),
            ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(([^)]*)")),
        ),
        "ruby": (
            ("class", re.compile(r"^\s*class\s+([A-Za-z_:]\w*(?:::\w+)*)")),
            ("module", re.compile(r"^\s*module\s+([A-Za-z_:]\w*(?:::\w+)*)")),
            ("function", re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)")),
        ),
        "java": (
            ("type", re.compile(r"^\s*(?:public\s+|protected\s+|private\s+)?(?:abstract\s+|final\s+)?(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")),
        ),
        "csharp": (
            ("type", re.compile(r"^\s*(?:public\s+|internal\s+|protected\s+|private\s+)?(?:abstract\s+|sealed\s+|static\s+)?(?:class|interface|enum|record|struct)\s+([A-Za-z_]\w*)")),
        ),
        "kotlin": (
            ("type", re.compile(r"^\s*(?:public\s+|internal\s+|private\s+)?(?:data\s+|sealed\s+|enum\s+)?(?:class|interface|object)\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:suspend\s+)?fun\s+([A-Za-z_]\w*)\s*\(([^)]*)")),
        ),
        "swift": (
            ("type", re.compile(r"^\s*(?:public\s+|internal\s+|private\s+)?(?:class|struct|enum|protocol|actor)\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:public\s+|internal\s+|private\s+)?func\s+([A-Za-z_]\w*)\s*\(([^)]*)")),
        ),
        "php": (
            ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:public\s+|protected\s+|private\s+|static\s+)*function\s+([A-Za-z_]\w*)\s*\(([^)]*)")),
        ),
        "c": (
            ("type", re.compile(r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:[A-Za-z_]\w*[\s*]+)+([A-Za-z_]\w*)\s*\(([^;]*)\)\s*\{")),
        ),
        "cpp": (
            ("type", re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct|enum)\s+([A-Za-z_]\w*)")),
            ("function", re.compile(r"^\s*(?:[\w:<>,~*&]+\s+)+([A-Za-z_]\w*(?:::\w+)*)\s*\(([^;]*)\)\s*(?:const\s*)?\{")),
        ),
    }

    def __init__(self, host_os_client: HostOSClient) -> None:
        self.host_os = host_os_client

    @classmethod
    def _language(cls, path: Path) -> Optional[str]:
        return cls._SPECIAL_FILES.get(path.name) or cls._LANGUAGES.get(
            path.suffix.lower()
        )

    @staticmethod
    def _short_signature(arguments: str, max_chars: int = 160) -> str:
        compact = " ".join(arguments.split())
        return compact if len(compact) <= max_chars else compact[:max_chars] + "..."

    def _python_symbols(self, source: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [], f"SyntaxError at line {exc.lineno}: {exc.msg}"
        symbols: List[Dict[str, Any]] = []

        def add_function(node: ast.FunctionDef | ast.AsyncFunctionDef, owner: str = ""):
            name = f"{owner}.{node.name}" if owner else node.name
            try:
                signature = ast.unparse(node.args)
            except Exception:
                signature = ""
            symbols.append(
                {
                    "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                    "name": name,
                    "line": node.lineno,
                    "end_line": getattr(node, "end_lineno", None),
                    "signature": self._short_signature(signature),
                }
            )

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    {
                        "kind": "class",
                        "name": node.name,
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", None),
                    }
                )
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        add_function(member, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function(node)
        return symbols, None

    def _generic_symbols(self, language: str, lines: List[str]) -> List[Dict[str, Any]]:
        patterns = self._GENERIC_PATTERNS.get(language, ())
        symbols: List[Dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            for kind, pattern in patterns:
                match = pattern.search(line)
                if not match:
                    continue
                symbol: Dict[str, Any] = {
                    "kind": kind,
                    "name": match.group(1),
                    "line": line_number,
                }
                if match.lastindex and match.lastindex >= 2 and match.group(2) is not None:
                    symbol["signature"] = self._short_signature(match.group(2))
                symbols.append(symbol)
                break
        return symbols

    def _iter_source_files(self, root: Path) -> Iterable[Path]:
        for current_root, directories, filenames in os.walk(root):
            current = Path(current_root)
            directories[:] = sorted(
                name for name in directories if not is_ignored(current / name)
            )
            for filename in sorted(filenames):
                path = current / filename
                if not is_ignored(path.relative_to(root)) and self._language(path):
                    yield path

    def _build_map(
        self, root: Path, max_files: int, max_symbols: int, max_output_chars: int
    ) -> Dict[str, Any]:
        files: List[Dict[str, Any]] = []
        language_counts: Dict[str, int] = {}
        symbol_count = 0
        output_chars = 0
        files_truncated = False
        symbols_truncated = False
        output_truncated = False
        skipped_large_files = 0

        for path in self._iter_source_files(root):
            if len(files) >= max_files:
                files_truncated = True
                break
            try:
                if path.stat().st_size > 2 * 1024 * 1024:
                    skipped_large_files += 1
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            language = self._language(path) or "text"
            lines = source.splitlines()
            if language == "python":
                symbols, parse_error = self._python_symbols(source)
            else:
                symbols = self._generic_symbols(language, lines)
                parse_error = None
            remaining_symbols = max_symbols - symbol_count
            if len(symbols) > remaining_symbols:
                symbols = symbols[: max(0, remaining_symbols)]
                symbols_truncated = True
            entry: Dict[str, Any] = {
                "path": relative,
                "language": language,
                "lines": len(lines),
                "symbols": symbols,
            }
            if parse_error:
                entry["parse_error"] = parse_error
            estimated = len(relative) + sum(
                len(symbol.get("name", "")) + len(symbol.get("signature", "")) + 50
                for symbol in symbols
            )
            if output_chars + estimated > max_output_chars:
                output_truncated = True
                break
            files.append(entry)
            output_chars += estimated
            symbol_count += len(symbols)
            language_counts[language] = language_counts.get(language, 0) + 1
            if symbol_count >= max_symbols:
                symbols_truncated = True
                break

        payload = {
            "root": str(root),
            "file_count": len(files),
            "symbol_count": symbol_count,
            "languages": language_counts,
            "files_truncated": files_truncated,
            "symbols_truncated": symbols_truncated,
            "output_truncated": output_truncated,
            "skipped_large_files": skipped_large_files,
            "files": files,
        }
        payload_budget = max(256, max_output_chars - 64)
        while files and len(json.dumps(payload, ensure_ascii=False)) > payload_budget:
            last_file = files[-1]
            if last_file["symbols"]:
                last_file["symbols"].pop()
                symbols_truncated = True
            else:
                files.pop()
            output_truncated = True
            symbol_count = sum(len(item["symbols"]) for item in files)
            language_counts = {}
            for item in files:
                language = item["language"]
                language_counts[language] = language_counts.get(language, 0) + 1
            payload.update(
                file_count=len(files),
                symbol_count=symbol_count,
                languages=language_counts,
                symbols_truncated=symbols_truncated,
                output_truncated=output_truncated,
            )
        payload["serialized_chars"] = 0
        for _ in range(3):
            payload["serialized_chars"] = len(
                json.dumps(payload, ensure_ascii=False)
            )
        return payload

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def get_repository_map(
        self,
        path: str = ".",
        max_files: int = 200,
        max_symbols: int = 1000,
    ) -> SkillResult:
        """Return a bounded cross-language map of files, symbols, lines and signatures.

        This lightweight map requires no persistent Code Graph index. Use the Code
        Graph separately when semantic docstring search or dependency tracing is
        needed.
        """

        if max_files < 1 or max_files > 1000:
            return SkillResult.fail("max_files must be between 1 and 1000.")
        if max_symbols < 1 or max_symbols > 5000:
            return SkillResult.fail("max_symbols must be between 1 and 5000.")
        try:
            safe_path = self.host_os.validate_path(path, is_write=False)
            if not safe_path.is_dir():
                return SkillResult.fail(f"Error: Path is not a directory ({path}).")
            result = await asyncio.to_thread(
                self._build_map,
                safe_path,
                max_files,
                max_symbols,
                self.host_os.config.file_read_max_chars * 3,
            )
            main_logger.info(
                f"[Host OS] Repository map: {result['file_count']} files, "
                f"{result['symbol_count']} symbols."
            )
            return SkillResult.ok(json.dumps(result, ensure_ascii=False))
        except PermissionError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error building repository map: {exc}")
