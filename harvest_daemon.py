#!/usr/bin/env python3
"""全天打捞守护进程 —— 用多把 YouTube API Key 持续发现并跟踪 Minecraft Shorts。

设计要点
--------
1. 常驻进程，自己管调度（不依赖任务计划程序的抖动）
2. 多钥匙轮转，各自记当日用量，不超额
3. 自适应时间切片：按最近上传密度动态定窗口宽度，避免单次搜索 50 条截断
4. 零重叠：publishedAfter/Before 精确对接，每一点配额都买到新信息
5. 两段式：先采一轮，过不了门槛的直接扔，省下的配额全给有苗头的
6. 按年龄自适应采样；用「涨速加速」判定，不用绝对播放量

用法
----
    python harvest_daemon.py --dry-run          # 只看调度，不真发请求
    python harvest_daemon.py                    # 正式跑（Ctrl+C 停）
    python harvest_daemon.py --once             # 只跑一个周期（调试用）
    python harvest_daemon.py --report           # 打印当前池子状态后退出

环境变量（.env）
----------------
    YOUTUBE_API_KEY          第一把（必须）
    YOUTUBE_API_KEY_2        第二把（可选）
    YOUTUBE_API_KEY_3        第三把（可选）
    ...
    也可以写 YOUTUBE_API_KEY_SEARCH 作为候选池里的补充
"""

import argparse
import json
import math
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from yt_collect import BJT, YouTubeAPI

BASE = Path(__file__).parent
STATE_PATH = BASE / "harvest_state.json"

# ---------------------------------------------------------------------------
# 分层打捞（六档）
# ---------------------------------------------------------------------------
# 为什么必须分档：实测「播放/订阅比 ≥ 2」对大号数学上不可达 ——
# 393 万订阅的频道要 786 万播放才算预警，永远触发不了。
# 所以进池门槛、预警门槛都按订阅规模分档，小号看比值，大号看绝对量。

TIERS = ["T1", "T2", "T3", "T4", "T5", "T6"]

TIER_LABEL = {
    "T1": "0~100 订阅",
    "T2": "100~1k",
    "T3": "1k~10k",
    "T4": "10k~100k",
    "T5": "100k~1M",
    "T6": "1M+",
}

# 档位边界（订阅数下限）与每档规则
#
# 关于 alert_views：绝对门槛必须落在「发布后 6 小时内物理可达」的量级。
# 实测外推（速度 × 360 分钟）显示原来的 50 万/15 万根本到不了，
# 会导致一条都不预警。所以下调，并且加了「相对阈值」作为主判据。
TIER_RULES = {
    #            订阅下限      进池:播放/分  进池:最小播放   预警:绝对量   预警:速度倍数  高优先级:比值
    "T1": {"min_subs": 0,          "gate_vpm": 30, "gate_min_views": 200,    "alert_views": 2_000,    "alert_speed_mult": 6.0, "boost_ratio": 50.0},
    "T2": {"min_subs": 100,        "gate_vpm": 25, "gate_min_views": 250,    "alert_views": 5_000,    "alert_speed_mult": 6.0, "boost_ratio": 20.0},
    "T3": {"min_subs": 1_000,      "gate_vpm": 25, "gate_min_views": 300,    "alert_views": 15_000,   "alert_speed_mult": 6.0, "boost_ratio": 5.0},
    "T4": {"min_subs": 10_000,     "gate_vpm": 30, "gate_min_views": 500,    "alert_views": 40_000,   "alert_speed_mult": 6.0, "boost_ratio": 1.0},
    "T5": {"min_subs": 100_000,    "gate_vpm": 40, "gate_min_views": 1_000,  "alert_views": 100_000,  "alert_speed_mult": 5.0, "boost_ratio": 0.2},
    "T6": {"min_subs": 1_000_000,  "gate_vpm": 60, "gate_min_views": 2_000,  "alert_views": 300_000,  "alert_speed_mult": 5.0, "boost_ratio": 0.05},
}

# 候选池按档位分配名额（总数 400）—— 防止小号把池子撑爆
TIER_QUOTA = {"T1": 20, "T2": 60, "T3": 100, "T4": 100, "T5": 80, "T6": 40}


def tier_of(subs: int) -> str:
    """按订阅数定档。"""
    subs = int(subs or 0)
    for t in reversed(TIERS):
        if subs >= TIER_RULES[t]["min_subs"]:
            return t
    return "T1"


def tier_vpm_baseline(state, tier):
    """该档位实测的「播放/分钟」基线（有数据用数据，没有用默认）。

    默认值是量级先验，跑一天后自动被真实数据替换（取 p75）。
    """
    stats = (state.get("tier_stats") or {}).get(tier) or {}
    samples = stats.get("vpm_samples") or []
    if len(samples) >= 20:
        s = sorted(samples)
        return s[int(len(s) * 0.75)]        # p75
    return TIER_VPM_PRIOR[tier]


# 各档位「播放/分钟」的先验基线（用于标准化，让跨档位可比）
TIER_VPM_PRIOR = {"T1": 15.0, "T2": 20.0, "T3": 30.0,
                  "T4": 40.0, "T5": 60.0, "T6": 120.0}


def promotion_score(views, vpm, subs, tier, likes=0):
    """排序分 —— 「这个视频跑得比它同档位的常规水平快多少」。

    用途：① 档位名额满了，同档内谁留下 ② 通知邮件里的排序

    公式（加法，不是乘法）：
        log2(速度倍数) × 10  +  log10(播放量)

    为什么用加法：乘法会让播放量主导（实测 T6 的 2.5M 播放
    分数是 T1 黑马的 286 倍）。改成加法后两部分互不压倒：
      速度倍数 每翻一倍 → +10 分
      播放量 每翻十倍 → +1 分

    例：
      T1 50订阅/29.9万播放/200每分 → log2(13.3)*10 + log10(299000) = 37.3 + 5.5 = 42.8
      T6 393万订阅/250万播放/5万每分 → log2(416.7)*10 + log10(2500000) = 86.7 + 6.4 = 93.1
    """
    base = TIER_VPM_PRIOR.get(tier, 30.0)
    speed_mult = max(0.01, vpm / max(1.0, base))
    score = math.log2(speed_mult) * 10.0 + math.log10(max(10, views))
    if views >= 50 and likes:
        rate = likes / views
        if rate >= 0.03:
            score += min(8.0, (rate - 0.03) * 80)     # 点赞率加成，最多 +8 分
    return round(score, 2)



