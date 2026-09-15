# -*- coding: utf-8 -*-
"""题库体检：重复 / 键一致性 / 格式 / 与学习库和平台反馈对照。

重点验证一件事：**写进题库的键，与运行时查询用的键是否完全一致** ——
不一致就等于白写（查询永远命中不到）。
"""
import json
import os
import re
import sys

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

from api.learned import normalize_title          # noqa: E402

C = json.load(open("cache.json", encoding="utf-8"))
L = json.load(open("learned_answers.json", encoding="utf-8"))
G = json.load(open("cache_guessed.json", encoding="utf-8"))
SOLVED = json.load(open("listening_solved.json", encoding="utf-8"))

print(f"题库 {len(C)} 条 | 学习库 {len(L)} | 候选区 {len(G)}")
print()

print("=== 1. 键是否已归一化（重复的前兆）===")
bad = [k for k in C if k != normalize_title(k)]
print(f"  未归一化的键: {len(bad)}", "✓" if not bad else "✗")
for k in bad[:5]:
    print(f"    {k[:70]}")

print("\n=== 2. 归一化后是否撞键（真重复）===")
seen, dup = {}, []
for k in C:
    nk = normalize_title(k)
    if nk in seen:
        dup.append((seen[nk], k, nk))
    seen[nk] = k
print(f"  重复组: {len(dup)}", "✓" if not dup else "✗")
for a, b, nk in dup[:5]:
    print(f"    {nk[:60]}\n      A={a[:60]}\n      B={b[:60]}")

print("\n=== 3. 答案格式（听力题必须是纯字母且长度=小问数）===")
def blanks(t):
    n = re.findall(r"(?<!\d)(\d{1,2})\s*[\.\、]\s*A[\)）\.]", t)
    return len(set(n)) if n else 0
bad_fmt = []
for k, v in C.items():
    if "听力" not in k:
        continue
    n = blanks(k)
    ok = bool(re.fullmatch(r"[A-Ha-h]+", str(v))) and (n == 0 or len(str(v)) == n)
    if not ok:
        bad_fmt.append((k, v, n))
print(f"  格式异常: {len(bad_fmt)}", "✓" if not bad_fmt else "✗")
for k, v, n in bad_fmt:
    print(f"    {k[:56]} 值={v!r} 期望长度={n}")

print("\n=== 4. 题库 vs 学习库 asr_verified 是否一致（写入是否有丢失/错位）===")
mismatch = []
for k, v in C.items():
    if "听力" not in k:
        continue
    asr = (L.get(k) or {}).get("asr_verified")
    if asr and asr != v:
        mismatch.append((k, v, asr))
print(f"  不一致: {len(mismatch)}", "✓" if not mismatch else "✗")
for k, v, a in mismatch[:8]:
    print(f"    {k[:56]} cache={v} asr={a}")

print("\n=== 5. 候选区/题库是否串了（同一题两处都有值）===")
both = [k for k in C if k in G]
print(f"  同时存在于题库与候选区: {len(both)}", "✓" if not both else "需注意")
for k in both[:5]:
    print(f"    {k[:60]}")

print("\n=== 6. 关键验证：题库键能否被运行时查询键命中 ===")
# 运行时的键来自 questions[].title，规范化后与题库键比对。
# 用日志里实际出现过的标题做样本（最真实的证据）。
log = ""
for f in ("chaoxing.log",):
    if os.path.exists(f):
        log = open(f, encoding="utf-8", errors="ignore").read()
titles = set()
for m in re.finditer(r"(?:从缓存中获取答案|获取答案|处理后标题)：(.{10,400})", log):
    t = m.group(1).split(" -> ")[0].strip()
    t = normalize_title(re.sub(r"^\d+", "", re.sub(r"（\d+\.\d+分）$", "", t)))
    if "听力" in t:
        titles.add(t)
hit = sum(1 for t in titles if t in C)
print(f"  日志中出现的听力题标题 {len(titles)} 个，能在题库命中 {hit} 个")
for t in list(titles)[:6]:
    print(f"    {'命中 ' if t in C else '未命中'} {t[:66]}")

print("\n=== 7. 平台反馈：章节检测成绩解析记录 ===")
parsed = re.findall(r"章节检测成绩解析：全对=(\w+).*?沉淀进题库 (\d+) 题", log)
allright = sum(1 for a, _ in parsed if a == "True")
print(f"  解析到成绩 {len(parsed)} 次 | 其中全对 {allright} 次")
for a, t in parsed[-8:]:
    print(f"    全对={a}")
extra = re.findall(r"章节检测全部正确（成绩 (\S+?) 分），通过！", log)
print(f"  含【全部正确，通过】的日志行: {len(extra)} 次")
skip = re.findall(r"章节检测作答详情解析为空，跳过成绩检查", log)
print(f"  含【解析为空跳过】的日志行: {len(skip)} 次")
