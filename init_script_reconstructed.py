# Восстановлено из vm013-benchmark-gameserver.log (InitScript 1-8)
# Это скрипт-обёртка, который GameServer выполняет внутри VM-десктопа через
# подсистему subscriptions::ExecuteScript. Реальный исполняемый бенчмарк-лаунчер —
# это C:/Users/gamer/Documents/benchmark-gta/Benchmark.Gta.exe (бинарь, скачанный
# с CDN), а этот Python-скрипт лишь готовит для него окружение и запускает.
#
# ВНИМАНИЕ: отступы Python в логе были срезаны journald (длинные строки),
# поэтому они восстановлены вручную исходя из логики. Перепроверь руками
# перед запуском. Все идентификаторы, имена функций, URL-ы и константы — точно
# как в оригинале.

# actual
import zipfile
import steam_pk, os, subprocess, json, wmi, re, time, shutil
from win32com.client import Dispatch
from pkinit import disk_helper, pklog, critical_exit, utils, socialclub, exit_and_close_session, GameShaders, file_helper
from pathlib import Path
from pkinit import arguments
import ast

playkey_pro = disk_helper.init_discs()
gpu_name = utils.get_gpu()
EmptyMonitor = {}


def get_exe_params():
    try:
        exe_params = ""
        if a.exe_cmd_line:
            data = ast.literal_eval(a.exe_cmd_line)
            exe_params = data.get("exe_params", "")
        return exe_params
    except Exception as ex:
        pklog("ERR in get_exe_params: %s" % str(ex))
        return ""


a = arguments()
dns_name = a.vm
exe_params = get_exe_params()

BUILD_NAME = "benchmark-gta.zip"
EXE_DIR = "C:/Users/gamer/Documents/benchmark-gta"
EXE_PATH = f"{EXE_DIR}/Benchmark.Gta.exe"
ADDONS_PATH = f"{EXE_DIR}/addons"

VK_CYBERPUNK_CONFIG = "https://vkplaycloud.mrgcdn.ru/games/Configs/Cyberpunk/benchmark/UserSettings_120_test.json"
PARTNERS_CYBERPUNK_CONFIG = "https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_RayTracing.json"
PARTNERS_1080_CYBERPUNK_CONFIG = "https://vkplaycloud.mrgcdn.ru/Games/Configs/Cyberpunk/benchmark/UserSettings_2k.json"

DOWNLOAD_LIST = [
    {
        "CdnPath": "",  # cyberpunk config
        "DestinationFile": "C:/Users/Gamer/AppData/Local/CD Projekt Red/Cyberpunk 2077/UserSettings.json"
    },
    {
        "CdnPath": "https://vkplaycloud.mrgcdn.ru/games/Configs/Black_Myth_Wukong_Benchmark_Tool/GameUserSettings.ini",  # wukong config
        "DestinationFile": "F:/launch/Steam/steamapps/common/Black Myth Wukong Benchmark Tool/b1/Saved/Config/Windows/GameUserSettings.ini"
    }
]


