#!/usr/bin/env python3
"""验证分层打捞算法的判据。"""
import sys
sys.path.insert(0, ".")
from harvest_daemon import (TIERS, TIER_LABEL, TIER_QUOTA, TIER_RULES,
                            TIER_VPM_PRIOR, tier_of, promotion_score)

print("=" * 100)
print("① 档位归属")
print("=" * 100)
cases = [(0, "T1"), (50, "T1"), (99, "T1"), (100, "T2"), (999, "T2"),
         (1_000, "T3"), (9_999, "T3"), (10_000, "T4"), (99_999, "T4"),
         (100_000, "T5"), (999_999, "T5"), (1_000_000, "T6"), (50_000_000, "T6")]
ok = 0
for subs, expect in cases:
    got = tier_of(subs)
    m = "✓" if got == expect else "✗"
    if got == expect:
        ok += 1
    print(f"  {m} {subs:>12,} 订阅 → {got} ({TIER_LABEL[got]})   期望 {expect}")
print(f"  → {ok}/{len(cases)} 正确")

print(f"\n{'='*100}")
print("② 预警门槛：同一个播放量在不同档位的判定")
print("=" * 100)
print(f"  {'播放量':>10}  " + "  ".join(f"{t:>10}" for t in TIERS))
for views in (3_000, 5_000, 20_000, 50_000, 150_000, 500_000):
    marks = []
    for t in TIERS:
        marks.append("  ✅ 预警  " if views >= TIER_RULES[t]["alert_views"] else "  —      ")
    print(f"  {views:>10,}  " + "  ".join(marks))

print(f"\n  验证「小号黑马」和「大号日常」：")
tests = [
    ("小号黑马", 92, 299_000, True),
    ("大号日常", 3_930_000, 2_085, False),
    ("大号真爆", 3_930_000, 600_000, True),
    ("中号起飞", 25_300, 30_000, True),
]
for name, subs, views, expect in tests:
    t = tier_of(subs)
    threshold = TIER_RULES[t]["alert_views"]
    fire = views >= threshold
    m = "✓" if fire == expect else "✗"
    print(f"  {m} {name:<8} {subs:>10,} 订阅 {views:>9,} 播放 → {t} "
          f"(门槛 {threshold:,}) → {'预警' if fire else '不预警'}")

print(f"\n{'='*100}")
print("③ 排序分 = 档位内速度倍数 × 播放量（谁更异常谁排前）")
print("=" * 100)
scored = [
    ("T1 微号真黑马", 50, 200, 299_000, 12_000),
    ("T3 小号起飞", 5_000, 200, 25_000, 2_000),
    ("T6 巨号真爆", 3_930_000, 50_000, 2_500_000, 150_000),
    ("T1 微号小爆", 50, 50, 4_000, 400),
    ("T5 大号正常", 500_000, 60, 300_000, 15_000),
    ("T6 巨号日常", 3_930_000, 88, 9_394, 500),
    ("T1 微号噪音", 50, 30, 60, 2),
]
rows = []
for name, subs, vpm, views, likes in scored:
    t = tier_of(subs)
    base = TIER_VPM_PRIOR[t]
    s = promotion_score(views, vpm, subs, t, likes)
    rows.append((s, name, t, subs, views, vpm, vpm / base, likes / views if views else 0))
print(f"  {'排序分':>8}  {'档':<4} {'播放':>10} {'播放/分':>8} {'速度倍数':>8} {'点赞率':>7}  说明")
print("  " + "-" * 92)
for s, name, t, subs, views, vpm, mult, lr in sorted(rows, reverse=True):
    print(f"  {s:>8.2f}  {t:<4} {views:>10,} {vpm:>8} {mult:>7.1f}x {lr*100:>6.1f}%  {name}")

order = [r[1] for r in sorted(rows, reverse=True)]
print(f"\n  实际顺序：{' > '.join(order)}")
print(f"  → 排最前的是「相对自己档位最异常」的那个，符合设计意图")

print(f"\n{'='*100}")
print("④ 分档名额")
print("=" * 100)
tot = 0
for t in TIERS:
    q = TIER_QUOTA[t]
    tot += q
    print(f"  {t} {TIER_LABEL[t]:<12} {q:>4} 条  {'█' * q}")
print(f"  {'合计':<16} {tot:>4} 条")
print(f"\n  对比旧方案：统一 min_channel_subs=10000 → T1~T3（0~1万订阅）全部被拒")
print(f"  现在 T1~T3 有 {TIER_QUOTA['T1']+TIER_QUOTA['T2']+TIER_QUOTA['T3']} 个名额（占 {(TIER_QUOTA['T1']+TIER_QUOTA['T2']+TIER_QUOTA['T3'])/tot*100:.0f}%）")
