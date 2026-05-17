import platform
is_windows = platform.system() == 'Windows'
if is_windows:
    import win32file,win32api,winreg
import os,shutil,string,subprocess,time,sys,traceback,re,argparse,base64,json,urllib.request,shlex
from pathlib import Path, WindowsPath
import configparser as cp
from collections import OrderedDict
from collections import namedtuple
from configparser import ConfigParser


def pklog(log):
    print(log, flush=True)
    if is_windows:
        desktop_path = 'C:\\temp\\Desktop.exe'
    else:
        desktop_path = '/home/gamer/.wd/desktop'
    elk = ''
    if len(log) > 2 and log[0:3].lower() == 'err' or log.startswith('CRITICAL'):
        elk = '#elk '
    subprocess.call([desktop_path, '--ext_log', '{}py: {}'.format(elk, log)])

def exit_and_close_session():
    os._exit(707)

def critical_error(log):
    pklog(f'STACK      {"".join(traceback.format_stack()[:-1])}')
    pklog('CRITICAL    {}'.format(log))
    pklog('TRACEBACK   {}'.format(traceback.format_exc()))
    
def critical_exit(log, exitcode = 2):
    critical_error(log)
    os._exit(exitcode)

def system(cmd):
    return subprocess.call(cmd, creationflags=0x08000008, shell=True)

def popen(cmdline):
    with os.popen(cmdline) as f:
        return f.read().rstrip()

def safer(url):
    utils.try_download(url, 'C:\\temp\\Safer.reg')
    system('sc start AppIDSvc')
    system('taskkill /f /im explorer.exe')
    system('reg import C:\\temp\\Safer.reg')
    system('del C:\\temp\\Safer.reg')


class disk_helper:

    def find_drive_letter(label):
        for drive in string.ascii_uppercase:
            drive = drive + ':\\'
            if os.path.exists(drive):
                info = win32api.GetVolumeInformation(drive)
                if info[0] == label:
                    return drive
        return ""
    
    def set_drive_letter(label, letter):
        import wmi
        for volume in wmi.WMI().Win32_Volume():
            if volume.Label == label:
                if volume.DriveLetter and volume.DriveLetter.upper() == letter.upper()[0:2]:
                    return True
                if volume.DriveLetter:
                    old = volume.DriveLetter + "\\"
                    pklog('INFO        set_drive_letter "%s" :: %s -> %s' % (label, old, letter))
                    win32file.DeleteVolumeMountPoint(old)
                    if os.path.exists(letter):
                        win32file.DeleteVolumeMountPoint(letter)
                win32file.SetVolumeMountPoint(letter, volume.DeviceID)
                return True
        return False
    
    def wait_for_disc(label, timeout):
        begin = time.time()
        while time.time() - begin < timeout:
            if disk_helper.find_drive_letter(label):
                return True
            else:
                time.sleep(1)
        return False

    @staticmethod
    def find_drive(label):
        by_label = '/dev/disk/by-label/'
        if os.path.isdir(by_label):
            for drive in os.listdir(by_label):
                if label == drive:
                    return by_label + label
        return ""
    
    @staticmethod
    def wait_for_disc_linux(label, timeout):
        begin = time.time()
        while time.time() - begin < timeout:
            drive = disk_helper.find_drive(label)
            if drive:
                return drive
            else:
                time.sleep(1)
        return ""

    @staticmethod
    def mount(disk):
        process = subprocess.Popen(shlex.split('udisksctl mount -b {}'.format(disk)), stderr=subprocess.PIPE)
        err = process.communicate()[1].rstrip()
        if process.wait() == 0:
            return True
        pklog(err)
        return False

    @staticmethod
    def create_proplaykey_links():
        begin = time.time()
        def create_links(launchers, games, begin):
            if not file_helper.create_symlink(Path(games, 'DD/Epic Games'), Path(launchers, 'Epic Games'), True, False):
                pklog('ERR       pkinit.create_proplaykey_links: failed to create Epic Games symlink')
            pklog('INFO      pkinit.create_proplaykey_links: done ok. Time: {} s'.format(time.time() - begin))
        create_links('D:/', 'F:/', begin)

    # init_discs() returns True for PRO servers and False otherwise
    def init_discs():
        pklog('INFO      pkinit.init_discs: begin')
        if is_windows:
            if disk_helper.find_drive_letter('Launchers'): # this branch is for PRO servers
                return True
            return False
        else:
            launchers = disk_helper.find_drive('Launchers')
            if launchers:
                    return True
            return False

