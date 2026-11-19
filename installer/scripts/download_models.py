"""按 model_sources.json 下载/校验模型。

用法：
  python download_models.py --sources <model_sources.json> --app <安装目录> \
      --ids clap,tts --endpoint https://hf-mirror.com

- 断点续传（HTTP Range），逐文件 sha256 校验，校验失败删除重下。
- url=null 的条目（manual）不编造地址，只打印放置说明并写进 install_status.json。
- HF 类条目用 repo + 文件名拼 URL；endpoint 可换成 huggingface.co。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

CHUNK = 1024 * 1024
UA = "RadioIntelligence-Installer/1.0"


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(4 * CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def already_ok(path: Path, sha: str) -> bool:
    return path.exists() and path.stat().st_size > 0 and sha256_of(path).lower() == sha.lower()


def build_url(item: dict, filename: str, endpoint: str) -> str | None:
    if item.get("url"):
        return item["url"]
    repo = item.get("repo")
    if repo:
        ep = endpoint.rstrip("/")
        return f"{ep}/{repo}/resolve/main/{filename}"
    return None  # manual-only, no fabrication


def download_one(url: str, target: Path, expected_bytes: int, expected_sha: str,
                 retries: int = 3) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    for attempt in range(1, retries + 1):
        done = part.stat().st_size if part.exists() else 0
        if done > expected_bytes:
            part.unlink()
            done = 0
        headers = {"User-Agent": UA}
        if done:
            headers["Range"] = f"bytes={done}-"
        req = urllib.request.Request(url, headers=headers)
        log(f"  下载 {target.name} 从 {done/1e6:.0f} MB 续传 (尝试 {attempt}/{retries})")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                mode = "ab" if done and resp.status == 206 else "wb"
                if mode == "wb":
                    done = 0
                with part.open(mode) as out:
                    while True:
                        b = resp.read(CHUNK)
                        if not b:
                            break
                        out.write(b)
                        done += len(b)
        except Exception as e:  # noqa
            log(f"  传输中断: {e}")
            time.sleep(2)
            continue
        if done != expected_bytes:
            log(f"  字节数不符 {done} != {expected_bytes}，重试")
            continue
        got = sha256_of(part)
        if got.lower() != expected_sha.lower():
            log(f"  sha256 不符 {got}，删除重试")
            part.unlink()
            continue
        part.replace(target)
        log(f"  ✅ {target.name} 校验通过")
        return True
    log(f"  ❌ {target.name} 失败（{retries} 次）")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True)
    ap.add_argument("--app", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--endpoint", default="https://hf-mirror.com")
    args = ap.parse_args()

    data = json.loads(Path(args.sources).read_text(encoding="utf-8"))
    wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
    app = Path(args.app)
    status: dict = {"models": {}, "manual": []}

    for item in data["models"]:
        if item["id"] not in wanted:
            continue
        label = item["label"]
        files = item.get("files")
        if files is None:
            files = [{
                "path": item["path"],
                "sha256": item["sha256"],
                "bytes": item["bytes"],
            }]
        all_ok = True
        for f in files:
            target = app / f["path"]
            if already_ok(target, f["sha256"]):
                log(f"[跳过] {f['path']} 已存在且校验通过")
                continue
            # 2026-11-19：file 级 url 优先（TTS 主模型+mmproj 各自独立源），
            # 再退回 item 级 url / repo 拼接。
            url = f.get("url") or build_url(item, Path(f["path"]).name, args.endpoint)
            if not url:
                log(f"[手动] {label}: {item.get('manual_hint', '请手动放置')}")
                status["manual"].append({"id": item["id"], "path": f["path"],
                                         "sha256": f["sha256"],
                                         "hint": item.get("manual_hint", "")})
                all_ok = False
                continue
            if not download_one(url, target, f["bytes"], f["sha256"]):
                all_ok = False
        status["models"][item["id"]] = "ok" if all_ok else "incomplete"

    st = app / "data_runtime" / "install_status_models.json"
    st.parent.mkdir(parents=True, exist_ok=True)
    # merge with existing (installer may call per-model)
    merged: dict = {}
    if st.exists():
        try:
            merged = json.loads(st.read_text(encoding="utf-8"))
        except Exception:
            merged = {}
    merged.update(status)
    st.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
