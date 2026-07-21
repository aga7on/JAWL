# Отчёт по форку JAWL Coding Agent

Актуальность: 22 июля 2026 года.

## Краткий итог

Изолированный форк находится в `G:\AI\JAWL-Coding`, рабочая ветка —
`codex/coding-agent`. Базой служит upstream-коммит `cceaf75` из JAWL 0.16.1.1-beta.
Вершина основного agent-кода — `363bc38`; последующие коммиты относятся только к
документации, видимому мониторингу и упаковке переносимой сборки.

Исходный каталог `G:\AI\jawl-4` не использовался как рабочая копия для этих
изменений. В новый форк перенесены локальные настройки, память, базы и sandbox,
чтобы Gecko продолжил существующую историю работы.

Относительно upstream выполнено:

- 60 инженерных Git-коммитов основного coding-контура; последующие небольшие
  коммиты добавляют документацию и portable launcher;
- изменено 184 файла;
- добавлено 32 666 и удалено 295 строк;
- реализована 41 проверяемая capability coding-агента;
- capability gate: 41/41, 217 тестов, pass rate 100%;
- полный тестовый прогон: 805 passed, 2 skipped;
- финальная выборочная проверка desktop, IPC и MCP: 21/21.

Главный машинный отчёт проверки:
`.jawl-benchmarks/coding-20260721T215518Z.json`.

## Что сохранено от философии JAWL

Coding-контур добавлен поверх существующего фреймворка, а не вместо него.
Сохранены:

- автономный Heartbeat и событийная модель;
- L0–L3 архитектура;
- локальные SQL, vector и graph базы;
- Vector-Graph RAG и долговременная память;
- Telethon и остальные L2-интерфейсы;
- ReAct и Swarm;
- личность, мотиваторы и самостоятельное планирование;
- обратная совместимость старого wrapper-вызова навыков.

Новые возможности используют тот же реестр навыков, EventBus и жизненный цикл.
Coding-функции можно включать политиками, не превращая весь JAWL в безусловно
привилегированный shell-агент.

## Основные изменения

### 1. Работа с Qwen и QWB

- Восстановление tool calls из шумного или нестандартного ответа Qwen.
- Сохранение нескольких tool calls в одном ответе.
- Ограниченные повторы временных upstream-ошибок без бесконечного зависания.
- Thinking-политика `first_step`: Thinking используется на первом шаге ReAct,
  последующие инструментальные шаги не обязаны заново оплачивать полный reasoning.
- Отдельная телеметрия фактического thinking-режима.
- Динамический бюджет контекста с сохранением текущего события, свежих данных и
  точной on-demand выдачей сигнатур навыков.
- Безопасная обработка отмены downstream-клиента и длинного upstream SSE.
- Работа моста после получения токенов идёт через direct transport; постоянно
  открытый Edge для обычных запросов не требуется. CDP нужен для получения или
  обновления браузерной авторизации.

JAWL получает финальный ответ и нормализованные reasoning-метрики. Полный скрытый
thinking-текст не подмешивается обратно как пользовательский контекст: это не
нужно для следующего ReAct-шага и только раздувало бы prompt. Tool calls и
финальный результат сохраняются в рабочем протоколе.

### 2. Контекст репозитория и навигация

- Ограниченная карта репозитория, поиск и чтение диапазонов строк.
- Метаданные поиска файлов и защита от неконтролируемого чтения больших деревьев.
- Синтаксические определения и вхождения символов.
- Ограниченные dependency slices.
- Инкрементальные LSP-сессии с синхронизацией документов, LRU-лимитами,
  наблюдаемым состоянием и явным reset.
- Семантическая навигация и транзакционный multi-file rename через LSP.
- Корректная работа UTF-16 позиций LSP.

### 3. Редактирование и exact-state безопасность

- Атомарные checked-патчи с хешем исходного состояния.
- Отказ при устаревшем или неоднозначном совпадении.
- Checkpoint файлов и восстановление без затирания чужих изменений.
- Parser-guarded замена целых определений.
- Review ограниченных diff-hunks с устойчивыми хешами и привязкой к символам.
- Транзакционный проектный rename с preview, повторной проверкой и rollback.
- Защита от race condition между чтением, редактированием, проверкой и коммитом.

### 4. Планы, рабочие пространства и доставка результата