def record_tier_sample(state, tier, vpm):
    """记录该档位观察到的播放速度，用于算基线。"""
    stats = state.setdefault("tier_stats", {}).setdefault(
        tier, {"vpm_samples": [], "alerted": 0, "promoted": 0})
    arr = stats.setdefault("vpm_samples", [])
    arr.append(round(float(vpm), 2))
    if len(arr) > 500:
        del arr[:-500]


def alert_check(cand, state):
    """按档位判定是否该发预警。

    判定改为「满足其一」而不是「全部满足」：

      ① 速度异常：播放/分钟 ≥ 该档位基线 × alert_speed_mult
         （跟「自己档位的常规水平」比，小号跑出异常速度也能触发）
      ② 绝对量大：播放 ≥ alert_views
         （兜底，抓那些绝对值确实大的）

    「加速中」从必要条件降为**加分项**——原来要求三个条件同时满足，
    会把预警量压到零。

    返回 (是否预警, 是否高优先级, 详情 dict)
    """
    tier = cand.get("tier") or "T3"
    rule = TIER_RULES[tier]
    samples = cand.get("samples") or []
    if not samples:
        return False, False, {"why": "无采样"}

    views = int(samples[-1]["views"])
    subs = int(cand.get("channel_subs") or 0)
    age = age_minutes(cand.get("published_at"))
    vpm = views / age if age > 1 else 0
    base = tier_vpm_baseline(state, tier)
    speed_mult = vpm / max(1.0, base)
    acc, rates = is_accelerating(cand)

    detail = {"tier": tier, "views": views, "subs": subs, "vpm": round(vpm, 1),
              "speed_mult": round(speed_mult, 2), "base_vpm": round(base, 1),
              "alert_views": rule["alert_views"], "accel": acc, "rates": rates}

    hit_speed = speed_mult >= rule["alert_speed_mult"]
    hit_volume = views >= rule["alert_views"]

    if not (hit_speed or hit_volume):
        why = []
        if age < 360:
            why.append(f"观察中({age:.0f}/360分)")
        if views < rule["alert_views"]:
            why.append(f"量{views:,}<{rule['alert_views']:,}")
        if speed_mult < rule["alert_speed_mult"]:
            why.append(f"速度{speed_mult:.1f}x<{rule['alert_speed_mult']}x")
        return False, False, {**detail, "why": " ".join(why[:2])}

    # 高优先级：比值本身就是黑马级别（小号打出远超体量的成绩）
    ratio = (views / subs) if subs else 0
    priority = (ratio >= rule["boost_ratio"]) or (speed_mult >= rule["alert_speed_mult"] * 2)
    detail.update({"ratio": round(ratio, 3), "high_priority": priority,
                   "trigger": ("速度异常" if hit_speed else "") +
                              ("+绝对量" if (hit_speed and hit_volume) else
                               ("绝对量" if hit_volume else ""))})
    return True, priority, detail


# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------
CFG = {
    # ---- 切片 ----
    "target_results_per_search": 38,   # 每次搜索期望拿到的结果数（留 24% 余量，避免打满 50）
    "min_slice_seconds": 300,          # 切片最短 5 分钟（对齐 cron-job.org 触发频率）
    "max_slice_seconds": 1800,         # 切片最长 30 分钟（深夜低谷期）

    # ---- 门槛 ----
    "max_duration": 61,                # 只收 Shorts
    # 标题必须包含其中**至少一个完整词**（按词匹配，不是子串匹配）
    # 用词匹配是为了拦掉 "minecraft" 出现在无关语境里的视频（例如 "Mike Tomlin"）
    "title_keywords": ["#minecraft", "minecraft", "minecraftmemes", "minecraftshorts",
                       "maizen", "verity", "#mc", "mc"],
    "title_allow_hashtag": True,       # 允许 #minecraft 这种 hashtag 形式也算命中
    # ---- 频道门槛：已改为按订阅分档（见 TIER_RULES）----
    # 旧的统一门槛 min_channel_subs=10000 已废弃 —— 它会滤掉实测存在的
    # 92 订阅 / 29.9 万播放（3250x）这类黑马，而它们正是最有价值的信号。
    # 现在所有档位都能进池，靠 TIER_RULES 的每档门槛 + TIER_QUOTA 的分档名额控制质量。

    # ---- 内容形态过滤（拦「跑题」和「有人声口播」）----
    # 允许的分类。实测池内跑题视频集中在 Travel/People&Blogs；
    # 而真正在爆的几乎都在 Gaming。
    # 1=Film&Animation（MAIZEN 那类动画） 20=Gaming 24=Entertainment 23=Comedy
    "allow_categories": ["1", "20", "23", "24"],
    # 描述超过这个长度视为「讲解/口播型」→ 大概率有人声，不适合直接搬运。
    # 实测：纯视觉梗的描述长度是 0~300；口播型是 700~4300。
    "max_description_chars": 400,
    # 描述里出现这些词 = 直播/切片/转载，一律不要
    "banned_desc_markers": ["twitch.tv", "kick.com", "live stream", "直播", "clip:"],
    # 标题里出现这些词 = 直播切片或合集，不要
    "banned_title_markers": ["live stream", "stream highlight", "twitch", "full video", "compilation"],

    # ---- 播放速度门槛 ----
    "gate_min_views_per_min": 25,
    "gate_min_age_minutes": 10,        # 到这个年龄才用门槛判定
    "staging_max_age_minutes": 90,     # 观察区里超过这个年龄还没过门槛就扔掉
    "staging_max_size": 3000,          # 观察区上限（只存极简字段，开销很小）

    # ---- 观察 ----
    "sample_schedule": [               # (年龄上限分钟, 采样间隔分钟)
        (30, 10),
        (120, 20),
        (360, 60),
    ],
    # 观察窗口：发布后多少分钟停止观察（按**发布时间**算，不按进池时间）
    # 用发布时间做基准：它是 YouTube 给的、稳定的；
    # 「发现时刻」取决于搜索运气，不稳定，会导致同样的视频被观察不同时长。
    "observe_minutes": 360,
    "max_pool_size": 400,              # 候选池上限，超了就扔涨得最慢的
    "drop_if_views_below": 300,        # 2 小时后仍低于这个播放 → 扔
    "drop_if_stale_checks": 3,         # 连续 N 次检查几乎不涨 → 扔

    # ---- 配额 ----
    "daily_budget_per_key": 9500,      # 单把钥匙每天最多用多少点（留 500 点余量）
    "max_keys": 10,                    # 最多认几把钥匙（YOUTUBE_API_KEY, _2 .. _10）

    # ---- 运行 ----
    "loop_sleep_seconds": 20,          # 主循环空转间隔
    "slice_lookahead_hours": 2,        # 用接下来几小时的历史密度估切片宽度
}


