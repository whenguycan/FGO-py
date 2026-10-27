#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""probe_mumu_adb.py - 只读探测 MuMu 模拟器的 adb 端点、分辨率、方向与游戏包。

用途:
    在启动 FGO-py 之前确认「MuMu 能不能被 adb 找到」，以及它现在是横屏还是竖屏、
    FGO 是否已安装/在前台。`-m` 启动时用的是同一套探测代码（fgoProfile.probe），
    所以这里通过 = `-m` 也能通过。

运行（仅标准库，不需要 airtest / cv2）:
    C:\\Users\\Aaron\\FGO-py.portable\\Python311\\python.exe probe_mumu_adb.py
    ... --port 16384                指定端口（等价于 --serial 127.0.0.1:16384）
    ... --serial 127.0.0.1:16416    指定端点（MuMu 多开实例）
    ... --adb "C:\\...\\adb.exe"    指定 adb 可执行文件
    ... --json                      额外输出一份机器可读结果

依赖:
    无第三方依赖；adb 依次查找 PATH -> MuMu 自带 shell\\adb.exe -> ANDROID_HOME -> airtest 内置。
退出码:
    0 探测成功；2 探测失败（会列出每个端点的失败原因与修复提示）。
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import fgoProfile
from fgoConst import PACKAGE_TO_REGION


def estimate_render(width, height, orientation):
    """airtest 在拿不到窗口帧时的回退算法：orientation 1/3 交换宽高。"""
    if orientation % 2 == 1:
        return (0, 0, height, width)
    return (0, 0, width, height)


def estimate_raw(render):
    """Android 8+ 的 screencap 返回“当前方向”的帧，所以 raw 尺寸与 render 同向。"""
    return (render[3], render[2])


def inspect(serial, adb):
    info = {'serial': serial, 'adb': adb}
    for key, args in (('size', ('wm', 'size')),):
        try:
            info[key] = fgoProfile.shell(serial, *args, adb=adb).strip()
        except fgoProfile.ProfileError as e:
            info[key] = f'<{e}>'
    try:
        dump = fgoProfile.shell(serial, 'dumpsys', 'input', adb=adb)
        match = re.search(r'SurfaceOrientation:\s*(\d+)', dump)
        info['orientation'] = int(match.group(1)) if match else 0
    except fgoProfile.ProfileError:
        info['orientation'] = 0
    try:
        info['packages'] = re.findall(r'package:([\w.]+)',
                                      fgoProfile.shell(serial, 'pm', 'list', 'packages', adb=adb))
    except fgoProfile.ProfileError:
        info['packages'] = []
    try:
        info['top'] = re.findall(r'ACTIVITY ([\w.]+/[\w.$]+)',
                                 fgoProfile.shell(serial, 'dumpsys', 'activity', 'top', adb=adb))
    except fgoProfile.ProfileError:
        info['top'] = []
    return info


def main():
    parser = argparse.ArgumentParser(description='只读探测 MuMu 的 adb 端点与屏幕状态')
    parser.add_argument('--serial', help='adb 端点，如 127.0.0.1:16384')
    parser.add_argument('--port', type=int, help='MuMu adb 端口')
    parser.add_argument('--adb', help='adb 可执行文件路径')
    parser.add_argument('--json', action='store_true', help='额外输出 JSON')
    args = parser.parse_args()

    endpoint = args.serial or (f'127.0.0.1:{args.port}' if args.port else None)
    fgoProfile.use('mumu')
    try:
        adb = args.adb or fgoProfile.find_adb()
        result = fgoProfile.probe(endpoint, adb=adb)
    except fgoProfile.ProfileError as e:
        print(f'[e] {e}', file=sys.stderr)
        return 2

    profile = fgoProfile.current()
    info = inspect(result.serial, result.adb)
    width = height = 0
    match = re.search(r'(\d+)\s*x\s*(\d+)', info.get('size', ''))
    if match:
        width, height = int(match.group(1)), int(match.group(2))
    orientation = info.get('orientation', 0)
    render = estimate_render(width, height, orientation)
    raw = estimate_raw(render)
    transform = profile.transform(list(render), raw if width and height else None)
    fgo_packages = [i for i in PACKAGE_TO_REGION if i in info['packages']]
    foreground = [i for i in info['top'] if any(i.startswith(p) for p in fgo_packages)]

    print('[OK] MuMu adb 可用')
    for line in fgoProfile.report_lines(transform, serial=result.serial,
                                        raw=raw if width and height else None,
                                        render=render, adb=result.adb):
        print(f'  {line}')
    print('  (raw/render 为 adb 估算值，精确值以 verify_mumu_screenshot.py 的输出为准)')
    print(f'  orientation : SurfaceOrientation={orientation} -> '
          f'{"横屏" if orientation % 2 == 1 else "竖屏"}')
    print(f'  FGO package : {fgo_packages or "未安装（请先在 MuMu 里安装 FGO）"}')
    print(f'  foreground  : {foreground[0] if foreground else "（不在前台，截图与点击前请切到游戏）"}')
    print(f'  canvas 映射 : 画布(640,360) -> device '
          f'{tuple(round(v) for v in transform.to_device(640, 360))}')

    if args.json:
        print(json.dumps({'probe': result.__dict__, 'render': render,
                          'orientation': orientation, 'fgo_packages': fgo_packages,
                          'foreground': foreground,
                          'canvas_center': tuple(round(v) for v in transform.to_device(640, 360))},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