- Долговечные task plans с requirements, зависимостями, ревизиями и evidence.
- Проверка качества и пропорциональности плана.
- Адаптивное replanning с сохранением уже выполненных требований.
- Отдельный persistent Git worktree для параллельной задачи.
- Durable checkpoints рабочего дерева, индекса, плана и событийной линии.
- Exact-state diff acceptance.
- Commit gate: коммит разрешён только для ровно того состояния, которое прошло
  успешную проверку.
- Branch delivery preflight: локальная проверка fast-forward, ahead/behind и
  конфликтов с целевой веткой без изменения рабочего дерева и без скрытого fetch.

### 5. Выполнение команд и проверка

- Shell-free argv для управляемых task-команд.
- Allowlist политик команд и профилей проверки.
- Таймаут всего дерева процессов и ограниченные логи.
- One-shot approvals, связанные с точной командой, workspace и сроком действия.
- Именованные host/container профили.
- Docker/Podman isolation как опциональный backend.
- Автоматическое определение affected pytest-наборов с fail-closed fallback на
  полный suite.
- Поиск flaky-тестов несколькими стабильностными прогонами.
- Duration-aware pytest sharding с накопленной локальной историей длительности.
- Уведомления о запросах разрешения на desktop и в Telegram.
- Строго привязанные к chat/actor Telegram-команды подтверждения.

Широкое выполнение host-команд по умолчанию не включено. Возможность существует,
но выдача полномочий остаётся явным решением владельца.

### 6. Жизненный цикл, события и восстановление

- Единый lifecycle registry для tools, compaction, shutdown и delegated work.
- Детерминированный порядок hooks и ограничение времени observer-ов.
- Безопасное откладывание срочного события до границы ReAct вместо уничтожения
  активного ответа провайдера.
- Ограниченные очереди Heartbeat/ReAct с осмысленной coalescing-политикой.
- Durable action journal и trace correlation между LLM, действием, проверкой,
  планом и коммитом.
- Автоматическая классификация незавершённых операций после перезапуска без их
  слепого повторного выполнения.
- Исправлена совместимость реального L1 bootstrap.
- IPC теперь читает и обычный UTF-8, и прежние файлы с UTF-8 BOM; новый writer
  создаёт обычный UTF-8.

### 7. Делегированная работа

- Durable registry задач Swarm.
- Точное связывание worker-а с ревизией родительского плана и шагом.
- Отчёт подчинённого агента считается предложением, а не автоматическим доказательством.
- Проверка identity, report hash, workspace и verification evidence.
- Exact-handle cancel, restart reconciliation и корректный shutdown drain.

### 8. MCP

Добавлен policy-controlled MCP-клиент на стабильном Python SDK:

- stdio и Streamable HTTP transports;
- progressive discovery вместо загрузки всех инструментов в prompt;
- точные allowlists серверов и операций;
- свежий schema hash перед вызовом для защиты от подмены инструмента;
- локальная JSON Schema validation;
- ограниченные и редактированные результаты;
- lifecycle-owned сессии и корректная cancellation propagation;
- отсутствие автоматического повтора неизвестного внешнего side effect;
- resources и prompts только при явном включении;
- payload-free health в системном контексте.

В runtime MCP оставлен выключенным, потому что production-серверы и их allowlists
ещё не заданы. Это безопасное состояние, а не отсутствие поддержки.

### 9. Управление Windows и GUI-приложениями

Добавлен semantic Windows UI Automation broker:

- `observe_desktop` — ограниченный снимок видимых окон и accessibility-элементов;
- `act_on_desktop_element` — invoke, click, focus, set value, toggle, select,
  expand и collapse;
- `wait_for_desktop_element` — ожидание точного постусловия;
- короткоживущие opaque element references;
- SHA-256 fingerprint семантического состояния;
- отказ до действия, если target успел измениться;
- раздельные признаки `dispatched` и `verified`;
- ограничение числа окон, элементов, текста и размера результата;
- редактирование чувствительных значений;
- запрет `set_value` для password-полей;
- хранение только пяти последних карт ссылок в памяти.

Сохранены визуальные fallback-инструменты: скриншот всех экранов, grid,
координатный click, keyboard/hotkeys, focus window и clipboard. Скриншот получает
точный SHA и sandbox path. Мост умеет обнаружить OpenAI `image_url`, загрузить
изображение в Qwen OSS и передать его как `files` в web-запрос.

