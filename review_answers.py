# -*- coding: utf-8 -*-
"""复核听力答案：平台锚点校准 + 第三模型投票（用户要求"全部正确答案入题库"）。

为什么需要：
  两个模型（2.5-flash / 3.0-flash）在 10 个题组上各差一个小问，谁对无法自证。
  但 4 个题组有"平台已确认的小问"，可以当锚点判断模型可靠性
  （实测 3.1-Ex01 平台确认问2=A -> 2.5-flash 正确、3.0-flash 错误）。
  对其余无锚点的题，用第三个模型投票，取"多数意见"。

产出：
  listening_solved.json 增加 reviewed / final2 / confidence 字段
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
R_PATH = "listening_solved.json"
TR_PATH = "transcripts.json"
RESULTS = json.load(open(R_PATH, encoding="utf-8"))
TR = json.load(open(TR_PATH, encoding="utf-8"))

_CFG = json.load(open("ui_config.json", encoding="utf-8"))
# 账号不写死（避免随仓库外泄）：用环境变量 CK_ACCOUNT 指定手机号
_M = [p for p in _CFG["profiles"]
      if p.get("username") == os.environ.get("CK_ACCOUNT", "")]
if not _M:
    raise SystemExit("请用环境变量 CK_ACCOUNT 指定学习通账号（手机号）")
_P = _M[0]
KEY, BASE = _P["ai_key"], _P["ai_endpoint"].rstrip("/")
THIRD = "agnes-2.5-pro"          # 第三票（与前两个不同档位）
TIE = "agnes-2.5-pro-beta"       # 需要打破平局时再用

from api.learned import LearnedAnswers   # noqa: E402
learner = LearnedAnswers()


def ask(model, transcript, title, n, retry=3):
    sys_p = (
        "你是英语四级听力解题专家。下面给你听力录音的文字转写（语音识别结果）、"
        "以及题目与选项。**必须依据听力原文作答**，不要凭常识猜。\n"
        f"本题组共 {n} 个小问，按小问顺序输出所选字母、去掉题号连写，"
        f"长度必须为 {n}（如 3 个小问依次 C、A、D 就输出 CAD）。\n"
        "只输出字母，不要解释、空格、标点。"
    )
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
                                               f"请输出 {n} 个字母。"}]},
                timeout=150, verify=False)
            if r.status_code == 200:
                txt = r.json()["choices"][0]["message"]["content"]
                c = re.findall(r"[A-Ha-h]{2,12}", txt)
                if c:
                    return max(c, key=len).upper()
                return ""
            if r.status_code == 429:
                time.sleep(20 + k * 15)
                continue
            return ""
        except Exception:
            time.sleep(5)
    return ""


def main():
    changed = 0
    for title, v in RESULTS.items():
        n = v["blanks"]
        tr = TR.get(v["audio"], "")
        if not tr:
            continue
        votes = {m: a[:n] for m, a in v["ai_answers"].items() if len(a) >= n}
        # 若某模型答案比小问数短，尝试补齐长度（截断到 n 后仍短则丢弃）
        if not votes:
            votes = {m: a for m, a in v["ai_answers"].items() if a}
        sv = {}
        try:
            sv = learner.get_sub_verified(title)
        except Exception:
            pass

        need_vote = len(set(votes.values())) > 1 or len(votes) < 2
        if need_vote:
            a3 = ask(THIRD, tr, title, n)
            if a3 and len(a3) >= n:
                votes[THIRD] = a3[:n]
                print(f"  第三票 {THIRD}={a3[:n]} | {title[7:38]}", flush=True)
            time.sleep(6)

        # 多数票
        best = ""
        if votes:
            cnt = {}
            for a in votes.values():
                cnt[a] = cnt.get(a, 0) + 1
            top = max(cnt.values())
            cands = [a for a, c in cnt.items() if c == top]
            best = cands[0] if len(cands) == 1 else max(cands, key=len)
            if len(cands) > 1:
                a4 = ask(TIE, tr, title, n)
                if a4 and len(a4) >= n and a4[:n] in cands:
                    best = a4[:n]
                    votes[TIE] = a4[:n]
                time.sleep(6)

        # 平台锚点校准（平台判定优先级最高）
        final, conflict = [], 0
        for i in range(n):
            plat = str(sv.get(str(i)) or "").strip().upper()
            vote_i = best[i] if i < len(best) else ""
            if plat:
                final.append(plat)
                if vote_i and vote_i != plat:
                    conflict += 1
            else:
                final.append(vote_i)
        v["votes"] = votes
        v["reviewed"] = "".join(final)
        v["platform_conflict"] = conflict
        v["confidence"] = ("high" if len(set(votes.values())) == 1
                           else ("mid" if len(votes) >= 2 else "low"))
        if v.get("final") != v["reviewed"]:
            changed += 1
        print(f"{title[7:42]:<44} n={n} 票={votes} -> 最终={v['reviewed']} "
              f"[{v['confidence']}] 冲突{conflict}", flush=True)

    json.dump(RESULTS, open(R_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n复核完成：{len(RESULTS)} 题，修改 {changed} 题 -> {R_PATH}", flush=True)


if __name__ == "__main__":
    main()
