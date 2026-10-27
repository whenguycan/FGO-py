#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""verify_mumu_screenshot.py - 截图 + 点击位置标注 + 坐标/方向报告（MuMu / 真机通用）

用途:
    用与 FGO-py 完全相同的连接与归一化路径（fgoMumu.install() 之后的
    fgoMumuAndroid.Android）截一帧，把 1280x720 画布坐标换算成设备坐标，
    用「圆点 + 十字 + 坐标文字」标在图上，
    用来确认 `-m` 之后坐标是否已经切到 MuMu、以及旋转/宽高交换是否正确。
    标注前会打印 profile、设备类型、实际截图分辨率与实际方向（横/竖）。

运行（在 FGO-py 源码目录，使用自带 airtest 的便携 Python）:
    C:\\Users\\Aaron\\FGO-py.portable\\Python311\\python.exe verify_mumu_screenshot.py
    ... --profile realphone            真机对照（不带 -m 的那套坐标）
    ... --all                          标注全部键位（fgoConst.KEYMAP）
    ... --tap                          真的点一下每个标注点（默认只画不点）
    ... --port 16384                   指定 MuMu adb 端口
    ... --serial 127.0.0.1:16384       指定 adb 端点（如多开实例）
    ... --force-rotate 90              钉死旋转角度（自动判断不对时用）
    ... --selfcheck                    只做坐标数学等价检查，不连设备

依赖:
    airtest（便携 Python311 已安装）、opencv-python、numpy；
    adb 由 MuMu 自带 / airtest 内置 / PATH 提供。不需要 PySide6、pponnxcr。

输出（默认写到本脚本所在目录）:
    mumu_click_verify_<时间戳>.png        1280x720 归一化画布 + 标注
    mumu_click_verify_<时间戳>_raw.png    原始方向帧 + 标注（证明旋转与分辨率）
    mumu_click_verify_<时间戳>_after.png  --tap 时点击之后的画面
退出码:
    0 成功；2 连接/截图失败（会打印具体原因与修复提示）。
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

DEFAULT_POINTS = (
    ('menu', 1185, 644),        # fgoDetect MENU 矩形中心 (1104,613)-(1267,676)
    ('attack', 1182, 658),      # ATTACK 矩形中心 (1155,635)-(1210,682)
    ('card1', 185, 440),        # KEYMAP '1'
    ('card4', 875, 440),        # KEYMAP '4'
    ('apple_silver', 875, 440),
)


def legacy_scale_border(render):
    """改动前 fgoAndroid.adjustOffset() 的公式，用于证明真机路径零变化。"""
    x, y, w, h = render
    if w * 9 > h * 16:
        return 720 / h, (round(w - h * 16 / 9) >> 1, 0)
    return 1280 / w, (0, round(h - w * 9 / 16) >> 1)


def selfcheck():
    import fgoProfile
    problems = []
    real = fgoProfile.PROFILES['realphone']
    cases = [(0, 0, 1280, 720), (0, 0, 960, 540), (0, 0, 1080, 2400), (0, 0, 1440, 2960),
             (0, 0, 2340, 1080), (143, 0, 1080, 2160), (0, 100, 1080, 2340)]
    for render in cases:
        transform = real.transform(list(render), None)
        scale, border = legacy_scale_border(render)
        if abs(transform.scale - scale) > 1e-12 or tuple(transform.border) != border or transform.rotate != 0:
            problems.append(f'{render}: scale {transform.scale} vs {scale}, border {transform.border} vs {border}, rotate {transform.rotate}')
        for point in ((0, 0), (640, 360), (1279, 719)):
            want = (round(point[0] / scale + border[0] + render[0]), round(point[1] / scale + border[1] + render[1]))
            got = tuple(round(v) for v in transform.to_device(*point))
            if want != got:
                problems.append(f'{render} {point}: {got} != {want}')
    if fgoProfile.PROFILES['mumu'].keymap is fgoProfile.PROFILES['realphone'].keymap:
        problems.append('mumu keymap 与真机共用同一对象（应为独立副本）')
    # MuMu 横屏显示 960x540、原始帧为竖屏 540x960：应旋转 90° 且画布中心落在设备中心
    transform = fgoProfile.PROFILES['mumu'].transform([0, 0, 960, 540], (960, 540))
    if transform.rotate != 90 or abs(transform.scale - 720 / 540) > 1e-12:
        problems.append(f'mumu rotate/scale: {transform.rotate} {transform.scale}')
    center = tuple(round(v) for v in transform.to_device(640, 360))
    if center != (480, 270):
        problems.append(f'mumu canvas center -> {center} != (480, 270)')
    raw = tuple(round(v) for v in transform.to_raw(640, 360))
    if raw != (270, 479):
        problems.append(f'mumu canvas center -> raw {raw} != (270, 479)')
    # MuMu 横屏 + 横屏帧：不需要旋转
    transform = fgoProfile.PROFILES['mumu'].transform([0, 0, 960, 540], (540, 960))
    if transform.rotate != 0 or tuple(round(v) for v in transform.to_device(640, 360)) != (480, 270):
        problems.append(f'mumu landscape frame: rotate {transform.rotate}')
    if problems:
        print('[FAIL] 坐标数学检查未通过：')
        for problem in problems:
            print('  -', problem)
        return 2
    print(f'[PASS] 真机公式等价 {len(cases)} 个分辨率；MuMu 旋转/宽高交换/独立 keymap 检查通过')
    return 0


