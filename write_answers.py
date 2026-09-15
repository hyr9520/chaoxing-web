# -*- coding: utf-8 -*-
"""定稿并写入题库（不再调用大模型，用已收集的投票结果）。

定稿规则（按可靠性）：
  1. 平台已确认的小问（sub_verified）—— 绝对优先，实测判定
  2. 多模型一致票 —— 直接采用
  3. 票数分散 —— 采用 agnes-2.5-flash 的答案
     （依据：唯一可区分的平台锚点 3.1-Ex01 问2=A 上它答对、3.0-flash 答错）
  4. 长度不符 / 无答案的题跳过，不入库
"""
import json
import os
import sys
import time

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

from api.learned import LearnedAnswers          # noqa: E402
from api.answer import CacheDAO                 # noqa: E402

PREF = "agnes-3.0-flash"
R = json.load(open("listening_solved.json", encoding="utf-8"))
learner, cdao = LearnedAnswers(), CacheDAO()

recs = json.load(open("learned_answers.json", encoding="utf-8"))
written, skipped = [], []

for title, v in R.items():
    n = v.get("blanks") or 0
    raw = v.get("votes") or v.get("ai_answers") or {}
    votes = {}
    for m, a in raw.items():
        a = str(a or "").upper()
        if len(a) >= n and n:
            votes[m] = a[:n]
    if not votes:
        skipped.append((title, "无有效投票"))
        continue

    cnt = {}
    for a in votes.values():
        cnt[a] = cnt.get(a, 0) + 1
    top = max(cnt.values())
    cands = [a for a, c in cnt.items() if c == top]
    if len(cands) == 1:
        best, conf = cands[0], "high"
    else:
        best = votes.get(PREF) or max(cands, key=len)
        conf = "mid"

    # 平台锚点强制（平台判定过的小问以平台为准）
    try:
        sv = learner.get_sub_verified(title)
    except Exception:
        sv = {}
    final, plat_used, conflict = [], 0, 0
    for i in range(n):
        plat = str(sv.get(str(i)) or "").strip().upper()
        vi = best[i] if i < len(best) else ""
        if plat:
            final.append(plat)
            plat_used += 1
            if vi and vi != plat:
                conflict += 1
        else:
            final.append(vi)
    final = "".join(final)
    if len(final) != n or not final.isalpha():
        skipped.append((title, f"组装异常 {final!r}"))
        continue

    v["final"], v["confidence"] = final, conf
    v["platform_used"], v["platform_conflict"] = plat_used, conflict
    v["votes"] = votes

    if cdao.get_cache(title) != final:
        cdao.add_cache(title, final)
    e = recs.get(title) or {}
    e["asr_verified"] = final
    e["asr_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    e["asr_conf"] = conf
    e["asr_source"] = "whisper+" + "+".join(sorted(votes))
    recs[title] = e
    written.append((title, final, conf, plat_used, conflict))

json.dump(R, open("listening_solved.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
json.dump(recs, open("learned_answers.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)

print(f"写入题库 {len(written)} 题 | 跳过 {len(skipped)} 题")
print()
for t, f, c, pu, cf in written:
    tag = f"平台锚点{pu}个" + (f",冲突{cf}" if cf else "")
    print(f"  {t[7:40]:<42} {f:<5} [{c}] {tag}")
if skipped:
    print("\n跳过：")
    for t, why in skipped:
        print(f"  {t[7:40]:<42} {why}")
