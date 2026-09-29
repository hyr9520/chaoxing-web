# -*- coding: utf-8 -*-
"""「额外课程」登记（2026-09-27）。

## 为什么需要它

超星「超星班」等**新建班级的课程，不在任何课程列表接口里**——实测
`/api/courses`(get_course_list) / `visit/courselistdata`(courseType 0/1/2/3)
/ `stu-work` 全量作业页 / `i.chaoxing.com` **全都看不到它**。

但用户**确实是它的成员**：只要给出 `courseId + clazzId`，
`visit/stucoursemiddle` 就会 302 成功（Location 里带该用户在此课的 `cpi`），
进而能打开考试列表页、能答题。

## 做法

把这类课程登记进 `{data_dir}/extra_courses.json`，考试扫描时自动并入课程源。
**cpi 不需要用户填** —— 扫描时从课程中间页的 302 地址里解析（见 api/exam.py）。
"""
import json
import logging
import os

from api.config import data_dir

logger = logging.getLogger(__name__)
_FILE = "extra_courses.json"


def _path() -> str:
    return os.path.join(data_dir(), _FILE)


def load() -> list:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        return [it for it in d if isinstance(it, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def save(items: list) -> None:
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
    except Exception as e:
        logger.warning(f"写 extra_courses.json 失败：{e}")


def add(courseid: str, clazzid: str, title: str = "") -> dict:
    cid = str(courseid or "").strip()
    cls = str(clazzid or "").strip()
    if not cid:
        return {"ok": False, "msg": "courseId 不能为空", "items": load()}
    if not cls:
        return {"ok": False, "msg": "clazzId 不能为空", "items": load()}
    items = load()
    for it in items:
        if str(it.get("courseid")) == cid and str(it.get("clazzid")) == cls:
            if title:
                it["title"] = title
            save(items)
            return {"ok": True, "msg": "该课程已在列表中（名称已更新）", "items": items}
    items.append({"courseid": cid, "clazzid": cls,
                  "title": (title or "").strip() or f"额外课程 {cid}"})
    save(items)
    return {"ok": True, "msg": "已添加", "items": items}


def remove(courseid: str, clazzid: str = "") -> dict:
    cid = str(courseid or "").strip()
    cls = str(clazzid or "").strip()
    items = [it for it in load()
             if not (str(it.get("courseid")) == cid
                     and (not cls or str(it.get("clazzid")) == cls))]
    save(items)
    return {"ok": True, "msg": "已删除", "items": items}


def as_courses(extra: list = None) -> list:
    """转成考试扫描用的课程 dict（`cpi` 留空 → 扫描时自动解析）。"""
    out = []
    for it in (extra if extra is not None else load()):
        cid = str(it.get("courseid") or "").strip()
        cls = str(it.get("clazzid") or "").strip()
        if not (cid and cls):
            continue
        out.append({"courseId": cid, "clazzId": cls, "cpi": "",
                    "title": it.get("title") or f"额外课程 {cid}",
                    "extra": True})
    return out
