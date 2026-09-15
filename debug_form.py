# -*- coding: utf-8 -*-
"""对比各题型在提交表单里的真实字段形态（用已保存的真实页面）。"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bs4 import BeautifulSoup

html = open("_live_ch1.html", encoding="utf-8").read()
soup = BeautifulSoup(html, "html.parser")
form = soup.find("form", id="form1") or soup.find(
    "form", action=re.compile("addStudentWorkNew"))

print("form action:", (form.get("action") or "")[:110])
print("form 里 input/textarea 总数:", len(form.find_all(["input", "textarea"])))
print()

CASES = [("405915533", "多选题"), ("405915530", "单选题"),
         ("405915537", "判断题"), ("405915536", "填空题")]

for qid, label in CASES:
    print(f"--- {label} {qid} ---")
    hits = []
    for el in form.find_all(["input", "textarea"]):
        nm = el.get("name") or ""
        if qid in nm or qid in (el.get("id") or ""):
            hits.append((el.name, el.get("type") or "", nm, el.get("value")))
    for name, typ, nm, val in hits:
        print(f"   <{name} type={typ!r}> name={nm!r} value={str(val)[:30]!r}")
    if not hits:
        print("   （表单里没有任何该题字段！）")
    print()

print("=== 表单里所有含 'answer' 的字段名（前 40 个）===")
allnames = [el.get("name") for el in form.find_all(["input", "textarea"])
            if el.get("name") and "answer" in el.get("name")]
for n in allnames[:40]:
    print("  ", n)
print(f"  ... 共 {len(allnames)} 个")

print()
print("=== 多选题选项的原始 HTML（判断答案字段怎么存）===")
d = None
for dd in soup.find_all("div", class_="singleQuesId"):
    if dd.get("data") == "405915533":
        d = dd
        break
if d:
    for ul in d.find_all("ul"):
        print(ul.prettify()[:900])
        break
