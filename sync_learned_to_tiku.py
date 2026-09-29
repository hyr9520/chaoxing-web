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

# 2026-09-15 【必须容错】此前这行是裸的 `sys.stdout.reconfigure(...)`。
# 命令行下没问题，但网关是用 pythonw.exe（无控制台）启动的 —— 此时
# sys.stdout 是 None，直接抛 AttributeError:
#     'NoneType' object has no attribute 'reconfigure'
# 结果自动回灌一启动就崩，而且因为异常被 _sync_once 吞掉，**表面完全无症状**，
# 只在 /api/overview 的 sync.last_res 里留一句"异常：..."。
# 这个 bug 手工跑永远遇不到（手工跑必有控制台），仅靠"挂上去看"绝对发现不了。
try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
# 同理：无控制台时 stderr 也可能是 None，别让日志路径再崩一次。
try:
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = r"D:\work\2026-09-09-13-50-17"
DATA = os.path.join(ROOT, "chaoxing_data")
ROOT_LEARNED = os.path.join(ROOT, "chaoxing", "learned_answers.json")
TIKU_DB = os.path.join(ROOT, "tikuAdapter", "tiku.db")

# 题型判定要跟 api/answer.py 的 TikuAdapter._query 完全一致，否则 hash 对不上：
#   single→0  multiple→1  completion→2  judgement→3  其它(含听力)→4
import re
# 🔴 2026-09-28：题库矫正（清洗 UI 污染 + 同题冲突仲裁）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api import tiku_fix                                              # noqa: E402
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


def collect(paths, _p=print):
    """从多个 learned_answers.json 汇总 verified 答案。

    返回 {title: {"answer":..., "options":..., "qtype":...}}

    2026-09-14 起学习库会带 options（回灌题库必需）；老记录没有 options 的，
    只有听力题能安全回灌（听力题选项为空），其余跳过并计数。

    2026-09-15 【自动回灌加固】两处要害改动，都是"人在跑"变"机器在跑"后的必然要求：

    ① 存疑答案一律不回灌（skip_suspect 计数）
       api/learned.py 规定：判错时撤销 verified，但 bandui（部分对）时如果
       verified 与本次提交值**不同**，则**保留待考**。这是设计内的保守行为 ——
       可它会让"最近一次作答已经错了"的答案继续挂在 verified 上。
       实测 acc3 有 3 条这类记录（听力 3.7/3.9/3.10，verified_src=restore_20260911
       从备份恢复的旧值，last_mark=bandui）。它们已经躺在题库里了，本次回灌
       恰好因"已存在"被跳过、没造成新污染 —— 但**靠巧合不算安全**：
       一旦换个账号再答错、题目又恰好不在库里，这条存疑答案就会灌进全局题库，
       污染所有实例。所以这里显式拦：最近一次判过错的，不进全局库。

       注意范围：只排除 last_mark ∈ {cuo, bandui} 的。last_mark 是 dui/None
       的正常放行 —— 不误伤大多数干净记录。

    ② 同题多来源冲突时，取"最新验证"的那份（原来是先到先得）
       原实现按 paths 顺序首次出现即定型，而 paths[0] 恒为**根目录的旧文件**
       （chaoxing/learned_answers.json，Sep 14 的老数据）。等于让旧数据压过
       各实例的新答案。实测当前两处恰好不冲突（冲突数 0），但这是运气。
       改为按 verified_at 取最新，无 verified_at 的排最后。
    """
    _SUSPECT = ("cuo", "bandui")
    out = {}
    suspect = 0
    for p in paths:
        if not os.path.exists(p):
            continue
        try:
            d = json.load(io.open(p, encoding="utf-8"))
        except Exception as e:
            _p("  跳过 %s：%s" % (p, e))
            continue
        for k, v in (d or {}).items():
            v = v or {}
            ans = v.get("verified")
            if not ans:
                continue
            # ① 存疑拦截：最近一次作答结果是错/部分错 → 不信任这条 verified
            if str(v.get("last_mark") or "") in _SUSPECT:
                suspect += 1
                continue
            k = k.strip()
            opts = str(v.get("options") or "").strip()
            at = str(v.get("verified_at") or "")
            # 🔴 2026-09-28：带上 wrong（该题干历史上被判错的答案）。
            #    题库矫正（api/tiku_fix.py）用它做"库里这个答案是不是错的"仲裁。
            wrong = list(v.get("wrong") or [])
            rec = out.get(k)
            if rec is None:
                out[k] = {"answer": str(ans).strip(), "options": opts,
                          "_at": at, "wrong": wrong}
            else:
                # ② 冲突取新：verified_at 更新者胜；无时间戳视为最旧
                newer = (at > rec.get("_at", "")) if (at or rec.get("_at")) else False
                if newer:
                    # 保留已补到的 options（旧记录可能没有）
                    if not opts and rec.get("options"):
                        opts = rec["options"]
                    out[k] = {"answer": str(ans).strip(), "options": opts,
                              "_at": at, "wrong": wrong}
                elif opts and not rec.get("options"):
                    # 补上别处存到的选项
                    rec["options"] = opts
                # wrong 取并集：判错证据来自哪个实例都算数，越多越安全
                rec_w = rec.setdefault("wrong", [])
                for w in wrong:
                    if w not in rec_w:
                        rec_w.append(w)
    for r in out.values():
        r.pop("_at", None)
    if suspect:
        _p("  已排除存疑答案 %d 条（最近一次判错，不进全局题库）" % suspect)
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


