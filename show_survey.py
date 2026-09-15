# -*- coding: utf-8 -*-
"""查看某次普查报告里的章节题型分布（避免命令行引号转义问题）。"""
import glob
import json
import sys
from collections import Counter

NAMES = {"0": "单选", "1": "多选", "2": "填空", "3": "判断",
         "4": "简答", "5": "名词解释", "6": "论述", "7": "计算",
         "14": "完型", "15": "阅读", "19": "听力"}

pattern = sys.argv[1] if len(sys.argv) > 1 else "survey_multi_*.json"
f = sorted(glob.glob(pattern))[-1]
d = json.load(open(f, encoding="utf-8"))
qs = d.get("questions") or []

print(f"报告: {f}")
print(f"总题数: {len(qs)}\n")

c = Counter((q["chapter"], q["type_code"]) for q in qs)
chaps = {}
for (ch, tc), n in c.items():
    chaps.setdefault(ch, {})[tc] = n
for ch, m in chaps.items():
    desc = "  ".join(f"{NAMES.get(k, k)}×{v}" for k, v in sorted(m.items()))
    print(f"  {ch}: {desc}")

print()
print("=== 填空题样例（看空数分布）===")
blanks = [q for q in qs if q["type_code"] == "2"]
for q in blanks[:8]:
    t = " ".join(str(q["title"]).split())
    dots = str(q["title"]).count("____")
    print(f"  空数约{dots} [{q['chapter']}] {t[:80]}")

print()
print("=== 简答题样例 ===")
for q in [x for x in qs if x["type_code"] == "4"][:4]:
    t = " ".join(str(q["title"]).split())
    print(f"  [{q['chapter']}] {t[:90]}")
