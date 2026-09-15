# -*- coding: utf-8 -*-
"""实战验证：在真实平台上跑章节测验，确认提交格式被平台接受。

目标：指定账号《人工智能伦理与安全》（用于测试的课程）
重点验证：
  1. 判断题 -> answer{id} = "true"/"false"
  2. 填空题 -> tiankongsize{id} = 空数 + answer{id}1/answer{id}2 … 逐空
  3. 多空填空题能否被平台正确接收（这是最可能出问题的地方）

用法：
    python live_test_quiz.py --chapter 第一章 --dry-run   # 只抓题作答，不提交
    python live_test_quiz.py --chapter 第一章            # 真实提交并看得分
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api.logger import logger
from api.base import Chaoxing, Account, SessionManager, StudyResult
from api.answer import Tiku
from api.learned import normalize_title
from survey_question_types import _load_account

COURSE_ID = "265783377"      # 人工智能伦理与安全
# 账号不写死在源码里（避免随仓库外泄）。来源优先级：
#   --account 命令行参数 > 环境变量 CK_ACCOUNT > 空（必填）
DEFAULT_ACCOUNT = os.environ.get("CK_ACCOUNT", "")


def build_ai_tiku(data: dict, submit: bool):
    """纯 AI 题库（不依赖本地 tikuAdapter，避免它没启动导致干扰）。

    以 config.ini 的 [tiku] 段为底（含 true_list / false_list 等必需项），
    再覆盖成"纯 AI"配置。
    """
    conf = {}
    try:
        from main import load_config_from_file
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "config.ini")
        _, tiku_conf, _ = load_config_from_file(cfg_path)
        conf = dict(tiku_conf)
    except Exception as e:
        print(f"[警告] 读取 config.ini 失败（{e}），使用内置默认值")

    conf.update({
        "provider": "AI",
        "endpoint": str(data.get("ai_endpoint") or "").strip(),
        "key": str(data.get("ai_key") or "").strip(),
        "model": str(data.get("ai_model") or "").strip(),
        "http_proxy": conf.get("http_proxy", ""),
        "min_interval_seconds": conf.get("min_interval_seconds", "2"),
        "submit": "true" if submit else "false",
        "cover_rate": "0.5",
        "delay": conf.get("delay", "0"),
        "check_llm_connection": "false",
    })
    if not (conf["endpoint"] and conf["key"] and conf["model"]):
        raise SystemExit("AI 配置缺失（endpoint/key/model），无法作答")
    t = Tiku.get_tiku_from_config(conf)
    t.init_tiku()
    return t


def login(phone: str):
    data = _load_account(phone)
    tiku = build_ai_tiku(data, submit=True)
    chao = Chaoxing(account=Account(phone, str(data.get("password") or "")),
                    tiku=tiku, query_delay=1.0, work_max_retries=1)
    res = chao.login()
    if not res.get("status"):
        raise SystemExit(f"登录失败：{res.get('msg')}")

    # 账号核对（防止登错号）
    _s = SessionManager.get_session()
    info = _s.get("https://sso.chaoxing.com/apis/login/userLogin4Uname.do",
                  timeout=15).json()
    m = info.get("msg") or {}
    print(f"[账号核对] {m.get('name')} / {m.get('phone')} / {m.get('schoolname')}")
    if str(m.get("phone") or "") != phone:
        raise SystemExit(f"❌ 登录账号不匹配：期望 {phone}，实际 {m.get('phone')}")
    return chao, data


def find_course(chao, course_id):
    for c in chao.get_course_list():
        if str(c.get("courseId")) == course_id:
            return c
    raise SystemExit(f"课程列表里找不到 courseId={course_id}")


def find_work_job(chao, course, chapter_key):
    """在指定章节里找章节测验任务点。"""
    points = chao.get_course_point(course["courseId"], course["clazzId"],
                                   course["cpi"])
    for point in (points or {}).get("points") or []:
        title = str(point.get("title") or "")
        if chapter_key not in title:
            continue
        jobs, job_info = chao.get_job_list(course, point)
        for job in jobs:
            if job.get("type") == "workid":
                print(f"[定位] 章节《{title}》-> 测验 jobid={job.get('jobid')}")
                return point, job, job_info
    return None, None, None


def main():
    ap = argparse.ArgumentParser(description="章节测验实战验证")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT,
                    help="学习通账号（手机号）；也可用环境变量 CK_ACCOUNT")
    ap.add_argument("--course", default=COURSE_ID, help="courseId")
    ap.add_argument("--chapter", default="第一章", help="章节标题关键字")
    ap.add_argument("--dry-run", action="store_true",
                    help="只抓题与作答，不提交（完全不产生写操作）")
    args = ap.parse_args()
    if not args.account:
        raise SystemExit("缺少账号：请用 --account 指定，或设置环境变量 CK_ACCOUNT")

    chao, data = login(args.account)
    course = find_course(chao, args.course)
    print(f"[课程] {course.get('title')}")

    point, job, job_info = find_work_job(chao, course, args.chapter)
    if not job:
        raise SystemExit(f"没找到包含『{args.chapter}』的章节测验")

    if args.dry_run:
        # 只抓题：直接调 decode，不进入 study_work 的提交分支
        _s = SessionManager.get_session()
        resp = _s.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
            "api": "1", "workId": job["jobid"].replace("work-", ""),
            "jobid": job["jobid"], "originJobId": job["jobid"],
            "needRedirect": "true", "skipHeader": "true",
            "knowledgeid": str(job_info.get("knowledgeid", "")),
            "ktoken": job_info.get("ktoken", ""), "cpi": job_info.get("cpi", ""),
            "ut": "s", "clazzId": course["clazzId"], "type": "",
            "enc": job.get("enc", ""), "mooc2": "1",
            "courseid": course["courseId"],
        }, timeout=25)
        from api.decode import decode_questions_info
        qs = decode_questions_info(resp.text)["questions"]
        print(f"\n[dry-run] 抓到 {len(qs)} 题，逐题查看解析结果：")
        for q in qs:
            print(f"  · [{q['type']}] id={q['id']} blankCount={q.get('blankCount')} "
                  f"is_group={q.get('is_group')}")
            print(f"      {normalize_title(q['title'])[:80]}")
            if q["type"] == "completion":
                qid = q["id"]
                # 逐空字段两种前缀都可能：PC 版 answerEditor{qid}N / 手机版 answer{qid}N
                fields = [k for k in q["answerField"]
                          if k != f"answer{qid}"
                          and not k.startswith("answertype")
                          and (k.startswith(f"answerEditor{qid}")
                               or k.startswith(f"answer{qid}"))]
                print(f"      填空字段({len(fields)}): {fields}")
        print("\n[dry-run] 未提交，无任何写操作。")
        return

    print(f"\n[开始作答] {point.get('title')}")
    t0 = time.time()
    result = chao.study_work(course, job, job_info)
    print(f"\n[结果] {result}   耗时 {time.time() - t0:.0f}s")
    print("请到学习通查看该测验的得分与『我的答案』。")


if __name__ == "__main__":
    main()