Живой read-only smoke test текущего рабочего стола увидел 7 окон и 40 UIA-элементов.
Стандартные Win32/WPF/WinUI-приложения и доступные через UIA элементы Edge/Electron
могут управляться семантически. Custom canvas, игры и часть графических редакторов
потребуют vision + координаты. UAC secure desktop, экран блокировки и повышенные
процессы остаются защищены Windows.

Агенту явно запрещено самостоятельно выключать, перезагружать или блокировать ПК,
работать с паролями и подтверждать покупки без прямого разрешения пользователя.

### 10. Телеметрия и оценка

- Privacy-preserving coding telemetry без сохранения prompts, thoughts,
  параметров инструментов и чувствительных результатов.
- Раздельные provider/estimated tokens, LLM/tool latency, outcomes и verification
  failures.
- Детерминированный capability benchmark.
- Небольшие fixed repositories с публичными и скрытыми тестами.
- Реальный ReAct evaluation driver для JAWL.
- Изолированный driver для внешних CLI-агентов и криптографический контракт
  одинакового задания/grader-а.
- Fail-closed сравнение: несовпадающие контракты нельзя выдать за честный benchmark.

Живое платное сравнение с внешним Codex CLI не запускалось без отдельного разрешения.

## Runtime-состояние на момент отчёта

- JAWL/Gecko: запущен через `python -m src.main`.
- PID реального Python-процесса: `30032`.
- PID venv/launcher shim: `40040`.
- QWB: PID `3088`.
- QWB endpoint: `http://127.0.0.1:8000/v1`.
- QWB transport: `direct`.
- Модель: `qwen3.8-max-preview`.
- Три Qwen-профиля загружены и на момент проверки имеют состояние `healthy`.
- Telethon успешно авторизован через настроенный MTProxy.
- Host OS, Host Terminal, multimodality и vision загружены.

Процессы были запущены скрыто, поэтому у них нет собственных окон консоли. Это не
означает, что они остановлены. Для видимого мониторинга запустите
`SHOW_JAWL_STATUS.bat`: он не создаёт второй JAWL/QWB, а открывает живой лог агента
и health-панель моста.

Для штатного интерактивного меню существует `start.bat`. Не запускайте второй
экземпляр агента, пока текущий PID жив. `start_agent.bat` предназначен для прямого
видимого запуска после штатной остановки работающего экземпляра.

`start.bat` также является portable bootstrapper: если `venv` отсутствует, он
находит Python 3.11 и передаёт управление `jawl.py`, который создаёт окружение и
скачивает зависимости. Инструкция для чистого ZIP находится в
`PORTABLE_README_RU.md`.

## Текущая проблема upstream Qwen

Во время финальной проверки Qwen несколько раз вернул `high demand` всем трём
профилям. Мост корректно ротировал аккаунты, назначал короткий cooldown и не
оставлял JAWL висеть бесконечно. То же произошло на минимальном запросе
`Reply exactly: OK`, поэтому причиной не является размер JAWL-контекста, IPC или
парсинг Thinking. После cooldown health снова показывает все аккаунты здоровыми.

Это временное внешнее состояние Qwen. JAWL и мост оставлены запущенными.

## Где читать подробности

- `docs/coding_agent_architecture.md` — подробные контракты и архитектура.
- `docs/coding_agent_gap_audit.md` — честный gap-аудит относительно ведущих
  coding-агентов.
- `docs/coding_agent_lifecycle_hooks.md` — lifecycle hooks и политики.
- `docs/yaml/interfaces/host_os.md` — Host OS, coding и desktop-конфигурация.
- `docs/yaml/interfaces/mcp.md` — конфигурация MCP.
- `benchmarks/coding_agent/README.md` — capability benchmark.
- `benchmarks/coding_tasks/README.md` — fixed-task и cross-agent evaluation.
- `.jawl-benchmarks/coding-20260721T215518Z.json` — последний машинный отчёт.

## Как перепроверить

Из корня `G:\AI\JAWL-Coding`:

```powershell
# Текущая ветка и изменения
git status --short
git branch --show-current

# Все 60 коммитов форка
git log --oneline cceaf75..codex/coding-agent

# Capability gate
.\venv\Scripts\python.exe benchmarks\coding_agent\run.py

# Полный test suite
.\venv\Scripts\python.exe -m pytest -q

# Health моста
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 6
```

