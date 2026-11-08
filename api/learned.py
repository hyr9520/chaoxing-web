# -*- coding: utf-8 -*-
"""本地学习库：记录"已验证正确答案"与"错误答案"，供跨会话 / 跨账号复用。

背景（2026-09-11 用户需求）：已做过的题（尤其听力题靠猜），提交后平台会给出
每小问的得分与对错标记。把"答对的答案"沉淀下来、把"答错的选项"记录为负反馈，
下次遇到同题（其他账号 / 重做机会）即可：优先用已验证答案，或让 AI 排除错误项
重新分析。听力题只能做一次，但这门课多个账号共享同一套题 → 学习成果可复用。

数据文件：learned_answers.json（与 cache.json 同目录）
结构：
{
  "<题目title>": {
      "verified": "BA",              # 该题组被判定全对时的"我的答案"（连写形态）
      "verified_at": "2026-09-11 15:02:33",
      "wrong": ["CD", "BA"],         # 历史答错记录（新→旧，去重，最多保留 5 条）
      "wrong_at": "2026-09-11 15:10:00",
      "last_mark": "dui|cuo|bandui|unknown",
      "last_score": 50.0,
      "updated_at": "..."
  }
}
"""
import json
import os
import re
import tempfile
import threading
import time

_LOCK = threading.Lock()

