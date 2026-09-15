# -*- coding: utf-8 -*-
"""题型普查工具（2026-09-13 新增）。

用途
----
1. **报名新课程后先跑一次**：只读题目、统计题型分布，**不作答、不提交**，
   对账号零风险。
2. **收集未知题型码**：超星官方支持 18 种题型，本程序目前只映射了
   0-4 与 19。跑这个能捞出真实的题型码，再补进 `api/decode.py` 的
   `type_map`，就能为每种题型定制提交格式。

用法
----
    # 先登录（复用 ui_config.json 里的账号档案 + 已保存的 cookies）
    python survey_question_types.py --account <你的学习通手机号>

    # 只看某几门课（courseId 逗号分隔）
    python survey_question_types.py --account <你的学习通手机号> --course 212758088,204634761

    # 不指定 --course 则扫描该账号全部课程

输出
----
    survey_<账号>_<时间>.txt   人可读报告
    survey_<账号>_<时间>.json  机器可读（含每道题的题型码与题干摘要）
"""
import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.logger import logger
from api.base import Chaoxing, SessionManager
from api.answer import Tiku
from api.decode import decode_questions_info, _get_question_type
from api.homework import scan_works

# 已知题型码 → 名称（与 api/decode.py::_get_question_type 保持一致）
KNOWN_TYPES = {
    "0": "单选题", "1": "多选题", "2": "填空题", "3": "判断题",
    "4": "简答题", "5": "名词解释", "6": "论述题", "7": "计算题",
    "8": "其它题", "9": "分录题", "10": "资料题",
    "14": "完型填空", "19": "听力题",
    # 以下按答案形态归为 unknown，这里仍显示真实题型名便于判断
    "11": "连线题", "13": "排序题", "15": "阅读理解",
    "18": "口语题", "20": "共用选项", "21": "测评题",
}

# 课程考试列表（**只读**：只列清单，不进入考试、不拉考卷）
# 接口来源：CxKitty cxapi/classes.py::PAGE_EXAM_LIST
API_EXAM_LIST = "https://mooc1-api.chaoxing.com/exam/phone/task-list"


