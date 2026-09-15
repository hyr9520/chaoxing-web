# -*- coding: utf-8 -*-
"""统一修复三个存储的键形态（题库已修，这里处理学习库与候选区），
并做三方一致性验证：同一道题在 cache/learned/cache_guessed 里的键必须相同。
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


def query_norm(t):
    t = re.sub(r"^\d+", "", str(t or ""))
    t = re.sub(r"（\d+\.\d+分）$", "", t)
    return normalize_title(t)


def migrate(path, merge=None):
    data = json.load(open(path, encoding="utf-8"))
    out, fixed, merged_n = {}, [], 0
    for k, v in data.items():
        nk = query_norm(k)
        if nk != k:
            fixed.append(k)
        if nk in out:
            merged_n += 1
            if merge:
                out[nk] = merge(out[nk], v, nk)
            continue
        out[nk] = v
    if fixed:
        shutil.copy2(path, f"{path}.bak.fixkeys.{int(time.time())}")
        json.dump(out, open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    print(f"{os.path.basename(path)}: {len(data)} -> {len(out)} 条"
          f"（键修复 {len(fixed)}、合并 {merged_n}）")
    for k in fixed[:6]:
        print(f"    {k[:66]!r}")
    return out


def merge_rec(a, b, key):
    """合并同一题的两条学习记录（保留信息最全的）。"""
    rec = dict(a)
    if not rec.get("verified") and b.get("verified"):
        rec["verified"] = b["verified"]; rec["verified_at"] = b.get("verified_at")
    if not rec.get("asr_verified") and b.get("asr_verified"):
        rec["asr_verified"] = b["asr_verified"]; rec["asr_at"] = b.get("asr_at")
        rec["asr_conf"] = b.get("asr_conf"); rec["asr_source"] = b.get("asr_source")
    wl = list(rec.get("wrong") or [])
    for w in (b.get("wrong") or []):
        if w not in wl:
            wl.append(w)
    if wl:
        rec["wrong"] = wl[:5]
    sv = dict(rec.get("sub_verified") or {})
    for k2, v2 in (b.get("sub_verified") or {}).items():
        sv.setdefault(k2, v2)
    if sv:
        rec["sub_verified"] = sv
    rec["updated_at"] = max(str(rec.get("updated_at") or ""),
                            str(b.get("updated_at") or ""))
    return rec


L_PATH, G_PATH = "learned_answers.json", "cache_guessed.json"
L = migrate(L_PATH, merge_rec)
G = migrate(G_PATH)

print("\n=== 三方一致性（听力题）===")
C = json.load(open("cache.json", encoding="utf-8"))
lis_c = {k: v for k, v in C.items() if "听力" in k}
lis_l = {k: v for k, v in L.items() if "听力" in k}
lis_g = {k: v for k, v in G.items() if "听力" in k}
print(f"  cache {len(lis_c)} | learned {len(lis_l)} | guessed {len(lis_g)}")
bad = 0
for k in lis_c:
    if k not in lis_l:
        bad += 1
        print(f"  [cache 有但 learned 没有] {k[:60]}")
    elif lis_l[k].get("asr_verified") and lis_l[k]["asr_verified"] != lis_c[k]:
        bad += 1
        print(f"  [值不一致] {k[:50]} cache={lis_c[k]} asr={lis_l[k]['asr_verified']}")
for k in lis_g:
    if k in lis_c:
        bad += 1
        print(f"  [候选区与题库撞键] {k[:60]}")
print("  ✓ 三方一致" if bad == 0 else f"  ✗ {bad} 处问题")
