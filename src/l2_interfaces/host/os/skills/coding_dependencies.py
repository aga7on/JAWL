"""Bounded, zero-index source dependency slices."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from src.l2_interfaces.host.os.client import HostOSAccessLevel, HostOSClient
from src.l2_interfaces.host.os.decorators import require_access
from src.l2_interfaces.host.os.polls.utils import is_ignored
from src.l3_agent.skills.registry import SkillResult, skill
from src.l3_agent.swarm.roles import Subagents
from src.utils.logger import main_logger


class HostOSCodingDependencies:
    """Build local import/include slices without a persistent graph."""

    _MAX_SOURCE_BYTES = 2 * 1024 * 1024
    _MAX_SCAN_FILES = 2000
    _SOURCE_SUFFIXES = {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".js",
        ".jsx",
        ".py",
        ".ts",
        ".tsx",
    }
    _RELATIVE_MODULE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")
    _JS_IMPORT = re.compile(
        r"(?:\bfrom\s+|\brequire\s*\(\s*|\bimport\s*(?:\(\s*)?)[\"']([^\"']+)[\"']"
    )
    _C_INCLUDE = re.compile(r'^\s*#\s*include\s+"([^"]+)"')

    def __init__(self, host_os_client: HostOSClient) -> None:
        self.host_os = host_os_client

    def _iter_source_files(self, root: Path) -> Iterable[Path]:
        for current_root, directories, filenames in os.walk(root):
            current = Path(current_root)
            directories[:] = sorted(
                name for name in directories if not is_ignored(current / name)
            )
            for filename in sorted(filenames):
                path = current / filename
                relative = path.relative_to(root)
                if path.suffix.lower() in self._SOURCE_SUFFIXES and not is_ignored(
                    relative
                ):
                    yield path

    @staticmethod
    def _python_module(relative: Path) -> str:
        without_suffix = relative.with_suffix("")
        parts = list(without_suffix.parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @staticmethod
    def _resolve_python_module(
        module: str, module_map: Dict[str, str]
    ) -> Optional[str]:
        candidate = module
        while candidate:
            target = module_map.get(candidate)
            if target is not None:
                return target
            candidate = candidate.rpartition(".")[0]
        return None

    def _python_edges(
        self,
        source_path: str,
        source: str,
        module_map: Dict[str, str],
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [], f"{source_path}:{exc.lineno}: {exc.msg}"
        relative = Path(source_path)
        current_module = self._python_module(relative)
        current_package = (
            current_module
            if relative.name == "__init__.py"
            else current_module.rpartition(".")[0]
        )
        edges: List[Dict[str, Any]] = []
        seen = set()

        def add(target: Optional[str], line: int, imported: str) -> None:
            if target is None or target == source_path:
                return
            key = (target, line, imported)
            if key in seen:
                return
            seen.add(key)
            edges.append(
                {
                    "source": source_path,
                    "target": target,
                    "kind": "python_import",
                    "line": line,
                    "import": imported,
                }
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(
                        self._resolve_python_module(alias.name, module_map),
                        node.lineno,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package_parts = current_package.split(".") if current_package else []
                    keep = max(0, len(package_parts) - (node.level - 1))
                    prefix_parts = package_parts[:keep]
                    if node.module:
                        prefix_parts.extend(node.module.split("."))
                    base_module = ".".join(prefix_parts)
                else:
                    base_module = node.module or ""
                for alias in node.names:
                    imported = (
                        f"{base_module}.{alias.name}" if base_module else alias.name
                    )
                    target = module_map.get(imported)
                    if target is None:
                        target = self._resolve_python_module(base_module, module_map)
                    add(target, node.lineno, imported)
        return edges, None

    def _resolve_relative_module(
        self, root: Path, source: Path, requested: str
    ) -> Optional[str]:
        if not requested.startswith("."):
            return None
        base = (source.parent / requested).resolve()
        if not base.is_relative_to(root):
            return None
        candidates = [base]
        if not base.suffix:
            candidates.extend(base.with_suffix(suffix) for suffix in self._RELATIVE_MODULE_SUFFIXES)
            candidates.extend(base / f"index{suffix}" for suffix in self._RELATIVE_MODULE_SUFFIXES)
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() in self._SOURCE_SUFFIXES:
                return candidate.relative_to(root).as_posix()
        return None

    def _generic_edges(
        self, root: Path, path: Path, source: str
    ) -> List[Dict[str, Any]]:
        relative = path.relative_to(root).as_posix()
        edges: List[Dict[str, Any]] = []
        suffix = path.suffix.lower()
        for line_number, line in enumerate(source.splitlines(), start=1):
            if suffix in {".js", ".jsx", ".ts", ".tsx"}:
                matches = self._JS_IMPORT.finditer(line)
                kind = "relative_module"
            elif suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
                match = self._C_INCLUDE.search(line)
                matches = [match] if match else []
                kind = "local_include"
            else:
                continue
            for match in matches:
                requested = match.group(1)
                if kind == "local_include":
                    candidate = (path.parent / requested).resolve()
                    target = (
                        candidate.relative_to(root).as_posix()
                        if candidate.is_relative_to(root) and candidate.is_file()
                        else None
                    )
                else:
                    target = self._resolve_relative_module(root, path, requested)
                if target and target != relative:
                    edges.append(
                        {
                            "source": relative,
                            "target": target,
                            "kind": kind,
                            "line": line_number,
                            "import": requested,
                        }
                    )
        return edges

    def _build_slice(
        self,
        root: Path,
        entry: Path,
        direction: str,
        max_depth: int,
        max_files: int,
        max_output_chars: int,
    ) -> Dict[str, Any]:
        source_paths: List[Path] = [entry]
        index_truncated = False
        skipped_large_files = 0
        for path in self._iter_source_files(root):
            if path == entry:
                continue
            if len(source_paths) >= self._MAX_SCAN_FILES:
                index_truncated = True
                break
            try:
                if path.stat().st_size > self._MAX_SOURCE_BYTES:
                    skipped_large_files += 1
                    continue
            except OSError:
                continue
            source_paths.append(path)

        module_map = {
            self._python_module(path.relative_to(root)): path.relative_to(root).as_posix()
            for path in source_paths
            if path.suffix.lower() == ".py"
        }
        all_edges: List[Dict[str, Any]] = []
        parse_errors: List[str] = []
        for path in source_paths:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix.lower() == ".py":
                edges, error = self._python_edges(relative, source, module_map)
                all_edges.extend(edges)
                if error:
                    parse_errors.append(error[:240])
            else:
                all_edges.extend(self._generic_edges(root, path, source))

        outgoing: Dict[str, List[Dict[str, Any]]] = {}
        incoming: Dict[str, List[Dict[str, Any]]] = {}
        for edge in all_edges:
            outgoing.setdefault(edge["source"], []).append(edge)
            incoming.setdefault(edge["target"], []).append(edge)

        entry_relative = entry.relative_to(root).as_posix()
        depths = {entry_relative: 0}
        relations: Dict[str, set[str]] = {entry_relative: {"entry"}}
        queue = deque([entry_relative])
        slice_truncated = False
        while queue:
            current = queue.popleft()
            depth = depths[current]
            if depth >= max_depth:
                continue
            candidates: List[Tuple[Dict[str, Any], str, str]] = []
            if direction in {"dependencies", "both"}:
                candidates.extend(
                    (edge, edge["target"], "dependency")
                    for edge in outgoing.get(current, [])
                )
            if direction in {"dependents", "both"}:
                candidates.extend(
                    (edge, edge["source"], "dependent")
                    for edge in incoming.get(current, [])
                )
            for _, neighbor, relation in sorted(
                candidates, key=lambda item: (item[1], item[0]["line"])
            ):
                relations.setdefault(neighbor, set()).add(relation)
                if neighbor in depths:
                    continue
                if len(depths) >= max_files:
                    slice_truncated = True
                    continue
                depths[neighbor] = depth + 1
                queue.append(neighbor)

        selected = set(depths)
        edges = [
            edge
            for edge in all_edges
            if edge["source"] in selected and edge["target"] in selected
        ]
        edges.sort(key=lambda edge: (edge["source"], edge["target"], edge["line"]))
        nodes = [
            {
                "path": path,
                "depth": depth,
                "relations": sorted(relations.get(path, set())),
            }
            for path, depth in sorted(depths.items(), key=lambda item: (item[1], item[0]))
        ]
        payload: Dict[str, Any] = {
            "root": str(root),
            "entry_file": entry_relative,
            "direction": direction,
            "max_depth": max_depth,
            "indexed_file_count": len(source_paths),
            "index_truncated": index_truncated,
            "slice_truncated": slice_truncated,
            "skipped_large_files": skipped_large_files,
            "parse_errors": parse_errors[:10],
            "node_count": len(nodes),
            "edge_count": len(edges),
            "output_truncated": False,
            "nodes": nodes,
            "edges": edges,
        }
        payload_budget = max(512, max_output_chars - 64)
        while edges and len(json.dumps(payload, ensure_ascii=False)) > payload_budget:
            edges.pop()
            payload["output_truncated"] = True
        while len(nodes) > 1 and len(json.dumps(payload, ensure_ascii=False)) > payload_budget:
            removed = nodes.pop()
            edges[:] = [
                edge
                for edge in edges
                if removed["path"] not in {edge["source"], edge["target"]}
            ]
            payload["output_truncated"] = True
        payload["returned_node_count"] = len(nodes)
        payload["returned_edge_count"] = len(edges)
        payload["serialized_chars"] = 0
        for _ in range(3):
            payload["serialized_chars"] = len(json.dumps(payload, ensure_ascii=False))
        return payload

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def get_code_dependency_slice(
        self,
        entry_file: str,
        path: str = ".",
        direction: str = "both",
        max_depth: int = 2,
        max_files: int = 100,
    ) -> SkillResult:
        """Return a bounded local dependency/dependent slice around one source file."""

        if direction not in {"dependencies", "dependents", "both"}:
            return SkillResult.fail(
                "direction must be 'dependencies', 'dependents', or 'both'."
            )
        if max_depth < 1 or max_depth > 10:
            return SkillResult.fail("max_depth must be between 1 and 10.")
        if max_files < 1 or max_files > 500:
            return SkillResult.fail("max_files must be between 1 and 500.")
        try:
            root = self.host_os.validate_path(path, is_write=False)
            if not root.is_dir():
                return SkillResult.fail(f"Error: Path is not a directory ({path}).")
            entry = (root / entry_file).resolve()
            self.host_os.validate_path(entry, is_write=False)
            if not entry.is_relative_to(root) or not entry.is_file():
                return SkillResult.fail("entry_file must be a file inside path.")
            if entry.suffix.lower() not in self._SOURCE_SUFFIXES:
                return SkillResult.fail("entry_file is not a supported source file.")
            result = await asyncio.to_thread(
                self._build_slice,
                root,
                entry,
                direction,
                max_depth,
                max_files,
                self.host_os.config.file_read_max_chars * 2,
            )
            main_logger.info(
                f"[Host OS] Dependency slice '{result['entry_file']}': "
                f"{result['node_count']} files, {result['edge_count']} edges."
            )
            return SkillResult.ok(json.dumps(result, ensure_ascii=False))
        except PermissionError as exc:
            return SkillResult.fail(str(exc))
        except Exception as exc:
            return SkillResult.fail(f"Error building dependency slice: {exc}")
