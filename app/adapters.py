"""模拟适配器：温控与孵化箱信号。

真实部署时替换这两个类即可（保持同名方法），业务层不依赖具体硬件。
温度曲线：停机后温度按指数下降，恢复供电后回升。
"""
import math
from datetime import datetime, timedelta

SET_TEMP_C = 37.8          # 标准孵化温度
SAFE_MIN_C = 36.0          # 低于此温视为受影响
STOP_COOLDOWN_MIN = 1.2    # 停机降温时间常数（分钟，模拟值）
WARMUP_MIN = 2.0           # 复电升温时间常数


class SimTempAdapter:
    """温度模拟：根据最近一次停机区间推算任意时刻温度。"""

    def __init__(self, stop_started_at: datetime | None = None,
                 stop_ended_at: datetime | None = None):
        self.stop_started_at = stop_started_at
        self.stop_ended_at = stop_ended_at

    def temp_at(self, t: datetime) -> float:
        ambient = 24.0
        if self.stop_started_at is None or t < self.stop_started_at:
            return round(SET_TEMP_C, 2)
        if self.stop_ended_at is None or self.stop_started_at <= t < self.stop_ended_at:
            mins = (t - self.stop_started_at).total_seconds() / 60
            v = ambient + (SET_TEMP_C - ambient) * math.exp(-mins / STOP_COOLDOWN_MIN)
            return round(v, 2)
        mins = (t - self.stop_ended_at).total_seconds() / 60
        v = SET_TEMP_C - (SET_TEMP_C - ambient) * math.exp(-mins / WARMUP_MIN)
        return round(v, 2)

    def affected(self, t: datetime) -> bool:
        return self.temp_at(t) < SAFE_MIN_C


class SimSignalAdapter:
    """孵化箱信号模拟：开机/停机事件流。

    预置一台运行中、一台发生过中途停机的孵化箱，供演示与测试。
    run() 驱动时钟时把生成的事件写入 signal_events。
    """

    def __init__(self):
        # incubator_code -> 'running' | 'stopped'
        self._state: dict[str, str] = {}

    def state(self, code: str) -> str:
        return self._state.get(code, 'running')

    def set_state(self, code: str, state: str) -> None:
        assert state in ('running', 'stopped')
        self._state[code] = state

    def emit(self, code: str, kind: str, at: datetime, detail: str = "") -> dict:
        self.set_state(code, 'running' if kind == 'power_on' else 'stopped')
        return {"incubator": code, "kind": kind, "occurred_at": at.isoformat(timespec="seconds"),
                "detail": detail}
