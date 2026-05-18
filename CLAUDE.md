# pkbench

Python-оркестратор Cyberpunk + Wukong бенчмарков на эфемерных Windows-VM (RTX 5060 passthrough) через libvirt + QEMU Guest Agent. Заменяет production-стек GameServer + `Benchmark.Gta.exe` (декомпилированные исходники — `/tmp/bga_src/` и `/tmp/bga_unbundled/`, восстанавливаются из `benchmark-gta/Benchmark.Gta.exe` через `ilspycmd`).

## Цель и приоритет fidelity

Главная цель — результаты, **близкие к оригинальному exe**, не «лучше». Если оригинал ставит DLSS off + RT off + 1080p — мы повторяем точно. Любые отклонения в логике конфига = invalid result.

VM эфемерные (постоянно пересоздаются из master image). Можно агрессивно мутировать `gamer`-аккаунт (пароль/права) — это норма флоу.

## Архитектура (3 файла)

```
benchmark/
├── pkbench.py        — хост-CLI (Python 3.6, OL7)
├── vm_bench.py       — точка входа на VM (Python 3.6-32 Windows)
└── steam_backup.py   — отдельный backup-tool для Steam Trusted Device

sync_to_host.sh       — копирует эти три на хост в /root/benchmark/, chmod +x
```

**Транспорт хост↔VM**: libvirt-python (`libvirt.open('qemu:///system')` + `libvirt_qemu.qemuAgentCommand(dom, json, timeout, 0)`). НЕ subprocess virsh. На OL7 ставится `yum install libvirt-python`.

**Цепочка запуска бенча в VM**: PsExec → cmd.exe → `bench_psexec.bat` → `run_via_ga_launcher.bat` → `python vm_bench.py`. `.bat`-обёртки **генерируются** pkbench.py при deploy, в репо их нет. PsExec→cmd.exe обязательно (CreateProcessAsUser для python.exe напрямую возвращает ERROR_LOGON_FAILURE).

## CLI

```bash
# Главный флоу: deploy + VNC + бенч + pull одним заходом
sudo -E ./pkbench.py <vm> [vk|rt|2k]

# Отладочный read файла с VM
./pkbench.py cat <vm> 'C:\benchmark\last_run.log'
```

### Env-vars

| Env | Что включает |
|---|---|
| `STEAM_USER` + `STEAM_PASS` | Wukong-таск после Cyberpunk |
| `STEAM_GUARD_IMAP_HOST` + `STEAM_GUARD_EMAIL` + `STEAM_GUARD_EMAIL_PASS` | авто Steam Guard 2FA через IMAP |
| `ONLY_WUKONG=1` | debug: пропустить Cyberpunk |
| `HTTP_PORT` (default 8765) | порт временного HTTP-сервера для деплоя ffmpeg+PsExec |
| `STEAM_DIR` (для steam_backup.py, default `F:\launch\Steam`) | путь Steam-клиента |

## Что делает основной флоу