def parse_points(text):
    points = []
    for chunk in text.replace(';', '|').split('|'):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, _, coords = chunk.partition(':')
        x, _, y = coords.partition(',')
        try:
            points.append((label.strip() or f'p{len(points)}', int(x), int(y)))
        except ValueError:
            raise SystemExit(f'--points 格式应为 label:x,y;label:x,y ，收到：{chunk!r}')
    return points or list(DEFAULT_POINTS)


def collect_points(args):
    import fgoProfile
    if args.points:
        return parse_points(args.points)
    if args.all:
        return [(f'vk{ord(key):02X}', point[0], point[1])
                for key, point in fgoProfile.current().keymap.items()]
    return list(DEFAULT_POINTS)


def draw(im, items, title):
    import cv2
    out = im.copy()
    height, width = out.shape[:2]
    for label, x, y in items:
        x, y = int(round(x)), int(round(y))
        cv2.circle(out, (x, y), 8, (0, 0, 255), -1, cv2.LINE_AA)
        cv2.circle(out, (x, y), 16, (0, 0, 255), 1, cv2.LINE_AA)
        cv2.line(out, (x - 22, y), (x + 22, y), (0, 255, 255), 2, cv2.LINE_AA)
        cv2.line(out, (x, y - 22), (x, y + 22), (0, 255, 255), 2, cv2.LINE_AA)
        text = f'{label} ({x},{y})'
        tx = x - 190 if x + 190 > width else x + 18    # 右侧放不下就画到左边
        ty = y - 12 if y > 30 else y + 30
        cv2.putText(out, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(out, text, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, .5, (0, 255, 255), 1, cv2.LINE_AA)
    bar = len(title) * 20 + 10
    overlay = out.copy()
    cv2.rectangle(overlay, (0, 0), (width, bar), (0, 0, 0), -1)
    cv2.addWeighted(overlay, .55, out, .45, 0, out)
    y0 = 20
    for line in title:
        cv2.putText(out, line, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1, cv2.LINE_AA)
        y0 += 20
    return out


def save(im, name):
    import cv2
    if not cv2.imwrite(name, im, [cv2.IMWRITE_PNG_COMPRESSION, 3]):
        raise SystemExit(f'写入失败：{name}')
    return name


def first_device():
    """脚本便利：未指定 --serial 时取 adb devices 里第一台在线设备（应用内 Device() 仍是“先连接”语义）。"""
    import fgoProfile
    try:
        output = fgoProfile.run_adb('devices')
    except fgoProfile.ProfileError:
        return None
    for line in output.splitlines():
        parts = line.split('\t')
        if len(parts) == 2 and parts[1].strip() == 'device':
            return parts[0].strip()
    return None


def fail(message, hints=()):
    print(f'\n[e] {message}', file=sys.stderr)
    for hint in hints:
        print(f'    - {hint}', file=sys.stderr)
    return 2


def main():
    parser = argparse.ArgumentParser(description='MuMu / 真机 截图标注与坐标验证')
    parser.add_argument('--profile', choices=('mumu', 'realphone'), default='mumu')
    parser.add_argument('--serial', help='adb 端点，如 127.0.0.1:16384')
    parser.add_argument('--port', type=int, help='MuMu adb 端口（等价于 --serial 127.0.0.1:<port>）')
    parser.add_argument('--all', action='store_true', help='标注全部 KEYMAP 键位')
    parser.add_argument('--points', help='自定义标注点，格式 label:x,y;label:x,y')
    parser.add_argument('--tap', action='store_true', help='真的点击每个标注点（会操作游戏画面）')
    parser.add_argument('--force-rotate', type=int, choices=(0, 90, 180, 270), help='钉死旋转角度')
    parser.add_argument('--out', default=ROOT, help='输出目录（默认脚本所在目录）')
    parser.add_argument('--selfcheck', action='store_true', help='只做坐标数学检查，不连设备')
    args = parser.parse_args()

    if args.selfcheck:
        return selfcheck()

    import fgoProfile
    import fgoAndroid
    import fgoMumu

    endpoint = args.serial or (f'127.0.0.1:{args.port}' if args.port else None)
    try:
        profile = fgoProfile.use(args.profile)
        if args.force_rotate is not None:
            from dataclasses import replace
            fgoProfile.PROFILES[profile.name] = replace(profile, forced_rotate=args.force_rotate)
            profile = fgoProfile.use(profile.name)
        if args.profile == 'mumu':
            fgoProfile.probe(endpoint)
        elif endpoint:
            fgoProfile.set_serial(endpoint)
        fgoMumu.install()
    except fgoProfile.ProfileError as e:
        return fail(str(e))

    print(f'[i] profile = {fgoProfile.current().name} ({fgoProfile.current().device_type})')
    serial = fgoProfile.default_serial()
    if not serial:
        serial = first_device()
        if serial:
            print(f'[i] 未指定 --serial，使用 adb devices 的第一台在线设备：{serial}')
    try:
        device = fgoAndroid.Android(serial)
    except Exception as e:
        return fail(f'设备初始化异常：{e}', _connection_hints(e))
    if not device.name:
        return fail(f'未连接到设备（profile={args.profile}）', _connection_hints(device.last_error))

    try:
        canvas = device.screenshot()
        raw = device.snapshot()
    except Exception as e:
        return fail(f'截图失败：{e}', _capture_hints(e))

    transform = device.transform
    render = list(device.render)
    try:
        capture = device.cap_method
    except Exception:
        capture = f'{fgoProfile.current().cap_method}(unknown)'
    try:
        adb_path = fgoProfile.find_adb()
    except fgoProfile.ProfileError:
        adb_path = 'unknown'
    lines = fgoProfile.report_lines(transform, serial=device.name, cap=capture,
                                    raw=raw.shape[:2], render=render, adb=adb_path)
    for line in lines:
        print(f'[i] {line}')
    canvas_size = f'{canvas.shape[1]}x{canvas.shape[0]}'
    print(f'[i] screenshot : {canvas_size} (canvas 1280x720 归一化后)')

    points = collect_points(args)
    ts = time.strftime('%Y%m%d_%H%M%S')
    prefix = os.path.join(args.out, f'{args.profile}_click_verify_{ts}')
    title = [f'{args.profile} / {fgoProfile.current().device_type} / {device.name}',
             f'raw {raw.shape[1]}x{raw.shape[0]} {"landscape" if raw.shape[1] > raw.shape[0] else "portrait"}'
             f'  render {render[2]}x{render[3]}  rotate {transform.rotate}deg',
             f'canvas {canvas_size}  scale {transform.scale:.4f}  border {transform.border}']
    canvas_items = [(label, x, y) for label, x, y in points]
    raw_items = [(label, *transform.to_raw(x, y)) for label, x, y in points]
    files = [save(draw(canvas, canvas_items, title), prefix + '.png'),
             save(draw(raw, raw_items, title), prefix + '_raw.png')]
    print(f'[i] canvas 标注 -> {files[0]}')
    print(f'[i] raw    标注 -> {files[1]}')
    print(f'[i] 设备坐标示例: menu 画布(1185,644) -> device '
          f'{tuple(round(v) for v in transform.to_device(1185, 644))} / raw '
          f'{tuple(round(v) for v in transform.to_raw(1185, 644))}')

    if args.tap:
        print('[!] --tap：正在按标注点依次点击，请确认游戏处于可点击状态')
        for label, x, y in points:
            device.touch((x, y))
            print(f'[i] tap {label} canvas=({x},{y})')
            time.sleep(.6)
        try:
            files.append(save(draw(device.screenshot(), [], title), prefix + '_after.png'))
            print(f'[i] 点击后画面 -> {files[-1]}')
        except Exception as e:
            print(f'[w] 点击后截图失败：{e}')
    return 0


def _connection_hints(error):
    text = (str(error) if error else '').lower()
    hints = ['确认 MuMu 已启动：python probe_mumu_adb.py',
             '端口不对时：--port <端口> 或 set FGO_MUMU_ADB=127.0.0.1:<端口>',
             'adb 冲突时：adb kill-server 后重试']
    if 'unauthorized' in text or 'offline' in text:
        hints.insert(0, '设备处于 offline/unauthorized：在 MuMu 里重新勾选 ADB 调试')
    if 'not found' in text or 'no module' in text:
        hints.insert(0, '请用便携解释器运行：C:\\Users\\Aaron\\FGO-py.portable\\Python311\\python.exe')
    return hints


def _capture_hints(error):
    text = (str(error) if error else '').lower()
    if 'yosemite' in text or 'javacap' in text:
        return ['截图后端 JAVACAP 失败（需向模拟器安装 Yosemite.apk）',
                'airtest 会依次降级 MINICAP -> ADBCAP，可重试；仍失败请检查 MuMu 是否允许安装应用']
    if 'minicap' in text:
        return ['截图后端 MINICAP 失败（需要匹配 CPU 架构的 minicap）',
                'MuMu 为 x86_64，airtest 自带该二进制；若被安全软件拦截请放行']
    return ['检查 adb 连接：python probe_mumu_adb.py',
            '确认 MuMu 已进入桌面、游戏画面未被最小化']


if __name__ == '__main__':
    sys.exit(main())