# ---------------------------------------------------------------------------
# 多钥匙管理
# ---------------------------------------------------------------------------

class KeyPool:
    """管理多把 API Key，记录各自当日用量，轮转分配。

    注意：Google 的配额按「项目」算，每天太平洋时间 0 点重置。
    北京时间的重置点是 15:00 或 16:00（取决于夏令时），所以本类用
    16:00 BJT 作为记账日界，宁可晚换日也不要提前归零。
    """

    # 记账日界：北京时间 16:00（太平洋时间 0 点）
    RESET_HOUR_BJT = 16

    def __init__(self, state):
        self.keys = self._load_keys()
        if not self.keys:
            print("[×] 没有找到任何 API Key（检查 .env 的 YOUTUBE_API_KEY）")
            sys.exit(1)
        self.today = self._accounting_day()
        usage = state.setdefault("key_usage", {})
        if usage.get("_day") != self.today:
            usage.clear()
            usage["_day"] = self.today
        self.usage = usage
        self.state = state
        self.clients = {}
        self._rr = state.setdefault("key_rr", 0)
        # 今日被服务端拒绝（配额耗尽等）的钥匙，本轮不再尝试
        self.dead = set(usage.get("_dead") or [])
        print(f"[√] 载入 {len(self.keys)} 把钥匙，记账日 {self.today}，"
              f"今日已用 {sum(v for k, v in usage.items() if not k.startswith('_')):,} 点"
              + (f"，已耗尽 {len(self.dead)} 把" if self.dead else ""))

    @classmethod
    def _accounting_day(cls):
        now = datetime.now(BJT)
        if now.hour < cls.RESET_HOUR_BJT:
            now = now - timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    @staticmethod
    def _load_keys():
        keys = []
        # 第一个
        k = os.getenv("YOUTUBE_API_KEY", "").strip()
        if k and not k.startswith("YOUR_"):
            keys.append(k)
        # 后面的（_2 .. _max_keys）
        for i in range(2, CFG["max_keys"] + 1):
            k = os.getenv(f"YOUTUBE_API_KEY_{i}", "").strip()
            if k and not k.startswith("YOUR_") and k not in keys:
                keys.append(k)
        # SEARCH 那把作为补充
        k = os.getenv("YOUTUBE_API_KEY_SEARCH", "").strip()
        if k and not k.startswith("YOUR_") and k not in keys:
            keys.append(k)
        return keys

    def _label(self, key):
        return f"{key[:8]}…{key[-4:]}"

    def used(self, key):
        return int(self.usage.get(key, 0))

    def remaining(self, key):
        return max(0, CFG["daily_budget_per_key"] - self.used(key))

    def charge(self, key, cost):
        self.usage[key] = self.used(key) + cost

    def pick(self, cost, for_observation=False):
        """挑一把还有额度、且今日未被服务端拒绝的钥匙。"""
        n = len(self.keys)
        for i in range(n):
            idx = (self._rr + i) % n
            key = self.keys[idx]
            if key in self.dead:
                continue
            if self.used(key) + cost <= CFG["daily_budget_per_key"]:
                self._rr = (idx + 1) % n
                self.state["key_rr"] = self._rr
                if key not in self.clients:
                    self.clients[key] = YouTubeAPI(key)
                return self.clients[key], key
        return None, None

    def mark_quota_error(self, key):
        """收到配额相关错误。

        注意：Google 有时对**突发频率**也返回 quota/429，那是一次性的，
        不代表当日额度用完。所以先记一次「软失败」，连续两次才停用，
        避免误杀一把还有额度的钥匙。
        """
        fails = self.state.setdefault("key_quota_fails", {})
        fails[key] = int(fails.get(key, 0)) + 1
        if fails[key] >= 2:
            self.retire(key)
        else:
            print(f"     [!] 钥匙 {key[:8]}… 报配额错误（第 {fails[key]} 次），"
                  f"再撞一次就停用")
        return fails[key] >= 2

    def retire(self, key):
        """停用一把钥匙（今日不再使用）。"""
        self.dead.add(key)
        usage = self.state.setdefault("key_usage", {})
        usage["_dead"] = sorted(self.dead)
        usage[key] = CFG["daily_budget_per_key"]      # 记账上标满，避免再被挑中
        print(f"     [!] 钥匙 {key[:8]}… 已停用（可用 {len(self.alive())}/{len(self.keys)} 把）")

    def alive(self):
        return [k for k in self.keys if k not in self.dead]

    def total_remaining(self):
        return sum(self.remaining(k) for k in self.keys)

    def snapshot(self):
        return [
            {"key": self._label(k), "used": self.used(k), "remaining": self.remaining(k)}
            for k in self.keys
        ]


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

DEFAULT_STATE = {
    "cursor_utc": None,          # 上次扫到哪个时刻
    "staging": {},               # 观察区：已登记但还没够年龄判定的视频（只存元信息）
    "candidates": {},            # video_id -> 候选记录（过了门槛，值得采样）
    "notified": {},              # video_id -> 时间
    "rejected": {},              # video_id -> 时间
    "channels": {},              # channel_id -> {subs, fetched_at}
    "search_log": [],            # 最近若干次搜索的结果数（用于估算密度）
    "hourly_density": {},        # 小时 -> {results, span, n}，供次日同时段预测
    "reject_reasons": {},        # 拒绝原因统计，用于调参
    "key_usage": {},
    "key_quota_fails": {},
    "stats": {},
}


