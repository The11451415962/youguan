#!/usr/bin/env python3
"""YouTube 侧采集：取「近期已起量」的 Minecraft Shorts，供套利窗口实测。

只做取数，不碰 viral_radar.py / radar_state.json。

用法：
    python yt_collect.py                 # 最近 24 小时，最多 50 条
    python yt_collect.py --hours 48 --max 80
    python yt_collect.py --out yt_sample.json

需要：.env 里有可用的 YOUTUBE_API_KEY，且本机能访问 YouTube（代理）。
配额：约 100（search）+ 1（videos.list 每 50 条）+ 1（channels.list）= 约 102 点。
"""

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent
OUT_DEFAULT = BASE_DIR / "yt_sample.json"
BJT = timezone(timedelta(hours=8))

API_ROOT = "https://www.googleapis.com/youtube/v3"


def _build_opener():
    """显式走本地代理。代理不可用时直接报错，不静默直连（直连必然超时）。

    注意：yt_monitor.build_youtube_client 里的 try 只包住了建对象、
    没包住真正的请求，代理失败会静默回退直连，然后超时。这里不复用那个函数。
    """
    host = os.getenv("CLASH_PROXY_HOST", "127.0.0.1")
    port = os.getenv("CLASH_PROXY_PORT", "7897")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if not host or not port:
        print("[i] 未配置代理，直连模式")
        return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

    proxy = f"http://{host}:{port}"
    # 先验证代理真的能出去，避免后面每一步都超时
    try:
        probe = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        probe.open("https://www.google.com/generate_204", timeout=15).read()
        print(f"[√] 代理可用: {proxy}")
    except Exception as e:
        print(f"[×] 代理 {proxy} 不通: {type(e).__name__}: {str(e)[:100]}")
        print("    请确认 Clash 已开启且端口正确（.env 里可设 CLASH_PROXY_PORT）")
        sys.exit(1)

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ctx),
    )


class YouTubeAPI:
    """直接打 YouTube Data API v3 的 REST 端点（走代理，避免 httplib2 的坑）。"""

    def __init__(self, key):
        self.key = key
        self.opener = _build_opener()
        self.quota_used = 0

    def get(self, endpoint, params, cost=1):
        params = dict(params)
        params["key"] = self.key
        url = f"{API_ROOT}/{endpoint}?" + urllib.parse.urlencode(params)
        try:
            with self.opener.open(urllib.request.Request(url, headers={"User-Agent": "yt-collect/1.0"}), timeout=30) as r:
                self.quota_used += cost
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            try:
                err = json.loads(body)["error"]
                reason = (err.get("errors") or [{}])[0].get("reason", "")
                raise RuntimeError(f"HTTP {e.code} [{reason}] {err.get('message', '')[:200]}") from None
            except (json.JSONDecodeError, KeyError):
                raise RuntimeError(f"HTTP {e.code}: {body[:200]}") from None


def build_client():
    key = os.getenv("YOUTUBE_API_KEY_SEARCH") or os.getenv("YOUTUBE_API_KEY", "")
    if not key or key.startswith("YOUR_"):
        print("[×] 没有可用的 YouTube API Key（检查 .env 的 YOUTUBE_API_KEY）")
        sys.exit(1)
    print(f"[i] 使用 Key: {key[:8]}...{key[-4:]}")
    return YouTubeAPI(key)


def search_recent(api, hours, max_results):
    """按播放量倒序取最近 N 小时的 Minecraft 短视频。"""
    after = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    print(f"[i] 搜索 publishedAfter={after}  (最近 {hours} 小时)")

    resp = api.get("search", {
        "part": "snippet",
        "q": "Minecraft",
        "type": "video",
        "videoDuration": "short",
        "order": "viewCount",
        "publishedAfter": after,
        "maxResults": min(50, max_results),
        "regionCode": "US",
    }, cost=100)

    ids = []
    for item in resp.get("items", []):
        vid = (item.get("id") or {}).get("videoId")
        if vid:
            ids.append(vid)
    print(f"[i] 搜索返回 {len(ids)} 条候选（累计配额 {api.quota_used}）")
    return ids


