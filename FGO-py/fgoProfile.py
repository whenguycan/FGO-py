"""fgoProfile.py - 设备 profile：真机 / MuMu 模拟器的连接、截图后端与坐标基准（单一入口）。

全项目只保留一套 1280x720 画布（fgoConst.KEYMAP 与 fgoDetect 的检测矩形都以它为基准）。
profile 描述的是「某台设备如何映射到这张画布」：

    serial        设备 adb 端点（MuMu 由 probe() 自动探测，真机为 None = 沿用 airtest 默认设备）
    cap_method    交给 airtest 的截图后端（失败时 airtest 自动降级 MINICAP -> ADBCAP）
    min_render    识别游戏包时要求的最小渲染尺寸（竖屏/小分辨率设备需要放宽）
    auto_rotate   是否允许把竖屏/旋转过的帧归一化到横屏画布（真机固定 False，零行为变化）
    keymap        独立的坐标表（画布像素），MuMu 可单独调而不会影响真机
    coord_delta   画布->设备的固定像素修正（实测有偏移时写在这里）
    coord_scale   画布->设备映射的缩放微调（默认 1.0）

只允许通过 use() 切换 profile；其它模块一律通过 current() / default_serial() /
Profile.transform() 读取，不要各自判断设备类型。

本模块只依赖标准库（cv2 为可选，仅截图归一化用），因此探测脚本可以在没有 airtest 的环境里单独运行。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from fgoConst import KEYMAP, PACKAGE_TO_REGION

try:
    import cv2
except ImportError:
    cv2 = None

CANVAS = (1280, 720)
MUMU_ADB_ENV = 'FGO_MUMU_ADB'
MUMU_DEFAULT_ENDPOINTS = ('127.0.0.1:16384', '127.0.0.1:7555')
_MUMU_PORT_HINTS = (16384, 7555, 16416, 16448)

_current = None
_probed_serial = None
_probe_result = None


class ProfileError(RuntimeError):
    """profile 无法激活时抛出（例如 MuMu adb 探测全部失败），消息里带可执行的修复提示。"""


# --------------------------------------------------------------------------- 坐标变换


@dataclass(frozen=True)
class Transform:
    """画布(1280x720) <-> 设备 的换算。真机 rotate=0 时与旧公式逐位等价。"""

    rotate: int = 0                     # 顺时针角度：把原始帧转正到横屏画布
    scale: float = 1.0                  # 画布像素 -> 设备像素
    border: tuple = (0, 0)              # 画布方向下的黑边
    offset: tuple = (0, 0)              # 渲染区偏移（画布方向）
    frame: tuple = CANVAS               # 画布方向的设备帧尺寸
    raw: tuple = None                   # 原始帧尺寸 (h, w)
    landscape: bool = True              # 当前显示是否为横屏
    coord_delta: tuple = (0, 0)         # 画布->设备的像素修正
    coord_scale: float = 1.0            # 画布->设备的缩放微调
    canvas: tuple = CANVAS

    def _device(self, x, y):
        scale = self.scale / (self.coord_scale or 1.0)
        return (x / scale + self.border[0] + self.offset[0] + self.coord_delta[0],
                y / scale + self.border[1] + self.offset[1] + self.coord_delta[1])

    def to_canvas(self, im):
        """原始截图 -> 1280x720 画布（旋转、裁黑边/偏移、缩放）。"""
        if cv2 is None:
            raise ProfileError('需要 opencv-python 才能归一化截图（pip install opencv-python）')
        if self.rotate:
            im = _rotate(im, self.rotate)
        x, y = self.offset
        bx, by = self.border
        fw, fh = self.frame
        h, w = im.shape[:2]
        x0 = max(0, min(int(x + bx), w - 1))
        y0 = max(0, min(int(y + by), h - 1))
        x1 = max(x0 + 1, min(int(x + fw - bx), w))
        y1 = max(y0 + 1, min(int(y + fh - by), h))
        return cv2.resize(im[y0:y1, x0:x1], self.canvas, interpolation=cv2.INTER_CUBIC)

    def to_device(self, x, y):
        """画布坐标 -> airtest 触摸所用坐标（当前显示方向的空间）。"""
        fx, fy = self._device(x, y)
        if self.landscape:
            return fx, fy
        raw_h = self.raw[0] if self.raw else self.frame[1]
        return fy, raw_h - 1 - fx       # 竖屏显示：撤销 90° 旋转回原始帧/显示空间

    def to_raw(self, x, y):
        """画布坐标 -> 原始帧像素（截图标注用）。"""
        fx, fy = self._device(x, y)
        if not self.rotate:
            return fx, fy
        raw_h = self.raw[0] if self.raw else self.frame[1]
        raw_w = self.raw[1] if self.raw else self.frame[0]
        if self.rotate == 90:
            return fy, raw_h - 1 - fx
        if self.rotate == 180:
            return raw_w - 1 - fx, raw_h - 1 - fy
        return raw_w - 1 - fy, fx


def _rotate(im, degrees):
    table = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if degrees not in table:
        raise ProfileError(f'不支持的旋转角度: {degrees}（只支持 0/90/180/270）')
    return cv2.rotate(im, table[degrees])


def build_transform(render, raw_shape=None, rotate=0, delta=(0, 0), coord_scale=1.0):
    """由 render=(offset_x,offset_y,width,height) 与原始帧尺寸构造 Transform。

    rotate=0 时与改动前的 adjustOffset() 公式完全一致（真机路径）。
    """
    ox, oy = float(render[0]), float(render[1])
    rw, rh = float(render[2]), float(render[3])
    # rotate=0 走旧公式（真机行为不变）；只有真正归一化旋转时才交换宽高与偏移
    landscape = (rw > rh) or rotate == 0
    if rotate in (90, 270):
        fw, fh = (float(raw_shape[0]), float(raw_shape[1])) if raw_shape else (rh, rw)
        ox, oy = oy, ox
    else:
        fw, fh = rw, rh
    if fw * 9 > fh * 16:
        scale, border = 720 / fh, (round(fw - fh * 16 / 9) >> 1, 0)
    else:
        scale, border = 1280 / fw, (0, round(fh - fw * 9 / 16) >> 1)
    return Transform(rotate=rotate, scale=scale, border=border, offset=(ox, oy),
                     frame=(int(fw), int(fh)), raw=raw_shape, landscape=landscape,
                     coord_delta=tuple(delta), coord_scale=coord_scale)


# --------------------------------------------------------------------------- profile


@dataclass(frozen=True)
class Profile:
    name: str
    device_type: str                    # 'phone' | 'emulator'
    serial: str = None                  # None = 沿用 airtest 默认设备
    adb_candidates: tuple = ()
    cap_method: str = 'JAVACAP'
    min_render: tuple = (960, 540)      # 渲染尺寸下限（>=）：与旧实现的 >959/>539 等价
    auto_rotate: bool = False
    forced_rotate: int = None           # 0/90/180/270，调试时可钉死
    keymap: dict = None                 # 画布坐标表（独立对象）
    coord_delta: tuple = (0, 0)
    coord_scale: float = 1.0

    def transform(self, render, raw_shape=None):
        rotate = 0
        if self.auto_rotate:
            rotate = self.forced_rotate
            if rotate is None:
                # 原始帧是竖的 -> 顺时针 90° 转正；横的 -> 不用转
                rotate = 0 if not raw_shape or raw_shape[1] > raw_shape[0] else 90
        return build_transform(render, raw_shape, rotate, self.coord_delta, self.coord_scale)


REAL_PHONE = Profile(
    name='realphone', device_type='phone', serial=None, adb_candidates=(),
    cap_method='JAVACAP', min_render=(960, 540), auto_rotate=False,
    keymap=KEYMAP, coord_delta=(0, 0), coord_scale=1.0,
)

# MuMu 的坐标表是独立副本：需要为模拟器微调键位时改这里，不会影响真机。
MUMU_KEYMAP = dict(KEYMAP)
MUMU_KEYMAP.update({})

MUMU = Profile(
    name='mumu', device_type='emulator', serial=None,
    adb_candidates=MUMU_DEFAULT_ENDPOINTS,
    cap_method='JAVACAP', min_render=(200, 200), auto_rotate=True,
    keymap=MUMU_KEYMAP, coord_delta=(0, 0), coord_scale=1.0,
)

PROFILES = {'realphone': REAL_PHONE, 'mumu': MUMU}


def use(name='realphone'):
    """切换 profile 的唯一入口。不带 -m 时调用 use('realphone')，行为与改动前一致。"""
    global _current
    key = (name or 'realphone').strip().lower()
    if key not in PROFILES:
        raise ProfileError(f'未知 profile: {name!r}，可选：{", ".join(PROFILES)}')
    _current = PROFILES[key]
    return _current


def current():
    return _current or REAL_PHONE


def default_serial():
    """Device(name=None) 时使用的端点。真机返回 None -> airtest 自行选默认设备。"""
    if _probed_serial:
        return _probed_serial
    return current().serial


def set_serial(serial):
    """脚本/调试用：手动指定端点（等价于 -m --mumu-adb）。"""
    global _probed_serial
    _probed_serial = serial or None
    return _probed_serial


def probe_result():
    return _probe_result


# --------------------------------------------------------------------------- adb / 探测


@dataclass(frozen=True)
class ProbeResult:
    serial: str
    adb: str
    width: int = 0
    height: int = 0
    orientation: int = 0

    @property
    def landscape(self):
        return self.orientation % 2 == 1

    @property
    def resolution(self):
        return f'{self.width}x{self.height}' if self.width and self.height else 'unknown'


def _run(args, timeout=10):
    try:
        proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, check=False,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ProfileError(f'命令超时({timeout}s)：{args[0]} {" ".join(args[1:])}')
    except OSError as e:
        raise ProfileError(f'命令无法执行：{args[0]}（{e}）')
    return proc.stdout.decode('utf-8', 'replace').strip()


def run_adb(*args, adb=None, timeout=10):
    return _run([adb or find_adb(), *args], timeout)


def shell(serial, *args, adb=None, timeout=10):
    return _run([adb or find_adb(), '-s', serial, 'shell', *args], timeout)


def _mumu_roots():
    roots = []
    for key in ('MUMU_HOME', 'MUMU_MANAGER'):
        if os.environ.get(key):
            path = os.environ[key]
            roots.append(os.path.dirname(path) if path.lower().endswith('.exe') else path)
    for drive in ('C:', 'D:', 'E:', 'F:', 'G:'):
        roots += [f'{drive}\\MuMu',
                  f'{drive}\\Program Files\\Netease\\MuMu Player 12',
                  f'{drive}\\Program Files\\Netease\\MuMuPlayerGlobal-12.0']
    program_files = os.environ.get('ProgramFiles', r'C:\Program Files')
    roots.append(os.path.join(program_files, 'Netease', 'MuMu Player 12'))
    return [i for i in roots if i and os.path.isdir(i)]


def find_mumu_manager():
    for root in _mumu_roots():
        for candidate in (os.path.join(root, 'nx_main', 'MuMuManager.exe'),
                          os.path.join(root, 'shell', 'MuMuManager.exe')):
            if os.path.isfile(candidate):
                return candidate
    return None


def find_adb():
    """PATH -> MuMu 自带 -> ANDROID_HOME -> airtest 内置。"""
    for name in ('adb', 'adb.exe'):
        path = shutil.which(name)
        if path:
            return path
    for root in _mumu_roots():
        for candidate in (os.path.join(root, 'shell', 'adb.exe'),
                          os.path.join(root, 'nx_main', 'adb.exe')):
            if os.path.isfile(candidate):
                return candidate
    android_home = os.environ.get('ANDROID_HOME')
    if android_home:
        candidate = os.path.join(android_home, 'platform-tools',
                                 'adb.exe' if os.name == 'nt' else 'adb')
        if os.path.isfile(candidate):
            return candidate
    try:
        from airtest.core.android.adb import ADB
        return ADB.builtin_adb_path()
    except Exception:
        pass
    raise ProfileError('未找到 adb。请把 adb 加入 PATH、设置 ANDROID_HOME，'
                       '或确认 MuMu 安装目录下的 shell\\adb.exe 存在。')


def _mumu_manager_endpoint():
    """MuMu 12: MuMuManager.exe adb -v <index> 输出 {"adb_host":..., "adb_port":...}。"""
    manager = find_mumu_manager()
    if not manager:
        return None
    for index in range(4):
        try:
            out = _run([manager, 'adb', '-v', str(index)], 15)
            data = json.loads(out[out.find('{'):out.rfind('}') + 1])
        except Exception:
            continue
        host, port = data.get('adb_host'), data.get('adb_port')
        if host and port:
            return f'{host}:{port}'
    return None


def _probe_endpoint(adb, endpoint):
    """返回 (ProbeResult|None, 失败原因)。"""
    try:
        out = _run([adb, 'connect', endpoint], 8)
    except ProfileError as e:
        return None, str(e)
    lowered = out.lower()
    if 'cannot connect' in lowered or 'failed to connect' in lowered or 'unable to connect' in lowered:
        return None, (out.splitlines() or ['connect failed'])[-1] or 'connect failed'
    try:
        state = _run([adb, '-s', endpoint, 'get-state'], 8)
    except ProfileError as e:
        return None, str(e)
    if state.strip() != 'device':
        return None, f'设备状态 {state.strip() or "unknown"}（可能 offline/unauthorized）'
    try:
        size = _run([adb, '-s', endpoint, 'shell', 'wm', 'size'], 8)
        match = re.search(r'(\d+)\s*x\s*(\d+)', size)
        width, height = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        dump = _run([adb, '-s', endpoint, 'shell', 'dumpsys', 'input'], 10)
        match = re.search(r'SurfaceOrientation:\s*(\d+)', dump) or re.search(r'orientation=(\d+)', dump)
        orientation = int(match.group(1)) if match else 0
    except ProfileError:
        width = height = 0
        orientation = 0
    return ProbeResult(endpoint, adb, width, height, orientation), ''


def probe(serial=None, adb=None, verbose=True):
    """探测 MuMu 的 adb 端点；成功后写入 default_serial()。只有 mumu profile 需要调用。"""
    global _probed_serial, _probe_result
    profile = current()
    if profile.name != 'mumu':
        return None
    adb = adb or find_adb()
    explicit = serial or os.environ.get(MUMU_ADB_ENV)
    if explicit:
        candidates = [explicit]      # 显式指定端点时不做回退，避免"指定错端口却连到别的实例"
        allow_manager = False
    else:
        candidates = list(dict.fromkeys(list(profile.adb_candidates)
                                        + [f'127.0.0.1:{p}' for p in _MUMU_PORT_HINTS]))
        allow_manager = True
    failures = []
    for endpoint in candidates:
        result, reason = _probe_endpoint(adb, endpoint)
        if result:
            _probed_serial, _probe_result = result.serial, result
            if verbose:
                print(f'[MuMu] adb {result.serial}  物理分辨率 {result.resolution}  '
                      f'方向 {"横屏" if result.landscape else "竖屏"}  adb={adb}')
            return result
        failures.append((endpoint, reason))
    fallback = _mumu_manager_endpoint() if allow_manager else None
    if fallback and fallback not in candidates:
        result, reason = _probe_endpoint(adb, fallback)
        if result:
            _probed_serial, _probe_result = result.serial, result
            if verbose:
                print(f'[MuMu] 由 MuMuManager 探测到 adb {result.serial}')
            return result
        failures.append((fallback, reason))
    raise ProfileError(_probe_error(failures, adb))


def _probe_error(failures, adb):
    lines = ['MuMu adb 探测失败。已尝试：']
    for endpoint, reason in failures:
        lines.append(f'  - {endpoint}：{reason}')
    lines += [
        f'  adb: {adb}',
        '请依次确认：',
        '  1) MuMu 已启动并进入桌面（不是“启动中”）；',
        '  2) MuMu 设置 → 其他 → ADB 调试 已开启，并核对端口'
        '（MuMu 12 默认 16384，旧版/部分实例为 7555）；',
        '  3) 端口非默认时：python fgoMumu.py -m --mumu-adb 127.0.0.1:<端口>，'
        '或设置环境变量 FGO_MUMU_ADB=127.0.0.1:<端口>；',
        '  4) adb 版本冲突时先执行 adb kill-server 再重试；',
        '  5) 在 MuMu 安装目录的 nx_main 下执行 MuMuManager.exe adb -v 0 可看到真实端口。',
    ]
    return '\n'.join(lines)


# --------------------------------------------------------------------------- 报告


def report_lines(transform, *, serial=None, cap=None, raw=None, render=None, adb=None):
    """应用日志与验证脚本共用的几何报告（单一实现，避免两处各写一套）。"""
    profile = current()
    lines = [f'profile      : {profile.name} ({profile.device_type})',
             f'serial       : {serial or default_serial() or "-"}']
    if adb:
        lines.append(f'adb          : {adb}')
    if raw:
        lines.append(f'raw          : {raw[1]}x{raw[0]} ({"landscape" if raw[1] > raw[0] else "portrait"})')
    if render:
        lines.append(f'render       : {render[2]}x{render[3]} '
                     f'({"landscape" if render[2] > render[3] else "portrait"}) '
                     f'offset=({render[0]},{render[1]})')
    lines += [f'rotate       : {transform.rotate}',
              f'scale        : {transform.scale:.4f}  border={transform.border}  delta={transform.coord_delta}',
              f'canvas       : {transform.canvas[0]}x{transform.canvas[1]}']
    if cap:
        lines.append(f'capture      : {cap}')
    return lines


if __name__ == '__main__':
    print('profiles:', ', '.join(PROFILES))
    for name, profile in PROFILES.items():
        print(f'  {name:9s} type={profile.device_type:8s} serial={profile.serial} '
              f'endpoints={profile.adb_candidates} auto_rotate={profile.auto_rotate}')
