# vm_lib — снапшот GameServer-овых Python-модулей с VM

Зеркало модулей, которые лежат на Windows VM в `C:\Program Files (x86)\Python36-32\lib\`
и которые наши скрипты (`init_script_reconstructed.py`, `run_benchmark.py`) импортируют.
Локально мы их **не запускаем** (тащат Win32 зависимости — `win32com`, `wmi`),
держим только как референс для разработки и чтобы pyright/Pylance корректно
резолвил импорты в редакторе.

## Зачем

- **pyright/Pylance** — autocomplete, jump-to-definition, проверка сигнатур
  для `from pkinit import disk_helper, pklog, ...` и `import steam_pk`.
  Подцепляется через `extraPaths: ["vm_lib"]` в `pyrightconfig.json`.
- **Дальнейшая разработка** — задача #2 из CONTEXT.md (свой оркестратор без
  `Benchmark.Gta.exe`) требует понимания того, что именно делает `pkinit`.
- **Версионирование** = git. История = `git log vm_lib/`,
  diff между капчами = что GameServer изменил.

## Что НЕ лежит здесь и не должно

- Никакого редактирования. Если нужно патчить `pklog` — делай это в
  [sitecustomize.py](../sitecustomize.py), как уже сделано.
- Ruff/форматтеры исключают эту директорию (`pyproject.toml` → `extend-exclude`).

## Структура

`pkinit` импортируется как `from pkinit import ...` с несколькими атрибутами
(`disk_helper`, `utils`, `socialclub`, `GameShaders`, `file_helper`, `arguments`,
`pklog`, `critical_exit`, `exit_and_close_session`). По факту с VM это может
быть либо один файл с re-export'ами, либо пакет с подмодулями — структура
будет видна при копировании. Оба варианта валидны:

```
vm_lib/
├── pkinit.py          # если на VM это один файл
└── steam_pk.py
```

или

```
vm_lib/
├── pkinit/
│   ├── __init__.py
│   ├── disk_helper.py
│   ├── utils.py
│   └── ...
└── steam_pk.py
```

## Как снимать с VM

Через QEMU GA (см. паттерны из [`pull_results.sh`](../pull_results.sh)).
Источники на VM:

| Файл | Путь на VM |
|------|-----------|
| `pkinit.py` (или пакет) | `C:\Program Files (x86)\Python36-32\lib\pkinit.py` |
| `steam_pk.py` | `C:\Program Files (x86)\Python36-32\lib\steam_pk.py` |

После копирования — оформи коммит с заголовком вида:

```
vm_lib: snapshot from vm043, 2026-05-17
```

И в теле коммита укажи:
- С какой VM снято (DNS / hostname)
- Когда (дата)
- Версия Python на VM (`python --version`, обычно 3.6.x x86)
- (опционально) MD5/SHA каждого файла для верификации позже

## Текущий снапшот

_Заполнить после первого копирования._

- **VM**: …
- **Date**: …
- **Python**: …
- **Files**:
  - `pkinit*` — md5 …
  - `steam_pk.py` — md5 …