def prepare_benchmark_build():
    global BUILD_NAME, ADDONS_PATH, EXE_DIR
    try:
        if os.path.exists(EXE_DIR):
            shutil.rmtree(EXE_DIR)  # реконструировано: оригинал обрезан на "shutil.rmtree(E..."
        zip_path = f"C:/Users/gamer/Documents/{BUILD_NAME}"
        unzip_path = f"C:/Users/gamer/Documents/{BUILD_NAME.replace('.zip', '')}"
        utils.try_download(f"https://vkplaycloud.mrgcdn.ru/Games/Configs/benchmark2025/{BUILD_NAME}", zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(unzip_path)
        os.remove(zip_path)
        os.mkdir(ADDONS_PATH)
    except Exception as ex:
        critical_exit(f" ERR IN Download_files --- {str(ex)}")


def add_manifests(steam_path):
    try:
        if playkey_pro:  # временный фикс, удалить после правок от серверных!!!
            library_f_pro = ('D:/Steam/steamapps/libraryfolders.vdf')
            if os.path.exists(library_f_pro):
                os.remove(library_f_pro)
            urlRemote = 'https://vkplaycloud.mrgcdn.ru/Games/Configs/library_f_pro/libraryfolders.vdf'
            utils.try_download(urlRemote, library_f_pro)
        # подкидывание манифестов Wukong
        manifest_wukong = 3132990
        result = [steam_path]
        text = Path(steam_path, 'steamapps', 'libraryfolders.vdf').read_text(encoding='utf8')
        result.extend(re.findall(r'"(.:\\.*)"', text))
        for lib_path in result:
            steam_pk.steam_copy_manifest(lib_path, manifest_wukong, -1, 'russian')
        steam_pk.steam_copy_steamworks_manifest(Path(steam_path, 'SteamworksManifests'), manifest_wukong, -1, 'russian')
    except Exception as ex:
        pklog('ERR in Adding_manifests: %s' % str(ex))


def start_benchmark():
    global EXE_PATH
    try:
        pId = os.getpid()
        subprocess.Popen(
            f'{EXE_PATH} {dns_name} {pId} {exe_params}',
            cwd=os.path.dirname(EXE_PATH),
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except Exception as ex:
        critical_exit(f"ERR IN Start_benchmark --- {str(ex)}")


def transfer_files_for_download():
    global DOWNLOAD_LIST, VK_CYBERPUNK_CONFIG, PARTNERS_CYBERPUNK_CONFIG, PARTNERS_1080_CYBERPUNK_CONFIG
    if dns_name.endswith('.i'):
        DOWNLOAD_LIST[0]["CdnPath"] = VK_CYBERPUNK_CONFIG
    else:
        DOWNLOAD_LIST[0]["CdnPath"] = PARTNERS_CYBERPUNK_CONFIG
    if '1080' in gpu_name:
        DOWNLOAD_LIST[0]["CdnPath"] = PARTNERS_1080_CYBERPUNK_CONFIG
    try:
        with open(f'{ADDONS_PATH}/data.json', 'w') as json_file:
            json.dump(DOWNLOAD_LIST, json_file, indent=4)
    except Exception as ex:
        critical_exit(f" ERR IN transferring files for download --- {str(ex)}")


def initialize_cyberpunk_shortcut():
    try:
        lnk_path = r"F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.lnk.lnk"
        target = r"F:\launch\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe"  # реконструировано: оригинал обрезан
        args = "-skipStartScreen -benchmark -watchdogTimeout 180"
        if os.path.exists(lnk_path):
            os.remove(lnk_path)
        shell = Dispatch('WScript.Shell')
        shortcut = shell.CreateShortcut(lnk_path)
        shortcut.TargetPath = target
        shortcut.Arguments = args
        shortcut.Save()
    except Exception as ex:
        pklog(f"ERR IN initialize_cyberpunk_shortcut --- {ex}")


if __name__ == "__main__":
    # увеличенный Timeout на запуск
    initialize_cyberpunk_shortcut()
    if playkey_pro:
        file_helper.create_symlink(Path("D:/Shaders"), Path("F:/Shaders"))
    try:
        GameShaders.setup_dx_shaders('Cyberpunk')
    except Exception as ex:
        pklog('ERR in shaders  %s' % str(ex))
    # скачивание нужных файлов для проверки
    prepare_benchmark_build()
    # получение информации о видеокарте/CPU/памяти/pagefile
    file = f"{ADDONS_PATH}/SystemInfo.json"
    info_gpu = utils.get_gpuinfo()
    info_cpu = wmi.WMI().Win32_Processor()[0]
    info_sys = wmi.WMI().Win32_OperatingSystem()[0]
    numbers_of_cores = info_cpu.NumberOfEnabledCore
    number_of_threads = info_cpu.NumberOfLogicalProcessors
    cpu_name = info_cpu.Name.strip()
    gpu_mem = round(int(info_gpu.DedicatedVideoMemory) / 1024 / 1024 / 1024)
    gpu_name = info_gpu.Description.strip()
    ram = round(int(info_sys.TotalVisibleMemorySize) / 1024 / 1024)
    pagefile_size = round((int(info_sys.TotalVirtualMemorySize) - int(info_sys.TotalVisibleMemorySize)) / 1024)
    data = {
        "CPU": {
            "Name": cpu_name,
            "NumberOfCores": numbers_of_cores,
            "NumberOfThreads": number_of_threads
        },
        "GPU": {
            "Name": gpu_name,
            "GpuMemorySize": gpu_mem
        },
        "RAM": {
            "MemorySize": ram,
            "PagefileSize": pagefile_size
        }
    }
    if os.path.exists(file):
        os.remove(file)
    with open(file, "w+") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    # копирование манифестов 3dmark
    steam_path = "F:/launch/Steam"
    if playkey_pro:
        steam_path = "D:/Steam"
    add_manifests(steam_path)
    # передача файлов в exe для скачивания
    transfer_files_for_download()
    # запуск exe проверки
    start_benchmark()
