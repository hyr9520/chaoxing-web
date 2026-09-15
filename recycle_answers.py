# -*- coding: utf-8 -*-
"""历史回收：扫描已完成的测验，把"平台判对"的答案沉淀进题库（cache.json）。

要点：
- 题组容器：div（class 含 singleQuesId）；题干在 div.Zy_TItle（含序号+题型+题干）
- 每个题组内 div.newAnswerBx = 一个小问的作答块（我的答案/得分/对错标记）
- 题库键优先沿用 cache.json 已有的键（权威形态：与查询时的标题完全一致），
  用题目特征串（如 Cet-4-1.7-Exercise 01）匹配；没有才用解析出的标题
"""
import json
import os
import re
import sys

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

import web_ui
from api.base import SessionManager
from api.learned import LearnedAnswers, normalize_title
from api.answer import CacheDAO, _assemble_subs
from bs4 import BeautifulSoup

CACHE_PATH = "cache.json"


def norm_title(t: str) -> str:
    """与 answer.py query_all 的标题规范化保持一致（去开头序号、去末尾分值）。

    坑（2026-09-11）：详情页标题开头常是换行/空格再跟题号（"\\n1\\n【听力题】"），
    直接 re.sub(r"^\\d+") 去不掉数字，产出的键会带 "1 " 前缀，
    与运行时查询键对不上。必须先压空白再去数字（与 fetch_audio.norm 一致）。
    """
    t = normalize_title(str(t or ""))            # 先压空白、去首尾
    t = re.sub(r"^\d+[\s\.、]*", "", t)           # 再去开头的题号
    t = re.sub(r"（\d+\.\d+分）$", "", t)
    return normalize_title(t)


def parse_groups(html_text: str) -> list:
    """解析详情页题组：[{title, blocks:[{my_answer, score, mark}]}]"""
    try:
        soup = BeautifulSoup(html_text, "html.parser")
    except Exception:
        return []
    out = []
    for g in soup.find_all("div"):
        if "singleQuesId" not in " ".join(g.get("class") or []):
            continue
        t_el = None
        for cand in g.find_all("div", class_="Zy_TItle"):
            t_el = cand
            break
        if t_el is None:
            t_el = g.find(class_="newZy_TItle")
        # get_text() 无参：保留内部 &nbsp;(\xa0)，与题库键形态一致
        title = norm_title(t_el.get_text() if t_el is not None else "")
        blocks = []
        for b in g.find_all("div", class_="newAnswerBx"):
            mya = b.find("div", class_="answerCon")
            sc = b.find("span", class_="scoreNum")
            mark = ""
            for e in b.find_all(True):
                cls = " ".join(e.get("class") or [])
                if "marking_bandui" in cls:
                    mark = "bandui"
                    break
                if "marking_dui" in cls:
                    mark = "dui"
                    break
                if "marking_cuo" in cls:
                    mark = "cuo"
                    break
            score = None
            if sc is not None:
                try:
                    score = float(sc.get_text(strip=True))
                except (TypeError, ValueError):
                    score = None
            blocks.append({"my_answer": mya.get_text(strip=True) if mya else "",
                           "score": score, "mark": mark})
        out.append({"title": title, "blocks": blocks})
    return out


def infer_type(title: str) -> str:
    if "【单选题】" in title:
        return "single"
    if "【多选题】" in title:
        return "multiple"
    if "【填空题】" in title:
        return "completion"
    if "【判断题】" in title:
        return "judgement"
    if "【听力题】" in title:
        return "single"
    return "single"


