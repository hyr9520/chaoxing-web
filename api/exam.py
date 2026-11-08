# -*- coding: utf-8 -*-
"""超星「考试」模块（独立 exam，区别于作业与章节测验）。

## 协议（2026-09-26 真机验证）

考试列表**不是**独立 URL，必须走三步：

1. **课程中间页**（拿 `cpi`/`enc`/`openc`）
   ```
   GET https://mooc1-1.chaoxing.com/mooc-ans/visit/stucoursemiddle?courseid=..&clazzid=..&vc=1&cpi=..&ismooc2=1&v=2
   → 302 到 mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?...&enc=<hex>
   ```
   `enc` 只在**跳转后的 URL** 里（不固定，每次不同）。

2. **考试列表页**
   ```
   GET https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list
       ?courseid=..&clazzid=..&cpi=..&ut=s&enc=..&openc=..
   → 31616 字节 HTML，标题「考试列表」
   ```
   ⚠️ `enc` 和 `openc` **两个都要**（只给 enc → "参数传递不正确！"）
   ⚠️ `openc` 是页面里 `<input id="openc">` 的值（考试专用令牌）

3. **考试答题页**（从列表条目点击进入）
   ```
   GET https://mooc1.chaoxing.com/exam/test/reVersionTestStartNew?courseId=..&...
   ```

## 进考场真实链路（2026-09-26 从 stu-exam-list.js 反解，真机确认前两步）

考试列表页 `onclick` 全部指向 `viewExamAnswer(paperId, relationId)`（= 看答案页），
**不是**直接进考场。真正的「进入考试」是 JS 函数 `startTest()`：

```js
// 1) 先拿动态 enc（⚠️ 每次进考场都要重新取）
GET /exam/test/getIdentifyCode?id=<testPaperId>&inpCode=<验证码>
    &classId=..&courseId=..&mooc2=1
    → {"status": true, "enc": "<32位hex>"}

// 2) 用它进考场
GET /exam/test/reVersionTestStartNew
    ?courseId=..&classId=..&tId=<testPaperId>&id=<testUserRelationId>
    &p=1&tag=1&enc=<上一步的enc>&cpi=..&openc=..&newMooc=true
```

`_HOST_CP2_` 在考试域下解析为 `""`（空串），所以上两步要打在
`https://mooc1.chaoxing.com` 上。

### 🔴 未打通的部分（2026-09-26 实测）
- `getIdentifyCode` 已 **404**（GET）/ **405**（POST）→ 该接口在当前版本已下线或换名，
  `enc` 的签发入口**尚未定位**。缺 `enc` 直连 `reVersionTestStartNew` 返回
  1148 字节的「**无权限访问！**」提示页（title=提示）。
- 列表页自身的 `enc`（出现在 `changeStatus()` 的 URL 里）**不能**用于答题页 ——
  实测拿它进考场仍是「无权限访问」。
- ⚠️ 因此**「考试自动作答」在拿到 `enc` 之前无法实现，也无法真机验证**。
  用户当前账号也没有「未完成的考试」（只有 已过期 / 已完成），
  重做开关 `isRetest` 亦为 false → 已完成的考试无法重做。

### 可重考时的额外步骤
`isRetest == true` 时，`startTest()` 会先调：
```
GET /exam/test/reVersionReTest?courseId=..&classId=..&tId=..&id=..&p=1&tag=1
→ {"status": true}
```
成功后才进 `reVersionTestStartNew`。

## 列表页结构（解析要点）

- 空态：`<div class="null-data">暂无考试</div>`（`has-content` 里只放这一个）
- **有考试时**（2026-09-26 真机确认）：
  ```
  div.bottomList > ul > li
    └ div[onclick="viewExamAnswer('paperId','relationId')"]
        ├ div.tag.icon-exam-g
        └ div.right-content
            ├ p.overHidden2.fl   ← 考试标题
            └ p.status.fl        ← 状态词（已过期 / 已完成 / 未完成 …）
  ```
- 页面级隐藏字段：`courseId` / `classId` / `cpi` / `openc` / `examEnc` /
  `testPaperId` / `testUserRelationId` / `isRetest` / `fid` / `stuLoginName`
- 页面内 `TestRelationInfo = {"<paperId>":{"limitTime":60,"expiredAutoSubmit":1}, ...}`
- 状态筛选 radio 的 `data`：`0`=全部 `1`=未完成 `2`=已完成

### 状态词实测值
`已过期`（最常见，套卷类） / `已完成` / `未完成` / `待批阅` / `已批阅`
⚠️ **「已过期」与「已完成」都不可作答**，只有出现「未完成」才有机会进考场。
"""
import re
import os
import json
import time
import random
import secrets
import logging
from math import floor
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, unquote
import html as _html

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ---- 接口常量 ----
PAGE_COURSE_MIDDLE = "https://mooc1-1.chaoxing.com/mooc-ans/visit/stucoursemiddle"
PAGE_COURSELIST_DATA = "https://mooc1-1.chaoxing.com/visit/courselistdata"
PAGE_EXAM_LIST = "https://mooc1.chaoxing.com/exam-ans/mooc2/exam/exam-list"
PAGE_EXAM_START = "https://mooc1.chaoxing.com/exam/test/reVersionTestStartNew"
API_EXAM_IS_EXPIRE = "https://mooc1.chaoxing.com/exam/test/isExpire"

# ---- 手机客户端协议（2026-09-26 从 guowang23333/CxKitty 实录，作为唯一权威） ----
# 考试走的是 **手机端协议**（mooc1-api 域），不是 PC 的 exam-ans/mooc2 列表链路。
# 三步：封面页(cover) → 开始考试(start, 302 出 enc) → 逐题拉取(reVersionTestStartNew, start=n) → 提交。
PAGE_EXAM_COVER = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/task-exam"
PAGE_EXAM_QUESTION = "https://mooc1-api.chaoxing.com/exam-ans/exam/test/reVersionTestStartNew"
PAGE_EXAM_PREVIEW = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/preview"
API_EXAM_START = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/start"
API_EXAM_ANSWER_SHEET = "https://mooc1-api.chaoxing.com/exam-ans/exam/phone/loadAnswerStatic"
API_EXAM_SUBMIT = "https://mooc1.chaoxing.com/exam-ans/exam/test/reVersionSubmitTestNew"
PAGE_EXAM_LIST_PHONE = "https://mooc1-api.chaoxing.com/exam/phone/task-list"

# ── 重做 / 查看答案（2026-11-07 新增，真机验证）─────────────────────
PAGE_EXAM_RETEST = "https://mooc1.chaoxing.com/exam-ans/exam/test/reVersionReTest"
PAGE_EXAM_LOOK = "https://mooc1.chaoxing.com/exam-ans/exam/test/look"
PAGE_PAPER_DETAIL = ("https://mooc1.chaoxing.com/exam-ans/exam/test/"
                     "reVersionPaperMarkContentNew")
PAGE_MYCOURSE_TRANSFER = "https://mooc1.chaoxing.com/exam-ans/mycourse/transfer"


def set_retest(session, course: Dict[str, Any], exam: Dict[str, Any]) -> Dict[str, Any]:
    """设置重考（等价页面上那个「重考」按钮）。

    链路来自 `stu-exam-list.js::toSetReTest`（2026-11-07 反解 + 真机验证）：
        GET /exam/test/reVersionReTest?courseId=&classId=&tId={paperId}&id={relationId}&p=1&tag=1
        → {"msg":"重考成功" | "已是待解答状态！", "status":true}

    注意 `id` 用的是 **testUserRelationId**（考试列表里的 relation_id），
    不是 exam_id —— 传错会返回 status:false。
    """
    cid = str(course.get("courseId") or "")
    cls = str(course.get("clazzId") or course.get("classId") or "")
    pid = str(exam.get("test_paper_id") or exam.get("paper_id")
              or exam.get("exam_id") or "")
    rid = str(exam.get("test_user_relation_id") or exam.get("relation_id") or "")
    if not (cid and cls and pid and rid):
        return {"ok": False,
                "msg": f"缺少参数 courseId={cid} classId={cls} "
                       f"paperId={pid} relationId={rid}"}
    try:
        r = session.get(PAGE_EXAM_RETEST, timeout=25,
                        params={"courseId": cid, "classId": cls, "tId": pid,
                                "id": rid, "p": 1, "tag": 1})
        data = json.loads(r.text) if r.text.strip().startswith("{") else {}
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "msg": f"请求失败：{type(e).__name__}: {e}"}
    return {"ok": bool(data.get("status")),
            "msg": data.get("msg") or f"HTTP {r.status_code}", "raw": data}


def parse_answer_page(body: str) -> List[Dict[str, Any]]:
    """解析「试卷详情」HTML → [{title,type,options,right,mine}]。

    页面结构（2026-11-06 真机）：
      <div class="questionLi" data="{qid}">
        <h3 class="mark_name">1. <span>(单选题, 0.5 分)</span>
            <span class="qtContent">题干</span></h3>
        <ul class="mark_letter qtDetail"><li>A. 选项</li>...</ul>
        <div class="mark_key"> 我的答案:<span class="stuAnswerContent">D</span>
                               正确答案:<span class="rightAnswerContent">D</span>

    ⚠️ B1/B2/C 型题（共用备选答案）的 `rightAnswerContent` 在平台上**本来就是空的**
       （老师没设标准答案）⇒ 这里如实返回空串，**不猜**。
    """
    out: List[Dict[str, Any]] = []
    blocks = re.split(r'<div class="marBom60 questionLi[^"]*"[^>]*data="(\d+)"', body)
    for i in range(1, len(blocks) - 1, 2):
        seg = blocks[i + 1]
        m_type = re.search(r"\((单选题|多选题|判断题|填空题|简答题|名词解释|论述题|计算题)"
                           r"[,，]\s*([\d.]+)\s*分\)", seg)
        qtype = m_type.group(1) if m_type else ""
        m_q = re.search(r'class="qtContent">([\s\S]*?)</span>', seg)
        title = _clean_text(m_q.group(1)) if m_q else ""
        if not title:
            continue
        opts: List[Dict[str, str]] = []
        m_list = re.search(r'class="mark_letter[^"]*"[^>]*>([\s\S]*?)</ul>', seg)
        if m_list:
            for li in re.findall(r"<li[^>]*>([\s\S]*?)</li>", m_list.group(1)):
                t = _clean_text(li)
                if not t:
                    continue
                lm = re.match(r"^([A-Z])[.、\s]\s*(.+)$", t)
                opts.append({"letter": lm.group(1), "text": lm.group(2).strip()}
                            if lm else {"letter": "", "text": t})
        m_r = re.search(r'class="rightAnswerContent"[^>]*>([\s\S]*?)</span>', seg)
        m_m = re.search(r'class="stuAnswerContent"[^>]*>([\s\S]*?)</span>', seg)
        out.append({
            "title": title, "type": qtype, "options": opts,
            "right": _clean_text(m_r.group(1)) if m_r else "",
            "mine": _clean_text(m_m.group(1)) if m_m else "",
        })
    return out