## История 60 инженерных коммитов

Ниже перечислены коммиты форка в порядке выполнения:

1. `b32c0f2` — сохранены локальные исправления QWB и Telethon как baseline.
2. `401ce83` — dependency-aware выполнение действий.
3. `4fdc18c` — изоляция проверки коллизии webhook-порта.
4. `49ed4f7` — atomic checked patches и checkpoints.
5. `5846853` — persistent task-isolated Git workspaces.
6. `a872620` — durable action lifecycle и interruption evidence.
7. `d225933` — commit gate по verification fingerprint.
8. `c879b33` — bounded repository context tools.
9. `a32d833` — bounded task diff review.
10. `dc1ddf7` — deterministic coding capability benchmark.
11. `d97df7a` — protocol failures и execution telemetry.
12. `84fd20b` — durable plans и completion gates.
13. `303e038` — cross-cycle trace correlation.
14. `e1021ad` — bounded streaming Git diff.
15. `5657a17` — allowlisted repository verification policy.
16. `02464a5` — native/hybrid tool transports.
17. `81a73d0` — fixed-repository patch-quality evaluation.
18. `82dcfa9` — syntax-aware symbol navigation.
19. `9274337` — bounded dependency slices.
20. `f8d5751` — hardening skill guards и tick ordering.
21. `656f243` — isolated live coding evaluation driver.
22. `f8ae5f8` — semantic LSP navigation.
23. `ad1a04b` — batch task-scoped verified workflows.
24. `820a949` — восстановление Qwen tool calls из noisy output.
25. `97f6054` — transient QWB retries.
26. `13bdc36` — управление Thinking по шагам ReAct.
27. `8bc927d` — external coding CLI evaluator.
28. `82f848f` — метрики фактического Thinking.
29. `a7330c8` — bounded dynamic context и skill discovery.
30. `b2a0ec0` — deterministic lifecycle policy hooks.
31. `2e7b72d` — safe-boundary defer срочных событий.
32. `88bdd7d` — transactional coding recovery.
33. `49d4db4` — policy-controlled task commands.
34. `6284fad` — bounded/coalesced event queues.
35. `16a4728` — fail-closed agent comparison.
36. `1c20d0b` — declarative lifecycle commands.
37. `11057cf` — one-shot command approvals.
38. `a3508a9` — lifecycle across compaction/shutdown/delegation boundaries.
39. `49c1863` — durable delegated work.
40. `f06aeec` — reconciliation результатов делегирования.
41. `a571edd` — exact-state adaptive replanning.
42. `e7dd681` — bounded incremental LSP sessions.
43. `180f4a8` — parser-guarded structural editing.
44. `0bd5b27` — mandatory exact-state diff review.
45. `e692b78` — transactional project symbol rename.
46. `19d2c90` — named container policy profiles.
47. `2d24380` — redacted approval notifications.
48. `0a75524` — startup reconciliation прерванных coding actions.
49. `e370ef7` — authenticated Telegram approvals.
50. `8cbde92` — external CLI preflight contracts.
51. `16f294b` — fail-closed flaky verification.
52. `2319351` — fail-closed affected-test selection.
53. `a84d424` — privacy-preserving coding telemetry.
54. `883a29c` — duration-aware pytest sharding.
55. `003f285` — proportional plan-quality gate.
56. `be17e7d` — exact-state branch-delivery preflight.
57. `b125f31` — policy-controlled MCP interoperability.
58. `ea2304a` — exact-state desktop automation.
59. `e737531` — real L1 bootstrap compatibility.
60. `363bc38` — IPC UTF-8 BOM compatibility.

## Известные ограничения и дальнейшие улучшения

- Нужна органическая проверка качества `first_step` Thinking после прекращения
  `high demand` у Qwen.
- Нужны конкретные MCP-серверы и минимальные allowlists перед включением MCP.
- macOS/Linux пока не имеют аналога Windows semantic UIA broker.
- Secure desktop и повышенные приложения намеренно не обходятся.
- Общие semantic refactors кроме rename зависят от возможностей конкретного LSP.
- Публикация ветки, создание PR и разрешение конфликтов остаются отдельными
  явными операциями.
- Полное live-сравнение с проприетарными агентами требует одинакового задания,
  sandbox и разрешения на расход соответствующих квот.
