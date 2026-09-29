# -*- coding: utf-8 -*-
"""已批阅作业的「正确答案」回收。

━━ 背景（2026-09-27 真机实测）━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
作业提交后平台状态是「待批阅」，而 `_harvest_homework_scores` 只在**提交那一瞬间**
检查判分（`parsed["_marks"]` 实际上从未被赋值，是空转的）。结果：作业答案
永远进不了题库 —— 同一份作业重做时，未命中外部题库的题全部重新问 AI。

但批阅完成后，平台的「已批阅详情页」**直接给出每题的正确答案**（明文 HTML）：

    /mooc-ans/work/phone/selectWorkQuestionYiPiYue
        ?courseId=…&classId=…&workId=…&workAnswerId=…&questionLinkId=<qid>
        &status=4&isReport=false&mooc=1&times=0&isAccessibility=0&ut=s

⚠️ 关键：**带上 `questionLinkId` 时返回的是整卷**（314KB，含全部题目的题干、
   选项、正确答案、我的答案、得分），不是单题 → 一次请求就能拿到全部答案。

━━ 为什么不需要"等批阅"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
因为答案不依赖"我的答案"。只要作业**已批阅**，任何时候探一次都能拿到。
所以策略是：**每次用户扫描作业时顺带探一次** —— 已批阅的回收，没批阅的下次再探。
零轮询、零定时器、零常驻线程。

━━ 设计约束（用户要求：不影响正常运行、不增大负荷）━━━━━━━━━━━━━━━━
  · 只处理有分数（已批阅）的作业；「待批阅/未提交」一律不碰
  · 已回收过的记在 recycled_homeworks.json，不重复请求
  · 全程 try/except 兜住，任何失败都不影响主流程
  · 由调用方放到后台线程跑，不阻塞界面
  · 每份作业最多 3 次 GET（批阅页 → 答题卡取 qid → 整卷）
"""
import json
import os
import re
import time

from bs4 import BeautifulSoup

from api.config import data_dir
from api.learned import LearnedAnswers

# 只回收这些题型（有唯一标准答案）；填空/简答平台不给出正确答案
_OBJECTIVE = ("单选题", "多选题", "判断题")
_RECYCLE_FILE = "recycled_homeworks.json"
_HOST = "https://mooc1.chaoxing.com/mooc-ans"
_UA_WAIT = 0.4          # 请求间隔，避免过快


def _recycle_path() -> str:
    return os.path.join(data_dir(), _RECYCLE_FILE)