class file_helper:

    def try_remove(path):
        path = Path(path)
        if path.exists() or path.is_symlink():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(str(path))
            else:
                path.unlink()
    
    def try_copyfile(src, dst, overwrite = True):
        src = Path(src)
        dst = Path(dst)
        if src.exists() and (overwrite or not dst.exists()):
            dst.parent.mkdir(parents=True, exist_ok=True)
            file_helper.try_remove(dst)
            shutil.copyfile(src, dst)
    
    def create_symlink(link, target, dir = False, overwrite = True):
        link = Path(link)
        target = Path(target)
        if overwrite or not link.exists() and not link.is_symlink():
            if dir:
                target.mkdir(parents=True, exist_ok=True)
            link.parent.mkdir(parents=True, exist_ok=True)
            file_helper.try_remove(link)
            link.symlink_to(target, True if dir else False);
        return link.exists() and link.is_symlink() and (link.resolve() == target)

    def edit(src, editor, dst = None,encoding="utf8"):
        src = Path(src)
        dst = None if dst is None else Path(dst)
        s = ''
        try:
            with src.open(mode = 'r', encoding = encoding, newline = '') as f:
                s = f.read()
        except:
            critical_error(f'file_helper.edit: open({src}) error')
        s = editor(s)
        if dst is None:
            dst = src
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open(mode = 'w', encoding = encoding, newline = '') as f:
            f.write(s)

class reg_key:
    key = None
    def __init__(self, key, subkey, access):
        self.access = access
        self.key = winreg.CreateKeyEx(key, subkey, 0, access)

    def __enter__(self):
        return self

    def __exit__(self, Type, Value, Trace):
        self.CloseKey()

    def CloseKey(self):
        winreg.CloseKey(self.key)

    def SetValue(self, value, type, data):
        winreg.SetValueEx(self.key, value, 0, type, data)

    def GetValue(self, value):
        return winreg.QueryValueEx(self.key, value)[0]

    def GetAllValues(self):
        i = 0
        res = {}
        try:
            while True:
                val = winreg.EnumValue(self.key, i)
                res[val[0]] = val[1]
                i += 1
        except OSError:
            pass
        return res

    def EnumKey(self, fn, data = None):
        i = 0
        try:
            while fn(reg_key(self.key, winreg.EnumKey(self.key, i), self.access), data):
                i += 1
        except OSError:
            pass

class battlenet:

    class Params:
        battle_net = Path()
        agent = Path()
        roaming = Path()
        db_src = Path()
        conf_src = Path()
    
    def get_params(playkey_pro, db_name):
        params = battlenet.Params()
        params.agent = Path(os.getenv('ALLUSERSPROFILE'), 'Battle.net', 'Agent')
        params.roaming = Path(os.getenv('APPDATA'), 'Battle.net')
        if playkey_pro:
            params.battle_net = Path(r'D:\Battle.net')
            params.db_src = Path(r'F:\PKDefFiles\product.db')
            params.conf_src = Path(r'F:\PKDefFiles\Battle.net\Battle.net.config')
        else:
            params.battle_net = Path(r'F:\DD\battle.net')
            params.db_src = params.battle_net.joinpath('PKDefFiles', db_name)
            params.conf_src = params.battle_net.joinpath(r'PKDefFiles\Battle.net\Battle.net.config')
        return params

    def lang_pattern(lang):
        return bytes('\x03\x32\x04%s\x3a\x04%s\x42\x08' % (lang, lang), 'utf8')
    
    def set_game_lang(db_path, lang):
        s = db_path.read_bytes()
        s = s.replace(battlenet.lang_pattern('ruRU'), battlenet.lang_pattern(lang))
        db_path.write_bytes(s)
    
    def prepare_launcher(params, lang):
        file_helper.create_symlink(params.agent.parent, params.battle_net.joinpath('Battle.net'), True)
        file_helper.try_copyfile(params.db_src, params.agent.joinpath('product.db'))
        file_helper.try_copyfile(params.conf_src, params.roaming.joinpath('Battle.net.config'), False)
        battlenet.set_game_lang(params.agent.joinpath('product.db'), lang)

    def run(params, game, lang):
        battlenet.prepare_launcher(params, lang)
        subprocess.Popen(r'"%s\Battle.net.exe" --exec="launch %s" --setlanguage="%s"' % (params.battle_net, game, lang))