def _clean_text(s: str) -> str:
    """去标签 + 实体还原 + 空白压缩（页面里 <br>/&nbsp; 很多）。"""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def fetch_exam_answer_page(session, course: Dict[str, Any],
                           exam: Dict[str, Any]) -> Dict[str, Any]:
    """取「试卷详情」页 HTML（含每题**正确答案**）。

    ⚠️ **安全边界**：只有平台**允许查看答案**时才有东西可提取。
       不允许查看的考试，这一步拿到的会是空壳页 ⇒ 直接返回 ok=False，
       **绝不做任何猜测或反推**。

    链路（2026-11-06 真机验证，三步缺一不可）：
      1) `/exam/test/look?...&examId=&examAnswerId={relationId}&cpi=` → 交卷成功页
      2) 从该页取「查看试卷详情」的 `refer` 参数（指向 reVersionPaperMarkContentNew）
      3) **必须**走 `/mycourse/transfer?refer=<urlencoded>`；
         直连 refer 目标只有 803 字节壳页，走 transfer 才拿到 777KB 完整页。

    Returns: {"ok": bool, "html": str, "msg": str, "question_count": int}
    """
    cid = str(course.get("courseId") or "")
    cls = str(course.get("clazzId") or course.get("classId") or "")
    cpi = str(course.get("cpi") or "")
    eid = str(exam.get("test_paper_id") or exam.get("paper_id")
              or exam.get("exam_id") or "")
    rid = str(exam.get("test_user_relation_id") or exam.get("relation_id") or "")
    if not (cid and cls and eid and rid):
        return {"ok": False, "html": "", "msg": "缺少 courseId/classId/examId/relationId",
                "question_count": 0}
    try:
        # ① 交卷成功页
        r1 = session.get(PAGE_EXAM_LOOK, timeout=25,
                         params={"courseId": cid, "classId": cls, "examId": eid,
                                 "examAnswerId": rid, "cpi": cpi})
        page = r1.text
        m = re.search(r'refer=([^"&\']*reVersionPaperMarkContentNew[^"\']*)', page)
        if not m:
            return {"ok": False, "html": "", "question_count": 0,
                    "msg": "该考试不提供答案查看（页面无「查看试卷详情」入口）"}
        refer = unquote(_html.unescape(m.group(1)))
        # ② 走 transfer
        r2 = session.get(PAGE_MYCOURSE_TRANSFER, timeout=30, params={
            "moocId": cid, "clazzid": cls, "ut": "s", "refer": refer})
        body = r2.text
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "html": "", "question_count": 0,
                "msg": f"请求失败：{type(e).__name__}: {e}"}
    cnt = body.count('class="rightAnswerContent"')
    if cnt == 0:
        return {"ok": False, "html": "", "question_count": 0,
                "msg": "答案页未包含正确答案（可能不允许查看或已过期）"}
    return {"ok": True, "html": body, "msg": "", "question_count": cnt}

# 手机端 UA（超星 App）——考试链路必须带，否则被判 PC 环境拒答
UA_MOBILE = ("Mozilla/5.0 (Linux; Android 13; 2206122SC Build/TKQ1.220829.002; wv) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/104.0.5112.97 "
             "Mobile Safari/537.36 (schild:5f5f50e45eea4b9a8f5f50e45eea4b9a) "
             "(device:2206122SC) Language/zh_CN com.chaoxing.mobile/"
             "ChaoXingStudy_3_6.4.4_android_phone_10830_262 (@Kalimdor)")

# 题型枚举（与 CxKitty schema.py 完全一致，用于表单字段构造）
QT_SINGLE = 0      # 单选题
QT_MULTIPLE = 1    # 多选题
QT_BLANK = 2       # 填空题
QT_JUDGE = 3       # 判断题
QT_SHORT = 4       # 简答题
QT_NOUN = 5        # 名词解释
QT_ESSAY = 6       # 论述题
QT_CALC = 7        # 计算题
QT_OTHER = 8       # 其它
QT_ENTRY = 9       # 分录题
QT_MATERIAL = 10   # 资料题
QT_LINK = 11       # 连线题
QT_SORT = 13       # 排序题
QT_CLOZE = 14      # 完型填空
QT_READING = 15    # 阅读理解

_QT_NAME = {
    QT_SINGLE: "单选题", QT_MULTIPLE: "多选题", QT_BLANK: "填空题",
    QT_JUDGE: "判断题", QT_SHORT: "简答题", QT_NOUN: "名词解释",
    QT_ESSAY: "论述题", QT_CALC: "计算题", QT_OTHER: "其它",
    QT_ENTRY: "分录题", QT_MATERIAL: "资料题", QT_LINK: "连线题",
    QT_SORT: "排序题", QT_CLOZE: "完型填空", QT_READING: "阅读理解",
}

# 能自动作答的题型（其余题型的答案链路尚未覆盖，遇到就跳过但继续考试）
ANSWERABLE_TYPES = (QT_SINGLE, QT_MULTIPLE, QT_BLANK, QT_JUDGE)

# 虚拟 IMEI（登录后可换成真实的；同一考试 Session 内保持一致即可）
_DEFAULT_IMEI = ""


class ExamUnavailable(Exception):
    """该课程没有考试入口 / 未开放。"""


class ExamError(Exception):
    """考试链路通用异常。"""


class ExamDenied(ExamError):
    """无权限访问（enc 失效 / 不在开放窗口 / 非指定 IP）。"""


class ExamSubmitted(ExamError):
    """考试已提交，不能再作答。"""


# ── 「最短答题时间」兼容（2026-09-27）────────────────────────────────
# 背景：老师发布考试时可设「最短答题时间」（发布参数 `submitLimitTime`）。
# 本程序答题极快（每题约 1-2 秒），50 题常常 1-2 分钟就答完，远早于下限，
# 此时平台会拒绝交卷。原实现直接返回「交卷失败」并结束 → 考试停在"进行中"
# （题目答案已逐题 `tempSave` 暂存，不会丢，但需要人工去点交卷）。
# 现在：识别到这类提示就**等待后重试**；累计有硬上限，绝不无限等待。
_MIN_SUBMIT_HINTS = (
    "最短",            # 覆盖「未到最短答题时间」「不足最短时长」等
    "时间不足",
    "答题时间不足",
    "作答时间不足",
    "未到最短",
    # 🔴 2026-09-28 真机补漏：老师可设「考试开始后 N 分钟内不允许交卷」，
    #    平台报文是「限时提交：考试10分钟内不允许提交考试」——
    #    上面几个词**一个都匹配不上** → `_is_too_early` 返回 False →
    #    程序把"答得太快"当成"交卷失败"直接放弃整场
    #    （真机事故：考试《550》09:30:28 开考、09:31:28 就报"交卷失败"结束，
    #      20 题答案全部暂存但没交上；用户报"设了 10 分钟才能交卷，
    #      结果几分钟不到就结束了，还没有日志"）。
    #    这两个词命中后，下面的等待重试逻辑会等够时间再交（答案已暂存不会丢）。
    "限时提交",
    "不允许提交",
    "分钟内不允许",
)
_SUBMIT_WAIT_MAX = 1800.0    # 累计最多等 30 分钟
_SUBMIT_WAIT_GAP = 60.0      # 每次默认等 60 秒再试


def _is_too_early(msg: str) -> bool:
    """平台是否以「还没到最短答题时间」为由拒绝交卷。"""
    s = str(msg or "")
    return any(h in s for h in _MIN_SUBMIT_HINTS)


def _norm_options(options: Any) -> List[Dict[str, str]]:
    """把考试题的 options 归一化成 `[{letter, text}]`（2026-10-12）。

    为什么需要：平台返回的是 `list[dict]`（`{"letter","value","text"}`），
    直接落盘会让核对脚本面对两套结构。归一后固定为字母 + 文本两列，
    判断题无选项，返回空列表。
    """
    out: List[Dict[str, str]] = []
    for o in options or []:
        if isinstance(o, dict):
            letter = str(o.get("letter") or o.get("key") or "").strip()
            text = str(o.get("text") or o.get("value") or "").strip()
        else:
            letter, text = str(o).strip(), ""
        if letter or text:
            out.append({"letter": letter, "text": text})
    return out


def _parse_wait_seconds(msg: str) -> int:
    """从提示里解析「还需等待多少秒」；解析不出返回 0（走默认间隔）。"""
    s = str(msg or "")
    m = re.search(r"(\d+)\s*分", s)
    if m:
        return int(m.group(1)) * 60 + 5
    m = re.search(r"(\d+)\s*秒", s)
    if m:
        return int(m.group(1)) + 5
    return 0


def _next_wait_gap(msg: str, waited: float, remain_time: int = 0) -> float:
    """🔴 2026-09-28 新增（纯函数，可单测）：下一次交卷重试该等多久。

    规则（从严到宽依次夹取）：
      ① 提示里的明确时长（如「10分钟内不允许提交」→ 605s），否则默认 60s
      ② 不超过剩余考试时间 - 60s（留余量给交卷请求本身，别等到超时）
      ③ 不超过累计上限 `_SUBMIT_WAIT_MAX`，且单次不超过 600s

    Returns:
        秒数；**≤0 表示不该再等**（剩余时间已不够），调用方应放弃并提示手动交卷。
    """
    gap = float(_parse_wait_seconds(msg) or _SUBMIT_WAIT_GAP)
    if remain_time and remain_time > 0:
        safe = float(remain_time) - 60.0
        if safe <= 0:
            return 0.0
        gap = min(gap, safe)
    return min(gap, _SUBMIT_WAIT_MAX - waited, 600.0)


def _get_imei() -> str:
    """取本次进程的虚拟 IMEI（16 进制 32 位）。"""
    global _DEFAULT_IMEI
    if not _DEFAULT_IMEI:
        _DEFAULT_IMEI = secrets.token_hex(16)
    return _DEFAULT_IMEI


def get_exam_signature(uid, qid, x, y) -> Dict[str, Any]:
    """考试提交接口的风控签名参数组（pos / rd / value / _edt）。

    🔴 算法原样移植自 guowang23333/CxKitty `cxapi/utils.py::get_exam_signature`。
    缺这四个参数 → 提交接口拒绝（或记作无效作答）。

    Args:
        uid: 用户 puid（数字）
        qid: 题目 id（可为 0，表示仅交卷）
        x, y: 屏幕点击坐标（随机即可）
    """
    ts = f"{round(time.time() * 1000)}"
    r1 = random.randrange(0, 9)
    r2 = random.randrange(0, 9)
    a = f"{secrets.token_hex(16)}{ts[4:]}{r1}{r2}{qid or ''}"
    temp = 0
    for ch in a:
        temp = (temp << 5) - temp + ord(ch)
    salt = f"{r1}{r2}{(0x7fffffff & temp) % 10}"
    enc_val = f"{uid}"
    if qid:
        enc_val += f"_{qid}"
    enc_val += f"|{salt}"
    enc_val2 = "".join(str(ord(c)) for c in enc_val)
    b = len(enc_val2) // 5
    c = int(enc_val2[b] + enc_val2[2 * b] + enc_val2[3 * b] + enc_val2[4 * b])
    d = len(enc_val) // 2 + 1
    e = (c * int(enc_val2[:10]) + d) % 0x7FFFFFFF
    pos = f"({x}|{y})"
    result = ""
    for ch in pos:
        t = ord(ch) ^ floor(e / 0x7FFFFFFF * 0xFF)
        result += f"{t:02x}"
        e = (c * e + d) % 0x7FFFFFFF
    return {
        "pos": f"{result}{secrets.token_hex(4)}",
        "rd": random.random(),
        "value": pos,
        "_edt": f"{ts}{salt}",
    }


