import json

import pytest
from src.l2_interfaces.host.os.skills.files.search import HostOSSearch
from src.l2_interfaces.host.os.skills.files.metadata import HostOSMetadata
from src.l2_interfaces.host.os.skills.files.archive import HostOSArchive
import zipfile


@pytest.mark.asyncio
async def test_search_list_directory(os_client, tmp_path):
    """Тест: листинг директории строит правильное ASCII дерево."""
    search = HostOSSearch(os_client)
    
    # Подготовка структуры
    target_dir = os_client.sandbox_dir / "my_project"
    target_dir.mkdir()
    (target_dir / "main.py").write_text("print('test')", encoding="utf-8")
    (target_dir / ".hidden").touch()
    
    res = await search.list_directory("sandbox/my_project", max_depth=1)
    
    assert res.is_success is True
    assert "my_project" in res.message
    assert "main.py" in res.message
    assert ".hidden" not in res.message  # Скрытые файлы фильтруются


@pytest.mark.asyncio
async def test_search_files_by_pattern(os_client):
    """Тест: поиск файлов по паттерну."""
    search = HostOSSearch(os_client)
    
    (os_client.sandbox_dir / "test1.py").touch()
    (os_client.sandbox_dir / "test2.log").touch()
    
    res = await search.search_files("*.py", "sandbox")
    
    assert res.is_success is True
    assert "test1.py" in res.message
    assert "test2.log" not in res.message


@pytest.mark.asyncio
async def test_search_content_in_files(os_client):
    """Тест: глобальный поиск строки (grep) по содержимому файлов."""
    search = HostOSSearch(os_client)
    
    f1 = os_client.sandbox_dir / "app.py"
    f1.write_text("def auth():\n    secret = 'PASSWORD_123'\n", encoding="utf-8")
    
    f2 = os_client.sandbox_dir / "config.json"
    f2.write_text('{"token": "PASSWORD_123"}', encoding="utf-8")
    
    res = await search.search_content_in_files("PASSWORD_123", "sandbox")
    
    assert res.is_success is True
    assert "app.py:2" in res.message
    assert "config.json:1" in res.message


@pytest.mark.asyncio
async def test_repository_search_returns_bounded_context_and_unicode_column(os_client):
    search = HostOSSearch(os_client)
    source_dir = os_client.sandbox_dir / "repo_search"
    source_dir.mkdir()
    (source_dir / "app.py").write_text(
        "before\n\u043f\u0440\u0435\u0444\u0438\u043a\u0441 target_call()\nafter\n",
        encoding="utf-8",
    )
    (source_dir / "notes.md").write_text("target_call ignored\n", encoding="utf-8")

    result = await search.search_repository(
        "target_call",
        "sandbox/repo_search",
        globs=["*.py"],
        context_lines=1,
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["match_count"] == 1
    assert payload["matches"][0]["path"].endswith("app.py")
    assert payload["matches"][0]["line"] == 2
    assert payload["matches"][0]["column"] == 9
    assert payload["matches"][0]["before"][0]["text"] == "before"
    assert payload["matches"][0]["after"][0]["text"] == "after"


@pytest.mark.asyncio
async def test_repository_search_python_fallback_and_global_limit(os_client, monkeypatch):
    search = HostOSSearch(os_client)
    source_dir = os_client.sandbox_dir / "fallback_search"
    source_dir.mkdir()
    for number in range(4):
        (source_dir / f"file_{number}.txt").write_text(
            f"needle_{number}\n", encoding="utf-8"
        )
    monkeypatch.setattr(
        "src.l2_interfaces.host.os.skills.files.search.shutil.which",
        lambda executable: None,
    )

    result = await search.search_repository(
        r"needle_\d", "sandbox/fallback_search", regex=True, max_matches=2
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["backend"] == "python"
    assert payload["match_count"] == 2
    assert payload["truncated"] is True


@pytest.mark.asyncio
async def test_repository_search_enforces_serialized_output_budget(os_client):
    search = HostOSSearch(os_client)
    source_dir = os_client.sandbox_dir / "budget_search"
    source_dir.mkdir()
    for number in range(20):
        (source_dir / f"large_{number}.txt").write_text(
            "before " + "a" * 300 + "\nneedle\nafter " + "b" * 300 + "\n",
            encoding="utf-8",
        )
    os_client.config.file_read_max_chars = 500

    result = await search.search_repository(
        "needle",
        "sandbox/budget_search",
        context_lines=2,
        max_matches=20,
    )
    payload = json.loads(result.message)

    assert result.is_success is True
    assert payload["truncated"] is True
    assert payload["serialized_chars"] <= 1000
    assert len(result.message) <= 1000


@pytest.mark.asyncio
async def test_set_file_metadata(os_client):
    """Тест: привязка метаданных к файлу."""
    meta_skill = HostOSMetadata(os_client)
    
    test_file = os_client.sandbox_dir / "image.png"
    test_file.touch()
    
    res = await meta_skill.set_file_description("sandbox/image.png", "Это скриншот окна браузера.")
    
    assert res.is_success is True
    
    # Проверяем реестр
    meta_data = os_client.get_file_metadata()
    assert "image.png" in meta_data
    assert meta_data["image.png"] == "Это скриншот окна браузера."


@pytest.mark.asyncio
async def test_extract_archive_success(os_client):
    """Тест: успешная (легальная) распаковка архива (позитивный кейс)."""
    archive_skill = HostOSArchive(os_client)
    
    zip_path = os_client.sandbox_dir / "test.zip"
    extract_path = os_client.sandbox_dir / "extracted"
    
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("safe_file.txt", "Safe content")
        
    res = await archive_skill.extract_archive("sandbox/test.zip", "sandbox/extracted")
    
    assert res.is_success is True
    assert (extract_path / "safe_file.txt").exists()
    assert (extract_path / "safe_file.txt").read_text() == "Safe content"
