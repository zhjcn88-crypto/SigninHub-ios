"""轻量定时调度器：每天在配置的时间点触发 run_all（带随机浮动）。

为什么需要随机浮动（jitter）：
    固定 HH:MM 精确打卡的行为特征太明显——每天同一分钟、同样的调用顺序。
    这里把「目标时间」改成「目标时间 + 当天固定的随机偏移」，
    偏移在当天首次进入判定窗口时抽出并落盘，保证：
      1) 同一天内多次轮询不会抖动（不会一会儿说该跑了一会儿说不该跑）
      2) 重启后偏移不变（不会因为重启把已抽到的延迟重置回 0）
    偏移范围 0~jitter_min 分钟，jitter_min=0 时行为与旧版完全一致。
"""
import json
import random
import threading
from datetime import datetime, timezone, timedelta

from . import runner, db
from .config import load_config, DATA_DIR


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self._last_run_day = None
        # 当天抽到的偏移（分钟）：{day: minutes}。落盘到 DATA_DIR/sched_jitter.json
        self._plan = self._load_plan()

    def stop(self):
        self._stop.set()

    # ── 偏移计划（落盘，重启不丢） ─────────────────────────────────
    @property
    def _plan_path(self):
        p = DATA_DIR / "sched_jitter.json"
        return p

    def _load_plan(self) -> dict:
        try:
            with open(self._plan_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_plan(self):
        try:
            self._plan_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._plan_path, "w", encoding="utf-8") as f:
                json.dump(self._plan, f, ensure_ascii=False)
        except Exception:
            pass

    def _delay_for(self, day: str, jitter_min: int) -> int:
        """取当天偏移；没有就抽一个并落盘。"""
        try:
            j = int(jitter_min or 0)
        except Exception:
            j = 0
        if j <= 0:
            return 0
        if day in self._plan:
            try:
                return max(0, min(j, int(self._plan[day])))
            except Exception:
                pass
        v = random.randint(0, j)
        self._plan = {day: v}          # 只保留当天，避免文件无限增长
        self._save_plan()
        return v

    # ── 时间 ──────────────────────────────────────────────────────
    def _now(self, offset_hours: int) -> datetime:
        return datetime.now(timezone(timedelta(hours=offset_hours)))

    @staticmethod
    def _hhmm(dt: datetime) -> str:
        return dt.strftime("%H:%M")

    def _due(self, now: datetime, times, jitter_min: int) -> bool:
        """now 是否已过某个目标时间的 (t + 当天偏移)，且还在容忍窗口内。

        用「已过 + 窗口」而不是「等于」：轮询间隔 30 秒，
        精确比分钟容易漏触发（某次轮询刚好跳过那一分钟就整天不跑）。
        窗口 5 分钟足够宽，配合 _last_run_day 保证一天只跑一次。
        """
        day = now.strftime("%Y-%m-%d")
        delay = self._delay_for(day, jitter_min)
        for t in times:
            try:
                hh, mm = str(t).split(":")
                t0 = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            except Exception:
                continue
            # 偏移必须夹在当天之内：否则 fire_at 落到次日，当天的判定窗口永远等不到，
            # 次日又会按新日期重新抽偏移（实测 times=["23:58"] + jitter 30 时整天不触发）。
            # 上限取 23:54，保证 5 分钟容忍窗口也不会跨过零点。
            max_delay = (23 * 60 + 54) - (int(hh) * 60 + int(mm))
            eff = max(0, min(delay, max_delay))
            fire_at = t0 + timedelta(minutes=eff)
            if fire_at <= now < fire_at + timedelta(minutes=5):
                return True
        return False

    def run(self):
        cfg = load_config()
        if cfg["scheduler"].get("run_on_start"):
            try:
                runner.run_all(trigger="startup")
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                cfg = load_config()
                sc = cfg["scheduler"]
                if sc.get("enabled"):
                    off = sc.get("timezone_offset_hours", 8)
                    now = self._now(off)
                    day = now.strftime("%Y-%m-%d")
                    times = set(sc.get("times", []))
                    jitter = sc.get("jitter_min", 0)
                    # 当天已跑过就不再跑（哪怕还在窗口内）
                    if self._last_run_day != day and self._due(now, times, jitter):
                        self._last_run_day = day
                        runner.run_all(trigger="schedule")
            except Exception:
                pass
            # 每 30 秒检查一次
            self._stop.wait(30)