def mobile_headers(user_id: str = "") -> Dict[str, str]:
    """手机端协议请求头（考试链路必须带）。"""
    h = {
        "User-Agent": UA_MOBILE,
        "X-Requested-With": "com.chaoxing.mobile",
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    return h


# ============================================================================
# 手机端考试协议（CxKitty 逻辑）—— 完整链路
# ============================================================================

def parse_exam_question(node) -> Optional[Dict[str, Any]]:
    """解析单题节点（手机端考试页 DOM）。

    真机结构（CxKitty 实录）：
        div.questionWrap.singleQuesId.ans-cc-exam[data=qid]
          input[name='questionId']        ← 题目 id
          input[name^='type']             ← 题型数字
          div.tit                         ← 题干（含题型、分值、正文）
          ul / .answer_p ...              ← 选项
    """
    if node is None:
        return None
    qid_node = node.select_one("input[name='questionId']")
    type_node = node.select_one("input[name^='type']")
    if qid_node is None or type_node is None:
        return None
    try:
        qid = str(qid_node.get("value", "")).strip()
        qtype = int(type_node.get("value", "-1"))
    except (TypeError, ValueError):
        return None
    if not qid:
        return None

    # 题干：div.tit 去掉 <h3>（题型+总分）、「N.」「（x分）」、重复的题型名
    # 🔴 2026-09-27 真机实录修正：原逻辑按行过滤，但真机 div.tit 里是一整行
    #    `<h3>单选题（共20题，100.0分）</h3> 1.<span>（5.0分）</span>单选题<br/>题干`
    #    → 结果题干带着「（共20题，100.0分） 1. （5.0分） 单选题」整串垃圾，
    #    题库/AI 都没法匹配。现在按 <br/> 切，取题干主体。
    title = ""
    tit = node.select_one("div.tit")
    if tit is not None:
        _html = str(tit)
        _html = re.sub(r"<h3[^>]*>.*?</h3>", "", _html, flags=re.S | re.I)
        _segs = re.split(r"<br\s*/?>", _html, flags=re.I)
        _body = "<br/>".join(_segs[1:]) if len(_segs) > 1 else _html
        _t = BeautifulSoup(_body, "html.parser").get_text(" ", strip=True)
        _t = re.sub(r"[（(]\s*[\d.]+\s*分\s*[)）]", "", _t)      # （5.0分）
        _t = re.sub(r"^\s*\d+\s*[.、]\s*", "", _t)              # 1.
        _t = re.sub(r"^\s*(单选题|多选题|判断题|填空题|简答题|"
                    r"名词解释|论述题|计算题|其它)\s*", "", _t)   # 题型名
        title = re.sub(r"\s+", " ", _t).strip()
    title = title.strip()

    # 选项：🔴 2026-09-27 真机实录 —— 结构是 **div.answerList**，不是 span.num_option：
    #     <div class="singleChoice answerList radioList singleoption" name="C">
    #       <span class="No">A</span>
    #       <div class="answerInfo"><cc>圣诞节</cc></div>
    #     </div>
    #   ⚠️ `name` 是**提交值**，`span.No` 才是界面显示的 A/B/C/D。
    #      发布时勾了「选项乱序」 → 两者不一致（显示 A 的提交值可能是 C）！
    #      所以必须同时保留 letter（给 AI 看）和 value（提交用）。
    options = []
    for opt in node.select("div.answerList"):
        letter_node = opt.select_one("span.No")
        cc = opt.select_one("cc")
        letter = (letter_node.get_text(" ", strip=True) if letter_node else "").strip()
        val = (opt.get("name") or "").strip()
        text = (cc.get_text(" ", strip=True) if cc is not None else "").strip()
        if not (letter or val or text):
            continue
        options.append({"letter": letter or val, "value": val or letter,
                        "text": text or letter})
    if not options:      # 兜底：旧结构（span.num_option）
        for sp in node.select("span.num_option"):
            letter = (sp.get_text(" ", strip=True) or "").strip()
            val = (sp.get("data") or "").strip()
            content = ""
            parent = sp.find_parent("div", class_="clearfix") or sp.parent
            if parent is not None:
                ap = parent.select_one(".answer_p")
                if ap is not None:
                    content = ap.get_text(" ", strip=True)
            options.append({"letter": letter, "value": val, "text": content or letter})

    blank_count = len(node.select("textarea"))
    return {
        "id": qid,
        "type": qtype,
        "type_name": _QT_NAME.get(qtype, f"未知({qtype})"),
        "title": title,
        "options": options,
        "blank_count": blank_count,
    }


def parse_exam_page(html_text: str) -> Dict[str, Any]:
    """解析考试单题页：公共参数 + 当前题目。"""
    soup = BeautifulSoup(html_text, "html.parser")
    out: Dict[str, Any] = {"error": None, "form": {}, "questions": [], "blank_tip": ""}

    # 错误提示
    tip = soup.body.select_one("p.blankTips") if soup.body else None
    if tip is not None:
        out["blank_tip"] = tip.get_text(" ", strip=True)
        return out

    form = soup.select_one("form#submitTest")
    if form is None:
        out["error"] = "找不到 form#submitTest"
        return out

    def _f(fid: str) -> str:
        n = form.select_one(f"input#{fid}")
        return (n.get("value", "") if n is not None else "") or ""

    out["form"] = {
        "enc": _f("enc"),
        "encRemainTime": _f("encRemainTime"),
        "remainTime": _f("remainTime"),
        "encLastUpdateTime": _f("encLastUpdateTime"),
    }
    wm = soup.select_one("input#ExamWaterMark")
    if wm is not None:
        out["form"]["ExamWaterMark"] = wm.get("value", "")

    node = form.select_one("div.questionWrap.singleQuesId.ans-cc-exam") \
        or soup.select_one("div.questionWrap.singleQuesId.ans-cc-exam")
    q = parse_exam_question(node)
    if q:
        out["questions"].append(q)
    else:
        out["error"] = "解析不到题目节点"
    return out


def fetch_exam_cover(session, course: Dict[str, Any], exam_id, enc_task,
                     user_id: str = "") -> Dict[str, Any]:
    """① 考试封面页：拿 examAnswerId / monitorEnc / need_code。

    Args:
        exam_id: 考试 id（= taskrefId = tId）
        enc_task: 考试校验 key（从考试列表条目拿）
    """
    params = {
        "redo": 1,                    # 强制跳过"重新作答"重定向
        "taskrefId": exam_id,
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "userId": user_id,
        "role": "",
        "source": 0,
        "enc_task": enc_task,
        "cpi": course.get("cpi") or "",
        "vx": 0,
        "examsignal": 1,              # 强制跳过考试承诺书
    }
    resp = session.get(PAGE_EXAM_COVER, params=params, timeout=30,
                       allow_redirects=False, headers=mobile_headers(user_id))
    if resp.status_code == 302:
        loc = resp.headers.get("Location", "")
        if "/exam-ans/exam/phone/look" in loc:
            raise ExamSubmitted("考试已完成")
        raise ExamError(f"封面页 302 到未知位置：{loc[:120]}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    h2 = soup.select_one("h2.color6.fs36.textCenter.marBom60.line64")
    if h2 is not None:
        msg = h2.get_text(" ", strip=True)
        raise ExamUnavailable(f"考试不可进入：{msg}")

    def _inp(i: str) -> str:
        n = soup.select_one(f"input#{i}")
        return (n.get("value", "") if n is not None else "") or ""

    title_node = soup.select_one("span.overHidden2")
    return {
        "title": title_node.get_text(" ", strip=True) if title_node else "",
        "exam_answer_id": _inp("testUserRelationId"),
        "monitor_enc": _inp("monitorEnc"),
        "face_need": bool(_inp("faceRecognitionCompare")),
        # 🔴 2026-09-27：封面页会带 `captchaCheck=1` 表示开了**人机验证**。
        # 页面逻辑：`if($('#captchaCheck').val()=='1'){ showCXCaptcha(...) }`
        # → 真机需过滑块/点选，成功后把 `captchavalidate` 拼进 start 请求。
        # 程序给不了这个票据，硬提交只会拿到一张含糊的"未知原因"错误页。
        # 这里显式识别出来，交给上层给出明确提示（而不是让人以为程序坏了）。
        "captcha_need": _inp("captchaCheck") == "1",
        "exam_code_need": _inp("needcode") == "1",
        # 2026-09-27：带上封面页完整 URL —— 自动过滑块模块要在**同一个会话**
        # 的真浏览器里打开它（会话一致，签发的 captchavalidate 才有效）。
        "cover_url": PAGE_EXAM_COVER + "?" + urlencode(params),
    }


# ── 开考前「要求探测」（2026-10-12）─────────────────────────────────
# 何同学诉求：开考前先看清这场考试有什么要求（人脸 / 人机验证 / 考试码 /
# 能不能进 / 时间窗口），别等开考了才发现做不了。
#
# 🔴 关键约束：本函数**只 GET 封面页**，绝不调 start_exam / 不暂存 / 不提交
#    ⇒ **不消耗作答机会**，可以反复调用。
#
# ⚠️ 已知边界（务必如实告知，别让界面骗人）：
#   · 「考试限时」「最短答题时间」「是否允许中途离开」这三项**封面页拿不到**，
#     平台只在**进入考场后的答题页**（form#submitTest 的 remainTime / encRemainTime）
#     才给出。所以预检只能标"进考场后可见"。
#   · 「离开作答页面」计数是**浏览器端行为检测**（监听听 visibilitychange /
#     focus-blur 后上报），本程序走协议模拟、不执行 JS ⇒ **不会产生离开记录**。
#     平台侧唯一会替考生自动交卷的时机是**考试截止时间到点**。
_EXAM_TIME_RE = re.compile(r"(20\d\d-\d\d-\d\d \d\d:\d\d)")
# 封面页拿不到、必须进考场才可见的项 —— 如实列出，别让界面撒谎
_PRECHECK_UNKNOWN = [
    "考试限时（剩余时间）", "最短答题时间（不满 N 分钟不许交卷）",
    "允许的重做次数", "防作弊开关（切屏/抓拍/仅限 App）",
]


def precheck_exam(session, course: Dict[str, Any], exam_id, enc_task,
                  user_id: str = "") -> Dict[str, Any]:
    """开考前只读探测一场考试的要求。**不消耗作答机会**。

    Returns:
        {title, can_enter, state, reason, start_time, end_time,
         face_need, captcha_need, exam_code_need, exam_answer_id,
         limits[], unknown[], cover_url}
    """
    params = {
        "redo": 1, "taskrefId": exam_id,
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "userId": user_id, "role": "", "source": 0,
        "enc_task": enc_task, "cpi": course.get("cpi") or "",
        "vx": 0, "examsignal": 1,
    }
    out: Dict[str, Any] = {
        "title": "", "can_enter": False, "state": "", "reason": "",
        "start_time": "", "end_time": "", "student": "",
        "face_need": None, "captcha_need": None, "exam_code_need": None,
        "exam_answer_id": "", "limits": [], "unknown": list(_PRECHECK_UNKNOWN),
        "cover_url": PAGE_EXAM_COVER + "?" + urlencode(params),
    }
    try:
        resp = session.get(PAGE_EXAM_COVER, params=params, timeout=30,
                           allow_redirects=False, headers=mobile_headers(user_id))
    except Exception as e:                                           # noqa: BLE001
        out.update(state="error", reason=f"封面页请求失败：{type(e).__name__}: {e}")
        return out

    if resp.status_code == 302:
        loc = resp.headers.get("Location", "")
        if "/exam-ans/exam/phone/look" in loc:
            out.update(state="submitted", can_enter=False,
                       reason="已提交过（本场没有剩余作答机会）")
        else:
            out.update(state="redirect", can_enter=False,
                       reason=f"被重定向到：{loc[:120]}")
        return out
    if resp.status_code != 200:
        out.update(state="http_%d" % resp.status_code,
                   reason=f"封面页 HTTP {resp.status_code}")
        return out

    soup = BeautifulSoup(resp.text, "html.parser")

    def _inp(i: str) -> str:
        n = soup.select_one(f"input#{i}")
        return (n.get("value", "") if n is not None else "") or ""

    # 分支 A：「考试尚未开始 / 已结束」这类提示页
    h2 = soup.select_one("h2.color6.fs36.textCenter.marBom60.line64")
    if h2 is not None:
        msg = h2.get_text(" ", strip=True)
        out.update(state=msg, reason=msg)
        for p in soup.select("div.result_gray_div p"):
            t = p.get_text(" ", strip=True)
            if t.startswith("姓名"):
                out["student"] = t.split("：", 1)[-1].strip()
            elif "开始时间" in t:
                out["start_time"] = t.split("：", 1)[-1].strip()
            elif "截止时间" in t:
                out["end_time"] = t.split("：", 1)[-1].strip()
        if "尚未开始" in msg:
            out["limits"].append(
                f"未到开考时间（{out['start_time'] or '见页面'} 开始），现在进不去")
        else:
            out["limits"].append(msg)
        return out

    # 分支 B：正常封面页 = 现在就能进
    title_node = soup.select_one("span.overHidden2")
    out.update(
        title=title_node.get_text(" ", strip=True) if title_node else "",
        can_enter=True, state="ready", reason="可以进入",
        exam_answer_id=_inp("testUserRelationId"),
    )
    out["face_need"] = bool(_inp("faceRecognitionCompare"))
    out["captcha_need"] = _inp("captchaCheck") == "1"
    out["exam_code_need"] = _inp("needcode") == "1"

    # 能探到的显式限制
    if out["face_need"]:
        out["limits"].append("需要**人脸识别** —— 本程序不处理，会跳过该考试")
    if out["captcha_need"]:
        out["limits"].append("需要**人机验证（滑块）** —— 程序会尝试自动通过；"
                             "失败则跳过，且不消耗作答机会")
    if out["exam_code_need"]:
        out["limits"].append("需要**考试码** —— 开考时必须把老师给的码填进来")

    # 页面里若出现时间字样，顺手抓出来（不同发布方式字段位置不一）
    txt = resp.text
    if not out["start_time"]:
        m = _EXAM_TIME_RE.search(txt)
        if m:
            out["start_time"] = m.group(1)

    # 注：若走到这里说明封面页是"可进入"形态，人脸/滑块/考试码已按真实值填好；
    #     「未开考」分支在更早 return，那三项保持 None ⇒ 界面显示"未知"而不是"否"。
    return out


def start_exam(session, course: Dict[str, Any], exam: Dict[str, Any],
               exam_answer_id: str, code: str = "", user_id: str = "",
               captcha_validate: str = "") -> str:
    """② 开始考试 → 302，从 Location 里拿**动态 enc**。

    Returns:
        enc（答题期间所有请求都要带它）
    """
    params = {
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "examId": exam.get("exam_id") or exam.get("test_paper_id") or "",
        "source": 0,
        "examAnswerId": exam_answer_id,
        "cpi": course.get("cpi") or "",
        "keyboardDisplayRequiresUserAction": 1,
        "imei": _get_imei(),
        "faceDetection": 0,
        "facekey": "",
        "faceDetectionResult": "",
        "jt": 0,
        "code": code or "",
    }
    # 2026-09-27：人机验证（滑块）通过后 SDK 签发的票据，必须拼进 start 请求；
    # 没有就不带这个参数（空值会被平台当作无效票据）。
    if captcha_validate:
        params["captchavalidate"] = captcha_validate
    resp = session.get(API_EXAM_START, params=params, timeout=30,
                       allow_redirects=False, headers=mobile_headers(user_id))
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, "html.parser")
        t = soup.select_one("p.blankTips,li.msg")
        raise ExamError(f"开始考试失败：{t.get_text(' ', strip=True) if t else '未知原因'}")
    if resp.status_code != 302:
        raise ExamError(f"开始考试 HTTP {resp.status_code}")
    loc = resp.headers.get("Location", "")
    # 2026-09-27：曾用它定位"提交被拒"的根因（Referer/uid），现降为 debug
    # —— 正常运行时无价值，出问题时打开 debug 即可看到平台给了哪些参数。
    logger.debug(f"[考试诊断] start 的 302 Location：{loc[:500]}")
    m = re.search(r"[?&]enc=([0-9a-zA-Z]{16,})", loc)
    if not m:
        raise ExamError(f"302 里没有 enc：{loc[:160]}")
    logger.info(f"开始考试成功，拿到 enc={m.group(1)[:8]}…")
    return m.group(1)


def fetch_exam_question_page(session, course: Dict[str, Any], exam: Dict[str, Any],
                             exam_answer_id: str, index: int, enc: str,
                             last_update_time: int = 0, enc_remain_time: int = 0,
                             user_id: str = "") -> Dict[str, Any]:
    """③ 逐题拉取（`start=index` 翻页，index 从 0 开始）。"""
    params = {
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "tId": exam.get("exam_id") or exam.get("test_paper_id") or "",
        "id": exam_answer_id,
        "source": 0,
        "p": 1,
        "start": index,                # 🔴 翻页靠它
        "isphone": "true",
        "tag": int(enc_remain_time == 0),
        "cpi": course.get("cpi") or "",
        "imei": _get_imei(),
        "enc": enc,
        "keyboardDisplayRequiresUserAction": 1,
        "monitorStatus": 0,
        "monitorOp": -1,
        "remainTimeParam": enc_remain_time,
        "relationAnswerLastUpdateTime": last_update_time,
    }
    resp = session.get(PAGE_EXAM_QUESTION, params=params, timeout=30,
                       headers=mobile_headers(user_id))
    resp.raise_for_status()
    # 2026-09-27：排查开关 —— 设环境变量 EXAM_DUMP_HTML=1 时把每题原始 HTML 存盘。
    # 起因：考试题目的 `options` 解析出来是空列表（题目结构变了），
    #       而作业/其他链路没有这个页面，必须留证据才好修。
    if os.environ.get("EXAM_DUMP_HTML"):
        try:
            _d = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "_exam_dump")
            os.makedirs(_d, exist_ok=True)
            with open(os.path.join(_d, f"q{index}.html"), "w",
                      encoding="utf-8") as _f:
                _f.write(resp.text)
        except Exception:
            pass
    parsed = parse_exam_page(resp.text)
    tip = parsed.get("blank_tip") or ""
    if tip:
        if tip == "考试已经提交":
            raise ExamSubmitted(tip)
        if tip in ("无权限访问！", "当前用户账号发生异常，无法进行考试",
                   "当前班级发生异常，无法进行考试"):
            raise ExamDenied(tip)
        # 🔴 2026-09-27 修 bug：**卷尾识别**。
        # 50 题答完后请求第 51 题，平台不返回"没有更多题目"，而是回一句
        # `无效参数！`（越界响应）。旧代码把它一律当异常抛出 → 主循环判成
        # "作答中断"→ **直接返回且跳过交卷**（实测：50 题全答完、日志却停在
        # "作答中断：拉取第 51 题失败：无效参数！"，成绩从未提交）。
        # 这里把「已答过题 + 越界」归一为"已到卷尾"，返回空题目让主循环
        # 正常 break 并进入交卷；第 1 题就报错则仍按真异常处理。
        if index > 0 and ("无效参数" in tip or "参数" in tip):
            logger.info(f"第 {index + 1} 题越界（{tip}）→ 判定已到卷尾，转交卷")
            return {"questions": [], "form": {}, "blank_tip": ""}
        raise ExamError(f"拉取第 {index + 1} 题失败：{tip}")
    return parsed


