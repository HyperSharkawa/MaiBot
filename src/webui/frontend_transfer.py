"""以有界采样窗口计算下载速度和持续低速时间。"""

from collections import deque

SAMPLE_SECONDS = 1
WINDOW_SECONDS = 10
MIN_BYTES_PER_SECOND = 5_000
LOW_SPEED_SECONDS = 60


class LowSpeedError(TimeoutError):
    """ZIP 已持续低于最低速度，调用方应结束当前源。"""


class TransferMeter:
    """保存最多一个速度窗口的累计字节采样；不保留下载内容。"""

    def __init__(self, started: float):
        """以单调时钟建立零字节起点，供独立的一秒采样任务使用。"""
        self.samples = deque([(started, 0)], maxlen=WINDOW_SECONDS + 2)
        self.low_since = None

    def sample(self, now: float, received: int) -> tuple[float, float]:
        """返回最近窗口的字节速度和连续低速秒数；达到阈值即清零低速计时。"""
        previous_time = self.samples[-1][0]
        self.samples.append((now, received))
        while len(self.samples) > 2 and self.samples[1][0] <= now - WINDOW_SECONDS:
            self.samples.popleft()
        first_time, first_bytes = self.samples[0]
        elapsed = now - first_time
        speed = (received - first_bytes) / elapsed if elapsed > 0 else 0.0
        if speed < MIN_BYTES_PER_SECOND:
            if self.low_since is None:
                self.low_since = previous_time
        else:
            self.low_since = None
        low_seconds = now - self.low_since if self.low_since is not None else 0.0
        return speed, low_seconds
