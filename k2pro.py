from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
import time
import requests

__version__=1.0
__date__='2026.01.23'


class PrinterError(RuntimeError):
    """Ошибки связи/состояния/валидации команд принтера."""


@dataclass
class K2ProConfig:
    base_url: str                  # пример: "http://192.168.1.50:7125"
    api_key: Optional[str] = None  # если Moonraker защищён ключом
    timeout: float = 10.0

    # Вы сказали: "ездить в воздухе" — держим минимальный Z.
    # Можно поменять на 5/20 по ситуации.
    z_air_min: float = 30.0


class K2Pro:
    """
    Управление Creality K2 Pro через Moonraker (Klipper).
    Основная цель: безопасные перемещения головы "в воздухе".
    """

    def __init__(self, cfg: K2ProConfig):
        self.cfg = cfg

    # ---------------- HTTP helpers ----------------
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

    # ---------------- Basic status ----------------
    def printer_info(self) -> Dict[str, Any]:
        return self._get("/printer/info")["result"]

    def query_status(self) -> Dict[str, Any]:
        """
        Вытаскиваем ключевые объекты: координаты, лимиты, состояние, хоуминг.
        """
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

    def get_position(self) -> Tuple[float, float, float]:
        st = self.query_status()
        pos = st["toolhead"]["position"]  # [x, y, z, e]
        return float(pos[0]), float(pos[1]), float(pos[2])

    def get_limits(self) -> Tuple[Tuple[float, float], Tuple[float, float], Tuple[float, float]]:
        """
        Лимиты из конфигурации Klipper (toolhead.axis_minimum/maximum).
        """
        st = self.query_status()
        mn = st["toolhead"]["axis_minimum"]  # [xmin,ymin,zmin,emin]
        mx = st["toolhead"]["axis_maximum"]  # [xmax,ymax,zmax,emax]
        return (float(mn[0]), float(mx[0])), (float(mn[1]), float(mx[1])), (float(mn[2]), float(mx[2]))

    # ---------------- G-code ----------------
    def send_gcode(self, script: str) -> None:
        """
        Отправить G-code (одна или несколько строк, разделённых \\n).
        """
        self._post("/printer/gcode/script", {"script": script})

    def home(self, axes: str = "XYZ") -> None:
        """
        Хоуминг. Осторожно: принтер поедет к концевикам.
        """
        self._ensure_ready()
        self.send_gcode(f"G28 {axes.upper()}")

    def wait_moves(self, poll_interval: float = 0.2, timeout: float = 60.0) -> None:
        """
        Ожидание завершения перемещений.
        """
        t0 = time.time()
        while True:
            st = self.query_status()
            moving = bool(st.get("toolhead", {}).get("moving", False))
            if not moving:
                return
            if time.time() - t0 > timeout:
                raise PrinterError("Timeout waiting for moves to finish")
            time.sleep(poll_interval)

    # ---------------- Validation ----------------
    @staticmethod
    def _range_check(v: float, lo: float, hi: float, name: str) -> None:
        if v < lo or v > hi:
            raise PrinterError(f"{name}={v:.3f} out of range [{lo:.3f}, {hi:.3f}]")

    def validate_point_air(self, x: float, y: float, z: float) -> None:
        """
        Валидация точки для перемещений "в воздухе":
        - в пределах axis min/max
        - z не ниже cfg.z_air_min
        """
        (xmin, xmax), (ymin, ymax), (zmin, zmax) = self.get_limits()
        self._range_check(x, xmin, xmax, "X")
        self._range_check(y, ymin, ymax, "Y")
        self._range_check(z, zmin, zmax, "Z")
        if z < self.cfg.z_air_min:
            raise PrinterError(f"Z={z:.3f} ниже z_air_min={self.cfg.z_air_min:.3f} (режим 'в воздухе')")

    # ---------------- Moves: relative/absolute ----------------
    def move_relative(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0,
                      feedrate_mm_min: int = 3000, wait: bool = False, max_abs_dz: float = 5.0) -> None:
        """
        Относительный сдвиг.
        max_abs_dz — защита от случайного большого шага по Z.
        """
        self._ensure_ready()
        self._ensure_homed("xyz")

        if abs(dz) > max_abs_dz:
            raise PrinterError(f"Refusing dz={dz}: exceeds max_abs_dz={max_abs_dz} мм")

        parts = []
        if dx: parts.append(f"X{dx:.3f}")
        if dy: parts.append(f"Y{dy:.3f}")
        if dz: parts.append(f"Z{dz:.3f}")
        if not parts:
            return

        script = "\n".join([
            "G91",
            f"G1 {' '.join(parts)} F{int(feedrate_mm_min)}",
            "G90",
        ])
        self.send_gcode(script)

        if wait:
            self.wait_moves()

    def move_absolute_air(self, x: float, y: float, z: float,
                          speed_mm_s: float = 80.0, wait: bool = True) -> None:
        """
        Абсолютное перемещение в точку (x,y,z) с ограничением: z >= z_air_min.
        speed_mm_s переводится в F (мм/мин).
        """
        self._ensure_ready()
        self._ensure_homed("xyz")

        if speed_mm_s <= 0:
            raise PrinterError("speed_mm_s must be > 0")

        self.validate_point_air(x, y, z)
        F = int(speed_mm_s * 60.0)

        self.send_gcode("\n".join([
            "G90",
            f"G1 X{x:.3f} Y{y:.3f} Z{z:.3f} F{F}",
        ]))

        if wait:
            self.wait_moves()

    # ---------------- Your requested: start->end line ----------------
    def move_line_air(self,
                      start: Tuple[float, float, float],
                      end: Tuple[float, float, float],
                      speed_mm_s: float,
                      wait: bool = True) -> None:
        """
        Движение по прямой:
          1) перейти в start
          2) затем одним прямолинейным перемещением перейти в end
        В обоих точках Z должен быть >= z_air_min.
        """
        self._ensure_ready()
        self._ensure_homed("xyz")

        if speed_mm_s <= 0:
            raise PrinterError("speed_mm_s must be > 0")

        x1, y1, z1 = map(float, start)
        x2, y2, z2 = map(float, end)

        self.validate_point_air(x1, y1, z1)
        self.validate_point_air(x2, y2, z2)

        F = int(speed_mm_s * 60.0)

        script = "\n".join([
            "G90",
            f"G1 X{x1:.3f} Y{y1:.3f} Z{z1:.3f} F{F}",
            f"G1 X{x2:.3f} Y{y2:.3f} Z{z2:.3f} F{F}",
        ])
        self.send_gcode(script)

        if wait:
            self.wait_moves()

    # ---------------- Convenience: safe XY while staying in air ----------------
    def move_xy_air(self, x: float, y: float, speed_mm_s: float = 120.0, wait: bool = True) -> None:
        """
        Переместиться по XY, сохранив текущий Z, но не ниже z_air_min:
        - если текущий Z < z_air_min, сначала поднимем Z до z_air_min
        - потом переместим XY
        """
        self._ensure_ready()
        self._ensure_homed("xyz")

        cx, cy, cz = self.get_position()
        z = max(cz, self.cfg.z_air_min)

        self.validate_point_air(x, y, z)

        F = int(speed_mm_s * 60.0)
        script = "\n".join([
            "G90",
            f"G1 Z{z:.3f} F{int(30*60)}",          # поднимаем/фиксируем Z более медленно (30 мм/с)
            f"G1 X{x:.3f} Y{y:.3f} F{F}",
        ])
        self.send_gcode(script)

        if wait:
            self.wait_moves()
            
if __name__=='__main__':
    printer = K2Pro(K2ProConfig(
        base_url="http://IP_ПРИНТЕРА:7125",
        z_air_min=15.0,   # ездим в воздухе не ниже 15 мм
    ))
    
    print(printer.printer_info())
    
    printer.home("XYZ")
    
    start = (50, 50, 50)
    end   = (70, 80, 50)
    
    printer.move_line_air(start, end, speed_mm_s=20)  # 100 мм/с по прямой