#!/usr/bin/env python3
"""一键添加监控频道：贴个链接就行。

做四件事（全自动，不需要手动编辑 config.json，也不需要手动跑初始化）：
  1. 从各种形式的链接/句柄里解析出 channel_id（用 YouTube API，不依赖 requests）
  2. 从远端拉 config.json，检查是否已在监控列表里（防重复）
  3. 把频道名 + ID 加进 channels
  4. 拉该频道最近的视频，写进 history.json 作为「已通知」种子
     —— 这样它不会把存量视频当成新视频狂发邮件

用法：
    python add_channel.py https://www.youtube.com/@MrBeast
    python add_channel.py @MrBeast UCxxxxxxxxxxxxxxxxxxxxxx
    python add_channel.py --name "自定义名字" @handle     # 手动指定显示名
    python add_channel.py --dry-run @handle               # 只看解析结果
    python add_channel.py --no-push @handle               # 只改本地，不推远端

输入的链接形式都支持：
    https://www.youtube.com/@handle
    https://www.youtube.com/channel/UCxxxx
    https://www.youtube.com/c/CustomName
    https://www.youtube.com/user/OldName
    @handle
    UCxxxx（直接给 ID）
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, ".")
from yt_collect import build_client

REPO = "The11451415962/youguan"
BRANCH = "main"
BASE = Path(__file__).parent
BJT = timezone(timedelta(hours=8))

SEED_VIDEOS = 30        # 每个新频道预置多少条历史（防止把存量当新视频）


def gh(*a, input_data=None):
    p = subprocess.run(["gh", *a], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", input=input_data)
    return (p.stdout, None) if p.returncode == 0 else (None, (p.stderr or "").strip())


def gh_json(path):
    out, err = gh("api", f"repos/{REPO}/contents/{path}",
                  "-H", "Accept: application/vnd.github.raw")
    if err:
        return None, err
    try:
        return json.loads(out), None
    except json.JSONDecodeError as e:
        return None, f"JSON 解析失败: {e}"


def put_file(path, content, message):
    out, err = gh("api", f"repos/{REPO}/contents/{path}")
    sha = json.loads(out)["sha"] if not err else None
    body = {"message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode(),
            "branch": BRANCH}
    if sha:
        body["sha"] = sha
    _, err = gh("api", "-X", "PUT", f"repos/{REPO}/contents/{path}",
                "--input", "-", input_data=json.dumps(body))
    return (True, None) if not err else (False, err)


def parse_ref(text):
    """把各种形式的输入解析成 (类型, 值)。"""
    text = text.strip()
    if re.fullmatch(r"UC[\w-]{22}", text):
        return "id", text
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", text)
    if m:
        return "id", m.group(1)
    m = re.search(r"youtube\.com/@([\w.\-]+)", text)
    if m:
        return "handle", "@" + m.group(1)
    m = re.search(r"youtube\.com/(?:c|user)/([\w.\-]+)", text)
    if m:
        return "handle", "@" + m.group(1)
    if text.startswith("@"):
        return "handle", text
    if re.fullmatch(r"[\w.\-]+", text):
        return "handle", "@" + text
    return None, None


def resolve_channel(api, kind, val):
    """用 YouTube API 解析频道。返回 (channel_id, 频道名) 或 (None, 错误)。"""
    params = {"part": "snippet,statistics,contentDetails"}
    if kind == "id":
        params["id"] = val
    else:
        params["forHandle"] = val
    try:
        resp = api.get("channels", params)
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:150]}"
    items = resp.get("items", [])
    if not items:
        return None, None, "找不到这个频道（链接可能不对，或频道已删除）"
    ch = items[0]
    sn = ch.get("snippet") or {}
    st = ch.get("statistics") or {}
    return ch["id"], sn.get("title", ""), {
        "subs": int(st.get("subscriberCount") or 0),
        "videos": int(st.get("videoCount") or 0),
        "country": sn.get("country", ""),
        "uploads": ((ch.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads", ""),
    }


def fetch_recent_ids(api, uploads, n=SEED_VIDEOS):
    """拉该频道最近的视频 id（按发布顺序，新→旧）。"""
    ids = []
    page = None
    while len(ids) < n:
        params = {"part": "contentDetails", "playlistId": uploads,
                  "maxResults": min(50, n - len(ids))}
        if page:
            params["pageToken"] = page
        try:
            resp = api.get("playlistItems", params)
        except Exception:
            break
        for it in resp.get("items", []):
            vid = (it.get("contentDetails") or {}).get("videoId")
            if vid:
                ids.append(vid)
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("refs", nargs="+", help="频道链接 / @handle / UCxxxx")
    ap.add_argument("--name", default="", help="手动指定显示名（覆盖自动获取的）")
    ap.add_argument("--seed", type=int, default=SEED_VIDEOS, help="预置多少条历史")
    ap.add_argument("--dry-run", action="store_true", help="只看解析结果，不修改")
    ap.add_argument("--no-push", action="store_true", help="只改本地文件，不推远端")
    args = ap.parse_args()

    api_key_ok = True
    try:
        api = build_client()
    except SystemExit:
        api_key_ok = False
        api = None

    if not api_key_ok:
        print("[×] 没有可用的 API Key（检查 .env 的 YOUTUBE_API_KEY）")
        sys.exit(1)

    # 拉远端配置和历史
    cfg, err = gh_json("config.json")
    if err:
        print(f"[×] 读 config.json 失败: {err}")
        sys.exit(1)
    hist, err = gh_json("history.json")
    if err:
        print(f"[!] 读 history.json 失败（将新建）: {err}")
        hist = {}

    channels = cfg.setdefault("channels", {})
    print(f"[i] 当前监控 {len(channels)} 个频道")

    added, skipped, failed = [], [], []

    for ref in args.refs:
        print(f"\n── 处理: {ref} ──")
        kind, val = parse_ref(ref)
        if not kind:
            print(f"  [×] 无法识别的输入格式")
            failed.append(ref)
            continue
        print(f"  解析为 {kind}: {val}")

        cid, title, info = resolve_channel(api, kind, val)
        if not cid:
            print(f"  [×] {info}")
            failed.append(ref)
            continue

        name = args.name or title
        print(f"  ✓ 找到频道: {name}")
        print(f"    ID      : {cid}")
        print(f"    订阅数  : {info['subs']:,}")
        print(f"    视频数  : {info['videos']:,}")
        if info.get("country"):
            print(f"    地区    : {info['country']}")

        if cid in channels:
            print(f"  ⏭  已在监控列表里（显示名: {channels[cid]}），跳过")
            skipped.append(name)
            continue

        if args.dry_run:
            print(f"  [dry-run] 会添加，并预置最近 {args.seed} 条视频为已通知")
            added.append(name)
            continue

        # 拉历史种子
        ids = fetch_recent_ids(api, info["uploads"], args.seed)
        print(f"  ✓ 预置 {len(ids)} 条历史视频（避免把存量当新视频）")

        channels[cid] = name
        # 新视频放最前，保证「最新在最后」的不变式不被破坏
        # （yt_monitor 只判断「是否存在」，顺序不影响正确性，但保持整洁）
        hist[cid] = list(reversed(ids))

        added.append(name)

    print(f"\n{'='*70}")
    print(f"结果: 新增 {len(added)} / 已存在 {len(skipped)} / 失败 {len(failed)}")
    if added:
        print(f"  新增: {', '.join(added)}")
    if failed:
        print(f"  失败: {', '.join(failed)}")
    print(f"{'='*70}")

    if args.dry_run:
        print("\n[i] dry-run 模式，未做任何修改")
        return
    if not added:
        print("\n[i] 没有新增，无需推送")
        return

    if args.no_push:
        Path(BASE / "config_new.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(BASE / "history_added.json").write_text(
            json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[i] 已写入 config_new.json / history_added.json（未推送）")
        return

    # 推送
    msg = f"feat: 新增监控频道 {'、'.join(added)}"
    ok, err = put_file("history.json", json.dumps(hist, ensure_ascii=False, indent=2),
                       msg + "（含历史种子）")
    if not ok:
        print(f"\n[×] 推送 history 失败: {err}")
        sys.exit(1)
    print(f"\n[√] history.json 已更新")

    ok, err = put_file("config.json", json.dumps(cfg, ensure_ascii=False, indent=2), msg)
    if not ok:
        print(f"[×] 推送 config 失败: {err}")
        sys.exit(1)
    print(f"[√] config.json 已更新")

    print(f"\n[√] 完成！{len(added)} 个频道已加入监控")
    print(f"    下一次运行（5 分钟内）就会开始检查它们")
    print(f"    存量视频已预置为「已通知」，不会误发邮件")
    print(f"\n配额用量: {api.quota_used}")


if __name__ == "__main__":
    main()
