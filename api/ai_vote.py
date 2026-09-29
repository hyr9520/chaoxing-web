# -*- coding: utf-8 -*-
"""AI 答案二次确认：多槽位投票裁决（2026-09-28）。

## 为什么要做

实测归因（2026-09-28，139 次章节检测的日志反推）：**仅由 AI 作答**的题
错误率明显高于题库命中的题。

| 那次测验 | 答案来源 | 结果 |
|---|---|---|
| 得 10.0 分 | 3 题全部走 AI（题库未命中）| 几乎全错 |
| 得 33.3 分 | 1 题题库命中（**对了**）+ 2 题走 AI | 3 题对 1 |

⇒ 题库命中的题基本是对的，AI **单次输出**不能直接拿去赌分。
⚠️ 日志里那句「题库覆盖率 100%」是**误导指标** —— 它只说明每题都拿到了
一个答案，不代表答案是对的。

## 为什么是「3 票」而不是「2 票」

用户提出的关键质疑：**两次不一致怎么办？**

- 2 票（偶数）天然可能出现 1:1 平票，而**平票时信息量为零**（等于抛硬币）
- 3 票（奇数）必有严格多数 ⇒ 一定能裁决

所以流程不是"两次一致才用"，而是：

```
票1 = 主槽位模型
  └ 票2 = 换一个**不同签名**（Key/Endpoint/Model）的槽位
       ├ 一致 → 采用（置信=high，2/2）
       └ 不一致 → 票3 = 再换第三个槽位 → 取多数（置信=majority，2:1）
            └ 槽位不够 / 仍无多数 → 按题型兜底（见下）
```

⚠️ 第 2 票必须换**不同槽位**：同一个模型重问几乎必然给同一个答案，
那不叫投票，只是浪费一次请求。

## 平票 / 无多数时的兜底（每条都由真实数据支撑，不是拍脑袋）

### 多选题 → 逐选项多数决，争议选项倾向「不选」

实测计分规则（139 次检测反推）：**未含多选题**的检测得分永远是整数倍
（20 / 25 / 33.3 的倍数），而**含多选题**的检测出现了 `2.5` / `3.5` / `4.5`
个题的分 ⇒ **多选题漏选（部分正确）给一半分**。

于是对「有争议的选项」算期望值（设它有 50% 概率是正确答案）：

| 做法 | 期望得分 |
|---|---|
| **选上它**：50% 全对(1.0) + 50% 错选(0) | **0.50** |
| **不选它**：50% 全对(1.0) + 50% 漏选(0.5) | **0.75** |

⇒ **不选更优**。所以未达严格多数的选项**一律不选**。
（这比"整题投票"合理得多：多选题两个模型答案不同，往往是"一个模型
多选了一个选项"，逐选项投票能精确定位到那个选项。）

### 判断题 → 取「对」

平台**已判定过**的 273 道判断题里：「对」164 道（60.1%）/「错」109 道（39.9%）。
无其他信息时按这个先验取「对」。

⚠️ 我原本以为"判断题多是陷阱题、该选错"，**实测数据直接推翻了** ——
所以这条是量出来的，不是猜的。日志会记录每次兜底，将来可继续验证。

### 单选题 / 填空 / 简答 → 取主槽位答案

没有更强的证据可用时，取池中**主模型**（能力最强的那一档）的输出，
而不是随机或按选项位置瞎蒙。

## 统一形态

投票在**映射后的字母形态**上做（`ABCD` / `正确`），不在 AI 原始输出上做 ——
否则一个模型输出选项内容、另一个输出字母，两票根本无法对齐。
"""
import io
import json
import os
import re
import time
from collections import Counter

# ── 判断题的对/错表述（与 api/base.py 的 judge 判定保持同源）──
JUDGE_T = {"对", "正确", "是", "√", "true", "t", "yes", "y", "1", "对的", "正确的"}
JUDGE_F = {"错", "错误", "否", "×", "x", "false", "f", "no", "n", "0",
           "不对", "错的", "错误的"}

# 比对时忽略的排版字符（全角/半角标点、空白）
_LOOSE = re.compile(r"[\s，,、;；。.：:·|/]+")

# 多选答案的切分符
_MULTI_SEP = re.compile(r"[\n\r,，、;；]+")

# 连写字母串（"ACD"）
_LETTERS = re.compile(r"^[A-Za-z]+$")

