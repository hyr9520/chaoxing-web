# -*- coding: utf-8 -*-
"""探测：主账号已提交的听力测验详情页里，平台是否给出了"正确答案"。

如果有 -> 直接提取入库（这是标准答案，比 ASR 更硬）；没有 -> 走小问级回收。
"""
import json
import os
import re
import sys

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

import web_ui
from api.base import SessionManager

cfg = web_ui.load_ui_config()
# 账号不写死（避免随仓库外泄）：用环境变量 CK_ACCOUNT 指定手机号
_m = [p for p in cfg["profiles"]
      if p.get("username") == os.environ.get("CK_ACCOUNT", "")]
if not _m:
    raise SystemExit("请用环境变量 CK_ACCOUNT 指定学习通账号（手机号）")
prof = _m[0]
ok, msg, courses = web_ui._do_login(prof)
print("登录:", ok, "| 用户:", end=" ", flush=True)
cx = web_ui.state["chaoxing"]
s = SessionManager.get_session()
c = [x for x in courses if x["courseId"] == "262671420"][0]
pts = cx.get_course_point(c["courseId"], c["clazzId"], c["cpi"])["points"]

# 找 3.2 的听力测验
target = [p for p in pts if p["title"].startswith("3.2")]
job = job_info = None
for num in range(3):
    pr = {"clazzid": c["clazzId"], "courseid": c["courseId"], "knowledgeid": target[0]["id"],
          "ut": "s", "cpi": c["cpi"], "v": "2025-0424-1038-3", "mooc2": 1, "num": str(num)}
    r = s.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=pr, timeout=20)
    t = re.findall(r"mArg=\{(.*?)\};", r.text.replace(" ", ""))
    if t:
        try:
            d = json.loads("{" + t[0] + "}")
            for a in d.get("attachments", []):
                if a.get("type") == "workid":
                    job, job_info = a, d.get("defaults", {})
                    break
        except Exception:
            pass
    if job:
        break
if not job:
    print("没找到 workid"); sys.exit(1)

h = s.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
    "api": "1", "workId": job["jobid"].replace("work-", ""), "jobid": job["jobid"],
    "originJobId": job["jobid"], "needRedirect": "true", "skipHeader": "true",
    "knowledgeid": str(job_info["knowledgeid"]), "ktoken": job_info["ktoken"],
    "cpi": job_info["cpi"], "ut": "s", "clazzId": c["clazzId"], "type": "",
    "enc": job.get("enc", ""), "mooc2": "1", "courseid": c["courseId"]}, timeout=25).text

open("_page_main_32.html", "w", encoding="utf-8").write(h)
print(f"页面已存 _page_main_32.html（{len(h)} 字符）")

print("\n=== 搜索正确答案的痕迹 ===")
for pat, label in [
    (r"正确答案", "中文'正确答案'"),
    (r"correctAnswer", "correctAnswer 字段"),
    (r"answer[Cc]orrect", "answerCorrect 字段"),
    (r'class="[^"]*colorGreen[^"]*"', "绿色标记（判对样式）"),
    (r"marking_dui", "marking_dui 判对标记"),
    (r"marking_cuo", "marking_cuo 判错标记"),
    (r"标准答案", "中文'标准答案'"),
]:
    ms = re.findall(pat, h)
    print(f"  {label:<28} {'✓ ' + str(len(ms)) + ' 处' if ms else '无'}")

# 如果有"正确答案"字样，打印上下文
m = re.search(r"正确答案", h)
if m:
    a, b = max(0, m.start() - 200), min(len(h), m.end() + 500)
    print("\n=== '正确答案' 上下文 ===")
    print(h[a:b].replace("\n", " ")[:700])
else:
    print("\n页面没有'正确答案'字段（平台不返回）-> 只能靠作答块的对错标记做小问级回收")
    # 看看作答块结构是否正常（marking_dui/cuo 数量）
    dui = len(re.findall(r"marking_dui", h))
    cuo = len(re.findall(r"marking_cuo", h))
    band = len(re.findall(r"marking_bandui", h))
    ans = re.findall(r'data="([0-9a-f]{32})"\s+type="mp3"', h)
    print(f"  作答块标记: 判对 {dui} / 判错 {cuo} / 半对 {band} | 音频 {len(ans)} 个")
