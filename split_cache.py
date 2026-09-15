# -*- coding: utf-8 -*-
"""把题库里"未经平台验证"的答案移出主库（2026-09-11）。

背景：AI / 搜题服务的答案此前会被直接写进 cache.json，未经平台判定就成了
"标准答案"。用户实测 3.x 听力题题库里存的就是这类猜测值，下次遇到会直接命中，
连 AI 都不再重新分析，于是错误一直沿用。

本脚本按"学习库是否给出 verified（平台判定全对时记录的答案）"切分：
  - 有 verified 且与题库值一致  -> 保留在 cache.json（平台确认过的）
  - 其余                        -> 移到 cache_guessed.json（猜测值，不再被查询链使用）

用法：python split_cache.py [--dry-run]
"""
import json
import os
import shutil
import sys

DRY = "--dry-run" in sys.argv
BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache.json")
LEARNED = os.path.join(BASE, "learned_answers.json")
GUESSED = os.path.join(BASE, "cache_guessed.json")


def main() -> None:
    with open(CACHE, encoding="utf-8") as f:
        cache = json.load(f)
    try:
        with open(LEARNED, encoding="utf-8") as f:
            learned = json.load(f)
    except Exception:
        learned = {}

    keep, guessed = {}, {}
    for k, v in cache.items():
        rec = learned.get(k) or {}
        ver = str(rec.get("verified") or "").strip()
        if ver and ver == str(v).strip():
            keep[k] = v                      # 平台确认过
        else:
            guessed[k] = v

    # 猜测值并入已有候选文件（保留历史，便于比对）
    if os.path.exists(GUESSED):
        with open(GUESSED, encoding="utf-8") as f:
            old = json.load(f)
        for k, v in old.items():
            guessed.setdefault(k, v)

    listen = sum(1 for k in guessed if "听力" in k)
    print(f"题库原 {len(cache)} 条")
    print(f"  保留（平台已验证）: {len(keep)} 条")
    print(f"  移出（未验证猜测）: {len(guessed)} 条，其中听力题 {listen} 条")
    print()
    print("=== 被移出的条目（前 18 条）===")
    for i, (k, v) in enumerate(list(guessed.items())[:18]):
        print(f"  {k[:56]}  -> {str(v)[:30]!r}")
    if len(guessed) > 18:
        print(f"  ... 其余 {len(guessed) - 18} 条")

    if DRY:
        print("\n[dry-run] 未写入")
        return

    for p in (CACHE, LEARNED):
        if os.path.exists(p) and not os.path.exists(p + ".bak.split"):
            shutil.copy2(p, p + ".bak.split")
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=1)
    with open(GUESSED, "w", encoding="utf-8") as f:
        json.dump(guessed, f, ensure_ascii=False, indent=1)
    print(f"\n已写入：cache.json {len(keep)} 条"
          f" / cache_guessed.json {len(guessed)} 条（备份 *.bak.split）")


if __name__ == "__main__":
    main()
