#!/usr/bin/env python3
"""监控停机哨兵。

做的事：查这个仓库最近有没有「YouTube Monitor」的成功运行。
如果超过 MAX_SILENCE_HOURS 小时没有任何一次成功运行，说明监控已经停了
（触发器挂了 / 配额全灭 / 你自己关掉了），立刻发邮件告警并让任务失败。

为什么需要它：
2026-10-15 外部触发器（cron-job.org）静默停止，监控整整 16 天没跑，
期间一次邮件都没发，没有任何人能发现。这个哨兵就是为了让这种情况
在 2 小时内就暴露。

用法（一般由 workflow 调用）：
    python watchdog.py

环境变量：
    GH_TOKEN               有 actions:read 权限的 token
    GITHUB_REPOSITORY      owner/repo
    SMTP_PASSWORD          SMTP 授权码
    SMTP_SENDER            发件人
    NOTIFICATION_RECIPIENT 收件人
    MAX_SILENCE_HOURS      允许多久没有成功运行（默认 2）
"""

import json
import os
import smtplib
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

BJT = timezone(timedelta(hours=8))
WATCHED_WORKFLOW = "YouTube Monitor"   # 被监视的 workflow 名字
BASE_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def gh_api(path: str):
    """调用 GitHub REST API，返回解析后的 JSON。"""
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or ""
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "youguan-watchdog/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def recent_runs(repo: str, limit: int = 50):
    """拉最近的运行记录。"""
    data = gh_api(f"/repos/{repo}/actions/runs?per_page={limit}")
    return data.get("workflow_runs") or []


def parse_time(text: str):
    """解析 GitHub 的 ISO 时间。"""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def find_last_success(runs):
    """找出被监视 workflow 最近一次成功运行。"""
    for run in runs:
        if run.get("name") != WATCHED_WORKFLOW:
            continue
        if run.get("conclusion") != "success":
            continue
        stamp = run.get("created_at") or run.get("run_started_at")
        if not stamp:
            continue
        return run, parse_time(stamp)
    return None, None


# ---------------------------------------------------------------------------
# 邮件
# ---------------------------------------------------------------------------

def load_smtp_config():
    """SMTP 的 host/port 从 config.json 读，密码等敏感项从环境变量读。"""
    host, port, use_tls = "smtp.qq.com", 587, True
    config_path = BASE_DIR / "config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                smtp = (json.load(f) or {}).get("smtp") or {}
            host = smtp.get("host", host)
            port = int(smtp.get("port", port))
            use_tls = smtp.get("use_tls", use_tls)
        except Exception:
            pass
    return {
        "host": host,
        "port": port,
        "use_tls": use_tls,
        "password": os.getenv("SMTP_PASSWORD", ""),
        "sender": os.getenv("SMTP_SENDER", ""),
        "recipient": os.getenv("NOTIFICATION_RECIPIENT", ""),
    }


def send_mail(subject: str, lines: list) -> bool:
    """发一封纯文本 + HTML 的告警邮件。"""
    cfg = load_smtp_config()
    if not (cfg["sender"] and cfg["password"] and cfg["recipient"]):
        print("[!] SMTP 配置不完整，跳过发信")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = cfg["recipient"]

    text = subject + "\n\n" + "\n".join(lines)
    html = (
        f"<h2>⚠️ {subject}</h2>"
        + "".join(f"<p>{line}</p>" for line in lines)
        + "<hr><p><small>这封邮件由 youguan 的停机哨兵发出。</small></p>"
    )
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        if cfg["port"] == 465:
            import ssl
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                      context=ssl.create_default_context(), timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        server.ehlo()
        if cfg["port"] != 465 and cfg["use_tls"]:
            import ssl
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(cfg["sender"], cfg["password"])
        server.sendmail(cfg["sender"], cfg["recipient"], msg.as_string())
        server.quit()
        print(f"[√] 告警邮件已发送 -> {cfg['recipient']}")
        return True
    except Exception as e:
        print(f"[×] 发信失败: {e}")
        return False


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def main() -> int:
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo:
        print("[×] 缺少 GITHUB_REPOSITORY")
        return 1

    try:
        max_silence = float(os.getenv("MAX_SILENCE_HOURS", "2") or 2)
    except ValueError:
        max_silence = 2.0

    print(f"[i] 仓库: {repo}")
    print(f"[i] 监视对象: {WATCHED_WORKFLOW}")
    print(f"[i] 容忍静默时长: {max_silence} 小时")

    try:
        runs = recent_runs(repo)
    except Exception as e:
        print(f"[×] 拉取运行记录失败: {e}")
        return 1

    print(f"[i] 拉到 {len(runs)} 条运行记录")

    run, last_ok = find_last_success(runs)
    now = datetime.now(timezone.utc)

    if last_ok is None:
        silence = None
        print("[!] 最近 50 条里没有任何一次成功运行")
    else:
        silence = (now - last_ok).total_seconds() / 3600
        print(f"[i] 最近一次成功运行: {last_ok.astimezone(BJT):%Y-%m-%d %H:%M:%S} 北京时间"
              f"（{silence:.1f} 小时前）")

    if silence is not None and silence <= max_silence:
        print(f"[√] 监控正常，静默 {silence:.1f}h <= {max_silence}h")
        return 0

    # ---- 停摆了，告警 ----
    lines = [
        f"监控已经超过 {max_silence} 小时没有成功运行。",
        (f"最近一次成功运行: {last_ok.astimezone(BJT):%Y-%m-%d %H:%M:%S} 北京时间"
         f"（{silence:.1f} 小时前）" if last_ok else "最近 50 条记录里没有任何一次成功运行。"),
        "可能原因：",
        "· 外部触发器 cron-job.org 停摆（历史上发生过，停了 16 天）",
        "· GitHub 原生定时器被延迟或禁用",
        "· API Key 全部失去额度",
        "· 有人手动关掉了 workflow",
        "",
        f"去这里看一眼: https://github.com/{repo}/actions",
    ]
    ok = send_mail("[YouTube 监控停摆] 已经很久没有成功运行了", lines)
    print("[×] 监控停摆，退出码 1（让 workflow 变红）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
