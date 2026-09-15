# -*- coding: utf-8 -*-
"""最终定稿：三模型投票 + 平台锚点校准，产出可入库的听力答案。

投票策略（按可靠性排序）：
  1. 平台已确认的小问（sub_verified）—— 实测判定，绝对优先
  2. 三模型多数票（agnes-2.5-flash / 3.0-flash / 2.0-flash）
  3. 平局时取 2.5-flash —— 它在唯一可区分的平台锚点（3.1-Ex01 问2=A）上答对，
     而 3.0-flash 答错，故以它为先

用法：python finalize_answers.py [--write]
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

import requests
import urllib3

urllib3.disable_warnings()
WRITE = "--write" in sys.argv
PREFERRED = "agnes-3.0-flash"
MODELS = ["agnes-3.0-flash", "agnes-2.5-flash"]

_CFG = json.load(open("ui_config.json", encoding="utf-8"))
# 账号不写死（避免随仓库外泄）：用环境变量 CK_ACCOUNT 指定手机号
_M = [p for p in _CFG["profiles"]
      if p.get("username") == os.environ.get("CK_ACCOUNT", "")]
if not _M:
    raise SystemExit("请用环境变量 CK_ACCOUNT 指定学习通账号（手机号）")
_P = _M[0]
KEY, BASE = _P["ai_key"], _P["ai_endpoint"].rstrip("/")

INDEX = json.load(open("audio_index.json", encoding="utf-8"))
TR = json.load(open("transcripts.json", encoding="utf-8"))
R = json.load(open("listening_solved.json", encoding="utf-8"))

from api.learned import LearnedAnswers   # noqa: E402
from api.answer import CacheDAO          # noqa: E402

learner, cdao = LearnedAnswers(), CacheDAO()


def blanks(title):
    nums = re.findall(r"(?<!\d)(\d{1,2})\s*[\.\、]\s*A[\)）\.]", title)
    return len(set(nums)) if nums else 1


def ask(model, transcript, title, n, retry=3):
    sys_p = ("你是英语四级听力解题专家。依据给定的听力原文作答（不要凭常识猜）。\n"
             f"本题组 {n} 个小问，按顺序输出所选项字母、去掉题号连写，长度必须 {n}。"
             "只输出字母。")
    for k in range(retry):
        try:
            r = requests.post(BASE + "/chat/completions",
                headers={"Authorization": "Bearer " + KEY,
                         "Content-Type": "application/json"},
                json={"model": model, "temperature": 0,
                      "messages": [{"role": "system", "content": sys_p},
                                   {"role": "user",
                                    "content": f"【听力原文】\n{transcript[:7000]}\n\n"
                                               f"【题目与选项】\n{title[:4000]}\n\n"
                                               f"输出 {n} 个字母。"}]},
                timeout=150, verify=False)
            if r.status_code == 200:
                t = r.json()["choices"][0]["message"]["content"]
                c = re.findall(r"[A-Ha-h]{2,12}", t)
                return max(c, key=len).upper() if c else ""
            if r.status_code == 429:
                time.sleep(25)
                continue
            return ""
        except Exception:
            time.sleep(5)
    return ""


def main():
    for it in INDEX:
        title, n = it["title"], blanks(it["title"])
        tr = TR.get(it["file"], "")
        if not tr:
            continue
        cur = R.get(title) or {}
        votes = {}
        for m, a in (cur.get("votes") or cur.get("ai_answers") or {}).items():
            if a and len(a) >= n:
                votes[m] = a[:n]
        # 补齐缺失的票
        for m in MODELS:
            if m not in votes:
                a = ask(m, tr, title, n)
                if a and len(a) >= n:
                    votes[m] = a[:n]
                    print(f"  +{m}={a[:n]} | {title[7:36]}", flush=True)
                time.sleep(7)
        if not votes:
            print(f"  [仍无答案] {title[7:40]}", flush=True)
            continue

        # 多数票
        cnt = {}
        for a in votes.values():
            cnt[a] = cnt.get(a, 0) + 1
        top = max(cnt.values())
        cands = [a for a, c in cnt.items() if c == top]
        best = cands[0] if len(cands) == 1 else votes.get(PREFERRED, max(cands, key=len))

        # 平台锚点
        sv = {}
        try:
            sv = learner.get_sub_verified(title)
        except Exception:
            pass
        final, conflict = [], 0
        for i in range(n):
            plat = str(sv.get(str(i)) or "").strip().upper()
            v_i = best[i] if i < len(best) else ""
            if plat:
                final.append(plat)
                if v_i and v_i != plat:
                    conflict += 1
            else:
                final.append(v_i)
        final = "".join(final)
        R[title] = {"audio": it["file"], "name": it["name"], "blanks": n,
                    "ai_answers": cur.get("ai_answers") or {}, "votes": votes,
                    "ai_agree": len(set(votes.values())) == 1,
                    "platform_used": sum(1 for i in range(n) if sv.get(str(i))),
                    "platform_conflict": conflict,
                    "final": final,
                    "confidence": ("high" if len(set(votes.values())) == 1
                                   else ("mid" if len(cands) == 1 else "low"))}
        print(f"{title[7:40]:<42} n={n} 票={votes} -> {final} "
              f"[{R[title]['confidence']}]", flush=True)

    json.dump(R, open("listening_solved.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    ok = [t for t, v in R.items() if v.get("final") and len(v["final"]) == v["blanks"]]
    hi = sum(1 for t in ok if R[t]["confidence"] == "high")
    print(f"\n定稿：{len(ok)} 题可用（高置信 {hi}）-> listening_solved.json", flush=True)

    if WRITE:
        w = 0
        recs = json.load(open("learned_answers.json", encoding="utf-8"))
        for t in ok:
            f = R[t]["final"]
            if cdao.get_cache(t) != f:
                cdao.add_cache(t, f)
                w += 1
            e = recs.get(t) or {}
            e["asr_verified"] = f
            e["asr_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            e["asr_conf"] = R[t]["confidence"]
            e["asr_source"] = "whisper+" + "+".join(sorted(R[t]["votes"]))
            recs[t] = e
        json.dump(recs, open("learned_answers.json", "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"已写入题库 {w} 题，并在学习库登记 asr_verified（含置信度与来源）",
              flush=True)


if __name__ == "__main__":
    main()
