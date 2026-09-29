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
import html as _html
import json as _json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup

from api.answer_check import check_submittable, set_option_letters
from api.logger import logger
from api.learned import LearnedAnswers, normalize_title

# ---------------------------------------------------------------------------
# 接口常量（2026-09-26 全面核对，均已真机验证）
# ---------------------------------------------------------------------------
# 作业列表（手机端「作业」总列表页，**一次返回全部课程**，每条形如：
#   <li onclick="goTask(this);" data=".../android/mtaskmsgspecial?taskrefId=..&type=work&enc_task=..">
# 旧版 getAllWork 需要 enc 且无 enc 时返回 3004 错误页 → 已弃用）
PAGE_WORK_LIST = "https://mooc1.chaoxing.com/mooc-ans/work/stu-work"
# 作业详情/卡片页（手机端）——给出 workId / workAnswerId / encWork / 可重做次数
PAGE_WORK_DETAIL = "https://mooc1.chaoxing.com/mooc-ans/android/mtaskmsgspecial"
# 作业「重做」接口（GET，status=1 表示重置成功，随后才能进 doHomeWork 重新作答）
API_WORK_REDO = "https://mooc1.chaoxing.com/mooc-ans/work/phone/redo"
# 作业答题页（手机端 SSR，返回含题目 + 答案的完整表单）
PAGE_WORK_DOHW = "https://mooc1.chaoxing.com/mooc-ans/work/phone/doHomeWork"
# 作业提交（新版 doHomeWork 页面的目标接口）——表单整体 serialize 提交
#   POST .../doNormalHomeWorkSubmit?tempSave=false   （tempSave=true 为暂存草稿）
API_WORK_SUBMIT2 = "https://mooc1.chaoxing.com/mooc-ans/work/phone/doNormalHomeWorkSubmit"

# 作业答题页（旧手机端 SSR）——保留兼容
PAGE_MOBILE_WORK = "https://mooc1-api.chaoxing.com/android/mworkspecial"
# 作业提交
API_WORK_COMMIT = "https://mooc1-api.chaoxing.com/work/addStudentWorkNew"
# 作业列表（旧 PC 端接口，需 enc；保留仅为兼容老数据）
API_WORK_LIST = "https://mooc1.chaoxing.com/mooc-ans/work/getAllWork"
# 作业详情（获取 enc_work 等提交参数）
PAGE_WORK_VIEW = "https://mooc1.chaoxing.com/mooc-ans/work/view"

# 作业状态判据（平台给的中文原文）
_WORK_STATUS_DONE = ("已完成", "已批阅", "待批阅")


class HomeworkNeedsAttention(Exception):
    """作业需要人工介入（未知题型 / 权限被拒 / 已批阅 / 需人脸等）。

    抛出后由上层捕获 → 暂停该作业并汇报，不重试、不硬闯。
    """


class HomeworkUnavailable(Exception):
    """作业当前不可做（已完成 / 已过期 / 教师删除）——非错误，静默跳过。"""


# --------------------------------------------------------------------------
# 列表扫描
# --------------------------------------------------------------------------
def _parse_stu_work_list(html_text: str, course_id: str = "") -> List[Dict[str, Any]]:
    """解析「作业总列表页」(stu-work) 的所有作业条目。

    页面结构（2026-09-26 真机核对，已 unescape 后）：
        <li onclick="goTask(this);"
            data="https://mooc1.chaoxing.com/mooc-ans/android/mtaskmsgspecial
                  ?taskrefId=56066169&msgId=0&courseId=265817798
                  &userId=402584497&clazzId=151250355&type=work&enc_task=47a8...">
            <p>第2章-数据类型与数据结构-选择题</p>
            <span>已完成</span>
            <span>《R语言医学数...》</span>
            <span class="fr">剩余2191小时59分钟</span>
        </li>

    注意：该页面**一次返回全部课程**的作业，需按 courseId 过滤。
    """
    # 页面里中文字段被 HTML 实体编码（如 &#31532; = 第），必须先解码
    body = _html.unescape(html_text)

    out: List[Dict[str, Any]] = []
    pattern = re.compile(
        r'<li\s+onclick="goTask\(this\);"\s+data="([^"]+)"(.*?)</li>', re.S)
    for m in pattern.finditer(body):
        url, seg = m.group(1), m.group(2)
        qs = parse_qs(urlparse(url).query)

        this_course = (qs.get("courseId") or [""])[0]
        if course_id and this_course != str(course_id):
            continue

        title = ""
        tm = re.search(r"<p[^>]*>(.*?)</p>", seg, re.S)
        if tm:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", tm.group(1))).strip()

        status = ""
        for s in re.findall(r"<span[^>]*>([^<]{1,12})</span>", seg):
            s = s.strip()
            if s in ("已完成", "未交", "待批阅", "已批阅", "未开始", "进行中",
                     "未提交", "已过期", "已互评", "待互评"):
                status = s
                break

        # 归属课程名（用于汇报）+ 🔴 完整课程名（用于跨 ID 体系匹配）
        #
        # 2026-09-26 关键发现：stu-work 全量页给出的 courseId 是
        # **「课程班次 ID」**（如 266313880），而 get_course_list() 返回的是
        # **「课程模板 ID」**（如 265817798）——两套 ID 完全不同，交集为 0。
        # 所以按 courseId 匹配作业到课程卡片**永远匹配不上**
        # （这正是用户反复反馈"怎么还是没有作业"的真正根因）。
        #
        # 可靠的桥梁是**课程名**：
        #   <span class="fr">剩余…</span> 前一个是《课程名》，可能被省略号截断
        #   但 aria-label 里有**完整**课程名：
        #   aria-label="作业名称X作业状态Y所属课程安全人机工程剩余时间…"
        course_name = ""
        cm = re.search(r"《([^》]{1,40})》", seg)
        if cm:
            course_name = cm.group(1).strip()

        course_full = ""
        am = re.search(r'aria-label="[^"]*所属课程([^剩"]{1,60})', seg)
        if am:
            course_full = am.group(1).strip()

        # 两种入口形态：
        #   A) mtaskmsgspecial?taskrefId=..&...&enc_task=..   普通作业（可作答）
        #   B) work/phone/eval-list?..&workRelationId=..&enc=.. 互评作业（不做）
        taskrefid = (qs.get("taskrefId") or [""])[0]
        work_rel = (qs.get("workRelationId") or [""])[0]
        is_eval = "/eval-list" in url

        out.append({
            # 新版接口没有 workId，workid 先用 taskrefId 占位，
            # 真正 workId 在 fetch_work_detail 里从详情页拿到
            "workid": taskrefid or work_rel,
            "taskrefid": taskrefid,
            "workrelationid": work_rel,
            "name": title or f"作业{taskrefid or work_rel}",
            "status": status,
            "is_eval": is_eval,
            "courseid": this_course,
            "clazzid": (qs.get("clazzId") or [""])[0],
            "userid": (qs.get("userId") or [""])[0],
            "enc_task": (qs.get("enc_task") or [""])[0] or (qs.get("enc") or [""])[0],
            "course_name": course_name,
            # 🔴 完整课程名（aria-label 里的），用于与 get_course_list 的 title 做匹配
            "course_full": course_full or course_name,
            "cpi": (qs.get("cpi") or [""])[0],
            "href": url,
            "raw": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", seg)).strip()[:200],
        })

    # 去重：优先用 taskrefId，互评作业用 workRelationId。
    # ⚠️ 同一份作业可能同时出现在两条形态里（未作答的 mtaskmsgspecial +
    #    已作答互评的 eval-list），此时**保留可作答的那条**（不是 eval-list）。
    best: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for w in out:
        key = w["taskrefid"] or w["workrelationid"]
        if not key:
            continue
        if key not in best:
            best[key] = w
            order.append(key)
        else:
            # 新来的这条更"可作答"就替换
            old = best[key]
            if old.get("is_eval") and not w.get("is_eval"):
                best[key] = w
    uniq = [best[k] for k in order]
    return uniq


