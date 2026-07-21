"""
Skills for targeted editing (patching) of files.
Saves context tokens and reduces the risk of file corruption compared to full rewrites.
"""

import asyncio
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.logger import main_logger

from src.l2_interfaces.host.os.client import HostOSClient, HostOSAccessLevel
from src.l2_interfaces.host.os.decorators import require_access

from src.l3_agent.swarm.roles import Subagents

from src.l3_agent.skills.registry import SkillResult, skill


class HostOSEditor:
    """Skills for targeted code editing."""

    def __init__(self, host_os_client: HostOSClient):
        self.host_os = host_os_client
        self.checkpoints_dir = self.host_os.system_dir / "checkpoints"
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sha256(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        """Replace a file atomically using a temporary file in the same directory."""

        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(file_descriptor, "wb") as temp_file:
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            if path.exists():
                os.chmod(temp_path, path.stat().st_mode)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _create_checkpoint(
        self, target: Path, before: bytes, after_sha256: str
    ) -> str:
        checkpoint_id = uuid.uuid4().hex
        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        checkpoint_dir.mkdir(parents=False, exist_ok=False)
        (checkpoint_dir / "content.bin").write_bytes(before)
        manifest = {
            "version": 1,
            "checkpoint_id": checkpoint_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target": str(target),
            "before_sha256": self._sha256(before),
            "after_sha256": after_sha256,
        }
        (checkpoint_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return checkpoint_id

    def _load_checkpoint(self, checkpoint_id: str) -> tuple[Dict[str, Any], bytes]:
        if not checkpoint_id or any(
            char not in "0123456789abcdef" for char in checkpoint_id
        ):
            raise ValueError("Invalid checkpoint_id.")
        if len(checkpoint_id) != 32:
            raise ValueError("Invalid checkpoint_id.")

        checkpoint_dir = self.checkpoints_dir / checkpoint_id
        manifest_path = checkpoint_dir / "manifest.json"
        content_path = checkpoint_dir / "content.bin"
        if not manifest_path.is_file() or not content_path.is_file():
            raise FileNotFoundError(f"Checkpoint not found ({checkpoint_id}).")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("checkpoint_id") != checkpoint_id:
            raise ValueError("Checkpoint manifest identity mismatch.")
        content = content_path.read_bytes()
        if self._sha256(content) != manifest.get("before_sha256"):
            raise ValueError("Checkpoint content checksum mismatch.")
        return manifest, content

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def delete_lines_matching(
        self, filepath: str, match_string: str, exact_match: bool = False
    ) -> SkillResult:
        """
        Deletes lines containing 'match_string'.

        exact_match: Requires full string match (ignoring edge whitespace).
        """

        try:
            safe_path = self.host_os.validate_path(filepath, is_write=True)
            if not safe_path.is_file():
                return SkillResult.fail(f"Error: File not found ({filepath}).")

            def _delete():
                with open(safe_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()

                new_lines = []
                deleted_count = 0

                for line in lines:
                    if exact_match:
                        if line.strip() == match_string.strip():
                            deleted_count += 1
                            continue
                    else:
                        if match_string in line:
                            deleted_count += 1
                            continue
                    new_lines.append(line)

                if deleted_count == 0:
                    return False, "No matches found. No lines deleted."

                with open(safe_path, "w", encoding="utf-8") as f:
                    f.writelines(new_lines)

                return True, f"Successfully deleted lines: {deleted_count}."

            is_success, msg = await asyncio.to_thread(_delete)

            if is_success:
                main_logger.info(f"[Host OS] Deleted lines in file: {safe_path.name}")
                return SkillResult.ok("True")
            else:
                return SkillResult.fail(msg)

        except PermissionError as e:
            return SkillResult.fail(str(e))
        except Exception as e:
            return SkillResult.fail(f"Error deleting lines: {e}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def patch_file(
        self, filepath: str, search_block: str, replace_block: str
    ) -> SkillResult:
        """
        Targeted file modification. Saves tokens/reduces corruption risk vs full rewrite.

        search_block: Exact fragment to replace.
        replace_block: New code insert.
        """

        if not search_block:
            return SkillResult.fail("Error: search_block cannot be empty.")

        try:
            safe_path = self.host_os.validate_path(filepath, is_write=True)
            if not safe_path.is_file():
                return SkillResult.fail(f"Error: File not found ({filepath}).")

            def _patch():
                with open(safe_path, "r", encoding="utf-8") as f:
                    content = f.read()

                if search_block not in content:
                    clean_search = search_block.replace("\r\n", "\n").strip()
                    clean_content = content.replace("\r\n", "\n")

                    if clean_search not in clean_content:
                        return (
                            False,
                            "The block to search for (search_block) was not found in the file.",
                        )

                    new_content = clean_content.replace(clean_search, replace_block.strip())
                else:
                    new_content = content.replace(search_block, replace_block)

                self._atomic_write(safe_path, new_content.encode("utf-8"))

                return True, "File successfully patched."

            is_success, msg = await asyncio.to_thread(_patch)

            if is_success:
                main_logger.info(f"[Host OS] Patched file: {safe_path.name}")
                return SkillResult.ok("True")
            else:
                return SkillResult.fail(msg)

        except PermissionError as e:
            return SkillResult.fail(str(e))
        except Exception as e:
            return SkillResult.fail(f"Error patching file: {e}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def apply_file_patch(
        self,
        filepath: str,
        edits: List[Dict[str, str]],
        expected_sha256: Optional[str] = None,
    ) -> SkillResult:
        """Apply exact, single-match edits atomically and create a rollback checkpoint.

        Each edit must contain ``search`` and ``replace`` strings. Every search block
        must match exactly once in the progressively edited file. ``expected_sha256``
        enables optimistic concurrency and rejects edits based on stale file content.
        """

        try:
            safe_path = self.host_os.validate_path(filepath, is_write=True)
            if not safe_path.is_file():
                return SkillResult.fail(f"Error: File not found ({filepath}).")
            if not edits:
                return SkillResult.fail("Error: edits cannot be empty.")
            if len(edits) > 100:
                return SkillResult.fail("Error: a patch is limited to 100 edits.")

            def _apply() -> SkillResult:
                before = safe_path.read_bytes()
                before_sha256 = self._sha256(before)
                if expected_sha256 and expected_sha256.lower() != before_sha256:
                    return SkillResult.fail(
                        "Patch rejected: file changed since it was read "
                        f"(expected {expected_sha256.lower()}, "
                        f"current {before_sha256})."
                    )

                try:
                    updated = before.decode("utf-8")
                except UnicodeDecodeError:
                    return SkillResult.fail(
                        "Patch rejected: file is not valid UTF-8 text."
                    )

                for index, edit in enumerate(edits, start=1):
                    if not isinstance(edit, dict):
                        return SkillResult.fail(
                            f"Patch rejected: edit {index} must be an object."
                        )
                    search = edit.get("search")
                    replace = edit.get("replace")
                    if not isinstance(search, str) or not search:
                        return SkillResult.fail(
                            f"Patch rejected: edit {index} has an empty or invalid "
                            "search block."
                        )
                    if not isinstance(replace, str):
                        return SkillResult.fail(
                            f"Patch rejected: edit {index} has an invalid replace "
                            "block."
                        )

                    occurrences = updated.count(search)
                    if occurrences != 1:
                        return SkillResult.fail(
                            f"Patch rejected: edit {index} expected exactly one match, "
                            f"found {occurrences}. No changes were written."
                        )
                    updated = updated.replace(search, replace, 1)

                after = updated.encode("utf-8")
                after_sha256 = self._sha256(after)
                if after == before:
                    return SkillResult.fail("Patch rejected: edits produce no changes.")

                checkpoint_id = self._create_checkpoint(
                    safe_path, before, after_sha256
                )
                self._atomic_write(safe_path, after)
                payload = {
                    "checkpoint_id": checkpoint_id,
                    "filepath": filepath,
                    "edits_applied": len(edits),
                    "before_sha256": before_sha256,
                    "after_sha256": after_sha256,
                }
                return SkillResult.ok(json.dumps(payload, ensure_ascii=False))

            result = await asyncio.to_thread(_apply)
            if result.is_success:
                main_logger.info(f"[Host OS] Safely patched file: {safe_path.name}")
            return result

        except PermissionError as e:
            return SkillResult.fail(str(e))
        except Exception as e:
            return SkillResult.fail(f"Error applying safe file patch: {e}")

    @skill(swarm=[Subagents.CODER, Subagents.QA_ENGINEER, Subagents.SYSADMIN])
    @require_access(HostOSAccessLevel.SANDBOX)
    async def restore_file_checkpoint(
        self,
        checkpoint_id: str,
        expected_current_sha256: Optional[str] = None,
    ) -> SkillResult:
        """Restore a file checkpoint unless the file changed after the patch.

        By default the checkpoint's recorded post-patch checksum is used as the
        concurrency guard. Pass ``expected_current_sha256`` only when restoring a
        known later state; unrelated newer changes are never overwritten silently.
        """

        try:
            def _restore() -> SkillResult:
                manifest, content = self._load_checkpoint(checkpoint_id)
                safe_path = self.host_os.validate_path(
                    manifest["target"], is_write=True
                )
                if not safe_path.is_file():
                    return SkillResult.fail(
                        f"Restore rejected: target file no longer exists ({safe_path})."
                    )

                current = safe_path.read_bytes()
                current_sha256 = self._sha256(current)
                required_sha256 = (
                    expected_current_sha256.lower()
                    if expected_current_sha256
                    else manifest["after_sha256"]
                )
                if current_sha256 != required_sha256:
                    return SkillResult.fail(
                        "Restore rejected: target changed after the checkpoint "
                        f"(expected {required_sha256}, current {current_sha256})."
                    )

                self._atomic_write(safe_path, content)
                payload = {
                    "checkpoint_id": checkpoint_id,
                    "filepath": str(safe_path),
                    "restored_sha256": manifest["before_sha256"],
                }
                return SkillResult.ok(json.dumps(payload, ensure_ascii=False))

            result = await asyncio.to_thread(_restore)
            if result.is_success:
                main_logger.info(
                    f"[Host OS] Restored file checkpoint: {checkpoint_id}"
                )
            return result

        except PermissionError as e:
            return SkillResult.fail(str(e))
        except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as e:
            return SkillResult.fail(f"Restore rejected: {e}")
        except Exception as e:
            return SkillResult.fail(f"Error restoring file checkpoint: {e}")