def load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            if isinstance(s, dict):
                for k, v in DEFAULT_STATE.items():
                    s.setdefault(k, v.copy() if isinstance(v, (dict, list)) else v)
                return s
        except (json.JSONDecodeError, OSError) as e:
            print(f"[!] 状态文件损坏，重新开始: {e}")
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# 时间工具
# ---------------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def iso_z(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def bjt_str(dt):
    return dt.astimezone(BJT).strftime("%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 切片宽度估算
# ---------------------------------------------------------------------------

def estimate_density(state, window=12):
    """从最近的搜索记录估上传密度（条/分钟）。"""
    log = state.get("search_log") or []
    log = log[-window:]
    if not log:
        return 1.9          # 没有数据时用实测均值（约 19 条/10 分钟）
    total_results, total_span = 0, 0.0
    for e in log:
        total_results += e.get("results", 0)
        total_span += max(0.1, e.get("span_minutes", 0))
    if total_span <= 0:
        return 1.9
    return max(0.05, total_results / total_span)


def record_hourly(state, when, results, span_minutes):
    """按小时累计「结果数 / 时间跨度」，供次日同时段预测。"""
    hourly = state.setdefault("hourly_density", {})
    key = str(when.astimezone(BJT).hour)
    e = hourly.setdefault(key, {"results": 0, "span": 0.0, "n": 0})
    e["results"] += results
    e["span"] += span_minutes
    e["n"] += 1


def hourly_density(state, when):
    """取该小时历史密度；没有样本返回 None。"""
    e = (state.get("hourly_density") or {}).get(str(when.astimezone(BJT).hour))
    if not e or e.get("span", 0) <= 0:
        return None
    return e["results"] / e["span"]


def next_slice_seconds(state, when=None):
    """按密度决定下一个时间片多宽，并留安全余量。

    优先用「该小时的历史密度」（次日同时段更准），退回到最近窗口的滚动密度。
    """
    when = when or now_utc()
    density = hourly_density(state, when)
    source = "同小时历史"
    if density is None:
        density = estimate_density(state)
        source = "滚动窗口"

    target = CFG["target_results_per_search"]
    ideal = (target / density) * 60                   # 秒
    sec = int(max(CFG["min_slice_seconds"], min(CFG["max_slice_seconds"], ideal)))
    return sec, density, source


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

SEARCH_QUERY = "Minecraft"


class QuotaExhausted(Exception):
    """某把钥匙的当日配额用完了。"""


def _is_quota_error(msg):
    m = (msg or "").lower()
    return ("quota" in m or "429" in m or "ratelimitexceeded" in m
            or "dailylimitexceeded" in m)


def do_search(pool, state, after, before):
    """搜一个时间窗。配额耗尽会自动换下一把钥匙重试。

    返回 (items, key, cost, err, quota_exhausted)
        items=None + quota_exhausted=True  → 所有钥匙都没配额了，本轮放弃（**不推进游标**）
        items=None + err                   → 其它错误，本轮放弃（**不推进游标**，下轮重试同一片）
    """
    tried = set()
    last_err = None

    for _ in range(len(pool.keys)):
        client, key = pool.pick(cost=100)
        if client is None:
            return None, None, 0, "所有钥匙的配额都用完了", True
        if key in tried:
            break
        tried.add(key)

        try:
            resp = client.get("search", {
                "part": "snippet",
                "q": SEARCH_QUERY,
                "type": "video",
                "videoDuration": "short",
                "order": "date",
                "publishedAfter": iso_z(after),
                "publishedBefore": iso_z(before),
                "maxResults": 50,
            }, cost=100)
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:160]}"
            last_err = msg
            if _is_quota_error(msg):
                retired = pool.mark_quota_error(key)   # 连续两次才停用，防误杀
                if retired:
                    continue
                # 软失败：也换一把试，但别把这把判死
                continue
            if "429" in msg or "403" in msg or "500" in msg or "503" in msg:
                continue                     # 临时故障，也换一把试试
            return None, None, 0, msg, False

        pool.charge(key, 100)
        items = []
        for it in resp.get("items", []):
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            sn = it.get("snippet") or {}
            items.append({
                "video_id": vid,
                "title": sn.get("title", ""),
                "channel_id": sn.get("channelId", ""),
                "channel_name": sn.get("channelTitle", ""),
                "published_at": sn.get("publishedAt", ""),
            })
        return items, key, 100, None, False

    return None, None, 0, last_err or "没有可用钥匙", _is_quota_error(last_err or "")


