import hashlib
import json

import pytest

from src.l2_interfaces.host.os.skills.files.reader import HostOSReader
from src.l2_interfaces.host.os.skills.files.writer import HostOSWriter
from src.l2_interfaces.host.os.skills.files.editor import HostOSEditor
from src.l2_interfaces.host.os.skills.files.workspace import HostOSWorkspace


@pytest.mark.asyncio
async def test_os_files_write_and_read(os_client):
    writer = HostOSWriter(os_client)
    reader = HostOSReader(os_client)
    filepath = str(os_client.sandbox_dir / "hello.txt")

    res_write = await writer.write_file(filepath, "Hello World")
    assert res_write.is_success is True

    res_read = await reader.read_file(filepath)
    assert res_read.is_success is True
    assert "Hello World" in res_read.message
    assert hashlib.sha256(b"Hello World").hexdigest() in res_read.message


@pytest.mark.asyncio
async def test_os_files_delete_out_of_bounds(os_client):
    """Тест: агент не должен иметь возможности удалять файлы вне песочницы на 1 уровне."""
    writer = HostOSWriter(os_client)
    forbidden_path = str(os_client.framework_dir / "main.py")

    res_del = await writer.delete_file(forbidden_path)
    assert res_del.is_success is False
    assert "OBSERVER: Writing is permitted strictly inside" in res_del.message


@pytest.mark.asyncio
async def test_os_files_delete_directory(os_client):
    """Тест: успешное рекурсивное удаление папки."""
    writer = HostOSWriter(os_client)

    # Создаем папку и файл внутри
    target_dir = os_client.sandbox_dir / "target_folder"
    target_dir.mkdir()
    (target_dir / "inner_file.txt").touch()

    # Удаляем
    res = await writer.delete_directory(str(target_dir))

    assert res.is_success is True
    assert not target_dir.exists()


@pytest.mark.asyncio
async def test_os_files_delete_directory_root_protection(os_client):
    """Тест: попытка удалить корень песочницы или фреймворка блокируется."""
    writer = HostOSWriter(os_client)

    # Пытаемся снести всю песочницу
    res = await writer.delete_directory(str(os_client.sandbox_dir))

    assert res.is_success is False
    assert "Deleting the root sandbox or framework directory is forbidden" in res.message
    assert os_client.sandbox_dir.exists()  # Папка должна выжить


@pytest.mark.asyncio
async def test_os_files_create_directories(os_client):
    """Тест: массовое создание вложенных директорий."""
    writer = HostOSWriter(os_client)

    # Передаем два пути. Один простой, второй вложенный
    paths = [
        str(os_client.sandbox_dir / "docs"),
        str(os_client.sandbox_dir / "src" / "api" / "v1"),
    ]

    res = await writer.create_directories(paths)

    assert res.is_success is True
    assert (os_client.sandbox_dir / "docs").exists()
    assert (os_client.sandbox_dir / "src" / "api" / "v1").exists()
    assert res.message == "True"


@pytest.mark.asyncio
async def test_os_files_open_and_close_workspace(os_client):
    """Тест: вкладки редактора (open_file / close_file)."""
    workspace = HostOSWorkspace(os_client)
    target = os_client.sandbox_dir / "editor_test.py"
    target.touch()

    # Открытие
    res_open = await workspace.open_file("sandbox/editor_test.py")
    assert res_open.is_success is True, res_open.message
    assert "editor_test.py" in os_client.state.opened_workspace_files

    # Превышение лимита
    os_client.config.workspace_max_opened_files = 1
    target2 = os_client.sandbox_dir / "editor_test2.py"
    target2.touch()
    res_limit = await workspace.open_file("sandbox/editor_test2.py")
    assert res_limit.is_success is False
    assert "Maximum number of files are already open" in res_limit.message

    # Закрытие
    res_close = await workspace.close_file("sandbox/editor_test.py")
    assert res_close.is_success is True
    assert "editor_test.py" not in os_client.state.opened_workspace_files


@pytest.mark.asyncio
async def test_os_files_open_directory_recursive(os_client):
    """Тест: массовое открытие файлов с фильтрацией мусора."""
    workspace = HostOSWorkspace(os_client)

    # Создаем структуру
    sub_dir = os_client.sandbox_dir / "src"
    sub_dir.mkdir()
    (sub_dir / "main.py").touch()
    (sub_dir / "image.png").touch()  # Должно проигнорироваться

    git_dir = os_client.sandbox_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").touch()  # Должно проигнорироваться

    res = await workspace.open_directory_workspace(path=".", recursive=True)

    assert res.is_success is True
    assert "src/main.py" in os_client.state.opened_workspace_files
    assert "src/image.png" not in os_client.state.opened_workspace_files
    assert ".git/config" not in os_client.state.opened_workspace_files


@pytest.mark.asyncio
async def test_os_files_patch_file(os_client):
    """Тест: точечная замена куска кода."""
    editor = HostOSEditor(os_client)
    target = os_client.sandbox_dir / "script.py"
    target.write_text("def sum(a, b):\n    return a - b\n", encoding="utf-8")

    # Успешный патч
    res_patch = await editor.patch_file(
        filepath="sandbox/script.py",
        search_block="    return a - b",
        replace_block="    return a + b",
    )

    assert res_patch.is_success is True
    assert "return a + b" in target.read_text(encoding="utf-8")

    # Патч с ошибкой (блок not found)
    res_fail = await editor.patch_file("sandbox/script.py", "return a * b", "return a / b")
    assert res_fail.is_success is False
    assert "not found" in res_fail.message


