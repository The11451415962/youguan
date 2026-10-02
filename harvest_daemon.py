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
# 可调参数
# ---------------------------------------------------------------------------
CFG = {
    # ---- 切片 ----
    "target_results_per_search": 38,   # 每次搜索期望拿到的结果数（留出余量，避免打满 50）
    "min_slice_seconds": 300,          # 切片最短 5 分钟（高峰期）
    "max_slice_seconds": 1800,         # 切片最长 30 分钟（低谷期）
    "spacing_seconds": 90,             # 相邻两次搜索之间的最小间隔（避免打爆）

    # ---- 门槛 ----
    "max_duration": 61,                # 只收 Shorts
    "title_keywords": ["minecraft", "maizen", "verity", "aphmau", "jj"],
    "gate_min_views_per_min": 20,      # 第一道门槛：播放/分钟
    "gate_min_age_minutes": 8,         # 太新的不判（计数还没更新）

    # ---- 观察 ----
    "sample_schedule": [               # (年龄上限分钟, 采样间隔分钟)
        (30, 10),
        (120, 20),
        (360, 60),
    ],
    "max_pool_size": 400,              # 候选池上限，超了就扔涨得最慢的
    "drop_if_views_below": 300,        # 2 小时后仍低于这个播放 → 扔
    "drop_if_stale_checks": 3,         # 连续 N 次检查几乎不涨 → 扔

    # ---- 配额 ----
    "daily_budget_per_key": 9500,      # 单把钥匙每天最多用多少点
    "reserve_for_observation": 0.45,   # 给观察层预留的配额比例

    # ---- 运行 ----
    "loop_sleep_seconds": 20,          # 主循环空转间隔
    "max_cycle_seconds": 240,          # 单个周期最长耗时，防止堆积
}


# ---------------------------------------------------------------------------
# 多钥匙管理
# ---------------------------------------------------------------------------

class KeyPool:
    """管理多把 API Key，记录各自当日用量，轮转分配。"""

    def __init__(self, state):
        self.keys = self._load_keys()
        if not self.keys:
            print("[×] 没有找到任何 API Key（检查 .env 的 YOUTUBE_API_KEY）")
            sys.exit(1)
        today = datetime.now(BJT).strftime("%Y-%m-%d")
        usage = state.setdefault("key_usage", {})
        if usage.get("_date") != today:
            usage.clear()
            usage["_date"] = today
        self.usage = usage
        self.state = state
        self.clients = {}
        self._rr = state.setdefault("key_rr", 0)
        print(f"[√] 载入 {len(self.keys)} 把钥匙，今日已用 "
              f"{sum(v for k, v in usage.items() if not k.startswith('_')):,} 点")

    @staticmethod
    def _load_keys():
        keys = []
        # 第一个
        k = os.getenv("YOUTUBE_API_KEY", "").strip()
        if k and not k.startswith("YOUR_"):
            keys.append(k)
        # 后面的（_2 .. _10）以及 SEARCH 备用
        for i in range(2, 11):
            k = os.getenv(f"YOUTUBE_API_KEY_{i}", "").strip()
            if k and not k.startswith("YOUR_") and k not in keys:
                keys.append(k)
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
        """挑一把还有额度的钥匙。返回 (client, key) 或 (None, None)。"""
        n = len(self.keys)
        limit_ratio = 1.0 if not for_observation else 1.0
        for i in range(n):
            idx = (self._rr + i) % n
            key = self.keys[idx]
            budget = CFG["daily_budget_per_key"] * limit_ratio
            if self.used(key) + cost <= budget:
                self._rr = (idx + 1) % n
                self.state["key_rr"] = self._rr
                if key not in self.clients:
                    self.clients[key] = YouTubeAPI(key)
                return self.clients[key], key
        return None, None

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
    "candidates": {},            # video_id -> 候选记录
    "notified": {},              # video_id -> 时间
    "rejected": {},              # video_id -> 时间
    "channels": {},              # channel_id -> {subs, fetched_at}
    "search_log": [],            # 最近若干次搜索的结果数（用于估算密度）
    "key_usage": {},
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
        return 1.5          # 没有数据时用保守默认值（约每 10 分钟 15 条）
    total_results, total_span = 0, 0.0
    for e in log:
        total_results += e.get("results", 0)
        total_span += max(0.1, e.get("span_minutes", 0))
    if total_span <= 0:
        return 1.5
    return max(0.05, total_results / total_span)


def next_slice_seconds(state):
    """按密度决定下一个时间片多宽，并留安全余量。"""
    density = estimate_density(state)                 # 条/分钟
    target = CFG["target_results_per_search"]
    ideal = (target / density) * 60                   # 秒
    sec = int(max(CFG["min_slice_seconds"], min(CFG["max_slice_seconds"], ideal)))
    return sec, density


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

SEARCH_QUERY = "Minecraft"


def do_search(pool, state, after, before):
    """搜一个时间窗。返回 (ids, 详细信息, 消耗)。"""
    client, key = pool.pick(cost=100)
    if client is None:
        return None, None, 0, "配额耗尽"

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
        # 搜索失败不扣配额（可能没成功）
        return None, None, 0, f"{type(e).__name__}: {str(e)[:120]}"

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
    return items, key, 100, None


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
    keys = (cfg or CFG)["title_keywords"]
    t = (title or "").lower()
    return any(k in t for k in keys)


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


