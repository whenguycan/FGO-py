#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fgoMumu.py - MuMu 入口：带 -m 时切换 profile，然后原样转交给 fgo.py。

为什么需要这个文件：
    便携版的 deploy/updater.py 在每次启动时都会执行
        git reset --hard origin/master
    所以直接修改 fgo.py / fgoAndroid.py 等"受版本控制"的文件的改动，会在下一次启动时被
    还原（现象：加了 -m 后窗口一闪就退，因为 fgo.py 不认识 -m，argparse 直接 exit 2）。
    本文件、fgoProfile.py、fgoMumuAndroid.py 都是**未被 git 跟踪**的新文件，reset 不会动它们。

用法（在 FGO-py 源码目录，使用便携 Python）:
    python fgoMumu.py -m gui                       MuMu 图形界面
    python fgoMumu.py -m cli                       MuMu 命令行
    python fgoMumu.py -m --mumu-adb 127.0.0.1:16416 gui   指定 MuMu adb 端口
    python fgoMumu.py gui                          不带 -m：与 python fgo.py gui 完全相同（真机）

依赖：airtest / opencv / numpy / PySide6 … 均由便携 Python311 提供；
      adb 依次查找 PATH → MuMu 自带 → ANDROID_HOME → airtest 内置。

失败时不会"闪退"：会打印原因并等待回车，便于看到 -m 探测失败的确切原因。
"""
import argparse
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)


def install():
    """把 fgoAndroid.Android 换成 profile 化实现（幂等；必须在 fgoDevice 导入之前调用）。"""
    import fgoAndroid
    import fgoMumuAndroid
    if 'fgoDevice' in sys.modules:
        raise RuntimeError('fgoDevice 已被导入，MuMu 补丁必须在此之前安装')
    fgoAndroid.Android = fgoMumuAndroid.Android
    return fgoMumuAndroid.Android


def parse(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-m', '--mumu', action='store_true')
    parser.add_argument('--mumu-adb', default=None)
    known, rest = parser.parse_known_args(argv)
    return known.mumu or bool(known.mumu_adb), known.mumu_adb, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    mumu, endpoint, rest = parse(argv)
    import fgoProfile
    fgoProfile.use('mumu' if mumu else 'realphone')
    if mumu:
        try:
            fgoProfile.probe(endpoint)
        except fgoProfile.ProfileError as e:
            print(f'[FGO-py] {e}', file=sys.stderr)
            return 2
        install()
        print(f'[FGO-py] MuMu profile 已启用，转交 fgo.py {rest or ""}'.rstrip())
    import runpy
    sys.argv = ['fgo.py'] + rest
    runpy.run_path(os.path.join(ROOT, 'fgo.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    try:
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    if code:
        try:
            input(f'\n[FGO-py] 启动失败（退出码 {code}），按回车关闭窗口…')
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)
