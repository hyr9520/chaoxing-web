# -*- coding: utf-8 -*-
"""下载所有听力题音频，并建立"题组 ↔ 音频"对应表（用户需求：先转文字再作答）。

流程（2026-09-11 打通）：
  题目页 HTML 里音频以 <span data="{objectId}" type="mp3" name="xxx.mp3"> 形式给出；
  用 /mooc-ans/ueditorupload/read?objectId=...&fileOriName=... 换取带签名的
  d0.cldisk.com 临时直链，再下载 mp3。

产出：
  audio/*.mp3                音频文件
  audio_index.json           [{title, file, objectId, name, chapter}]
"""
import html as H
import json
import os
import re
import sys

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

import web_ui
from api.base import SessionManager
from api.learned import normalize_title
from bs4 import BeautifulSoup

AUDIO_DIR = "audio"
INDEX = "audio_index.json"
ONLY = sys.argv[1] if len(sys.argv) > 1 else None      # 可指定只跑某章节前缀，如 "3.2"

os.makedirs(AUDIO_DIR, exist_ok=True)

cfg = web_ui.load_ui_config()
_ACC = os.environ.get("CK_ACCOUNT", "")   # 账号不写死（避免随仓库外泄）
_matches = [p for p in cfg["profiles"] if p.get("username") == _ACC]
if not _matches:
    raise SystemExit("请用环境变量 CK_ACCOUNT 指定学习通账号（手机号）")
prof = _matches[0]
ok, msg, courses = web_ui._do_login(prof)
print("登录:", ok, flush=True)
cx = web_ui.state["chaoxing"]
s = SessionManager.get_session()
s.headers.update({"Referer": "https://mooc1.chaoxing.com/"})
c = [x for x in courses if x["courseId"] == "262671420"][0]
pts = cx.get_course_point(c["courseId"], c["clazzId"], c["cpi"])["points"]
print("章节点:", len(pts), flush=True)


def norm(t: str) -> str:
    """题目页标题规范化，必须与运行时 query_all 的键完全一致。

    坑（2026-09-11 踩过）：div.Zy_TItle 的 get_text() 开头是换行／空格再跟题号
    （如 "\\n1\\n【听力题】..."），直接 re.sub(r"^\\d+") 去不掉数字 —— 键会变成
    "1 【听力题】..."，而运行时查询的键是 "【听力题】...", 两者对不上，
    写进去的答案永远命中不到。必须先压空白再去数字。
    """
    t = normalize_title(t)                 # 先压空白、去首尾
    t = re.sub(r"^\d+[\s\.、]*", "", t)     # 再去开头的题号
    t = re.sub(r"（\d+\.\d+分）$", "", t)
    return normalize_title(t)


def parse_groups_with_audio(html_text):
    """解析题目页：返回 [{title, audios:[{objectId,name}]}]"""
    soup = BeautifulSoup(html_text, "html.parser")
    out = []
    for g in soup.find_all("div"):
        if "singleQuesId" not in " ".join(g.get("class") or []):
            continue
        t_el = g.find("div", class_="Zy_TItle") or g.find(class_="newZy_TItle")
        title = norm(t_el.get_text() if t_el is not None else "")
        audios = []
        for sp in g.find_all("span"):
            if (sp.get("type") or "").lower() == "mp3" and sp.get("data"):
                audios.append({"objectId": sp["data"],
                               "name": sp.get("name") or "audio.mp3"})
        out.append({"title": title, "audios": audios})
    return out


def signed_url(oid, name):
    u = (f"https://mooc1.chaoxing.com/mooc-ans/ueditorupload/read"
         f"?objectId={oid}&fileOriName={name}")
    r = s.get(u, timeout=40)
    body = H.unescape(r.text)
    m = re.search(r"https?://[\w\.\-]+/download/[^\"'<>\s]+", body)
    return m.group(0) if m else None


index = []
seen = set()
for p in pts:
    if not re.match(r"^\d+\.", p["title"]):
        continue
    if ONLY and not p["title"].startswith(ONLY):
        continue
    job = job_info = None
    for num in range(3):
        pr = {"clazzid": c["clazzId"], "courseid": c["courseId"], "knowledgeid": p["id"],
              "ut": "s", "cpi": c["cpi"], "v": "2025-0424-1038-3", "mooc2": 1, "num": str(num)}
        try:
            r = s.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards",
                      params=pr, timeout=20)
        except Exception:
            break
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
        continue
    try:
        h = s.get("https://mooc1.chaoxing.com/mooc-ans/api/work", params={
            "api": "1", "workId": job["jobid"].replace("work-", ""), "jobid": job["jobid"],
            "originJobId": job["jobid"], "needRedirect": "true", "skipHeader": "true",
            "knowledgeid": str(job_info["knowledgeid"]), "ktoken": job_info["ktoken"],
            "cpi": job_info["cpi"], "ut": "s", "clazzId": c["clazzId"], "type": "",
            "enc": job.get("enc", ""), "mooc2": "1", "courseid": c["courseId"]},
            timeout=25).text
    except Exception:
        continue
    groups = parse_groups_with_audio(h)
    got = 0
    for g in groups:
        for au in g["audios"]:
            key = au["objectId"]
            if key in seen:
                continue
            seen.add(key)
            out_name = f"{key}_{re.sub(r'[^\\w\\.-]', '_', au['name'])}"
            path = os.path.join(AUDIO_DIR, out_name)
            if not os.path.exists(path) or os.path.getsize(path) < 10000:
                u = signed_url(au["objectId"], au["name"])
                if not u:
                    print(f"   [无签名链接] {au['name']}", flush=True)
                    continue
                try:
                    rr = s.get(u, timeout=120, allow_redirects=True)
                    body = rr.content
                    # mp3 帧头有多种（fff3/fff2/fff b/ID3），不要漏判；同时排除 HTML 页
                    head = body[:15].lstrip().lower()
                    is_html = head.startswith(b"<") or b"<!doctype" in head
                    is_mp3 = (not is_html) and len(body) > 50000
                    if is_mp3:
                        open(path, "wb").write(body)
                        got += 1
                    else:
                        print(f"   [下载异常] {au['name']} {rr.status_code} "
                              f"{len(body)}B", flush=True)
                        continue
                except Exception as e:
                    print(f"   [下载失败] {au['name']} {type(e).__name__}", flush=True)
                    continue
            index.append({"title": g["title"], "file": path, "objectId": au["objectId"],
                          "name": au["name"], "chapter": p["title"]})
    if got or groups:
        print(f"  {p['title'][:40]}: 题组 {len(groups)} / 新下载 {got}", flush=True)

json.dump(index, open(INDEX, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\n完成：音频 {len([f for f in os.listdir(AUDIO_DIR) if f.endswith('.mp3')])} 个"
      f" | 索引 {len(index)} 条 -> {INDEX}", flush=True)