def build_answer_form(question: Dict[str, Any], answer) -> Dict[str, Any]:
    """构造单题答题表单（字段名与题型一一对应，与 CxKitty 完全一致）。

    - 单选/判断：`answer{qid}`
    - 多选：`answers{qid}`
    - 填空：`answer{qid}1` `answer{qid}2` … + `blankNum{qid}` = "1,2,"
    """
    qid = question["id"]
    qtype = question["type"]
    form = {
        f"type{qid}": qtype,
        "questionId": qid,
        f"typeName{qid}": _QT_NAME.get(qtype, "其它"),
        "hidetext": "",
    }
    if qtype == QT_SINGLE:
        form[f"answer{qid}"] = _as_choice(answer, question)
    elif qtype == QT_MULTIPLE:
        form[f"answers{qid}"] = _as_choice(answer, question, multiple=True)
    elif qtype == QT_JUDGE:
        form[f"answer{qid}"] = _as_judge(answer)
    elif qtype == QT_BLANK:
        blanks = _as_blanks(answer)
        blank_num = ""
        for i, v in enumerate(blanks, 1):
            form[f"answer{qid}{i}"] = v
            blank_num += f"{i},"
        form[f"blankNum{qid}"] = blank_num
    elif qtype in (QT_SHORT, QT_NOUN, QT_ESSAY, QT_CALC, QT_OTHER, QT_ENTRY):
        # 🔴 2026-11-07（何同学实测免疫学）：主观题原先直接 `raise` ⇒ 该题提交被
        #   平台拒绝 ⇒ 连错 3 题触发「提交通道异常，提前终止」⇒ **整卷未提交**
        #   （实测作业1 答到 203/222 卡死，submitted=false，分数不更新）。
        #   超星主观题用文本域 `answer{qid}` 提交，给占位内容即可保住整卷；
        #   主观题本就要老师判分，占位不影响客观题得分。
        form[f"answer{qid}"] = (str(answer or "").strip()
                                or "略（要点见课堂笔记）")
    elif qtype in (20, 21, 22):
        # 🔴 2026-11-07：题型 20/21/22 = B1/B2/C 型题（**共用备选答案**）。
        #   实测免疫学作业1：这 4 个题组在答案页里 `rightAnswerContent` **是空的**
        #   （老师没设标准答案），程序既拿不到答案、提交时又被 raise 拒绝
        #   ⇒ 连拒 3 次触发"提交通道异常"⇒ **整卷 220 题全白答、未提交**。
        #   这里按单选提交：有答案就用，没有就取第一个选项（5 选 1，期望 0.2 分），
        #   目的是**保住整卷**，不让 4 道题毁掉另外 218 道。
        form[f"answer{qid}"] = _as_choice(answer, question) or (
            ((question.get("options") or [{}])[0] or {}).get("value")
            or ((question.get("options") or [{}])[0] or {}).get("letter")
            or "A")
    else:
        # 🔴 2026-11-07：**未知题型不许再抛错**。
        #   本轮已经两次栽在同一个坑上（题型4、题型20 各让整卷 220 题白答）：
        #   只要 `raise`，该题提交就被拒，连拒 3 次 ⇒ 整卷未提交、分数不更新。
        #   超星题型码远不止 0~9/20~22（10 连线、11 排序、15 完形、18 阅读…），
        #   以后必然还会遇到。所以这里改成**兜底文本提交**：宁可这一题 0 分，
        #   也不能让它毁掉整卷。
        logger.warning("未知题型 %s（题 %s）→ 按文本兜底提交，避免整卷失败",
                       qtype, qid)
        form[f"answer{qid}"] = str(answer or "").strip() or "略"
    return form