def scan_works(session, course: Dict[str, Any]) -> List[Dict[str, Any]]:
    """扫描某门课程下的作业列表。

    Returns:
        [{"workid","taskrefid","name","status","enc_task",...}...]
        status: "未交" / "已完成" / "已批阅" / "未开始"；空串 = 平台没给状态（视为未完成）
    """
    course_id = str(course.get("courseId") or "")
    params = {
        "courseId": course_id,
        "courseid": course_id,
        "classId": course.get("clazzId"),
        "clazzid": course.get("clazzId"),
        "cpi": course.get("cpi", ""),
        "ut": "s",
    }
    try:
        resp = session.get(PAGE_WORK_LIST, params=params, timeout=25)
    except Exception as e:
        logger.warning(f"作业列表请求失败（{course.get('title')}）：{e}")
        return []

    if resp.status_code != 200:
        logger.warning(f"作业列表 HTTP {resp.status_code}（{course.get('title')}）")
        return []

    works = _parse_stu_work_list(resp.text, course_id=course_id)

    # 兜底：新版接口没解析到内容时，回退旧 PC 接口（某些老课程仍需 enc）
    if not works:
        works = _scan_works_legacy(session, course)

    logger.info(f"课程《{course.get('title')}》作业扫描完成，共 {len(works)} 份")
    return works


def _scan_works_legacy(session, course: Dict[str, Any]) -> List[Dict[str, Any]]:
    """旧版 PC 作业列表接口（getAllWork，需 enc）。仅作兜底。"""
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
        logger.warning(f"[兜底] 作业列表请求失败（{course.get('title')}）：{e}")
        return out

    if resp.status_code != 200:
        return out

    # 平台错误页（3004 / 校验失败）直接判空，别把错误页当成"0 份作业"
    if re.search(r"(3004|enc校验失败|长时间没有操作)", resp.text):
        return out

    soup = BeautifulSoup(resp.text, "html.parser")
    for tr in soup.select("tr"):
        a = tr.select_one("a[href*='work/view'], a[href*='workId=']")
        if not a:
            continue
        href = a.get("href") or ""
        m = re.search(r"workId=(\d+)", href)
        if not m:
            continue
        row_text = tr.get_text(" ", strip=True)
        status = ""
        for kw in ("未交", "未提交", "已完成", "已批阅", "待批阅", "未开始", "已过期"):
            if kw in row_text:
                status = kw
                break
        out.append({
            "workid": m.group(1),
            "taskrefid": m.group(1),
            "name": (a.get("title") or a.get_text() or "").strip() or f"作业{m.group(1)}",
            "status": status,
            "href": href,
            "raw": row_text[:200],
        })
    return out


# --------------------------------------------------------------------------
# 作业详情（拿 workId / workAnswerId / 可重做次数）
# --------------------------------------------------------------------------
def parse_work_detail(html_text: str) -> Dict[str, Any]:
    """解析 mtaskmsgspecial 作业卡片页。

    真机核对字段（2026-09-26）：
        function redo() {
            var workId = "56066169";
            var classId = "151250355";
            var workAnswerId = "54822857";
            var courseId = "265817798";
            var redoTimes = parseInt('10') + parseInt('0') - parseInt('0');
        }
        function gotodo() {
            var url = .../work/phone/doHomeWork?courseId=..&workAnswerId=..
                      &workId=..&classId=..&cpi=..&status=1&...&enc=f2fc...
        }
    """
    body = _html.unescape(html_text)

    def _num(name: str) -> str:
        m = re.search(rf'var\s+{name}\s*=\s*"(\d+)"', body)
        return m.group(1) if m else ""

    info: Dict[str, Any] = {
        "workid": _num("workId"),
        "classid": _num("classId"),
        "workanswerid": _num("workAnswerId"),
        "courseid": _num("courseId"),
        "cpi": "",
        "enc": "",
        "redotimes": 0,
        "redo_total": 0,
        "redo_used": 0,
        "score_rule": "",
        "score": "",
        "question_total": 0,
        "name": "",
    }

    # ---- 重做次数（2026-09-26 真机确认的三段式）----
    #   redoTimes = parseInt('10') + parseInt('0') - parseInt('2');
    #               └ 总上限 ─┘    └ 额外加 ─┘   └ 已用掉 ─┘
    # 例：10+0-2 = 还能重做 8 次。
    #
    # 🔴🔴 2026-09-26 22:05 修：**三段里后两段常常是空串**！
    #   实测本作业页面原文：
    #       var redoTimes = parseInt('99') + parseInt('') - parseInt('');
    #   而旧正则要求 `parseInt('(\d+)')` 至少一位数字 → **匹配失败**
    #   → info["redotimes"] 保持初始 0
    #   → 上层看到"可重做 0 次"，重做请求被自己拦下（"重做被拒：重做失败"）。
    #   ⇒ 正则改成 `'(\d*)'`，空串按 0 算。
    m = re.search(r"redoTimes\s*=\s*parseInt\('(\d*)'\)\s*\+\s*parseInt\('(\d*)'\)"
                  r"\s*-\s*parseInt\('(\d*)'\)", body)
    if m:
        def _i(x):
            return int(x) if (x or "").strip() else 0
        total, extra, used = (_i(x) for x in m.groups())
        info["redo_total"] = total + extra
        info["redo_used"] = used
        info["redotimes"] = total + extra - used
    else:
        # 兜底：页面里可能只剩单值形式 parseInt('99')
        m1 = re.search(r"redoTimes\s*=\s*parseInt\('(\d+)'\)", body)
        if m1:
            info["redotimes"] = int(m1.group(1))
            info["redo_total"] = int(m1.group(1))

    # ---- 取分规则（🔴 决定重做有没有风险）----
    #   var tip = "最高成绩";  → 重做考低了不亏（取历史最高）
    #   var tip = "本次成绩";  → 重做覆盖上一次（考低了就亏了）
    m = re.search(r'var\s+tip\s*=\s*"([^"]{1,12})"', body)
    if m:
        info["score_rule"] = m.group(1).strip()
    if not info["score_rule"]:
        # 兜底：整页找关键词
        if "最高成绩" in body:
            info["score_rule"] = "最高成绩"
        elif "本次成绩" in body:
            info["score_rule"] = "本次成绩"

    m = re.search(r'/doHomeWork\?([^"\'\s]+)', body)
    if m:
        qs = parse_qs(m.group(1))
        info["enc"] = (qs.get("enc") or [""])[0]
        info["cpi"] = (qs.get("cpi") or [""])[0]

    # 得分：<p class="Number">100 <span>分</span></p>
    m = re.search(r'class="Number"[^>]*>\s*([\d.]+)', body)
    if not m:
        m = re.search(r'>\s*([\d.]+)\s*分\s*<', body)
    if m:
        info["score"] = m.group(1)

    # 题量（"共100题，客观题100道"）
    m = re.search(r'共\s*(\d+)\s*题', body)
    if m:
        info["question_total"] = int(m.group(1))

    # 作业名（<title> 往往是"作业"，标题在 h3 / p.title）
    m = re.search(r'<h3[^>]*>([^<]{2,60})</h3>', body)
    if m:
        info["name"] = m.group(1).strip()

    return info


