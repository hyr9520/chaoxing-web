# -*- coding: utf-8 -*-
"""学习库 → TikuAdapter 题库 单向同步。

【为什么需要这个脚本】
程序有两条独立的"记住答案"通道，以前**没有打通**：

  学习库 learned_answers.json
      平台判对后由 api/learned.py 写入（verified 字段）。
      只在**当前实例**内生效，换账号/换场景就丢了。

  TikuAdapter 题库 tiku.db
      所有实例共用。但**没有任何代码往里写**（只有我手工回灌过一次）。

结果：某个账号把题做对了、平台确认全对，学习库确实记下了，
      题库却还是空的 → 另一个账号遇到同一道题，照样搜不到。
      这就是"我明明全做对过，题库怎么还搜不到"的第 4 层原因。

本脚本把学习库里所有 verified 答案（含高分听力题）回灌进 tiku.db，
按 TikuAdapter 的真实 hash 算法写入，让所有实例都能搜到。

【hash 算法】（读源码 internal/search/db.go 确认）
    hash = md5( 题干 + json(sorted(选项)) + 题型 + 平台 )   # plat 默认 0
【查询前提】（internal/controller/search.go）
    URL 必须带 ?use=local，否则本地库根本不参与查询。

用法：
    python _sync_learned_to_tiku.py --dry-run     # 只看会同步什么
    python _sync_learned_to_tiku.py               # 从全部实例汇总后回灌
    python _sync_learned_to_tiku.py --accs 1,2    # 只取指定实例
"""
import argparse
import glob
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"D:\work\2026-09-09-13-50-17"
DATA = os.path.join(ROOT, "chaoxing_data")
ROOT_LEARNED = os.path.join(ROOT, "chaoxing", "learned_answers.json")
TIKU_DB = os.path.join(ROOT, "tikuAdapter", "tiku.db")

# 题型判定要跟 api/answer.py 的 TikuAdapter._query 完全一致，否则 hash 对不上：
#   single→0  multiple→1  completion→2  judgement→3  其它(含听力)→4
import re
_LISTEN_RE = re.compile(r"听力|\.mp3\b|Questions\s+\d+\s+to\s+\d+", re.I)
_JUDGE_RE = re.compile(r"判断题")
_FILL_RE = re.compile(r"填空题")
_PICK_RE = re.compile(r"单选题")
_MULTI_RE = re.compile(r"多选题")


def qtype_of(title: str) -> int:
    # 2026-09-14 修 bug：原先只判听力和判断题，**填空题被归进默认的 0（单选）**，
    # 于是回灌题库时写的 hash 用的是 type=0，而查询侧 TikuAdapter._query 对
    # 填空题算的是 type=2 —— 两边永久对不上，填空题永远搜不到。
    # 顺序要紧：听力题题干里也可能出现"填空题"字样，必须先判听力。
    if _LISTEN_RE.search(title):
        return 4
    if _JUDGE_RE.search(title):
        return 3
    if _FILL_RE.search(title):
        return 2
    if _MULTI_RE.search(title):
        return 1
    return 0