# 记录文件（投票裁决留痕，供事后归因）
LOG_NAME = "ai_vote_log.jsonl"

# 明显更弱的小参数量模型（4B / 7B / mini / tiny…）
_WEAK_MODEL = re.compile(r"(?:^|[-_/])(?:[1-9]b|mini|tiny|nano|small)(?:$|[-_/])",
                         re.I)


def is_weak_model(model: str) -> bool:
    """模型名是否像「明显更弱的小模型」（4B/7B/mini/tiny…）。

    用途：投票选槽位时把这类模型**排到最后**。
    让一个 4B 小模型去给 deepseek-v4.1-flash 的答案"投票"，
    很可能把本来正确的多数票拆散 —— 那不是独立意见，是噪声。
    当然，池里只有它可做第二意见时，仍比"完全不验证"好。
    """
    return bool(_WEAK_MODEL.search(str(model or "")))


def _loose(s) -> str:
    """比对键：忽略空白与中英文标点差异。"""
    return _LOOSE.sub("", str(s or "")).strip()


def judge_norm(s):
    """把判断题的各种表述归一成 'T' / 'F'；认不出返回原文（小写去空白）。"""
    t = _loose(s).lower()
    if t in JUDGE_T:
        return "T"
    if t in JUDGE_F:
        return "F"
    return t


def split_multi(ans) -> set:
    """把多选题答案拆成「选项字母/元素」集合。

    兼容三种形态（实测都出现过）：
      "ACD"           → {'A','C','D'}
      "A\\nC\\nD"      → {'A','C','D'}
      "革命性\\n纪律性" → {'革命性','纪律性'}
    """
    s = str(ans or "").strip()
    if not s:
        return set()
    parts = [p.strip() for p in _MULTI_SEP.split(s) if p.strip()]
    if len(parts) == 1 and _LETTERS.match(parts[0]) and len(parts[0]) > 1:
        return set(parts[0].upper())
    out = set()
    for p in parts:
        if _LETTERS.match(p) and len(p) > 1:
            out |= set(p.upper())
        else:
            out.add(p)
    return out


def same(a, b, q_type) -> bool:
    """两票是否算「同一个答案」（按题型用不同的等价判定）。"""
    if q_type == "multiple":
        sa, sb = split_multi(a), split_multi(b)
        return bool(sa) and sa == sb
    if q_type == "judgement":
        return judge_norm(a) == judge_norm(b)
    return _loose(a) == _loose(b)


def merge_multi(ans_list) -> tuple:
    """多选逐选项多数决。

    规则：**严格多数**（得票 > 总票数一半）的选项才保留。
    - 3 票 → 需 ≥2 票
    - 2 票 → 需 2 票（全票一致）
    未过半的选项**一律不选** —— 依据见模块 docstring 的期望值分析
    （漏选得一半分 > 错选得 0 分）。

    返回 (选项列表[按得票降序], 各选项得票 Counter)。
    """
    n = len(ans_list)
    cnt = Counter()
    for a in ans_list:
        for o in split_multi(a):
            cnt[o] += 1
    if n <= 0:
        return [], cnt
    picked = [o for o, c in cnt.items() if c * 2 > n]
    picked.sort(key=lambda o: (-cnt[o], o))
    return picked, cnt


def _order_letters(picked) -> str:
    """把选项字母拼成平台要的连写形态（字母序，和 map_multiple_answer 一致）。"""
    return "".join(sorted(x.upper() for x in picked if len(str(x)) == 1))


def _fallback(votes, q_type) -> tuple:
    """无严格多数时的兜底。返回 (答案, 说明)。"""
    # 多选：先试逐选项多数决（可能有部分选项过半）
    if q_type == "multiple":
        picked, cnt = merge_multi([a for _, a in votes])
        tpl = " / ".join(f"{k}:{v}票" for k, v in sorted(cnt.items()))
        if picked:
            return _order_letters(picked), f"逐选项多数决（{tpl}）"
        return votes[0][1], f"逐选项无选项过半（{tpl}）→ 取主槽位"

    # 判断题：按平台先验（对 60.1% / 错 39.9%）取「对」
    if q_type == "judgement":
        for _, a in votes:
            if judge_norm(a) == "T":
                return a, "判断题平票 → 按平台先验（对 60.1%）取「对」"
        return votes[0][1], "判断题平票且票里无「对」表述 → 取主槽位"

    # 单选 / 填空 / 简答 / 未知：取主槽位（能力最强的那一档）
    return votes[0][1], "无严格多数 → 取主槽位答案"


