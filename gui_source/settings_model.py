import json
import os
import sys

DEFAULT_PRESETS = {
    "1": (3.3, 2),
    "2": (3.3, 5),
    "3": (5, 2),
    "4": (5, 5),
    "5": (9, 5),
    "6": (12, 5),
    "7": (20, 5),
    "8": (24, 10),
    "9": (30, 10),
}

DEFAULT_L1060_PRESETS = {
    "1": ("CC", 1.0),
    "2": ("CC", 2.0),
    "3": ("CV", 3.3),
    "4": ("CV", 5.0),
    "5": ("CV", 12.0),
    "6": ("CR", 50.0),
    "7": ("CR", 100.0),
    "8": ("CP", 10.0),
    "9": ("CP", 20.0),
}

DEFAULT_COLOR_PALETTE = {
    "dark": {
        "off": "khaki",
        "on": "lightgreen",
        "cv": "skyblue",
        "cc": "tomato",
        "lcd_voltage": "default",
        "lcd_current": "default",
        "lcd_power": "default",
        "lcd_energy": "default",
        "lcd_temperature": "default",
        "lcd_resistance": "default",
        "general_green": "mediumaquamarine",
        "general_red": "orangered",
        "general_yellow": "yellow",
        "general_blue": "lightblue",
        "line1": "salmon",
        "line2": "turquoise",
    },
    "light": {
        "lcd_voltage": "default",
        "lcd_current": "default",
        "lcd_power": "default",
        "lcd_energy": "default",
        "lcd_temperature": "default",
        "lcd_resistance": "default",
        "off": "darkgoldenrod",
        "on": "darkgreen",
        "cv": "darkblue",
        "cc": "darkred",
        "general_green": "forestgreen",
        "general_red": "firebrick",
        "general_yellow": "goldenrod",
        "general_blue": "darkblue",
        "line1": "orangered",
        "line2": "darkcyan",
    },
    "modify_this_to_add_your_custom_theme": {
        "based_on_dark_or_light": "dark",
        "any_other_item": "any_other_color",
    },
}


def _to_plain(obj):
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _to_plain(v) for k, v in obj.__dict__.items()}
    return obj


class CalibrationSettings:
    def __init__(self) -> None:
        self.use = False
        self.v_k = 1.0
        self.v_b = 0.0
        self.i_k = 1.0
        self.i_b = 0.0
        self.vset_k = 1.0
        self.vset_b = 0.0
        self.iset_k = 1.0
        self.iset_b = 0.0


class DeviceSettings:
    def __init__(self, type="P906", id="p906-1", name="P906") -> None:
        self.type = type
        self.id = id
        self.name = name
        self.idcode = ""
        self.m01ch = "CH-0"
        self.color = "66CCFF"
        self.blink = False
        self.output_warning = False
        self.lock_when_output = False
        self.ignore_hw_lock = False
        self.presets = dict(DEFAULT_PRESETS)
        self.v_threshold = 0.002
        self.i_threshold = 0.002
        self.avgmode = 1
        self.cali = CalibrationSettings()
        # L1060-only, display-only fallback painted before the first real
        # Type10 response lands after a fresh app launch -- never auto-pushed
        # to the device on link() (would silently override whatever's
        # physically dialed into the unit).
        self.l1060_mode = "CC"
        self.l1060_targets = {"CC": 1.0, "CV": 5.0, "CR": 100.0, "CP": 10.0}
        self.l1060_presets = dict(DEFAULT_L1060_PRESETS)


class AdapterSettings:
    def __init__(self) -> None:
        self.comport = ""
        self.baudrate = 921600
        self.address = "0E:4C:B9:EF:E0"
        self.freq = 2473
        self.txpower = "4dBm"
        # "serial" (default, zero-friction) or "tcp" for the ESP32 WiFi host
        # link -- see plans/nrf_adapter_esp32_wifi_link_plan.md, "Decisions
        # made" #3 (manual IP entry, no mDNS).
        self.transport = "serial"
        self.host = ""
        self.tcp_port = 9000


class UiSettings:
    def __init__(self) -> None:
        self.theme = "dark"
        self.color_palette = json.loads(json.dumps(DEFAULT_COLOR_PALETTE))
        self.data_pts = 100000
        self.display_pts = 600
        self.graph_max_fps = 50
        self.state_fps = 15
        self.interp = 1
        self.opengl = False
        self.antialias = True
        self.bitadjust = True
        # "stacked" (every device panel in the left column) or "single"
        # (one panel at a time, picked from a device button row).
        self.device_layout = "stacked"


class Setting:
    def __init__(self) -> None:
        self.adapter = AdapterSettings()
        self.devices = [DeviceSettings()]
        self.ui = UiSettings()

    def save(self, filename):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(_to_plain(self), f, indent=4, ensure_ascii=False)

    def load(self, filename):
        if not os.path.exists(filename):
            self.save(filename)
            return
        raw = json.load(open(filename, "r", encoding="utf-8"))

        self.adapter.__dict__.update(raw.get("adapter", {}))

        devices_raw = raw.get("devices") or [{}]
        devices = []
        for draw in devices_raw:
            draw = dict(draw)
            cali_raw = draw.pop("cali", {})
            dev = DeviceSettings()
            dev.__dict__.update(draw)
            dev.cali.__dict__.update(cali_raw)
            devices.append(dev)
        self.devices = devices

        def_palette = self.ui.color_palette
        ui_raw = dict(raw.get("ui", {}))
        palette_raw = ui_raw.pop("color_palette", None)
        self.ui.__dict__.update(ui_raw)
        if palette_raw is not None:
            self.ui.color_palette = palette_raw
        for k, v in def_palette.items():
            if k not in self.ui.color_palette:
                self.ui.color_palette[k] = v
            elif len(self.ui.color_palette[k]) < len(v):
                v.update(self.ui.color_palette[k])
                self.ui.color_palette[k] = v

    def get_color(self, key, override_theme=None):
        t = override_theme or self.ui.theme
        if t in ("dark", "light"):
            return self.ui.color_palette[t].get(key)
        else:
            if "based_on_dark_or_light" not in self.ui.color_palette[t]:
                self.ui.color_palette[t]["based_on_dark_or_light"] = "dark"
            return self.ui.color_palette[t].get(
                key,
                self.ui.color_palette[
                    self.ui.color_palette[t]["based_on_dark_or_light"]
                ].get(key),
            )

    def __repr__(self) -> str:
        return f"Setting({_to_plain(self)})"


SETTING_FILE = os.path.join(os.path.dirname(sys.argv[0]), "settings.json")

setting = Setting()
setting.load(SETTING_FILE)
setting.save(SETTING_FILE)
