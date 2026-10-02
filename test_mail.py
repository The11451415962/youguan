#!/usr/bin/env python3
"""测试邮件模块：只验证 SMTP 登录能不能通，不真发邮件。

用法：
    python test_mail.py            # 只测登录
    python test_mail.py --send     # 真发一封测试邮件（会在你收件箱留一封）
"""
import argparse
import smtplib
import ssl
import sys

sys.path.insert(0, ".")
from harvest_daemon import build_digest, send_mail, smtp_config, smtp_ready


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真发一封测试邮件")
    args = ap.parse_args()

    cfg = smtp_config()
    print("=" * 76)
    print("SMTP 配置（来自环境变量 / .env）")
    print("=" * 76)
    print(f"  服务器    : {cfg['host']}:{cfg['port']}")
    print(f"  发件人    : {cfg['sender'] or '(未设置)'}")
    print(f"  授权码    : {'已设置(' + str(len(cfg['password'])) + '位)' if cfg['password'] else '(未设置)'}")
    print(f"  收件人    : {cfg['recipient'] or '(未设置)'}")

    ok, missing = smtp_ready()
    if not ok:
        print(f"\n[×] 缺少配置: {', '.join(missing)}")
        print("    CI 里由 GitHub Secrets 注入；本地测试需要 .env 里有对应值")
        sys.exit(1)
    print(f"\n[√] 配置完整")

    # 只登录，不发送
    print(f"\n[i] 测试 SMTP 登录（不发送邮件）...")
    try:
        ctx = ssl.create_default_context()
        if cfg["port"] == 465:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=30)
        server.ehlo()
        if cfg["port"] != 465:
            server.starttls(context=ctx)
            server.ehlo()
        server.login(cfg["sender"], cfg["password"])
        print(f"  [√] 登录成功")
        server.quit()
    except Exception as e:
        print(f"  [×] 登录失败: {type(e).__name__}: {str(e)[:200]}")
        print(f"\n  常见原因：")
        print(f"    - 授权码错误（注意不是 QQ 登录密码，是 16 位授权码）")
        print(f"    - SMTP 服务没开启（QQ 邮箱 → 设置 → 账户 → POP3/SMTP服务）")
        print(f"    - 发件人与授权码不属于同一个邮箱")
        sys.exit(1)

    # 构造一封示例邮件，看渲染效果
    demo = [{
        "video_id": "DEMO123456",
        "title": "演示：这是邮件长什么样（不是真视频）",
        "channel": "演示频道",
        "subs": 12_600,
        "tier": "T3",
        "views": 28_400,
        "likes": 1_420,
        "like_rate": 0.05,
        "vpm": 631.0,
        "speed_mult": 21.0,
        "ratio": 2.25,
        "desc_len": 356,
        "pub_bjt": "10-02 21:30",
        "age": 45.0,
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "trigger": "速度异常",
        "high": True,
    }]
    msg = build_digest(demo)
    print(f"\n[i] 示例邮件已构造：")
    print(f"  主题: {msg['Subject']}")
    body = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
    print(f"  正文预览:")
    for line in body.splitlines()[:12]:
        print(f"    {line}")

    if args.send:
        print(f"\n[i] 正在发送测试邮件...")
        ok, info = send_mail(msg)
        print(f"  [{'√' if ok else '×'}] {info}")
    else:
        print(f"\n[i] 没加 --send，所以没有真发。要真发加 --send")


if __name__ == "__main__":
    main()
