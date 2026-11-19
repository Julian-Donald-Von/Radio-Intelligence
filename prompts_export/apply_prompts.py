# -*- coding: utf-8 -*-
"""把 prompts_export\*.txt 写回 config.json（自动备份 + 校验 + 原子替换）。

用法：先关掉应用（应用运行中会用内存里的值覆盖本文件），再：
    ./venv/Scripts/python.exe prompts_export\apply_prompts.py
"""
import json, os, shutil, time

ROOT = r"D:\MusicAI"
CFG = os.path.join(ROOT, "data_runtime", "config.json")
OUT = os.path.join(ROOT, "prompts_export")

d = json.load(open(CFG, encoding="utf-8"))
d.setdefault("prompts", {})
bak = CFG + ".bak-" + time.strftime("%%Y%%m%%d-%%H%%M%%S")
shutil.copy2(CFG, bak)
changed = []
for fn in os.listdir(OUT):
    if not fn.endswith(".txt"):
        continue
    key = fn[:-4]
    txt = open(os.path.join(OUT, fn), encoding="utf-8").read()
    if d["prompts"].get(key) != txt:
        changed.append((key, len(d["prompts"].get(key) or ""), len(txt)))
        d["prompts"][key] = txt
if not changed:
    print("没有变化。")
else:
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CFG)
    for key, old, new in changed:
        print("  写入 %-22s %d → %d 字符" % (key, old, new))
    print("备份：%%s" %% bak)
    print("✅ 已写回。重新打开应用生效。")
