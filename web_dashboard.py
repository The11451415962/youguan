#!/usr/bin/env python3
"""
YouTube 监控 Web 仪表盘
运行: python yt_monitor.py --web
然后浏览器打开 http://localhost:5000
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, jsonify

load_dotenv()

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
HISTORY_PATH = BASE_DIR / "history.json"
LOG_PATH = BASE_DIR / "yt_monitor.log"

BJT = timezone(timedelta(hours=8))

app = Flask(__name__)

# 关闭 Flask 默认日志输出到控制台（避免和监控脚本日志混在一起）
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)


def load_json_safe(path: Path, default=None):
    """安全读取 JSON 文件。"""
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default if default is not None else {}


def parse_log_tail(lines: int = 50) -> list:
    """读取日志文件末尾若干行。"""
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
            return all_lines[-lines:]
    except IOError:
        return []


@app.route("/")
def index():
    """仪表盘主页。"""
    config = load_json_safe(CONFIG_PATH, {})
    history = load_json_safe(HISTORY_PATH, {})
    logs = parse_log_tail(80)

    # 统计每个频道的通知数量
    channel_stats = {}
    for cid, cname in config.get("channels", {}).items():
        notified_count = len(history.get(cid, []))
        channel_stats[cid] = {
            "name": cname,
            "notified_count": notified_count,
        }

    last_run = None
    if logs:
        # 从日志末尾找最后一次运行时间
        for line in reversed(logs):
            if line.strip():
                last_run = line[:19]
                break

    return render_template(
        "dashboard.html",
        channel_stats=channel_stats,
        logs=logs,
        last_run=last_run,
        config=config,
    )


@app.route("/api/status")
def api_status():
    """API 端点：返回当前状态（供前端自动刷新）。"""
    config = load_json_safe(CONFIG_PATH, {})
    history = load_json_safe(HISTORY_PATH, {})

    channels = []
    for cid, cname in config.get("channels", {}).items():
        channels.append({
            "id": cid,
            "name": cname,
            "notified_count": len(history.get(cid, [])),
        })

    return jsonify({
        "channels": channels,
        "total_notified": sum(c["notified_count"] for c in channels),
        "channel_count": len(channels),
    })


@app.route("/api/logs")
def api_logs():
    """API 端点：返回最新日志。"""
    return jsonify({"logs": parse_log_tail(80)})


def main():
    print("=" * 50)
    print("YouTube 监控 Web 仪表盘")
    print("浏览器打开: http://localhost:5000")
    print("按 Ctrl+C 停止")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)


if __name__ == "__main__":
    main()
