# MuMu 模拟器支持（`-m` 切换 profile）

FGO-py 默认按真机工作。用 **`fgoMumu.py`** 启动并加上 `-m` 后，连接方式、截图后端与坐标基准会整体切到
MuMu 那一套 profile；不带 `-m` 时与原来的 `python fgo.py ...` 完全一致。

## 0. 为什么是 `fgoMumu.py` 而不是直接改 `fgo.py`

便携版的 `deploy/updater.py` 每次启动都会执行：

```python
git reset --hard origin/master      # 见 deploy/updater.py 第 8 行
```

它会把 `fgo.py`、`fgoAndroid.py` 等**受版本控制**的文件还原。直接改这些文件，下次启动就白改
（典型现象：给 `fgo.py` 加 `-m` 后窗口一闪就退，因为 argparse 不认识 `-m`，直接 `exit 2`）。

因此 MuMu 支持全部放在 **未被 git 跟踪的新文件**里，`reset` 不会动它们：

| 文件 | 作用 |
| --- | --- |
| `fgoMumu.py` | 入口：解析 `-m/--mumu/--mumu-adb`，探测 MuMu，安装补丁，再转交原 `fgo.py` |
| `fgoProfile.py` | profile、坐标 `Transform`、adb 探测与报错（单一入口） |
| `fgoMumuAndroid.py` | profile 化的 `Android`（旋转归一化、坐标换算、可用性判断） |
| `verify_mumu_screenshot.py` / `probe_mumu_adb.py` | 验证与排错脚本 |

## 1. 准备 MuMu

1. 启动 MuMu 并进入桌面；
2. MuMu 设置 → 其他 → **ADB 调试** 打开，记下端口（MuMu 12 默认 `16384`，旧版/部分实例为 `7555`）；
3. 在 MuMu 里安装并登录 FGO，切到游戏主界面；
4. 先探测一次：

```powershell
C:\Users\Aaron\FGO-py.portable\Python311\python.exe FGO-py\probe_mumu_adb.py
```

应看到 `[OK] MuMu adb 可用`、物理分辨率、`SurfaceOrientation`（横/竖屏）、FGO 包名与前台状态，
以及「画布(640,360) -> device」的换算结果。

端口不是默认值时（显式端点不会回退到默认端口，避免连错实例）：

```powershell
python fgoMumu.py -m --mumu-adb 127.0.0.1:16416 gui
set FGO_MUMU_ADB=127.0.0.1:16416 && python fgoMumu.py -m gui
```

不指定端点时才按 `16384 → 7555 → 16416 → 16448 → MuMuManager.exe adb -v N` 依次探测。

## 2. 启动

```powershell
cd C:\Users\Aaron\FGO-py.portable\FGO-py\FGO-py
C:\Users\Aaron\FGO-py.portable\Python311\python.exe fgoMumu.py -m gui    # 图形界面
C:\Users\Aaron\FGO-py.portable\Python311\python.exe fgoMumu.py -m cli    # 命令行（启动即已连上 MuMu）
C:\Users\Aaron\FGO-py.portable\Python311\python.exe fgoMumu.py gui       # 不带 -m：等同 python fgo.py gui
```

便携启动器 `FGO-py.portable\FGO-py.bat` 把最后一行从

```bat
python fgo.py gui
```

改成（其余行不动，`updater` 可以继续跑）：

```bat
python fgoMumu.py -m gui
```

这样以后每次自动更新后 `-m` 依然有效。

## 3. 如何确认坐标已切到 MuMu

启动日志（控制台与 `fgoLog/`）里会有设备几何报告：

```
profile      : mumu (emulator)      serial : 127.0.0.1:16384
raw          : 540x960 (portrait)   render : 960x540 (landscape) offset=(0,0)
rotate       : 90                   scale  : 1.3333  border=(0, 0)  delta=(0, 0)
canvas       : 1280x720             capture: JAVACAP
```

- `profile` 为 `mumu`、`serial` 为探测到的端点 → 坐标已走 MuMu 配置；
- `raw` 是**实际截图**尺寸与方向（竖屏帧会被 `rotate 90` 归一化到横屏画布）；
- `render` 是 airtest 的渲染区，`canvas` 恒为 `1280x720`（模板与键位都以它为基准）。

> 实测样例：MuMu 12 的 `JAVACAP` 返回**竖屏 540x960** 帧，而显示是横屏 960x540。
> 所以 `raw` 显示 `540x960 (portrait)`、`rotate 90` 是正常的归一化，不是错误。
> 初始化那行日志里 `rotate` 还是 `0` 且带 `note: rotate is re-derived from the first captured frame`，
> 首帧到达后会再打印 `Rotate 90deg to normalize ...`，以最后一条为准。

肉眼确认：

```powershell
python verify_mumu_screenshot.py                        # mumu_click_verify_<时间戳>.png
python verify_mumu_screenshot.py --profile realphone    # 真机/旧行为对照
```

## 4. 脚本复用

| 脚本 | 用途 | 典型参数 |
| --- | --- | --- |
| `probe_mumu_adb.py` | 只读探测端口/分辨率/方向/前台包，不依赖 airtest | `--port 16384`、`--json` |
| `verify_mumu_screenshot.py` | 截图 + 画布坐标标注 + 几何报告 | `--all`、`--tap`、`--force-rotate 90`、`--serial` |
| `verify_mumu_screenshot.py --selfcheck` | 只做坐标数学检查（真机公式等价、旋转/宽高交换、keymap 独立性），不连设备 | 改代码后自检 |

`--tap` 会真的点击游戏画面，默认只画不点。输出写到脚本所在目录。

## 5. 坐标/旋转对不上时

1. 先跑 `--selfcheck` 排除归一化数学问题；
2. `_raw.png` 里 UI 躺倒 → 用 `--force-rotate 90`（或 `270`）定角，再写进 `fgoProfile.MUMU.forced_rotate`；
3. 整体固定像素偏移 → `fgoProfile.MUMU.coord_delta=(dx, dy)`；
4. 个别键位不准 → 改 `fgoProfile.MUMU_KEYMAP`（与真机 `fgoConst.KEYMAP` 是两份独立对象）。

## 6. 排错

| 现象 | 处理 |
| --- | --- |
| **加 `-m` 后窗口一闪就退** | 多半是直接改了 `fgo.py` 又被 updater `git reset --hard` 还原。改用 `python fgoMumu.py -m gui` |
| `MuMu adb 探测失败` | 确认 MuMu 已进桌面、ADB 调试已开；`MuMuManager.exe adb -v 0` 看真实端口；必要时 `adb kill-server` |
| `设备状态 offline/unauthorized` | 在 MuMu 设置里关掉再打开 ADB 调试 |
| `截图后端 JAVACAP 失败` | airtest 会按 `JAVACAP → MINICAP → ADBCAP` 降级，重试即可；ADBCAP 更慢但兼容性最好 |
| 用了全局 Python 报 `No module named 'airtest'` | 改用 `C:\Users\Aaron\FGO-py.portable\Python311\python.exe` |
