from __future__ import annotations

__version__=1.0
__date__='2026.01.23'

from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import time
import requests


class PrinterError(RuntimeError):
 pass


@dataclass
class K2ProConfig:
    base_url: str                  # например: "http://192.168.1.50:7125"
    api_key: Optional[str] = None
    timeout: float = 10.0

    # Безопасная высота "в воздухе" (мм)
    z_air_min: float = 20.0

    # Скорость подъёма/опускания Z (мм/с)
    z_speed_mm_s: float = 8.0


class K2Pro:
    def __init__(self, cfg: K2ProConfig, *, auto_init: bool = True):
        self.cfg = cfg

        # Кэш лимитов (xmin/xmax/ymin/ymax/zmin/zmax)
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
        """
        Инициализация клиента: проверяем, что принтер доступен, и кэшируем лимиты осей.
        """
        self._ensure_ready()
        self.refresh_limits()

    def refresh_limits(self) -> None:
        """
        Обновить кэш лимитов осей из Klipper.
        Вызывать после FIRMWARE_RESTART / изменения конфигурации.
        """
        st = self.query_status()
        mn = st["toolhead"]["axis_minimum"]  # [xmin,ymin,zmin,emin]
        mx = st["toolhead"]["axis_maximum"]  # [xmax,ymax,zmax,emax]
        self._limits = ( (float(mn[0]), float(mx[0])),
                         (float(mn[1]), float(mx[1])),
                         (float(mn[2]), float(mx[2])) )

    def get_limits_cached(self) -> Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]:
        if self._limits is None:
            raise PrinterError("Limits are not initialized. Call initialize() or use auto_init=True.")
        return self._limits

    # ---------------- G-code ----------------
    def send_gcode(self, script: str) -> None:
        self._post("/printer/gcode/script", {"script": script})

    def home(self, axes: str = "XYZ") -> None:
        self._ensure_ready()
        self.send_gcode(f"G28 {axes.upper()}")

    # ---------------- Validation helpers ----------------
    @staticmethod
    def _range_check(v: float, lo: float, hi: float, name: str) -> None:
        if v < lo or v > hi:
            raise PrinterError(f"{name}={v:.3f} out of range [{lo:.3f}, {hi:.3f}]")

    def _check_xyz_in_limits(self, x: float, y: float, z: float) -> None:
        (xmin, xmax), (ymin, ymax), (zmin, zmax) = self.get_limits_cached()
        self._range_check(x, xmin, xmax, "X")
        self._range_check(y, ymin, ymax, "Y")
        self._range_check(z, zmin, zmax, "Z")

    # ---------------- Motion limits ----------------
    def set_motion_limits(self, velocity_mm_s: float, accel_mm_s2: float) -> None:
        self._ensure_ready()
        if velocity_mm_s <= 0 or accel_mm_s2 <= 0:
            raise PrinterError("velocity_mm_s and accel_mm_s2 must be > 0")
        self.send_gcode(f"SET_VELOCITY_LIMIT VELOCITY={velocity_mm_s:.3f} ACCEL={accel_mm_s2:.1f}")

    # ---------------- Safe Y-only pass ----------------
    def safe_y_pass(
        self,
        *,
        x: float,
        y_start: float,
        y_end: float,
        z_contact: float,
        travel_speed_mm_s: float = 25.0,
        approach_speed_mm_s: float = 25.0,
        wait: bool = True,
    ) -> None:
        """
        Безопасный проезд колесиком строго вдоль Y:
          Z вверх -> (X, Y_start) -> Z вниз -> Y_end -> Z вверх
        """
        self._ensure_ready()
        self._ensure_homed("xyz")

        if travel_speed_mm_s <= 0 or approach_speed_mm_s <= 0:
            raise PrinterError("Speeds must be > 0")

        x = float(x)
        y_start = float(y_start)
        y_end = float(y_end)
        z_contact = float(z_contact)

        # Валидация по кэшированным лимитам (НЕ дергаем Moonraker каждый раз)
        self._check_xyz_in_limits(x, y_start, z_contact)
        self._check_xyz_in_limits(x, y_end, z_contact)

        z_air = float(self.cfg.z_air_min)
        if z_air < z_contact:
            # в таком случае поднимемся хотя бы на z_contact, чтобы не ехать "сквозь"
            z_air = z_contact

        Fz = int(self.cfg.z_speed_mm_s * 60.0)
        F_approach = int(approach_speed_mm_s * 60.0)
        F_travel = int(travel_speed_mm_s * 60.0)

        script = "\n".join([
            "G90",
            f"G1 Z{z_air:.3f} F{Fz}",
            f"G1 X{x:.3f} Y{y_start:.3f} F{F_approach}",
            f"G1 Z{z_contact:.3f} F{Fz}",
            f"G1 Y{y_end:.3f} F{F_travel}",  # строго по Y
            f"G1 Z{z_air:.3f} F{Fz}",
        ])
        self.send_gcode(script)

        if wait:
            self.wait_moves()
      #%%       
if __name__=='__main__':
   #%%
    p = K2Pro(K2ProConfig(
        base_url="http://10.2.15.109:7125",
        z_air_min=30,   # ездим в воздухе не ниже 
    ))
    
    print(p.printer_info())
    #%%
    p.home("XYZ")
    p.set_motion_limits(velocity_mm_s=35, accel_mm_s2=500)
    #%%

    
    p.safe_y_pass(
    x=150,
    y_start=30,
    y_end=250,
    z_contact=20,          # подобрать экспериментально!
    approach_speed_mm_s=25,
    travel_speed_mm_s=20,
    )
    
    

   