import os
import sys
import time
from typing import List

import pyqtgraph as pg
import richuru
from loguru import logger
from PyQt5 import QtWidgets

from settings_model import setting

DEBUG = os.environ.get("MDP_ENABLE_LOG") is not None or "--debug" in sys.argv

ARG_PATH = os.path.dirname(sys.argv[0])
ABS_PATH = os.path.dirname(__file__)
ICON_PATH = os.path.join(ABS_PATH, "icon.ico")
FONT_PATH = os.path.join(ABS_PATH, "SarasaFixedSC-SemiBold.ttf")

if DEBUG:
    richuru.install(level="DEBUG")
    logger.add(
        os.path.join(ARG_PATH, "mdp.log"), level="TRACE", backtrace=True, diagnose=True
    )
else:
    richuru.install(level="INFO")
logger.info("---- NEW SESSION ----")
logger.info(f"ARG_PATH: {ARG_PATH}")
logger.info(f"ABS_PATH: {ABS_PATH}")

OPENGL_AVALIABLE = False
if sys.platform == "win32":  # OpenGL is not available on Linux
    try:
        import OpenGL  # noqa: F401

        OPENGL_AVALIABLE = True
        logger.success("OpenGL successfully enabled")
    except Exception as e:
        logger.warning(f"Enabling OpenGL failed with {e}.")
else:
    logger.info("OpenGL disabled on Linux")

NUMBA_ENABLED = False
try:
    import llvmlite  # noqa: F401
    import numba as nb  # noqa: F401

    pg.setConfigOption("useNumba", True)
    logger.success("Numba successfully enabled")
    NUMBA_ENABLED = True
except Exception as e:
    logger.warning(f"Enabling Numba failed with {e}.")


def update_pyqtgraph_setting():
    pg.setConfigOption("antialias", setting.ui.antialias)
    if OPENGL_AVALIABLE:
        pg.setConfigOption("enableExperimental", setting.ui.opengl)
        pg.setConfigOption("useOpenGL", setting.ui.opengl)
    logger.debug(f"Antialias: {setting.ui.antialias}, OpenGL: {setting.ui.opengl}")


class FPSCounter(object):
    def __init__(self, max_sample=40) -> None:
        self.t = time.perf_counter()
        self.max_sample = max_sample
        self.t_list: List[float] = []
        self._fps = 0

    def clear(self) -> None:
        self.t = time.perf_counter()
        self.t_list = []
        self._fps = 0

    def tick(self) -> None:
        t = time.perf_counter()
        self.t_list.append(t - self.t)
        self.t = t
        if len(self.t_list) > self.max_sample:
            self.t_list.pop(0)

    @property
    def fps(self) -> float:
        length = len(self.t_list)
        sum_t = sum(self.t_list)
        if length == 0 or sum_t == 0:
            self._fps = 0
        else:
            fps = length / sum_t
            if abs(fps - self._fps) > 2 or self._fps == 0:
                self._fps = fps
            else:
                self._fps += (fps - self._fps) * 2 / self._fps
        return self._fps


def center_window(instance: QtWidgets.QWidget, width=None, height=None) -> None:
    if instance.isMaximized():  # restore window size
        instance.showNormal()
    if instance.isVisible():  # bring window to front
        instance.activateWindow()
    scr_geo = QtWidgets.QApplication.primaryScreen().geometry()
    if not width or not height:  # center window
        geo = instance.geometry()
        center_x = (scr_geo.width() - geo.width()) // 2
        center_y = (scr_geo.height() - geo.height()) // 2
        instance.move(center_x, center_y)
    else:  # set window size and center window
        center_x = (scr_geo.width() - width) // 2
        center_y = (scr_geo.height() - height) // 2
        instance.setGeometry(center_x, center_y, width, height)


def float_str(value, limit=1e5):
    if value > limit:
        return f"{value:.1e}"
    else:
        return f"{value:.3f}"


def set_color(widget: QtWidgets.QWidget, rgb):
    if not rgb or rgb == "default":
        widget.setStyleSheet("")
        return
    color = f"rgb({rgb[0]},{rgb[1]},{rgb[2]})" if isinstance(rgb, tuple) else rgb
    widget.setStyleSheet(f"color: {color}")