@pytest.mark.asyncio
async def test_safe_file_patch_is_precise_and_reversible(os_client):
    editor = HostOSEditor(os_client)
    target = os_client.sandbox_dir / "safe_patch.py"
    original = "value = 1\nlabel = 'before'\n"
    target.write_text(original, encoding="utf-8")
    original_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()

    result = await editor.apply_file_patch(
        filepath="sandbox/safe_patch.py",
        edits=[
            {"search": "value = 1", "replace": "value = 2"},
            {"search": "label = 'before'", "replace": "label = 'after'"},
        ],
        expected_sha256=original_sha256,
    )

    assert result.is_success is True
    payload = json.loads(result.message)
    assert payload["edits_applied"] == 2
    assert payload["before_sha256"] == original_sha256
    assert target.read_text(encoding="utf-8") == "value = 2\nlabel = 'after'\n"

    restored = await editor.restore_file_checkpoint(payload["checkpoint_id"])

    assert restored.is_success is True
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_safe_file_patch_rejects_ambiguous_or_stale_edits(os_client):
    editor = HostOSEditor(os_client)
    target = os_client.sandbox_dir / "ambiguous.py"
    original = "flag = False\nflag = False\n"
    target.write_text(original, encoding="utf-8")

    ambiguous = await editor.apply_file_patch(
        filepath="sandbox/ambiguous.py",
        edits=[{"search": "flag = False", "replace": "flag = True"}],
    )
    stale = await editor.apply_file_patch(
        filepath="sandbox/ambiguous.py",
        edits=[{"search": original, "replace": "flag = True\n"}],
        expected_sha256="0" * 64,
    )

    assert ambiguous.is_success is False
    assert "found 2" in ambiguous.message
    assert stale.is_success is False
    assert "file changed since it was read" in stale.message
    assert target.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_checkpoint_restore_refuses_to_overwrite_newer_changes(os_client):
    editor = HostOSEditor(os_client)
    target = os_client.sandbox_dir / "evolved.py"
    target.write_text("version = 1\n", encoding="utf-8")

    patched = await editor.apply_file_patch(
        filepath="sandbox/evolved.py",
        edits=[{"search": "version = 1", "replace": "version = 2"}],
    )
    checkpoint_id = json.loads(patched.message)["checkpoint_id"]
    target.write_text("version = 3\n", encoding="utf-8")

    restored = await editor.restore_file_checkpoint(checkpoint_id)

    assert restored.is_success is False
    assert "target changed after the checkpoint" in restored.message
    assert target.read_text(encoding="utf-8") == "version = 3\n"


@pytest.mark.asyncio
async def test_os_files_read_files_in_directory_char_limit(os_client):
    """Тест: Массовое чтение папки жестко прерывается при достижении лимита по токенам/символам."""
    reader = HostOSReader(os_client)

    # Ставим жесткий лимит в 10 символов. Метод умножает его на 2 (итого 20 символов на всю папку).
    os_client.config.file_read_max_chars = 10

    # Создаем 3 файла по 10 символов (итого 30 символов)
    (os_client.sandbox_dir / "f1.txt").write_text("1234567890", encoding="utf-8")
    (os_client.sandbox_dir / "f2.txt").write_text("1234567890", encoding="utf-8")
    (os_client.sandbox_dir / "f3.txt").write_text("1234567890", encoding="utf-8")

    res = await reader.read_files_in_directory("sandbox", max_files=10)

    assert res.is_success is True
    # Проверяем, что сработал аварийный тормоз
    assert "Reached global character reading limit" in res.message
    # Третий файл банально не влезет в лимит 20 символов
    assert "f3.txt" not in res.message


@pytest.mark.asyncio
async def test_read_file_range_returns_numbered_bounded_lines_and_checksum(os_client):
    reader = HostOSReader(os_client)
    target = os_client.sandbox_dir / "numbered.py"
    target.write_text(
        "".join(f"line_{number}\n" for number in range(1, 11)), encoding="utf-8"
    )

    result = await reader.read_file_range(
        "sandbox/numbered.py", start_line=3, end_line=9, max_lines=4
    )

    assert result.is_success is True
    assert "Lines: 3-6 of 10" in result.message
    assert "     3 | line_3" in result.message
    assert "     6 | line_6" in result.message
    assert "line_7" not in result.message
    assert "Requested range capped" in result.message
    assert hashlib.sha256(target.read_bytes()).hexdigest() in result.message


@pytest.mark.asyncio
async def test_read_file_range_rejects_invalid_or_out_of_bounds_ranges(os_client):
    reader = HostOSReader(os_client)
    target = os_client.sandbox_dir / "short.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")

    invalid = await reader.read_file_range("sandbox/short.txt", start_line=0)
    reversed_range = await reader.read_file_range(
        "sandbox/short.txt", start_line=2, end_line=1
    )
    out_of_bounds = await reader.read_file_range(
        "sandbox/short.txt", start_line=5
    )

    assert invalid.is_success is False
    assert reversed_range.is_success is False
    assert out_of_bounds.is_success is False
    assert "beyond the file's 2 lines" in out_of_bounds.message
