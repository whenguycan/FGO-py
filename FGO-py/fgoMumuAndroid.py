#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fgoMumuAndroid.py - profile 化的 airtest Android 实现（真机 / MuMu 共用）。

为什么单独成文件：便携版启动时会执行 deploy/updater.py，里面的
    git reset --hard origin/master
会把 fgoAndroid.py 等受版本控制的文件还原。本文件与 fgoProfile.py / fgoMumu.py
都是未被 git 跟踪的新文件，reset 不会动它们；fgoMumu.py 会在导入原 fgo.py 之前
把 fgoAndroid.Android 换成这里的实现。

与旧实现的关系：
    rotate=0（真机 profile）时 scale/border/touch/swipe/screenshot 的计算与
    fgoAndroid.py 原实现逐位等价；只有 mumu profile 才会做旋转/宽高交换归一化。
"""
import os
import re
import threading
import time

import cv2
import numpy

from airtest.core.android.adb import ADB
from airtest.core.android.android import Android as Airtest

import fgoProfile
from fgoConst import PACKAGE_TO_REGION
from fgoLogging import getLogger

logger = getLogger('Android')


class Android(Airtest):
    def __init__(self, serial=None, profile=None, **kwargs):
        self.mutex = threading.Lock()
        self.profile = profile or fgoProfile.current()
        self.deviceType = self.profile.device_type
        self.last_error = None
        self.render = [0, 0, 0, 0]
        self.transform = fgoProfile.Transform()
        self._raw_shape = None
        self._rotate_check = self.profile.auto_rotate
        serial = serial or fgoProfile.default_serial()
        if serial is None or serial == 'None':
            self.name = None
            return
        try:
            super().__init__(serial, **{'cap_method': self.profile.cap_method,
                                        'host': os.environ.get('ADB_SERVER_SOCKET', 'tcp:localhost:5037').split(":")[1:]} | kwargs)
            if package := self.detectPackage():
                self.package = package
            self.adjustOffset()
            self.rotation_watcher.reg_callback(self.onRotation)
        except Exception as e:
            logger.exception(e)
            self.last_error = e
            self.name = None
        else:
            self.name = self.serialno
            self.logGeometry()

    @property
    def available(self):
        if not self.name:
            return False
        try:
            proc = self.touch_proxy.server_proc
        except (NotImplementedError, AttributeError):
            return True  # ADBTOUCH has no server_proc but still works
        if proc is None or proc.poll() is None:
            return True
        self.name = None
        return False

    @staticmethod
    def enumDevices():
        return [i for i, _ in ADB().devices('device')]

    def detectPackage(self):
        'Find the running game package, tolerating any render size and the game not being in foreground.'
        minWidth, minHeight = self.profile.min_render
        try:
            activities = re.findall(r'ACTIVITY ([A-Za-z0-9_.]+)/', self.adb.shell('dumpsys activity top'))[::-1]
        except Exception as e:
            logger.exception(e)
            return None
        for package in activities:
            try:
                render = self.get_render_resolution(True, package)
            except Exception:
                continue
            if render[2] - render[0] >= minWidth and render[3] - render[1] >= minHeight:
                return package
        return self.guessPackage()

    def guessPackage(self):
        'Fall back to any known FGO package installed on the device.'
        try:
            installed = re.findall(r'package:([\w.]+)', self.adb.shell('pm list packages'))
        except Exception as e:
            logger.exception(e)
            return None
        return next((package for package in PACKAGE_TO_REGION if package in installed), None)

    def adjustOffset(self):
        self.render = [round(i) for i in self.get_render_resolution(True, getattr(self, 'package', None))]
        self.transform = self.profile.transform(self.render, self._raw_shape)
        self.scale, self.border = self.transform.scale, self.transform.border
        self.key = {c: [round(i) for i in self.transform.to_device(*p)] for c, p in self.profile.keymap.items()}

    def onRotation(self, _):
        self._rotate_check = self.profile.auto_rotate
        try:
            self.adjustOffset()
        except Exception as e:
            logger.exception(e)

    def _syncGeometry(self, im):
        'Rotated profiles re-derive geometry when the frame orientation changes; real phone keeps the old path.'
        if not self.profile.auto_rotate:
            return
        shape = im.shape[:2]
        if not (self._rotate_check or shape != self._raw_shape):
            return
        previous = self.transform.rotate
        self._rotate_check = False
        self._raw_shape = shape
        try:
            self.adjustOffset()
        except Exception as e:
            logger.exception(e)
        else:
            if self.transform.rotate != previous:
                logger.warning(f'Rotate {self.transform.rotate}deg to normalize {shape[1]}x{shape[0]} frame for {self.profile.name}')
                self.logGeometry()
            frameWidth, frameHeight = (shape[1], shape[0]) if self.transform.rotate in (0, 180) else (shape[0], shape[1])
            if abs(frameWidth - self.transform.frame[0]) > 4 or abs(frameHeight - self.transform.frame[1]) > 4:
                logger.warning(f'Frame {frameWidth}x{frameHeight} does not match render frame '
                               f'{self.transform.frame[0]}x{self.transform.frame[1]}, coordinates may be off '
                               f'(try verify_mumu_screenshot.py --force-rotate)')

    def logGeometry(self):
        message = '\n'.join(fgoProfile.report_lines(self.transform, serial=self.name, cap=self.profile.cap_method,
                                                    raw=self._raw_shape, render=self.render))
        if self.profile.auto_rotate and self._raw_shape is None:
            message += '\nnote         : rotate is re-derived from the first captured frame'
        if self.profile.name == 'realphone':
            logger.debug(message)
        else:
            logger.warning(message)

    def touch(self, pos):
        with self.mutex:
            super().touch([round(i) for i in self.transform.to_device(*pos)])

    def swipe(self, begin, end):
        p1, p2 = [numpy.array(self._touch_point_by_orientation(list(self.transform.to_device(*i)))) for i in (begin, end)]
        vd = p2 - p1
        lvd = numpy.linalg.norm(vd)
        vd /= .2 * self.transform.scale * lvd
        vx = numpy.array([0., 0.])

        def send(method, pos):
            self.touch_proxy.handle(' '.join((method, '0', *[str(i) for i in self.touch_proxy.transform_xy(*pos)], '50\nc\n')))

        with self.mutex:
            send('d', p1)
            time.sleep(.01)
            for _ in range(2):
                send('m', p1 + vx)
                vx += vd
                time.sleep(.02)
            vd *= 5
            while numpy.linalg.norm(vx) < lvd:
                send('m', p1 + vx)
                vx += vd
                time.sleep(.008)
            send('m', p2)
            time.sleep(.35)
            self.touch_proxy.handle('u 0\nc\n')
            time.sleep(.02)

    def press(self, key):
        with self.mutex:
            super().touch(self.key[key])

    def pinch(self):
        with self.mutex:
            super().pinch(percent=.2)

    def snapshot(self, *args, **kwargs):
        return super().snapshot(*args, **kwargs)

    def screenshot(self):
        im = None
        if self.profile.name == 'mumu':
            try:
                raw = self.adb.cmd('exec-out screencap -p', ensure_unicode=False)
                im = cv2.imdecode(numpy.frombuffer(raw, numpy.uint8), cv2.IMREAD_COLOR)
            except Exception as e:
                logger.exception(e)
                im = None
        if im is None:
            im = super().snapshot()
        self._syncGeometry(im)
        return self.transform.to_canvas(im)

    def invoke169(self):
        x, y = (lambda r: (int(r.group(1)), int(r.group(2))))(re.search(r'(\d+)x(\d+)', self.adb.raw_shell('wm size')))
        if x * 16 < y * 9:
            self.adb.raw_shell('wm size %dx%d' % (x, x * 16 // 9))
        elif y * 16 < x * 9:
            self.adb.raw_shell('wm size %dx%d' % (y * 16 // 9, y))
        self.adjustOffset()

    def revoke169(self):
        self.adb.raw_shell('wm size reset')
