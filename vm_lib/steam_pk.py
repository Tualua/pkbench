import platform
is_windows = platform.system() == 'Windows'
from ctypes import cdll, c_uint, c_buffer, byref, c_char_p, Structure, c_short, c_int, pointer, c_longlong
if is_windows:
    import winreg
try:
    import vdf
    has_vdf = True
except:
    has_vdf = False
import time, sys, os, re, shutil, stat, urllib.request, subprocess, traceback
from multiprocessing import Process, Pipe
from pathlib import Path
from pkinit import disk_helper, file_helper, pklog, critical_exit, utils, re_sub

start_time = int(time.time())
logs_steampath_error = []

def recursive_path_check(path : Path):
    logs_steampath_error.append(f'  ## Checking {path}')
    while(True):
        try:
            if path.exists():
                logs_steampath_error.append(f'  Exist [{oct(path.stat().st_mode)[-3:]}]: {str(path)}')
                break
            else:
                logs_steampath_error.append(f'  Not exist:   {str(path)}')
        except Exception as ex:
            logs_steampath_error.append(f'Path: {path} Exception: {ex}')

        if path == path.parent:
            break
        path = path.parent

def worl(win_val, linux_val):
    return win_val if is_windows else linux_val

def steam_proto(proto_cmd):
    return os.system(worl(f'start {proto_cmd}', f'steam {proto_cmd} &'))

def get_steam_path():
    if is_windows:
        try: 
            aKey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Valve\Steam', 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY)
            aValue = winreg.QueryValueEx(aKey, 'InstallPath')
            return aValue[0]
        except EnvironmentError as ex:                                          
            pklog('ERR       steam_pk: did you install the steam? %s' % ex)
            return r'C:\launch\Steam'
    else:
        return r'/home/gamer/.steam/steam'

class GUID(Structure):
    _fields_ = [("steam_instance",c_short), ("user_id",c_longlong)]
    def steam_id(self):
        high,low = divmod(self.user_id, 0x100000000)
        return low*2 + high 