def fetch_details(pool, video_ids):
    """批量取详情。返回 {vid: {...}}。"""
    out = {}
    ids = list(video_ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        client, key = pool.pick(cost=1, for_observation=True)
        if client is None:
            break
        try:
            resp = client.get("videos", {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(chunk),
            })
        except Exception as e:
            print(f"    [!] 取详情失败: {type(e).__name__}: {str(e)[:90]}")
            continue
        pool.charge(key, 1)
        for it in resp.get("items", []):
            st = it.get("statistics") or {}
            sn = it.get("snippet") or {}
            cd = it.get("contentDetails") or {}
            out[it["id"]] = {
                "views": _int(st.get("viewCount")),
                "likes": _int(st.get("likeCount")),
                "duration": parse_duration(cd.get("duration", "")),
                "title": sn.get("title", ""),
                "published_at": sn.get("publishedAt", ""),
                # 有字幕 ≈ 大概率有人声（口播/解说）；纯视觉内容通常没有字幕
                "has_captions": (cd.get("caption") == "true"),
                "made_for_kids": bool(sn.get("madeForKids")),
                "category_id": str(sn.get("categoryId", "") or ""),
                "description": (sn.get("description") or ""),
                "tags": sn.get("tags") or [],
                "default_audio_language": sn.get("defaultAudioLanguage", ""),
            }
    return out


def fetch_channel_subs(pool, state, channel_ids):
    """批量取订阅数，缓存 7 天。"""
    cache = state.setdefault("channels", {})
    result, missing = {}, []
    now = now_utc()
    for cid in set(channel_ids):
        if not cid:
            continue
        e = cache.get(cid) or {}
        fetched = parse_iso(e.get("fetched_at"))
        if fetched and (now - fetched).total_seconds() < 7 * 86400:
            result[cid] = int(e.get("subs") or 0)
        else:
            missing.append(cid)

    for i in range(0, len(missing), 50):
        chunk = missing[i:i + 50]
        client, key = pool.pick(cost=1)
        if client is None:
            break
        try:
            resp = client.get("channels", {"part": "statistics", "id": ",".join(chunk)})
        except Exception:
            continue
        pool.charge(key, 1)
        for it in resp.get("items", []):
            subs = _int((it.get("statistics") or {}).get("subscriberCount"))
            cache[it["id"]] = {"subs": subs, "fetched_at": iso_z(now)}
            result[it["id"]] = subs
    return result


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_duration(s):
    if not s:
        return 0
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0
    d, h, mi, sec = (int(g or 0) for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------

def age_minutes(published_at):
    pub = parse_iso(published_at)
    if not pub:
        return 10 ** 9
    return (now_utc() - pub).total_seconds() / 60.0


def title_ok(title, cfg=None):
    """标题是否真的在讲 Minecraft —— 按「完整词 + 出处」判断。

    关键区分：**正文提到** vs **仅 hashtag 提到**。

    实测发现 `#minecraft` 是被滥用最严重的标签：
    "Daddy Pig Drinks George's Potion #peppapig #minecraft #animation"
    这种小猪佩奇视频挂 #minecraft 纯粹为了蹭流量。

    规则：
      - 正文（去掉 hashtag）里出现 minecraft 系列词 → 通过
      - 只在 hashtag 里出现 → 至少 2 个 minecraft 系 hashtag 才算数
    """
    cfg = cfg or CFG
    raw = (title or "").lower()
    if not raw:
        return False

    words = [k.lower() for k in cfg["title_keywords"]]
    strong = [w for w in words if "minecraft" in w or w in ("maizen", "verity")]

    hashtags = re.findall(r"#[\w\u4e00-\u9fff]+", raw)
    body = re.sub(r"#[\w\u4e00-\u9fff]+", " ", raw)
    body_words = set(re.findall(r"[\w\u4e00-\u9fff]+", body))

    for k in strong:
        kk = k.lstrip("#")
        if kk in body_words:
            return True
        if " " in kk and kk in body:
            return True

    mc_tags = [h for h in hashtags if "minecraft" in h or h in ("#maizen", "#verity")]
    return len(mc_tags) >= 2


def weak_title_ok(d, cfg=None):
    """标题只有 1 个 minecraft hashtag 时的补救判定。

    针对「标题是外语、看不出 Minecraft、但内容确实是」的情况
    （实测：阿拉伯语/俄语的 Minecraft 短视频，标题正文是母语）。
    额外要求：分类是 Gaming + 官方 tags 里有 minecraft 系标签。
    小猪佩奇那种分类是 Animation、tags 只有 peppa，会被拦住。
    """
    cfg = cfg or CFG
    title = (d.get("title") or "").lower()
    hashtags = re.findall(r"#[\w\u4e00-\u9fff]+", title)
    if not any("minecraft" in h or h in ("#maizen", "#verity") for h in hashtags):
        return False
    if str(d.get("category_id") or "") != "20":       # 必须是 Gaming
        return False
    tags = " ".join(str(t).lower() for t in (d.get("tags") or []))
    return "minecraft" in tags or "майнкрафт" in tags or "ماينكرافت" in tags


def sample_interval_minutes(age):
    for upper, interval in CFG["sample_schedule"]:
        if age <= upper:
            return interval
    return CFG["sample_schedule"][-1][1]


def sample_dt(sample):
    """取采样点的时间。兼容字符串（旧数据）和 datetime（内存中的新数据）。"""
    t = sample.get("t")
    if isinstance(t, datetime):
        return t
    parsed = parse_iso(t)
    return parsed if parsed else now_utc()


def rate_between(a, b):
    """两个采样点之间的涨幅（播放/分钟）。"""
    dt = (sample_dt(b) - sample_dt(a)).total_seconds() / 60.0
    if dt <= 0:
        return 0.0
    return (int(b["views"]) - int(a["views"])) / dt


def due_for_sample(cand):
    last = parse_iso(cand.get("last_checked_at"))
    if last is None:
        return True
    age = age_minutes(cand.get("published_at"))
    interval = sample_interval_minutes(age)
    return (now_utc() - last).total_seconds() >= interval * 60


def growth(cand, n=2):
    s = cand.get("samples") or []
    if len(s) < n:
        return None
    return int(s[-1]["views"]) - int(s[-n - 1]["views"])


def is_accelerating(cand):
    """后段涨速 > 前段涨速 × 1.5 视为加速。"""
    s = cand.get("samples") or []
    if len(s) < 4:
        return False, None
    half = len(s) // 2
    early = rate_between(s[0], s[half])
    late = rate_between(s[half], s[-1])
    if early <= 0:
        return False, (early, late)
    return late > early * 1.5, (early, late)


def like_rate(cand):
    """点赞率 = 点赞 / 播放。返回 (率, 点赞数, 播放数)；数据不足返回 (None, 0, 0)。"""
    samples = cand.get("samples") or []
    if not samples:
        return None, 0, 0
    last = samples[-1]
    views = int(last.get("views") or 0)
    likes = int(last.get("likes") or 0)
    if views < 50:
        return None, likes, views          # 播放太少，比率没意义
    return likes / views, likes, views


def should_drop(cand):
    """淘汰：过期 / 没人气 / 涨不动。判定标准按档位分。"""
    age = age_minutes(cand.get("published_at"))
    views = int((cand.get("samples") or [{"views": 0}])[-1]["views"])
    stale = int(cand.get("stale_checks") or 0)
    tier = cand.get("tier") or "T3"

    # 观察窗口结束（按发布时间算，不用进池时间）—— 过了就停止采样
    if age > CFG["observe_minutes"]:
        return f"观察期满({CFG['observe_minutes']//60}h)"

    # 分档的「无望」门槛：大号的绝对量要求更高
    hopeless_views = {"T1": 500, "T2": 800, "T3": 2_000,
                      "T4": 5_000, "T5": 15_000, "T6": 50_000}[tier]
    if age > 120 and views < hopeless_views:
        return f"无望({views}播放<{hopeless_views})"

    # 分档的「停涨」门槛
    stale_growth = {"T1": 20, "T2": 40, "T3": 80,
                    "T4": 200, "T5": 500, "T6": 1_500}[tier]
    if stale >= CFG["drop_if_stale_checks"]:
        g = last_growth(cand)
        if g is None or g < stale_growth:
            return f"停涨({stale}轮)"
    return None


# ---------------------------------------------------------------------------
# 单个周期
# ---------------------------------------------------------------------------

def cycle(pool, state, dry_run=False):
    started = time.time()
    stats = {"searched": 0, "new": 0, "gated_in": 0, "sampled": 0, "dropped": 0}

    # ① 采集：往前推进切片
    cursor = parse_iso(state.get("cursor_utc"))
    now = now_utc()
    if cursor is None or cursor > now:
        cursor = now - timedelta(minutes=30)

    gap = (now - cursor).total_seconds()
    if gap >= CFG["min_slice_seconds"]:
        sec, density, source = next_slice_seconds(state, cursor)

        # 关键：切片不能超过「距上次搜索实际过了多久」。
        # 否则每 5 分钟触发一次、却每次回看 21 分钟 —— 四次里三次在重复买同一批视频。
        uncapped = sec
        sec = int(min(sec, gap))
        if sec < uncapped:
            print(f"  [采集] 切片由 {uncapped//60}分 收窄到 {sec//60}分{sec%60}秒"
                  f"（对齐触发间隔，避免重叠）")

        before = min(now, cursor + timedelta(seconds=sec))
        print(f"  [采集] {bjt_str(cursor)} → {bjt_str(before)}  "
              f"({sec//60}分{sec%60}秒, 密度 {density:.2f} 条/分, 来源:{source})")

        if dry_run:
            state["cursor_utc"] = iso_z(before)
            stats["searched"] += 1
        else:
            items, key, cost, err, quota_dead = do_search(pool, state, cursor, before)
            if err:
                print(f"  [采集] 失败: {err}")
                if quota_dead:
                    print(f"  [采集] 所有钥匙配额耗尽，本轮暂停采集（**游标不推进**，"
                          f"下轮或配额重置后继续）")
                    stats["quota_blocked"] = 1
                else:
                    print(f"  [采集] 本轮跳过（**游标不推进**，下轮重试同一片，不会丢数据）")
            else:
                # 只有真正搜到了才推进游标 —— 保证不漏时间片
                state["cursor_utc"] = iso_z(before)
                stats["searched"] += 1
                state.setdefault("search_log", []).append({
                    "at": iso_z(now),
                    "results": len(items),
                    "span_minutes": sec / 60.0,
                })
                state["search_log"] = state["search_log"][-50:]
                record_hourly(state, cursor, len(items), sec / 60.0)
                print(f"  [采集] 拿到 {len(items)} 条  (key {key[:8]}…)")
                if len(items) >= 48:
                    print(f"  [采集] ⚠ 打满上限，下一片自动收窄")
                _ingest(pool, state, items, stats)
    else:
        print(f"  [采集] 距下一片还差 {int((CFG['min_slice_seconds']-gap)/60)} 分"
              f"（游标 {bjt_str(cursor)}）")

    # ①·补 判定观察区里够年龄的视频（真正的门槛在这里生效）
    _evaluate_staging(pool, state, stats)

    # ② 观察：给到期的候选采样
    _sample_due(pool, state, stats)

    # ③ 清理
    _prune(state, stats)

    state["stats"]["last_cycle"] = {
        "at": iso_z(now),
        "elapsed": round(time.time() - started, 1),
        **stats,
    }
    return stats


def content_ok(d, cfg=None):
    """内容形态是否合格。返回 (是否通过, 拒绝原因)。

    拦三类：
      - 跑题：分类不在白名单（实测跑题的集中在 Travel/People&Blogs）
      - 口播/讲解：描述过长（实测纯视觉梗 0~300 字，口播型 700~4300 字）
      - 直播切片/转载：描述或标题里带直播平台标记
    """
    cfg = cfg or CFG
    cat = str(d.get("category_id") or "")
    if cat and cfg["allow_categories"] and cat not in cfg["allow_categories"]:
        return False, "分类不符"

    desc = (d.get("description") or "").lower()
    if len(desc) > cfg["max_description_chars"]:
        return False, "描述过长(疑口播)"

    title = (d.get("title") or "").lower()
    for m in cfg["banned_desc_markers"]:
        if m in desc:
            return False, f"直播/切片({m})"
    for m in cfg["banned_title_markers"]:
        if m in title:
            return False, f"标题含({m})"
    return True, None


def _ingest(pool, state, items, stats):
    """把搜索结果的**元信息**登记进「观察区」。

    注意：这一步**不发详情请求、不做门槛判定**，因为刚发布的视频播放量必然很低，
    此刻判定必然误放。只登记元信息（几乎零成本），等它长到
    gate_min_age_minutes 之后再由 _evaluate_staging() 统一判定。
    """
    staging = state.setdefault("staging", {})
    pool_c = state["candidates"]
    now_iso = iso_z(now_utc())
    added = 0

    for it in items:
        vid = it["video_id"]
        if (vid in pool_c or vid in staging
                or vid in state["notified"] or vid in state.get("rejected", {})):
            continue
        staging[vid] = {
            "video_id": vid,
            "title": it.get("title", ""),
            "channel_id": it.get("channel_id", ""),
            "channel_name": it.get("channel_name", ""),
            "published_at": it.get("published_at", ""),
            "first_seen_at": now_iso,
        }
        added += 1

    state["staging"] = staging
    stats["staged"] = added
    print(f"  [登记] 观察区新增 {added} 条（区内存 {len(staging)} 条，待判定）")


def _evaluate_staging(pool, state, stats):
    """把观察区里「够年龄」的视频按门槛判定，通过才进候选池。

    这是整套漏斗的真正闸门：决定哪些视频值得花观察配额去跟踪。
    """
    staging = state.setdefault("staging", {})
    pool_c = state["candidates"]
    if not staging:
        return

    now = now_utc()
    # ① 先淘汰观察区里的过期项（从没长起来的）
    expired = 0
    for vid, e in list(staging.items()):
        age = age_minutes(e.get("published_at"))
        if age > CFG["staging_max_age_minutes"]:
            del staging[vid]
            state["rejected"][vid] = iso_z(now)
            expired += 1
    if expired:
        print(f"  [登记] 观察区清理 {expired} 条（超过 {CFG['staging_max_age_minutes']} 分钟仍未判定进池）")

    # ② 挑出够年龄的，批量取详情
    due = [vid for vid, e in staging.items()
           if age_minutes(e.get("published_at")) >= CFG["gate_min_age_minutes"]]
    if not due:
        return

    # 观察区太大时只判最老的一批，避免一次请求太多
    due.sort(key=lambda v: staging[v].get("published_at") or "")
    due = due[:500]

    details = fetch_details(pool, due)
    subs_map = fetch_channel_subs(pool, state, [
        (details.get(v, {}) or staging[v]).get("channel_id") or staging[v].get("channel_id", "")
        for v in due])

    promoted = gated_views = gated_dur = gated_title = gated_content = 0
    gated_quota = gated_minviews = 0
    for vid in due:
        e = staging.get(vid)
        d = details.get(vid)
        if e is None:
            continue
        if d is None:                      # 取不到（被删/私享）→ 扔掉
            del staging[vid]
            state["rejected"][vid] = iso_z(now)
            continue

        title = d.get("title") or e.get("title", "")
        pub = d.get("published_at") or e.get("published_at", "")
        age = age_minutes(pub)
        views = int(d["views"])
        vpm = views / age if age > 0.5 else 0
        subs = subs_map.get(d.get("channel_id") or e.get("channel_id", ""), 0)

        def drop(reason_counter):
            del staging[vid]
            state["rejected"][vid] = iso_z(now)
            return reason_counter + 1

        tier = tier_of(subs)
        rule = TIER_RULES[tier]

        # 判定顺序：先做最便宜的排除，再做需要计算的
        if not (0 < d["duration"] <= CFG["max_duration"]):
            gated_dur = drop(gated_dur)
            continue
        # 标题判定：主规则从严；外语标题的合法内容用弱规则补救
        if not title_ok(title) and not weak_title_ok(d):
            gated_title = drop(gated_title)
            continue
        ok, why = content_ok(d)
        if not ok:
            gated_content = drop(gated_content)
            state.setdefault("reject_reasons", {})
            state["reject_reasons"][why] = state["reject_reasons"].get(why, 0) + 1
            continue
        # 按档位的进池门槛：播放速度 + 最小绝对播放量
        if vpm < rule["gate_vpm"]:
            gated_views = drop(gated_views)
            continue
        if views < rule["gate_min_views"]:
            gated_minviews = drop(gated_minviews)
            continue

        # 该档位名额满了？按推广分决定谁留下
        tier_count = sum(1 for c in pool_c.values() if c.get("tier") == tier)
        if tier_count >= TIER_QUOTA[tier]:
            weakest = min(
                (c for c in pool_c.values() if c.get("tier") == tier),
                key=lambda c: c.get("promo_score", 0.0), default=None)
            my_score = promotion_score(views, vpm, subs, tier, d.get("likes", 0))
            if weakest is None or my_score <= weakest.get("promo_score", 0.0):
                gated_quota = drop(gated_quota)
                continue
            # 顶掉最弱的
            del pool_c[weakest["video_id"]]
            state["rejected"][weakest["video_id"]] = iso_z(now)
            gated_quota += 1

        record_tier_sample(state, tier, vpm)

        # 通过 → 进候选池。档位在此**锁定**，后续不再重算
        # （频道订阅会变，不锁的话一个视频可能被两套规则判过，产生重复通知）
        pool_c[vid] = {
            "video_id": vid,
            "title": title,
            "channel_id": e.get("channel_id", ""),
            "channel_name": e.get("channel_name", ""),
            "channel_subs": subs,
            "tier": tier,
            "tier_locked_at": iso_z(now),
            "published_at": pub,
            "first_seen_at": e.get("first_seen_at", iso_z(now)),
            "last_checked_at": iso_z(now),
            "samples": [{"t": iso_z(now), "views": views, "likes": d.get("likes", 0)}],
            "stale_checks": 0,
            "notified": False,
            "gate_vpm": round(vpm, 1),
            "promo_score": round(promotion_score(views, vpm, subs, tier, d.get("likes", 0)), 3),
        }
        del staging[vid]
        promoted += 1
        stats["gated_in"] = stats.get("gated_in", 0) + 1

    if promoted or gated_views or gated_dur or gated_title or gated_content or gated_quota or gated_minviews:
        print(f"  [判定] 进池 {promoted} | 播放太慢 {gated_views} | 绝对量不足 {gated_minviews} | "
              f"内容形态 {gated_content} | 非Shorts {gated_dur} | 标题不符 {gated_title} | "
              f"档位名额挤掉 {gated_quota} | 待判定 {len(staging)}")


def _ingest_legacy(pool, state, items, stats):
    """（已弃用）旧的一次性入池逻辑，保留供参考。"""


def _sample_due(pool, state, stats):
    """给到期的候选采样。"""
    pool_c = state["candidates"]
    due = [vid for vid, c in pool_c.items() if due_for_sample(c)]
    if not due:
        print(f"  [观察] 池内 {len(pool_c)} 条，本轮无需采样")
        return

    print(f"  [观察] 采样 {len(due)} 条")
    details = fetch_details(pool, due)
    for vid in due:
        c = pool_c.get(vid)
        d = details.get(vid)
        if c is None or d is None:
            continue
        prev = int(c["samples"][-1]["views"]) if c["samples"] else 0
        c["samples"].append({
            "t": iso_z(now_utc()),
            "views": d["views"],
            # 点赞是免费的信号（同一次 videos.list 就返回了），
            # 用来算点赞率，区分「算法推的」和「观众真的喜欢」
            "likes": d.get("likes", 0),
        })
        if len(c["samples"]) > 24:
            del c["samples"][:-24]
        c["last_checked_at"] = iso_z(now_utc())
        c["stale_checks"] = (int(c.get("stale_checks") or 0) + 1) if d["views"] - prev < 5 else 0
        stats["sampled"] += 1

        # 按档位判定预警（不再是统一的 views/subs >= 2）
        if not c.get("notified"):
            fire, high, detail = alert_check(c, state)
            if fire:
                c["notified"] = True
                state["notified"][vid] = iso_z(now_utc())
                t = state.setdefault("tier_stats", {}).setdefault(
                    detail["tier"], {"vpm_samples": [], "alerted": 0, "promoted": 0})
                t["alerted"] = int(t.get("alerted", 0)) + 1
                rates = detail.get("rates") or (0, 0)
                tag = "🔥🔥 高优先级" if high else "🔥"
                print(f"  [{tag}] {c['title'][:46]}")
                print(f"           {detail['tier']} {TIER_LABEL[detail['tier']]} | "
                      f"{detail['views']:,} 播放 / {detail['subs']:,} 订阅 "
                      f"= {detail.get('ratio', 0):.2f}x | "
                      f"门槛 {detail['alert_views']:,} | "
                      f"涨速 {rates[0]:.0f}→{rates[1]:.0f}/分 | "
                      f"发布后 {age_minutes(c['published_at']):.0f} 分")
                print(f"           https://www.youtube.com/watch?v={vid}")
            elif detail.get("why") and stats.get("verbose"):
                print(f"  [·] {c['title'][:38]} 未预警: {detail['why']}")


def _prune(state, stats):
    """淘汰与清理。"""
    pool_c = state["candidates"]
    for vid, c in list(pool_c.items()):
        reason = should_drop(c)
        if reason:
            del pool_c[vid]
            state["rejected"][vid] = iso_z(now_utc())
            stats["dropped"] += 1
            print(f"  [淘汰] {reason}  {c['title'][:40]}")

    # 池子上限：超了就扔涨得最慢的（按最近涨速排）
    max_pool = CFG["max_pool_size"]
    if len(pool_c) > max_pool:
        def recent_rate(c):
            s = c.get("samples") or []
            if len(s) < 2:
                return 0.0
            return rate_between(s[-2], s[-1])
        ranked = sorted(pool_c.items(), key=lambda kv: recent_rate(kv[1]))
        overflow = len(pool_c) - max_pool
        for vid, c in ranked[:overflow]:
            del pool_c[vid]
            state["rejected"][vid] = iso_z(now_utc())
            stats["dropped"] += 1
        print(f"  [淘汰] 池满({max_pool})，按涨速扔出 {overflow} 条")

    # 名单保留期
    cutoff_n = iso_z(now_utc() - timedelta(days=7))
    state["notified"] = {k: v for k, v in state["notified"].items() if v >= cutoff_n}
    cutoff_r = iso_z(now_utc() - timedelta(days=2))
    state["rejected"] = {k: v for k, v in state["rejected"].items() if v >= cutoff_r}


# ---------------------------------------------------------------------------
# 报告 / 主循环
# ---------------------------------------------------------------------------

def report(state, pool=None):
    c = state["candidates"]
    print(f"\n{'═'*92}")
    print(f"当前状态   北京时间 {bjt_str(now_utc())}")
    print(f"{'═'*92}")
    print(f"  游标（已扫到）: {bjt_str(parse_iso(state.get('cursor_utc')) or now_utc() - timedelta(days=99))}")
    print(f"  观察区        : {len(state.get('staging') or {})} 条（待判定）")
    print(f"  候选池        : {len(c)} 条")
    print(f"  已预警        : {len(state['notified'])} 条")
    print(f"  已淘汰        : {len(state['rejected'])} 条")
    print(f"  频道缓存      : {len(state.get('channels', {}))} 个")

    if pool:
        print(f"\n  钥匙用量:")
        for s in pool.snapshot():
            pct = s["used"] / CFG["daily_budget_per_key"] * 100
            print(f"    {s['key']}  {s['used']:>6,} / {CFG['daily_budget_per_key']:,} "
                  f"({pct:>5.1f}%)")
        print(f"    剩余合计: {pool.total_remaining():,} 点")

    if c:
        print(f"\n  候选池明细（按播放/订阅比排序）:")
        rows = []
        for v in c.values():
            views = int(v["samples"][-1]["views"]) if v["samples"] else 0
            subs = int(v.get("channel_subs") or 0)
            rows.append((views / subs if subs else 0, v, views, subs))
        rows.sort(key=lambda r: -r[0])
        hdr = "    {:>10} {:>9} {:>8} {:>6} {:>4}  {}"
        print(hdr.format("播放", "订阅", "播放/订阅", "检查数", "发布后", "标题"))
        for ratio, v, views, subs in rows[:30]:
            print(hdr.format(
                f"{views:,}", f"{subs:,}", f"{ratio:.2f}x",
                len(v["samples"]),
                f"{age_minutes(v['published_at']):.0f}分",
                v["title"][:40]))
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印调度，不发请求")
    ap.add_argument("--once", action="store_true", help="只跑一个周期")
    ap.add_argument("--report", action="store_true", help="打印状态后退出")
    ap.add_argument("--cycles", type=int, default=0, help="跑 N 个周期后退出（0=无限）")
    args = ap.parse_args()

    state = load_state()
    pool = None if args.dry_run else KeyPool(state)

    if args.report:
        report(state, pool)
        return

    print(f"{'═'*92}")
    print(f"全天打捞守护进程启动   北京时间 {bjt_str(now_utc())}")
    print(f"{'═'*92}")
    print(f"  切片        : 自适应 {CFG['min_slice_seconds']//60}~{CFG['max_slice_seconds']//60} 分钟")
    print(f"  目标结果数  : {CFG['target_results_per_search']} 条/次搜索")
    print(f"  分层门槛    : " + " | ".join(
        f"{t} {TIER_LABEL[t]}: 播放/分≥{TIER_RULES[t]['gate_vpm']}, 预警≥{TIER_RULES[t]['alert_views']:,}"
        for t in TIERS[:3]))
    print(f"                " + " | ".join(
        f"{t} {TIER_LABEL[t]}: 播放/分≥{TIER_RULES[t]['gate_vpm']}, 预警≥{TIER_RULES[t]['alert_views']:,}"
        for t in TIERS[3:]))
    print(f"  内容过滤    : 描述≤{CFG['max_description_chars']}字 | 分类∈{CFG['allow_categories']}")
    print(f"  分档名额    : " + " ".join(f"{t}={TIER_QUOTA[t]}" for t in TIERS) +
          f"  (共 {sum(TIER_QUOTA.values())})")
    print(f"  采样节奏    : " + " / ".join(f"{a}分内每{b}分" for a, b in CFG["sample_schedule"]))
    print(f"  单把日上限  : {CFG['daily_budget_per_key']:,} 点")
    if pool:
        print(f"  钥匙数      : {len(pool.keys)}")
        print(f"  日可用总量  : {pool.total_remaining():,} 点")
    print()

    stop = {"flag": False}

    def handler(sig, frame):
        stop["flag"] = True
        print("\n[i] 收到停止信号，保存状态后退出 ...")

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    n = 0
    while not stop["flag"]:
        n += 1
        print(f"\n───── 周期 #{n}  {bjt_str(now_utc())} ─────")
        try:
            cycle(pool, state, dry_run=args.dry_run)
        except Exception as e:
            print(f"  [!] 周期异常: {type(e).__name__}: {str(e)[:200]}")

        save_state(state)
        if pool:
            print(f"  [配额] 剩余 {pool.total_remaining():,} 点 | "
                  f"池内 {len(state['candidates'])} 条")

        if args.once or (args.cycles and n >= args.cycles):
            break
        for _ in range(CFG["loop_sleep_seconds"]):
            if stop["flag"]:
                break
            time.sleep(1)

    save_state(state)
    report(state, pool)
    print("[√] 已退出并保存状态")


if __name__ == "__main__":
    main()
