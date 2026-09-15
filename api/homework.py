# -*- coding: utf-8 -*-
"""作业模块（2026-09-13 新增）。

背景：学习通「作业」与「章节测验」虽然共用 addStudentWorkNew 提交接口，
但**入口与页面结构完全不同**：

  章节测验（已有 study_work）：
    GET  mooc1.chaoxing.com/mooc-ans/api/work?...        → PC 版答题页
         题干 div.Zy_TItle / 选项 ul>li / 变量 q_type_code 0-4,19

  作业（本模块）：
    列表 GET https://mooc1.chaoxing.com/mooc-ans/work/getAllWork
           ?courseId=&classId=&isdisplaytable=2&mooc=1&ut=s&enc=
    答题 GET https://mooc1-api.chaoxing.com/android/mworkspecial   ← 手机端 SSR
         题干 div.Py-m1-title / 选项 li.more-choose-item / 题型 input#answertype
    提交 POST https://mooc1-api.chaoxing.com/work/addStudentWorkNew（ua=app）

字段对照（来自 CxKitty 实测，权威）：
  单选/多选  answer{id} = "A" / "ABC"
  判断题     answer{id} = "true" / "false"
  填空题     tiankongsize{id}=空数 + answer{id}1 / answer{id}2 …
  表单公共   answerwqbid = "{id},{id2},"

本模块设计为**独立通道**：不改编 study_work 的任何行为（章节测验照旧），
只新增 scan_works / study_work_homework 两条路径，由上层按用户勾选调度。

失败策略（用户 2026-09-13 明确要求）：能自愈的自愈；遇到"需要人判断"的
情况（未知题型、权限被拒、需要人脸/验证码）就**暂停并汇报**，不硬闯。
"""
import re
import time
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from api.answer_check import check_submittable, set_option_letters
from api.logger import logger
from api.learned import LearnedAnswers, normalize_title

# 作业答题页（手机端 SSR，无需浏览器）
PAGE_MOBILE_WORK = "https://mooc1-api.chaoxing.com/android/mworkspecial"
# 作业提交
API_WORK_COMMIT = "https://mooc1-api.chaoxing.com/work/addStudentWorkNew"
# 作业列表（PC 端「作业」标签页数据源）
API_WORK_LIST = "https://mooc1.chaoxing.com/mooc-ans/work/getAllWork"
# 作业详情（获取 enc_work 等提交参数）
PAGE_WORK_VIEW = "https://mooc1.chaoxing.com/mooc-ans/work/view"


class HomeworkNeedsAttention(Exception):
    """作业需要人工介入（未知题型 / 权限被拒 / 已批阅 / 需人脸等）。

    抛出后由上层捕获 → 暂停该作业并汇报，不重试、不硬闯。
    """


class HomeworkUnavailable(Exception):
    """作业当前不可做（已完成 / 已过期 / 教师删除）——非错误，静默跳过。"""


# --------------------------------------------------------------------------
# 列表扫描
# --------------------------------------------------------------------------
def scan_works(session, course: Dict[str, Any]) -> List[Dict[str, Any]]:
    """扫描某门课程下的作业列表。

    Returns:
        [{"workid", "name", "status", "deadline", "raw"}...]
        status: "未交" / "已完成" / "已批阅" / "未开始" 等平台原文
    """
    out: List[Dict[str, Any]] = []
    try:
        resp = session.get(API_WORK_LIST, params={
            "courseId": course.get("courseId"),
            "classId": course.get("clazzId"),
            "isdisplaytable": "2",
            "mooc": "1",
            "ut": "s",
            "enc": course.get("enc", ""),
            "cpi": course.get("cpi", ""),
        }, timeout=20)
    except Exception as e:
        logger.warning(f"作业列表请求失败（{course.get('title')}）：{e}")
        return out

    if resp.status_code != 200:
        logger.warning(f"作业列表 HTTP {resp.status_code}（{course.get('title')}）")
        return out

    html = BeautifulSoup(resp.text, "html.parser")

    # 结构 A：表格行 <tr> 内含作业链接（PC 列表常见）
    for tr in html.select("tr"):
        a = tr.select_one("a[href*='work/view'], a[href*='workId=']")
        if not a:
            continue
        href = a.get("href") or ""
        m = re.search(r"workId=(\d+)", href) or re.search(r"workId=(\d+)", str(a))
        if not m:
            continue
        workid = m.group(1)
        name = (a.get("title") or a.get_text() or "").strip()
        row_text = tr.get_text(" ", strip=True)
        status = ""
        for kw in ("未交", "未提交", "已完成", "已批阅", "待批阅", "未开始", "已过期"):
            if kw in row_text:
                status = kw
                break
        out.append({
            "workid": workid,
            "name": name or f"作业{workid}",
            "status": status,
            "href": href,
            "raw": row_text,
        })

    # 结构 B：卡片式 <div class="work-item"> / <li>
    if not out:
        for node in html.select("div.work-item, li.work-item, div.workList li"):
            a = node.select_one("a[href]")
            if not a:
                continue
            href = a.get("href") or ""
            m = re.search(r"workId=(\d+)", href)
            if not m:
                continue
            out.append({
                "workid": m.group(1),
                "name": (a.get("title") or a.get_text() or "").strip(),
                "status": "",
                "href": href,
                "raw": node.get_text(" ", strip=True),
            })

    # 去重（同一 workid 只留第一条）
    seen = set()
    uniq = []
    for w in out:
        if w["workid"] in seen:
            continue
        seen.add(w["workid"])
        uniq.append(w)

    logger.info(f"课程《{course.get('title')}》作业扫描完成，共 {len(uniq)} 份")
    return uniq