class Steam:
    dll = None
    appinfo_change_numbers = {}

    def __init__(self):
        if is_windows:
            steam_path = get_steam_path() + r'/Steam.dll'
            try: 
                self.dll = cdll.LoadLibrary(steam_path)
            except EnvironmentError as ex:
                pklog('CRITICAL  steam_pk: Steam(): failed to load "%s"! %s' % (steam_path, ex))
                raise

        self.appinfo_change_numbers = self.user_app_info_change_number()

        cnt = 0
        while not self.is_running():
            print("steam_pk: waiting for startup")
            cnt += 1
            if cnt == 3:
                steam_proto('steam://open/games')
            time.sleep(1)

    def is_running(self):
        if is_windows:
            return self.dll.SteamStartup(c_uint(0xf),c_buffer(268)) != 0
        else:
            return os.system('''bash -c '[ -f ~/.steampid ] && [ "$(ps --no-header -ocomm --pid $(cat ~/.steampid))" == "steam" ]' ''') == 0

    def get_appinfo_change_number(self, user_id):
        regex = re.compile(r'\s+"AppInfoChangeNumber"\s+"(\d+)"', re.MULTILINE|re.DOTALL|re.IGNORECASE)
        config = Path(get_steam_path(), 'userdata', str(user_id), 'config', 'localconfig.vdf')
        if config.is_file():
            match = regex.search(config.read_text(errors='replace'))
            if match:
                return int(match.group(1))
        return 0

    def user_app_info_change_number(self):
        map = {}
        userdata = Path(os.path.join(get_steam_path(),'userdata'))
        if userdata.is_dir():
            for dir in userdata.iterdir():
                try:
                    user_id = int(Path(dir).name)
                    map[user_id] = self.get_appinfo_change_number(user_id)
                except ValueError as ex:
                    pklog('WARNING  steam_pk: not a number in userdata: %s' % Path(dir).name)
        return map

    def get_steam_id(self):
        if is_windows:
            return self.get_user().steam_id()
        else:
            return self.get_user()

    def get_user(self):
        wait = False
        if is_windows:
            guid = GUID() 
            name = c_char_p(255)
            size = c_uint(0)
            while True:
                self.dll.SteamGetUser(name,c_uint(255),pointer(size),byref(guid),c_buffer(268))
                if guid.user_id == 0 :
                    wait = True
                    print("steam_pk: waiting for user")
                    time.sleep(1)
                else:
                    break
            result = guid
            steam_id = guid.steam_id()
        else:
            loginusers = Path(get_steam_path(), 'config', 'loginusers.vdf')
            while not loginusers.exists():
                print("steam_pk: waiting for user")
                time.sleep(1)

            while True:
                try:
                    with open(loginusers) as f:
                        lu = vdf.parse(f)
                    for u in lu['users'].items():
                        if u[1]['MostRecent'] == '1' and int(u[1]['Timestamp']) >= start_time:
                            result = steam_id = int(u[0]) & 0xFFFFFFFF
                            break
                    else:
                        wait = True
                        print("steam_pk: waiting for user")
                        time.sleep(1)
                        continue
                    break
                except Exception as ex:
                    pklog(f'WARNING  steam_pk: get_steam_id() exception: {ex}')
                time.sleep(1)
                
        if wait:
            pklog(f'INFO      steam_pk: user ({steam_id}) has logged in')
        return result

    def localconfig_vdf(self, steam_id):
        return Path(get_steam_path(), 'userdata', str(steam_id), 'config', 'localconfig.vdf')

    def wait_for_appinfo_change(self):
        steam_id = self.get_steam_id()
        config = self.localconfig_vdf(steam_id)
        appinfo_change_number = 0

        if steam_id in self.appinfo_change_numbers:
            appinfo_change_number = self.appinfo_change_numbers[steam_id]

        while not config.is_file():
            print("steam_pk: waiting for {0} to appear".format(config.absolute()))
            time.sleep(1)
        
        new_appinfo_change_number = self.get_appinfo_change_number(steam_id)
        while appinfo_change_number == new_appinfo_change_number:
            print("steam_pk: waiting for game info to change at {0}. Now it is {1}".format(config.absolute(), appinfo_change_number))
            new_appinfo_change_number = self.get_appinfo_change_number(steam_id)
            time.sleep(1)

        print ("steam_pk: appinfo changed from {0} to {1}".format(appinfo_change_number, new_appinfo_change_number))
        
        software_re = re.compile(r'"Software".*\{.*"Valve".*\{.*"Steam".*\{.*"Apps".*{', re.MULTILINE|re.DOTALL|re.IGNORECASE)
        while not software_re.search(config.read_text(errors='replace')):
            print("steam_pk: waiting for game info to appear at {0}".format(config.absolute()))
            time.sleep(1)

    
    def check_ownership(self, game_id, wait=False):
        try:
            if wait:
                self.wait_for_appinfo_change()
            
            if os.getenv('DISABLE_OWNERSHIP_CHECK','0') == '1':
                return True

            result = False
            if is_windows:
                owned = c_int(0)
                steam_user = self.get_user()
                while self.dll.SteamCheckAppOwnership(c_uint(game_id),byref(owned),byref(steam_user),c_buffer(268)) == 0:
                    print('steam_pk: SteamCheckAppOwnership returned 0, retrying')
                    time.sleep(1)

                result = owned.value != 0
            else:
                steam_id = self.get_steam_id()
                localconfig = self.localconfig_vdf(steam_id)

                while True:
                    try:
                        with open(localconfig) as f:
                            return f'"{game_id}"' in f.read()
                    except Exception as ex:
                        pklog(f'WARNING  steam_pk: check_ownership() exception: {ex}')
                    time.sleep(1)
            pklog(f'INFO      steam_pk: check_ownership() complete. game {game_id} {result}')
            return result
            
        except Exception:
            pklog('CRITICAL  steam_pk: check_ownership(): %s' % traceback.format_exc())
            raise


GOOD_EXIT_CODE = 1113 if is_windows else 113 # 1 byte exit code on linux

def move_folder(src, dst):
    if src.is_dir() and dst.is_dir():
        for file in src.iterdir():
            move_folder(file, dst.joinpath(file.name))
    else:
        try:
            src.replace(dst)
        except OSError as ex:
            pklog('ERR       steam_pk: replace() failed! %s' % ex)

def steam_check_game_worker(steam_id, source_path=None, dest_path=None, wait=True):
    try:
        if Steam().check_ownership(steam_id, wait=wait):
            if source_path is not None and dest_path is not None:
                move_folder(Path(source_path), Path(dest_path))
            sys.exit(GOOD_EXIT_CODE)
        else:
            sys.exit(-1)
    except Exception:
        sys.exit(0)
        
def execute(target, args):
    p = Process(target = target, args = args)
    p.start()
    p.join()
    return p.exitcode
        
