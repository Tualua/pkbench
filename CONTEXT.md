# Проект: VM Benchmark Automation

## Цель проекта

Автономный запуск бенчмарков (Cyberpunk 2077, Black Myth: Wukong) на Windows VM без GameServer VK Play Cloud. Используется для тестирования и оптимизации производительности VM в отвязке от production-инфраструктуры.

---

## Инфраструктура

### Хост (гипервизор)
- OS: Oracle Linux 7 (el7), ядро адаптировано под QEMU 6.2 (в процессе миграции с 4.2)
- Гипервизор: QEMU/KVM + libvirt
- GPU passthrough: VFIO (физическая GPU пробрасывается в VM)
- Управление VM: `virsh`, QEMU Guest Agent
- Сеть: Mikrotik, WireGuard VPN, DDNS `*.sn.mynetname.net`, Cloudflare DNS

### VM (vm013, vm043, etc.)
- OS: Windows 10 Pro (10.0.19045)
- GPU: NVIDIA GeForce RTX 5060 8GB (VFIO passthrough, PCIe Gen4 x8)
- CPU: AMD Ryzen 9 3950X (5 vCPU, pinned)
- RAM: 16 GB + 1GB hugepages
- Диски: iSCSI (игры на `F:\launch\Steam\`)
- Дисплей: VK Play Cloud IDD (Indirect Display Driver) — `VKPKDisplay.dll`, `IndirectKmd.sys`
- Python: `C:\Program Files (x86)\Python36-32\python.exe` (Python 3.6 x86)
- Модули pkinit: `C:\Program Files (x86)\Python36-32\lib\pkinit.py`, `steam_pk.py`, etc.
- Бенчмарки: `F:\launch\Steam\steamapps\common\Cyberpunk 2077\`, Wukong Benchmark Tool

### Текущие ограничения
- QEMU 4.2 → ReBAR не работает (BAR1 = 256MB вместо 8GB). После миграции на 6.2 нужно добавить `<rom bar='on'/>` в XML
- BAR1 = 256MB → потеря ~20-40% GPU производительности
- PCIe Gen4 x8 (достаточно для RTX 5060)

---

## Архитектура решения

### Как работает GameServer (оригинал)
1. GameServer (Linux, Go) пишет Python-скрипт `C:\temp\sc<session_id>.py` в VM
2. Запускает: `cmd /c python "c:/temp/sc<id>.py" "1920" "1080" "60" "--platform=Windows" "--vm=vm013.hv05.pk.minecolo.io" > "c:/temp/sc.log<id>" 2>&1`
3. Python-скрипт (`init_script`) подготавливает окружение и запускает `Benchmark.Gta.exe`
4. `Benchmark.Gta.exe` (.NET 6, `Benchmark.Gta.BusinessLogic`) оркестрирует бенчмарки
5. Результаты пишутся в Google Sheets через `ExitService.WriteResultsAsync()`

### Наше автономное решение

```
run_benchmark.py          ← точка входа (запускать это)
init_script_reconstructed.py  ← восстановленный init_script GameServer
sitecustomize.py          ← патч pklog для работы без Desktop.exe
```

**Флоу:**
1. `run_benchmark.py` патчит `start_benchmark()` в init_script (чтобы exe не запустился из него)
2. Запускает патченный init_script → скачивает `benchmark-gta.zip`, готовит `addons/data.json`, `SystemInfo.json`, создаёт Cyberpunk shortcut
3. Перезаписывает `data.json` с нужным конфигом Cyberpunk (минуя логику dns_name)
4. Запускает `Benchmark.Gta.exe test 0 <tasks>` → `DnsName="test"` триггерит `IsDebugMode=true` → credentials читаются из stdin (файл `_creds.tmp`)

---

## Файлы проекта

### На VM (`C:\benchmark\`)
- `run_benchmark.py` — главный скрипт запуска
- `init_script_reconstructed.py` — восстановленный init_script GameServer

### На VM в site-packages
- `sitecustomize.py` → `C:\Program Files (x86)\Python36-32\lib\site-packages\`

### На хосте
- `prepare_vm.sh` — скачать ffmpeg + PsExec, задеплоить всё в VM (HTTP для больших бинарей)
- `pull_results.sh` — стянуть результаты бенчмарка с VM
- `find_vm_paths3.sh` — диагностика путей на VM
- `check_display2.sh` — диагностика дисплея VM
- `bench_psexec.bat` / `run_via_ga_launcher.bat` — внутренние Windows-обёртки (PsExec → cmd → python)

---

## Ключевые детали реализации

### sitecustomize.py
Активируется только при `BENCHMARK_STANDALONE=1` в env.
Загружает `pkinit.py` заранее и патчит `pklog` → `print()` вместо вызова `Desktop.exe --ext_log`.
`Desktop.exe` живёт постоянно в `C:\temp\` — нельзя использовать его наличие как признак GameServer.

### init_script_reconstructed.py
Восстановлен из journald-лога GameServer (строки обрезаны, отступы восстановлены вручную).
Два обрезанных места помечены комментариями.
Логика выбора конфига Cyberpunk:
- `dns_name.endswith('.i')` → `UserSettings_120_test.json` (VK Play)
- иначе → `UserSettings_RayTracing.json` (Partners)
- `'1080' in gpu_name` → `UserSettings_2k.json` (перекрывает предыдущие)
**Важно:** `run_benchmark.py` перезаписывает `data.json` после init_script, поэтому dns_name логика не важна.

### Benchmark.Gta.exe (декомпилированный .NET)
- `StartupParameters.IsDebugMode = true` если `DnsName == "test"` ИЛИ `PId == 0`
- Debug mode: читает Steam login/password из Console.ReadLine() (stdin)
- Credentials в production берёт из Google Sheets по dns_name
- Tasks передаются 3-м аргументом через `;`: `Cyberpunk;Wukong;DiskSpd`
- Доступные задачи: `Cyberpunk, Wukong, Mark, DiskSpd, Furmark, SecureBoot, SpeedTest, Masscan, Virtual`

### Cyberpunk результаты
- Путь: `C:\Users\gamer\Documents\CD Projekt Red\Cyberpunk 2077\benchmarkResults\benchmark_<datetime>\summary.json`
- Ключевые поля: `Data.averageFps`, `Data.minFps`, `Data.maxFps`, `Data.gpuName`, `Data.rayTracingEnabled`, `Data.DLSSEnabled`
- 2 прогона: первый — прогрев (результат не берётся), второй — финальный
- Shortcut запуска: `Cyberpunk2077.lnk.lnk` с флагами `-skipStartScreen -benchmark -watchdogTimeout 180`

### Конфиги Cyberpunk (CDN)
```
vk:  https://vkplaycloud.mrgcdn.ru/games/Configs/Cyberpunk/benchmark/UserSettings_120_test.json
rt:  https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_RayTracing.json
2k:  https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_2k.json
```
VK-конфиг (120fps test): RT выключен, без апскейлинга, Medium/High настройки — это **эталонный конфиг** для сравнения производительности VM.

### NVENC эмуляция
GameServer стримит через: Desktop Duplication API → NVENC → UDP.
Конфиг из лога: 1920×1080@60fps, H.264 High profile, 8 slices, 16 ref frames, 25 Mbps CBR, без lookahead/B-frames.
Эмуляция: `ffmpeg -f lavfi -i ddagrab=... -c:v h264_nvenc -preset p1 -tune ll -rc cbr ...`
IDD-дисплей совместим с Desktop Duplication API — `ddagrab` работает корректно.
ReBAR отключён (BAR1=256MB) — текущая реальная потеря производительности ~20-40%.

---

## Результаты бенчмарков (vm043, RTX 5060)

| Конфиг | averageFps | minFps | maxFps | RT | DLSS |
|--------|-----------|--------|--------|-----|------|
| RayTracing | 19.4 | 8.2 | 37.3 | ✅ | ❌ |
| VK 120fps test | 24.4 | 20.1 | 28.3 | ❌ | ❌ |

24 FPS на RTX 5060 при VK-конфиге — занижено из-за отсутствия ReBAR (BAR1=256MB).
После включения ReBAR (при переходе на QEMU 6.2) ожидается +20-40%.

---

## Использование

### Запуск бенчмарка на VM
```powershell
# На VM в PowerShell:
& "C:\Program Files (x86)\Python36-32\python.exe" C:\benchmark\run_benchmark.py <steam_login> <steam_password> Cyberpunk
```

### Деплой файлов на VM с хоста
```bash
bash prepare_vm.sh vm013    # скачать ffmpeg + PsExec и задеплоить всё
```

### Получить результаты с VM
```bash
bash pull_results.sh vm013
```

### Диагностика
```bash
bash find_vm_paths3.sh vm013   # пути Python, pkinit, Steam
bash check_display2.sh vm013   # конфигурация дисплея
```

---

## Что планируется дальше

1. **Включить ReBAR** после миграции на QEMU 6.2 — добавить `<rom bar='on'/>` в XML VM + Above 4G Decoding в BIOS хоста
2. **Убрать зависимость от Benchmark.Gta.exe** — написать собственный оркестратор который:
   - Запускает Cyberpunk через shortcut напрямую
   - Ждёт появления `summary.json`
   - Парсит результаты
   - Не требует Steam credentials
3. **Добавить Wukong** — нужно изучить `WukongBenchmarkService` из dotPeek
4. **NVENC нагрузка** — интегрировать ffmpeg симуляцию в бенчмарк для получения реалистичных результатов
5. **Автоматизация через GA** — запуск бенчмарка с хоста без RDP/PowerShell на VM

---

## Известные проблемы и решения

| Проблема | Причина | Решение |
|----------|---------|---------|
| `missing_ok=True` не работает | Python 3.6 (добавлено в 3.8) | `try/except` вокруг `unlink()` |
| `SyntaxError` в патченном init_script | Патч заменял только начало `try` блока, оставляя `except` без `try` | Заменять весь блок `try/except` целиком |
| Пробелы в пути `CD Projekt Red` ломают cmd | Экранирование в JSON через GA | Использовать Python скрипты через GA вместо cmd |
| `capture-output` в GA не работает | Старая версия QEMU GA | Писать вывод в файл, читать через `guest-file-read` |
| `run_benchmark.py` берёт RayTracing конфиг | Логика dns_name в init_script игнорирует `.i` суффикс | Перезаписывать `data.json` явно после init_script |
| VM перезагружается после бенча | `ExitService.CloseCheckAsync()` в Benchmark.Gta.exe | Ctrl-C до завершения или написать собственный оркестратор |
