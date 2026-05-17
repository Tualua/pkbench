@echo off
REM run_via_ga_launcher.bat — внутренний launcher для запуска run_via_ga.py.
REM
REM Зачем: bench_psexec.bat пытался запустить python.exe напрямую через PsExec,
REM но CreateProcessAsUser для python.exe выдаёт ERROR_LOGON_FAILURE
REM ("password is incorrect") — Windows API quirk для путей со спецсимволами
REM или ACL-related. Те же флаги PsExec для cmd.exe работают.
REM
REM Решение: PsExec запускает cmd.exe (через bench_psexec.bat), cmd зовёт ЭТОТ
REM .bat обычным CreateProcess (а не AsUser), .bat запускает python.exe тем же
REM CreateProcess в уже-аутентифицированной сессии gamer'а.
REM
REM Usage: вызывается из bench_psexec.bat → PsExec → cmd /c этот файл.
REM Аргумент: <bench-config> (vk | rt | 2k).

"C:\Program Files (x86)\Python36-32\python.exe" "C:\benchmark\run_via_ga.py" "%~1"