def steam_check_game(steam_id, source_path=None, dest_path=None, wait=True):
    return GOOD_EXIT_CODE == execute(target = steam_check_game_worker, args = (steam_id, source_path, dest_path, wait))

def steam_copy_dlcs(dlcs):
    for dlc in dlcs:
        if len(dlc) > 3:
            pklog('WARNING   steam_pk: steam_copy_dlcs: the 4th arg of dlc is ignored!')
        steam_check_game(dlc[0], dlc[1], dlc[2], False)

def steam_launch(steam_id, params):
    if not params and os.getenv('DISABLE_OWNERSHIP_CHECK','0') == '1':
        steam_proto(f'steam://rungameid/{steam_id}')
    else:
        if is_windows:
            os.system(get_steam_path() + '/steam.exe -applaunch %d %s' % (steam_id, params))
        else:
            os.system(f'steam -applaunch {steam_id} {params}')

def steam_run_game_or_store(steam_id, params):
    steam_proto('steam://open/games')
    if steam_check_game(steam_id):
        steam_launch(steam_id, params)
    else:
        steam_proto(f'steam://store/{steam_id}')

def steam_run_game(steam_id, params, dlcs):
    while not steam_check_game(steam_id):
        time.sleep(1)
    
    if dlcs is not None:
        steam_copy_dlcs(dlcs)

    m = re.search('(^)(?P<quote>[\"\'])?(.+?)(?(quote)(?P=quote)| )', params)
    if m is not None and Path(m[3]).exists():
        os.system(params)
    else:
        steam_launch(steam_id, params)

def steam_edit_manifest(src, dst, state = -1, lang = None):
    steam_edit_manifest.installdir = None
    def editor(text):
        text = re.sub(r'("LastOwner"\s*)"\d+"', r'\1""', text)
        if state >= 0 :
            text = re.sub(r'("StateFlags"\s*)"\d+"', r'\1"%d"' % state, text)
        if lang is not None :
            text = re.sub(r'("language"\s*)".*"', r'\1"%s"' % lang, text)
        steam_edit_manifest.installdir = re.search(r'"installdir"\s*"(.+)"', text).group(1)
        logs_steampath_error.append(f'++++  ++++  editor : installdir = {steam_edit_manifest.installdir}')
        return text
        
    if dst.exists():
        os.chmod(dst, stat.S_IWRITE)
        if not src.exists():
            src = dst
    if src.exists():
        file_helper.edit(src, editor, dst)
        logs_steampath_error.append(f'++++  steam_edit_manifest : {src} found. installdir = {steam_edit_manifest.installdir}')
    else:
        pklog(f'INFO       steam_pk.steam_edit_manifest: {src} does not exists')
        logs_steampath_error.append(f'steam_edit_manifest : {src} does not exists')
        recursive_path_check(Path(src))
    return steam_edit_manifest.installdir

default_appmanifest_228980 = '''"AppState"
{
	"appid"		"228980"
	"Universe"		"1"
	"name"		"Steamworks Common Redistributables"
	"StateFlags"		"4"
	"installdir"		"Steamworks Shared"
	"LastUpdated"		"1605364276"
	"UpdateResult"		"0"
	"SizeOnDisk"		"0"
	"buildid"		"4685643"
	"LastOwner"		"76561198028040638"
	"BytesToDownload"		"0"
	"BytesDownloaded"		"0"
	"AutoUpdateBehavior"		"0"
	"AllowOtherDownloadsWhileRunning"		"0"
	"ScheduledAutoUpdate"		"0"
	"InstalledDepots"
	{
	}
	"UserConfig"
	{
		"betakey"		"public"
	}
}
'''

def steam_copy_manifest(library_path, steam_id, state = -1, lang = None):
    try:
        src = Path(library_path, 'steamapps/manifests/appmanifest_%d.acf' % steam_id)
        dst = Path(library_path, 'steamapps/appmanifest_%d.acf' % steam_id)
        return steam_edit_manifest(src, dst, state, lang)

    except OSError as ex:
        if ex.errno == 13:
            recursive_path_check(Path(ex.filename))
        pklog(f'ERR       steam_pk: steam_copy_manifest : {repr(ex)}')
        logs_steampath_error.append(f'steam_copy_manifest : {repr(ex)}')
        logs_steampath_error.append(traceback.format_exc())

    except Exception as ex:
        pklog('ERR       steam_pk: steam_copy_manifest : %s' % ex)
    return None