# --------------------------------------------------------------------------
# 作业答题页解析（手机端 DOM）
# --------------------------------------------------------------------------
def _parse_mobile_question(node) -> Optional[Dict[str, Any]]:
    """解析手机端作业页的单道题（div.Py-mian1）。"""
    type_input = node.select_one("input[id^='answertype']")
    if not type_input:
        return None
    try:
        qid = int(type_input["id"][10:])   # 'answertype' 长度 10
    except (KeyError, ValueError):
        return None
    try:
        type_code = int(type_input.get("value") or 0)
    except (TypeError, ValueError):
        type_code = 0

    # 题型码：0 单选 / 1 多选 / 2 填空 / 3 判断 / 4 简答
    type_map = {0: "single", 1: "multiple", 2: "completion",
                3: "judgement", 4: "shortanswer"}
    q_type = type_map.get(type_code, "shortanswer")

    title_node = node.select_one("div.Py-m1-title")
    title = ""
    if title_node:
        parts = []
        for item in title_node.descendants:
            if getattr(item, "name", None) == "img":
                parts.append(f'<img src="{item.get("src", "")}">')
            elif isinstance(item, str):
                parts.append(item)
        title = re.sub(r"[\r\t\n\u00a0 ]+", " ", "".join(parts)).strip()
        # 去掉题号前缀（"1." "2、" "3．" 等）：题库/学习库的键统一不带题号，
        # 否则同一道题在章节测验与作业两条通道里会分裂成两个键，命中不到。
        title = re.sub(r"^\s*\d{1,3}\s*[.、．,，)）]\s*", "", title).strip()

    options: List[str] = []
    blank_count = 0
    if q_type in ("single", "multiple"):
        for li in node.select("li.more-choose-item"):
            em = li.select_one("em.choose-opt")
            key = (em.get("id-param") if em else "") or ""
            desc = li.select_one("div.choose-desc")
            val = desc.get_text(" ", strip=True) if desc else ""
            options.append(f"{key}.{val}" if key else val)
    elif q_type == "completion":
        blank_count = len(node.select("ul.blankList2 > li"))
    elif q_type == "judgement":
        # 判断题没有 ul 选项，固定两个：正确 / 错误
        options = ["正确", "错误"]

    if not title:
        return None

    return {
        "id": qid,
        "title": title,
        "options": "\n".join(options),
        "type": q_type,
        "type_code": type_code,
        "blankCount": blank_count,
    }


