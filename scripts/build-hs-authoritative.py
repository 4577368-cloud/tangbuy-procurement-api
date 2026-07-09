#!/usr/bin/env python3
"""构建权威 HS 兜底库（中国海关 10 位商品编码）。

数据源：中国海关「单一窗口」公开参数，经 guoyunhe/singlewindow 整理：
  - CusComplex.json   全量商品：CODE_TS(10位)、G_NAME(品名)、CONTROL_MARK(监管)、UNIT_1/2(单位码)
  - CusUnit.json      法定单位码 → 名称

用途：本地 catalog + suggest 都不命中时，作为**权威兜底**校验/检索 10 位编码。
非本系统主目录（catalog.json）替代品，二者并存。

用法：
  python3 scripts/build-hs-authoritative.py --src /path/to/singlewindow-json
  # 缺省从 pinned raw GitHub 下载源文件
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "category"
OUT_FILE = OUT / "hs-authoritative.json"
INDEX_FILE = OUT / "hs-authoritative-index.json"

# guoyunhe/singlewindow @ main（数据时点约 2022，源自 singlewindow.cn 官方参数）
RAW_BASE = "https://raw.githubusercontent.com/guoyunhe/singlewindow/main"
SOURCE_VINTAGE = "singlewindow.cn (via guoyunhe/singlewindow, ~2022)"
NEEDED = ["CusComplex.json", "CusUnit.json"]


def _load_source(src: Path | None, name: str) -> object:
    if src is not None:
        path = src / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    print(f"↓ 下载 {name} …", file=sys.stderr)
    with urllib.request.urlopen(f"{RAW_BASE}/{name}", timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_code(raw: object) -> str:
    text = str(raw or "").strip()
    if text.isdigit() and len(text) == 9:
        return text.zfill(10)
    return text


def tokenize(text: str) -> set[str]:
    text = (text or "").lower()
    tokens: set[str] = set()
    tokens.update(re.findall(r"[a-z]{2,}", text))
    han = re.sub(r"[^\u4e00-\u9fff]", "", text)
    for i in range(max(0, len(han) - 1)):
        tokens.add(han[i : i + 2])
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,8}", text))
    return {t for t in tokens if len(t) >= 2}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=None, help="本地 singlewindow JSON 目录")
    args = ap.parse_args()

    complex_rows = _load_source(args.src, "CusComplex.json")
    unit_rows = _load_source(args.src, "CusUnit.json")
    if not isinstance(complex_rows, list) or not isinstance(unit_rows, list):
        print("源数据格式异常", file=sys.stderr)
        return 1

    unit_map = {
        str(u.get("UNIT_CODE") or "").strip(): str(u.get("UNIT_NAME") or "").strip()
        for u in unit_rows
        if isinstance(u, dict)
    }

    by_code: dict[str, dict] = {}
    for row in complex_rows:
        if not isinstance(row, dict):
            continue
        code = normalize_code(row.get("CODE_TS"))
        if not (code.isdigit() and len(code) == 10):
            continue
        name = str(row.get("G_NAME") or "").strip()
        control = str(row.get("CONTROL_MARK") or "").replace("\x00", "").strip()
        u1 = unit_map.get(str(row.get("UNIT_1") or "").strip(), "")
        u2 = unit_map.get(str(row.get("UNIT_2") or "").strip(), "")

        entry = by_code.get(code)
        if entry is None:
            entry = {
                "hs_code": code,
                "name": name,
                "names": [],
                "control_mark": control,
                "unit_1": u1,
                "unit_2": u2,
            }
            by_code[code] = entry
        if name and name not in entry["names"]:
            entry["names"].append(name)
        # 补齐更完整的单位/监管信息
        if not entry["unit_1"] and u1:
            entry["unit_1"] = u1
        if not entry["unit_2"] and u2:
            entry["unit_2"] = u2
        if not entry["control_mark"] and control:
            entry["control_mark"] = control

    # 倒排索引：分词 → 编码（供品名反查兜底）
    index: dict[str, list[str]] = {}
    for code, entry in by_code.items():
        toks: set[str] = set()
        for nm in entry["names"]:
            toks |= tokenize(nm)
        for tok in toks:
            index.setdefault(tok, [])
            if code not in index[tok]:
                index[tok].append(code)
    # 控制单 token 体积
    for tok in list(index.keys()):
        if len(index[tok]) > 60:
            index[tok] = index[tok][:60]

    OUT.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "source": SOURCE_VINTAGE,
            "code_count": len(by_code),
            "grain": "CODE_TS(10位)",
            "note": "权威兜底库，与主目录 catalog.json 并存；监管条件为原始字母码。",
        },
        "by_code": by_code,
    }
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    print(
        f"✅ 权威兜底库 {len(by_code)} 个 10 位编码 · 索引 {len(index)} token → {OUT_FILE.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