def steam_copy_steamworks_manifest(steamworks_path, steam_id, state = -1, lang = None):
    try:
        src = Path(steamworks_path, r'%s_appmanifest_228980.acf' % steam_id)
        dst = Path(get_steam_path(), r'steamapps/appmanifest_228980.acf')
        appinfo = Path(get_steam_path(), 'appcache/appinfo.vdf')
        #logs_steampath_error.append(f'steam_edit_manifest : appinfo.exists = {appinfo.exists()}')
        if dst.exists():
            dst.unlink()
        if src.exists() and appinfo.exists():
            steam_edit_manifest(src, dst, state, lang)
        else:
            with dst.open(mode = 'w', encoding = 'utf8', newline = '') as f:
                f.write(default_appmanifest_228980)

    except OSError as ex:
        if ex.errno == 13:
            recursive_path_check(Path(ex.filename))
        pklog(f'ERR       steam_pk: steam_copy_steamworks_manifest : {repr(ex)}')
        logs_steampath_error.append(f'steam_copy_steamworks_manifest : {repr(ex)}')
        logs_steampath_error.append(traceback.format_exc())

    except Exception as ex:
        pklog('ERR       steam_pk: steam_copy_steamworks_manifest : %s' % ex)

def steam_move_manifests(steam_free_ids, steam_path, library_path, lang = None):
    steam_path_pk = Path(library_path, 'steamapps/manifests')
    steam_path = Path(steam_path, 'steamapps')
    for manifest_pk in steam_path_pk.glob('*.acf') :
        try:
            manifest = steam_path.joinpath(manifest_pk.name)
            state = -1 if re.search(r'appmanifest_(\d+).acf', manifest.name).group(1) in steam_free_ids else 1
            if manifest.exists() :
                text = manifest.read_text(encoding = 'utf8')
                state = -1 if re.search(r'"StateFlags"\s*"1"', text) is None else 1
                lang = re.search(r'"language"\s*"(\w+)"', text).group(1)
            steam_edit_manifest(manifest_pk, manifest, state, lang)

        except Exception as ex:
            pklog('ERR       steam_pk: steam_move_manifests: %s \n%s' % (str(manifest), ex))

def steam_remove_manifests(steam_path, library_paths):
    for manifest in Path(steam_path, 'steamapps').glob('*.acf') :
        try:
            manifest_exists = False
            for library in library_paths:
                if Path(library, r'steamapps/manifests', manifest.name).exists() :
                    manifest_exists = True
                    break
            if not manifest_exists :
                manifest.unlink()
        except Exception as ex:
            pklog('ERR       steam_pk: steam_remove_manifests: %s \n%s' % (str(manifest), ex))


def steam_copy_manifests(steam_free_ids, profile_path, library_paths, remove_user_manifests = True, lang = None):
    if remove_user_manifests :
        steam_remove_manifests(profile_path, library_paths)
    for library in library_paths :
        steam_move_manifests(steam_free_ids, profile_path, library, lang)


def steam_set_language(language):
    try: 
        if is_windows:
            aKey = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'SOFTWARE\Valve\Steam\steamglobal')
            winreg.SetValueEx(aKey, 'Language', 0, winreg.REG_SZ, language)
        else:
            r = {}
            registry = Path('/home/gamer/.steam/registry.vdf')
            if registry.exists():
                with open(registry) as f:
                    r = vdf.parse(f)

            if 'Registry' not in r or 'Initialized' not in r['Registry']:
                r = { 'Registry': { 'Initialized': '1', 'HKLM': {'Software': {'Valve': {'Steam': {'SteamPID': '0', 'TempAppCmdLine': '', 'ReLaunchCmdLine': '', 'ClientLauncherType': '0'}}}}, 'HKCU': {'Software': {'Valve': {'Steam': {'RunningAppID': '0', 'steamglobal': {'language': 'russian'}, 'language': 'russian', 'RememberPassword': '1'}}}}}}
            r['Registry']['HKCU']['Software']['Valve']['Steam']['language'] = language
            r['Registry']['HKCU']['Software']['Valve']['Steam']['steamglobal']['language'] = language
            with open(registry, 'w') as f:
                vdf.dump(r, f, pretty=True)
    except Exception as ex:
        pklog('ERR       steam_pk: steam_set_language: %s' % ex)

# deprecated!
def steam_win10_ready():
    pklog('WARNING  steam_pk: using deprecated steam_win10_ready()')
        
def steam_run_game_helper(pipe, steam_id, params, dlcs):
    pipe.send(True)
    steam_run_game(steam_id, params, dlcs)

