# -*- coding: utf-8 -*-
"""超星「教师端」模块：发布作业 / 发布考试。

## 来源（2026-09-26 破译）

浏览器网页端**没有**教师端入口（普通学生号登录看不到）。
但**学习通桌面客户端（Electron）有**，且把每次页面加载**明文写进日志**：

    %APPDATA%\\cxstudy\\logs\\<YYYYMMDD>\\cxstudy_main_<YYYYMMDD>.log
    行格式：[info] 页面加载完成，did-finish-load:url: <完整URL>

从中提取出 193 号在班级 HHHH 手动发作业的**全部 URL**，再拉对应 JS
（`module/work/js/createWork.js`、`work-publish.js`）得到接口与参数。

完整协议见 `D:\\work\\2026-09-16-16-08-02\\.workbuddy\\memory\\教师发布协议.md`。

## 发布作业链路

```
① 新建编辑器   GET  /mooc2-ans/work/goToWorkEditor?courseid=&clazzid=&mooc=1&workid=&built=1&from=
② 建草稿       POST /mooc2-ans/work/save-work   workStatus=-1  → {status, workid}
③ 配题         /mooc2-ans/qbank-ai/page  或  /mooc2-ans/qbank/smartimport
④ 保存带题     GET  /mooc2-ans/work/save-work   workStatus=0
⑤ 设置页       GET  /mooc2-ans/work/library/setting?...
⑥ 正式发布     POST /mooc2-ans/work/library/publish     ← 核心
```

⚠️ 所有域名走 `mooc2-ans.chaoxing.com`。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from .logger import logger

HOST = "https://mooc2-ans.chaoxing.com"

# 发布接口里「值可能是空串」的可选字段（原样保留，值用 ""）
_OPTIONAL_STR_FIELDS = (
    "viewTypeScore", "allowPaste", "knowTrueFalse",
    "randomSort", "randomOptions", "finishStandard", "blankobj",
    "completionIngoreCase", "multiHalfScore", "answerEmptyIsObj",
    "blankIgnoreComma", "evaluation", "evaluationStandard", "evaluationNum",
    "evaluationStartTime", "evaluationEndTime", "anonymousMarking",
    "anonymousMarkingResult", "teacherMarkScore", "studentMarkScore",
    "evalPartakeScore", "stuMarkNum", "selfMark", "limitReviewer",
    "removeMaxMinScore", "aiReview", "aiAutoMarkScore",
    "setMarkScore", "markScoreMin", "markScoreMax", "sortAvgScore",
    "randomNum", "randomType", "randomTypeSet", "randomCustomScore",
    "redoDifferQuestion", "redoDifferLibrary",
    "endNoticeTime", "intervalNotice",
    "allowDownloadAttachment", "notShowTeacherComment", "questionExplanation",
)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _add_days(s: str, days: int) -> str:
    """给 'YYYY-MM-DD HH:MM' 加天数。"""
    try:
        t = time.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return s
    ts = time.mktime(t) + days * 86400
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _to_int(v: Any, d: int = 0) -> int:
    try:
        return int(str(v).strip())
    except Exception:
        return d


class TeacherError(Exception):
    """教师端操作失败（带平台返回的 msg）。"""


# --------------------------------------------------------------------------
# 作业发布
# --------------------------------------------------------------------------
class WorkPublisher:
    """193 号（课程创建者）发布作业。

    用法::

        pub = WorkPublisher(cx)
        workid = pub.create_work(courseid, clazzid, cpi, title)
        pub.save_work(courseid, clazzid, cpi, workid, title)      # 保存带题
        pub.publish_work(courseid, cpi, workid, classid=clazzid,
                         start_time=..., end_time=..., ...)       # 正式发布
    """

    def __init__(self, cx):
        # cx 需具备 .session（requests.Session）与 .get/.post（或直接用 session）
        self.cx = cx

    # ---- 底层请求 ----
    def _session(self):
        s = getattr(self.cx, "session", None)
        if s is None:
            raise TeacherError("Chaoxing 实例没有 session")
        return s

    def _headers(self, referer: str = "") -> Dict[str, str]:
        h = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
        if referer:
            h["Referer"] = referer
        return h

    def _json_post(self, path: str, data: Dict[str, Any],
                   referer: str = "") -> Dict[str, Any]:
        s = self._session()
        url = HOST + path
        # ⚠️ SessionManager 把 timeout 硬编码进 partial（5s），显式传会被覆盖；
        #    这里不传 timeout，避免 TypeError。
        r = s.post(url, data=data, headers=self._headers(referer))
        txt = (r.text or "").strip()
        if not txt:
            raise TeacherError(f"{path} 返回空（HTTP {r.status_code}）")
        try:
            return json.loads(txt)
        except Exception:
            raise TeacherError(f"{path} 返回非 JSON：{txt[:200]}")

    def _json_get(self, path: str, params: Dict[str, Any],
                  referer: str = "") -> Dict[str, Any]:
        s = self._session()
        url = HOST + path
        r = s.get(url, params=params, headers=self._headers(referer))
        txt = (r.text or "").strip()
        if not txt:
            raise TeacherError(f"{path} 返回空（HTTP {r.status_code}）")
        try:
            return json.loads(txt)
        except Exception:
            raise TeacherError(f"{path} 返回非 JSON：{txt[:200]}")

    # ---- ① 新建编辑器页（预热，拿会话；失败不致命）----
    def open_editor(self, courseid: str, clazzid: str,
                    workid: str = "") -> str:
        s = self._session()
        url = (f"{HOST}/mooc2-ans/work/goToWorkEditor"
               f"?courseid={courseid}&clazzid={clazzid}&mooc=1"
               f"&workid={workid}&built=1&from=")
        try:
            r = s.get(url, headers=self._headers())
            return r.text or ""
        except Exception as e:      # noqa: BLE001
            logger.warning(f"打开作业编辑器失败（忽略）：{e}")
            return ""

    # ---- ② 建草稿 ----
    def create_work(self, courseid: str, clazzid: str, cpi: str,
                    title: str, grading: str = "0",
                    directoryid: str = "0",
                    question_group: str = "0") -> str:
        """创建空作业草稿，返回 workid（作业库 ID）。

        对应页面 `toImport()` 里的 POST /mooc2-ans/work/save-work
        """
        ref = (f"{HOST}/mooc2-ans/work/goToWorkEditor"
               f"?courseid={courseid}&clazzid={clazzid}&mooc=1&workid=&built=1&from=")
        d = self._json_post("/mooc2-ans/work/save-work", {
            "courseid": courseid, "clazzid": clazzid, "cpi": cpi,
            "title": title, "grading": grading, "workStatus": -1,
            "directoryid": directoryid, "questionGroup": question_group,
        }, referer=ref)
        if not d.get("status"):
            raise TeacherError(f"创建作业失败：{d.get('msg') or d}")
        wid = str(d.get("workid") or "")
        if not wid:
            raise TeacherError(f"创建作业未返回 workid：{d}")
        logger.info(f"作业草稿已创建：{title} → workid={wid}")
        return wid

    # ---- ④ 保存（带题，workStatus=0）----
    def save_work(self, courseid: str, clazzid: str, cpi: str,
                  workid: str, title: str, grading: str = "0",
                  directoryid: str = "0",
                  question_group: str = "0") -> Dict[str, Any]:
        """GET /mooc2-ans/work/save-work，workStatus=0（保存，未发布）。"""
        ref = (f"{HOST}/mooc2-ans/work/goToWorkEditor"
               f"?courseid={courseid}&clazzid={clazzid}&cpi={cpi}&workid={workid}")
        d = self._json_get("/mooc2-ans/work/save-work", {
            "courseid": courseid, "clazzid": clazzid, "cpi": cpi,
            "workid": workid, "title": title, "grading": grading,
            "directoryid": directoryid, "workStatus": 0,
            "questionGroup": question_group,
        }, referer=ref)
        if not d.get("status"):
            raise TeacherError(f"保存作业失败：{d.get('msg') or d}")
        logger.info(f"作业已保存（未发布）：workid={workid}")
        return d

    # ---- 发布前校验 ----
    def check_question(self, courseid: str, clazzid: str, cpi: str,
                       workid: str) -> Dict[str, Any]:
        """GET /mooc2-ans/work/check/question
        返回 status: 1=可发布 / 0=失败 / 2=无题 / 3=需确认
        """
        return self._json_get("/mooc2-ans/work/check/question", {
            "courseid": courseid, "clazzid": clazzid,
            "cpi": cpi, "workid": workid,
        })

    # ---- 读取设置页（拿默认值）----
    def open_setting(self, courseid: str, cpi: str, workid: str) -> str:
        """GET 发布设置页，返回 HTML（用于解析 curDate 等）。"""
        s = self._session()
        url = (f"{HOST}/mooc2-ans/work/library/setting"
               f"?courseid={courseid}&cpi={cpi}&workLibraryId={workid}"
               f"&fromCreate=1&from=")
        r = s.get(url, headers=self._headers())
        return r.text or ""

    # ==================================================================
    # ④ 加题（纯 HTTP，无需 Playwright）
    # ------------------------------------------------------------------
    # 2026-09-26 破译：抓 createWork.js 的 saveSingleQuestion/saveMultipleChoice
    # 得到 POST /mooc2-ans/work/toEditWorkQuestion 的完整字段。
    #
    # 流程（每个题型两步）：
    #   ① POST /mooc2-ans/work/questionInit
    #         courseid, clazzid, cpi, workid, relationid(=同类题 relationid 或 0),
    #         title, directoryid=0, grading=0, questionGroup=0,
    #         originType=<题型码>, courseQuestionTypeId=<题型码>
    #      → {"status":true,"workid","relationid","questionid","score"}
    #   ② POST /mooc2-ans/work/toEditWorkQuestion  （form-urlencoded）
    #         公共字段见 _build_question_form()
    #
    # 题型码（originType / type）：
    #   0=单选 1=多选 2=填空 3=判断 4=简答 5=名词解释 6=论述 7=计算
    #   8=其它 9=分录 11=连线 13=排序 14=完型填空 15=阅读理解 17=程序
    #   18=口语 19=听力 20=共用选项 21=测评 22=口语测评 23=钟表
    #   24=选词填空 26=写作
    #
    # 各题型字段差异（实测）：
    #   单选 : content + A/B/C/D + defAnswer=A            + answerForTF=true
    #   多选 : content + A/B/C/D + defAnswer=A&defAnswer=B&...（多个）
    #   判断 : content + answerForTF=true（**不传选项、不传 defAnswer**）
    #          ⚠️ 判断题真正的答案在 answerForTF，需再验；见 _answer_for_tf()
    #   填空 : content + 题空序号作为字段名（"1","2",...）+ blankSubject/newCreate
    #   简答 : content + （答案可选）
    # ==================================================================

    TYPE_NAMES = {
        0: "单选题", 1: "多选题", 2: "填空题", 3: "判断题", 4: "简答题",
        5: "名词解释", 6: "论述题", 7: "计算题", 8: "其它", 9: "分录题",
        11: "连线题", 13: "排序题", 14: "完型填空", 15: "阅读理解",
        17: "程序题", 18: "口语题", 19: "听力题", 20: "共用选项题",
        21: "测评题", 22: "口语测评题", 23: "钟表题", 24: "选词填空",
        26: "写作题",
    }

    # 编辑器页的隐藏字段（enc 等）——每次开会话取一次即可
    _edit_ctx: Optional[Dict[str, str]] = None

    def load_editor_context(self, courseid: str, clazzid: str,
                            workid: str, force: bool = False) -> Dict[str, str]:
        """GET 编辑器页并解析隐藏字段（enc / title / grading 等）。

        这些字段在提交单题时是**必填**的，但每个作业固定不变，
        所以缓存起来（force=True 可强制刷新）。
        """
        if self._edit_ctx and not force \
                and self._edit_ctx.get("workid") == workid:
            return self._edit_ctx
        s = self._session()
        url = (f"{HOST}/mooc2-ans/work/goToWorkEditor"
               f"?courseid={courseid}&clazzid={clazzid}&mooc=1"
               f"&workid={workid}&built=1&from=")
        r = s.get(url, headers=self._headers())
        html = r.text or ""
        if "workForm" not in html:
            raise TeacherError(
                f"编辑器页异常（len={len(html)}，找不到 workForm）——可能未登录或 workid 失效")

        def _hidden(fid: str) -> str:
            m = re.search(
                rf'id="{fid}"[^>]*name="{fid}"[^>]*value="([^"]*)"', html)
            if not m:
                m = re.search(
                    rf'name="{fid}"[^>]*id="{fid}"[^>]*value="([^"]*)"', html)
            return m.group(1) if m else ""

        ctx = {
            "workid": workid,
            "courseid": _hidden("courseid") or courseid,
            "clazzid": _hidden("clazzid") or clazzid,
            "cpi": _hidden("cpi"),
            "enc": _hidden("enc"),
            "title": _hidden("title"),
            "grading": _hidden("grading") or "0",
            "questionGroup": _hidden("questionGroup") or "0",
            "directoryid": _hidden("directoryid") or "0",
            "mooc": _hidden("mooc") or "1",
        }
        # title 是正常 input（非 hidden），写法不同，补一次
        if not ctx["title"]:
            m = re.search(r'id="title"[^>]*name="title"[^>]*value="([^"]*)"', html)
            if not m:
                m = re.search(r'name="title"[^>]*value="([^"]*)"', html)
            ctx["title"] = m.group(1) if m else ""
        if not ctx["enc"]:
            raise TeacherError("编辑器页没解析到 enc，无法加题")
        self._edit_ctx = ctx
        logger.info(f"编辑器上下文已加载：workid={workid} enc={ctx['enc'][:12]}…")
        return ctx

    def add_question(self, ctx: Dict[str, str], qtype: int,
                     stem: str, options: Optional[List[str]] = None,
                     answers: Optional[List[str]] = None,
                     score: float = 10.0,
                     analysis: str = "") -> Dict[str, Any]:
        """加一道题（纯 HTTP）。返回 questionInit 的回包（含 questionid）。

        :param qtype:   题型码（0=单选 1=多选 2=填空 3=判断 4=简答 …）
        :param stem:    题干（纯文本；内部会包成 <p>…</p>）
        :param options: 选项列表（单选/多选必填；判断/填空/简答传 None）
        :param answers: 正确答案：
                          · 单选 ['A']  多选 ['A','B','D']
                          · 判断 ['对'] / ['错']（会转成 answerForTF）
                          · 填空 ['北京','上海']（按题空顺序）
                          · 简答 [参考答案文本]（可空）
        :param score:   本题分值
        """
        ctx = ctx or {}
        workid = ctx.get("workid") or ""
        if not workid:
            raise TeacherError("add_question：ctx 缺少 workid")

        courseid = ctx.get("courseid") or ""
        clazzid = ctx.get("clazzid") or ""
        cpi = ctx.get("cpi") or ""
        title = ctx.get("title") or ""
        grading = ctx.get("grading") or "0"
        tname = self.TYPE_NAMES.get(int(qtype), f"题型{qtype}")

        # ---- ① questionInit：拿 questionid / workLibraryRelationid ----
        ref = (f"{HOST}/mooc2-ans/work/goToWorkEditor"
               f"?courseid={courseid}&clazzid={clazzid}&mooc=1&workid={workid}&built=1&from=")
        init = self._json_post("/mooc2-ans/work/questionInit", {
            "courseid": courseid, "clazzid": clazzid, "cpi": cpi,
            "workid": workid,
            "relationid": "0",          # 题型分组 ID；0 = 新建一组
            "title": title,
            "directoryid": ctx.get("directoryid") or "0",
            "grading": grading,
            "questionGroup": ctx.get("questionGroup") or "0",
            "originType": str(int(qtype)),
            "courseQuestionTypeId": str(int(qtype)),
        }, referer=ref)
        if not init.get("status"):
            raise TeacherError(f"questionInit 失败：{init.get('msg') or init}")
        qid = str(init.get("questionid") or "")
        relid = str(init.get("relationid") or "")
        if not qid:
            raise TeacherError(f"questionInit 未返回 questionid：{init}")

        # ---- ② toEditWorkQuestion：写题目内容 ----
        form = self._build_question_form(
            ctx, qid, relid, int(qtype), tname, stem,
            options=options, answers=answers, score=score, analysis=analysis)
        r = self._session().post(
            HOST + "/mooc2-ans/work/toEditWorkQuestion",
            data=form, headers=self._headers(ref))
        txt = (r.text or "").strip()
        try:
            res = json.loads(txt)
        except Exception:
            raise TeacherError(f"toEditWorkQuestion 返回非 JSON：{txt[:200]}")
        if not res.get("success"):
            raise TeacherError(
                f"保存题目失败（{tname}）：{res.get('msg') or res}")
        logger.info(f"已加题 [{tname}] qid={qid} 分值={score} 题干={stem[:24]}")
        return {"questionid": qid, "relationid": relid,
                "success": True, "raw": res}

    def _build_question_form(self, ctx: Dict[str, str], qid: str, relid: str,
                             qtype: int, tname: str, stem: str,
                             options: Optional[List[str]] = None,
                             answers: Optional[List[str]] = None,
                             score: float = 10.0,
                             analysis: str = "") -> List[tuple]:
        """按实测的字段顺序拼 toEditWorkQuestion 表单（list[tuple] 支持重复键）。"""
        def _p(t: str) -> str:
            return f"<p>{t}</p>"

        f: List[tuple] = [
            ("queNumMaxSize", ""),
            ("courseid", ctx.get("courseid") or ""),
            ("clazzid", ctx.get("clazzid") or ""),
            ("chapterId", "0"),
            ("fromChapter", "0"),
            ("workid", ctx.get("workid") or ""),
            ("directoryid", ctx.get("directoryid") or "0"),
            ("cpi", ctx.get("cpi") or ""),
            ("enc", ctx.get("enc") or ""),
            ("grading", ctx.get("grading") or "0"),
            ("questionGroup", ctx.get("questionGroup") or "0"),
            ("mooc", ctx.get("mooc") or "1"),
            ("questionid", qid),
            ("workLibraryRelationid", relid),
            ("type", str(int(qtype))),
            ("typeName", tname),
            ("courseQuestionTypeId", str(int(qtype))),
            ("wrapAnswer", ""),
            ("answerForTF", "true"),
            ("title", ctx.get("title") or ""),
            ("updateSignal", "true"),
            ("score", str(score)),
            ("content", _p(stem)),
        ]

        answers = answers or []
        if qtype in (0, 1):                      # 单选 / 多选
            opts = options or []
            letters = "ABCDEFGH"
            # 选项与 defAnswer 交替排列（实测：A, defAnswer(A), B, defAnswer(B) …）
            for i, opt in enumerate(opts):
                L = letters[i]
                f.append((L, _p(opt)))
                if L in answers:
                    f.append(("defAnswer", L))
            f += [("answerAnalysis", analysis), ("difficulty", "0.8"),
                  ("easy", "0"), ("topicId", "0"),
                  ("schoolTopicIds", ""), ("labelIdArr", "")]
            return f

        if qtype == 3:                            # 判断题：不传选项/defAnswer
            ans = (answers[0] if answers else "对")
            is_true = str(ans).strip().lower() in ("对", "正确", "是", "true", "t", "yes", "√", "a")
            # 覆盖前面默认的 answerForTF=true
            f = [(k, v) for (k, v) in f if k != "answerForTF"]
            f.append(("answerForTF", "true" if is_true else "false"))
            f += [("answerAnalysis", analysis), ("difficulty", "0.8"),
                  ("easy", "0"), ("topicId", "0"),
                  ("schoolTopicIds", ""), ("labelIdArr", "")]
            return f

        if qtype == 2:                            # 填空题
            # 🔴🔴 2026-09-26 定论（读 createWork.js 的 saveBlankQuestion()）：
            #    填空题**不用**「字段名=题空序号」，而是把所有空的答案
            #    用 **★（U+2605）** 拼成一个字符串，塞进**单字段 `wrapAnswer`**：
            #
            #        answerArr = answerArr.join("★");
            #        $("#wrapAnswer").val(answerArr);
            #
            #    （`#wrapAnswer` 就是 #workForm 里的隐藏域，跟着
            #      $("#workForm").serialize() 一起提交）
            #
            #    之前写成 ("1", "<p>北京</p>")、("2","<p>上海</p>") → 平台读不到
            #    → check/question 报 status=3「填空题第1题未设置正确答案」。
            #    值本身仍是 UEditor HTML（<p>答案</p>）。
            parts = []
            for a in (answers or []):
                s = str(a)
                # 平台自己会给每个空包一层 <p>，所以这里传**纯文本**最干净
                # （传 <p>x</p> 会变成 <p><p>x</p></p> 双层）
                if s.lstrip().startswith("<"):
                    s = re.sub(r"^\s*<p>|</p>\s*$", "", s.strip())
                parts.append(s)
            # 就地替换基础字段里的空 wrapAnswer（保持字段顺序）
            f = [("wrapAnswer", "★".join(parts)) if k == "wrapAnswer" else (k, v)
                 for (k, v) in f]
            f += [("newCreate", "1"), ("blankSubject", "0"),
                  ("answerAnalysis", analysis), ("difficulty", "0.8"),
                  ("easy", "0"), ("topicId", "0"),
                  ("schoolTopicIds", ""), ("labelIdArr", "")]
            return f

        # 简答 / 其它：只题干（+可选参考答案，同样走 UEditor → 包 <p>）
        if answers:
            s = str(answers[0])
            f.append(("1", s if s.lstrip().startswith("<") else _p(s)))
        f += [("answerAnalysis", analysis), ("difficulty", "0.8"),
              ("easy", "0"), ("topicId", "0"),
              ("schoolTopicIds", ""), ("labelIdArr", "")]
        return f

    def add_questions(self, ctx: Dict[str, str],
                      questions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量加题。questions = [{'type':0,'stem':…,'options':[…],
                                   'answers':[…],'score':10,'analysis':''}, …]"""
        ok, fail = [], []
        for i, q in enumerate(questions, 1):
            try:
                r = self.add_question(
                    ctx, q["type"], q["stem"],
                    options=q.get("options"), answers=q.get("answers"),
                    score=float(q.get("score", 10)),
                    analysis=q.get("analysis", ""))
                ok.append({"idx": i, "type": q["type"], **r})
            except Exception as e:               # noqa: BLE001
                logger.warning(f"第 {i} 题添加失败：{e}")
                fail.append({"idx": i, "type": q.get("type"),
                             "stem": str(q.get("stem"))[:30], "err": str(e)})
        logger.info(f"批量加题完成：成功 {len(ok)} / 失败 {len(fail)}")
        return {"ok": ok, "fail": fail,
                "ok_n": len(ok), "fail_n": len(fail)}


    @staticmethod
    def parse_cur_date(html: str) -> str:
        m = re.search(r'id="curDate"[^>]*value="([^"]*)"', html or "")
        return m.group(1) if m else ""

    # ---- ⑥ 正式发布 ----
    def publish_work(self, courseid: str, cpi: str, workid: str,
                     classid: str,
                     start_time: str = "",
                     end_time: str = "",
                     passing_standard: str = "60",
                     redo_times: str = "0",
                     redo_highest_score: str = "0",
                     answer_after_end: str = "0",
                     allow_answer: str = "1",
                     not_show_last_answer: str = "1",
                     publish_type: int = 0,
                     persons: Optional[List[int]] = None,
                     groups: Optional[List[int]] = None,
                     extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """POST /mooc2-ans/work/library/publish —— 正式发布作业。

        :param classid: 班级 ID（HHHH = 155927249）
        :param start_time/end_time: 'YYYY-MM-DD HH:MM'；空则默认 now ~ now+7天
        :param publish_type: 0=全班 / 1=指定人 / 2=指定组
        :param redo_highest_score: '1'=取最高分（安全）；'0'=取本次（会覆盖）
        """
        if not start_time:
            start_time = _now_str()
        if not end_time:
            end_time = _add_days(start_time, 7)

        item: Dict[str, Any] = {
            "classid": _to_int(classid),
            "publishType": int(publish_type),
        }
        if publish_type == 1:
            item["persons"] = list(persons or [])
            item["groups"] = []
        elif publish_type == 2:
            item["groups"] = list(groups or [])
            item["persons"] = []

        data: Dict[str, Any] = {
            "courseid": courseid,
            "workLibraryId": workid,
            "cpi": cpi,
            "publishObj": json.dumps([item], ensure_ascii=False),
            "startTime": start_time,
            "endTime": end_time,
            "answerAfterEnd": answer_after_end,
            "answerAfterEndDeadline": "",
            "passingStandard": passing_standard,
            "redoTimes": redo_times,
            "redoHighestScore": redo_highest_score,
            "allowAnswer": allow_answer,
            "notShowLastAnswer": not_show_last_answer,
            "teachPlan": "0",
            "saveAsTemplate": "0",
            "limitReviewerObj": json.dumps([]),
        }
        for k in _OPTIONAL_STR_FIELDS:
            data.setdefault(k, "")
        if extra:
            data.update(extra)

        ref = (f"{HOST}/mooc2-ans/work/library/setting"
               f"?courseid={courseid}&cpi={cpi}&workLibraryId={workid}"
               f"&fromCreate=1&from=")
        d = self._json_post("/mooc2-ans/work/library/publish", data, referer=ref)
        if not d.get("status"):
            raise TeacherError(f"发布作业失败：{d.get('msg') or d}")
        logger.info(f"作业已发布：workid={workid} {start_time} ~ {end_time}")
        return d

    # ---- 删除 ----
    def delete_work(self, courseid: str, cpi: str, workid: str) -> Dict[str, Any]:
        """GET /mooc2-ans/work/library/work-update  status=2（进回收站）。"""
        return self._json_get("/mooc2-ans/work/library/work-update", {
            "courseid": courseid, "workid": workid, "cpi": cpi, "status": 2,
        })


# --------------------------------------------------------------------------
# 一站式：发一份作业
# --------------------------------------------------------------------------
def publish_homework(cx, courseid: str, clazzid: str, cpi: str,
                     title: str,
                     start_time: str = "", end_time: str = "",
                     passing_standard: str = "60",
                     redo_times: str = "0",
                     redo_highest_score: str = "0") -> Dict[str, Any]:
    """一条龙：建草稿 → 保存 → 发布。

    ⚠️ 中间「配题」这一步（qbank-ai / smartimport）需要题目内容，
    本函数**不自动配题**；调用方应先建草稿配好题，或用
    :func:`create_draft` 只建草稿。
    """
    pub = WorkPublisher(cx)
    pub.open_editor(courseid, clazzid)
    workid = pub.create_work(courseid, clazzid, cpi, title)
    pub.save_work(courseid, clazzid, cpi, workid, title)
    return pub.publish_work(
        courseid, cpi, workid, clazzid,
        start_time=start_time, end_time=end_time,
        passing_standard=passing_standard,
        redo_times=redo_times,
        redo_highest_score=redo_highest_score,
    )


def create_draft(cx, courseid: str, clazzid: str, cpi: str,
                 title: str) -> str:
    """只建作业草稿，返回 workid（供手动/程序配题后自行发布）。"""
    pub = WorkPublisher(cx)
    pub.open_editor(courseid, clazzid)
    return pub.create_work(courseid, clazzid, cpi, title)
