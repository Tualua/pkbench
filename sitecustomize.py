"""
sitecustomize.py — патч pklog для автономного запуска без GameServer.

Куда класть:
    C:/Program Files (x86)/Python36-32/lib/site-packages/sitecustomize.py

Срабатывает только когда установлена переменная окружения BENCHMARK_STANDALONE=1.
Под живым GameServer эта переменная не выставляется — патч не применяется.

pkinit.py не модифицируется.
"""
import os
import sys

if os.environ.get('BENCHMARK_STANDALONE') == '1':
    import importlib.util

    # pkinit лежит в lib/, рядом с нами нет — ищем явно
    _lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pkinit.py')
    _lib = os.path.normpath(_lib)  # -> C:\Program Files (x86)\Python36-32\lib\pkinit.py

    if os.path.exists(_lib):
        _spec = importlib.util.spec_from_file_location('pkinit', _lib)
        _mod = importlib.util.module_from_spec(_spec)
        sys.modules['pkinit'] = _mod   # регистрируем до exec_module
        _spec.loader.exec_module(_mod)

        # Единственный патч: pklog не вызывает Desktop.exe, просто печатает
        _mod.pklog = lambda log: print(log, flush=True)