def steam_run_game_async_helper(steam_id, params, dlcs):
    parent_pipe, child_pipe = Pipe()
    Process(target=steam_run_game_helper, args=(child_pipe, steam_id, params, dlcs)).start()
    parent_pipe.recv()
    os._exit(0)


def apply_reg_file(url = 'https://vkplaycloud.mrgcdn.ru/Games/Configs/steam/PlaykeyRegFile.reg'):
    if not is_windows:
        return
    try:
        reg_file='c:/temp/steampkregfile.reg'
        if not os.path.exists(reg_file) :
            utils.try_download(url, reg_file)
            subprocess.run('reg import "{}" /reg:64'.format(reg_file))
    except Exception as ex:
        pklog('ERR   steam_pk: apply_reg_file: {}'.format(ex))


def steam_run_game_async(steam_id, dlcs = [], params = ""): # dlcs = [ ( steam_id, source_path, dest_path ), ( steam_id, source_path, dest_path ) ]
    apply_reg_file()
    execute(target=steam_run_game_async_helper, args=(steam_id, params, dlcs))

# deprecated    
def steam_run_dlcs(steam_id, dlcs = [], params = ""):
    pklog('WARNING   steam_pk: steam_run_dlcs deprecated! use steam_run_game_async instead!')
    steam_run_game_async(steam_id, dlcs, params)

# deprecated    
def steam_run_standard(steam_id, params = ""):
    pklog('WARNING   steam_pk: steam_run_standard deprecated! use steam_run_game_async instead!')
    steam_run_game_async(steam_id, None, params)

def steam_run_free(steam_id):
    apply_reg_file()
    steam_proto('steam://nav/games')
    time.sleep(2)
    steam_proto(f'steam://rungameid/{steam_id}')

def steam_copy_manifests_for_free_games(library_paths, game_ids, lang = None):
    for path in library_paths:
        for id in game_ids:
            steam_copy_manifest(path, id, -1, lang)
    
def steam_get_libraries(steam_path):
    result = []
    try:
        if is_windows:
            text = Path(steam_path, 'steamapps', 'libraryfolders.vdf').read_text(encoding = 'utf8')
            result.extend(re.findall(r'"(.:\\.*)"', text))
        else:
            with open(Path(steam_path, 'steamapps', 'libraryfolders.vdf')) as f:
                r = vdf.parse(f)
            for k in r['libraryfolders'].keys():
                if k.isdigit():
                    result.append(r['libraryfolders'][k]['path'])
    except Exception as ex:
        pklog('ERR       steam_pk: steam_get_libraries: %s' % ex)

    p = os.path.normpath(steam_path)
    if p.lower() not in (os.path.normpath(p).lower() for p in result):
        result.append(p)
    return result

    