def fetch_details(api, ids):
    """批量取播放量 / 时长 / 标题。每 50 个 ID 一次请求 = 1 点。"""
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        resp = api.get("videos", {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(chunk),
        })
        for item in resp.get("items", []):
            vid = item.get("id")
            sn = item.get("snippet") or {}
            st = item.get("statistics") or {}
            cd = item.get("contentDetails") or {}
            dur = parse_iso_duration(cd.get("duration", ""))
            out[vid] = {
                "video_id": vid,
                "title": sn.get("title", ""),
                "channel_id": sn.get("channelId", ""),
                "channel_name": sn.get("channelTitle", ""),
                "published_at": sn.get("publishedAt", ""),
                "duration_seconds": dur,
                "is_short": 0 < dur <= 61,
                "views": _int(st.get("viewCount")),
                "likes": _int(st.get("likeCount")),
                "comments": _int(st.get("commentCount")),
                "url": f"https://www.youtube.com/watch?v={vid}",
            }
    return out


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_iso_duration(s):
    """PT#H#M#S -> 秒"""
    import re
    if not s:
        return 0
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s)
    if not m:
        return 0
    d, h, mi, sec = (int(g or 0) for g in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def attach_channel_subs(api, videos):
    """补上频道订阅数——这是判断「大号自带流量 vs 真黑马」的关键字段。"""
    cids = sorted({v["channel_id"] for v in videos.values() if v.get("channel_id")})
    subs = {}
    for i in range(0, len(cids), 50):
        chunk = cids[i:i + 50]
        try:
            resp = api.get("channels", {"part": "statistics", "id": ",".join(chunk)})
        except Exception as e:
            print(f"[!] 取频道订阅数失败: {e}")
            continue
        for item in resp.get("items", []):
            st = item.get("statistics") or {}
            subs[item.get("id")] = _int(st.get("subscriberCount"))
    for v in videos.values():
        v["channel_subs"] = subs.get(v["channel_id"])
    return subs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24, help="回看小时数（默认 24）")
    ap.add_argument("--max", type=int, default=50, help="最多取多少条（默认 50）")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    api = build_client()
    ids = search_recent(api, args.hours, args.max)
    if not ids:
        print("[!] 搜索没返回结果——可能是配额用完或参数问题")
        sys.exit(2)

    videos = fetch_details(api, ids)
    print(f"[i] 取得 {len(videos)} 条视频详情（累计配额 {api.quota_used}）")
    attach_channel_subs(api, videos)

    now = datetime.now(timezone.utc)
    rows = []
    for v in videos.values():
        pub = datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) if v["published_at"] else None
        age_h = (now - pub).total_seconds() / 3600 if pub else None
        v["age_hours"] = round(age_h, 2) if age_h else None
        v["views_per_hour"] = round(v["views"] / age_h) if age_h and age_h > 0.05 else None
        v["published_bjt"] = pub.astimezone(BJT).strftime("%Y-%m-%d %H:%M") if pub else ""
        rows.append(v)

    rows.sort(key=lambda r: r["views"], reverse=True)

    payload = {
        "collected_at": now.isoformat(),
        "collected_at_bjt": now.astimezone(BJT).strftime("%Y-%m-%d %H:%M:%S"),
        "hours": args.hours,
        "count": len(rows),
        "videos": rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n[√] 已写入 {args.out}")
    print(f"\n{'播放量':>10} {'时长':>5} {'发布后':>7} {'播放/时':>8} {'订阅':>10}  标题")
    print("-" * 100)
    for r in rows[:25]:
        subs = f"{r['channel_subs']:,}" if r.get("channel_subs") else "?"
        print(f"{r['views']:>10,} {r['duration_seconds']:>5}s {str(r['age_hours'])+'h':>7} "
              f"{str(r['views_per_hour'] or '-'):>8} {subs:>10}  {r['title'][:38]}")


if __name__ == "__main__":
    main()
