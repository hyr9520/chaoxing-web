# -*- coding: utf-8 -*-
"""一次性迁移：统一题库/学习库的键形态（2026-09-11 键分裂修复）。

背景：同一道题历史上产生过 3 种键（带 <img> 标签 / 不带 / 空白差异），
导致学习库的错题记录查询时命中不到 → 负反馈失效 → 重复提交已判错的答案。

本脚本做三件事（幂等，可重复运行）：
1. cache.json / learned_answers.json 的键统一走 normalize_title()
2. 归一化后撞在一起的同题记录做合并（学习库：verified 取非空、wrong 去重合并）
3. 清洗题库里的"垃圾答案"（AI 拒答整句、选项内容文本）—— 这些值在
   query_all 读取时本来就会被 plausible_answer 拦掉，留着只是占空间

用法：python migrate_keys.py [--dry-run]
"""
import json
import os
import re
import sys
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api.learned import normalize_title              # noqa: E402
from api.answer_check import plausible_answer        # noqa: E402

DRY = "--dry-run" in sys.argv
BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache.json")
LEARNED = os.path.join(BASE, "learned_answers.json")

# 明显是 AI 拒答而非答案的文本
REFUSE_RE = re.compile(r"无法(回答|作答|访问|确定)|缺少|抱歉|不能(回答|作答)|"
                       r"抱歉|没有提供|please provide|I (can'?t|cannot)", re.IGNORECASE)


def infer_type(title: str) -> str:
    """从题干的题型标记推断题目类型（用于决定答案形态是否合法）。"""
    t = str(title or "")
    if "【单选题】" in t or "【听力题】" in t or "单选题" in t:
        return "single"
    if "【多选题】" in t or "多选题" in t:
        return "multiple"
    if "【判断题】" in t:
        return "judgement"
    if "【填空题】" in t or "【完形填空】" in t:
        return "completion"
    return "unknown"


def is_garbage(title: str, value: str) -> bool:
    """判断缓存值是否是垃圾（拒答文本 / 选项内容文本）。"""
    v = str(value or "").strip()
    if not v:
        return True
    if REFUSE_RE.search(v):
        return True
    qtype = infer_type(title)
    if qtype == "unknown":
        # 题型不明：保守，只删超长或明显是句子的
        return len(v) > 120
    return not plausible_answer(v, qtype)


def backup(path: str) -> None:
    if not os.path.exists(path):
        return
    dst = f"{path}.bak.migrate"
    if not os.path.exists(dst):
        shutil.copy2(path, dst)


def better_value(title: str, new_v, old_v):
    """两个值冲突时选更"合法"的那个（合法 > 垃圾）。"""
    if old_v is None:
        return new_v
    if is_garbage(title, old_v) and not is_garbage(title, new_v):
        return new_v
    return old_v


def prefix_merge(out: dict, merge_fn=None) -> int:
    """把"短键"合并进以它开头的"长键"（同一道题的标题被截断成两种长度）。

    实测：'【听力题】Cet-4-3.20-Exercise.mp3 Questions 19 to 21 are based...'
    与完整的长标题其实是同一道题，短键上存着合法答案 BDA，
    不合并的话查询时用长键永远命中不到。
    merge_fn: (key, 短键的值, 长键的值) -> 合并后的值；默认 better_value。
    返回合并条数。
    """
    merge_fn = merge_fn or better_value
    merged = 0
    for short in sorted(list(out.keys()), key=len):
        if short not in out:
            continue
        cands = [lk for lk in out
                 if len(lk) > len(short) + 5 and lk.startswith(short)]
        if not cands:
            continue
        lk = max(cands, key=len)
        old = out.get(lk)
        out[lk] = merge_fn(lk, out[short], old)
        if old != out[lk]:
            print(f"  [前缀合并] {lk[:50]}  -> {str(out[lk])[:70]}")
        del out[short]
        merged += 1
    return merged


def merge_record(_key, a: dict, b: dict) -> dict:
    """合并同一道题的两条学习记录（a=短键那条，b=长键那条）。"""
    if b is None:
        return a
    rec = dict(b)
    if not rec.get("verified") and a.get("verified"):
        rec["verified"] = a["verified"]
        rec["verified_at"] = a.get("verified_at")
    wl = list(rec.get("wrong") or [])
    for w in (a.get("wrong") or []):
        if w not in wl:
            wl.append(w)
    if wl:
        rec["wrong"] = wl[:5]
    rank = {"cuo": 3, "bandui": 2, "unknown": 1, "dui": 0}
    if rank.get(str(a.get("last_mark")), 0) > rank.get(str(rec.get("last_mark")), 0):
        rec["last_mark"] = a.get("last_mark")
        rec["last_score"] = a.get("last_score")
    rec["updated_at"] = max(str(a.get("updated_at") or ""),
                            str(b.get("updated_at") or ""))
    return rec


def migrate_cache() -> dict:
    with open(CACHE, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    renamed = 0
    for k, v in data.items():
        nk = normalize_title(k)
        if nk != k:
            renamed += 1
        out[nk] = better_value(nk, v, out.get(nk))
    collapsed = len(data) - len(out)
    merged = prefix_merge(out)

    final = {}
    dropped = 0
    for k, v in out.items():
        if is_garbage(k, v):
            dropped += 1
            print(f"  [丢弃脏答案] {k[:55]} -> {str(v)[:60]!r}")
            continue
        final[k] = v
    print(f"题库: {len(data)} -> {len(final)} 条"
          f"（键归一化 {renamed}、同键合并 {collapsed}、前缀合并 {merged}、"
          f"丢弃脏答案 {dropped}）")
    return final


def migrate_learned() -> dict:
    if not os.path.exists(LEARNED):
        return {}
    with open(LEARNED, encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    merged = 0
    for k, v in data.items():
        nk = normalize_title(k)
        if nk in out:
            merged += 1
            out[nk] = merge_record(nk, v, out[nk])
            print(f"  [合并学习记录] {nk[:55]}  wrong={out[nk].get('wrong')} "
                  f"verified={out[nk].get('verified')!r}")
            continue
        out[nk] = v
    merged += prefix_merge(out, merge_record)
    print(f"学习库: {len(data)} -> {len(out)} 条（合并 {merged}）")
    return out


def main() -> None:
    backup(CACHE)
    backup(LEARNED)
    print("=== 迁移预览 ===" if DRY else "=== 开始迁移 ===")
    cache_out = migrate_cache()
    learned_out = migrate_learned()
    if DRY:
        print("\n[dry-run] 未写入文件")
        return
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache_out, f, ensure_ascii=False, indent=1)
    if learned_out:
        with open(LEARNED, "w", encoding="utf-8") as f:
            json.dump(learned_out, f, ensure_ascii=False, indent=1)
    print("\n已写入。备份：cache.json.bak.migrate / learned_answers.json.bak.migrate")


if __name__ == "__main__":
    main()