def letters_to_text(answer: str, opts: list, qtype: int = 0):
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

    2026-09-15 【多选题再修一次 —— 格式是数组，不是拼接字符串】
    上面那个 bug 修完后，多选题又暴露了第二层问题：多选题的 `answer`
    必须是**选项数组**（每个选项一个元素），不能是"多行拼接的单个字符串"。

    实测（id=146 一道多选，hash 完全自洽但查不到）：
        answer = ["选项1"]                  → bestAnswer=[]        ✗
        answer = ["选项1\n选项2"]           → bestAnswer=[]        ✗
        answer = ["选项1", "选项2"]         → bestAnswer=[选项1,选项2] index=[0,1]  ✓

    所以本题改成**按题型返回不同形态**：
        qtype=1（多选）→ 返回 list[str]，调用方 json.dumps 后即为正确数组
                          （选项集为空时返回 []，调用方会跳过）
        其它题型      → 返回 str（单选项文本 / 已是文本的原值）

    转换规则：
      - 纯字母（单个或多个，如 "B" / "ACD"）→ 按选项顺序取对应文本
      - 已经是文本 → 原样返回（多选时包成单元素即可，仍按数组落库）
      - 字母超出选项范围或取不到 → 返回原值（交由调用方决定是否跳过）
    """
    s = str(answer or "").strip()
    if not s:
        return [] if qtype == 1 else s
    if not re.fullmatch(r"[A-Za-z]+", s):
        # 已是文本（如 "origin"）。多选也要包成数组，否则查不到。
        return [s] if qtype == 1 else s
    parts = []
    for ch in s.upper():
        idx = ord(ch) - ord("A")
        if 0 <= idx < len(opts):
            parts.append(str(opts[idx]))
        else:
            # 越界，宁可不改也不要写错
            return [s] if qtype == 1 else s
    if qtype == 1:
        return parts                    # 多选：数组形态，一个选项一个元素
    return "\n".join(parts)


def _prune_baks(_p, keep: int = 5) -> None:
    """只保留最近 keep 份 tiku.db.bak.sync.* —— 防止自动回灌把磁盘写爆。

    2026-09-15 【必须做】改成定时自动回灌后，"有新增就复制一份备份"会变成
    每 10 分钟一次的无界增长：一天 144 轮 = 最多 144 个 130KB 文件（≈19MB/天，
    一个月 560MB+）。手工跑时代没人注意，自动化后会直接吃满磁盘。

    注意：**只清理本脚本自己产出的 `.bak.sync.*`**，其它人工备份
    （.bak.clean / .bak.hashfix.* / .bak.letterfix.* / .bak.restore.* 等）
    一律不碰 —— 那些是排查问题时手工留下的节点，用户明确要求保留。
    """
    import glob as _g
    pat = TIKU_DB + ".bak.sync.*"
    files = sorted(_g.glob(pat), key=lambda p: os.path.getmtime(p), reverse=True)
    for old in files[keep:]:
        try:
            os.remove(old)
            _p("  备份轮转：已删除旧备份 %s" % os.path.basename(old))
        except Exception:
            pass


def sync(dry_run: bool = False, accs: str = "", quiet: bool = False):
    """回灌主体。CLI 与自动定时器共用。

    2026-09-15 改造：原先逻辑全塞在 main() 里，只有命令行能用。网关需要
    定时自动回灌（用户需求："能自动的只是会有延迟的话问题不大"），故拆出
    本函数 —— main() 只负责解析参数再调它。

    返回 dict（供定时器记录日志）：
        {"ok":bool, "scanned":int, "learned":int, "added":int,
         "skipped":int, "noopt":int, "bad":int, "msg":str}
    安全保证（无人值守必须成立）：
      - 幂等：已存在的题一律跳过，重复跑无副作用
      - 写前备份 tiku.db
      - 落库前强制自检，发现字母式答案直接放弃本轮写入
      - 不重启任何服务（TikuAdapter 实时读库，实测立即生效）
    """
    def _p(*a):
        if not quiet:
            print(*a)

    paths = []
    if os.path.exists(ROOT_LEARNED):
        paths.append(ROOT_LEARNED)
    for d in sorted(glob.glob(os.path.join(DATA, "acc*"))):
        name = os.path.basename(d)
        if accs and name not in accs.split(","):
            continue
        paths.append(os.path.join(d, "learned_answers.json"))

    _p("扫描 %d 个学习库文件" % len(paths))
    learned = collect(paths, _p)
    _p("汇总到 verified 答案 %d 条" % len(learned))

    if not os.path.exists(TIKU_DB):
        msg = "题库文件不存在：%s" % TIKU_DB
        _p(msg)
        return {"ok": False, "scanned": len(paths), "learned": len(learned),
                "added": 0, "skipped": 0, "noopt": 0, "bad": 0, "msg": msg}

    # 2026-09-15 【防并发】自动定时器上线后必须能容忍"另一个进程也在写库"。
    # 实测未加 timeout 时，两线程同时写会直接抛
    #   OperationalError('database is locked')
    #   IntegrityError('UNIQUE constraint failed: tiku.hash, tiku.course_name')
    # 前者靠 timeout 等待重试解决；后者靠下面的写前重查 + 逐条容错解决。
    con = sqlite3.connect(TIKU_DB, timeout=30)
    cur = con.cursor()
    cur.execute("SELECT question FROM tiku")
    exist = {r[0] for r in cur.fetchall()}
    _p("题库现有 %d 条" % len(exist))

    add, skip, noopt = [], 0, []
    meta = {}          # 🔴 2026-09-28：{题干: {"wrong": [被判错的答案文本]}}，
                       #    供题库矫正（api/tiku_fix.decide）仲裁用
    # 2026-09-15 【统计细分】noopt 以前是个混装桶，光看数字（130+ 条）会误以为
    # 回灌出了故障。实际绝大多数是**历史遗留**：2026-09-14 之前 learned.py 根本
    # 不落 options 字段（见 api/learned.py:206 注释），这些老记录答案再对也没有
    # 选项，算不出 hash，永远无法回灌 —— 只能靠重做一遍题补上 options。
    # 拆成两个计数器后，一眼可辨：legacy（无 options 字段，历史包袱，正常）
    # 与 noopt_orphan（有 options 字段却解析为空，才是真异常需排查）。
    noopt_legacy = 0        # 记录里压根没有 options 字段/为空 → 历史数据
    noopt_orphan = 0        # 有 options 字段但解析不出选项 → 可疑，需查
    for q, rec in sorted(learned.items()):
        # 🔴 2026-09-28：**不再在这里提前 continue**。
        #    原先"库里已有 → 直接跳过"写在最前面，这些题就永远走不到下面的
        #    meta 收集 → 题库矫正阶段拿到的集合恒为空（真机踩到：加了矫正却
        #    一直不输出任何统计）。改为"标记 + 继续算答案"，最后再决定
        #    是新增还是仅矫正。
        _already = q in exist
        if _already:
            skip += 1
        t = qtype_of(q)
        # 2026-09-14 【重要修正】听力题必须用空选项回灌，别被"题组带选项"迷惑：
        #   听力题(type=4)：程序发来的 q_info['options'] 会把每组小题的 A/B/C/D
        #     拼成长串（3.5 题 12 个），但 **查询侧已强制 type=4 传 []**（见
        #     api/answer.py TikuAdapter._query），所以回灌也必须用 []。
        #     早期误以为"听力题选项嵌在题干里"只是运气对上，实测若照抄题组
        #     选项反而全部落空（22 条实测：带选项 0/22、空选项 22/22）。
        #
        # 2026-09-15 【判断题修 bug —— 注释与代码不符，真出事了】
        #   上面注释早就写着"判断题(type=3)：查询侧 options 为空串 → 产出 []
        #   → 回灌用 [] ✓"，但**代码从来没做到**。原表达式是
        #       opts = [] if t == 4 else parse_options(rec.get("options"))
        #   对 t==3 会走 parse_options，把学习库里的 "对\n错" 解析成 ["对","错"]
        #   一并写进库。而查询侧（api/answer.py:1488）对判断题拿到的
        #   q_info['options'] 是**空串**（选项焊死在题干里），恒发 []。
        #   两边 options 不等 → hash 不等 → 判断题永远搜不到。
        #
        #   实测（2026-09-15 自动回灌上线当天抓到）：
        #     id=145 新灌判断题 options=["对","错"] → 4 种查询形态全不命中
        #     id=120 老判断题     options=[]         → 发 [] 命中 ['错']
        #   而 145 正是**自动回灌刚写进去的**：手工回灌时判断题多因缺选项被
        #   跳过，bug 一直藏着，只有真正跑起来才暴露。
        #   修法：type=3 与 type=4 一样一律用 []，与查询侧对齐。
        opts = [] if t in (3, 4) else parse_options(rec.get("options"))
        if not opts and t not in (3, 4):
            noopt.append(q)
            if str(rec.get("options") or "").strip():
                noopt_orphan += 1      # 有选项原文却解析不出 → 异常
            else:
                noopt_legacy += 1      # 没有选项原文 → 历史遗留，正常
            continue
        # 2026-09-15 修 bug：tiku.answer 必须写**答案文本**，不能写选项字母。
        # 写字母会让 TikuAdapter 匹配失败并退化成返回第一个选项（= 错答案）。
        # 多选题还要额外注意：必须是**数组**（一个选项一个元素），
        # 拼接成单个字符串同样查不到。详见 letters_to_text 的 docstring。
        ans_text = letters_to_text(rec["answer"], opts, t)
        if t in (0, 1) and not ans_text:
            noopt.append(q)
            continue
        # 🔴 2026-09-28：入库前先清洗 UI 污染（"已选择/已选/已作答/已答"）。
        #    源头已在 api/homework.py:582 修掉，这里是第二道闸门 ——
        #    任何绕过该处的来源（旧备份恢复、别的抓取路径）都不会再污染全局库。
        ans_text = tiku_fix.clean_answer(ans_text)
        if not ans_text:
            noopt.append(q)
            continue
        # 🔴 2026-09-28：矫正元数据必须**无条件收集**（含库里已有的题）。
        #    answer = 本轮学到的答案文本；wrong = 该题干被判错过的答案（转文本）。
        _w = rec.get("wrong") or []
        meta[q] = {
            "answer": ans_text,
            "wrong": [letters_to_text(w, opts, t) for w in _w
                      if letters_to_text(w, opts, t)],
        }
        if _already:
            continue              # 库里已有 → 不新增，交给后续矫正阶段
        add.append((q, t, ans_text, opts))

    _p("-" * 70)
    _p("将新增 %d 条，跳过已存在 %d 条，因缺选项跳过 %d 条"
       % (len(add), skip, len(noopt)))
    for q, t, ans, opts in add[:8]:
        _show = ("|".join(str(x) for x in ans) if isinstance(ans, list)
                 else str(ans))
        _p("   [t%d] %-46s -> %-8s opts=%d"
           % (t, q[:46], _show[:24], len(opts)))
    if len(add) > 8:
        _p("   … 还有 %d 条" % (len(add) - 8))
    if noopt:
        _p("\n   ⚠️ 缺选项 %d 条（其中历史遗留 %d、可疑 %d）"
           % (len(noopt), noopt_legacy, noopt_orphan))
        _p("      · 历史遗留＝该题录入时还没开始存选项（2026-09-14 前），")
        _p("        答案再对也算不出 hash，必须重做一遍题补上 options 才能回灌，属正常。")
        if noopt_orphan:
            _p("      · 可疑＝有选项原文却解析不出，这才是需要排查的异常：")
            for q in noopt[:5]:
                _p("          %s" % q[:70])
        else:
            _p("      · 可疑 0 条 —— 本轮缺选项全部是历史包袱，无需处理。")

    _stat = {"ok": True, "scanned": len(paths), "learned": len(learned),
             "added": 0, "skipped": skip, "noopt": len(noopt),
             "noopt_legacy": noopt_legacy, "noopt_orphan": noopt_orphan,
             "bad": 0, "msg": ""}

    if dry_run:
        _p("\n[dry-run] 未写入")
        con.close()
        _stat["msg"] = "dry-run"
        return _stat
    if not add:
        # 🔴 2026-09-28：**不再提前返回**。题库跑满后 add 恒为空，而"已经躺在
        #    库里的错答案 / UI 污染"恰恰最需要矫正 —— 提前返回会让矫正永远不执行
        #    （真机踩到：加了矫正却一直没输出，就是卡在这里）。
        #    改为继续走完：备份 → 矫正 → 提交（插入循环自然空转）。
        _p("\n本轮无新增题目，继续执行题库矫正。")

    ts = time.strftime("%Y%m%d_%H%M%S")

    # 2026-09-15 【入库前强制自检】—— 防止"字母式答案"再次混入。
    # 血的教训：answer 字段必须是**答案文本**。一旦写成字母，TikuAdapter
    # 匹配不到就退化返回第一个选项，界面一切正常但**每题都在提交错答案**，
    # 极难察觉（本次是靠端到端逐条比对才挖出来）。这里在落库前硬拦。
    #
    # ⚠️ 2026-09-15 16:5x 【自检本身误伤，已修 —— 上线几轮就炸了】
    #   初版判定是"纯 `[A-Za-z]+` 就算字母式"，结果把**英文单词答案**全拦了：
    #     「ChatGPT 基于哪种架构训练的?」verified='B' → options 里第 2 项就是
    #         **Transformer**，转换后是货真价实的答案文本，却被判成"字母式"；
    #     「以下哪个不是分布式计算框架?」→ **Windows**，同样被误判。
    #   更糟的是自检一旦命中就 `ok=False` **中止整轮写入**，另外 115 条正常题
    #   全被连坐 —— 表现为 `/api/overview` 的 `sync.last_res` 长期停在同一句
    #   "检测到 1 条字母式答案，已中止写入"，回灌实际上处于**瘫痪**状态
    #   （而且因为不是异常，`ok` 只体现在字段里，不翻 last_res 根本发现不了）。
    #
    #   正确判据：**值是否出现在本题选项列表里**
    #     - 在选项列表里 → 它就是合法答案文本（哪怕长成英文单词）→ 放行
    #     - 不在列表里   → 说明 letters_to_text 没转换成功，仍是光秃秃的字母 → 拦
    #   这比"长得像字母就拦"精确得多，且天然覆盖 Transformer/Windows 这类词。
    #
    # 注意：多选题的 ans 是 list（多元素），要逐个元素查，不能 str() 整个列表
    # （str(['A','B']) 得到 "['A', 'B']"，re.fullmatch 会漏判）。
    def _looks_like_letter_but_not_an_option(x, opts):
        s = str(x).strip()
        if not re.fullmatch(r"[A-Za-z]+", s):
            return False                       # 不是纯字母 → 一定是文本，放行
        return s not in {str(o).strip() for o in opts}   # 不在选项里才算可疑

    bad = []
    for q, t, ans, opts in add:
        if t not in (0, 1):
            continue
        items = ans if isinstance(ans, list) else [ans]
        if any(_looks_like_letter_but_not_an_option(x, opts) for x in items):
            bad.append((q, t, ans))
    if bad:
        _p("\n" + "!" * 60)
        _p("检测到 %d 条答案仍是「字母式」，可能无法被 TikuAdapter 正确解析：" % len(bad))
        for q, t, ans in bad[:10]:
            _p("   [t%d] %-46s -> %r" % (t, q[:46], ans))
        # 2026-09-15 改：不再"中止整轮"。原实现一旦发现 bad 就 ok=False 直接返回，
        # 一条脏数据会把同轮几百条好数据全部拖死。改为**只剔除这几条**、
        # 其余照常写入，并在返回结果里报出 bad 条数供观察。
        _p("已剔除这些记录，其余正常写入。")
        _p("!" * 60)
        badset = {id(x) for x in bad}
        add = [x for x in add if id(x) not in badset]
        _bad_n = len(bad)
    else:
        _bad_n = 0

    bak = TIKU_DB + ".bak.sync." + ts
    shutil.copy2(TIKU_DB, bak)
    _prune_baks(_p)
    _p("\n已备份 -> %s" % os.path.basename(bak))

    # 2026-09-15 【防并发】写前用最新快照重查一次：上面 SELECT 到现在可能已有
    # 别的进程写入了同样的题。重查能挡掉绝大部分竞争，剩下的靠逐条容错兜底。
    cur.execute("SELECT question FROM tiku")
    exist2 = {r[0] for r in cur.fetchall()}
    ins, lost = 0, 0

    def _insert(q, t, ans_list, opts):
        """插一条记录。返回 True=真的写进去了，False=已存在/撞约束被跳过。

        2026-09-15 多选兼容特例（见下方说明）：answer 落库形态
          多选（t==1）→ 数组，**至少 2 个元素才有效**（TikuAdapter 对单元素
                          数组的多选查询会返回空，实测 id=146）
          其它        → 单元素包一层 [ans]
        """
        if q in exist2:
            return False
        ans_json = json.dumps(ans_list, ensure_ascii=False)
        cur.execute(
            "INSERT INTO tiku (question,type,options,answer,plat,hash,"
            "course_name,extra) VALUES (?,?,?,?,?,?,?,?)",
            (q, t, json.dumps(opts, ensure_ascii=False),
             ans_json, 0, tiku_hash(q, opts, t, 0), "", ""))
        return True

    def _reconcile(q, new_ans, wrong, stat, bucket):
        """库里已有该题干 → 逐行仲裁（不新增行，只可能改 answer）。

        · clean    → 库里是 UI 污染（"已选择"），清洗后与新答案一致 → UPDATE
        · update   → 库里答案在学习库 wrong 列表里（平台判过错）→ 用新答案覆盖
        · conflict → 无强证据 → **不改**，记入待核对清单
        """
        cur.execute("SELECT id, answer FROM tiku WHERE question=?", (q,))
        for rid, raw in cur.fetchall():
            try:
                old = json.loads(raw)
            except Exception:
                old = raw
            act, why = tiku_fix.decide(old, new_ans, wrong)
            if act == "clean":
                cur.execute("UPDATE tiku SET answer=? WHERE id=?",
                            (json.dumps(tiku_fix.clean_answer(old),
                                        ensure_ascii=False), rid))
                stat["clean"] += 1
                _p("  ✧ 清洗 id=%s：%s → %s"
                   % (rid, old, tiku_fix.clean_answer(old)))
            elif act == "update":
                cur.execute("UPDATE tiku SET answer=? WHERE id=?",
                            (json.dumps(new_ans, ensure_ascii=False), rid))
                stat["update"] += 1
                _p("  ↻ 矫正 id=%s：%s → %s（%s）" % (rid, old, new_ans, why))
            elif act == "conflict":
                stat["conflict"] += 1
                bucket.append({"id": rid, "question": q, "in_db": old,
                               "learned": new_ans, "reason": why})

    for q, t, ans, opts in add:
        ans_list = ans if isinstance(ans, list) else [ans]
        # ⚠️ 矫正**不在这里**做：题库已满时 add 为空、这个循环压根不执行，
        #    而"已经躺在库里的错答案"恰恰最需要矫正。统一放到写入段之后的
        #    独立矫正阶段（遍历 meta ∩ exist2）。
        try:
            ok = _insert(q, t, ans_list, opts)
            if not ok:
                lost += 1
                continue
            ins += 1
        except sqlite3.IntegrityError:
            # 唯一约束撞车（hash+course_name 已存在）＝ 等价于"这题已经有了"，
            # 不是错误。并发场景下这是正常结果，跳过即可。
            lost += 1
            continue

        # 2026-09-15 【多选单答案的兼容补丁】
        # TikuAdapter 对多选题（type=1）的匹配有个隐含要求：answer 数组**至少
        # 2 个元素**。实测（id=146，题干标「多选题」但平台判对时只有一个选项）：
        #     type=1 + ["选项A"]              → bestAnswer=[]      ✗ 查不到
        #     type=1 + ["选项A","选项B"]      → bestAnswer=[…]     ✓
        #     type=0 + ["选项A"]              → bestAnswer=[选项A]  ✓
        # 而程序查哪个 type 取决于**超星页面给的题型字段**（api/base.py:1506），
        # 不是题干文字 —— 所以"只出现在部分题型"的记录会永久搜不到。
        # 这里对"多选但答案只有 1 个选项"的题**额外补一条 type=0 的副本**，
        # 让两种可能查询都能命中。代价极小（同一题干、不同 hash，不会互相覆盖），
        # 收益是这类题不再无声失效。
        if t == 1 and len(ans_list) == 1:
            try:
                if _insert(q, 0, ans_list, opts):
                    ins += 1
            except sqlite3.IntegrityError:
                pass

    # ---- 🔴 2026-09-28 全库污染清洗（不依赖学习库，纯文本修复）----
    # 为什么必须独立：库里那批 `地动仪已选择` / `核糖核酸已选择` 是早期抓取
    # bug 留下的（源头已在 api/homework.py:582 修掉），但它们在**当前学习库里
    # 没有对应记录**（题干形态对不上），所以靠上面的矫正阶段永远碰不到。
    # 这里直接扫全库 answer，把 UI 状态词后缀洗掉 —— 纯文本修复，零语义风险。
    cur.execute("SELECT id, answer FROM tiku")
    fixed_pollute = 0
    for rid, raw in cur.fetchall():
        try:
            old = json.loads(raw)
        except Exception:
            continue
        if tiku_fix.is_polluted(old):
            newv = tiku_fix.clean_answer(old)
            cur.execute("UPDATE tiku SET answer=? WHERE id=?",
                        (json.dumps(newv, ensure_ascii=False), rid))
            fixed_pollute += 1
            _p("  ✧ 清洗污染 id=%s：%s → %s" % (rid, old, newv))
    if fixed_pollute:
        _p("全库污染清洗：%d 条" % fixed_pollute)

    # ---- 🔴 2026-09-28 题库矫正阶段（独立于"本轮有无新增"）----
    # 为什么必须独立：题库跑满后 add 恒为空，若挂在插入循环里就永远不执行；
    # 而"已经躺在库里的错答案 / UI 污染"恰恰是最该清理的那批。
    # 数据源 = meta（本轮学到答案的题干）∩ exist2（写前库里已有的题干）。
    fix_stat = {"clean": 0, "update": 0, "conflict": 0}
    fix_conflicts = []
    _fix_n = 0
    for q, info in meta.items():
        if q not in exist2:
            continue            # 本轮新插入的，无需矫正
        _fix_n += 1
        # ⚠️ 必须归一成 list：多选答案本身就是 list，直接 `[ans]` 会包成
        #    嵌套列表 `[['A','B']]`，既误报冲突、又可能在覆盖时写坏库里数据。
        _ans = info["answer"]
        _reconcile(q, _ans if isinstance(_ans, list) else [_ans],
                   info.get("wrong") or [], fix_stat, fix_conflicts)
    if _fix_n:
        _p("矫正：扫描 %d 条已存在题干" % _fix_n)

    try:
        con.commit()
    except sqlite3.OperationalError as e:
        _p("提交失败（%s），本轮未写入。" % e)
        con.close()
        _stat.update(ok=False, msg="提交失败：%s" % e)
        return _stat
    total = cur.execute("SELECT count(*) FROM tiku").fetchone()[0]
    con.close()
    if lost:
        _p("已新增 %d 条（并发跳过 %d 条）；题库现有 %d 条" % (ins, lost, total))
    else:
        _p("已新增 %d 条；题库现有 %d 条" % (ins, total))
    # 2026-09-15 更正：此前写"需重启 TikuAdapter 才生效"是**错的**。
    # 实测热写入一条新题、不重启，接口立刻就能查到（answerIndex/answerKey 均正确）。
    # TikuAdapter 每次查询都实时读 SQLite，不是启动时加载到内存。
    _p("（无需重启：TikuAdapter 每次查询实时读库，新记录立即可用）")

    # ---- 🔴 2026-09-28 矫正阶段结果 ----
    if fix_stat["clean"] or fix_stat["update"] or fix_stat["conflict"]:
        _p("-" * 70)
        _p("题库矫正：清洗污染 %d 条 ｜ 更正错答案 %d 条 ｜ 待人工核对 %d 条"
           % (fix_stat["clean"], fix_stat["update"], fix_stat["conflict"]))
    if fix_conflicts:
        cp = os.path.join(DATA, "tiku_conflicts.json")
        try:
            prev = []
            if os.path.exists(cp):
                prev = json.load(io.open(cp, encoding="utf-8")) or []
            seen = {(str(c.get("question")), str(c.get("in_db")),
                     str(c.get("learned"))) for c in prev}
            added_c = 0
            for c in fix_conflicts:
                key = (str(c.get("question")), str(c.get("in_db")),
                       str(c.get("learned")))
                if key in seen:
                    continue
                c["first_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")
                prev.append(c)
                seen.add(key)
                added_c += 1
            json.dump(prev, io.open(cp, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            if added_c:
                _p("待核对清单已更新（新增 %d 条）→ %s" % (added_c, cp))
        except Exception as e:
            _p("待核对清单写入失败：%s" % e)

    _stat.update(added=ins, bad=_bad_n,
                 fixed_pollute=fixed_pollute,
                 fixed_clean=fix_stat["clean"],
                 fixed_update=fix_stat["update"],
                 conflicts=fix_stat["conflict"],
                 msg="新增 %d 条，题库共 %d 条" % (ins, total)
                     + ("（剔除 %d 条字母式答案）" % _bad_n if _bad_n else "")
                     + ("（清洗 %d / 更正 %d / 待核对 %d）"
                        % (fix_stat["clean"], fix_stat["update"],
                           fix_stat["conflict"])
                        if any(fix_stat.values()) else ""))
    return _stat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--accs", default="")
    args = ap.parse_args()
    sync(dry_run=args.dry_run, accs=args.accs)


if __name__ == "__main__":
    main()