class socialclub:

    class Params:
        rg_path = Path()

    def get_params(playkey_pro, lang):
        params = socialclub.Params()
        if playkey_pro:
            file_helper.create_symlink(r'F:\DD\GTA5', r'F:\GTA5', True)
            file_helper.create_symlink(r'F:\DD\Rockstar Games', r'D:\Rockstar Games', True)
            params.rg_path = Path('D:/Rockstar Games')
        else:
            params.rg_path = Path('F:/DD/Rockstar Games')
            
        file_helper.create_symlink(r'C:\Program Files\Rockstar Games', params.rg_path.joinpath('Program_Files'), True)
        file_helper.create_symlink(r'C:\Users\Gamer\AppData\Local\Rockstar Games', params.rg_path.joinpath('AppData_Local'), True)
        file_helper.create_symlink(r'C:\ProgramData\Rockstar Games', params.rg_path.joinpath('ProgramData'), True)

        reg_file = params.rg_path.joinpath('PlaykeyRegFile.reg')
        rs = re_sub()
        rs.sub(r'"Language"="\S+"','"Language"="{}"'.format(lang))
        rs.apply(reg_file)
        subprocess.run('reg import "{}" /reg:64'.format(reg_file))
            
        return params

    def run_gta5(params):
        subprocess.run(str(params.rg_path.joinpath('Launcher', 'Launcher.exe')))