def parse_work_page(html_text: str) -> Dict[str, Any]:
    """解析手机端作业答题页。

    Returns:
        {
          "title": 作业名,
          "questions": [题目...],
          "form": {公共提交参数},
          "error": None | str   # 平台给的拦截提示
        }
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # 平台拦截提示（p.blankTips）
    tip = soup.select_one("p.blankTips")
    if tip:
        msg = tip.get_text(strip=True)
        if msg in ("无效的权限", "此作业已被老师删除！", "此作业已被老师删除"):
            raise HomeworkUnavailable(msg)
        raise HomeworkNeedsAttention(f"作业页提示：{msg}")

    # 已批阅（不可再答）
    head_title = ""
    if soup.head and soup.head.title:
        head_title = soup.head.title.get_text(strip=True)
    if "已批阅" in head_title:
        raise HomeworkUnavailable("作业已批阅")

    form = soup.select_one("form#form1")
    if form is None:
        raise HomeworkNeedsAttention("作业页缺少 form#form1（未创建完成或页面改版）")

    def fval(sel, attr="value"):
        node = form.select_one(sel)
        return (node.get(attr, "") if node else "").strip()

    title_node = soup.select_one("h3.py-Title, h3.chapter-title")
    work_title = title_node.get_text(strip=True) if title_node else ""

    params = {
        "workAnswerId": fval("input#workAnswerId"),
        "totalQuestionNum": fval("input#totalQuestionNum"),
        "workRelationId": fval("input#workRelationId"),
        "fullScore": fval("input#fullScore"),
        "enc_work": fval("input#enc_work"),
    }

    questions = []
    for node in soup.select("div.Py-mian1"):
        q = _parse_mobile_question(node)
        if q:
            questions.append(q)

    logger.info(f"作业《{work_title}》解析到 {len(questions)} 道题")
    return {"title": work_title, "questions": questions, "form": params, "error": None}


# --------------------------------------------------------------------------
# 提交表单构造
# --------------------------------------------------------------------------
def build_submit_form(questions: List[Dict[str, Any]],
                      answers: Dict[int, Any]) -> Dict[str, Any]:
    """按平台三类格式构造题目提交字段。

    answers: {qid: 答案}
      - single/multiple: "A" / "ABC"
      - judgement: "true" / "false"
      - completion: ["空1", "空2"] 或 "空1|空2"
    """
    form: Dict[str, Any] = {}
    ids = []
    for q in questions:
        qid = q["id"]
        ids.append(str(qid))
        q_type = q["type"]
        ans = answers.get(qid)
        form[f"answertype{qid}"] = q.get("type_code", 0)

        if q_type in ("single", "multiple"):
            form[f"answer{qid}"] = "" if ans is None else str(ans)
        elif q_type == "judgement":
            if ans is None:
                form[f"answer{qid}"] = ""
            else:
                s = str(ans).strip().lower()
                form[f"answer{qid}"] = "true" if s in (
                    "true", "t", "1", "对", "正确", "是", "√", "yes", "y") else "false"
        elif q_type == "completion":
            if isinstance(ans, list):
                parts = [str(x).strip() for x in ans]
            elif ans is None:
                parts = [""] * max(1, int(q.get("blankCount") or 1))
            else:
                parts = [p.strip() for p in
                         re.split(r"\s*[|｜\n]\s*|\s*[，,]\s*", str(ans)) if p.strip()]
            n = int(q.get("blankCount") or 0) or len(parts)
            if len(parts) < n:
                parts += [""] * (n - len(parts))
            elif len(parts) > n:
                parts = parts[:n]
            form[f"tiankongsize{qid}"] = len(parts)
            for i, p in enumerate(parts):
                form[f"answer{qid}{i + 1}"] = p
        else:
            # 简答等：直接落 answer{id}
            form[f"answer{qid}"] = "" if ans is None else str(ans)

    form["answerwqbid"] = ",".join(ids)
    return form


# --------------------------------------------------------------------------
# 单份作业：抓题 → 作答 → 提交
# --------------------------------------------------------------------------
def fetch_work(session, course: Dict[str, Any], work: Dict[str, Any],
               job_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """拉取作业题单（手机端协议）。"""
    job_info = job_info or {}
    params = {
        "courseid": course.get("courseId"),
        "workid": work.get("workid"),
        "jobid": work.get("jobid", ""),
        "needRedirect": "true",
        "knowledgeid": job_info.get("knowledgeid", work.get("knowledgeid", "")),
        "ut": "s",
        "clazzId": course.get("clazzId"),
        "cpi": course.get("cpi", ""),
        "ktoken": job_info.get("ktoken", work.get("ktoken", "")),
        "enc": work.get("enc", ""),
    }
    resp = session.get(PAGE_MOBILE_WORK, params=params, timeout=25)
    resp.raise_for_status()
    return parse_work_page(resp.text)


def submit_work(session, course: Dict[str, Any], work: Dict[str, Any],
                parsed: Dict[str, Any], questions: List[Dict[str, Any]],
                answers: Dict[int, Any], save_only: bool = False) -> Dict[str, Any]:
    """提交（或临时保存）作业。

    save_only=True 对应平台"保存"（pyFlag=1），用于先落盘再核对。
    """
    f = parsed["form"]
    data = {
        "pyFlag": "1" if save_only else "",
        "courseId": course.get("courseId"),
        "classId": course.get("clazzId"),
        "api": 1,
        "mooc": 0,
        "workAnswerId": f.get("workAnswerId", ""),
        "totalQuestionNum": f.get("totalQuestionNum", ""),
        "fullScore": f.get("fullScore", ""),
        "knowledgeid": "",
        "oldSchoolId": "",
        "oldWorkId": work.get("workid", ""),
        "jobid": work.get("jobid", ""),
        "workRelationId": f.get("workRelationId", ""),
        "enc_work": f.get("enc_work", ""),
        "isphone": "true",
        "userId": "",
        "workTimesEnc": "",
    }
    data.update(build_submit_form(questions, answers))

    resp = session.post(API_WORK_COMMIT, params={
        "keyboardDisplayRequiresUserAction": 1,
        "_classId": course.get("clazzId"),
        "courseid": course.get("courseId"),
        "token": f.get("enc_work", ""),
        "workAnswerId": f.get("workAnswerId", ""),
        "workid": f.get("workRelationId", ""),
        "cpi:": course.get("cpi", ""),
        "jobid": work.get("jobid", ""),
        "knowledgeid": "",
        "ua": "app",
    }, data=data, timeout=30)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"status": False, "msg": f"非 JSON 响应：{resp.text[:200]}"}
