"""模拟适配器：温控探头与孵化箱信号。

真实部署时替换为串口/MQTT 采集；本地演示与测试用本模块产生
带小幅波动的温度曲线，以及随机（可注入）的停机事件。
"""
from __future__ import annotations

import math
import random
from datetime import datetime

# 孵化设定温度（℃）；偏离 ±1.5℃ 视为告警
SET_TEMP = 37.8
ALARM_DELTA = 1.5


class MockTemperatureAdapter:
    """模拟温控探头：正弦日波动 + 随机噪声，可注入故障偏移。"""

    def __init__(self, set_temp: float = SET_TEMP, seed: int | None = None):
        self.set_temp = set_temp
        self.fault_offset = 0.0
        self._rnd = random.Random(seed)

    def read(self, ts: datetime | None = None) -> dict:
        ts = ts or datetime.now()
        wave = 0.25 * math.sin(ts.hour / 24.0 * 2 * math.pi)
        noise = self._rnd.uniform(-0.12, 0.12)
        temp = round(self.set_temp + wave + noise + self.fault_offset, 2)
        return {
            "temp_c": temp,
            "set_temp_c": self.set_temp,
            "alarm": abs(temp - self.set_temp) > ALARM_DELTA,
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def inject_fault(self, offset: float) -> None:
        """测试/演示用：注入温度偏移（模拟温控异常）。"""
        self.fault_offset = offset

    def clear_fault(self) -> None:
        self.fault_offset = 0.0


class MockIncubatorSignalAdapter:
    """模拟孵化箱运行信号：running / stopped 由本适配器随机或注入触发。"""

    def __init__(self, stop_probability: float = 0.0, seed: int | None = None):
        self.running = True
        self.stop_probability = stop_probability
        self._rnd = random.Random(seed)

    def poll(self) -> dict:
        if self.running and self._rnd.random() < self.stop_probability:
            self.running = False
            return {"signal": "stopped", "reason": "power_loss"}
        return {"signal": "running" if self.running else "stopped"}

    def force_stop(self) -> None:
        self.running = False

    def force_resume(self) -> None:
        self.running = True
