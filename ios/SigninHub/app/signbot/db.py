"""SQLite 存储：签到记录、通知、设置覆盖。线程安全（每操作独立连接）。"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

from .config import DB_PATH, harden_perm, tighten_data_dir

_LOCK = threading.Lock()


@contextmanager
def _conn():
    with _LOCK:
        # H3（BUGS-v2.61）：库文件里有签到明细与账号标识，权限必须 0600。
        # 首次连接会创建文件，所以在 connect 之后立刻收紧。
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        harden_perm(DB_PATH, 0o600)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    tighten_data_dir()
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS sign_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider   TEXT NOT NULL,
                day        TEXT NOT NULL,
                status     TEXT NOT NULL,      -- success | already | fail
                http_status INTEGER,
                message    TEXT,
                points     TEXT,
                detail     TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_logs_provider_day ON sign_logs(provider, day);
            CREATE TABLE IF NOT EXISTS notify_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level      TEXT,
                title      TEXT,
                body       TEXT,
                ok         INTEGER,
                created_at TEXT NOT NULL
            );
            """
        )
    _migrate()


def _migrate() -> None:
    """补列：老库没有 total_points / streak_days / used_points，ALTER 进来（SQLite 不支持 IF NOT EXISTS）。"""
    wanted = {"total_points": "INTEGER NOT NULL DEFAULT -1",
              "streak_days": "INTEGER NOT NULL DEFAULT -1",
              # 「累计已用」：资产卡翻转后显示的数字。WB 走 assets 的 used_total，
              # 其余三家由各自 provider 回填（Trae 逐包 credits_amount 求和、
              # Qoder user_quota.used + add_on_quota.used、MiniMax consumed）。
              "used_points": "INTEGER NOT NULL DEFAULT -1"}
    with _conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(sign_logs)").fetchall()}
        for name, decl in wanted.items():
            if name not in cols:
                try:
                    c.execute("ALTER TABLE sign_logs ADD COLUMN %s %s" % (name, decl))
                except sqlite3.OperationalError:
                    pass


def today_str(offset_hours: int = 8) -> str:
    tz = timezone(timedelta(hours=offset_hours))
    return datetime.now(tz).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def add_sign_log(provider: str, day: str, status: str, http_status, message: str,
                 points: str = "", detail: str = "",
                 total_points: int = -1, streak_days: int = -1,
                 used_points: int = -1) -> int:
    """写入签到记录：同一 provider 同一 day 只保留一条，新记录覆盖旧的。

    这样「一键全部签到」连点多次也不会堆出重复记录——当天永远最多一条/服务。
    total_points / streak_days / used_points 传 -1 表示这次没读到，保留库里已有的值，
    避免被覆盖成空；传 -2（total_points 专用）表示「这个 provider 压根没有积分概念」，
    要把旧值清成 NULL —— 否则像 MiniMax 这种只有连签天数的服务，会一直挂着上一版写坏的数字。
    """
    detail = detail[:4000] if detail else ""
    ts = now_iso()
    with _conn() as c:
        row = c.execute(
            "SELECT id, points, total_points, streak_days, used_points FROM sign_logs"
            " WHERE provider=? AND day=? ORDER BY id DESC LIMIT 1",
            (provider, day),
        ).fetchone()
        if row is not None:
            # 积分累加：已是 success 后又被 already 覆盖时不重复加
            keep_points = points
            if not keep_points and row["points"] and status != "fail":
                # 失败时不要把上一轮成功拿到的 points 留在今天这条记录上：
                # 前端「今日 +N」直接读这个字段，留着会让人以为这次也拿到了。
                keep_points = row["points"]
            # -2 = 这个服务没有积分概念，把旧值清成 -1（列是 NOT NULL，不能用 NULL；
            # 前端把 -1 当「没读到」不显示，效果就是清空）。
            if total_points == -2:
                keep_total = -1
            else:
                keep_total = total_points if total_points >= 0 else row["total_points"]
            keep_streak = streak_days if streak_days >= 0 else row["streak_days"]
            keep_used = used_points if used_points >= 0 else row["used_points"]
            c.execute(
                "UPDATE sign_logs SET status=?, http_status=?, message=?, points=?, detail=?,"
                " total_points=?, streak_days=?, used_points=?, created_at=? WHERE id=?",
                (status, http_status, message, keep_points, detail,
                 keep_total, keep_streak, keep_used, ts, row["id"]),
            )
            return row["id"]
        # 插入时 -2（该服务没有积分概念）落成 -1（未知）：列是 NOT NULL，
        # 前端把 -1 当「没读到」不显示，效果一样。
        ins_total = -1 if total_points == -2 else total_points
        cur = c.execute(
            "INSERT INTO sign_logs(provider,day,status,http_status,message,points,detail,"
            "total_points,streak_days,used_points,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (provider, day, status, http_status, message, points, detail,
             ins_total, streak_days, used_points, ts),
        )
        return cur.lastrowid