def fetch_work_detail(session, course: Dict[str, Any],
                      work: Dict[str, Any]) -> Dict[str, Any]:
    """拉取单份作业的详情页，返回 {workid, workanswerid, enc, redotimes, ...}。

    🔴 2026-09-26 关键修正：**必须优先用列表页给的 `href`**。
    实测对比（同一份作业）：
      · 走 href（带完整 enc_task + userId + clazzId + cpi）→ 12751B，能解析出
        redoTimes / tip（重做次数与取分规则）
      · 手工拼参数（只给 taskrefId+courseId+clazzId）→ **3991B 壳页面**，
        三个字段一个都拿不到
    `enc_task` 是平台侧的签名，漏一个参数就被打回壳页，所以能拿到 href 就别拼。

    🔴 2026-09-26 二次修正：`cpi` 也是**必备**参数（实测漏了它 → 12841B 变成壳页）。
    拼参数兜底时会自动从 href 里抽 cpi；两处都没有才判失败。
    """
    tid = work.get("taskrefid") or work.get("workid")
    href = (work.get("href") or "").strip()
    cpi = (work.get("cpi") or "").strip()

    # cpi 缺失时，尝试从 href 的 query 里补（平台把 cpi 藏在 doHomeWork URL 里）
    if not cpi and href:
        try:
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(href).query)
            cpi = (q.get("cpi") or [""])[0]
        except Exception:
            pass

    resp = None
    if href.startswith("http"):
        try:
            resp = session.get(href, timeout=25)
        except Exception as e:
            logger.warning(f"作业详情 href 请求失败（taskrefId={tid}）：{e}")
            resp = None

    # 壳页判定：不是 200，或页面过小（<6000B 基本就是被打回的壳页），
    # 或**压根解析不到 workId**（大小够但缺字段 = 也是壳页）
    need_fallback = (
        resp is None
        or resp.status_code != 200
        or len(resp.text) < 6000
        or "var workId" not in resp.text
    )

    if need_fallback:
        params = {
            "taskrefId": tid,
            "msgId": "0",
            "courseId": work.get("courseid") or course.get("courseId"),
            "userId": work.get("userid", ""),
            "clazzId": work.get("clazzid") or course.get("clazzId"),
            "type": "work",
            "enc_task": work.get("enc_task", ""),
            "cpi": cpi or course.get("cpi", ""),
            "ut": "s",
        }
        logger.info(f"[详情兜底] taskrefId={tid} cpi={params['cpi'] or '(空)'} "
                    f"enc_task={'有' if params['enc_task'] else '无'}")
        resp = session.get(PAGE_WORK_DETAIL, params=params, timeout=25)

    resp.raise_for_status()
    info = parse_work_detail(resp.text)
    if not info.get("workid"):
        raise HomeworkNeedsAttention(
            f"作业详情页未解析到 workId（taskrefId={tid}），"
            f"页面 {len(resp.text)}B，可能 href/cpi/enc_task 缺失或页面已改版")
    return info