def _as_choice(answer, question: Dict[str, Any], multiple: bool = False) -> str:
    """把答案归一化成**提交值**（span.num_option 的 data）。

    答案可能是：A/B/C（显示字母）或选项正文。统一映射到 data 值。
    """
    opts = question.get("options") or []
    raw = answer if isinstance(answer, str) else "".join(str(x) for x in (answer or []))
    s = (raw or "").strip()
    if not s:
        return ""
    # 期望的显示字母集合
    letters = re.findall(r"[A-Za-z]", s.upper())
    wanted = [x.upper() for x in s.upper() if x in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
    if not wanted:
        wanted = letters
    out = []
    # ① 按显示字母匹配 → 取 data（提交值）
    for w in dict.fromkeys(wanted):
        for o in opts:
            if not isinstance(o, dict):
                continue
            if (o.get("letter") or "").strip().upper() == w:
                out.append(o.get("value") or w)
                break
    # ② 答案若是**选项正文**（本身不含 A-Z），按正文匹配取 value。
    #    🔴 2026-09-27 修 bug：此前这段被写在 `for w in wanted` 的循环体里，
    #    而纯文本答案算出来的 `wanted` 是空列表 → 循环一次都不执行
    #    → **正文分支永远是死代码**，文本答案会被原样返回并直接提交给平台
    #    （平台只认选项的 data 值，等于交了个无效答案）。
    #    现在独立成段：仅在"没匹配到字母"且"答案不含字母"时才尝试。
    if not out and not wanted:
        for o in opts:
            if not isinstance(o, dict):
                continue
            t = str(o.get("text") or "")
            if t and (s in t or t in s):
                out.append(o.get("value") or o.get("letter") or "")
                break
    if not out:
        return s
    return "".join(out) if multiple else out[0]


def _as_judge(answer) -> str:
    """判断题 → "true" / "false"。"""
    s = str(answer or "").strip().lower()
    if s in ("true", "t", "对", "正确", "是", "√", "1", "yes", "y", "ri"):
        return "true"
    if s in ("false", "f", "错", "错误", "否", "×", "0", "no", "n", "wr"):
        return "false"
    return "true" if "对" in s or "正确" in s or "true" in s else "false"


def _as_blanks(answer) -> List[str]:
    """填空题 → 每个空一份文本。"""
    if isinstance(answer, (list, tuple)):
        return [str(x).strip() for x in answer]
    s = str(answer or "").strip()
    if "\n" in s:
        return [x.strip() for x in s.split("\n") if x.strip()]
    return [s]


def submit_exam_page(session, course: Dict[str, Any], exam: Dict[str, Any],
                     exam_answer_id: str, *, index: int = 0,
                     question: Optional[Dict[str, Any]] = None,
                     answer=None, enc: str = "", remain_time: int = 0,
                     enc_remain_time: int = 0, last_update_time: int = 0,
                     final: bool = False, user_id: str = "") -> Dict[str, Any]:
    """④ 提交（`tempSave=true` 暂存 / `false` 交卷），带风控签名。

    Returns:
        {"status": "success", "msg": ..., "_enc": 新enc, "_lut": ..., "_ert": ...}
    """
    x = random.randint(100, 1000)
    y = random.randint(100, 1000)
    qid = question["id"] if question else 0
    sig = get_exam_signature(uid=user_id or 0, qid=qid, x=x, y=y)
    params = {
        "classId": course.get("clazzId") or course.get("classId") or "",
        "courseId": course.get("courseId") or "",
        "cpi": course.get("cpi") or "",
        "testPaperId": exam.get("exam_id") or exam.get("test_paper_id") or "",
        "testUserRelationId": exam_answer_id,
        "tempSave": "false" if final else "true",
        "qid": qid or "",
        "version": 1,
        **sig,
    }
    data = {
        "courseId": course.get("courseId") or "",
        "testPaperId": exam.get("exam_id") or exam.get("test_paper_id") or "",
        "testUserRelationId": exam_answer_id,
        "classId": course.get("clazzId") or course.get("classId") or "",
        "type": 0,
        "isphone": "true",
        "imei": _get_imei(),
        "subCount": "",
        "remainTime": remain_time,
        "tempSave": "false" if final else "true",
        "timeOver": "false",
        "encRemainTime": enc_remain_time,
        "encLastUpdateTime": last_update_time,
        "enc": enc,
        "userId": user_id,
        "source": 0,
        "start": index,
        "enterPageTime": last_update_time,
        "monitorforcesubmit": 0,
        "answeredView": 0,
        "exitdtime": 0,
    }
    if question is not None and answer is not None:
        try:
            data.update(build_answer_form(question, answer))
        except ExamError as e:
            logger.warning(f"构造答题表单失败（{e}），跳过该题提交")
            return {"status": "skip", "msg": str(e)}

    # 🔴🔴 2026-09-27 找到「提交失败：无效操作」的真正根因：**缺 Referer**！
    # 实测对照（同一场考试、同样参数、只差请求头）：
    #   不带 Referer              → {"msg":"提交失败：无效操作"}
    #   Referer = 题目页 URL      → {"msg":"提交成功", "data":"..."}
    #   Referer = 封面页 URL      → {"msg":"提交失败：无效操作"}
    # 也就是说平台对 submitAnswer 做来源校验，**必须**是答题页自身的地址。
    # 这一条与 uid 缺一不可：uid 决定风控签名能否验过，Referer 决定来源是否被接受。
    _h = mobile_headers(user_id)
    _h["Referer"] = PAGE_EXAM_QUESTION
    resp = session.post(API_EXAM_SUBMIT, params=params, data=data, timeout=30,
                        headers=_h)
    resp.raise_for_status()
    try:
        j = resp.json()
    except Exception:
        return {"status": "fail", "msg": f"非 JSON 响应：{resp.text[:200]}"}

    if j.get("status") != "success":
        # 🔴 2026-09-27：交卷失败时把**完整响应与关键请求参数**落日志。
        # 起因：真机交卷收到 `提交失败：无效操作`，但只记 msg 完全无法定位
        # （平台对该场景没有更细的说明）。留下原始响应才能判断是参数缺失、
        # 还是"答题时间未达下限"、还是别的风控要求。
        if final:
            logger.warning(
                f"[交卷诊断] final=True 被拒 | msg={j.get('msg')!r} | "
                f"完整响应={str(j)[:600]} | "
                f"请求: enc={'有' if enc else '空'} "
                f"remainTime={remain_time} encRemainTime={enc_remain_time} "
                f"lastUpdateTime={last_update_time} "
                f"testPaperId={params.get('testPaperId')} "
                f"testUserRelationId={params.get('testUserRelationId')} "
                f"cpi={'有' if params.get('cpi') else '空'} "
                f"classId={params.get('classId')} courseId={params.get('courseId')}")
        return {"status": "fail", "msg": j.get("msg") or str(j)[:200]}
    # 暂存时后端会回新的风控三参数：lastUpdateTime|encRemainTime|enc
    if not final:
        seg = str(j.get("data") or "").split("|")
        if len(seg) >= 3:
            try:
                j["_lut"] = int(seg[0]); j["_ert"] = int(seg[1]); j["_enc"] = seg[2]
            except ValueError:
                pass
    return j


def fetch_exam_all(session, course: Dict[str, Any], exam: Dict[str, Any],
                   exam_answer_id: str, enc: str, remain_time: int = 0,
                   enc_remain_time: int = 0, last_update_time: int = 0,
                   user_id: str = "") -> List[Dict[str, Any]]:
    """整卷预览（一次拿全部题目，用于核对/兜底）。"""
    params = {
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "source": 0,
        "imei": _get_imei(),
        "start": 0,
        "cpi": course.get("cpi") or "",
        "examRelationId": exam.get("exam_id") or exam.get("test_paper_id") or "",
        "examRelationAnswerId": exam_answer_id,
        "monitorStatus": 0,
        "monitorOp": -1,
        "remainTimeParam": enc_remain_time,
        "relationAnswerLastUpdateTime": last_update_time,
        "enc": enc,
    }
    resp = session.get(PAGE_EXAM_PREVIEW, params=params, timeout=30,
                       headers=mobile_headers(user_id))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tip = soup.body.select_one("p.blankTips") if soup.body else None
    if tip is not None:
        raise ExamError(f"整卷预览失败：{tip.get_text(' ', strip=True)}")
    nodes = soup.select("div.questionWrap.singleQuesId.ans-cc-exam")
    return [q for q in (parse_exam_question(n) for n in nodes) if q]


def fetch_answer_sheet(session, course: Dict[str, Any], exam: Dict[str, Any],
                       exam_answer_id: str, enc: str, remain_time: int = 0,
                       enc_remain_time: int = 0, last_update_time: int = 0,
                       user_id: str = "") -> Dict[str, Dict[int, bool]]:
    """答题卡状态：{题型: {题号: 是否已答}}。"""
    params = {
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "source": 0,
        "start": 0,
        "cpi": course.get("cpi") or "",
        "examRelationId": exam.get("exam_id") or exam.get("test_paper_id") or "",
        "imei": _get_imei(),
        "examRelationAnswerId": exam_answer_id,
        "remainTimeParam": enc_remain_time,
        "relationAnswerLastUpdateTime": last_update_time,
        "enc": enc,
    }
    resp = session.get(API_EXAM_ANSWER_SHEET, params=params, timeout=30,
                       headers=mobile_headers(user_id))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    sheet: Dict[str, Dict[int, bool]] = {}
    for ul in soup.select("ul"):
        h4 = ul.select_one("h4.cardTit")
        if h4 is None:
            continue
        m = re.search(r"[一二三四五六七八九]\.\s*(\S+)", h4.get_text(" ", strip=True))
        name = m.group(1) if m else "未知"
        son: Dict[int, bool] = {}
        for li in ul.select("li"):
            try:
                idx = int(li.get("data"))
            except (TypeError, ValueError):
                continue
            son[idx] = "complated" in (li.get("class") or [])
        sheet[name] = son
    return sheet

# 状态词（页面筛选值 → 文本）
EXAM_STATUS_ALL = "0"
EXAM_STATUS_UNDONE = "1"
EXAM_STATUS_DONE = "2"

_DONE_WORDS = ("已完成", "已批阅", "已交卷", "已完成作答", "待批阅")


class ExamUnavailable(Exception):
    """该课程没有考试入口 / 未开放。"""


def fetch_course_tokens(session, course: Dict[str, Any]) -> Dict[str, str]:
    """走「课程中间页」拿考试所需令牌。

    Returns:
        {"enc": ..., "openc": ..., "cpi": ..., "clazzid": ..., "courseid": ...}
        失败返回 {}（不算错误，只是这门课没有考试入口）
    """
    cid = str(course.get("courseId") or "")
    cls = str(course.get("clazzId") or "")
    cpi = str(course.get("cpi") or "")
    if not (cid and cls):
        return {}

    params = {"courseid": cid, "clazzid": cls, "vc": "1",
              "cpi": cpi, "ismooc2": "1", "v": "2"}
    try:
        resp = session.get(PAGE_COURSE_MIDDLE, params=params, timeout=25,
                           allow_redirects=True)
    except Exception as e:
        logger.warning(f"课程中间页请求失败（{course.get('title')}）：{e}")
        return {}
    if resp.status_code != 200:
        logger.warning(f"课程中间页 HTTP {resp.status_code}"
                       f"（{course.get('title')}）")
        return {}

    body = resp.text
    # enc 优先从最终 URL 抠（最权威）
    enc = ""
    m = re.search(r"[?&]enc=([0-9a-zA-Z]{16,})", resp.url or "")
    if m:
        enc = m.group(1)
    if not enc:
        m = re.search(r'[?&]enc=([0-9a-zA-Z]{16,})', body)
        enc = m.group(1) if m else ""

    # openc 从隐藏 input 抠
    openc = ""
    m = re.search(r'id="openc"[^>]*value="([^"]*)"', body)
    if m:
        openc = m.group(1)
    if not openc:
        m = re.search(r'name="openc"[^>]*value="([^"]*)"', body)
        openc = m.group(1) if m else ""

    # 🔴 2026-09-27：cpi 未传入时，从中间页跳转后的 URL 里解析。
    #    用途：超星班（新建班级）不在任何课程列表接口里，只能靠「额外课程登记」
    #    提供 courseId+clazzId；此时没有 cpi，而 302 后的 Location 里正好带着它。
    if not cpi:
        m = re.search(r"[?&]cpi=(\d+)", resp.url or "") or \
            re.search(r"[?&]cpi=(\d+)", body)
        if m:
            cpi = m.group(1)

    # 页面里有没有「考试」tab（没 tab 说明这课没开考试）
    has_exam_tab = ("考试" in body) or ("exam-list" in body)

    return {"enc": enc, "openc": openc, "cpi": cpi,
            "clazzid": cls, "courseid": cid, "has_exam_tab": has_exam_tab}


def fetch_exam_list(session, course: Dict[str, Any],
                    tokens: Dict[str, str] = None) -> Dict[str, Any]:
    """取某门课程的考试列表。

    Returns:
        {"ok": bool, "params": {...}, "exams": [...], "empty": bool, "raw_len": n}
        · ok=False 表示接口没调通（区别于"没考试"）
        · empty=True 表示页面明确显示「暂无考试」
    """
    cid = str(course.get("courseId") or "")
    cls = str(course.get("clazzId") or "")
    cpi = str(course.get("cpi") or "")
    tk = tokens or fetch_course_tokens(session, course)
    if not tk:
        return {"ok": False, "msg": "拿不到课程令牌", "exams": [], "empty": True}

    # 额外课程（超星班等）没有 cpi → 用中间页解析出来的那个
    cpi = cpi or str(tk.get("cpi") or "")

    params = {"courseid": cid, "clazzid": cls, "cpi": cpi, "ut": "s",
              "enc": tk.get("enc", ""), "openc": tk.get("openc", "")}
    try:
        resp = session.get(PAGE_EXAM_LIST, params=params, timeout=25,
                           allow_redirects=True)
    except Exception as e:
        return {"ok": False, "msg": f"请求失败：{e}", "exams": [], "empty": True}

    body = resp.text or ""
    if "参数传递不正确" in body:
        return {"ok": False, "msg": "参数传递不正确（enc/openc 缺失）",
                "exams": [], "empty": True, "raw_len": len(body)}

    exams = parse_exam_list(body)
    return {"ok": True, "params": params, "exams": exams,
            "empty": len(exams) == 0, "raw_len": len(body)}


def parse_exam_list(html_text: str) -> List[Dict[str, Any]]:
    """解析考试列表页（2026-09-26 真机结构确认）。

    真实 DOM：
        div.bottomList > ul > li
          └ div[onclick="viewExamAnswer('paperId','relationId')"]
              └ div.right-content
                  ├ p.overHidden2.fl   ← 标题
                  └ p.status.fl        ← 状态（已过期 / 已完成 / 未完成 …）
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # 空态判定
    null_node = soup.find(class_="null-data")
    if null_node and "暂无考试" in null_node.get_text():
        return []

    out: List[Dict[str, Any]] = []

    # 页面级隐藏字段（提交时需要回传）
    hidden = {}
    for name in ("courseId", "classId", "cpi", "openc", "examEnc", "examsystem",
                 "testPaperId", "testUserRelationId", "isRetest", "fid",
                 "stuLoginName"):
        n = soup.find("input", {"id": name})
        if n is not None:
            hidden[name] = n.get("value", "")

    # ---- 主路径：真机确认的 bottomList 结构 ----
    for li in soup.select("div.bottomList ul li"):
        div = li.find("div", onclick=True)
        hd = div.get("onclick", "") if div else ""
        m = re.search(r"viewExamAnswer\('(\d+)'\s*,\s*'(\d+)'\)", hd)
        # 🔴 2026-09-27：新版班级（超星班等）用 **goTest(...)** 而非 viewExamAnswer(...)：
        #     goTest('courseId', paperId, 0, 'endTime', relationId, false, 'enc', '0')
        #     不解析它 → paperId/relationId 全空 → 被 is_exam_done 误判成「不可作答」，
        #     界面就会显示「可作答 0 场」，实际白白错过考试。
        g = None
        if (not m) and ("goTest" in hd):
            g = re.search(
                r"goTest\(\s*'?(\d+)'?\s*,\s*(\d+)\s*,\s*[^,]*,\s*'?([^',]*)'?\s*,"
                r"\s*(\d+)\s*,\s*[^,]*,\s*'([0-9a-zA-Z]*)'", hd)
        _paper = m.group(1) if m else (g.group(2) if g else "")
        _rel = m.group(2) if m else (g.group(4) if g else "")
        _enc = g.group(5) if g else ""
        _end = (g.group(3) or "").strip() if g else ""
        ttl = li.find("p", class_="overHidden2")
        st = li.find("p", class_="status")
        title = ttl.get_text(" ", strip=True) if ttl else ""
        status = st.get_text(" ", strip=True) if st else ""
        if not (title or m or g):
            continue
        node = {
            "title": title[:120],
            "status": status,
            "exam_time": "",
            "score": "",
            "url": hd[:400],
            "paper_id": _paper,
            "relation_id": _rel,
            "exam_enc": _enc,
            "test_paper_id": _paper,
            "test_user_relation_id": _rel,
            "exam_id": _paper,
            "expire_time": _end,
            "hidden": hidden,
            "raw": li.get_text(" ", strip=True)[:220],
        }
        # 时间 / 分数（有些学校会在 status 旁再挂一行）
        txt = node["raw"]
        tm = re.search(r"(\d{4}-\d{2}-\d{2}[ ]?\d{0,2}:?\d{0,2})", txt)
        node["exam_time"] = tm.group(1).strip() if tm else ""
        # 分数：先剔除「剩余 23小时48分钟」，否则 "48分" 会被误当成得分
        _t2 = re.sub(r"剩余\s*\d+\s*小时?\s*\d*\s*分?钟?", "", txt)
        sm = re.search(r"(\d+(?:\.\d+)?)\s*分(?!钟)", _t2)
        node["score"] = sm.group(1) if sm else ""
        # 🔴 2026-11-07：是否可重做。列表页里可重做的考试带「重考」按钮，
        #   不可重做的没有这个字样。次数只有交卷页才显示（"允许重考 N 次，
        #   已重考 M 次"），列表页拿不到，所以这里只给布尔标记。
        node["can_retest"] = ("重考" in txt)
        rm = re.search(r"允许重考\s*(\d+)\s*次.*?已重考\s*(\d+)\s*次", txt)
        if rm:
            node["retest_allowed"] = int(rm.group(1))
            node["retest_used"] = int(rm.group(2))
        out.append(node)

    if out:
        return out

    # ---- 兜底：结构变了时按通用 li 扫（保底不丢考试） ----
    container = soup.find(class_="has-content") or soup
    for li in container.find_all("li"):
        txt = li.get_text(" ", strip=True)
        if not txt:
            continue
        cls = " ".join(li.get("class") or [])
        if "page" in cls.lower() and "exam" not in cls.lower():
            continue
        node = {"title": "", "status": "", "exam_time": "", "score": "",
                "url": "", "paper_id": "", "relation_id": "",
                "exam_enc": "", "test_paper_id": "",
                "test_user_relation_id": "", "hidden": hidden,
                "raw": txt[:220]}
        cands = [x.get_text(" ", strip=True) for x in
                 li.find_all(["h3", "h4", "p", "strong", "span"])]
        cands = [c for c in cands if c and len(c) > 1]
        if cands:
            node["title"] = max(cands, key=len)[:120]
        for w in ("已完成", "未完成", "待批阅", "已批阅", "进行中", "已过期",
                  "未开始", "未提交", "已交卷"):
            if w in txt:
                node["status"] = w
                break
        for a in li.find_all(["a", "button", "div"]):
            for attr in ("onclick", "href", "data-url", "data"):
                v = a.get(attr)
                if v and ("exam" in str(v).lower() or "StartNew" in str(v)
                          or "goTest" in str(v)):
                    node["url"] = str(v)[:400]
                    mm = re.search(
                        r"viewExamAnswer\('(\d+)'\s*,\s*'(\d+)'\)", node["url"])
                    if mm:
                        node["paper_id"] = node["test_paper_id"] = mm.group(1)
                        node["relation_id"] = node["test_user_relation_id"] = mm.group(2)
                    else:
                        gg = re.search(
                            r"goTest\(\s*'?(\d+)'?\s*,\s*(\d+)\s*,\s*[^,]*,\s*'?([^',]*)'?\s*,"
                            r"\s*(\d+)\s*,\s*[^,]*,\s*'([0-9a-zA-Z]*)'", node["url"])
                        if gg:
                            node["paper_id"] = node["test_paper_id"] = gg.group(2)
                            node["relation_id"] = node["test_user_relation_id"] = gg.group(4)
                            node["exam_id"] = gg.group(2)
                            node["exam_enc"] = gg.group(5)
                            node["expire_time"] = (gg.group(3) or "").strip()
                    break
            if node["url"]:
                break
        if node["title"] or node["status"]:
            out.append(node)

    return out


def is_exam_done(exam: Dict[str, Any]) -> bool:
    """考试是否**不可作答**（已完成 / 已过期 / 机会已用完）。

    2026-09-26 真机补充：`relation_id == "0"` 表示**该学生没有作答记录**
    （机会未开放或已用完），配合「已过期」状态即为彻底做不了。
    实测 acc2/acc3 两个账号的考试全部命中此情形。
    """
    st = exam.get("status") or ""
    if any(w in st for w in _DONE_WORDS):
        return True
    if "已过期" in st or "已取消" in st or "已关闭" in st:
        return True
    # 只有「未完成」且 relationId 有效时才算真的可做
    rid = str(exam.get("relation_id") or exam.get("test_user_relation_id") or "")
    if rid in ("", "0"):
        return True
    return False


def can_take_exam(exam: Dict[str, Any]) -> bool:
    """这场考试**现在能不能进去作答**。

    必须同时满足：① 状态是未完成类；② relationId 有效（有作答记录/机会）。
    注意：考试由老师发布后才出现，且需在开放窗口内 —— 两者都满足才可进。
    """
    st = exam.get("status") or ""
    if any(w in st for w in _DONE_WORDS):
        return False
    if "已过期" in st or "已取消" in st or "已关闭" in st:
        return False
    rid = str(exam.get("relation_id") or exam.get("test_user_relation_id") or "")
    return rid not in ("", "0")


def fetch_exam_list_phone(session, course: Dict[str, Any],
                          user_id: str = "") -> List[Dict[str, Any]]:
    """手机端考试列表（`mooc1-api/exam/phone/task-list`）。

    🔴 这是**进考场必需的**——只有它给出 `enc_task`（考试校验 key），
    而 PC 的 exam-ans/mooc2 列表页没有这个字段。

    真机结构：`ul.nav > li[data]`，`data` 里带 `taskrefId` + `enc_task`。
    """
    params = {
        "courseId": course.get("courseId") or "",
        "classId": course.get("clazzId") or course.get("classId") or "",
        "cpi": course.get("cpi") or "",
    }
    resp = session.get(PAGE_EXAM_LIST_PHONE, params=params, timeout=30,
                       headers=mobile_headers(user_id))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    out: List[Dict[str, Any]] = []
    ul = soup.find("ul", {"class": "nav"})
    if ul is None:
        return out
    for li in ul.find_all("li"):
        data = li.get("data") or ""
        m_id = re.search(r"[?&]taskrefId=(\d+)", data)
        m_enc = re.search(r"[?&]enc_task=([^&]+)", data)
        if not m_id:
            continue
        title_node = li.find("p")
        status_node = li.find("span")
        expire_node = li.find("span", {"class": "fr"})
        out.append({
            "exam_id": m_id.group(1),
            "enc_task": m_enc.group(1) if m_enc else "",
            "title": (title_node.get_text(" ", strip=True) if title_node
                      else li.get_text(" ", strip=True))[:120],
            "status": status_node.get_text(" ", strip=True) if status_node else "",
            "expire_time": expire_node.get_text(" ", strip=True) if expire_node else "",
            "raw_data": data[:400],
            "source": "phone",
        })
    return out


def _try_solve_slide_captcha(session, cover_url: str) -> str:
    """考试前的人机验证（滑块）—— 用真浏览器 + touch 模拟自动通过（2026-09-27）。

    返回票据字符串（失败返回空串）。整个过程只打开封面页，**绝不触发开始考试**，
    所以失败也不会消耗作答机会。

    依赖 api/slide_captcha.py（playwright + opencv）；环境不具备时优雅返回空。
    """
    if not cover_url:
        return ""
    _solve = _avail = None
    try:
        from api.slide_captcha import solve as _solve, available as _avail
    except Exception:
        try:
            from .slide_captcha import solve as _solve, available as _avail
        except Exception as e:
            logger.warning(f"滑块验证模块不可用（跳过自动过验证）：{e}")
            return ""
    try:
        if not _avail():
            logger.warning("环境缺少 playwright / opencv-python，无法自动过滑块")
            return ""
    except Exception:
        return ""
    try:
        cookie = "; ".join(f"{k}={v}" for k, v in session.cookies.get_dict().items())
    except Exception:
        cookie = ""
    try:
        r = _solve(cover_url, cookie_header=cookie, tries=6, headless=True,
                   total_timeout=150.0)
    except Exception as e:
        logger.warning(f"自动过滑块异常：{type(e).__name__}: {e}")
        return ""
    if r.get("ok") and r.get("validate"):
        logger.info(f"🔐 自动过滑块成功（尝试 {r.get('tries')} 次）")
        return r["validate"]
    logger.warning(f"自动过滑块未成功：{r.get('msg')}")
    return ""


def run_exam(session, course: Dict[str, Any], exam: Dict[str, Any],
             answer_fn, *, user_id: str = "", code: str = "",
             max_questions: int = 300, auto_submit: bool = False,
             on_progress=None, _dump_fh=None) -> Dict[str, Any]:
    """完整跑一场考试：封面 → 开始 → 逐题取题作答并暂存 → 交卷。

    🔴 链路照搬 guowang23333/CxKitty（手机端协议），逐题用 `start=index` 翻页，
    每题作答后 `tempSave=true` 暂存，全部答完再 `tempSave=false` 交卷。

    Args:
        answer_fn: 回调，入参题目 dict，返回答案（None 表示没答案，跳过该题）
        auto_submit: True=答完直接交卷；False=只暂存不交卷（审核提交）。
                     ⚠️ 缺省 **False**（2026-10-12）：考试机会只有一次，
                     默认只暂存草稿，由用户自己在学习通核对后交卷。
        _dump_fh: 题目落盘文件句柄（由 `run_exam_with_dump` 传入）。
                  **本函数不负责关闭**，避免 18 处 return 漏关。
    Returns:
        {"ok": bool, "total": n, "answered": n, "skipped": n,
         "submitted": bool, "msg": str, "questions": [...]}
    """
    dump_fh = _dump_fh
    exam_id = exam.get("exam_id") or exam.get("test_paper_id") or ""
    enc_task = exam.get("enc_task") or ""
    if not exam_id or not enc_task:
        return {"ok": False, "msg": "缺少 exam_id 或 enc_task（需手机端列表）",
                "total": 0, "answered": 0, "skipped": 0, "submitted": False}

    # ① 封面页
    try:
        cover = fetch_exam_cover(session, course, exam_id, enc_task, user_id)
    except ExamSubmitted as e:
        return {"ok": True, "msg": f"已提交过：{e}（跳过）", "total": 0,
                "answered": 0, "skipped": 0, "submitted": True}
    except ExamUnavailable as e:
        return {"ok": True, "msg": f"不可进入：{e}（跳过）", "total": 0,
                "answered": 0, "skipped": 0, "submitted": False}
    exam_answer_id = cover["exam_answer_id"]
    if not exam_answer_id:
        return {"ok": False, "msg": "封面页拿不到 examAnswerId",
                "total": 0, "answered": 0, "skipped": 0, "submitted": False}
    if cover.get("face_need"):
        logger.warning("⚠️ 该考试要求人脸识别，本程序不处理，跳过")
        return {"ok": True, "msg": "要求人脸识别，跳过", "total": 0,
                "answered": 0, "skipped": 0, "submitted": False}
    # 🔴 2026-09-27：人机验证（滑块）。
    # 实测：这是超星**默认对所有考试开启**的（发布页没有开关），不是老师设的防作弊。
    # 现在改为「自动尝试通过」—— 用真浏览器 + touch 模拟过滑块，拿到 captchavalidate
    # 票据后拼进 start 请求。失败则优雅跳过（不消耗作答机会）。
    _cv = ""
    if cover.get("captcha_need"):
        logger.info("🔐 该考试需要人机验证（滑块），尝试自动通过…")
        _cv = _try_solve_slide_captcha(session, cover.get("cover_url") or "")
        if not _cv:
            logger.warning("⚠️ 自动过滑块未成功，跳过该考试（未消耗作答机会）")
            return {"ok": True,
                    "msg": "该考试需要「人机验证」（滑块），自动通过未成功 → 已跳过。"
                           "本场未消耗作答机会，可稍后重试。",
                    "total": 0, "answered": 0, "skipped": 0, "submitted": False,
                    "need_captcha": True}

    # ② 开始考试（拿动态 enc）
    try:
        enc = start_exam(session, course, exam, exam_answer_id, code, user_id,
                         captcha_validate=_cv)
    except ExamSubmitted as e:
        return {"ok": True, "msg": f"已提交过：{e}", "total": 0, "answered": 0,
                "skipped": 0, "submitted": True}
    except ExamError as e:
        return {"ok": False, "msg": f"开始考试失败：{e}", "total": 0,
                "answered": 0, "skipped": 0, "submitted": False}

    # ③ 逐题取题 + 作答 + 暂存
    total = 0
    answered = skipped = questions_done = 0
    _reject = 0          # 连续"暂存被拒"计数（用于提前终止必败流程）
    remain_time = enc_remain_time = last_update_time = 0
    try:
        while questions_done < max_questions:
            page = fetch_exam_question_page(
                session, course, exam, exam_answer_id, questions_done, enc,
                last_update_time, enc_remain_time, user_id)
            f = page.get("form") or {}
            enc = f.get("enc") or enc
            try:
                enc_remain_time = int(f.get("encRemainTime") or enc_remain_time or 0)
            except (TypeError, ValueError):
                pass
            try:
                remain_time = int(f.get("remainTime") or remain_time or 0)
            except (TypeError, ValueError):
                pass
            try:
                last_update_time = int(f.get("encLastUpdateTime") or last_update_time or 0)
            except (TypeError, ValueError):
                pass

            qs = page.get("questions") or []
            if not qs:
                logger.info(f"第 {questions_done + 1} 题解析不到（可能已到卷尾），结束")
                break
            q = qs[0]
            total += 1
            ans = None
            try:
                ans = answer_fn(q)
            except Exception as e:
                logger.warning(f"第 {questions_done + 1} 题 {q.get('id')} 作答异常：{e}")
            if ans is None or (isinstance(ans, str) and not ans.strip()):
                logger.warning(f"[考试] 第 {questions_done + 1} 题未获答案 -> "
                               f"{(q.get('title') or '')[:50]}")
                skipped += 1
            else:
                answered += 1

            r = submit_exam_page(
                session, course, exam, exam_answer_id, index=questions_done,
                question=q, answer=ans if (ans is not None) else "",
                enc=enc, remain_time=remain_time, enc_remain_time=enc_remain_time,
                last_update_time=last_update_time, final=False, user_id=user_id)
            if r.get("status") != "success":
                _jm = str(r.get("msg") or "")
                logger.warning(f"[考试] 第 {questions_done + 1} 题暂存被拒：{_jm}")
                # 🔴 2026-09-27：**必败情况提前终止**。
                # 平台已判"作答时间用完"时，后面每一题都会同样被拒 ——
                # 继续跑完 50 题纯属浪费（AI 调用 + 3 分钟），且会让人误以为
                # "答案已全部暂存"（实际平台一条都没收）。
                if "时间已用完" in _jm or "时间已到" in _jm:
                    return {"ok": False,
                            "msg": f"平台判定作答时间已用完，本题起全部无法提交"
                                   f"（已答 {answered} 题）：{_jm}",
                            "total": total, "answered": answered,
                            "skipped": skipped, "submitted": False,
                            "time_over": True}
                _reject += 1
                # 连续 3 题都被拒 → 通道异常（签名/参数/会话），立即停下
                if _reject >= 3:
                    return {"ok": False,
                            "msg": f"连续 {_reject} 题提交被平台拒绝（{_jm}）"
                                   f"→ 判定提交通道异常，提前终止",
                            "total": total, "answered": answered,
                            "skipped": skipped, "submitted": False,
                            "submit_rejected": True}
            else:
                _reject = 0
            if r.get("_enc"):
                enc = r["_enc"]
                last_update_time = r.get("_lut", last_update_time)
                enc_remain_time = r.get("_ert", enc_remain_time)

            # 🔴 2026-09-27：加逐题进度日志。
            # 起因：之前只在「未获答案/暂存被拒」时才打日志，一旦流程中断，
            # 完全看不出走到第几题、是卡住还是被外部重启（排查很痛苦）。
            logger.info(
                f"[考试] 第 {questions_done + 1} 题完成"
                f"（{'答:' + str(ans)[:24] if ans else '跳过'}）"
                f"｜累计 已答 {answered} / 跳过 {skipped}")

            if on_progress:
                try:
                    on_progress(questions_done + 1, q)
                except Exception:
                    pass
            # 🔴 2026-10-12：逐题落盘「题干 + 完整选项 + 程序最终答案」。
            #   为什么必须有：考试正确率此前**无法核算** —— 日志里只有程序选的
            #   字母，没有选项全文，想逐题核对就得"再进一次考场抓题页"，
            #   而反复 start 未交卷会被平台限制（10-12 已实证被卡死）。
            #   ⇒ 趁**本次已在考场内**把题面留下，是唯一零额外风险的取数时机。
            #   逐题追加写 JSONL：中途被限流/重启也不丢已完成部分。
            if dump_fh:
                try:
                    dump_fh.write(json.dumps({
                        "no": questions_done + 1,
                        "type": q.get("type"),
                        "title": q.get("title"),
                        "options": _norm_options(q.get("options")),
                        "answer": ans,
                    }, ensure_ascii=False) + "\n")
                    dump_fh.flush()
                except Exception:
                    pass
            questions_done += 1
            time.sleep(random.uniform(0.8, 1.6))
    except ExamSubmitted as e:
        return {"ok": True, "msg": f"考试已提交：{e}", "total": total,
                "answered": answered, "skipped": skipped, "submitted": True}
    except ExamDenied as e:
        return {"ok": False, "msg": f"无权限：{e}", "total": total,
                "answered": answered, "skipped": skipped, "submitted": False}
    except ExamError as e:
        return {"ok": False, "msg": f"作答中断：{e}", "total": total,
                "answered": answered, "skipped": skipped, "submitted": False}

    # ④ 交卷 / 只暂存
    if not auto_submit:
        return {"ok": True, "msg": f"已完成 {total} 题作答（未交卷，已暂存）",
                "total": total, "answered": answered, "skipped": skipped,
                "submitted": False}

    # 🔴 2026-09-27：兼容老师设置的「最短答题时间」。
    #    程序答题极快，可能远早于下限 → 平台拒收交卷请求。
    #    此时**等待后重试**（答案已逐题暂存，不会丢）；累计有上限，绝不无限等；
    #    其他失败原因一律走原逻辑立即返回，不改变既有行为。
    waited = 0.0
    while True:
        try:
            r = submit_exam_page(session, course, exam, exam_answer_id, index=0,
                                 question=None, answer=None, enc=enc,
                                 remain_time=remain_time, enc_remain_time=enc_remain_time,
                                 last_update_time=last_update_time, final=True,
                                 user_id=user_id)
        except Exception as e:
            return {"ok": False, "msg": f"交卷异常：{e}", "total": total,
                    "answered": answered, "skipped": skipped, "submitted": False}

        if r.get("status") == "success":
            break

        msg = str(r.get("msg") or "")
        if not (_is_too_early(msg) and waited < _SUBMIT_WAIT_MAX):
            # 其他失败原因，或已等满上限 → 原样返回，并说明答案已暂存
            return {"ok": False,
                    "msg": (f"交卷失败：{msg}"
                            f"（{total} 题答案已全部暂存，可到学习通手动交卷）"),
                    "total": total, "answered": answered, "skipped": skipped,
                    "submitted": False}

        gap = _next_wait_gap(msg, waited, remain_time)
        if gap <= 0:
            # 剩余考试时间不足以等到"允许交卷"的时刻 → 放弃并明确提示
            return {"ok": False,
                    "msg": (f"交卷失败：{msg}（剩余考试时间不足以等待，"
                            f"{total} 题答案已全部暂存，"
                            f"请尽快到学习通手动交卷）"),
                    "total": total, "answered": answered,
                    "skipped": skipped, "submitted": False}
        logger.warning(f"⏳ 平台未到可交卷时间（{msg}）→ 等待 {gap:.0f}s 后重试交卷"
                       f"（累计已等 {waited:.0f}s，上限 {_SUBMIT_WAIT_MAX:.0f}s）")
        time.sleep(gap)
        waited += gap

    return {"ok": True,
            "msg": (f"交卷成功，共 {total} 题（答 {answered} / 空 {skipped}）"
                    + (f"｜为凑最短答题时间等待 {waited:.0f}s" if waited else "")),
            "total": total, "answered": answered, "skipped": skipped,
            "submitted": True}


_LAST_PROGRESS: Dict[str, Any] = {"done": 0, "total": 0, "title": ""}


def get_exam_progress() -> Dict[str, Any]:
    """本场考试的实时进度（给 web_ui 的 `/api/exam/status` 显示用）。

    🔴 2026-10-13（何同学实测反馈）：考试进行中，界面**只显示刷课进度**
    （如 `56/56 100%`），完全看不出考试跑到第几题 ⇒ 无法判断"到底有没有在跑"。
    而 `run_exam` 本来就有 `on_progress(done, q)` 回调（每题调一次），
    只是调用方（`api/base.py` / `_launch_exam_only`）都没传 ⇒ 白有。
    ⇒ 这里统一兜一个实现，调用方**无需任何改动**。
    """
    return dict(_LAST_PROGRESS)


def run_exam_with_dump(*args, dump_path: str = "", **kwargs):
    """`run_exam` + 逐题落盘「题干 + 完整选项 + 程序最终答案」（2026-10-12）。

    为什么需要（真实动机）：
      考试正确率此前**无法核算** —— 日志里只有程序选的字母，没有选项全文；
      要逐题核对只能"再进一次考场抓题页"，而反复 `start` 未交卷会被平台
      限制（2026-10-12 已实证被卡死：任务 running 但日志停更）。
      ⇒ 趁**本次已在考场内**把题面留下，是唯一零额外风险的取数时机。

    与 `run_exam` 完全同源：只是多传一个文件句柄，**不改任何作答/提交行为**。
    落盘失败也不抛异常 —— 它是旁路，绝不能影响考试。
    """
    fh = None
    if dump_path:
        try:
            os.makedirs(os.path.dirname(dump_path) or ".", exist_ok=True)
            fh = open(dump_path, "w", encoding="utf-8")
        except OSError:
            logger.warning("题目落盘失败（不影响作答）：%s", dump_path)
    # 每场开始重置进度（一场一场连跑时不能串场）
    _LAST_PROGRESS.update({"done": 0, "total": 0, "title": ""})
    _user_cb = kwargs.get("on_progress")

    def _track(done, q=None):
        try:
            d = int(done)
            _LAST_PROGRESS["done"] = d
            if d > _LAST_PROGRESS["total"]:
                _LAST_PROGRESS["total"] = d
            _LAST_PROGRESS["title"] = str((q or {}).get("title") or "")[:40]
        except Exception:                                        # noqa: BLE001
            pass
        if _user_cb:
            try:
                _user_cb(done, q)
            except Exception:                                    # noqa: BLE001
                pass

    kwargs["on_progress"] = _track
    try:
        _res = run_exam(*args, _dump_fh=fh, **kwargs)
        # 跑完后用真实总数校正（total/answered 只有结束时才拿得到）
        try:
            _LAST_PROGRESS["total"] = int(
                _res.get("total") or _LAST_PROGRESS["total"] or 0)
            _LAST_PROGRESS["done"] = int(
                _res.get("answered") or _LAST_PROGRESS["done"] or 0)
        except Exception:                                        # noqa: BLE001
            pass
        return _res
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def scan_exams_all(session, courses: List[Dict[str, Any]],
                   progress=None) -> Dict[str, Any]:
    """扫描**全部课程**的考试清单。

    Returns:
        {"courses": [{"courseid","title","exams":[...],"note":..}],
         "total": n, "todo": n, "scanned": n, "failed": n}
    """
    out = []
    total = todo = failed = 0
    for i, c in enumerate(courses):
        title = c.get("title") or ""
        if progress:
            try:
                progress(i + 1, len(courses), title)
            except Exception:
                pass
        try:
            res = fetch_exam_list(session, c)
        except Exception as e:
            logger.warning(f"考试扫描异常（{title}）：{e}")
            failed += 1
            continue
        if not res.get("ok"):
            failed += 1
            # 额外课程（手动登记）即使是"扫不到"，也要留一行说明原因，
            # 否则用户会以为登记没生效
            if c.get("extra"):
                out.append({"courseid": str(c.get("courseId") or ""),
                            "clazzid": str(c.get("clazzId") or ""),
                            "title": title, "exams": [], "extra": True,
                            "note": res.get("msg") or "扫描失败"})
            continue
        exams = res.get("exams") or []
        if not exams:
            if c.get("extra"):
                out.append({"courseid": str(c.get("courseId") or ""),
                            "clazzid": str(c.get("clazzId") or ""),
                            "title": title, "exams": [], "extra": True,
                            "note": "该课程暂无考试"})
            continue
        for e in exams:
            total += 1
            if not is_exam_done(e):
                todo += 1
        out.append({"courseid": str(c.get("courseId") or ""),
                    "clazzid": str(c.get("clazzId") or ""),
                    "title": title, "exams": exams,
                    "extra": bool(c.get("extra"))})
    return {"courses": out, "total": total, "todo": todo,
            "scanned": len(courses), "failed": failed}