def update_totals(provider: str, day: str, total: int = -1, streak: int = -1,
                  used: int = -1) -> bool:
    """只更新某天某 provider 的持有积分 / 连登天数 / 累计已用，不动 status / message / points。

    为什么需要它：首页卡片与 Token 健康的 ``total_points`` 读的是**当天签到记录**，
    而「刷新余额」查到的新读数原先只回传给前端、没有落库 —— 于是资产卡变了、
    首页与 Token 健康却纹丝不动。

    当天还没有任何记录时**补一条 idle 占位**（用户 m14001）：以前这里是纯 UPDATE，
    查不到记录就什么都不做 —— 于是「刚填完签到配置、今天还没签」这个最常见的场景下，
    余额刷新拿到了数字却无处可写，卡片永远显示「—」。
    补出来的行 status='idle'，``recent_days()`` 统计时会把 idle 排除在 active 之外，
    所以不会污染「本周完成情况」与连续天数，只是给 total_points 一个落脚的格子。
    """
    total = -1 if total is None else int(total)
    streak = -1 if streak is None else int(streak)
    used = -1 if used is None else int(used)
    if total < 0 and streak < 0 and used < 0:
        return False
    sets, args = [], []
    if total >= 0:
        sets.append("total_points=?")
        args.append(total)
    if streak >= 0:
        sets.append("streak_days=?")
        args.append(streak)
    if used >= 0:
        sets.append("used_points=?")
        args.append(used)
    args.extend([provider, day])
    with _conn() as c:
        cur = c.execute(
            "UPDATE sign_logs SET " + ", ".join(sets) + " WHERE provider=? AND day=?",
            tuple(args),
        )
        if cur.rowcount > 0:
            return True
        # 没有当天记录 → 插一条 idle 占位，把这次读到的数字存下来。
        c.execute(
            "INSERT INTO sign_logs(provider,day,status,http_status,message,points,detail,"
            "total_points,streak_days,used_points,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (provider, day, "idle", None, "今日尚未执行", "", "",
             max(total, -1), max(streak, -1), max(used, -1), now_iso()),
        )
        return True


def dedupe_sign_logs() -> int:
    """清理历史重复：同一 provider+day 只保留 id 最大（最新）的一条。返回删除条数。"""
    with _conn() as c:
        rows = c.execute(
            "SELECT provider, day, COUNT(*) n FROM sign_logs GROUP BY provider, day HAVING n > 1"
        ).fetchall()
        removed = 0
        for r in rows:
            keep = c.execute(
                "SELECT MAX(id) FROM sign_logs WHERE provider=? AND day=?",
                (r["provider"], r["day"]),
            ).fetchone()[0]
            cur = c.execute(
                "DELETE FROM sign_logs WHERE provider=? AND day=? AND id<>?",
                (r["provider"], r["day"], keep),
            )
            removed += cur.rowcount
        return removed


def latest_by_provider(day: str | None = None) -> dict:
    """每个 provider 最新一条记录（可限定某天）。"""
    q = "SELECT * FROM sign_logs"
    args = []
    if day:
        q += " WHERE day = ?"
        args.append(day)
    q += " ORDER BY id ASC"
    out = {}
    with _conn() as c:
        for row in c.execute(q, args):
            out[row["provider"]] = dict(row)  # 后出现的覆盖 -> 取最新
    return out


def provider_history(provider: str, limit: int = 30) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sign_logs WHERE provider=? AND status<>'idle'"
            " ORDER BY id DESC LIMIT ?",
            (provider, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def recent_logs(limit: int = 100) -> list:
    # status='idle' 的行是 update_totals() 补出来的「只有余额、没有签到动作」的占位行
    # （见 update_totals 的注释），不属于签到历史 —— 记录页不该出现它们。
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM sign_logs WHERE status<>'idle' ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def day_summary(day: str) -> dict:
    with _conn() as c:
        rows = c.execute("SELECT * FROM sign_logs WHERE day=? ORDER BY id ASC", (day,)).fetchall()
    return {r["provider"]: dict(r) for r in rows}


def recent_days(n: int = 7, offset_hours: int = 8) -> list:
    """返回最近 n 天（含今天）每天的签到完成情况，按日期升序。"""
    tz = timezone(timedelta(hours=offset_hours))
    today = datetime.now(tz)
    out = []
    for i in range(n - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        summ = day_summary(d)
        statuses = {k: v["status"] for k, v in summ.items()}
        # idle = 该服务当天根本没被执行（只刷了余额、没签到），不算漏签
        active = {k: v for k, v in statuses.items() if v != "idle"}
        done = sum(1 for s in active.values() if s in ("success", "already"))
        out.append({
            "day": d,
            "weekday": "日一二三四五六"[(today - timedelta(days=i)).isoweekday() % 7],
            "done": done,
            "total": len(active),
            "statuses": statuses,
            "ok": len(active) > 0 and done == len(active),
        })
    return out


def compute_streak(days: list) -> int:
    """从最近一天向前统计连续「全部完成」天数；若今天尚未完成，从昨天起算。"""
    if not days:
        return 0
    seq = list(reversed(days))  # 今天在前
    idx = 0
    if not seq[0]["ok"]:
        idx = 1  # 今天还没完成，不计入也不截断，从昨天开始数
    streak = 0
    while idx < len(seq) and seq[idx]["ok"]:
        streak += 1
        idx += 1
    return streak


def add_notify_log(level: str, title: str, body: str, ok: bool) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO notify_logs(level,title,body,ok,created_at) VALUES(?,?,?,?,?)",
            (level, title, body, 1 if ok else 0, now_iso()),
        )


def recent_notify_logs(limit: int = 20) -> list:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, level, title, body, ok, created_at FROM notify_logs "
            "ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    # 同样排除 idle 占位行（update_totals 补出来的、没有签到动作的记录），
    # 否则「总记录数」会把每次余额刷新都算进去。
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM sign_logs WHERE status<>'idle'").fetchone()[0]
        success = c.execute("SELECT COUNT(*) FROM sign_logs WHERE status='success'").fetchone()[0]
        fail = c.execute("SELECT COUNT(*) FROM sign_logs WHERE status='fail'").fetchone()[0]
    return {"total": total, "success": success, "fail": fail}
