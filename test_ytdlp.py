#!/usr/bin/env python3
"""在 CI 上验证 yt-dlp 能否从机房 IP 下载到 YouTube 音频。

这是人声检测方案的 Go/No-Go 测试。
只下载音频（不下载视频画面），只取前若干秒。

用法：
    python test_ytdlp.py                     # 用内置样本视频
    python test_ytdlp.py --ids vid1 vid2
    python test_ytdlp.py --seconds 15 --attempts 3

输出：成功率、失败原因分布、平均耗时
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# 测试样本：真实 Minecraft Shorts（id, 说明）
SAMPLES = [
    ("_-wkx3yZigg", "波兰语 Minecraft 找错误"),
    ("VEH9fRSoR_U", "weirdest minecraft mod"),
    ("CI6H9p6_3aQ", "小猪佩奇蹭标签（已被过滤，仅作样本）"),
]

# yt-dlp 的 player client 尝试顺序
# ios/tv 通常不要求 PO Token，比默认 web 更容易在机房 IP 上成功
CLIENT_CHAINS = [
    ["ios"],
    ["tv"],
    ["web_safari"],
    ["android"],
    [],            # 默认
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def check_tools():
    print("=" * 88)
    print("环境检查")
    print("=" * 88)
    ok = True
    for tool in ("yt-dlp", "ffmpeg"):
        path = shutil.which(tool)
        if path:
            try:
                v = subprocess.run([tool, "--version"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=20)
                print(f"  ✓ {tool:<8} {path}  版本 {v.stdout.strip().splitlines()[0][:40]}")
            except Exception as e:
                print(f"  ✓ {tool:<8} {path}  (版本读取失败)")
        else:
            print(f"  ✗ {tool:<8} 未找到")
            ok = False
    return ok


def try_download(vid, outdir, seconds, clients):
    """尝试用指定 player client 下载音频。返回 (成功?, 耗时, 错误信息, 文件大小)。"""
    out_tmpl = str(Path(outdir) / f"{vid}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--user-agent", UA,
        "-f", "bestaudio/best",
        "--download-sections", f"*0-{seconds}",
        "--force-keyframes-at-cuts",
        "-o", out_tmpl,
    ]
    if clients:
        cmd += ["--extractor-args", f"youtube:player_client={','.join(clients)}"]
    cmd.append(f"https://www.youtube.com/watch?v={vid}")

    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except subprocess.TimeoutExpired:
        return False, time.time() - t0, "超时(120s)", 0

    dt = time.time() - t0
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip().replace("\n", " ")[:200]
        return False, dt, err, 0

    files = [f for f in Path(outdir).glob(f"{vid}.*")
             if f.suffix not in (".part", ".ytdl")]
    if not files:
        return False, dt, "下载成功但没找到文件", 0
    size = sum(f.stat().st_size for f in files)
    return True, dt, None, size


def probe_audio(vid, outdir):
    """用 ffprobe 确认拿到的确实是音频。"""
    files = [f for f in Path(outdir).glob(f"{vid}.*")
             if f.suffix not in (".part", ".ytdl")]
    if not files:
        return None
    f = files[0]
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,duration", "-of", "json", str(f)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        info = json.loads(p.stdout)
        streams = info.get("streams", [])
        return {
            "file": f.name,
            "streams": [(s.get("codec_type"), s.get("codec_name")) for s in streams],
            "duration": streams[0].get("duration") if streams else None,
        }
    except Exception as e:
        return {"file": f.name, "error": str(e)[:80]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", default=None)
    ap.add_argument("--seconds", type=int, default=15)
    ap.add_argument("--attempts", type=int, default=3, help="每个视频最多试几种 client")
    args = ap.parse_args()

    if not check_tools():
        print("\n[×] 缺少工具，无法继续")
        sys.exit(1)

    samples = [(i, d) for i, d in SAMPLES] if args.ids is None else [(i, "") for i in args.ids]

    print(f"\n{'='*88}")
    print(f"开始测试：{len(samples)} 个视频，每视频最多试 {args.attempts} 种 client，取前 {args.seconds} 秒音频")
    print(f"{'='*88}")

    workdir = Path(tempfile.mkdtemp(prefix="ytdlp_", dir=os.getcwd()))
    results = []

    for vid, desc in samples:
        print(f"\n▸ {vid}  {desc}")
        got = False
        for ci, clients in enumerate(CLIENT_CHAINS[:args.attempts]):
            label = ",".join(clients) if clients else "默认"
            ok, dt, err, size = try_download(vid, workdir, args.seconds, clients)
            if ok:
                info = probe_audio(vid, workdir)
                streams = info.get("streams") if info else None
                print(f"    ✓ [{label}] 成功  {dt:.1f}s  {size/1024:.0f} KB  "
                      f"流={streams}  时长={info.get('duration') if info else '?'}")
                results.append({"video_id": vid, "ok": True, "client": label,
                                "seconds": round(dt, 1), "bytes": size})
                got = True
                break
            else:
                short = (err or "")[:110]
                print(f"    ✗ [{label}] 失败 {dt:.1f}s  {short}")
                results.append({"video_id": vid, "ok": False, "client": label,
                                "seconds": round(dt, 1), "error": (err or "")[:200]})
        if not got:
            print(f"    → 该视频全部 client 都失败")

    # ---- 汇总 ----
    per_video = {}
    for r in results:
        per_video.setdefault(r["video_id"], []).append(r)
    success = sum(1 for v, rs in per_video.items() if any(r["ok"] for r in rs))
    total = len(per_video)

    print(f"\n{'='*88}")
    print("结果汇总")
    print(f"{'='*88}")
    print(f"  视频数          : {total}")
    print(f"  成功下载        : {success}  ({success/total*100:.0f}%)")
    print(f"  失败            : {total-success}")

    if success:
        times = [r["seconds"] for r in results if r["ok"]]
        sizes = [r["bytes"] for r in results if r["ok"]]
        print(f"  平均耗时        : {sum(times)/len(times):.1f}s")
        print(f"  平均文件大小    : {sum(sizes)/len(sizes)/1024:.0f} KB")
        clients_used = {}
        for r in results:
            if r["ok"]:
                clients_used[r["client"]] = clients_used.get(r["client"], 0) + 1
        print(f"  成功的 client   : {clients_used}")

    fails = [r for r in results if not r["ok"]]
    if fails:
        print(f"\n  失败原因 TOP:")
        from collections import Counter
        reasons = Counter()
        for r in fails:
            e = (r.get("error") or "").lower()
            if "sign in" in e or "bot" in e:
                reasons["需要登录/机器人检测"] += 1
            elif "403" in e or "forbidden" in e:
                reasons["403 被拒"] += 1
            elif "429" in e or "too many" in e:
                reasons["429 限流"] += 1
            elif "超时" in e:
                reasons["超时"] += 1
            elif "unavailable" in e or "private" in e:
                reasons["视频不可用"] += 1
            else:
                reasons[(r.get("error") or "")[:60]] += 1
        for k, v in reasons.most_common(6):
            print(f"    {v:>3} 次  {k}")

    verdict = ("✅ 可行（成功率 ≥70%）" if total and success/total >= 0.7 else
               "⚠️ 勉强（50~70%），需要优化 client 链" if total and success/total >= 0.5 else
               "❌ 不可行（<50%），机房 IP 被挡严重")
    print(f"\n  结论: {verdict}")

    json.dump({"results": results, "success": success, "total": total},
              open("ytdlp_test_result.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"  明细: ytdlp_test_result.json")
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
