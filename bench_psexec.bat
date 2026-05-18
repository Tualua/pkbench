@echo off
REM bench_psexec.bat — wrapper для запуска run_via_ga.py через PsExec под gamer'ом.
REM
REM Зачем: bench_vm.sh вызывает PsExec через GA (Session 0). PsExec в режиме -d
REM "отваливается" сразу, не отдавая stdout/stderr — невозможно понять почему не
REM запустилось. Здесь редиректим оба потока в psexec.log, который bench_vm.sh
REM читает через guest-file-read для диагностики.
REM
REM Usage: bench_psexec.bat <gamer-password> <bench-config> <load>
REM   gamer-password: пароль локальной учётки gamer (от bench_vm.sh)
REM   bench-config:   vk | rt | 2k
REM   load:           idle | nvenc

REM Флаги:
REM   -accepteula : без EULA-prompt'а
REM   -u/-p       : токен gamer'а — нужен для Steam-DRM Cyberpunk
REM   -i 1        : явно session 1 (БЕЗ номера PsExec ищет console session
REM                 через WTSGetActiveConsoleSessionId(), на headless-VM это
REM                 ненадёжно)
REM   -d          : detached, PsExec выходит сразу, run_via_ga.py крутится дальше
REM
REM ВАЖНО: запускаем cmd.exe, а НЕ python.exe напрямую.
REM Прямой PsExec → python.exe падает с "password is incorrect" — Windows API
REM quirk в CreateProcessAsUser для конкретно этого exe (путь со спецсимволами /
REM ACL / token interaction). Те же флаги для cmd.exe работают.
REM Поэтому: PsExec запускает cmd.exe в сессии gamer'а (доказано рабочее),
REM cmd зовёт run_via_ga_launcher.bat обычным CreateProcess (без AsUser),
REM launcher запускает python.exe из уже-аутентифицированной сессии.
REM ОДНА строка-аргумент после /c (вместо двух quoted args) — иначе cmd при 4
REM кавычках применяет MSDN-правило 2: strip первой и последней кавычки. Результат:
REM `C:\benchmark\launcher.bat" "vk` — с встроенной кавычкой, файл не находится.
REM Через `call ... %~2` всё в одной квотированной строке, cmd распарсит корректно.
"C:\benchmark\PsExec.exe" -accepteula -u gamer -p "%~1" -i 1 -d cmd.exe /c "call C:\benchmark\run_via_ga_launcher.bat %~2 %~3" > "C:\benchmark\psexec.log" 2>&1