def scan_exams(session, course: Dict[str, Any]) -> List[Dict[str, Any]]:
    """扫描某门课程的考试列表。

    ⚠ 刻意**只调列表接口**（纯只读）。真正开始考试的是
    `exam-ans/exam/phone/start`，调用它会消耗考试次数、可能触发监考 —— 
    绝不在普查阶段调用。因此考试内部题型需另行确认。
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urlparse, parse_qs
    out = []
    try:
        resp = session.get(API_EXAM_LIST, params={
            "courseId": course.get("courseId"),
            "classId": course.get("clazzId"),
            "cpi": course.get("cpi", ""),
        }, timeout=20)
    except Exception as e:
        logger.warning(f"考试列表请求失败（{course.get('title')}）：{e}")
        return out
    if resp.status_code != 200:
        return out
    html = BeautifulSoup(resp.text, "html.parser")
    ul = html.find("ul", {"class": "nav"})
    if not ul:
        return out
    for li in ul.find_all("li"):
        q = parse_qs(urlparse(li.get("data") or "").query)
        p = li.find("p")
        span = li.find("span")
        fr = li.find("span", {"class": "fr"})
        out.append({
            "exam_id": (q.get("taskrefId") or [""])[0],
            "enc_task": (q.get("enc_task") or [""])[0],
            "name": p.get_text(strip=True) if p else "",
            "status": span.get_text(strip=True) if span else "",
            "expire": fr.get_text(strip=True) if fr else "",
        })
    return out


def _load_account(phone: str) -> dict:
    """从 ui_config.json 里取账号档案 + 题库配置。

    ⚠ 坑（2026-09-13 实测踩到）：last_params 存的是"最后一次登录的账号"，
    里面同样有 username/password。早前用 `merged.update(base)` 合并，
    会把目标账号的 username/password **覆盖成别的号** —— 结果"用 193 的档案
    登了 195 的号"，列出来的课程全是另一个人的，而且看起来一切正常。
    所以：账号字段一律以 profile 为准，last_params 只用来补题库配置。
    """
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    base = dict(cfg.get("last_params") or {})
    ACCOUNT_KEYS = ("username", "password")
    for p in cfg.get("profiles", []):
        if str(p.get("username")) == str(phone):
            merged = dict(p)
            for k, v in base.items():
                if k in ACCOUNT_KEYS:
                    continue          # 绝不覆盖账号字段
                if v not in (None, ""):
                    merged.setdefault(k, v)   # 只补缺，不覆盖
            return merged
    raise SystemExit(f"ui_config.json 里找不到账号 {phone}")


def _build_tiku(data: dict):
    """构造题库实例（只为拿 session 用，不作答）。"""
    conf = {"provider": "", "submit": "false"}
    try:
        tiku = Tiku.get_tiku_from_config(conf)
        tiku.init_tiku()
        return tiku
    except Exception as e:
        logger.warning(f"题库构造失败（不影响普查）：{e}")
        tiku = Tiku.get_tiku_from_config({"provider": ""})
        return tiku


def _fetch_work_questions(chao, course, job, job_info):
    """抓取章节测验的题目（只读，不提交）。"""
    _session = SessionManager.get_session()
    try:
        resp = _session.get(
            "https://mooc1.chaoxing.com/mooc-ans/api/work",
            params={
                "api": "1",
                "workId": job["jobid"].replace("work-", ""),
                "jobid": job["jobid"],
                "originJobId": job["jobid"],
                "needRedirect": "true",
                "skipHeader": "true",
                "knowledgeid": str(job_info.get("knowledgeid", "")),
                "ktoken": job_info.get("ktoken", ""),
                "cpi": job_info.get("cpi", ""),
                "ut": "s",
                "clazzId": course["clazzId"],
                "type": "",
                "enc": job.get("enc", ""),
                "mooc2": "1",
                "courseid": course["courseId"],
            }, timeout=25)
    except Exception as e:
        logger.warning(f"抓题失败（{job.get('jobid')}）：{e}")
        return []
    if "教师未创建完成该测验" in resp.text:
        return []
    try:
        info = decode_questions_info(resp.text)
    except Exception as e:
        logger.warning(f"解析失败（{job.get('jobid')}）：{e}")
        return []
    return info.get("questions") or []


def survey(chao, courses, out_prefix):
    """扫描课程，统计题型分布 + 作业/考试清单。"""
    # (type_name, type_code) -> 计数
    counter = Counter()
    # type_code -> 题干样例（供补映射用）
    samples = defaultdict(list)
    # 未映射题型码（_get_question_type 返回 unknown）单独统计
    unknown_codes = Counter()
    per_course = {}
    details = []
    hw_info = {}      # 课程 -> 作业清单
    exam_info = {}    # 课程 -> 考试清单

    for c in courses:
        cid = str(c.get("courseId", ""))
        print(f"\n===== 《{c.get('title')}》 ({cid}) =====")
        course_stat = Counter()
        try:
            points = chao.get_course_point(cid, c.get("clazzId"), c.get("cpi"))
        except Exception as e:
            print(f"  读取章节失败：{e}")
            points = None
        pts = (points or {}).get("points") or []
        print(f"  章节数：{len(pts)}")
        for point in pts:
            try:
                jobs, job_info = chao.get_job_list(c, point)
            except Exception as e:
                print(f"  [跳过] 章节《{point.get('title')}》任务点读取失败：{e}")
                continue
            for job in jobs:
                if job.get("type") != "workid":
                    continue
                qs = _fetch_work_questions(chao, c, job, job_info)
                print(f"    · 《{point.get('title')}》测验：{len(qs)} 题")
                for q in qs:
                    code = str(q.get("answerField", {})
                               .get(f'answertype{q.get("id")}', "") or "")
                    tname = KNOWN_TYPES.get(code, f"未知({code})")
                    internal = _get_question_type(code)
                    counter[(tname, code)] += 1
                    course_stat[(tname, code)] += 1
                    if internal == "unknown":
                        unknown_codes[code] += 1
                        if len(samples[code]) < 5:
                            samples[code].append(str(q.get("title", ""))[:120])
                    details.append({
                        "course": c.get("title"), "courseId": cid,
                        "chapter": point.get("title"),
                        "qid": q.get("id"), "type_code": code,
                        "type_name": tname, "internal_type": internal,
                        "title": str(q.get("title", ""))[:200],
                    })
            time.sleep(0.3)

        # ---- 作业清单（只读列表，不抓题不提交）----
        try:
            works = scan_works(SessionManager.get_session(), c)
        except Exception as e:
            print(f"  作业列表读取失败：{e}")
            works = []
        works = [w for w in works
                 if (w.get("status") or "") not in ("已完成", "已批阅", "待批阅")]
        if works:
            print(f"  作业：{len(works)} 份待完成")
            for w in works:
                print(f"    · {w.get('name')} [{w.get('status') or '?'}]")
        hw_info[c.get("title")] = [
            {"name": w.get("name"), "status": w.get("status"),
             "workid": w.get("workid")} for w in works]

        # ---- 考试清单（**纯只读**，只列不进入）----
        try:
            exams = scan_exams(SessionManager.get_session(), c)
        except Exception as e:
            print(f"  考试列表读取失败：{e}")
            exams = []
        if exams:
            print(f"  考试：{len(exams)} 场")
            for e_ in exams:
                print(f"    · {e_.get('name')} [{e_.get('status') or '?'}]"
                      f"{' 截止 ' + e_['expire'] if e_.get('expire') else ''}")
        exam_info[c.get("title")] = exams

        per_course[c.get("title")] = dict(
            (f"{n}({code})", v) for (n, code), v in course_stat.items())
        time.sleep(0.3)

    # ---- 输出 ----
    lines = []
    lines.append("=" * 66)
    lines.append("题型普查报告")
    lines.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 66)

    lines.append("\n【总览】题型分布")
    if counter:
        for (tname, code), n in counter.most_common():
            internal = _get_question_type(code)
            mark = "" if internal != "unknown" else "   ← 兜底处理（unknown）"
            lines.append(f"  {tname:<10} 代码 {code:<4} 共 {n} 题{mark}")
    else:
        lines.append("  （没抓到题目 —— 可能课程无章节测验、章节未开放，或登录态失效）")

    if unknown_codes:
        lines.append("\n【重点】走 unknown 兜底的题型码（答案不会被拦，但格式未定制）")
        for code, n in unknown_codes.most_common():
            lines.append(f"  代码 {code}（{KNOWN_TYPES.get(code, '?')}）：{n} 题")
            for s_ in samples.get(code, []):
                lines.append(f"     样例：{s_}")

    lines.append("\n【分课程 · 章节测验】")
    for title, stat in per_course.items():
        lines.append(f"  《{title}》")
        if stat:
            for k, v in sorted(stat.items(), key=lambda x: -x[1]):
                lines.append(f"      {k}: {v}")
        else:
            lines.append("      （无章节测验）")

    lines.append("\n【分课程 · 作业】")
    any_hw = False
    for title, ws in hw_info.items():
        if not ws:
            continue
        any_hw = True
        lines.append(f"  《{title}》：{len(ws)} 份待完成")
        for w in ws:
            lines.append(f"      · {w['name']} [{w['status'] or '?'}]")
    if not any_hw:
        lines.append("  （都没有待完成作业 —— 课程广场报名的课通常只有视频+测验，"
                     "作业一般由任课老师在本班发布）")

    lines.append("\n【分课程 · 考试】（只列清单，未进入考试）")
    any_exam = False
    for title, es in exam_info.items():
        if not es:
            continue
        any_exam = True
        lines.append(f"  《{title}》：{len(es)} 场")
        for e_ in es:
            expire = f"  截止 {e_['expire']}" if e_.get("expire") else ""
            lines.append(f"      · {e_['name']} [{e_['status'] or '?'}]{expire}"
                         f"  (exam_id={e_['exam_id']})")
    if not any_exam:
        lines.append("  （没有发现考试）")

    report = "\n".join(lines)
    print("\n" + report)

    ts = time.strftime("%Y%m%d_%H%M%S")
    txt_path = f"{out_prefix}_{ts}.txt"
    json_path = f"{out_prefix}_{ts}.json"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(report)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": {f"{n}({c})": v for (n, c), v in counter.items()},
            "unknown_codes": dict(unknown_codes),
            "homework": hw_info,
            "exams": exam_info,
            "questions": details,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n报告已保存：\n  {txt_path}\n  {json_path}")
    return txt_path, json_path


def main():
    ap = argparse.ArgumentParser(description="题型普查（只读，不作答不提交）")
    ap.add_argument("--account", required=True, help="ui_config.json 里的手机号")
    ap.add_argument("--course", default="", help="courseId，逗号分隔；留空=全部课程")
    ap.add_argument("--out", default="survey", help="输出文件名前缀")
    ap.add_argument("--cookies", action="store_true",
                    help="用已保存的 cookies 登录（默认用账号密码，避免登错号）")
    ap.add_argument("--list-only", action="store_true",
                    help="只登录并列出课程，不做普查（用于快速验证登录态）")
    args = ap.parse_args()

    data = _load_account(args.account)
    tiku = _build_tiku(data)

    from api.base import Account
    uname = str(data.get("username") or "").strip()
    pwd = str(data.get("password") or "").strip()

    if args.cookies:
        # ⚠ cookies.txt 是**全局**的，存的是"最后一次登录的账号"（例如刚刷完
        # 的另一个号）。用它配 --account 会静默登成别的账号，扫出来的课程全错
        # 却看起来"登录成功"。所以只在显式加 --cookies 时才走这条路。
        chao = Chaoxing(tiku=tiku, query_delay=0.5)
        res = chao.login(login_with_cookies=True)
        print(f"[登录] 使用 cookies（可能是任意账号）：{uname} 仅为档案名")
    else:
        if not (uname and pwd):
            raise SystemExit(
                f"账号 {uname} 在 ui_config.json 里没有保存密码；\n"
                f"  请改用 --cookies（但注意会登成 last-login 账号），或先在网页版登录一次。")
        print(f"[登录] 使用账号密码：{uname}")
        chao = Chaoxing(account=Account(uname, pwd), tiku=tiku, query_delay=0.5)
        res = chao.login()

    if not res.get("status"):
        raise SystemExit(f"登录失败：{res.get('msg')}")

    # 核对实际登录的账号：防止"用 A 的档案登成 B 的号"而毫无察觉。
    # sso 接口返回真实姓名/手机号，是唯一可靠的核对手段。
    try:
        _s = SessionManager.get_session()
        _r = _s.get("https://sso.chaoxing.com/apis/login/userLogin4Uname.do",
                    timeout=15)
        _info = _r.json()
        if _info.get("result") == 1:
            _m = _info.get("msg") or {}
            print(f"[账号核对] 姓名={_m.get('name')}  手机={_m.get('phone')}  "
                  f"学校={_m.get('schoolname')}  uid={_m.get('puid')}")
            _expect = uname
            _got_phone = str(_m.get("phone") or "")
            if _got_phone and _expect and _got_phone != _expect:
                raise SystemExit(
                    f"❌ 账号不匹配！请求的是 {_expect}，实际登录的是 {_got_phone}。\n"
                    f"   已中止，避免扫到别人的课程。请检查 ui_config.json 的账号档案。")
        else:
            print("[账号核对] 无法获取账号信息，请自行确认")
    except SystemExit:
        raise
    except Exception as e:
        print(f"[账号核对] 跳过（{type(e).__name__}: {e}）")

    courses = chao.get_course_list()
    if args.course:
        wanted = {x.strip() for x in args.course.split(",") if x.strip()}
        courses = [c for c in courses if str(c.get("courseId")) in wanted]

    print(f"共 {len(courses)} 门课程待普查")
    for c in courses:
        print(f"  · {c.get('title')}  (courseId={c.get('courseId')})")
    if args.list_only:
        print("\n[--list-only] 仅列出课程，未做普查。")
        return
    survey(chao, courses, args.out)


if __name__ == "__main__":
    main()
