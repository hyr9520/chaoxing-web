# -*- coding: utf-8 -*-
"""清洗 TikuAdapter 题库（tiku.db）里的脏答案（2026-09-11）。

背景：tiku.db 是外部导入的题库，实测 12 条听力题的答案是
  '["A、\n\t\t\t\t\t They can respond to humans\' questions."]'
这类"选项内容 + 空白"的垃圾（和 cache.json 里清掉的是同一批），
搜题服务会把它们当答案返回，导致提交错误答案。

规则：听力题（多小问）的答案必须是纯选项字母连写（如 DCBD）；
非纯字母的一律删除（删掉后搜题服务返回空 -> 走 AI，而不是返回垃圾）。

用法：python clean_tiku_db.py [--dry-run]
"""
import json
import os
import re
import shutil
import sqlite3
import sys

DRY = "--dry-run" in sys.argv
DB = r"D:\work\2026-09-09-13-50-17\tikuAdapter\tiku.db"
LISTEN_RE = re.compile(r"Questions\s+\d+\s+to\s+\d+|\.mp3|听力")
LETTER_RE = re.compile(r"^[A-Za-z]+$")


def unwrap(raw):
    """tiku.db 的 answer 字段是 JSON 数组字符串，取出真实值。"""
    try:
        v = json.loads(raw)
    except Exception:
        return str(raw or "").strip()
    if isinstance(v, list):
        return " ".join(str(x) for x in v).strip()
    return str(v or "").strip()


def main() -> None:
    con = sqlite3.connect(DB)
    rows = con.execute("select id, question, answer from tiku").fetchall()
    bad, good = [], []
    for i, q, a in rows:
        if not LISTEN_RE.search(str(q or "")):
            good.append(i)
            continue
        val = unwrap(a)
        if LETTER_RE.fullmatch(val):
            good.append(i)
        else:
            bad.append((i, str(q)[:44], val[:44]))
    print(f"tiku.db 共 {len(rows)} 条 | 听力题脏答案 {len(bad)} 条")
    for i, q, v in bad:
        print(f"  [{i}] {q}\n        -> {v!r}")
    if DRY:
        print("\n[dry-run] 未修改")
        return
    if bad:
        con.execute("BEGIN")
        con.executemany("delete from tiku where id = ?", [(i,) for i, _, _ in bad])
        con.commit()
    left = con.execute("select count(*) from tiku").fetchone()[0]
    con.close()
    print(f"\n已删除 {len(bad)} 条，剩余 {left} 条")


if __name__ == "__main__":
    if not DRY:
        shutil.copy2(DB, DB + ".bak.clean")
        print("已备份 tiku.db.bak.clean")
    main()
