from __future__ import annotations

__version__=1.2
__date__='2026.02.09'


'''
Operates through Moonraker/Klipper protokol using HTTP
'''

from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import time
import requests


class PrinterError(RuntimeError):
    pass


@dataclass
class PrinterConfig:

    base_url: str                  # например: "http://192.168.1.50:7125"
    api_key: Optional[str] = None
    timeout: float = 60.0

    # Габариты насадки как bounding-box офсеты относительно точки toolhead (обычно сопло), мм.

    '''
    Если насадка выступает вправо по X на 30 мм и влево на 5 мм:
    attach_min_x = -5, attach_max_x = +30
    '''

    attach_min_x: float = 0.0
    attach_max_x: float = 0.0
    '''
    Если вперёд по Y выступ 20 мм, назад 0:
    attach_min_y = 0, attach_max_y = +20
    '''
    attach_min_y: float = 0.0
    attach_max_y: float = 0.0
    '''
    Если колесо ниже сопла на 12 мм (выступ вниз, т.е. к столу), и вверх насадка не выступает:
    attach_min_z = -12, attach_max_z = 0
    '''
    attach_min_z: float = 0.0
    attach_max_z: float = 0.0

    # Скорость подъёма/опускания Z (мм/с) для аккуратных движений по Z
    z_speed_mm_s: float = 8.0


