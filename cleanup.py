# -*- coding: utf-8 -*-
"""清理残留：音频改名可读、备份归档、删除测试与无主文件。

保留（有用的资产）：
  audio/            听力音频（已下载，可复用）
  models/fw-base/   Whisper 模型（可复用）
  transcripts.json  听力原文
  audio_index.json  题组↔音频对应
  listening_solved.json  作答与投票记录
  各 *.py 工具脚本
"""
import json
import os
import re
import shutil
import sys

sys.path.insert(0, r"D:\work\2026-09-09-13-50-17\chaoxing")
os.chdir(r"D:\work\2026-09-09-13-50-17\chaoxing")

print("=== 1. 音频改名为可读文件名 ===")
IDX = json.load(open("audio_index.json", encoding="utf-8"))
TR = json.load(open("transcripts.json", encoding="utf-8"))
rename = {}
used = set()
for it in IDX:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", it["name"])
    if not name.lower().endswith(".mp3"):
        name += ".mp3"
    base, ext = os.path.splitext(name)
    cand, k = name, 1
    while cand in used:
        k += 1
        cand = f"{base}_{k}{ext}"
    used.add(cand)
    old, new = it["file"], os.path.join("audio", cand)
    if old != new:
        rename[old] = new
        it["file"] = new

for old, new in rename.items():
    if os.path.exists(old) and not os.path.exists(new):
        shutil.move(old, new)
        print(f"  {os.path.basename(old)[:36]} -> {os.path.basename(new)}")
    elif os.path.exists(new) and old in TR:
        pass

new_tr = {}
for k, v in TR.items():
    new_tr[rename.get(k, k)] = v
json.dump(IDX, open("audio_index.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
json.dump(new_tr, open("transcripts.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=1)
print(f"  索引与转写路径已同步（音频 {len(used)} 个）")

print("\n=== 2. 备份归档到 backups/ ===")
os.makedirs("backups", exist_ok=True)
n = 0
for f in os.listdir("."):
    if ".bak." in f and os.path.isfile(f):
        dst = os.path.join("backups", f)
        if not os.path.exists(dst):
            shutil.move(f, dst)
            n += 1
print(f"  归档 {n} 个备份文件")
# 学习库/题库的运行期备份也一并归档
for f in os.listdir("."):
    if f.startswith("learned_answers.json.bak") and os.path.isfile(f):
        dst = os.path.join("backups", f)
        if not os.path.exists(dst):
            shutil.move(f, dst)
            n += 1

print("\n=== 3. 删除测试与无主残留 ===")
junk = ["_pw36.py", "_watch1.py", "_pw_capture.json", "_asr_test.txt",
        "_probe_out.txt", "_page_32.html", "_test_audio.mp3", "_smoke.py"]
for f in junk:
    if os.path.exists(f):
        os.remove(f)
        print(f"  已删 {f}")

print("\n=== 4. 当前目录（清理后）===")
for f in sorted(os.listdir(".")):
    if os.path.isfile(f):
        print(f"  {os.path.getsize(f)//1024:>6} KB  {f}")
for d in ("audio", "models", "backups"):
    if os.path.isdir(d):
        cnt = sum(len(fs) for _, _, fs in os.walk(d))
        print(f"      目录 {d}/  ({cnt} 个文件)")
