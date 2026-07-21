"""Token-efficient, cross-language repository context tools."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import threading
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

    _MAX_SOURCE_BYTES = 2 * 1024 * 1024

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
    _TREE_SITTER_LANGUAGES = {
        "c": "c",
        "cpp": "cpp",
        "csharp": "c_sharp",
        "go": "go",
        "java": "java",
        "javascript": "javascript",
        "kotlin": "kotlin",
        "php": "php",
        "ruby": "ruby",
        "rust": "rust",
        "swift": "swift",
        "typescript": "typescript",
    }
    _TREE_SITTER_DEFINITIONS = {
        "class_declaration": "class",
        "class_definition": "class",
        "enum_declaration": "enum",
        "enum_item": "enum",
        "function_declaration": "function",
        "function_definition": "function",
        "function_item": "function",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "method_definition": "method",
        "module": "module",
        "module_definition": "module",
        "record_declaration": "record",
        "struct_item": "struct",
        "trait_item": "trait",
        "type_alias_declaration": "type",
    }
    _TREE_SITTER_IDENTIFIERS = {
        "field_identifier",
        "identifier",
        "namespace_identifier",
        "property_identifier",
        "shorthand_property_identifier_pattern",
        "type_identifier",
    }

    def __init__(self, host_os_client: HostOSClient) -> None:
        self.host_os = host_os_client
        self._parser_cache: Dict[str, Tuple[Any, Optional[str]]] = {}
        self._parser_lock = threading.Lock()

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

    @staticmethod
    def _preview(lines: List[str], line_number: int, max_chars: int = 240) -> str:
        if line_number < 1 or line_number > len(lines):
            return ""
        preview = lines[line_number - 1].strip()
        return preview if len(preview) <= max_chars else preview[:max_chars] + "..."

    def _get_tree_sitter_parser(self, language: str) -> Tuple[Any, Optional[str]]:
        cached = self._parser_cache.get(language)
        if cached is not None:
            return cached
        parser_name = self._TREE_SITTER_LANGUAGES.get(language)
        if parser_name is None:
            result = (None, "language is not supported by tree-sitter adapter")
        else:
            try:
                from tree_sitter_languages import get_parser

                result = (get_parser(parser_name), None)
            except Exception as exc:
                result = (
                    None,
                    f"tree-sitter unavailable: {type(exc).__name__}: {exc}",
                )
        self._parser_cache[language] = result
        return result

    def _python_occurrences(
        self, source: str, symbol: str, include_references: bool
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [], f"SyntaxError at line {exc.lineno}: {exc.msg}"
        lines = source.splitlines()
        target = symbol.rsplit(".", maxsplit=1)[-1]
        require_qualified = "." in symbol
        occurrences: List[Dict[str, Any]] = []

        def walk_definitions(nodes: List[ast.stmt], owners: Tuple[str, ...] = ()) -> None:
            for node in nodes:
                if isinstance(node, ast.ClassDef):
                    qualified = ".".join((*owners, node.name))
                    if (qualified == symbol if require_qualified else node.name == target):
                        column = source.splitlines()[node.lineno - 1].find(
                            node.name, node.col_offset
                        )
                        occurrences.append(
                            {
                                "kind": "definition",
                                "symbol_kind": "class",
                                "qualified_name": qualified,
                                "line": node.lineno,
                                "column": max(0, column) + 1,
                                "end_line": getattr(node, "end_lineno", node.lineno),
                                "preview": self._preview(lines, node.lineno),
                                "backend": "python_ast",
                                "confidence": "syntactic",
                            }
                        )
                    walk_definitions(node.body, (*owners, node.name))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualified = ".".join((*owners, node.name))
                    if (qualified == symbol if require_qualified else node.name == target):
                        line_text = lines[node.lineno - 1]
                        column = line_text.find(node.name, node.col_offset)
                        occurrences.append(
                            {
                                "kind": "definition",
                                "symbol_kind": (
                                    "async_function"
                                    if isinstance(node, ast.AsyncFunctionDef)
                                    else "function"
                                ),
                                "qualified_name": qualified,
                                "line": node.lineno,
                                "column": max(0, column) + 1,
                                "end_line": getattr(node, "end_lineno", node.lineno),
                                "preview": self._preview(lines, node.lineno),
                                "backend": "python_ast",
                                "confidence": "syntactic",
                            }
                        )
                    walk_definitions(node.body, (*owners, node.name))

        walk_definitions(tree.body)
        if include_references:
            seen = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == target:
                    location = (node.lineno, node.col_offset + 1)
                elif isinstance(node, ast.Attribute) and node.attr == target:
                    location = (
                        node.lineno,
                        max(node.col_offset, getattr(node, "end_col_offset", 0) - len(target))
                        + 1,
                    )
                else:
                    continue
                if location in seen:
                    continue
                seen.add(location)
                occurrences.append(
                    {
                        "kind": "reference",
                        "line": location[0],
                        "column": location[1],
                        "preview": self._preview(lines, location[0]),
                        "backend": "python_ast",
                        "confidence": "syntactic",
                    }
                )
        return occurrences, None

    def _tree_sitter_occurrences(
        self,
        source: str,
        language: str,
        symbol: str,
        include_references: bool,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        parser, error = self._get_tree_sitter_parser(language)
        if parser is None:
            return [], error
        source_bytes = source.encode("utf-8")
        lines = source.splitlines()
        target = symbol.rsplit(".", maxsplit=1)[-1]
        occurrences: List[Dict[str, Any]] = []
        definition_spans = set()
        try:
            with self._parser_lock:
                root = parser.parse(source_bytes).root_node
            stack = [root]
            nodes = []
            while stack:
                node = stack.pop()
                nodes.append(node)
                stack.extend(reversed(node.children))
            for node in nodes:
                symbol_kind = self._TREE_SITTER_DEFINITIONS.get(node.type)
                if symbol_kind is None:
                    continue
                name_node = node.child_by_field_name("name")
                if name_node is None:
                    continue
                name = source_bytes[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                if name != target:
                    continue
                definition_spans.add((name_node.start_byte, name_node.end_byte))
                occurrences.append(
                    {
                        "kind": "definition",
                        "symbol_kind": symbol_kind,
                        "qualified_name": name,
                        "line": name_node.start_point[0] + 1,
                        "column": name_node.start_point[1] + 1,
                        "end_line": node.end_point[0] + 1,
                        "preview": self._preview(lines, name_node.start_point[0] + 1),
                        "backend": "tree_sitter",
                        "confidence": "syntactic",
                    }
                )
            if include_references:
                for node in nodes:
                    if node.type not in self._TREE_SITTER_IDENTIFIERS:
                        continue
                    span = (node.start_byte, node.end_byte)
                    if span in definition_spans:
                        continue
                    name = source_bytes[node.start_byte : node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    if name == target:
                        occurrences.append(
                            {
                                "kind": "reference",
                                "line": node.start_point[0] + 1,
                                "column": node.start_point[1] + 1,
                                "preview": self._preview(lines, node.start_point[0] + 1),
                                "backend": "tree_sitter",
                                "confidence": "syntactic",
                            }
                        )
            return occurrences, None
        except Exception as exc:
            return [], f"tree-sitter parse failed: {type(exc).__name__}: {exc}"

    def _lexical_occurrences(
        self,
        source: str,
        language: str,
        symbol: str,
        include_references: bool,
    ) -> List[Dict[str, Any]]:
        lines = source.splitlines()
        target = symbol.rsplit(".", maxsplit=1)[-1]
        token_pattern = re.compile(rf"(?<![\w$]){re.escape(target)}(?![\w$])")
        definitions: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for definition in self._generic_symbols(language, lines):
            if definition["name"] != target:
                continue
            line_number = definition["line"]
            column = lines[line_number - 1].find(target) + 1
            definitions[(line_number, column)] = {
                "kind": "definition",
                "symbol_kind": definition["kind"],
                "qualified_name": target,
                "line": line_number,
                "column": column,
                "preview": self._preview(lines, line_number),
                "backend": "lexical_fallback",
                "confidence": "lexical",
            }
        occurrences = list(definitions.values())
        if include_references:
            for line_number, line in enumerate(lines, start=1):
                for match in token_pattern.finditer(line):
                    location = (line_number, match.start() + 1)
                    if location in definitions:
                        continue
                    occurrences.append(
                        {
                            "kind": "reference",
                            "line": line_number,
                            "column": match.start() + 1,
                            "preview": self._preview(lines, line_number),
                            "backend": "lexical_fallback",
                            "confidence": "lexical",
                        }
                    )
        return occurrences

    def _find_symbol(
        self,
        root: Path,
        symbol: str,
        include_references: bool,
        max_files: int,
        max_results: int,
        max_output_chars: int,
    ) -> Dict[str, Any]:
        definitions: List[Dict[str, Any]] = []
        references: List[Dict[str, Any]] = []
        backend_counts: Dict[str, int] = {}
        fallback_reasons = set()
        match_counts_truncated = False
        files_scanned = 0
        files_truncated = False
        skipped_large_files = 0

        for path in self._iter_source_files(root):
            if files_scanned >= max_files:
                files_truncated = True
                break
            try:
                if path.stat().st_size > self._MAX_SOURCE_BYTES:
                    skipped_large_files += 1
                    continue
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files_scanned += 1
            language = self._language(path) or "text"
            if language == "python":
                occurrences, error = self._python_occurrences(
                    source, symbol, include_references
                )
                backend = "python_ast"
            else:
                occurrences, error = self._tree_sitter_occurrences(
                    source, language, symbol, include_references
                )
                backend = "tree_sitter"
            if error:
                fallback_reasons.add(error[:240])
                occurrences = self._lexical_occurrences(
                    source, language, symbol, include_references
                )
                backend = "lexical_fallback"
            backend_counts[backend] = backend_counts.get(backend, 0) + 1
            relative = path.relative_to(root).as_posix()
            for occurrence in occurrences:
                occurrence["path"] = relative
                bucket = definitions if occurrence["kind"] == "definition" else references
                if len(bucket) <= max_results:
                    bucket.append(occurrence)
                else:
                    match_counts_truncated = True

        all_results = definitions + references
        results_truncated = match_counts_truncated or len(all_results) > max_results
        results = all_results[:max_results]
        payload: Dict[str, Any] = {
            "root": str(root),
            "symbol": symbol,
            "include_references": include_references,
            "files_scanned": files_scanned,
            "files_truncated": files_truncated,
            "skipped_large_files": skipped_large_files,
            "definition_count": len(definitions),
            "reference_count": len(references),
            "match_counts_truncated": match_counts_truncated,
            "backend_counts": backend_counts,
            "fallback_reasons": sorted(fallback_reasons)[:5],
            "results_truncated": results_truncated,
            "output_truncated": False,
            "results": results,
        }
        payload_budget = max(512, max_output_chars - 64)
        while results and len(json.dumps(payload, ensure_ascii=False)) > payload_budget:
            results.pop()
            payload["results_truncated"] = True
            payload["output_truncated"] = True
        payload["serialized_chars"] = 0
        for _ in range(3):
            payload["serialized_chars"] = len(json.dumps(payload, ensure_ascii=False))
        return payload

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
                if path.stat().st_size > self._MAX_SOURCE_BYTES:
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

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def locate_code_symbol(
        self,
        symbol: str,
        path: str = ".",
        include_references: bool = True,
        max_files: int = 500,
        max_results: int = 100,
    ) -> SkillResult:
        """Locate bounded symbol definitions and usages without requiring an index.

        Python uses its AST, compatible tree-sitter installations serve other
        languages, and an explicitly labeled lexical fallback preserves
        availability. Results are definition-first and include precise ranges,
        backend confidence, and truncation diagnostics. This is occurrence
        navigation, not project-wide type/name resolution.
        """

        symbol = symbol.strip()
        parts = symbol.split(".")
        if not symbol or len(symbol) > 128 or any(
            not re.fullmatch(r"[A-Za-z_$][\w$]*", part) for part in parts
        ):
            return SkillResult.fail(
                "symbol must be a dotted identifier of at most 128 characters."
            )
        if max_files < 1 or max_files > 2000:
            return SkillResult.fail("max_files must be between 1 and 2000.")
        if max_results < 1 or max_results > 1000:
            return SkillResult.fail("max_results must be between 1 and 1000.")
        try:
            safe_path = self.host_os.validate_path(path, is_write=False)
            if not safe_path.is_dir():
                return SkillResult.fail(f"Error: Path is not a directory ({path}).")
            result = await asyncio.to_thread(
                self._find_symbol,
                safe_path,
                symbol,
                include_references,
                max_files,
                max_results,
                self.host_os.config.file_read_max_chars * 2,
            )
            main_logger.info(
                f"[Host OS] Symbol '{symbol}': {result['definition_count']} "
                f"definitions, {result['reference_count']} references."
            )
            return SkillResult.ok(json.dumps(result, ensure_ascii=False))
        except PermissionError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error locating code symbol: {exc}")