def main():
    cfg = web_ui.load_ui_config()
    # 账号不写死（避免随仓库外泄）：用环境变量 CK_ACCOUNT 指定手机号
    _m = [p for p in cfg["profiles"]
          if p.get("username") == os.environ.get("CK_ACCOUNT", "")]
    if not _m:
        raise SystemExit("请用环境变量 CK_ACCOUNT 指定学习通账号（手机号）")
    prof = _m[0]
    ok, msg, courses = web_ui._do_login(prof)
    print("登录:", ok, flush=True)
    cx = web_ui.state["chaoxing"]
    s = SessionManager.get_session()
    c = [x for x in courses if x["courseId"] == "262671420"][0]
    pts = cx.get_course_point(c["courseId"], c["clazzId"], c["cpi"])["points"]

    cache = json.load(open(CACHE_PATH, encoding="utf-8"))
    cache_keys = list(cache.keys())
    learner = LearnedAnswers()
    cdao = CacheDAO()

    stats = {"chapters": 0, "skipped_editable": 0, "questions": 0,
             "dui": 0, "cuo": 0, "bandui": 0, "to_tiku": 0, "updated": 0,
             "subs": 0}

    for p in pts:
        # 只扫 1.x 与 3.x（已完成测验集中在这些章节）；可按需要扩展
        if not (re.match(r"^1\.", p["title"]) or re.match(r"^3\.", p["title"])):
            continue
        job = job_info = None
        for n in range(3):
            pr = {"clazzid": c["clazzId"], "courseid": c["courseId"], "knowledgeid": p["id"],
                  "ut": "s", "cpi": c["cpi"], "v": "2025-0424-1038-3", "mooc2": 1, "num": str(n)}
            try:
                r = s.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=pr, timeout=20)
            except Exception:
                break
            t = re.findall(r"mArg=\{(.*?)\};", r.text.replace(" ", ""))
            if not t:
                break
            try:
                d = json.loads("{" + t[0] + "}")
            except Exception:
                break
            for a in d.get("attachments", []):
                if a.get("type") == "workid":
                    job, job_info = a, d.get("defaults", {})
                    break
            if job:
                break
        if not job:
            continue
        try:
            h = s.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
                "api": "1", "workId": job["jobid"].replace("work-", ""), "jobid": job["jobid"],
                "originJobId": job["jobid"], "needRedirect": "true", "skipHeader": "true",
                "knowledgeid": str(job_info["knowledgeid"]), "ktoken": job_info["ktoken"],
                "cpi": job_info["cpi"], "ut": "s", "clazzId": c["clazzId"], "type": "",
                "enc": job.get("enc", ""), "mooc2": "1", "courseid": c["courseId"]}, timeout=25).text
        except Exception:
            continue
        if "answerwqbid" in h:
            stats["skipped_editable"] += 1      # 未提交的测验：没有成绩可学
            continue
        groups = parse_groups(h)
        if not groups:
            continue
        stats["chapters"] += 1
        for g in groups:
            title = g["title"]
            blocks = g["blocks"]
            qtype = infer_type(title)
            if qtype in ("single", "multiple"):
                my_answer = "".join((b["my_answer"] or "").strip()[:1] for b in blocks)
            else:
                my_answer = " | ".join((b["my_answer"] or "").strip() for b in blocks if b["my_answer"])
            marks = [b["mark"] for b in blocks]
            if marks and all(m == "dui" for m in marks):
                mark = "dui"
            elif marks and all(m == "cuo" for m in marks):
                mark = "cuo"
            elif any(m in ("dui", "cuo", "bandui") for m in marks):
                mark = "bandui"
            else:
                mark = "unknown"
            stats["questions"] += 1
            if mark in ("dui", "cuo", "bandui"):
                stats[mark] += 1

            # 题库键：优先沿用 cache.json 已有的权威键（用题目特征串匹配）
            feat = re.search(r"(Cet-4-[\d.]+-Exercise\s*\d+)", title)
            key = title
            if feat:
                for k in cache_keys:
                    if feat.group(1) in k:
                        key = k
                        break
            learner.record(key, my_answer, mark, None)
            # 小问级沉淀（2026-09-11 用户需求）：整组没全对时，把其中"单独判对"
            # 的小问答案也捞出来（听力题常有几个小问是蒙对的）。
            subs = []
            for b in blocks:
                raw = (b["my_answer"] or "").strip()
                subs.append({
                    "answer": raw[:1] if qtype in ("single", "multiple") else raw,
                    "mark": b["mark"],
                })
            stats["subs"] += learner.record_subs(key, subs)
            save_full = ""
            if mark == "dui" and my_answer:
                save_full = my_answer
            else:
                # 整组没全对，但多轮积累后每个小问都可能已被确认（听力题常见）
                # -> 组装完整答案，等价于全对（2026-09-11）
                n_q = len(blocks)
                if n_q > 1:
                    sv = learner.get_sub_verified(key)
                    if sv and len(sv) >= n_q:
                        save_full = _assemble_subs(sv, n_q)
                        if save_full:
                            learner.record(key, save_full, "dui", None)
                            print(f"  [小问集齐] {key[:44]} -> {save_full}", flush=True)
            if save_full:
                old = cache.get(key)
                if old != save_full:
                    cdao.add_cache(key, save_full)
                    cache[key] = save_full
                    stats["to_tiku"] += 1
                    if old:
                        stats["updated"] += 1

    print()
    print("=== 历史回收完成 ===", flush=True)
    print(f"已处理章节: {stats['chapters']} 个（跳过未提交 {stats['skipped_editable']} 个）", flush=True)
    print(f"题组: {stats['questions']} 个 | 全对 {stats['dui']} / 全错 {stats['cuo']} / 部分对 {stats['bandui']}", flush=True)
    print(f"沉淀进题库: {stats['to_tiku']} 题（其中更新已有错误值 {stats['updated']} 题）", flush=True)
    print(f"小问级沉淀: {stats['subs']} 个（整组未全对但单独判对的小问答案）", flush=True)
    st = learner.stats()
    print(f"学习库累计: 已验证 {st['verified']} 题 / 错题 {st['wrong']} 题（共 {st['total']} 条）", flush=True)
    now = json.load(open(CACHE_PATH, encoding="utf-8"))
    print(f"题库 cache.json 条目: {len(now)} 条", flush=True)


if __name__ == "__main__":
    main()