class utils:

    def register_appinitdll_x64(path):
        try:
            with reg_key(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows', winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as key:
                key.SetValue('LoadAppInit_DLLs', winreg.REG_DWORD, 1)
                oldVal = key.GetValue('AppInit_DLLs')
                if oldVal is not None and oldVal != "":
                    path += "," + oldVal
                key.SetValue('AppInit_DLLs', winreg.REG_SZ, path)
        except Exception as ex:
            pklog('CRITICAL    %s' % ex)

    def try_download(url, dst, max_retries = 10):
        dst = Path(dst)
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            for retry in range(1, max_retries+1):
                try:
                    urllib.request.urlretrieve(url, dst)
                    if retry > 1:
                        pklog(f'WARNING try_download: total {retry} retries for url {url}')
                    return
                except:
                    if retry == max_retries:
                        raise

    def get_full_gpu_name():
        if is_windows:
            import wmi
            return wmi.WMI().Win32_VideoController()[0].Name
        else:
            return popen('lspci -nnd::300;lspci -nnd::302')

    def get_gpu():
        gpu = utils.get_full_gpu_name()
        if "M40" in gpu:
            return "M40"
        elif "M60" in gpu:
            return "M60"
        elif "2080 Ti" in gpu:
            return "2080ti"
        elif "2080" in gpu:
            return "2080"
        elif "1080 Ti" in gpu:
            return "1080ti"
        elif "1080" in gpu:
            return "1080"
        elif "1070 Ti" in gpu:
            return "1070ti"
        elif "1070" in gpu:
            return "1070"
        elif "1060" in gpu:
            return "1060"
        return gpu

    def get_gpuinfo():
        if is_windows:
            res = {}
            with reg_key(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\DirectX', winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                def fn(key, res):
                    vals = key.GetAllValues()
                    if vals['AdapterLuid'] != 0 and 'Basic' not in vals['Description']:
                        res.update(vals)
                        return False
                    return True
                key.EnumKey(fn, res)
            return namedtuple('GpuInfo', res.keys())(**res)
        else:
            device_id=popen('''echo $(lspci -nmmqd::300; lspci -nmmqd::302) | cut -d' ' -f4 | tr -d '"' ''')
            return namedtuple('GpuInfo', 'DeviceId')(int(device_id,16))

    def use_reg(reg_path,reg_url = None, rewrite = None):
        if is_windows:
            if rewrite:
                if os.path.exists(reg_path) : os.remove(reg_path)
            if reg_url is None and os.path.exists(reg_path):
                subprocess.call(['reg', 'import', reg_path])
                subprocess.call(['reg', 'import', reg_path, '/reg:64'])
            if not os.path.exists(reg_path) and reg_url is not None:
                utils.try_download(reg_url, reg_path)
                subprocess.call(['reg', 'import', reg_path])
                subprocess.call(['reg', 'import', reg_path, '/reg:64'])

    def symlink_for_dx(game_folder, x64_dll = True):
        if is_windows:
            if x64_dll:
                temp_file = 'C:/temp/SendInputFix_x64.dll'
                windows_file = 'C:/Windows/system32/dxgi.dll'
            else:
                temp_file = 'C:/temp/SendInputFix.dll'
                windows_file = 'C:/Windows/SysWOW64/dxgi.dll'

            file_helper.create_symlink(game_folder + 'dxgi.dll', temp_file, 0)
            file_helper.create_symlink(game_folder + 'dxgi_orig.dll', windows_file, 0)
  
class re_sub:
    def __init__(self):
        self.subs = []
        self.apps = []

    def sub(self, pattern, repl):
        self.subs.append((re.compile(pattern), repl))

    def append(self, search, app):
        self.apps.append((search, app))

    def apply(self, file,encoding="utf8"):
        def editor(text):
            for a in self.apps:
                if a[0] is None or text.find(a[0]) == -1:
                    text += a[1]
            for r in self.subs:
                text = r[0].sub(r[1], text)
            return text
        file_helper.edit(file, editor,None,encoding)

class MultiOrderedDict(OrderedDict):
    class key(str):
        def __eq__(self,other):
            return self.lower() == other.lower()
        def __hash__(self):
            return self.lower().__hash__()

    def __setitem__(self, key, value):
        if key in self:
            if isinstance(value, list):
                self[key].extend(value)
                return
            elif isinstance(value,str):
                if len(self[key])>1:
                    return
        super(MultiOrderedDict, self).__setitem__(key, value)

class microsoft_store:
    def _fix_xbox_live():
        os.system('netsh advfirewall set currentprofile state on')
        time.sleep(10)
        os.system('net stop iphlpsvc')
        os.system('net start iphlpsvc')
        time.sleep(60)
        os.system('net stop XBoxNetApiSvc')
        os.system('net start XBoxNetApiSvc')
        time.sleep(60)
        os.system('net stop XBoxNetApiSvc')
        os.system('net start XBoxNetApiSvc')

    def fix_xbox_live():
        subprocess.Popen(['python', '-c', 'from pkinit import microsoft_store as m; m._fix_xbox_live();'])

class epic:
    protocol = 'com.epicgames.launcher'

    class Params:
        launcher = Path()
        pdata = Path()
        def_files = Path()
        config = Path()

    def get_params(playkey_pro = None, epic_path = r'F:\DD\Epic Games', def_files = r'Epic'):
        if playkey_pro is None:
            playkey_pro = disk_helper.init_discs()
        params = epic.Params()
        params.launcher = Path(epic_path, 'Launcher', 'Portal', 'Binaries', 'Win64', 'EpicGamesLauncher.exe')
        params.pdata = Path(os.getenv('ALLUSERSPROFILE'), 'Epic')
        params.config = Path(os.getenv('LOCALAPPDATA'), 'EpicGamesLauncher', 'Saved', 'Config', 'Windows', 'GameUserSettings.ini')
        if playkey_pro:
            params.def_files = Path(r'F:', def_files)
        else:
            params.def_files = params.launcher.parent.joinpath(def_files)
        return params

    def prepare_re_sub(width, height, lang, wnd_rect=None):
        if wnd_rect is None:
            wnd_rect = (width/5.1, height/10, 9*width/10, 9*height/10)
        replace_window = r'Left=%d.000 Top=%d.000 Right=%d.000 Bottom=%d.000' % wnd_rect
        replace_screen = r'Left=0.000 Top=0.000 Right=%d.000 Bottom=%d.000' % (width, height)
        rs = re_sub()
        rs.append('[Internationalization]','\n[Internationalization]\nCulture=%s' % lang)
        rs.sub(r'Culture=\w+','Culture=%s' % lang)
        rs.append('[WindowSettings]','\n[WindowSettings]\nMainLauncherWindow=WindowRect=\"%s\",ScreenRect=\"%s\",ScreenDPI=1.000000,IsMaximised=\"false\"' % (replace_window, replace_screen))
        rs.sub(r'IsMaximised=\"true\"','IsMaximised=\"false\"')
        rs.sub(r'WindowRect="Left=\d+.000 Top=\d+.000 Right=\d+.000 Bottom=\d+.000"', r'WindowRect="%s"' % replace_window)
        rs.sub(r'ScreenRect="Left=\d+.000 Top=\d+.000 Right=\d+.000 Bottom=\d+.000"', r'ScreenRect="%s"' % replace_screen)
        rs.sub(r'DesiredScreenHeight=\d+','DesiredScreenHeight=%d' % height)
        rs.sub(r'DesiredScreenWidth=\d+','DesiredScreenWidth=%d' % width)
        rs.sub(r'ResolutionSizeY=\d+','ResolutionSizeY=%d' % height)
        rs.sub(r'ResolutionSizeX=\d+','ResolutionSizeX=%d' % width)
        rs.sub(r'PreferredFullscreenMode=\d+','PreferredFullscreenMode=1')
        return rs

    def write_config(config, filename, encoding):
        with open(str(filename), "+w", encoding=encoding) as file:
            for sect in config.sections():
                file.write("[{}]\n".format(sect))
                vals = config[sect]
                for val_name in vals:
                    val = config.get(sect, val_name)
                    if isinstance(val, list):
                        for vv in val:
                            if vv is not None and vv != "":
                                file.write("{}={}\n".format(val_name, vv))
                    else:
                        file.write("{}={}\n".format(val_name, val))
                file.write("\n")

    def get_file_encoding(filename):
        for encoding in ['utf8', 'utf16']:
            try:
                open(filename, encoding=encoding).read()
                return encoding
            except UnicodeDecodeError:
                pass
            except:
                break
        return 'utf8'

    def update_GameUserSettingsIni(params):
        filename = params.config
        filename.parent.mkdir(parents=True, exist_ok=True)
        config = cp.RawConfigParser(strict=False, dict_type=MultiOrderedDict)
        config.optionxform=lambda x: MultiOrderedDict.key(x)
        encoding = epic.get_file_encoding(filename)
        config.read(str(filename), encoding=encoding)

        section = 'RememberMe'
        if config.has_section(section) == False:
            config.add_section(section)
        if config.has_option(section,'Enable'):
            config.remove_option(section,'Enable')
        config[section]['Enable'] = 'True'

        epic.write_config(config, filename, encoding)
        return encoding

    def prepare_launcher(params, width, height, lang, wnd_rect=None):
        file_helper.create_symlink(params.pdata, params.def_files, True)

        rs = epic.prepare_re_sub(width, height, lang, wnd_rect)
        rs.append('[Launcher]', '\n[Launcher]\nDefaultAppInstallLocation=F:\\DD')
        rs.apply(params.config, epic.update_GameUserSettingsIni(params))

        with reg_key(winreg.HKEY_CLASSES_ROOT, epic.protocol, winreg.KEY_SET_VALUE) as key1:
            key1.SetValue('', winreg.REG_SZ, 'Epic Games Launcher Link')
            key1.SetValue('URL Protocol', winreg.REG_SZ, '')
            with reg_key(key1.key, r'DefaultIcon', winreg.KEY_SET_VALUE) as key2:
                key2.SetValue('', winreg.REG_SZ, '%s,0' % params.launcher)
            with reg_key(key1.key, r'shell', winreg.KEY_SET_VALUE) as key2:
                key2.SetValue('', winreg.REG_SZ, 'open')
                with reg_key(key2.key, r'open\command', winreg.KEY_SET_VALUE) as key3:
                    key3.SetValue('', winreg.REG_SZ, '"%s" %%1' % params.launcher)

    def is_first_run(GameUserSettings_ini, game_code):
        encoding = epic.get_file_encoding(GameUserSettings_ini)
        pattern = "^LastPlayedGame=\w+:\w+:(%s)$" % game_code
        if os.path.exists(GameUserSettings_ini):
            with open(GameUserSettings_ini, encoding=encoding) as file:
                for line in file:
                    if re.findall(pattern, line):
                        return False
        return True

    def run(params, width, height, lang, run_url, store_url='store/library', wnd_rect=None):
        if params == None:
            params = epic.get_params()

        epic.prepare_launcher(params, width, height, lang, wnd_rect)
        if not 'apps/' in run_url and not 'store/' in run_url:
        
            manifests = params.def_files.joinpath('EpicGamesLauncher/Data/Manifests')
            manifests.mkdir(parents=True, exist_ok=True)
            for f in manifests.parent.glob('*.item'):
                if '"MainGameAppName": "%s"' % run_url in f.read_text():
                    f.rename(manifests.joinpath(f.name))

            if epic.is_first_run(params.config, run_url):
                os.system('start %s://%s' % (epic.protocol, store_url))
            else:
                os.system('start %s://apps/%s?action=launch' % (epic.protocol, run_url))
        else:
            os.system('start %s://%s' % (epic.protocol, run_url))

class arguments:
    def __init__(self):
        self.parser = argparse.ArgumentParser()
        self.parser.add_argument('width', type=int)
        self.parser.add_argument('height', type=int)
        self.parser.add_argument('fps', type=int)
        self.parser.add_argument('--platform', type=str)
        self.parser.add_argument('--vm', type=str)
        self.parser.add_argument('--exe-cmd-line', '-e', help='Executable command line arguments', type=str)
        self.args, self._unknown = self.parser.parse_known_args()

    @property
    def unknown(self):
        return self._unknown

    @property
    def width(self):
        return self.args.width

    @property
    def height(self):
        return self.args.height

    @property
    def fps(self):
        return self.args.fps

    @property
    def platform(self):
        return self.args.platform

    @property
    def vm(self):
        return self.args.vm

    @property
    def exe_cmd_line(self):
        return self.args.exe_cmd_line if self.args.exe_cmd_line else ''

    @property
    def exe_cmd_line_b64decoded(self):
      return base64.b64decode(self.args.exe_cmd_line).decode('utf-8') if self.args.exe_cmd_line else ''

    def is_alpha(self):
        return len(self.unknown) != 0 and 'alpha=1' in self.unknown[0]


class inifile:
    def __init__(self, fname):
        self.fname = fname
        
    def __enter__(self):
        self.config = ConfigParser()
        self.config.read(self.fname)
        return self.config

    def __exit__(self, Type, Value, Trace):
        with open(self.fname, 'w') as f:
            self.config.write(f)

class jsonfile:
    def __init__(self, fname):
        self.fname = fname
        
    def __enter__(self):
        with open(self.fname) as f:
            self.config = json.load(f)
            return self.config

    def __exit__(self, Type, Value, Trace):
        with open(self.fname, 'w') as f:
            json.dump(self.config, f, indent=4)

class game_config:
    def download_config(config: WindowsPath, url_sub_folder: str, config_preset: str = None, symlink: bool = True, url: str = 'https://vkplaycloud.mrgcdn.ru'):
        config = Path(config)
        if config_preset:
            remote_config = config.with_name(config.stem + '_' + config_preset + config.suffix)
        else:
            remote_config = config

        url_remote_config = url + url_sub_folder + '/' + remote_config.name

        utils.try_download(url_remote_config, remote_config)
        if config_preset and symlink:
            file_helper.create_symlink(config, remote_config)

class GameShaders:
    def setup_dx_option(gpu_name: str) -> str:
        return 'DxcCache' if 'AMD' in gpu_name else 'DXCache'

    def search_minidc(game_folder: str, gpu_name: str) -> str:
        try:
            minidc_search = re.match(r"(\D+) (?P<gpu>\d+.+)", gpu_name)

            if minidc_search is None:
                return gpu_name

            gpu = minidc_search['gpu']
            shaders_path = Path(f'D:/Shaders/{game_folder}/{gpu}/')

            while not shaders_path.exists() and len(gpu.split()) > 1:
                gpu = gpu.rsplit(' ', 1)[0]
                shaders_path = Path(f'D:/Shaders/{game_folder}/{gpu}/')

            return gpu
        except Exception as ex:
            pklog(f'ERR in search_minidc_folder: {ex}')

    def search_gpu(game_folder: str) -> str:
        gpu_name = utils.get_gpu()

        if 'AMD' in gpu_name:
            return 'AMD'
        elif 'T4' in gpu_name:
            return 'T4'
        elif 'T10' in gpu_name:
            return 'T10'
        else:
            return GameShaders.search_minidc(game_folder, gpu_name)

    def search_game_path(game_folder: str, gpu_name: str = '') -> str:
        if not os.path.isdir(game_folder):
            shaders_path = os.path.join(f'D:/Shaders', game_folder, gpu_name)
            return shaders_path
        return game_folder

    def setup_dx_shaders(game_folder: str, transfer_func = shutil.move, gpu_name: str = '', dx_option: str = '', symlink: bool = False) -> None:
        if gpu_name == '':
            gpu_name = GameShaders.search_gpu(game_folder)

        if dx_option == '':
            dx_option = GameShaders.setup_dx_option(gpu_name)

        if 'AMD' in gpu_name:
            dx_cache_path = f'C:/Users/Gamer/AppData/Local/AMD/{dx_option}/'
        else:
            dx_cache_path = f'C:/Users/gamer/AppData/LocalLow/NVIDIA/PerDriverVersion/{dx_option}/'

        GameShaders.setup_shaders(game_folder, dx_cache_path, transfer_func, gpu_name, symlink)

    def setup_shaders(shaders_folder: str, shaders_cache_path: str, transfer_func = shutil.move, gpu_name: str = '', symlink: bool = False) -> None:
        try:
            if gpu_name == '':
                gpu_name = GameShaders.search_gpu(shaders_folder)

            shaders_folder = GameShaders.search_game_path(shaders_folder, gpu_name)

            if not os.path.exists(shaders_folder):
                return

            if symlink:
                path_file = Path(shaders_cache_path)
                if path_file.suffix:
                    shaders_folder = os.path.join(shaders_folder, os.path.basename(shaders_cache_path))
                file_helper.create_symlink(shaders_cache_path, shaders_folder)
                return

            os.makedirs(shaders_cache_path, exist_ok=True)

            for shader_file in os.listdir(shaders_folder):
                transfer_func(os.path.join(shaders_folder, shader_file), os.path.join(shaders_cache_path, shader_file))
        except Exception as ex:
            pklog(f'ERR in setup_shaders: {ex}')