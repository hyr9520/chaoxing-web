# -*- coding: utf-8 -*-
"""修复题库里未归一化的键 + 用日志里的真实查询标题验证命中 + 统计平台反馈。

修复对象：回收脚本早期写入的键，形如
  '1\n【单选题】Cet-4-1.16-Exercise 01\xa0\nHaving such a large supply...'
运行时查询会做「去开头数字 + 去末尾分值 + 压空白」，这类键永远匹配不上。
"""
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

from api.learned import normalize_title          # noqa: E402

C_PATH = "cache.json"


def query_norm(t):
    """完全复刻运行时 query_all 的键处理。"""
    t = re.sub(r"^\d+", "", str(t or ""))
    t = re.sub(r"（\d+\.\d+分）$", "", t)
    return normalize_title(t)


print("=== 1. 修复未归一化的键 ===")
C = json.load(open(C_PATH, encoding="utf-8"))
new, fixed, merged = {}, [], 0
for k, v in C.items():
    nk = query_norm(k)
    if nk != k:
        fixed.append((k, nk, v))
    if nk in new:
        merged += 1
        # 已有同题：优先保留"像答案"的那个（纯字母/非空）
        old = new[nk]
        if not re.fullmatch(r"[A-Ha-h]+", str(old)) and re.fullmatch(r"[A-Ha-h]+", str(v)):
            new[nk] = v
        continue
    new[nk] = v

print(f"  待修复键 {len(fixed)} 条 | 撞键合并 {merged} 条")
for k, nk, v in fixed[:12]:
    print(f"    {k[:58]!r}\n      -> {nk[:58]!r}  值={v}")
if fixed:
    shutil.copy2(C_PATH, f"{C_PATH}.bak.fixkeys.{int(time.time())}")
    json.dump(new, open(C_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"  已写入（{len(C)} -> {len(new)} 条）")
else:
    print("  无需修复")

print("\n=== 2. 复查：是否还有未归一化的键 ===")
C = json.load(open(C_PATH, encoding="utf-8"))
still = [k for k in C if k != query_norm(k)]
print(f"  剩余 {len(still)}", "✓" if not still else "✗")
for k in still[:5]:
    print(f"    {k[:70]!r}")

print("\n=== 3. 用日志里的真实查询标题验证命中 ===")
log = open("chaoxing.log", encoding="utf-8", errors="ignore").read()
titles = set()
for m in re.finditer(r"当前题目信息 -> \{'id': '\d+', 'title': \"([^\"]{10,600})\"", log):
    titles.add(query_norm(m.group(1)))
for m in re.finditer(r"【听力题】[^\n]{20,400}", log):
    titles.add(query_norm(m.group(0)))
lis = {t for t in titles if "听力" in t}
hit = [t for t in lis if t in C]
print(f"  日志中听力题标题 {len(lis)} 个 | 题库命中 {len(hit)} 个")
for t in sorted(lis)[:25]:
    print(f"    {'命中  ' if t in C else '未命中'} {t[:62]}")

print("\n=== 4. 平台反馈统计 ===")
allright = re.findall(r"章节检测全部正确（成绩 (\S+?)\s*分），通过！", log)
parsed = re.findall(r"章节检测成绩解析：全对=(\w+) 总得分=(\S+)", log)
wrong = re.findall(r"章节检测有 (\d+)/(\d+) 题回答错误", log)
skipped = re.findall(r"作答详情解析为空", log)
print(f"  「全部正确，通过」: {len(allright)} 次 {allright[:6]}")
print(f"  「成绩解析」: {len(parsed)} 次  ->  {parsed[:8]}")
print(f"  「有题回答错误」: {len(wrong)} 次  ->  {wrong[:6]}")
print(f"  「解析为空跳过」: {len(skipped)} 次")