def tiku_hash(question: str, options, type_: int, plat: int = 0) -> str:
    opts = json.dumps(sorted(list(options or [])), ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.md5(("%s%s%d%d" % (question, opts, int(type_), int(plat)))
                       .encode("utf-8")).hexdigest()


def collect(paths):
    """从多个 learned_answers.json 汇总 verified 答案。

    返回 {title: {"answer":..., "options":..., "qtype":...}}
    2026-09-14 起学习库会带 options（回灌题库必需）；老记录没有 options 的，
    只有听力题能安全回灌（听力题选项为空），其余跳过并计数。
    """
    out = {}
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception as e:
            print("  跳过 %s：%s" % (p, e))
            continue
        for k, v in (d or {}).items():
            ans = (v or {}).get("verified")
            if not ans:
                continue
            k = k.strip()
            opts = str((v or {}).get("options") or "").strip()
            rec = out.get(k)
            if rec is None:
                out[k] = {"answer": str(ans).strip(), "options": opts}
            elif opts and not rec.get("options"):
                # 补上别处存到的选项
                rec["options"] = opts
    return out


def parse_options(raw: str) -> list:
    """把学习库里的选项文本，转成查询侧会发给题库的那种列表。

    与 api/answer.py TikuAdapter._query 保持一致：
      按行拆 → 去掉 "A." / "A、" / "A)" 前缀 → 去掉空行
    """
    if not raw:
        return []
    out = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        out.append(re.sub(r"^[A-Za-z][.)．、:：]?\s?", "", line))
    return out


def letters_to_text(answer: str, opts: list) -> str:
    """把「选项字母」答案翻译成「答案文本」。

    2026-09-15 修 bug（严重）：此前直接把学习库里的 verified（形如 "B"）
    写进 tiku.answer。但 TikuAdapter 的 answer 字段语义是**答案文本**：
    它拿 answer 里的字符串去 options 里做匹配，匹配不到就退化成**返回第一
    个选项**。于是 "B" 匹配不到任何选项 → 接口 bestAnswer 恒等于 A 选项，
    **等于每次都灌一个错答案**。

    实测（2026-09-15）：回灌的 17 条（id 126~142）全部中招，
    例如「北斗三号系统的特点是()」库里 answer=["C"]，
    但接口返回 bestAnswer=["仅覆盖亚太地区"]（A 选项），而正确答案是
    「实现全球覆盖」；对照 104 条文本式入库的老数据（如英语题
    answer=["origin"]）则完全正常 —— 差别就在字母 vs 文本。

    转换规则：
      - 纯字母（单个或多个，如 "B" / "ACD"）→ 按选项顺序取对应文本，多选用换行连接
      - 已经是文本 → 原样返回
      - 字母超出选项范围或取不到 → 返回原值（交由调用方决定是否跳过）
    """
    s = str(answer or "").strip()
    if not s:
        return s
    if not re.fullmatch(r"[A-Za-z]+", s):
        return s  # 已是文本（如 "origin"）
    parts = []
    for ch in s.upper():
        idx = ord(ch) - ord("A")
        if 0 <= idx < len(opts):
            parts.append(str(opts[idx]))
        else:
            return s  # 越界，宁可不改也不要写错
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--accs", default="")
    args = ap.parse_args()

    paths = []
    if os.path.exists(ROOT_LEARNED):
        paths.append(ROOT_LEARNED)
    for d in sorted(glob.glob(os.path.join(DATA, "acc*"))):
        name = os.path.basename(d)
        if args.accs and name not in args.accs.split(","):
            continue
        paths.append(os.path.join(d, "learned_answers.json"))

    print("扫描 %d 个学习库文件" % len(paths))
    learned = collect(paths)
    print("汇总到 verified 答案 %d 条" % len(learned))

    con = sqlite3.connect(TIKU_DB)
    cur = con.cursor()
    cur.execute("SELECT question FROM tiku")
    exist = {r[0] for r in cur.fetchall()}
    print("题库现有 %d 条" % len(exist))

    add, skip, noopt = [], 0, []
    for q, rec in sorted(learned.items()):
        if q in exist:
            skip += 1
            continue
        t = qtype_of(q)
        # 2026-09-14 【重要修正】听力题必须用空选项回灌，别被"题组带选项"迷惑：
        #   听力题(type=4)：程序发来的 q_info['options'] 会把每组小题的 A/B/C/D
        #     拼成长串（3.5 题 12 个），但 **查询侧已强制 type=4 传 []**（见
        #     api/answer.py TikuAdapter._query），所以回灌也必须用 []。
        #     早期误以为"听力题选项嵌在题干里"只是运气对上，实测若照抄题组
        #     选项反而全部落空（22 条实测：带选项 0/22、空选项 22/22）。
        #   判断题(type=3)：查询侧 options 为空串 → 产出 [] → 回灌用 []  ✓
        # 选择题（single/multiple）选项是答案的载体，缺了就一定对不上，跳过。
        opts = [] if t == 4 else parse_options(rec.get("options"))
        if not opts and t not in (3, 4):
            noopt.append(q)
            continue
        # 2026-09-15 修 bug：tiku.answer 必须写**答案文本**，不能写选项字母。
        # 写字母会让 TikuAdapter 匹配失败并退化成返回第一个选项（= 错答案）。
        # 详见 letters_to_text 的 docstring。
        ans_text = letters_to_text(rec["answer"], opts)
        if t in (0, 1) and not ans_text:
            noopt.append(q)
            continue
        add.append((q, t, ans_text, opts))

    print("-" * 70)
    print("将新增 %d 条，跳过已存在 %d 条，因缺选项跳过 %d 条"
          % (len(add), skip, len(noopt)))
    for q, t, ans, opts in add[:8]:
        print("   [t%d] %-46s -> %-8s opts=%d"
              % (t, q[:46], ans, len(opts)))
    if len(add) > 8:
        print("   … 还有 %d 条" % (len(add) - 8))
    if noopt:
        print("\n   ⚠️ 缺选项（需程序跑一遍重新记录，或等学习库补上 options）：")
        for q in noopt[:5]:
            print("      %s" % q[:70])
        if len(noopt) > 5:
            print("      … 共 %d 条" % len(noopt))

    if args.dry_run:
        print("\n[dry-run] 未写入")
        return
    if not add:
        print("\n无需新增。")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")

    # 2026-09-15 【入库前强制自检】—— 防止"字母式答案"再次混入。
    # 血的教训：answer 字段必须是**答案文本**。一旦写成字母，TikuAdapter
    # 匹配不到就退化返回第一个选项，界面一切正常但**每题都在提交错答案**，
    # 极难察觉（本次是靠端到端逐条比对才挖出来）。这里在落库前硬拦。
    bad = [(q, t, ans) for q, t, ans, opts in add
           if t in (0, 1) and re.fullmatch(r"[A-Za-z]+", str(ans).strip())]
    if bad:
        print("\n" + "!" * 60)
        print("检测到 %d 条答案仍是「字母式」，可能无法被 TikuAdapter 正确解析：" % len(bad))
        for q, t, ans in bad[:10]:
            print("   [t%d] %-46s -> %r" % (t, q[:46], ans))
        print("已中止写入。请确认 letters_to_text() 是否覆盖了这些形态。")
        print("!" * 60)
        return

    bak = TIKU_DB + ".bak.sync." + ts
    shutil.copy2(TIKU_DB, bak)
    print("\n已备份 -> %s" % os.path.basename(bak))

    for q, t, ans, opts in add:
        cur.execute(
            "INSERT INTO tiku (question,type,options,answer,plat,hash,"
            "course_name,extra) VALUES (?,?,?,?,?,?,?,?)",
            (q, t, json.dumps(opts, ensure_ascii=False),
             json.dumps([ans], ensure_ascii=False), 0,
             tiku_hash(q, opts, t, 0), "", ""))
    con.commit()
    total = cur.execute("SELECT count(*) FROM tiku").fetchone()[0]
    con.close()
    print("已新增 %d 条；题库现有 %d 条" % (len(add), total))
    print("\n⚠️ 提醒：重启 TikuAdapter 后新记录才生效（它是启动时加载）。")


if __name__ == "__main__":
    main()
