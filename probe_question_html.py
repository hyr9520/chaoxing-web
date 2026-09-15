# -*- coding: utf-8 -*-
"""探测工具：dump 真实题目的 HTML 结构，用于校正解析器。

为什么需要：填空题的 DOM 结构在不同页面（PC 章节测验 / 手机端作业）不一样。
此前按 CxKitty 的作业页结构（ul.blankList2 > li）写解析，实测 PC 版章节测验
的填空题 blankCount 解析为 0、字段为空 —— 必须看真实 HTML 才能改对。

用法：
    python probe_question_html.py --chapter 第一章            # 看全部题型的 class 概览
    python probe_question_html.py --chapter 第一章 --qid 405915536   # dump 单题完整 HTML
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bs4 import BeautifulSoup

from api.base import SessionManager
from live_test_quiz import login, find_course, find_work_job, COURSE_ID


def fetch_html(chao, course, job, job_info):
    s = SessionManager.get_session()
    resp = s.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
        "api": "1", "workId": job["jobid"].replace("work-", ""),
        "jobid": job["jobid"], "originJobId": job["jobid"],
        "needRedirect": "true", "skipHeader": "true",
        "knowledgeid": str(job_info.get("knowledgeid", "")),
        "ktoken": job_info.get("ktoken", ""), "cpi": job_info.get("cpi", ""),
        "ut": "s", "clazzId": course["clazzId"], "type": "",
        "enc": job.get("enc", ""), "mooc2": "1",
        "courseid": course["courseId"],
    }, timeout=25)
    return resp.text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=os.environ.get("CK_ACCOUNT", ""),
                    help="学习通账号（手机号）；也可用环境变量 CK_ACCOUNT")
    ap.add_argument("--chapter", default="第一章")
    ap.add_argument("--qid", default="", help="只 dump 这个题目 id 的 HTML")
    ap.add_argument("--save", default="", help="把整页 HTML 存到该文件")
    args = ap.parse_args()

    chao, _ = login(args.account)
    course = find_course(chao, COURSE_ID)
    point, job, job_info = find_work_job(chao, course, args.chapter)
    if not job:
        raise SystemExit("找不到测验")

    html = fetch_html(chao, course, job, job_info)
    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[已保存] {args.save}  ({len(html)} 字符)")

    soup = BeautifulSoup(html, "html.parser")
    divs = soup.find_all("div", class_="singleQuesId")
    print(f"\n共 {len(divs)} 道题\n")

    if args.qid:
        for d in divs:
            if d.get("data") == args.qid:
                print("=" * 70)
                print(f"题目 id={args.qid} 完整 HTML：")
                print("=" * 70)
                print(d.prettify())
                return
        raise SystemExit(f"没找到 id={args.qid} 的题目")

    # 概览：每题的题型 + 关键元素的 class / 标签结构
    for d in divs:
        qid = d.get("data")
        ti = d.find("div", class_="TiMu")
        code = ti.attrs.get("data", "?") if ti else "?"
        title_div = d.find("div", class_="Zy_TItle")
        title = re.sub(r"\s+", " ", title_div.get_text(" ", strip=True))[:56] \
            if title_div else ""
        # 元素统计
        tags = {}
        for el in d.find_all(True):
            key = el.name + ("." + ".".join(el.get("class") or []) if el.get("class") else "")
            tags[key] = tags.get(key, 0) + 1
        interesting = {k: v for k, v in tags.items()
                       if any(w in k for w in ("blank", "input", "ul", "li", "span"))}
        print(f"[{code}] id={qid}  {title}")
        for k, v in sorted(interesting.items()):
            print(f"      {k} × {v}")
        print()


if __name__ == "__main__":
    main()
