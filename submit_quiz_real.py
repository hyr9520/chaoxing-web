# -*- coding: utf-8 -*-
"""章节测验真实提交：只做测验、跳过视频，统计平台反馈的正确率。

2026-09-15 新建。用户要求：不刷视频，直接把三门新课的章节测验做一遍提交，
看平台判对多少。

设计：
  * 复用生产的 `study_work()` —— 它内部就是"取题 → query_all → 提交 → 解析成绩"
    的完整链路，不另起炉灶，测的就是真实生产行为。
  * **只遍历章节里的 workid 任务点**，video/document 等一律跳过（不刷视频）。
  * `work_max_retries` 传 1：跑一轮即止，不做"答错→重做"的反复提交。
    原因：重做会不断覆盖学习库，真实正确率反而看不清。要的是**第一次作答的成绩**。
  * 判分结果由 study_work 内部写入学习库（`api/learned.py`），本脚本只管触发 + 统计。

用法：
    python submit_quiz_real.py            # 三门全跑
    python submit_quiz_real.py 光纤        # 只跑名字含"光纤"的
"""
import io
import json
import os
import sys
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

# ⚠️ 2026-09-15 补：必须在**导入 api 之前**设好 CK_DATA_DIR。
# 不设的话 data_dir() 会退化成「项目根目录」，学习库/缓存会写进 chaoxing/ 而不是
# chaoxing_data/acc2/ —— 本次实测就踩了这个坑：303 条学习库记录全落到项目根，
# 与生产实例（acc2）的数据目录脱节。数据没丢，但要手工搬回去。
# 沿用与 start_multi.spawn_instance 完全一致的口径，保证写进同一份数据。
ACC2_DIR = os.path.join(os.path.dirname(HERE), "chaoxing_data", "acc2")
os.environ.setdefault("CK_DATA_DIR", ACC2_DIR)

from api.base import Chaoxing, Account  # noqa: E402
from web_ui import build_tiku  # noqa: E402

TARGETS = [
    ("263717844", "146851705", "光纤通信系统与网络"),
    ("266731313", "153726595", "区域经济学"),
    ("262822368", "144883399", "工程材料学"),
]

TIKU_CONF = {
    "tiku_type": "adapter",
    "adapter_url": "http://127.0.0.1:8060/adapter-service/search?use=local,icodef,buguake,wanneng,tikuhai",
    "ai_fallback": True,
    "ai_endpoint": os.environ.get("CK_AI_ENDPOINT", "https://apihub.agnes-ai.cn/v1"),
    # ⚠️ 不硬编码密钥：从环境变量读（缺失时 AI 兜底不可用，题库链路照常）
    "ai_key": os.environ.get("CK_AI_KEY", ""),
    "ai_model": os.environ.get("CK_AI_MODEL", "agnes-3.0-flash"),
    "auto_submit": True,      # ← 真提交
    "cover_rate": 0.9,
}

# 账号也从环境变量读（避免把手机号/密码写进仓库）
CK_USER = os.environ.get("CK_USER", "")
CK_PWD = os.environ.get("CK_PWD", "")


def main():
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    targets = [t for t in TARGETS if want in t[2]] or TARGETS

    if not CK_USER or not CK_PWD:
        print("请先设置环境变量 CK_USER / CK_PWD（账号手机号与密码）")
        return 2

    tiku = build_tiku(TIKU_CONF["tiku_type"], TIKU_CONF)
    print(f"答题链路: {[type(p).__name__ for p in getattr(tiku, 'providers', [])]}")
    # work_max_retries=1 → 一轮作答即止，不反复重做
    acct = Account(CK_USER, CK_PWD)
    cx = Chaoxing(account=acct, tiku=tiku, query_delay=1.0, work_max_retries=1)
    res = cx.login()
    if not res.get("status"):
        print("登录失败:", res.get("msg"))
        return 1
    print("登录成功:", res.get("msg"), "\n")

    courses = {str(c.get("courseId")): c for c in cx.get_course_list()}

    stat = Counter()
    detail = []
    for cid, clz, title in targets:
        c = courses.get(cid)
        if not c:
            print(f"!!! 未找到课程 {title}")
            continue
        print("=" * 74)
        print(f"课程: {c.get('title')}")
        print("=" * 74)

        chapter = cx.get_course_point(cid, clz, c.get("cpi") or "")
        for p in (chapter.get("points") or []):
            try:
                jobs, jinfo = cx.get_job_list(c, p)
            except Exception as e:
                print(f"  [{p.get('title')}] 读任务点失败 {type(e).__name__}")
                continue
            works = [j for j in jobs if j.get("type") == "workid"]
            if not works:
                # 该章节没测验 → 直接跳过，绝不碰视频
                continue
            for j in works:
                pt = (p.get("title") or "")[:28]
                print(f"  → 提交测验: {pt:<30}", end=" ", flush=True)
                try:
                    r = cx.study_work(c, j, jinfo)
                except Exception as e:
                    print(f"异常 {type(e).__name__}: {e}")
                    stat["异常"] += 1
                    detail.append({"course": title, "paper": p.get("title"),
                                   "result": f"异常:{type(e).__name__}"})
                    continue
                nm = getattr(r, "name", str(r))
                print(f"[{nm}]")
                stat[nm] += 1
                detail.append({"course": title, "paper": p.get("title"), "result": nm})

    print()
    print("=" * 74)
    print("提交结果汇总：")
    for k, v in stat.most_common():
        print(f"   {k:<20} {v}")

    out = os.path.join(HERE, "backups",
                       f"quiz_submit_{__import__('time').strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=1)
    print(f"\n明细: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