def update_steam_paths(steam_path):
    if not is_windows:
        return
    
    steam_path = Path(steam_path)
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\\Valve\\Steam', 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_32KEY)
        winreg.SetValueEx(key, 'InstallPath', 0, winreg.REG_SZ, str(steam_path))
        winreg.CloseKey(key)
    except Exception as ex:
        pklog('ERR       steam_pk: update_steam_paths: failed to update InstallPath: {}'.format(ex))

    try:
        key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r'steam\\Shell\\Open\\Command', 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        value = winreg.QueryValue(key, '')
        regex = re.compile(r'[a-z]\:.*\.exe', re.IGNORECASE)
        found = regex.search(value)
        value = value[:found.start()] + str(Path(steam_path, 'Steam.exe')) + value[found.end():]
        winreg.SetValueEx(key, '', 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
    except Exception as ex:
        pklog('ERR       steam_pk: update_steam_paths: failed to update shell: {}'.format(ex))

def prepare_launcher(launcherLang, steam_path, steam_id, free_ids = [], lang = None):
    if lang is None:
        lang = launcherLang
    if is_windows:
        update_steam_paths(steam_path)
    else:
        steam_path = get_steam_path()
    exe_path = Path(steam_path, worl('Steam.exe', 'ubuntu12_32/steam'))
    if not exe_path.exists() :
        critical_exit(f'{exe_path} does not exists!')
    
    steam_set_language(launcherLang)
    
    steam_copy_steamworks_manifest(Path(steam_path, 'SteamworksManifests'), steam_id, -1, lang)
    installdir = None
    for lib_path in steam_get_libraries(steam_path) :
        logs_steampath_error.append(f'#### lib: {lib_path}')
        if installdir is None :
            dir = steam_copy_manifest(lib_path, steam_id, -1, lang)
            if dir is not None :
                installdir = Path(lib_path, r'steamapps/common', dir)
                logs_steampath_error.append(f'>>>>  prepare_launcher : installdir = {installdir} <<<<')
            else:
                pklog('DEBUG     steam_run::steam_copy_manifest failed: lib_path %s steam_id %s' % (lib_path, steam_id))
        for id in free_ids :
            steam_copy_manifest(lib_path, id, -1, lang)
    
    if installdir is None or not installdir.exists() or len(logs_steampath_error) < 150:
        for l in logs_steampath_error:
            pklog(f"DEBUG     {l.encode('utf-8')}")
    if installdir is None or not installdir.exists() :
        critical_exit(f'installdir ({installdir}) does not exists! steam_id {steam_id}')

    return installdir

def steam_run(launcherLang, steam_path, steam_id, dlcs = [], params = "", free_ids = [], lang = None):
    installdir = prepare_launcher(launcherLang, steam_path, steam_id, free_ids, lang)

    dlcs = [ ( dlc[0], str(installdir.joinpath(dlc[1])), str(installdir.joinpath(dlc[2])) ) for dlc in dlcs ]
    
    change_to_online_mode()
    steam_run_game_async(steam_id, dlcs, params)
    return installdir
    
def apply_skin():
    if not is_windows:
        return
    try:
        skin_name = 'tomato'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'SOFTWARE\Valve\Steam') as key:
            winreg.SetValueEx(key, 'SkinV5', 0, winreg.REG_SZ, skin_name)

        steam_path = get_steam_path()

        res_styles = Path(r'{}\skins\{}\resource\styles'.format(steam_path, skin_name))
        steam_cached = Path(r'{}\skins\{}\steam\cached'.format(steam_path, skin_name))

        res_styles.mkdir(parents=True, exist_ok=True)
        steam_cached.mkdir(parents=True, exist_ok=True)

        shutil.copyfile(steam_path + r'\resource\styles\steam.styles', res_styles / 'steam.styles')
        with open(steam_cached / 'AddShortcutDialog.res', 'w') as f:
            res = '''"steam/cached/AddShortcutDialog.res"
{
	layout
	{
		place { controls=AddSelectedButton,BrowseButton,AppList,Label1 height=0 width=0 margin-left=-9999 }
	}
}'''
            f.write(res)

        with open(steam_cached / 'SettingsSubInterface.res', 'w') as f:
            res = '''"steam/cached/SettingsSubInterface.res"
{
	layout
	{
		place { controls=SkinCombo,Label3 height=0 width=0 margin-left=-9999 }
	}
}
'''
            f.write(res)
    except Exception as ex:
        pklog('ERR       steam_pk: apply_skin: %s' % ex)

def set_vdf_key(vdf_path, key_path, new_key, new_value):
    if not has_vdf:
        return
    if Path(vdf_path).exists():
        with open(vdf_path) as f:
            new_vdf = data = vdf.load(f)
    else:
        new_vdf = data = {}
    for key in key_path:
        node = data.get(key)
        if node is None:
            data.update({key: {}})
            data = data.get(key)
        else:
            data = node
    
    data.update({new_key: new_value})
    with open(vdf_path, 'w') as f:
        vdf.dump(new_vdf, f, pretty=True)

def get_steam_id32():
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam\ActiveProcess', reserved=0, access=winreg.KEY_READ)
    steam_id32 = winreg.QueryValueEx(key,'ActiveUser')[0]
    while steam_id32 == 0:
        time.sleep(0.1)
        steam_id32 = winreg.QueryValueEx(key,'ActiveUser')[0]
    return steam_id32

def get_steam_id64():
    return get_steam_id32() + 76561197960265728

def change_to_online_mode():
    login_file = Path("D:/Steam/config/loginusers.vdf")
    if login_file.exists():
        rs = re_sub()
        rs.sub(r'(WantsOfflineMode\".+)\"1', r'\1"0')
        rs.apply(login_file)
        rs.subs = []

# if __name__ == "__main__":
    #steam_run_game(560130)
    #steam_check_game(730)
    #steam_copy_manifest("C:/games/SteamLibrary", 379720)
    #steam_copy_manifests(["570", "730", "440"], "F:/launch/Steam/", ["F:/Steam/SteamLibrary/", "F:/launch/Steam/"])
    #steam_set_language("russian")
    #steam_win10_ready()
    #steam_run_standard(271590)