def _load_recycled() -> dict:
    try:
        with open(_recycle_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_recycled(d: dict) -> None:
    try:
        p = _recycle_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except OSError:
        pass


def _get(session, url: str, timeout: int = 30):
    try:
        return session.get(url, timeout=timeout, allow_redirects=True)
    except Exception:
        return None


def _txt(node, default=""):
    return node.get_text(" ", strip=True) if node is not None else default


def parse_graded_paper(html: str) -> list:
    """解析已批阅整卷 HTML → [{no,type,title,options,right,mine,score}]。

    结构实证：
      <div class="stemDiv"> 1.<span class="stemGray">(单选题，1 分)</span>
        <div class="Picdiv workTextWrap qtContent"><p>题干</p></div></div>
      <div class="optionDiv">…<div class="ptag workTextWrap qtDetail">A.<p>…</p></div>…</div>
      <div class="answerDiv"><div class="answerPtit">
        正确答案<span class="rightAnswerContent">A</span>
        我的答案：<span class="stuAnswerContent">A</span>
        得<span class="font40"> 1 </span>分</div></div>
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for ad in soup.select("div.answerDiv"):
        stem = ad.find_previous("div", class_="stemDiv")
        if stem is None:
            continue
        head = stem.get_text(" ", strip=True)
        no = re.match(r"\s*(\d+)\s*\.", head)
        qc = stem.select_one("div.qtContent")
        qt = re.search(r"\((单选题|多选题|判断题|填空题|简答题|论述题|名词解释)[^)]*\)", head)
        opt_div = stem.find_next_sibling("div", class_="optionDiv")
        options = ""
        if opt_div is not None:
            parts = [d.get_text(" ", strip=True) for d in opt_div.select("div.qtDetail")]
            options = "\n".join(p for p in parts if p)
        invis = [s.get_text(" ", strip=True) for s in ad.select("span.element-invisible-hidden")]
        rows.append({
            "no": int(no.group(1)) if no else len(rows) + 1,
            "type": qt.group(1) if qt else "",
            "title": _txt(qc),
            "options": options,
            "right": _txt(ad.select_one("span.rightAnswerContent")),
            "mine": _txt(ad.select_one("span.stuAnswerContent")),
            "score": _txt(ad.select_one("span.font40")).strip(),
            "right_text": (invis[0] if invis else "").strip(" ;:："),
        })
    return rows


def recycle_one(session, work: dict, log=print) -> dict:
    """回收单份已批阅作业。返回 {"ok":bool, "added":int, "wrong":int, "msg":str}。

    work 需含：taskrefid、name、courseid/courseId、href。
    """
    work_id = str(work.get("taskrefid") or work.get("work_id") or "")
    course_id = str(work.get("courseid") or work.get("courseId") or "")
    clazz_id = str(work.get("clazzid") or work.get("clazzId") or "")
    href = str(work.get("href") or "")
    name = str(work.get("name") or "")
    if not (work_id and course_id and href):
        return {"ok": False, "added": 0, "wrong": 0, "msg": "缺参数"}

    # ① 作业入口页 → 拿到 workAnswerId
    #    实测：批阅页（task-work）里它是 **JS 变量** `var workAnswerId = "55683569";`
    #    （隐藏域 #answerId 只出现在详情页，此处取不到）。多写法兜底。
    r = _get(session, href)
    if r is None:
        return {"ok": False, "added": 0, "wrong": 0, "msg": "入口页请求失败"}
    m = (re.search(r'workAnswerId\s*[=:]\s*["\']?(\d+)', r.text)
         or re.search(r'id="answerId"[^>]*value="(\d+)"', r.text))
    if not m:
        return {"ok": False, "added": 0, "wrong": 0, "msg": "未批阅或无 answerId"}
    answer_id = m.group(1)
    time.sleep(_UA_WAIT)

    # ② **完全不传** questionLinkId 时返回「答题卡」（含全部 qid）
    #    ⚠️ 实测坑：显式传空的 `questionLinkId=` 会让接口取不到任何内容，
    #       必须整个参数都不出现。参数顺序也与真机抓包一致。
    card_url = (f"{_HOST}/work/phone/selectWorkQuestionYiPiYue?courseId={course_id}"
                f"&workAnswerId={answer_id}&workId={work_id}&knowledgeId=0"
                f"&status=4&classId={clazz_id}&oldWorkId=&mooc=1&ut=s")
    r2 = _get(session, card_url)
    if r2 is None:
        return {"ok": False, "added": 0, "wrong": 0, "msg": "答题卡请求失败"}
    qids = re.findall(r'<dl\s+data="(\d+)"', r2.text)
    if not qids:
        return {"ok": False, "added": 0, "wrong": 0, "msg": "未取到 qid（可能未批阅）"}
    time.sleep(_UA_WAIT)

    # ③ 传任一 qid → **整卷**（含全部正确答案）
    base = (f"{_HOST}/work/phone/selectWorkQuestionYiPiYue?courseId={course_id}"
            f"&classId={clazz_id}&workId={work_id}&workAnswerId={answer_id}")
    r3 = _get(session, base + f"&questionLinkId={qids[0]}&status=4&isReport=false"
                             "&mooc=1&times=0&isAccessibility=0&ut=s", timeout=60)
    if r3 is None:
        return {"ok": False, "added": 0, "wrong": 0, "msg": "整卷请求失败"}

    rows = parse_graded_paper(r3.text)
    if not rows:
        return {"ok": False, "added": 0, "wrong": 0, "msg": "整卷解析为空"}

    learner = LearnedAnswers()
    added = wrong = 0
    for r_ in rows:
        if r_["type"] not in _OBJECTIVE:
            continue                      # 填空/简答平台无标准答案
        ans = (r_["right"] or "").strip()
        if not ans:
            continue
        mine = (r_["mine"] or "").strip()
        if mine and mine != ans:
            learner.record(r_["title"], mine, "cuo", options=r_["options"] or None)
            wrong += 1
        learner.record(r_["title"], ans, "dui", options=r_["options"] or None)
        added += 1

    log(f"♻ 已回收《{name[:26]}》：正确答案 {added} 题"
        f"（其中错题 {wrong} 题已记入负反馈）")
    return {"ok": True, "added": added, "wrong": wrong, "msg": "ok"}


def try_recycle_all(cx, works: list, log=print) -> dict:
    """遍历「已批阅且未回收」的作业，逐份回收。

    调用方应放到后台线程；本函数自身也做了整体兜底。
    """
    stat = {"checked": 0, "done": 0, "added": 0, "wrong": 0, "skip": 0}
    try:
        session = cx.session if hasattr(cx, "session") else None
        if session is None:
            from api.base import SessionManager
            session = SessionManager.get_session()
        done = _load_recycled()
        for w in works or []:
            work_id = str(w.get("taskrefid") or "")
            if not work_id or work_id in done:
                continue
            # 只碰已批阅的：有分数 或 状态为「已完成」
            st = str(w.get("status") or "")
            sc = str(w.get("score") or "").strip()
            if not (sc or st in ("已完成", "已批阅")):
                continue
            stat["checked"] += 1
            try:
                res = recycle_one(session, w, log=log)
            except Exception as e:
                log(f"♻ 回收《{w.get('name', '')[:20]}》异常（已忽略）：{e}")
                continue
            if res["ok"]:
                stat["done"] += 1
                stat["added"] += res["added"]
                stat["wrong"] += res["wrong"]
                done[work_id] = {"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "name": w.get("name", ""), "added": res["added"]}
                _save_recycled(done)
            else:
                stat["skip"] += 1
            time.sleep(_UA_WAIT)
    except Exception as e:
        log(f"♻ 回收流程整体异常（已忽略，不影响主流程）：{e}")
    return stat