class Printer:
    def __init__(self, cfg: PrinterConfig, *, auto_init: bool = True):
        self.cfg = cfg
        self._limits: Optional[Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]] = None
        if auto_init:
            self.initialize()

    # ---------------- HTTP ----------------
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            h["X-Api-Key"] = self.cfg.api_key
        return h

    def _url(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    def _get(self, path: str, params: Optional[dict] = None) -> Dict[str, Any]:
        r = requests.get(self._url(path), params=params, headers=self._headers(), timeout=self.cfg.timeout)
        if not r.ok:
            raise PrinterError(f"GET {path} failed: {r.status_code} {r.text}")
        return r.json()

    def _post(self, path: str, payload: dict) -> Dict[str, Any]:
        r = requests.post(self._url(path), json=payload, headers=self._headers(), timeout=self.cfg.timeout)
        if not r.ok:
            raise PrinterError(f"POST {path} failed: {r.status_code} {r.text}")
        return r.json()

    # ---------------- Status ----------------
    def printer_info(self) -> Dict[str, Any]:
        return self._get("/printer/info")["result"]

    def query_status(self) -> Dict[str, Any]:
        return self._get(
            "/printer/objects/query",
            params={"toolhead": "", "gcode_move": "", "print_stats": "", "webhooks": ""},
        )["result"]["status"]

    def _ensure_ready(self) -> None:
        info = self.printer_info()
        if info.get("state") != "ready":
            raise PrinterError(f"Printer not ready: {info.get('state')} {info.get('state_message')}")

    def _ensure_homed(self, axes: str = "xyz") -> None:
        st = self.query_status()
        homed = (st.get("toolhead", {}) or {}).get("homed_axes", "") or ""
        for a in axes.lower():
            if a not in homed.lower():
                raise PrinterError(f"Axis '{a.upper()}' not homed. homed_axes='{homed}'. Run home() first.")

    def wait_moves_m400(self) -> None:
        self.send_gcode("M400")

    def wait_moves(self, poll_interval: float = 0.2, timeout: float = 120.0) -> None:
        t0 = time.time()
        while True:
            st = self.query_status()
            if not bool(st.get("toolhead", {}).get("moving", False)):
                return
            if time.time() - t0 > timeout:
                raise PrinterError("Timeout waiting for moves to finish")
            time.sleep(poll_interval)

    # ---------------- Init / limits cache ----------------
    def initialize(self) -> None:
        self._ensure_ready()
        self.refresh_limits()
        self._validate_attachment_box()

    def refresh_limits(self) -> None:
        st = self.query_status()
        mn = st["toolhead"]["axis_minimum"]  # [xmin,ymin,zmin,emin]
        mx = st["toolhead"]["axis_maximum"]  # [xmax,ymax,zmax,emax]
        self._limits = (
            (float(mn[0]), float(mx[0])),
            (float(mn[1]), float(mx[1])),
            (float(mn[2]), float(mx[2])),
        )

    def get_limits_cached(self) -> Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]:
        if self._limits is None:
            raise PrinterError("Limits are not initialized. Call initialize() or use auto_init=True.")
        return self._limits

    def _validate_attachment_box(self) -> None:
        # Допускаем отрицательные значения (это нормально), но min должен быть <= max
        for axis in ("x", "y", "z"):
            mn = float(getattr(self.cfg, f"attach_min_{axis}"))
            mx = float(getattr(self.cfg, f"attach_max_{axis}"))
            if mn > mx:
                raise PrinterError(f"attach_min_{axis} must be <= attach_max_{axis} (got {mn} > {mx})")

    # ---------------- G-code ----------------
    def send_gcode(self, script: str) -> None:
        self._post("/printer/gcode/script", {"script": script})

    # ---------------- Thermals (Moonraker/Klipper) ----------------
    @staticmethod
    def _as_float(v: Any, name: str) -> float:
        try:
            return float(v)
        except Exception as e:
            raise PrinterError(f"Cannot convert {name}='{v}' to float") from e

    def _query_objects(self, objects: Dict[str, Any]) -> Dict[str, Any]:
        """
        Low-level helper to query arbitrary Klipper objects via Moonraker.
        Example: objects={"heater_bed": None, "temperature_sensor chamber": None}
        """
        params = {k: "" for k in objects.keys()}
        return self._get("/printer/objects/query", params=params)["result"]["status"]

    def get_bed_temperature(self) -> Tuple[float, Optional[float]]:
        """
        Returns (current, target) for heater_bed.
        Klipper обычно возвращает:
          status["heater_bed"]["temperature"]
          status["heater_bed"]["target"]
        """
        st = self._query_objects({"heater_bed": None})
        hb = st.get("heater_bed")
        if not isinstance(hb, dict):
            raise PrinterError("Klipper object 'heater_bed' not found. Check printer.cfg configuration.")
        cur = self._as_float(hb.get("temperature"), "heater_bed.temperature")
        tgt = hb.get("target", None)
        tgt_f = None if tgt is None else self._as_float(tgt, "heater_bed.target")
        return cur, tgt_f

    def set_bed_temperature(self, temp_c: float, *, wait: bool = False) -> None:
        """
        Set bed temperature via standard G-code:
          wait=False -> M140 S<temp>
          wait=True  -> M190 S<temp> (wait until reached)
        """
        self._ensure_ready()
        temp_c = float(temp_c)
        if temp_c < 0 or temp_c > 150:
            raise PrinterError(f"Bed temperature out of expected range: {temp_c}C")

        cmd = "M190" if wait else "M140"
        self.send_gcode(f"{cmd} S{temp_c:.1f}")
        if wait:
            self.wait_moves_m400()

    def get_chamber_temperature(self) -> Tuple[float, Optional[float]]:
        """
        Returns (current, target) for chamber temperature.

        In Klipper there is no single universal 'chamber' object name.
        This method tries common variants in order:
          1) temperature_sensor chamber
          2) heater_generic chamber
          3) temperature_fan chamber
          4) chamber (rare/custom)

        For temperature_sensor: only 'temperature' exists (no target) -> target=None
        For heater_generic:     'temperature' + 'target'
        For temperature_fan:    typically has 'temperature' and 'target' (depends on config)

        If none found -> raises PrinterError with guidance.
        """
        candidates = [
            "temperature_sensor chamber",
            "heater_generic chamber",
            "temperature_fan chamber",
            "chamber",
        ]

        last_err = None
        for obj in candidates:
            try:
                st = self._query_objects({obj: None})
                data = st.get(obj)
                if isinstance(data, dict):
                    cur = self._as_float(data.get("temperature"), f"{obj}.temperature")
                    tgt = data.get("target", None)
                    tgt_f = None if tgt is None else self._as_float(tgt, f"{obj}.target")
                    return cur, tgt_f
            except Exception as e:
                last_err = e

        # Дополнительно: попробуем auto-discovery через /printer/objects/list (если доступно)
        try:
            objs = self._get("/printer/objects/list")["result"]["objects"]
        except Exception:
            objs = None

        hint = ""
        if isinstance(objs, list):
            # Подскажем, что искать
            chamber_like = [o for o in objs if "chamber" in o.lower()]
            hint = f" Available objects containing 'chamber': {chamber_like}" if chamber_like else ""

        raise PrinterError(
            "Chamber temperature object not found. "
            "Define it in Klipper config, e.g. [temperature_sensor chamber] or [heater_generic chamber]."
            + hint
        ) from last_err

    def set_chamber_temperature(self, temp_c: float, *, wait: bool = False) -> None:
        """
        Set chamber temperature.

        There is NO universal G-code for chamber temperature in Klipper.
        Most reliable way is to use Klipper objects:

        - If you configured: [heater_generic chamber]
            -> use SET_HEATER_TEMPERATURE HEATER=chamber TARGET=<temp>

        - If you configured chamber as temperature_fan:
            -> use SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber TARGET=<temp>

        This method tries both, by probing objects first.
        If neither exists -> raises PrinterError.
        """
        self._ensure_ready()
        temp_c = float(temp_c)
        if temp_c < 0 or temp_c > 90:
            raise PrinterError(f"Chamber temperature out of expected range: {temp_c}C")

        # Prefer heater_generic chamber if present
        try:
            st = self._query_objects({"heater_generic chamber": None})
            if isinstance(st.get("heater_generic chamber"), dict):
                self.send_gcode(f"SET_HEATER_TEMPERATURE HEATER=chamber TARGET={temp_c:.1f}")
                if wait:
                    # Простое ожидание: опрашиваем до достижения (с допуском)
                    self._wait_chamber_reach(temp_c, tol=1.0, timeout_s=self.cfg.timeout)
                return
        except Exception:
            pass

        # Try temperature_fan chamber
        try:
            st = self._query_objects({"temperature_fan chamber": None})
            if isinstance(st.get("temperature_fan chamber"), dict):
                self.send_gcode(f"SET_TEMPERATURE_FAN_TARGET TEMPERATURE_FAN=chamber TARGET={temp_c:.1f}")
                if wait:
                    self._wait_chamber_reach(temp_c, tol=1.0, timeout_s=self.cfg.timeout)
                return
        except Exception:
            pass

        raise PrinterError(
            "Cannot set chamber temperature: neither [heater_generic chamber] "
            "nor [temperature_fan chamber] found in Klipper objects."
        )

    def _wait_chamber_reach(self, target_c: float, *, tol: float = 1.0, timeout_s: float = 600.0, poll_s: float = 1.0) -> None:
        t0 = time.time()
        while True:
            cur, _tgt = self.get_chamber_temperature()
            if abs(cur - target_c) <= tol:
                return
            if time.time() - t0 > timeout_s:
                raise PrinterError(f"Timeout waiting for chamber to reach {target_c}C (current={cur}C)")
            time.sleep(poll_s)

    def home(self, axes: str = "XYZ", *, confirm: bool = True) -> None:
        """
        Выполнить homing (G28), но только после явного подтверждения в консоли,
        что с головы сняты все насадки/колесо и homing безопасен.

        confirm=True  -> спросить подтверждение
        confirm=False -> выполнить без вопроса (на ваш риск)
        """
        self._ensure_ready()

        if confirm:
            prompt = (
                f"About to run G28 {axes.upper()}.\n"
                "CONFIRM: all attachments/wheel are REMOVED from the toolhead.\n"
                "Type 'CONFIRM' to continue: "
            )
            ans = input(prompt).strip().upper()
            if ans != "CONFIRM":
                raise PrinterError("Homing cancelled by user (confirmation not received).")

        self.send_gcode(f"G28 {axes.upper()}")
        self.wait_moves_m400()
        self.move_absolute(x=self._limits[0][1]/2,y=self._limits[1][1]/2, z=-(self.cfg.attach_min_z)+10,speed_mm_s=20)


    # ---------------- Validation helpers ----------------
    @staticmethod
    def _range_check(v: float, lo: float, hi: float, name: str) -> None:
        if v < lo or v > hi:
            raise PrinterError(f"{name}={v:.3f} out of range [{lo:.3f}, {hi:.3f}]")

    def _check_xyz_with_attachment(self, x: float, y: float, z: float) -> None:
        """
        Проверяем, что bounding-box насадки целиком в пределах рабочего поля.
        """
        (xmin, xmax), (ymin, ymax), (zmin, zmax) = self.get_limits_cached()

        # Координаты крайних точек насадки
        x0 = x + float(self.cfg.attach_min_x)
        x1 = x + float(self.cfg.attach_max_x)
        y0 = y + float(self.cfg.attach_min_y)
        y1 = y + float(self.cfg.attach_max_y)
        z0 = z + float(self.cfg.attach_min_z)
        z1 = z + float(self.cfg.attach_max_z)

        self._range_check(x0, xmin, xmax, "X+attach_min_x")
        self._range_check(x1, xmin, xmax, "X+attach_max_x")
        self._range_check(y0, ymin, ymax, "Y+attach_min_y")
        self._range_check(y1, ymin, ymax, "Y+attach_max_y")
        self._range_check(z0, zmin, zmax, "Z+attach_min_z")
        self._range_check(z1, zmin, zmax, "Z+attach_max_z")

    # ---------------- Motion limits ----------------
    def set_motion_limits(self, velocity_mm_s: float, accel_mm_s2: float) -> None:
        self._ensure_ready()
        if velocity_mm_s <= 0 or accel_mm_s2 <= 0:
            raise PrinterError("velocity_mm_s and accel_mm_s2 must be > 0")
        self.send_gcode(f"SET_VELOCITY_LIMIT VELOCITY={velocity_mm_s:.3f} ACCEL={accel_mm_s2:.1f}")

    # ---------------- Moves ----------------
    def move_absolute(self, *, x: float, y: float, z: float, speed_mm_s: float, wait: bool = True) -> None:
        self._ensure_ready()
        self._ensure_homed("xyz")

        if speed_mm_s <= 0:
            raise PrinterError("speed_mm_s must be > 0")

        x = float(x); y = float(y); z = float(z)
        self._check_xyz_with_attachment(x, y, z)

        F = int(speed_mm_s * 60.0)
        self.send_gcode("\n".join([
            "G90",
            f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{F}",
        ]))
        if wait:
            self.wait_moves()

    def safe_y_pass(
        self,
        *,
        x: float,
        y_start: float,
        y_end: float,
        z_safe: float,
        z_contact: float,
        travel_speed_mm_s: float = 25.0,
        approach_speed_mm_s: float = 25.0,
        wait: bool = True,
    ) -> None:
        """
        Безопасный проезд колесиком строго вдоль Y.

        z_safe задаёте явно (в воздухе). Код проверит, что с учётом насадки
        и на z_safe, и на z_contact ничего не выходит за пределы.
        """
        self._ensure_ready()
        self._ensure_homed("xyz")

        if travel_speed_mm_s <= 0 or approach_speed_mm_s <= 0:
            raise PrinterError("Speeds must be > 0")

        x = float(x)
        y_start = float(y_start)
        y_end = float(y_end)
        z_safe = float(z_safe)
        z_contact = float(z_contact)

        # Проверим все ключевые точки (с учётом габаритов)
        self._check_xyz_with_attachment(x, y_start, z_safe)
        self._check_xyz_with_attachment(x, y_end, z_safe)
        self._check_xyz_with_attachment(x, y_start, z_contact)
        self._check_xyz_with_attachment(x, y_end, z_contact)

        Fz = int(self.cfg.z_speed_mm_s * 60.0)
        F_approach = int(approach_speed_mm_s * 60.0)
        F_travel = int(travel_speed_mm_s * 60.0)

        script = "\n".join([
            "G90",
            f"G1 Z{z_safe:.3f} F{Fz}",
            f"G1 X{x:.3f} Y{y_start:.3f} F{F_approach}",
            f"G1 Z{z_contact:.3f} F{Fz}",
            f"G1 Y{y_end:.3f} F{F_travel}",  # строго по Y
            f"G1 Z{z_safe:.3f} F{Fz}",
        ])
        self.send_gcode(script)

        if wait:
            self.wait_moves_m400()
      #%%
if __name__=='__main__':

    '''
    Если насадка выступает вправо по X на 30 мм и влево на 5 мм:
    attach_min_x = -5, attach_max_x = +30

    Если колесо ниже сопла на 12 мм (выступ вниз, т.е. к столу), и вверх насадка не выступает:
    attach_min_z = -12, attach_max_z = 0

    Если вперёд по Y выступ 20 мм, назад 0:
    attach_min_y = 0, attach_max_y = +20
    '''

    p = Printer(PrinterConfig(
        base_url="http://10.2.15.109:7125",
        attach_min_x=-30,  attach_max_x=30,
        attach_min_y=-20,  attach_max_y=-20,
        attach_min_z=-40, attach_max_z=0,
        ))

    print(p.printer_info())
    #%%
    p.home("XYZ")
    p.set_motion_limits(velocity_mm_s=100, accel_mm_s2=500)
    #%%
    for x in [100,200,300]:
        p.safe_y_pass(
        x=x,
        y_start=20,
        y_end=250,
        z_safe=50,
        z_contact=40,          # подобрать экспериментально!
        approach_speed_mm_s=100,
        travel_speed_mm_s=60,
        )