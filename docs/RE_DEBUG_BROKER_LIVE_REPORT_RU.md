# Отчёт живой проверки RE/Debug Broker

Дата проверки: 29 июля 2026 года.
Среда: реально запущенный JAWL, `qwen3.8-max-preview`, Windows x64.

## Итог

- JAWL после штатного перезапуска увидел `7/7` провайдеров.
- Через настоящий skill registry обнаружены все `33` операции.
- Выполнено `58` последовательных проверок без неожиданных ошибок.
- После теста нет активных broker-сессий и процессов x64dbg/x32dbg/TTD или
  `jawl_live_target`.
- Полный тест репозитория: `915 passed, 3 skipped`.
- Отдельная прямая live-матрица установленных инструментов: `1 passed`.

## Что выполнено через JAWL

| Провайдер | Живая проверка |
| --- | --- |
| radare2 | Все 7 операций: info, три уровня API анализа, функции, строки, disassembly, xrefs и raw JSON command |
| Ghidra | Новый headless project, PE-анализ, 461 экспортированная функция, большой результат вернулся валидным truncation envelope |
| WinDbg/CDB | Команды `r/lm`, реальный access violation, стек `Program.Crash` |
| TTD | Версия и путь обнаружены; `record_trace` безопасно отказал при `EULASigned=0`, не принял EULA и не вызвал UAC |
| Frida | Process list, suspended spawn, modules, чтение `MZ`, JavaScript message, RPC `20+22=42`, resume, owned-process cleanup |
| Qiling | Environment API и восемь ограниченных шагов x86 shellcode |
| Triton | Concrete execution до 42 и symbolic model со входом `0x29` |
| x64dbg | Все 11 операций: state, registers, modules, breakpoints, read, обратимая запись того же байта, disassembly, step-in и raw command |
| x32dbg | Автовыбор x86 для отдельного PE и проверка правильного target |

Вызовы шли по полному модельному маршруту:

```text
Host Terminal operator control
  → live JAWL SkillRegistry
  → DebugBroker.search_operations (schema + SHA-256)
  → DebugBroker.start_session
  → DebugBroker.call_operation
  → DebugBroker.stop_session
```

## Воспроизведение

Сначала запустить JAWL, затем:

```powershell
$env:JAWL_DEBUG_BROKER_JAWL_LIVE = "1"
venv\Scripts\python.exe -m pytest `
  tests/integration/src/l2/debug_broker/test_live_jawl_registry.py -q
```

Тест использует только безопасный `jawl_live_target.exe`. Запись x64dbg
восстанавливает тот же прочитанный байт. TTD-трасса не создаётся автоматически:
если recorder не готов, проверяется отказ; если он готов, операция записи
намеренно пропускается, потому что trace способен содержать чувствительную
память и требует отдельного осознанного запуска.