def redo_work(session, course: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """调用平台「重做」接口，重置上一次作答记录。

    ⚠️ 副作用：会覆盖上一次作答，成绩按「最高成绩」保留（平台原文）。
    返回 {"status": 1, "msg": "..."} —— status=1 表示重置成功。
    """
    params = {
        "workId": detail.get("workid"),
        "classId": detail.get("classid") or course.get("clazzId"),
        "courseId": detail.get("courseid") or course.get("courseId"),
        "workAnswerId": detail.get("workanswerid"),
    }
    resp = session.get(API_WORK_REDO, params=params, timeout=25)
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        return {"status": 0, "msg": f"非 JSON 响应：{resp.text[:200]}"}


# --------------------------------------------------------------------------
# 新答题页（doHomeWork）解析
# --------------------------------------------------------------------------
def _parse_dohw_question(node) -> Optional[Dict[str, Any]]:
    """解析 doHomeWork 页面上的一道题。

    结构（2026-09-26 真机核对）：
        <div class="Answer workTextWrap">
            <span data="answer215260741" class="choose check"
                  aria-label="选项C.报错" orichoice="C">C</span>
            <div id="C" class="centerSpan workWrap">报错</div>
        </div>
        ...
        <input type="hidden" name="answer215260741" id="answer215260741"
               value="B" class="answerInput"/>       ← 上次作答（可能为空）
        <input type="hidden" name="type215260741" value="0"/>
        <input type="hidden" name="questionId" value="215260741"/>
    """
    qid = ""
    for inp in node.select("input[name='questionId']"):
        v = (inp.get("value") or "").strip()
        if v:
            qid = v
            break
    if not qid:
        return None

    # 题型码
    t_inp = node.select_one(f"input[name='type{qid}']")
    try:
        type_code = int((t_inp.get("value") if t_inp else "") or 0)
    except (TypeError, ValueError):
        type_code = 0
    # 0 单选 / 1 多选 / 2 填空 / 3 判断 / 4 简答
    type_map = {0: "single", 1: "multiple", 2: "completion",
                3: "judgement", 4: "shortanswer"}
    q_type = type_map.get(type_code, "shortanswer")

    # 题干：优先取标题区（题号 + 文本），排除选项区
    title = ""
    label = node.select_one("div.centerSpan.workWrap, div.workWrap")
    if label:
        title = label.get_text(" ", strip=True)
    if not title:
        # 退化：整块的纯文本（去掉选项行）
        title = node.get_text(" ", strip=True)
    title = re.sub(r"[\r\t\n\u00a0 ]+", " ", title).strip()
    # 去题号前缀
    title = re.sub(r"^\s*\d{1,3}\s*[.、．,，)）]\s*", "", title).strip()
    # 去移动端的 "1/100" 之类页码
    title = re.sub(r"\s*\d+\s*/\s*\d+\s*$", "", title).strip()

    # 上次作答
    # 🔴🔴 2026-09-26 22:45 多选字段名是 **answers{qid}（复数）**，
    #    只找 answer{qid} 会让多选的"上次作答"永远读空。
    a_inp = (node.select_one(f"input[name='answer{qid}']")
             or node.select_one(f"input[name='answers{qid}']"))
    prev_answer = (a_inp.get("value") if a_inp else "") or ""
    # 填空题的"上次作答"存在 textarea 里（name=answer{qid}{空序号}）
    if not prev_answer:
        _ta = node.select_one(f"textarea[name^='answer{qid}']")
        if _ta is not None:
            prev_answer = (_ta.get_text() or "").strip()

    # 每题分值（真机 score{qid}；提交时必须原样回传）
    score = 0
    s_inp = node.select_one(f"input[name='score{qid}']")
    if s_inp is not None:
        try:
            score = float((s_inp.get("value") or "0").strip() or 0)
        except (TypeError, ValueError):
            score = 0

    options: List[str] = []
    blank_count = 0
    if q_type in ("single", "multiple"):
        # 🔴 2026-09-27 修：单选选项节点是 span.choose，**多选是 span.dxcheck**。
        # 只匹配 choose 会让多选 options 恒为空 → AI 看不到选项（提示词里
        # "选项：" 后面是空的）→ 瞎猜/回"题目信息缺失" → 映射字母也必然失败
        # （真机 100 题作业 20 道多选全军覆没的根因）。
        for span in node.select("div.Answer span.choose, span.choose, span.dxcheck"):
            letter = (span.get("orichoice") or span.get_text(strip=True) or "").strip()
            aria = span.get("aria-label") or ""
            # aria-label 形如 "选项C.报错"，取点号后的内容
            text = ""
            m = re.search(r"选项\s*[A-Za-z]?[.、．]\s*(.*)$", aria)
            if m:
                text = m.group(1).strip()
            if not text:
                # 退而取同组 div.centerSpan.workWrap
                div = span.find_next_sibling("div")
                if div:
                    text = div.get_text(" ", strip=True)
            # 🔴 2026-09-27 修：修改答案/重做路径下，**已作答的选项文本尾部
            # 会带平台 UI 状态词**（"已选择"）—— 真机实测 options 抓成
            # "A.封装已选择"，于是 AI 看到脏选项答错、映射也对不上
            # （"北宋" vs "北宋已选择" 相似度 0.57 < 0.8 → 映射失败丢题）。
            text = re.sub(r"\s*(已选择|已选|已作答|已答)\s*$", "", text).strip()
            if letter:
                options.append(f"{letter}.{text}" if text else letter)
    elif q_type == "completion":
        # 🔴🔴 2026-09-26 19:20 修：真机填空题的答案容器是 **textarea**
        #     （name=answer{qid}{空序号}），旧代码只数 `input` → 永远数出 0 个空
        #     → 提交时 blankNum 算错 → 填空题答案落不上去。
        #     判据优先级：blankNum{qid} 的值最权威（形如 "1,"）。
        bn = node.select_one(f"input[name='blankNum{qid}']")
        if bn is not None:
            raw = (bn.get("value") or "").strip()
            cnt = len([x for x in raw.split(",") if x.strip()])
            blank_count = cnt or 1
        else:
            areas = node.select(f"textarea[name^='answer{qid}']")
            if areas:
                blank_count = len(areas)
            else:
                blank_count = len(node.select(f"input[name^='answer{qid}']")) or 1
    elif q_type == "judgement":
        options = ["正确", "错误"]

    if not title:
        return None

    return {
        "id": int(qid) if qid.isdigit() else qid,
        "title": title,
        "options": "\n".join(options),
        "type": q_type,
        "type_code": type_code,
        "blankCount": blank_count,
        "score": score,
        "prev_answer": prev_answer,
    }


def parse_dohw_page(html_text: str) -> Dict[str, Any]:
    """解析 doHomeWork 答题页。"""
    body = _html.unescape(html_text)
    soup = BeautifulSoup(body, "html.parser")

    # 平台拦截提示
    tip = soup.select_one("p.blankTips")
    if tip:
        msg = tip.get_text(strip=True)
        if msg in ("无效的权限", "此作业已被老师删除！", "此作业已被老师删除"):
            raise HomeworkUnavailable(msg)
        raise HomeworkNeedsAttention(f"作业页提示：{msg}")

    m = re.search(r"(无效的参数|作业不存在|已批阅|不可作答)", body)
    if m and "submitTest" not in body:
        raise HomeworkUnavailable(m.group(1))

    form = soup.select_one("form#submitTest") or soup.select_one("form")
    if form is None:
        raise HomeworkNeedsAttention("答题页缺少 form（页面改版）")

    def fval(sel, attr="value"):
        """取隐藏域的值。

        🔴 2026-09-26 19:23 修：真机里 `courseId` / `workRelationId` / `classId`
        这几个字段**在 form 内重复出现 2~3 次，且多数只有 name、没有 id**。
        原先统一用 `select_one("input#xxx")`（按 id 选）→ 对这几个字段取不到值。
        现在：先按 id 选；选不到再把 `#id` 当 `[name=id]` 再试一次。
        """
        node = form.select_one(sel)
        if node is None:
            m2 = re.match(r"(input|textarea|select)#([\w-]+)$", sel)
            if m2:
                node = form.select_one(
                    f"{m2.group(1)}[name='{m2.group(2)}']")
        return (node.get(attr, "") if node else "").strip()

    work_title = ""
    tm = soup.select_one("h1, h2, h3, div.papertitle, span.tit")
    if tm:
        work_title = tm.get_text(strip=True)

    params = {
        "workRelationAnswerId": fval("input#workRelationAnswerId"),
        "workRelationId": fval("input#workRelationId"),
        "encWork": fval("input#encWork"),
        "enc": fval("input#enc"),
        "knowledgeid": fval("input#knowledgeid") or "0",
        "courseId": fval("input#courseId"),
        "classId": fval("input#classId"),
        "source": fval("input#source") or "0",
        # 🔴 2026-09-26 19:23 补：真机上这几个字段也存在（多为空串），
        #    提交时需原样回传，否则与真机表单不一致、平台可能拒绝。
        "workExamUploadUrl": fval("input#workExamUploadUrl"),
        "workExamUploadCrcUrl": fval("input#workExamUploadCrcUrl"),
        "vx": fval("input#vx") or "0",
        "workTimesEnc": fval("input#workTimesEnc"),
    }

    # 题目：每个题目块含一个 input[name='questionId']
    questions: List[Dict[str, Any]] = []
    seen_ids = set()
    for inp in soup.select("input[name='questionId']"):
        # 向上找到包含该题的容器
        # 🔴 2026-09-26 22:45：多选题的答案框是 `answers{qid}`（复数），
        #    只判 `answer{qid}` 会漏 → 容器定位到错误的层级。
        node = inp
        _qid = (inp.get("value") or "").strip()
        for _ in range(8):
            if node.parent is None:
                break
            node = node.parent
            if (_qid and (node.select_one(f"input[name='answer{_qid}']")
                          or node.select_one(f"input[name='answers{_qid}']"))):
                break
        q = _parse_dohw_question(node) if node else None
        if q and q["id"] not in seen_ids:
            seen_ids.add(q["id"])
            questions.append(q)

    # 兜底：按 answerInput 找题（某些页面题目块边界不同）
    if not questions:
        for a_inp in soup.select("input.answerInput[name^='answer']"):
            name = a_inp.get("name") or ""
            qid = name[7:] if name.startswith("answers") else name[6:]
            if not qid or qid in seen_ids:
                continue
            node = a_inp
            for _ in range(8):
                if node.parent is None:
                    break
                node = node.parent
                if node.select_one("div.Answer"):
                    break
            q = _parse_dohw_question(node) if node else None
            if q:
                seen_ids.add(q["id"])
                questions.append(q)

    # 🔴🔴🔴 2026-09-26 20:00 关键补充：**总题数 / 当前题号**
    #
    # 真机铁证（答题页 HTML）：
    #     <b aria-hidden="true">1/50</b>答题卡
    #     <span aria-label="共50题 第1题"></span>
    #     <input type="hidden" name="index" value="0">
    #
    # ⇒ doHomeWork **每次只返回当前 index 那 1 道题**（form 里只有 1 个
    #    questionId、1 个 index）。50 题作业必须翻 50 次。
    #    **这就是「50 题只做了 1 题、只拿 2 分」的根因。**
    #
    # 注意坑：填空题页面里还有一个无关的 "3/750"（字符计数），
    # 所以 X/Y 正则要**限定在答题卡**里，不能瞎抓。
    cur_index = 0
    try:
        cur_index = int((fval("input#index") or "0").strip() or 0)
    except (TypeError, ValueError):
        cur_index = 0

    total = 0
    # ① 最权威：<span aria-label="共50题 第1题">
    m = re.search(r'aria-label="共\s*(\d+)\s*题', body)
    if m:
        total = int(m.group(1))
    # ② 退而求其次：<b ...>1/50</b>答题卡
    if not total:
        m = re.search(r">\s*\d+\s*/\s*(\d+)\s*</b>\s*答题卡", body)
        if m:
            total = int(m.group(1))
    # ③ 再退：正文里第一处「共N题」
    if not total:
        m = re.search(r"共\s*(\d+)\s*题", body)
        if m:
            total = int(m.group(1))
    if total <= 0:
        total = len(questions)      # 拿不到就按已解析数量走（不死循环）

    logger.info(f"作业《{work_title}》解析到 {len(questions)} 道题 "
                f"（当前 index={cur_index}，共 {total} 题）")
    return {
        "title": work_title,
        "questions": questions,
        "form": params,
        "index": cur_index,
        "total": total,
        "error": None,
    }


# 🔴🔴🔴 2026-09-26 22:15：负载均衡粘性 cookie —— 翻页的隐藏杀手
#
# 平台（mooc1.chaoxing.com）在响应里会 Set-Cookie 三个**粘性路由** cookie：
#     route（追加第二个值） / k8s / jrose
# 它们把后续请求固定路由到**同一个后端节点**；该节点缓存了"当前题号"，
# 于是 `index` 参数被彻底忽略 → 永远返回第 1 题。
#
# 实测（同一 cookie 连发 index=0..9）：
#     同 session 连发  → 永远落点 0
#     每次清掉粘性 cookie → 0,1,2,...,19 全部正确
#
# ⇒ 取答题页前必须剔除这三个。**只踢这三个**：
#    JSESSIONID / vc3 / _uid / UID / fid / cx_p_token / p_auth_token 等
#    都是登录态，动了会掉线。
_STICKY_COOKIE_NAMES = ("route", "k8s", "jrose")


def _drop_sticky_cookies(session) -> None:
    """把负载均衡粘性 cookie 从 session 里剔除（失败也不影响主流程）。"""
    try:
        jar = getattr(session, "cookies", None)
        if jar is None:
            return
        for name in _STICKY_COOKIE_NAMES:
            # requests 的 cookiejar 可能有同名多条，逐个清
            try:
                while name in jar:
                    jar.clear(name, domain=None, path=None)
            except KeyError:
                pass
            except Exception:
                # 退化为按 list 过滤（兼容非标准 jar）
                try:
                    dead = [c for c in list(jar) if c.name == name]
                    for c in dead:
                        jar.clear(c.domain, c.path, c.name)
                except Exception:
                    pass
    except Exception:
        pass


def fetch_work_page(session, course: Dict[str, Any], detail: Dict[str, Any],
                    cpi: str = "", index: int = 0,
                    work_answer_id: Any = None) -> Dict[str, Any]:
    """打开 doHomeWork 答题页（须先 redo 才能重新作答）。

    🔴🔴🔴 2026-09-26 22:05 **定论：请求答题页时【绝不能传 workAnswerId】**
    
    这是"100 题作业只做到 9 题"的最终根因。实测对照（同一份 100 题作业，
    cookie 完全一致，只改 URL 参数）：

      A. 带 `&workAnswerId=55683569`（旧写法）：
           请求 i=0/1/2/30/60 → **全部落点 0**，qid 全是 405811899
           ⇒ 平台进入「续答上次记录」模式，**完全忽略 index**
      B. **不带 workAnswerId**，只传 `&index=N`：
           请求 i=0 → 落点 0 (qid=405811899)
           请求 i=1 → 落点 1 (qid=405811900)
           请求 i=2 → 落点 2 (qid=405811901)
           请求 i=20 → 落点 20 (qid=405811919)   ✅ 100% 尊重 index

    ⇒ 平台语义：`workAnswerId` = "我要接着这条记录作答"（index 失义）；
      只有**不带**它时，`index` 才作为「取第 N 题」的正经参数生效。
    **暂存/交卷**时才需要 workRelationAnswerId（见 submit_dohw）。

    🔴🔴🔴 2026-09-26 22:15 **第二层根因：负载均衡粘性 cookie 会让 index 失效**

    只去掉 workAnswerId 还不够！实测同一 cookie 连续请求 i=0..9：
        同 session 连发  → **永远落点 0**
        每次新 session   → 0,1,2,...,9  ✅ 全部正确

    抓到真凶（对比"请求前 / 请求后"的 cookie 集合）：
        请求一次后服务端**新塞了三个粘性 cookie**：
            route:  追加第二个值 '0a65fa708818ad1416475328b69707fd'
            k8s:    '1790431193.008.5068.67054'
            jrose:  '254400EB7FFE6479F176C1971FCF29E0.mooc-p4-2572419206-jdnk6'
      ⇒ 平台用它们把连接**粘到某个后端节点**，该节点缓存了"当前题号"，
        于是后续请求全命中缓存、**忽略 index**（表现为永远第 1 题）。
      ⇒ **清掉这三个 cookie 后 index 立刻生效**（实测 0..19 全对）。

    做法：请求答题页前，把这三个粘性 cookie 从 session 里剔除
    （只踢这三个，**绝不动 JSESSIONID / vc3 / _uid 等登录态**）。

    Args:
        index: 要取第几题（0 基）。
        work_answer_id: ⚠️ **已废弃，恒为 None**。保留形参仅为兼容旧调用点，
                        传入也会被忽略（传了就打回第 0 题，见上面的实测）。
    """
    # 🔴 清粘性 cookie（index 生效的前提）——见上面第二层根因
    _drop_sticky_cookies(session)

    cpi = cpi or detail.get("cpi") or course.get("cpi", "")
    params = {
        "courseId": detail.get("courseid") or course.get("courseId"),
        "workId": detail.get("workid"),
        "classId": detail.get("classid") or course.get("clazzId"),
        "cpi": cpi,
        "oldWorkId": "",
        "status": "1",
        "mooc": "1",
        "source": "0",
        "checkIntegrity": "true",
        "enc": detail.get("enc", ""),
        # 🔴 翻页靠它：不传 = 永远第 1 题；但**不能**和 workAnswerId 同时传
        "index": str(index),
    }
    resp = session.get(PAGE_WORK_DOHW, params=params, timeout=30)
    resp.raise_for_status()
    return parse_dohw_page(resp.text)


# --------------------------------------------------------------------------
# 🔴🔴🔴 逐题翻页：把「一题一页」的 doHomeWork 拼成完整题库
# --------------------------------------------------------------------------
def iter_dohw_questions(session, course: Dict[str, Any], detail: Dict[str, Any],
                        answer_fn, *, cpi: str = "", max_questions: int = 500,
                        save_each: bool = True, on_progress=None):
    """**逐题翻页**遍历整份作业，边翻边答边暂存。

    为什么必须这样（2026-09-26 真机定论）：
        doHomeWork 页面**一题一页**，`form` 里只有 1 个 `questionId` / 1 个
        `index`。要拿第 N 题必须带 `&index=N` 重新请求。
        以前的代码不传 index → 永远第 0 题 → 50 题作业只做了 1 题 → 2 分。

    翻页协议（真机 `getTheNextQuestion` + `submitFormNew` 反推）：
        1. GET  doHomeWork?...&index=i          → 拿第 i 题
        2. 作答（answer_fn(q) 返回答案）
        3. POST doNormalHomeWorkSubmit?tempSave=true
           （data = 该题表单；**必须先暂存**，否则翻页会丢答案）
           → response.answerId = **新 workRelationAnswerId**
        4. i += 1，用新 answerId 请求下一页
        5. 翻完 N 题 → 由调用方再 POST tempSave=false 正式交卷

    Args:
        answer_fn: 回调 `(question_dict) -> answer`，由上层接入题库/AI。
                   返回 None 表示无答案（会记为空）。
        max_questions: 安全上限（防平台 total 异常导致死循环）。
        save_each: 是否每题都暂存（True 才符合真机协议，否则答案会丢）。
        on_progress: 可选回调 `(done, total, question)`，用于前端进度。

    Returns:
        {
          "questions": [q, ...],       # 按题序排列的全部题目
          "answers":   {qid: ans},     # 对应答案
          "total": N,
          "answer_id": 最终作答记录 ID,
          "saved": 成功暂存数,
          "errors": [...],
        }
    """
    all_questions: List[Dict[str, Any]] = []
    answers: Dict[Any, Any] = {}
    errors: List[str] = []

    # 第 0 题先拿，顺便得到 total 和当前 answerId
    parsed = fetch_work_page(session, course, detail, cpi=cpi, index=0)
    total = int(parsed.get("total") or 0)
    if total <= 0:
        total = 1
    total = min(total, max_questions)

    cur_answer_id = detail.get("workanswerid")
    saved = 0

    # 🔴🔴🔴 2026-09-26 22:05 **最终形态：for i + index=N 逐页取**
    #
    # 因为 `fetch_work_page` 已改为**不传 workAnswerId**，
    # 平台的 `index` 参数才真正生效（实测 i=0→0, i=1→1, ..., i=20→20）。
    #
    # 之前的两次弯路（都记在这里，避免后人再踩）：
    #   ① `index` 缺失 → 永远第 0 题 → 50 题只做 1 题（2 分）
    #   ② 补了 index，但**同时**传了 workAnswerId → 平台进"续答模式"忽略 index
    #      → 100 题只收到 9 道不重复的题（去重把重复的回显全滤掉）
    #
    # 暂存仍按题目逐题做（真机 `submitForm(true)`），且**暂存必须带**
    # workRelationAnswerId —— 但那是 submit_dohw 的事，与取页无关。
    cur_answer_id = detail.get("workanswerid")
    saved = 0
    for i in range(total):
        try:
            page = fetch_work_page(session, course, detail, cpi=cpi, index=i)
        except Exception as e:
            errors.append(f"第 {i + 1} 题翻页失败：{type(e).__name__}: {e}")
            logger.warning(f"[翻页] index={i} 取题失败：{e}")
            break

        qs = page.get("questions") or []
        if not qs:
            errors.append(f"第 {i + 1} 题页面解析不到题目")
            break

        # 一页只有 1 题；若平台哪天一次给多题，也一并收下
        for q in qs:
            if any(str(x["id"]) == str(q["id"]) for x in all_questions):
                continue        # 去重（平台可能回显同一题）
            all_questions.append(q)

            ans = None
            try:
                ans = answer_fn(q)
            except Exception as e:
                logger.warning(f"题目 {q['id']} 作答回调异常：{e}")
            if ans is None or (isinstance(ans, str) and not ans.strip()):
                logger.warning(f"[翻页] 第 {i + 1} 题 {q['id']} 未获答案")
            answers[q["id"]] = ans

            if on_progress:
                try:
                    on_progress(i + 1, total, q)
                except Exception:
                    pass

        # 🔴 每题都暂存（真机协议）：既保住答案，又拿回新 answerId
        if save_each:
            try:
                r = submit_dohw(session, course, page, qs,
                                {q["id"]: answers.get(q["id"]) for q in qs},
                                save_only=True, work_answer_id=cur_answer_id)
                if r.get("status"):
                    saved += 1
                    nid = str(r.get("answerId") or "").strip()
                    if nid:
                        cur_answer_id = nid
                else:
                    errors.append(f"第 {i + 1} 题暂存被拒：{r.get('msg')}")
                    logger.warning(f"[翻页] index={i} 暂存失败：{r.get('msg')}")
            except Exception as e:
                errors.append(f"第 {i + 1} 题暂存异常：{type(e).__name__}: {e}")
                logger.warning(f"[翻页] index={i} 暂存异常：{e}")

    logger.info(f"[翻页] 完成：共取到 {len(all_questions)}/{total} 题，"
                f"暂存 {saved} 次，最终 answerId={cur_answer_id}")
    return {
        "questions": all_questions,
        "answers": answers,
        "total": total,
        "answer_id": cur_answer_id,
        "saved": saved,
        "errors": errors,
    }



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


# --------------------------------------------------------------------------
# 新版作业通道（doHomeWork 页面 + doNormalHomeWorkSubmit 提交）
# --------------------------------------------------------------------------
def build_dohw_form(parsed: Dict[str, Any],
                    questions: List[Dict[str, Any]],
                    answers: Dict[Any, Any]) -> Dict[str, Any]:
    """按 doHomeWork 页面的表单结构构造提交字段。

    🔴🔴 2026-09-26 19:20 **完整重写** —— 此前字段结构与真机严重不符，
    这就是「作业根本做不了」的架构性根因。

    真机实证（`_dump_第2章-...-判断题.html` / `...-填空题.html`，逐字段抠出）：

      判断题：
        <input type="hidden" name="answer215292590" value="false">
        <input type="hidden" name="type215292590"   value="3">
        <input type="hidden" name="score215292590"  value="2.0">
        <input type="hidden" name="questionId"      value="215292590">   ← 无后缀！
        <input type="hidden" name="index"           value="0">           ← 无后缀！

      填空题：
        <textarea name="answer2152924721">-1</textarea>                  ← 空序号从 1 开始
        <input type="hidden" name="type215292472"    value="2">
        <input type="hidden" name="score215292472"   value="5.0">
        <input type="hidden" name="questionId"       value="215292472">
        <input type="hidden" name="index"            value="0">
        <input type="hidden" name="blankNum215292472" value="1,">        ← 空数, 逗号结尾

    ⚠️ 旧版写错的 5 处（**每一处都会让平台收不到答案**）：
      ① `questionId` 被写成 `""`（应=当前题 qid）
      ② `index` 被写成 `""`（应=当前题序号）
      ③ 臆造了 `questionId{qid}` / `index{qid}` 两个平台不存在的字段
      ④ 完全没有 `score{qid}`（平台靠它知道每题分值）
      ⑤ 填空题用 `tiankongsize{qid}`，真机是 **`blankNum{qid}`**（值形如 "1,"）
         → 填空题的答案**永远落不上去**（用户报障"填空题做不了"就是这个）

    ⚠️ 为什么 `questionId`/`index` 要**逐题覆盖**成同一个键名：
       平台的表单是"一题一个 form 区块"，同名键靠**提交顺序**配对。
       单题作业看不出问题，多题作业必须严格按题目顺序依次 append。
       所以这里返回的是 (key, value) **列表**语义 —— 用 dict 会互相覆盖。
       为兼容旧调用方（`submit_dohw` 用 requests 的 data=dict），
       这里返回 dict；但**多题时用 MultiValueDict 保序**见下方 `dohw_form_pairs`。

    answers: {qid: 答案}
      - single/multiple: "A" / "ABC"
      - judgement: "true" / "false"
      - completion: ["空1","空2"] 或 "空1|空2"
    """
    form: Dict[str, Any] = dict(parsed.get("form") or {})

    # 逐题构造。注意 questionId/index 每道题都要出现一次、值不同，
    # dict 语义做不到 → 这里改由 `dohw_form_pairs` 输出有序列表，
    # 本函数保留 dict 形态供**单题**场景与旧调用方使用。
    pairs: List[tuple] = []

    for i, q in enumerate(questions):
        qid = q["id"]
        q_type = q["type"]
        ans = answers.get(qid)

        pairs.append(("type" + str(qid), q.get("type_code", 0)))
        # 🔴 真机字段：score{qid}（分值）—— 旧版完全缺失
        sc = q.get("score")
        if sc in (None, ""):
            sc = q.get("full_score") or parsed.get(f"score{qid}") or 0
        try:
            sc = float(sc)
        except (TypeError, ValueError):
            sc = 0
        pairs.append(("score" + str(qid), sc))

        if q_type in ("single", "multiple"):
            pairs.append(("answer" + str(qid), "" if ans is None else str(ans)))
        elif q_type == "judgement":
            if ans is None:
                pairs.append(("answer" + str(qid), ""))
            else:
                s = str(ans).strip().lower()
                pairs.append(("answer" + str(qid),
                              "true" if s in (
                                  "true", "t", "1", "对", "正确", "是",
                                  "√", "yes", "y") else "false"))
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
            # 🔴 真机字段：blankNum{qid} = "1,"（空数 + 逗号结尾）
            pairs.append(("blankNum" + str(qid),
                          ",".join(str(k + 1) for k in range(len(parts))) + ","))
            for k, p in enumerate(parts):
                # 真机：answer{qid}{空序号}，空序号从 1 开始（textarea）
                pairs.append(("answer" + str(qid) + str(k + 1), p))
        else:
            # 简答等：直接落 answer{qid}
            pairs.append(("answer" + str(qid), "" if ans is None else str(ans)))

        # 🔴🔴 每题一组「无后缀」的 questionId / index —— 真机如此，必须逐题追加
        pairs.append(("questionId", str(qid)))
        pairs.append(("index", str(i)))

    # 单题场景（本次 2 份作业都是单题）直接给 dict；
    # 多题时调用方应改用 dohw_form_pairs 保序（submit_dohw 内部已切换）。
    for k, v in pairs:
        form[k] = v

    form["_pairs"] = pairs      # 供 submit_dohw 取有序对
    return form


def dohw_form_pairs(parsed: Dict[str, Any],
                    questions: List[Dict[str, Any]],
                    answers: Dict[Any, Any],
                    work_answer_id: Any = None) -> List[tuple]:
    """返回**有序**表单键值对列表（多题作业必须用这个，不能退化成 dict）。

    为什么必须保序：每道题都带 `questionId` / `index` 两个**同名**字段，
    平台靠提交顺序把它们与题目一一配对（第 n 组 questionId/index → 第 n 题）。
    用 dict 会因键名相同而互相覆盖 → 只剩最后一题，
    其余题目在平台侧变成「无 questionId」→ 答案落不上去。

    ⚠️ requests 的 `data=list[tuple]` 天然支持重复键，故直接返回列表即可。

    🔴 2026-09-26 20:05：新增 `work_answer_id`。
    翻页协议要求每暂存一次就要用平台回传的**新 answerId**（真机
    `getTheNextQuestion` 里 `$("#workRelationAnswerId").val(data)`）。
    传 None 则用解析页里的原值。
    """
    p = dict(parsed.get("form") or {})
    # 🔴 用最新作答记录 ID 覆盖（翻页时必须）
    if work_answer_id not in (None, ""):
        p["workRelationAnswerId"] = str(work_answer_id)
    pairs: List[tuple] = []

    # ① 公共隐藏字段（来自解析页）
    # 🔴 2026-09-26 19:23 修：**空值也要带上**。
    #    真机上 workExamUploadUrl / workExamUploadCrcUrl / vx / workTimesEnc
    #    都以空串存在，平台可能靠"字段存在性"做校验（缺字段 vs 空字段不同）。
    #    旧写法 `if p[k] not in (None,"")` 会把它们丢掉 → 与真机不一致。
    #    现在：只要解析页里有这个键，就原样回传（含空串）。
    _COMMON = ("workExamUploadUrl", "workExamUploadCrcUrl",
               "workRelationAnswerId", "knowledgeid", "enc", "source",
               "encWork", "vx", "courseId", "workRelationId", "classId",
               "workTimesEnc", "oldWorkId", "mooc", "isphone")
    for k in _COMMON:
        if k in p:
            pairs.append((k, "" if p[k] is None else p[k]))

    # ② 逐题字段
    for i, q in enumerate(questions):
        qid = str(q["id"])
        q_type = q["type"]
        ans = answers.get(qid) if qid in answers else answers.get(q["id"])

        pairs.append(("type" + qid, q.get("type_code", 0)))

        sc = q.get("score")
        if sc in (None, ""):
            sc = q.get("full_score") or 0
        try:
            sc = float(sc)
        except (TypeError, ValueError):
            sc = 0
        pairs.append(("score" + qid, sc))

        # 🔴🔴🔴 2026-09-26 22:45 **题型码 → 提交字段名（真机 100 题逐页核对）**
        # ─────────────────────────────────────────────────────────────
        #  type  题型      提交字段名               旧代码          结果
        #   0    单选      answer{qid}             answer{qid}    ✅
        #   1    多选      **answers{qid}**（复数） answer{qid}    ❌ 20 题全丢
        #   2    填空      answer{qid}1 / {qid}2   answer{qid}N   ✅
        #   3    判断      answer{qid}             answer{qid}    ✅
        #   4    简答      answer{qid}             answer{qid}    ✅
        #
        # 真机证据（index=20 / qid=405811919）：
        #   <input name="type405811919" value="1"/>                  ← 题型码 1=多选
        #   <input name="answers405811919" value="" class="answerInput"/>  ← **复数！**
        # 旧代码对多选也写 `answer{qid}` → 平台找不到该字段 → 多选 20 题
        # 全部落空（回读为空、按 0 分计）。这是"50/100 题无回显"的主因。
        # ─────────────────────────────────────────────────────────────
        if q_type in ("single", "multiple"):
            field = ("answers" if q_type == "multiple" else "answer") + qid
            pairs.append((field, "" if ans is None else str(ans)))
        elif q_type == "judgement":
            if ans is None:
                pairs.append(("answer" + qid, ""))
            else:
                s = str(ans).strip().lower()
                pairs.append(("answer" + qid,
                              "true" if s in (
                                  "true", "t", "1", "对", "正确", "是",
                                  "√", "yes", "y") else "false"))
        elif q_type == "completion":
            # 🔴🔴 2026-09-26 23:00 **填空题「单空 vs 多空」的拆分必须看 blankCount**
            # 真机踩坑：某填空答案是 "9,596,961"（数字千分位），旧代码按 `,` 拆
            #   → ["9","596","961"] → 只写第一个空 "9" → 平台回读「9」≠「9,596,961」
            #   → 回读不符 → 整个作业被闸门拦下（63/64 差 1 题）。
            # 规则：
            #   blankCount<=1 → **整串就是一个空，绝不拆**（逗号是内容的一部分）
            #   blankCount>1  → 才按明确分隔符拆（| ｜ 换行 中文逗号，不拆英文逗号）
            _bn = int(q.get("blankCount") or 0) or 0
            if isinstance(ans, list):
                parts = [str(x).strip() for x in ans]
            elif ans is None:
                parts = [""] * max(1, _bn or 1)
            elif _bn <= 1:
                # 单空：整串原样写一个空（保护 "9,596,961" 这种带逗号的内容）
                parts = [str(ans).strip()]
            else:
                parts = [x.strip() for x in
                         re.split(r"\s*[|｜\n]\s*|\s*[，]\s*", str(ans)) if x.strip()]
            n = _bn or len(parts)
            if len(parts) < n:
                parts += [""] * (n - len(parts))
            elif len(parts) > n:
                parts = parts[:n]
            pairs.append(("blankNum" + qid,
                          ",".join(str(k + 1) for k in range(len(parts))) + ","))
            for k, x in enumerate(parts):
                pairs.append(("answer" + qid + str(k + 1), x))
        else:
            pairs.append(("answer" + qid, "" if ans is None else str(ans)))

        # 🔴 每题一组无后缀 questionId/index（真机如此；必须逐题追加，不可用 dict）
        pairs.append(("questionId", qid))
        pairs.append(("index", str(i)))

    return pairs


def submit_dohw(session, course: Dict[str, Any], parsed: Dict[str, Any],
                questions: List[Dict[str, Any]], answers: Dict[Any, Any],
                save_only: bool = False, work_answer_id: Any = None) -> Dict[str, Any]:
    """提交（或暂存）新版作业。

    save_only=True → tempSave=true（暂存草稿，不消耗作答次数）

    🔴 2026-09-26 19:20 改：改用 **有序键值对**（`dohw_form_pairs`）。
    requests 的 data 传 list[tuple] 支持重复键，这正是平台多题作业需要的 ——
    每道题都带同名 `questionId` / `index`，用 dict 会互相覆盖，只剩最后一题。

    🔴🔴 2026-09-26 20:05 补充（翻页协议来自真机 `submitFormNew`）：
        暂存与正式提交**是同一个接口**，只差 `tempSave`：
            POST doNormalHomeWorkSubmit?tempSave=true   → 暂存（翻页前必做）
            POST doNormalHomeWorkSubmit?tempSave=false  → 正式交卷
        返回值里的 **`answerId` = 新的 workRelationAnswerId**，
        翻下一页时必须带上它（真机 `getTheNextQuestion` 就是这么干的）。
        所以本函数把 `answerId` 一并返回，供翻页循环使用。
    """
    data = dohw_form_pairs(parsed, questions, answers,
                           work_answer_id=work_answer_id)
    params = {"tempSave": "true" if save_only else "false"}
    resp = session.post(API_WORK_SUBMIT2, params=params, data=data, timeout=30)
    resp.raise_for_status()
    try:
        j = resp.json()
    except Exception:
        return {"status": False, "msg": f"非 JSON 响应：{resp.text[:200]}"}
    if isinstance(j, dict):
        j.setdefault("answerId", "")
    return j


def fetch_all_works_once(session) -> Dict[str, List[Dict[str, Any]]]:
    """**一次请求**拿回全部课程的作业，按 courseId 分组。

    2026-09-26 新增：``stu-work`` 列表页本身一次就返回所有课程的作业，
    原先"每门课各请求一次"纯属浪费 —— 58 门课 = 58 次请求 ≈ 35 秒，
    且每次都要过限流器。这里改成只请求一次，把耗时压到 1~2 秒。

    Returns:
        {courseId(str): [work, ...]}；请求失败返回 {}
    """
    try:
        resp = session.get(PAGE_WORK_LIST, timeout=25)
    except Exception as e:
        logger.warning(f"作业列表全量请求失败：{e}")
        return {}
    if resp.status_code != 200:
        logger.warning(f"作业列表全量请求 HTTP {resp.status_code}")
        return {}

    works = _parse_stu_work_list(resp.text)   # 不传 course_id → 全都要
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for w in works:
        cid = str(w.get("courseid") or "")
        if not cid:
            continue
        grouped.setdefault(cid, []).append(w)
    logger.info(f"作业全量扫描：{len(works)} 份，分属 {len(grouped)} 门课程")
    return grouped


def fetch_all_works_detail(session) -> tuple:
    """同 fetch_all_works_once，但**同时**给出 courseId → 课程名 映射。

    🔴 2026-09-26 新增：stu-work 的 courseId 与 get_course_list 的 courseId
    是**两套 ID 体系**（班次 ID vs 模板 ID，交集为 0），无法直接匹配。
    唯一可靠的桥梁是课程名，所以这里把每条作业自带的课程名一并返回。

    Returns:
        (grouped, id2name)
          grouped : {班次courseId: [work, ...]}
          id2name : {班次courseId: "完整课程名"}
    """
    try:
        resp = session.get(PAGE_WORK_LIST, timeout=25)
    except Exception as e:
        logger.warning(f"作业列表全量请求失败：{e}")
        return {}, {}
    if resp.status_code != 200:
        logger.warning(f"作业列表全量请求 HTTP {resp.status_code}")
        return {}, {}

    works = _parse_stu_work_list(resp.text)
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    id2name: Dict[str, str] = {}
    for w in works:
        cid = str(w.get("courseid") or "")
        if not cid:
            continue
        grouped.setdefault(cid, []).append(w)
        nm = w.get("course_full") or w.get("course_name") or ""
        if nm and cid not in id2name:
            id2name[cid] = nm
    logger.info(f"作业全量扫描：{len(works)} 份，分属 {len(grouped)} 门课程")
    return grouped, id2name


def fetch_redo_info(session, course: Dict[str, Any],
                    work: Dict[str, Any]) -> Dict[str, Any]:
    """拉单份作业的重做信息（只读，**绝不触发重做**）。

    重做次数与取分规则只存在于详情页 `mtaskmsgspecial` 的 JS 里，
    列表页 `stu-work` 拿不到，所以必须逐份取详情。

    Returns:
        {"ok":bool, "redotimes":n, "redo_total":n, "redo_used":n,
         "score":"", "score_rule":"最高成绩"/"本次成绩", "msg": ...}
    """
    try:
        d = fetch_work_detail(session, course, work)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "redotimes": 0, "score_rule": ""}
    return {
        "ok": True,
        "redotimes": d.get("redotimes", 0),
        "redo_total": d.get("redo_total", 0),
        "redo_used": d.get("redo_used", 0),
        "score": d.get("score", ""),
        "score_rule": d.get("score_rule", ""),
        "workid": d.get("workid"),
        "workanswerid": d.get("workanswerid"),
    }


def fetch_redo_info_batch(session, courses_by_id: Dict[str, Dict[str, Any]],
                          works: List[Dict[str, Any]],
                          max_workers: int = 6) -> Dict[str, Dict[str, Any]]:
    """并发拉取多份作业的重做信息，按 taskrefId 索引返回。

    ⚠️ 只对**已完成/已批阅**的作业有意义（未作答的没有重做一说），
    调用方应自己过滤。并发度别开太高，超星有风控。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(w):
        # 🔴 2026-09-26：作业页的 courseId（班次 ID）与课程列表的 courseId
        # （模板 ID）是两套体系，查不到是**正常**的，不能因此判失败。
        # 详情页走 work["href"] 即可，course 只是兜底参数，找不到就传空 dict。
        c = courses_by_id.get(str(w.get("courseid") or "")) or {}
        return (str(w.get("taskrefid") or w.get("workid")),
                fetch_redo_info(session, c, w))

    out: Dict[str, Dict[str, Any]] = {}
    if not works:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as ex:
        futs = [ex.submit(_one, w) for w in works]
        for f in as_completed(futs):
            try:
                key, val = f.result()
                if key and key != "None":
                    out[key] = val
            except Exception as e:
                logger.warning(f"重做信息拉取失败：{e}")
    return out