def decide(votes, q_type="unknown") -> dict:
    """投票裁决。

    参数
    ----
    votes : list[(tag, answer)]
        按优先级排列；votes[0] 是**主槽位**（兜底时会用到）。
        answer 必须是**已统一成字母/对错词**的形态（见模块 docstring）。
    q_type : 题型（single / multiple / judgement / completion / ...）

    返回
    ----
    dict(answer, confidence, detail)
        confidence:
          'none'      无任何候选
          'single'    只有 1 票（池中无第二个可用槽位）
          'high'      全票一致（2/2 或 3/3）—— 最可信
          'majority'  奇数票多数决（2:1）
          'merged'    多选逐选项多数决
          'fallback'  题型兜底（无严格多数）
    """
    vs = [(str(t), str(a).strip()) for t, a in (votes or [])
          if a is not None and str(a).strip()]
    if not vs:
        return {"answer": None, "confidence": "none", "detail": "无候选"}
    if len(vs) == 1:
        return {"answer": vs[0][1], "confidence": "single",
                "detail": "仅 1 票（池中无第二个可用槽位，未做交叉验证）"}

    # ① 多选题：**必须逐选项裁决**，不能走下面的整题分组。
    #    原因：{A,C} 与 {A,C,D} 既不是"同一个答案"也不是"毫无关系"，
    #    整题分组会把它们算成两个不同的组 → 三票三个组 → 永远无多数
    #    → 白白落到 fallback，投票等于没做（2026-09-28 单测抓出来的）。
    if q_type == "multiple":
        if all(same(vs[0][1], a, "multiple") for _, a in vs[1:]):
            return {"answer": vs[0][1], "confidence": "high",
                    "detail": f"{len(vs)}/{len(vs)} 票一致"}
        picked, cnt = merge_multi([a for _, a in vs])
        tpl = " / ".join(f"{k}:{v}票" for k, v in sorted(cnt.items()))
        if picked:
            _m = _order_letters(picked)
            return {"answer": _m, "confidence": "merged",
                    "detail": f"逐选项多数决（{tpl}）→ {_m}"}
        return {"answer": vs[0][1], "confidence": "fallback",
                "detail": f"逐选项无选项过半（{tpl}）→ 取主槽位"}

    # 按「是否同一个答案」分组
    groups = []          # [{'ans': 代表答案, 'n': 票数, 'tags': [槽位标签]}]
    for tag, a in vs:
        for g in groups:
            if same(g["ans"], a, q_type):
                g["n"] += 1
                g["tags"].append(tag)
                break
        else:
            groups.append({"ans": a, "n": 1, "tags": [tag]})
    groups.sort(key=lambda g: -g["n"])

    # ① 全票一致
    if len(groups) == 1:
        g = groups[0]
        return {"answer": g["ans"], "confidence": "high",
                "detail": f"{g['n']}/{len(vs)} 票一致"}

    # ② 有严格多数（奇数票必有；偶数票平票时落到 ③）
    if groups[0]["n"] * 2 > len(vs):
        g = groups[0]
        others = "、".join(str(x["ans"])[:40] for x in groups[1:])
        return {"answer": g["ans"], "confidence": "majority",
                "detail": f"多数决 {g['n']}/{len(vs)}（少数：{others}）"}

    # ③ 平票 / 无多数 → 题型兜底
    ans, why = _fallback(vs, q_type)
    kinds = "、".join(str(x["ans"])[:30] for x in groups)
    return {"answer": ans, "confidence": "fallback",
            "detail": f"{why}（票型：{kinds}）"}


# ── 留痕（供事后核对"投票到底有没有让正确率变好"）──────────────

def log_vote(data_root: str, record: dict) -> None:
    """把一次投票裁决追加到 jsonl。失败绝不影响主流程。"""
    try:
        if not data_root:
            return
        os.makedirs(data_root, exist_ok=True)
        p = os.path.join(data_root, LOG_NAME)
        rec = dict(record)
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        with io.open(p, "a", encoding="utf-8") as f:
            # 注：jsonl 追加（单行 <4KB）在 Windows/POSIX 下都是原子的，
            # 多实例写入不会互相截断（且各实例有自己的 data_root）。
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