1. Генерирует свежий пароль gamer (один раз на запуск, не хранится на диске).
2. `net user gamer <pw>` на VM.
3. Очищает `C:\benchmark\` (rmdir + mkdir через PowerShell — cmd /c с complex quoting ломается).
4. `iptables flush` на хосте если root — для пробивки VNC.
5. Качает ffmpeg + PsExec в `vm_deploy_cache/` рядом с pkbench (~200MB, кэшируется).
6. Деплой: `vm_bench.py` + сгенерированные `.bat` через GA-base64; ffmpeg/PsExec через временный HTTP-сервер.
7. VNC: активирует gamer + Administrators, открывает Firewall TCP 5900, стартует RealVNC.
8. Печатает VNC-баннер на stderr: `<vm-ip>:5900 / user=gamer / pass=<X>`.
9. PsExec → cmd → launcher → `vm_bench.py <config> <steam_user> <steam_pass> <only_wukong> <imap_host> <email_user> <email_pass>` (8 args в bench_psexec.bat, 7 в launcher.bat).
10. Поллит `C:\benchmark\last_status.json` (его появление = «бенч закончен», до 30 мин).
11. Читает `last_status.json` + `last_result.json` → JSON в stdout.
12. Тянет raw `summary.json` (Cyberpunk) и Wukong-результат в CWD как `summary_<vm>_<host>_<cfg>_<dt>.json` / `wukong_<vm>_<host>_<dt>.json`.

## Артефакты на VM (`C:\benchmark\`)

- `last_run.log` — stdout/stderr всего прогона vm_bench (через `os.dup2(fd, 1/2)`, subprocess'ы тоже туда логируются).
- `last_status.json` — статус прогона ВСЕГДА (exit_code, duration, nvenc_died_early, wukong_skipped/present/error). Появление = сигнал хосту что бенч закончен.
- `last_result.json` — `{"cyberpunk": {...} | null, "wukong": {...} | null}` при exit_code=0.
- `last_ffmpeg.log` — stderr ffmpeg-NVENC.
- `psexec.log` — диагностика PsExec (он в `-d` режиме иначе глотает stdout/stderr).

## Cyberpunk-таск

Запускает игру через `.lnk` shortcut (`F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.lnk.lnk`), который создаётся в `setup_cyberpunk_shortcut()` (порт `initialize_cyberpunk_shortcut` из старого init_script_reconstructed). Минуя Steam-логику установки (для Cyberpunk это работает потому что .lnk → ShellExecute → exe-DRM-проверка через уже-запущенный Steam клиент).

Конфиг (`UserSettings.json`) качается с CDN VK Play Cloud в `C:\Users\Gamer\AppData\Local\CD Projekt Red\Cyberpunk 2077\`. Варианты: `vk` (120fps test, **эталонный**), `rt` (RayTracing), `2k`.

Опционально — прогрев DX shaders через `pkinit.GameShaders.setup_dx_shaders('Cyberpunk')` (pkinit на VM есть — это production-зависимость).

2 итерации: первая — прогрев (результат не берётся), вторая — финальный. В фоне:
- `escape_spammer` — каждые 10s шлёт ESC через SendInput-scancode (`DIK_ESCAPE = 0x01`) после 90s warmup в течение 2 минут. Скипает заставки.
- `annoyance_closer` — каждые 5s закрывает Steam-попапы по титлу (`'Специальные предложения'`, `'Список друзей'`, EN-варианты).

Результат: `C:\Users\gamer\Documents\CD Projekt Red\Cyberpunk 2077\benchmarkResults\benchmark_<dt>\summary.json`. Парсим `Data.{averageFps, minFps, maxFps, gpuName, rayTracingEnabled, DLSSEnabled}`.

## Wukong-таск

Запускается только если есть `STEAM_USER`/`STEAM_PASS`. Workflow:

1. `steam_ensure_logged_in(user, pass, email_creds)`:
   - Если Steam **уже запущен** И `ActiveUser != 0` в `HKCU\Software\Valve\Steam\ActiveProcess` И manifest на месте → **skip relogin**.
   - Иначе — `kill_steam()` + `clear_steam_login_state()` (registry `ActiveUser=0`, `AutoLoginUser=""`, удалить `loginusers.vdf`).
   - **Подложить Wukong manifest** (см. ниже) ДО `Popen([steam.exe])` — критично.
   - `Popen([steam.exe, -silent, -nofriendsui, -nochatui])`.
   - `FindWindow('SDL_app', 'Войти в Steam'/'Sign in to Steam')` — ждём до 120s.
   - `SetForegroundWindow + BringWindowToTop + ShowWindow(SW_SHOW)`.
   - `LoadKeyboardLayoutW('00000409', KLF_ACTIVATE)` + `SendMessage(WM_INPUTLANGCHANGEREQUEST=0x50)` — англ. раскладка (иначе кириллица в логине на ru-госте).
   - `write_line(user)` через SendInput со scancode (DIK set 1 из `Common.WinApi.ConfigButtons.getButtonsKey`), Shift на uppercase/спецсимволы.
   - `KeyPress(Tab)` → `write_line(pass)` → `KeyPress(Return)`.
   - Если `ActiveUser` остался 0 и есть `email_creds` → IMAP fetch Steam Guard кода (5 попыток × 15s), `write_line(code)` + Enter.
2. `Popen([steam.exe, ..., -applaunch, 3132990, -benchmark])` × 2 попытки.
3. Polling `b1-Win64-Shipping.exe` (до 5 мин на попытку).
4. Wait for result file in `C:\Users\gamer\AppData\Local\Temp\b1\BenchMarkHistory\Tool\` (любой файл с size>0, settle 5s).
5. Parse: `{FPSAvg, FPS95, GameVer}`. Kill `b1-Win64-Shipping.exe` + `b1_benchmark.exe`.

Фейл Wukong в обычном режиме **не валит** общий бенч (Cyberpunk-результат уже собран). В `ONLY_WUKONG=1` — фатал.

### Wukong manifest (КРИТИЧНО)

Без `F:\launch\Steam\steamapps\appmanifest_3132990.acf` Steam при `applaunch` показывает модальный install dialog. Мы НЕ генерируем manifest с нуля, а копируем pre-prepared snapshot из `F:\launch\Steam\steamapps\manifests\appmanifest_3132990.acf` (GameServer кладёт этот snapshot при подготовке master VM image — дословный эквивалент `steam_copy_manifest` из `vm_lib/steam_pk.py`).

```python
text = WUKONG_MANIFEST_SOURCE.read_text(encoding='utf-8')
text = re.sub(r'("LastOwner"\s*)"\d+"', r'\1""', text)   # обнулить LastOwner
WUKONG_MANIFEST_PATH.write_text(text, encoding='utf-8')
```

**ВАЖНО**: подкладывать **ДО** `Popen([steam.exe])`. Steam сканит `steamapps/` **только при старте клиента**, на лету изменения не подхватывает. Если положить manifest после старта Steam — applaunch всё равно покажет install dialog.

Если pre-prepared snapshot отсутствует на VM (краевой случай) — выводим warning, applaunch упадёт в диалог. Tab-based click на диалог в новом Steam UI 2025+ **не работает** (фокус приземляется на «Отмена»). Если столкнёшься с этим — снова искать pre-prepared snapshot на master VM image, не пытаться обходить через UI-automation.

## NVENC-нагрузка

Запускается ВСЕГДА параллельно с бенчем (production-fidelity: GameServer стримит постоянно через DX-hook injection в `SharedCapture_x64.dll` → NVENC → UDP, 1080p60 H.264 CBR 25Mbps).

Воспроизводим **только encoder-stage** (90%+ GPU-нагрузки от стрима) через `ffmpeg -re -f lavfi -i testsrc=size=1920x1080:rate=60 ... -c:v h264_nvenc -preset p1 -tune ll -rc cbr -b:v 25M -bf 0 -g 60 -slices 8 -refs 16 ...`. Capture-stage (Desktop Duplication API + ddagrab) **не воспроизводим**: ddagrab умирает в exclusive-fullscreen с `DXGI_ERROR_ACCESS_LOST`, auto-restart бесполезен, а production использует proprietary DX-hook injection через `SharedCapture_x64.dll` (1.5MB native PE, лежит в `/workspaces/pkbench/`; на VM в `C:\temp\`).

`-re` обязателен: без него lavfi отдаёт фреймы AFAP, NVENC encoder гонит ~200fps (3.34x), в 3 раза тяжелее production. С `-re` ffmpeg pacит на native 60fps.

Если ffmpeg не стартовал ИЛИ умер мид-бенч → `exit_code != 0` + `nvenc_died_early: true` в status (без encoder-нагрузки результат не репрезентативен).

## Известные ограничения VM

- BAR1=256MB без ReBAR (QEMU 4.2) — после миграции на QEMU 6.2 + `<rom bar='on'/>` в XML + Above 4G Decoding в BIOS хоста ожидается +20-40% FPS.
- Текущий результат VK-конфига idle: ~24 FPS (matches original exe).

## Ловушки реализации

Технические грабли, на которые наступили — на будущее:

1. **libvirt-python**: импортировать **И** `libvirt`, **И** `libvirt_qemu` (отдельный submodule). `qemuAgentCommand` — функция в `libvirt_qemu`, не метод на `virDomain`. На старых версиях libvirt-python это был метод — отсюда путаница.
2. **libvirt error handler**: при поллинге `file_exists(last_status.json)` каждые 10s libvirt шлёт в stderr 30+ строк spam'а про неуспешный `guest-file-open`. Регистрируем no-op handler: `libvirt.registerErrorHandler(lambda _ctx, _err: None, None)`. Все настоящие ошибки приходят как `libvirtError` exception и ловятся в `_agent()`.
3. **cmd /c quoting**: `cmd /c "if not exist X md X"` через QGA arg-vector → внутренние кавычки ломаются, mkdir не выполняется. Используем PowerShell с одиночными кавычками: `New-Item -ItemType Directory -Force -Path '...'`, `Remove-Item -LiteralPath '...' -Recurse -Force`.
4. **`net user gamer <pw>` через cmd /c**: cmd с 4 кавычками strip'ает первую и последнюю, пароль ставится С литеральными кавычками внутри. Зовём `net.exe` напрямую через `guest-exec arg-vector`.
5. **Collision имён `_find_window`**: в одном файле `_find_window(title)` (cyberpunk error-windows, 1 arg) и `_find_window(class, caption)` (Steam UI, 2 args). Python поздним определением перезаписал ранее → `TypeError`. Переименовали второе в `_find_window_by_class`.
6. **group libvirt**: для `qemu:///system` пользователь запускающий pkbench должен быть в группе `libvirt` (или root).
7. **steam.exe -login user pass УСТАРЕЛ**: в новых версиях Steam этот флаг не работает. Используем UI-automation через SendInput-scancodes (порт `Common.Steam.dll::PassAuthorization`).
8. **encoding='utf-8' обязателен** при `read_text`/`write_text` на Windows — иначе подхватит cp1251.
9. **`STEAM_LAUNCH_FLAGS = ['-silent', '-nofriendsui', '-nochatui']`**: меньше фоновых steamwebhelper instances. Auth-окно всё равно появляется когда нужен ввод (silent блокирует только idle main window).