_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title) -> str:
    """统一题库 / 学习库的键形态（2026-09-11 键分裂修复）。

    实测同一道听力题曾产生 3 种不同的键：
      A. '【听力题】Cet-4-3.1-Exercise 01'
      B. '【听力题】<img src="...video.png">Cet-4-3.1-Exercise 01.mp3...'
      C. '【听力题】 Cet-4-3.20-Exercise.mp3 Questions 19 to 21 ...'
    后果：学习库记录的错题答案查询时命中不到（标题形态不同），
    负反馈失效 → 下次仍提交同一个已判错的答案。

    规则：去 <img> 标签 + &nbsp; 归一 + 空白压缩 + 去首尾。
    题库(cache.json) 与学习库必须共用本函数，否则两边键对不上。

    边界（2026-09-13 补）：若题干**只有图片**（去掉 img 后为空），不能返回
    空串 —— 否则所有"纯图题"会共用同一个键 ""，缓存命中即给出完全无关的
    答案。此时退回已去掉 src 的图片标签本身，保证每题键唯一且稳定。
    """
    t = str(title or "")
    _had_img = bool(_IMG_RE.search(t))
    # 先把 <img src="..."> 收敛成不含 URL 的占位（URL 里常带随机 token，
    # 同一道题两次抓取的 URL 可能不同 → 键不稳定）。src 值进不了键。
    t = re.sub(r"<img\b[^>]*?\bsrc\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'>]+)[^>]*>",
               "<img>", t, flags=re.IGNORECASE)
    t = _IMG_RE.sub("", t)
    t = t.replace("\xa0", " ")
    # 题型标记（【听力题】等）后的空格一并去掉：带 <img> 的标题删掉 img 后
    # 不留空格，而纯文本标题常写成 '【听力题】 Cet-4-...'，不归一就对不上
    t = re.sub(r"(【[^】]*】)\s+", r"\1", t)
    t = _WS_RE.sub(" ", t).strip()
    if not t and _had_img:
        # 纯图题：用图片占位当键的一部分，避免所有纯图题撞成同一个空键
        t = "<img>"
    return t


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class LearnedAnswers:
    """学习库读写。所有方法线程安全（进程内）。"""

    def __init__(self, path: str = None):
        from api.config import data_dir   # 局部导入避免循环依赖
        self.path = path or os.path.join(data_dir(), "learned_answers.json")
        self._data = None
        self._mtime = None

    # ---------- 内部 ----------
    def _load(self) -> dict:
        # 带 mtime 失效的轻量缓存：多进程（服务 / 补做脚本）会写同一文件，
        # 必须能在 mtime 变化后看到新数据，否则跨进程学习成果不可见。
        # 2026-09-14：改用 st_mtime_ns（纳秒）。getmtime() 在部分文件系统上
        # 只有秒级精度，同一秒内的两次写入会被误判为"没变"，读到旧数据
        # （跨进程/快速连续作答时会出现"刚记的答案读不到"）。
        mtime = None
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            pass
        if self._data is not None and mtime == self._mtime:
            return self._data
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        self._data = data
        self._mtime = mtime
        return data

    def _save(self) -> None:
        """原子写：先写临时文件再替换，避免并发写坏文件。"""
        data = self._load()
        d = os.path.dirname(self.path) or "."
        try:
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".learned_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            # 退化：直接覆盖写
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        try:
            self._mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            self._mtime = None

    # ---------- 小问级（2026-09-11 用户需求）----------
    # 背景：听力题一个题组含多个小问，平台按"小问"给对错标记。此前的实现只在
    # 题组"全部小问都对"时才留下答案（并写入题库），题组里蒙对的那些小问被一起
    # 丢掉了 —— 用户要求把这些单独正确的小问答案也沉淀下来，下次能直接用。
    # 存储：rec["sub_verified"] = {"0": "B", "2": "D"}（键为小问序号，从 0 开始）
    def record_subs(self, title: str, subs: list) -> int:
        """记录小问级作答结果，只把判定为"对"的小问答案沉淀下来。

        subs: [{"answer": "B", "mark": "dui"}, ...] 按小问顺序
        返回本次新增/更新的小问数。
        """
        title = normalize_title(title)
        if not title or not subs:
            return 0
        changed = 0
        with _LOCK:
            data = self._load()
            rec = data.get(title) or {}
            cur = dict(rec.get("sub_verified") or {})
            for i, sub in enumerate(subs):
                ans = str((sub or {}).get("answer") or "").strip()
                mark = str((sub or {}).get("mark") or "")
                key = str(i)
                if mark == "dui":
                    if ans and cur.get(key) != ans:
                        cur[key] = ans
                        changed += 1
                elif mark in ("cuo", "bandui"):
                    # 该小问这次没被判"对" -> 撤销之前的"已验证"记录。
                    #
                    # 2026-09-29 修 bug（今日实测 40 例）：此前只认 `cuo`，
                    # `bandui`（部分对）按"不确定，宁缺勿滥，不动"处理。
                    # 后果是**死循环**：多选题只答一个选项时平台判 bandui，
                    # 而库里更早那次的 sub_verified 被原样保留 →
                    # `_apply_verified_subs()` 又把它填回答案对应位 →
                    # 提交同一个半对答案 → 再判 bandui → 无限循环。
                    # 实测形态高度一致：last_mark=bandui、verified=None、
                    # sub_verified={'0':'A'}、wrong=['A']、revoked_reason=bandui。
                    #
                    # 半对 == 该小问**没有被平台确认为正确**，与 cuo 同等对待：
                    # 都要撤下。多小问题组的"多轮积累"不受影响 —— 那是靠
                    # 各小问**分别判 dui** 累积的，与小问判 bandui 无关。
                    if key in cur:
                        cur.pop(key)
                        changed += 1
            if changed:
                rec["sub_verified"] = cur
                rec["sub_at"] = _now()
                data[title] = rec
                self._save()
        return changed

    def get_sub_verified(self, title: str) -> dict:
        """该题组已确认正确的小问答案 {小问序号: 答案}。"""
        title = normalize_title(title)
        if not title:
            return {}
        with _LOCK:
            rec = self._load().get(title) or {}
            v = rec.get("sub_verified")
            return {str(k): str(x) for k, x in v.items()} if isinstance(v, dict) else {}

    def get_partials(self, title: str) -> list:
        """该题历史「部分正确」的作答记录 [{"ans","score","at"}]，新的在前。

        2026-09-29 新增：供 AI 提示词使用 —— 多选题判 bandui 时，
        说明所选组合里**至少含一个正确选项**，据此让模型"补全"而非重新猜。
        """
        title = normalize_title(title)
        if not title:
            return []
        with _LOCK:
            rec = self._load().get(title) or {}
            v = rec.get("partials")
        return [dict(x) for x in v if isinstance(x, dict)] if isinstance(v, list) else []

    # ---------- 读 ----------
    def get_verified(self, title: str) -> str:
        """该题已验证的正确答案（全对时记录的我的答案）；无则空串。"""
        title = normalize_title(title)
        if not title:
            return ""
        with _LOCK:
            rec = self._load().get(title) or {}
            return str(rec.get("verified") or "").strip()

    def get_verified_record(self, title: str) -> dict:
        """返回 {"answer": str, "options": str} —— 供调用方做**选项一致性校验**。

        🔴 2026-09-27 新增，原因（真机实测 A 卷）：
        学习库只按**题干**归一化匹配，不校验选项。同一题干在不同试卷里
        选项数/顺序不同时，库里旧答案的**字母**会被原样提交 → 系统性错位。
        实测两例：「中国四大名著」库里 verified=ABC 但被答成 ABD；
        「HTTP 请求方法」库里 verified=ABCD 但被答成 ABC。
        所以查询侧必须拿到 options 一起比对。
        """
        title = normalize_title(title)
        if not title:
            return {}
        with _LOCK:
            rec = self._load().get(title) or {}
            return {
                "answer": str(rec.get("verified") or "").strip(),
                "options": str(rec.get("options") or "").strip(),
            }

    def get_wrong(self, title: str) -> list:
        """该题历史答错的答案列表（新→旧）。"""
        title = normalize_title(title)
        if not title:
            return []
        with _LOCK:
            rec = self._load().get(title) or {}
            v = rec.get("wrong")
            return [str(x) for x in v] if isinstance(v, list) else []

    # ---------- 写 ----------
    def record(self, title: str, my_answer: str, mark: str,
               score: float = None, options: str = None) -> None:
        """记录一次作答结果。

        mark: "dui"（全对）/ "cuo"（有错）/ "bandui"（部分对）/ "unknown"
        my_answer: 该题组的"我的答案"（连写形态，如 "BA"）；空则只更新标记。
        options: 原始选项文本（换行分隔）。**只在有值时写入**，用于日后把
            已验证答案回灌 TikuAdapter 题库（题库 hash 需要选项参与计算）。
            2026-09-14 补：此前不存选项，导致学习库里的答案没法回灌题库，
            "做过两遍的题、题库里却搜不到"就是这么来的。
        """
        title = normalize_title(title)
        if not title:
            return
        my_answer = str(my_answer or "").strip()
        mark = str(mark or "unknown")
        with _LOCK:
            data = self._load()
            rec = data.get(title) or {}
            rec["last_mark"] = mark
            if score is not None:
                rec["last_score"] = score
            if options and str(options).strip():
                # 选项是题目固有属性，不会随作答变化；有了就留着
                rec["options"] = str(options).strip()
            rec["updated_at"] = _now()
            if mark == "dui" and my_answer:
                if rec.get("verified") != my_answer:
                    rec["verified"] = my_answer
                    rec["verified_at"] = _now()
                # 已答对：从错误列表移除同值
                rec["wrong"] = [w for w in (rec.get("wrong") or []) if w != my_answer]
                # 已全对 -> 「部分正确线索」已完成使命，清掉（2026-09-29）
                rec.pop("partials", None)
            elif mark in ("cuo", "bandui") and my_answer:
                wl = [w for w in (rec.get("wrong") or []) if w != my_answer]
                wl.insert(0, my_answer)
                rec["wrong"] = wl[:5]          # 最多留 5 条
                rec["wrong_at"] = _now()
                if mark == "bandui":
                    # 🔴 2026-09-29 新增（用户要求）：「部分正确」线索。
                    #
                    # 多选题最容易卡在这里 —— 平台判 bandui 时**不会告诉你**
                    # 哪个选项对，但"得了部分分"这件事本身就有信息量：
                    # 选中的选项里**至少有一个是正确的**，只是组合不完整。
                    # 把 (答案, 得分) 记下来，下次作答时喂给 AI 当线索，
                    # 让它"在已有基础上补全"，而不是每次从零瞎猜。
                    #
                    # 与 wrong 的关系：wrong 记的是"这个组合整体不通过"，
                    # partials 记的是"这个组合里有对的东西" —— 两者不冲突，
                    # 提示词里会分开表述。全对后（mark=dui）自动清空。
                    ps = [p for p in (rec.get("partials") or [])
                          if str((p or {}).get("ans") or "") != my_answer]
                    ps.insert(0, {"ans": my_answer, "score": score, "at": _now()})
                    rec["partials"] = ps[:6]
                # 2026-09-15 修 bug：判错时必须撤销已失效的 verified。
                # 此前只增不减 —— 一道题先判对、后续再判错时，旧的 verified
                # 会被永久保留，而 query()/query_all() 都**优先返回 verified**
                # → 下次遇到该题直接交上已被平台证伪的答案，必错。
                # 实测中招 5 条（acc3 听力题 3.5/3.6/3.7/3.9/3.10，
                # verified_src=restore_20260911 从备份恢复的旧值）。
                #
                # 撤销规则：
                #   cuo（全错）→ 整组答案已证伪，撤销 verified。
                #   bandui（部分对）→ 只有当 verified 与本次提交的 my_answer
                #     完全相同时才撤销（说明"整组全对"这个结论是错的）；
                #     若两者不同，说明 verified 来自另一次作答，保留待考。
                _v = rec.get("verified")
                if _v and (mark == "cuo" or _v == my_answer):
                    rec.pop("verified", None)
                    rec.pop("verified_at", None)
                    rec.pop("verified_src", None)
                    rec["verified_revoked_at"] = _now()
                    rec["verified_revoked_reason"] = mark
            data[title] = rec
            self._save()

    # ---------- 统计 ----------
    def stats(self) -> dict:
        with _LOCK:
            data = self._load()
        verified = sum(1 for r in data.values() if r.get("verified"))
        wrong = sum(1 for r in data.values() if r.get("wrong"))
        subs = sum(len(r.get("sub_verified") or {}) for r in data.values())
        return {"total": len(data), "verified": verified,
                "wrong": wrong, "subs": subs}
