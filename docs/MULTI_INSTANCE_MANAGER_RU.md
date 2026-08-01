# Multi-Instance Manager JAWL Coding

Multi-Instance Manager запускает несколько полноценных главных агентов из одного
репозитория. Это не Swarm-подагенты внутри одного ReAct-цикла: каждый именованный
инстанс является отдельным процессом JAWL со своей личностью, памятью, GOAL,
журналами, интерфейсами и жизненным циклом. Обычный одиночный запуск `default`
остаётся обратно совместимым и не требует включения менеджера.

## Управление

Откройте `python jawl.py` и выберите `[M] Multi-Instance Manager`.

Экран позволяет:

- создать именованный профиль с чистой конфигурацией или из шаблона;
- выбрать модель, Telegram-режим, видимую консоль и автоматический restart;
- запустить, остановить, перезапустить или вывести инстанс из quarantine;
- открыть его отдельный чат, логи, каталог и scoped-меню конфигурации/GOAL;
- посмотреть объединённые хвосты логов;
- архивировать остановленный профиль или удалить его после подтверждения.

Supervisor работает отдельным процессом и периодически приводит фактическое
состояние к `desired_state`. Один межпроцессный lock не допускает запуска двух
supervisor-ов. Профиль после серии падений переходит в `quarantined`; выход из
этого состояния всегда требует явного `Start / clear quarantine`. Если
`auto_restart=false`, состояние `crashed` также остаётся стабильным до команды
оператора.

## Изоляция и общие ресурсы

Для `default` сохраняется прежняя раскладка каталогов. Именованный профиль
`Gecko` размещается в `runtime/instances/Gecko/`.

| Состояние | Расположение | Изоляция |
|---|---|---|
| YAML и локальный `.env` | `config/`, `.env` профиля | отдельные |
| SQL, Vector, Graph | `data/sql`, `data/vector`, `data/graph` | отдельные |
| GOAL и action journal | `data/agent/` | отдельные |
| Telegram session/cache | `data/interfaces/telegram/` | отдельные |
| логи и prompts/personality | `logs/`, `prompts/` | отдельные |
| PID, lock, stop-файл, IPC-порты | внутри private `data/` | отдельные |
| рабочий sandbox | корневой `sandbox/` | общий намеренно |
| служебные артефакты агента | `sandbox/_system/instances/<id>/` | отдельные |

Корневая `.env` сначала загружается как общий набор провайдеров и секретов.
Локальная `.env` именованного профиля, если создана оператором, применяется
поверх неё. Поэтому копировать секреты в каждый профиль необязательно.

## Telegram и порты

Менеджер резервирует отдельный блок TCP-портов и отклоняет конфликты активных
профилей. Для явного `telethon`/`aiogram` режима требуется уникальная
несекретная метка Telegram identity. Одна Telethon session или одна identity не
может одновременно принадлежать двум включённым профилям.

Telethon хранит session и MTProxy cache внутри private data профиля. При
недоступном маршруте используется общий устойчивый алгоритм: cached/configured
proxy, публичный `@mtp4tg`, параллельный MTProto checker с учётом DC аккаунта,
полный авторизованный start и только после него запись выбранного маршрута в
кэш. Proxy secret в диагностические сообщения не выводится.

## Координация главных агентов

`InstanceMesh` — ограниченная общая плоскость координации поверх изолированной
памяти. Навыки позволяют:

- получить список инстансов;
- отправить bounded message/delegation/steering;
- опубликовать задачу;
- атомарно забрать её одним агентом;
- завершить задачу кратким результатом.

Mesh не объединяет Vector/Graph/SQL базы и не раскрывает приватную память одного
агента другому. Получившая задачу главная инстанция при необходимости может
сама делегировать работу своему локальному Swarm.

## Файлы управления

- `runtime/instances/registry.json` — атомарный durable registry профилей и
  runtime-состояний;
- `runtime/instances/supervisor.pid` и `.lock` — владение supervisor;
- `runtime/instances/operations.lock` — сериализация reconcile с archive/delete;
- `runtime/instances/supervisor.log` — его диагностика;
- `sandbox/_system/instance_mesh/mesh.json` — bounded mesh board;
- `runtime/instances/_archive/` — recoverable архивы профилей.

Registry не переписывается на пустом reconcile tick. Это важно на Windows:
ранняя версия supervisor-а увеличивала revision каждую секунду в стабильном
`crashed` состоянии и создавала десятки тысяч лишних atomic writes.

## Проверка

Быстрый regression:

```powershell
.\venv\Scripts\python.exe -m pytest tests/unit/instances -q
.\venv\Scripts\python.exe -m pytest tests/integration/src/instances/test_live_multi_instance.py -q
```

Интеграционный сценарий реально запускает несколько дочерних процессов и
проверяет отдельные data/config/log paths, общий sandbox, отсутствие коллизий
портов и Telegram-настроек, mesh между тремя агентами, crash recovery,
quarantine и сохранение legacy layout. Полный реальный профиль `Mara` также был
запущен через `src/main.py`; его собственные логи, базы, GOAL и action journal
созданы под `runtime/instances/Mara/`, не в состоянии `default`.

## Эксплуатационные ограничения

- Общий sandbox означает намеренную совместную запись. Для одного файла всё
  равно нужен task ownership, отдельный Git worktree или coordination через
  Mesh/Swarm.
- Одинаковый Qwen аккаунт можно использовать разными агентами, но bridge quota
  и активные web-чаты остаются внешним общим ресурсом.
- Не копируйте `.session`, `.env`, proxy cache и runtime data в Git или архив
  дистрибутива.
- Сначала используйте recoverable archive. Permanent delete предназначен
  только для остановленного профиля и требует отдельного подтверждения.