def should_drop(cand):
    age = age_minutes(cand.get("published_at"))
    views = int((cand.get("samples") or [{"views": 0}])[-1]["views"])
    stale = int(cand.get("stale_checks") or 0)

    # 太老了，早就错过窗口
    if age > 480:
        return "过期(>8h)"
    # 很久了还没量
    if age > 120 and views < CFG["drop_if_views_below"]:
        return f"无望({views}播放)"
    # 连续多轮不涨
    if stale >= CFG["drop_if_stale_checks"]:
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
        sec, density = next_slice_seconds(state)
        before = min(now, cursor + timedelta(seconds=sec))
        print(f"  [采集] {bjt_str(cursor)} → {bjt_str(before)}  "
              f"({sec//60}分{sec%60}秒, 估密度 {density:.2f} 条/分)")

        if dry_run:
            state["cursor_utc"] = iso_z(before)
            stats["searched"] += 1
        else:
            items, key, cost, err = do_search(pool, state, cursor, before)
            if err:
                print(f"  [采集] 失败: {err}")
                if "配额" in err:
                    state["cursor_utc"] = iso_z(before)   # 跳过，别卡住
            else:
                state["cursor_utc"] = iso_z(before)
                stats["searched"] += 1
                state.setdefault("search_log", []).append({
                    "at": iso_z(now),
                    "results": len(items),
                    "span_minutes": sec / 60.0,
                })
                state["search_log"] = state["search_log"][-50:]
                print(f"  [采集] 拿到 {len(items)} 条  (key {key[:8]}…)")
                if len(items) >= 48:
                    print(f"  [采集] ⚠ 打满上限，下一片自动收窄")
                _ingest(pool, state, items, stats)
    else:
        print(f"  [采集] 距下一片还差 {int((CFG['min_slice_seconds']-gap)/60)} 分"
              f"（游标 {bjt_str(cursor)}）")

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


def _ingest(pool, state, items, stats):
    """把搜索结果做初筛后入池。"""
    pool_c = state["candidates"]
    fresh = []
    for it in items:
        vid = it["video_id"]
        if vid in pool_c or vid in state["notified"] or vid in state.get("rejected", {}):
            continue
        fresh.append(it)

    if not fresh:
        print("  [入池] 全部是已见过的")
        return

    details = fetch_details(pool, [f["video_id"] for f in fresh])
    subs_map = fetch_channel_subs(pool, state, [d.get("channel_id") for d in fresh])

    added = skipped_dur = skipped_title = skipped_views = 0
    for f in fresh:
        vid = f["video_id"]
        d = details.get(vid)
        if not d:
            continue
        if not (0 < d["duration"] <= CFG["max_duration"]):
            skipped_dur += 1
            continue
        title = d.get("title") or f.get("title", "")
        if not title_ok(title):
            skipped_title += 1
            state["rejected"][vid] = iso_z(now_utc())
            continue

        pub = d.get("published_at") or f.get("published_at", "")
        age = age_minutes(pub)
        views = d["views"]
        vpm = views / age if age > 0.5 else 0

        # 第一道门槛：太新不判；到年龄了还没量就扔
        if age >= CFG["gate_min_age_minutes"] and vpm < CFG["gate_min_views_per_min"]:
            skipped_views += 1
            state["rejected"][vid] = iso_z(now_utc())
            continue

        subs = subs_map.get(d.get("channel_id") or f.get("channel_id", ""), 0)
        now_iso = iso_z(now_utc())
        pool_c[vid] = {
            "video_id": vid,
            "title": title,
            "channel_id": f.get("channel_id", ""),
            "channel_name": f.get("channel_name", ""),
            "channel_subs": subs,
            "published_at": pub,
            "first_seen_at": now_iso,
            "last_checked_at": now_iso,
            "samples": [{"t": now_iso, "views": views}],
            "stale_checks": 0,
            "notified": False,
        }
        added += 1
        stats["gated_in"] += 1

    stats["new"] += added
    print(f"  [入池] 新增 {added} | 非Shorts {skipped_dur} | 标题不符 {skipped_title} | "
          f"无量 {skipped_views} | 池内共 {len(pool_c)}")


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
        c["samples"].append({"t": iso_z(now_utc()), "views": d["views"]})
        if len(c["samples"]) > 24:
            del c["samples"][:-24]
        c["last_checked_at"] = iso_z(now_utc())
        c["stale_checks"] = (int(c.get("stale_checks") or 0) + 1) if d["views"] - prev < 5 else 0
        stats["sampled"] += 1

        acc, rates = is_accelerating(c)
        if acc and c["channel_subs"]:
            ratio = d["views"] / c["channel_subs"]
            if ratio >= 2.0 and not c.get("notified"):
                c["notified"] = True
                state["notified"][vid] = iso_z(now_utc())
                print(f"  [🔥 预警] {c['title'][:46]}")
                print(f"            {d['views']:,} 播放 / {c['channel_subs']:,} 订阅 "
                      f"= {ratio:.1f}x | 涨速 {rates[0]:.0f}→{rates[1]:.0f}/分 | "
                      f"发布后 {age_minutes(c['published_at']):.0f} 分")


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
    print(f"  第一道门槛  : 播放/分钟 ≥ {CFG['gate_min_views_per_min']}")
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
