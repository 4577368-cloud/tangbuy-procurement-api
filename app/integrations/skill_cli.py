"""子进程调用仓库内 Skill CLI（与 TS skill-cli.ts 对齐）。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Optional

from app.core.paths import PROJECT_ROOT


def _extract_json_object(text: str) -> Optional[str]:
    trimmed = text.strip()
    if not trimmed:
        return None
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start == -1 or end <= start:
        return None
    candidate = trimmed[start : end + 1]
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        return None


def _build_ak_env() -> dict[str, str]:
    env = {**os.environ}
    if env.get("ALI_1688_AK", "").strip():
        return env
    ak_path = PROJECT_ROOT / "workspace" / ".1688-AK" / ".ak_store.json"
    try:
        store = json.loads(ak_path.read_text(encoding="utf-8"))
        ak = (store.get("ak") or "").strip()
        if ak:
            env["ALI_1688_AK"] = ak
    except (OSError, json.JSONDecodeError):
        pass
    return env


def run_python_cli(
    script: Path,
    args: list[str],
    *,
    timeout: int = 120,
    cwd: Optional[Path] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    env = _build_ak_env()
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            ["python3", str(script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd or script.parent),
            env=env,
        )
        raw = proc.stdout.strip()
        json_text = _extract_json_object(raw)
        if not json_text:
            err_hint = (proc.stderr or "").strip()[:300]
            return {"success": False, "error": raw or err_hint or "Skill CLI 无输出"}
        parsed = json.loads(json_text)
        if isinstance(parsed, dict):
            success = bool(parsed.get("success", True))
            return {
                "success": success,
                "markdown": parsed.get("markdown"),
                "data": parsed.get("data", parsed),
                "error": None if success else parsed.get("markdown") or parsed.get("error"),
            }
        return {"success": True, "data": parsed}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Skill CLI 超时"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


def normalize_count(raw: str) -> str:
    digits = re.sub(r"[^\d]", "", raw or "")
    return digits or (raw or "").strip()


def sourcing_cli_path() -> Path:
    override = os.environ.get("SKILL_1688_SOURCING_CLI", "").strip()
    if override:
        return Path(override)
    return PROJECT_ROOT / "scripts" / "vendor" / "1688-sourcing-inquiry" / "cli.py"


def supplychain_cli_path() -> Path:
    override = os.environ.get("SKILL_SUPPLYCHAIN_PROCUREMENT_CLI", "").strip()
    if override:
        return Path(override)
    return PROJECT_ROOT / "scripts" / "vendor" / "1688-supplychain-api-procurement" / "cli.py"


def category_mapper_path() -> Path:
    override = os.environ.get("SKILL_CATEGORY_MAPPER_SCRIPT", "").strip()
    if override:
        return Path(override)
    return PROJECT_ROOT / "scripts" / "category_mapper.py"


def inquiry_script_path() -> Path:
    override = os.environ.get("SKILL_INQUIRY_1688_SCRIPT", "").strip()
    if override:
        return Path(override)
    return PROJECT_ROOT / "scripts" / "vendor" / "inquiry-1688" / "scripts" / "inquiry.py"


def run_procurement_inquiry(offer_name: str, count: str, demand: str) -> dict[str, Any]:
    result = run_python_cli(
        sourcing_cli_path(),
        [
            "procurement",
            "--offerName",
            offer_name,
            "--count",
            normalize_count(count),
            "--demand",
            demand,
        ],
    )
    if not result.get("success"):
        return result
    url = _extract_requirement_url(result.get("data"))
    if url:
        result["requirementUrl"] = url
        follow = (
            "\n\n---\n"
            "**下一步**：任务中心已保存询盘链接，也可直接打开：\n"
            f"{url}\n\n"
            "供应商匹配与报价由 **1688 平台异步更新**，本系统暂无法像商家询盘那样一键拉回结果。"
        )
        result["markdown"] = f"{result.get('markdown') or '✅ 采购任务已创建'}{follow}"
    return result


def _extract_requirement_url(data: Any) -> Optional[str]:
    if not data or not isinstance(data, dict):
        return None
    nested = data.get("data")
    if isinstance(nested, dict):
        url = nested.get("requirementUrl")
        return str(url) if url else None
    return None


def run_supplychain_inquiry(args: list[str]) -> dict[str, Any]:
    cli = supplychain_cli_path()
    return run_python_cli(cli, ["inquiry", *args], timeout=180, cwd=cli.parent)


def run_category_suggest(
    title: str,
    hint: Optional[str] = None,
    goods_id: Optional[str] = None,
    image_url: Optional[str] = None,
    vision_keywords: Optional[list[str]] = None,
) -> dict[str, Any]:
    cmd = ["suggest", "--title", title]
    if hint:
        cmd.extend(["--hint", hint])
    if goods_id:
        cmd.extend(["--goods-id", goods_id])
    if image_url:
        cmd.extend(["--image-url", image_url])
    if vision_keywords:
        cmd.extend(["--vision-keywords", json.dumps(vision_keywords, ensure_ascii=False)])
    return unwrap_category_suggest_result(run_python_cli(category_mapper_path(), cmd, timeout=120))


def run_category_lookup(category_id: int) -> dict[str, Any]:
    raw = run_python_cli(category_mapper_path(), ["lookup", "--cid", str(category_id)], timeout=60)
    data = raw.get("data")
    if isinstance(data, dict) and data.get("cid"):
        cat = data
        return {
            "success": True,
            "category_id": cat.get("cid"),
            "category_cn_name": cat.get("cn_name"),
            "category_en_name": cat.get("en_name"),
            "hs_code": cat.get("hs_code"),
            "declare_cn_name": cat.get("dec_cn_name"),
            "declare_en_name": cat.get("dec_en_name"),
            "tariff": cat.get("tariff"),
        }
    return {"success": False, "error": "类目不存在"}


def unwrap_category_suggest_result(result: dict[str, Any]) -> dict[str, Any]:
    """run_python_cli 包一层 data；品类映射消费方需要内层 payload。"""
    data = result.get("data")
    if isinstance(data, dict) and ("category_id" in data or data.get("success") is False):
        return data
    return result


def is_inquiry_configured() -> bool:
    return bool(
        os.environ.get("ALPHASHOP_ACCESS_KEY", "").strip()
        and os.environ.get("ALPHASHOP_SECRET_KEY", "").strip()
    )


def run_inquiry_submit(
    item: str,
    question: str,
    quantity: Optional[str] = None,
    address: Optional[str] = None,
) -> dict[str, Any]:
    if not is_inquiry_configured():
        return {
            "success": False,
            "markdown": "❌ 未配置 ALPHASHOP_ACCESS_KEY / ALPHASHOP_SECRET_KEY。请在 .env.local 中配置遨虾 API 密钥。",
        }
    args = [item, question]
    if quantity:
        args.extend(["--quantity", normalize_count(quantity)])
    if address:
        args.extend(["--address", address])
    return run_python_cli(
        inquiry_script_path(),
        ["submit", *args],
        timeout=120,
        extra_env={
            "ALPHASHOP_ACCESS_KEY": os.environ.get("ALPHASHOP_ACCESS_KEY", ""),
            "ALPHASHOP_SECRET_KEY": os.environ.get("ALPHASHOP_SECRET_KEY", ""),
        },
    )


def run_inquiry_query(task_id: str) -> dict[str, Any]:
    if not is_inquiry_configured():
        return {"success": False, "markdown": "❌ 未配置遨虾 API 密钥"}
    return run_python_cli(
        inquiry_script_path(),
        ["query", task_id],
        timeout=120,
        extra_env={
            "ALPHASHOP_ACCESS_KEY": os.environ.get("ALPHASHOP_ACCESS_KEY", ""),
            "ALPHASHOP_SECRET_KEY": os.environ.get("ALPHASHOP_SECRET_KEY", ""),
        },
    )