## Декомпиляция оригинала

- `benchmark-gta/Benchmark.Gta.exe` — оригинальный .NET 6 бинарь GameServer'а.
- `/tmp/bga_src/` — декомпилированный C# (через `ilspycmd <dll>`). Папки: `Benchmark.Gta.BusinessLogic` (CyberpunkBenchmarkService, WukongBenchmarkService, etc.), `Common.Clients` (BaseSteamClient в Common.Steam.dll), и т.д.
- `/tmp/bga_unbundled/` — извлечённые .dll файлы из exe.
- `vm_lib/{pkinit,steam_pk,pypklog}.py` — production-зависимости с master VM (`C:\Program Files (x86)\Python36-32\lib\`). В репо как **референс для изучения**, не для деплоя (на VM они уже есть).
- `SharedCapture_x64.dll` — native PE из production capture, на VM лежит в `C:\temp\`. Анализ через `objdump` + `strings`.

Восстановить декомпиляцию: `ilspycmd /workspaces/pkbench/benchmark-gta/*.dll`.

## Legacy (deprecated)

В корне репо лежат старые bash + py файлы (`prepare_vm.sh`, `bench_vm.sh`, `pull_results.sh`, `vnc_prep.sh`, `ga_cat.sh`, `run_via_ga.py`, `run_benchmark.py`, `cyberpunk_runner.py`, `init_script_reconstructed.py`, `sitecustomize.py`, `bench_psexec.bat`, `run_via_ga_launcher.bat`). **Не использовать**, заменены `benchmark/pkbench.py` + `benchmark/vm_bench.py`. Удалить когда уверен что новый стек работает на всех нужных VM.

`CONTEXT.md` в корне — устаревший дизайн-документ под legacy bash-стек. Этот `CLAUDE.md` авторитетный.
