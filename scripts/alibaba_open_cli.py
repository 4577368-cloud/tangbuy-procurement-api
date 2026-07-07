#!/usr/bin/env python3
"""
1688 开放平台（gw.open.1688.com）param2 协议客户端 CLI

对齐官方 SDK `com.alibaba.openapi.client`（ApiExecutor）的签名协议，用于本项目 Node/TS
端无法直接使用 Java/PHP/.Net SDK 时自研调用。

协议要点（param2）：
  URL   = {gateway}/openapi/param2/{version}/{namespace}/{apiName}/{appKey}
  签名   = HMAC_SHA1(appSecret, signPath + 排序拼接(key+value))  → 大写十六进制
  signPath = "param2/{version}/{namespace}/{apiName}/{appKey}"（不含 /openapi/、无前导 /）
  系统参数 = _aop_timestamp(毫秒) / access_token(可选) / _aop_signature

命令：
  status                              检查凭证 / token 状态
  sign_test                          用固定输入自检签名（确定性）
  authorize_url [--redirect URI]     打印用户授权 URL
  exchange_code --code CODE [--redirect URI]   用 code 换 access_token 并落盘
  refresh                            用 refresh_token 刷新 access_token
  call --namespace NS --name API [--version 1] [--params JSON] [--no-token]

输出 JSON：{"success": bool, "markdown": str, "data": {...}}
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_GATEWAY = "https://gw.open.1688.com"
AUTHORIZE_ENDPOINT = "https://auth.1688.com/oauth/authorize"


def _work_root() -> Path:
    return Path(os.environ.get("AGENT_WORK_ROOT", os.getcwd()))


def _load_dotenv() -> None:
    """standalone 运行时，从 .env.local 补齐未设置的环境变量（bridged 运行时由 Node 注入）。"""
    env_path = _work_root() / ".env.local"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()
    except OSError:
        pass


def _token_path() -> Path:
    return _work_root() / "data" / "integrations" / "alibaba-open-token.json"


def _read_token_file() -> dict:
    path = _token_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_token_file(data: dict) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _config() -> dict:
    _load_dotenv()
    app_key = os.environ.get("ALIBABA_OPEN_APP_KEY", "").strip()
    app_secret = os.environ.get("ALIBABA_OPEN_APP_SECRET", "").strip()
    gateway = os.environ.get("ALIBABA_OPEN_GATEWAY", DEFAULT_GATEWAY).strip().rstrip("/")
    redirect_uri = os.environ.get("ALIBABA_OPEN_REDIRECT_URI", "").strip()

    # access_token 优先级：环境变量显式覆盖 > token 文件
    token_env = os.environ.get("ALIBABA_OPEN_ACCESS_TOKEN", "").strip()
    token_file = _read_token_file()
    access_token = token_env or token_file.get("access_token", "")
    refresh_token = token_file.get("refresh_token", "")

    return {
        "app_key": app_key,
        "app_secret": app_secret,
        "gateway": gateway,
        "redirect_uri": redirect_uri,
        "access_token": access_token,
        "refresh_token": refresh_token,
    }


def _output(success: bool, markdown: str = "", data: dict | None = None) -> None:
    payload: dict[str, Any] = {"success": success}
    if markdown:
        payload["markdown"] = markdown
    if data is not None:
        payload["data"] = data
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _mask(value: str, keep: int = 3) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep)


def _to_str(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def sign_param2(sign_path: str, params: dict, app_secret: str, encoding: str = "hex") -> str:
    """官方 param2 签名：HMAC-SHA1(signPath + 排序拼接 key+value) → 默认大写十六进制。"""
    items = sorted(
        (k, v)
        for k, v in params.items()
        if k != "_aop_signature" and v is not None and v != ""
    )
    base = sign_path + "".join(f"{k}{v}" for k, v in items)
    digest = hmac.new(app_secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    if encoding == "base64":
        return base64.b64encode(digest).decode("utf-8")
    return digest.hex().upper()


def _http_post_form(url: str, form: dict, timeout: int = 30) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        if raw:
            parsed = _parse_json(raw)
            if parsed and "_raw" not in parsed:
                return parsed
        raise
    return _parse_json(raw)


def _http_get(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_data": parsed}


def _extract_error(result: dict) -> str | None:
    for key in ("error_message", "errorMessage", "error", "error_code", "errorCode"):
        if result.get(key):
            code = result.get("error_code") or result.get("errorCode") or ""
            msg = result.get("error_message") or result.get("errorMessage") or result.get("error") or ""
            return f"{code} {msg}".strip()
    return None


def _refresh_access_token() -> bool:
    """静默刷新 access_token；成功返回 True。"""
    cfg = _config()
    if not cfg["refresh_token"]:
        return False
    try:
        result = _oauth_token_request(
            {"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]}
        )
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError):
        return False
    if not result.get("access_token"):
        return False
    _persist_token(result)
    return True


def api_call(
    namespace: str,
    name: str,
    params: dict,
    version: str = "1",
    use_token: bool = True,
    *,
    _attempt: int = 0,
) -> dict:
    cfg = _config()
    if not cfg["app_key"] or not cfg["app_secret"]:
        raise RuntimeError("未配置 ALIBABA_OPEN_APP_KEY / ALIBABA_OPEN_APP_SECRET")

    body = {str(k): _to_str(v) for k, v in (params or {}).items() if v is not None}
    body["_aop_timestamp"] = str(int(time.time() * 1000))
    if use_token:
        if not cfg["access_token"]:
            raise RuntimeError("缺少 access_token，请先完成 OAuth 授权（authorize_url → exchange_code）")
        body["access_token"] = cfg["access_token"]

    sign_path = f"param2/{version}/{namespace}/{name}/{cfg['app_key']}"
    body["_aop_signature"] = sign_param2(sign_path, body, cfg["app_secret"])

    url = f"{cfg['gateway']}/openapi/param2/{version}/{namespace}/{name}/{cfg['app_key']}"
    try:
        return _http_post_form(url, body)
    except urllib.error.HTTPError as exc:
        if (
            _attempt == 0
            and use_token
            and exc.code in (401, 403)
            and _refresh_access_token()
        ):
            return api_call(
                namespace,
                name,
                params,
                version,
                use_token,
                _attempt=1,
            )
        raise


# ---------------- commands ----------------

def cmd_status(_args: argparse.Namespace) -> None:
    cfg = _config()
    ready = bool(cfg["app_key"] and cfg["app_secret"])
    has_token = bool(cfg["access_token"])
    token_meta = _read_token_file()
    owner = token_meta.get("resource_owner") or token_meta.get("loginId")
    member_id = token_meta.get("memberId") or token_meta.get("aliId")
    account_label = token_meta.get("account_label")
    lines = [
        "✅ 1688 开放平台凭证已配置" if ready else "❌ 未配置 ALIBABA_OPEN_APP_KEY / ALIBABA_OPEN_APP_SECRET",
        f"- AppKey：{_mask(cfg['app_key']) or '（空）'}",
        f"- 网关：{cfg['gateway']}",
        f"- access_token：{'已授权' if has_token else '未授权（需 OAuth 换取）'}",
    ]
    if has_token and account_label:
        lines.append(f"- 账号备注：{account_label}")
    if has_token and owner:
        lines.append(f"- 授权 1688 账号（resource_owner）：{owner}")
    elif has_token and not owner:
        lines.append("- 授权 1688 账号（resource_owner）：未知（手工 token，请补登录名或走 OAuth）")
    if has_token and member_id:
        lines.append(f"- memberId / aliId：{member_id}")
    if has_token and token_meta.get("note"):
        lines.append(f"- 备注：{token_meta.get('note')}")
    _output(
        ready,
        "\n".join(lines),
        {
            "configured": ready,
            "has_access_token": has_token,
            "gateway": cfg["gateway"],
            "redirect_uri": cfg["redirect_uri"],
            "resource_owner": owner,
            "member_id": member_id,
            "account_label": account_label,
        },
    )


def cmd_sign_test(_args: argparse.Namespace) -> None:
    # 确定性自检：固定输入应得到固定签名，便于比对 SDK 生成值。
    # AppKey/AppSecret 取自环境（不硬编码密钥）；未配置时用占位符仅验证算法。
    cfg = _config()
    secret = cfg["app_secret"] or "TEST_SECRET"
    app_key = cfg["app_key"] or "TEST_APP_KEY"
    sign_path = f"param2/1/com.alibaba.product/alibaba.product.get/{app_key}"
    params = {"productId": "610947572360", "_aop_timestamp": "1700000000000"}
    hex_sig = sign_param2(sign_path, params, secret, "hex")
    b64_sig = sign_param2(sign_path, params, secret, "base64")
    _output(
        True,
        "签名自检（固定输入）\n"
        f"- signPath：{sign_path}\n"
        f"- HEX：{hex_sig}\n"
        f"- BASE64：{b64_sig}",
        {"sign_path": sign_path, "params": params, "hex": hex_sig, "base64": b64_sig},
    )


def cmd_authorize_url(args: argparse.Namespace) -> None:
    cfg = _config()
    if not cfg["app_key"]:
        _output(False, "❌ 未配置 ALIBABA_OPEN_APP_KEY")
        return
    redirect = args.redirect or cfg["redirect_uri"]
    if not redirect:
        _output(False, "❌ 缺少 redirect_uri（--redirect 或 ALIBABA_OPEN_REDIRECT_URI）")
        return
    query = urllib.parse.urlencode(
        {
            "client_id": cfg["app_key"],
            "site": "1688",
            "redirect_uri": redirect,
            "response_type": "code",
            "state": args.state or "tangbuy",
        }
    )
    url = f"{AUTHORIZE_ENDPOINT}?{query}"
    _output(
        True,
        "打开以下链接完成授权，回调后用返回的 code 执行 exchange_code：\n\n" + url,
        {"authorize_url": url, "redirect_uri": redirect},
    )


def _oauth_token_request(grant_params: dict) -> dict:
    cfg = _config()
    url = f"{cfg['gateway']}/openapi/http/1/system.oauth2/getToken/{cfg['app_key']}"
    form = {
        "client_id": cfg["app_key"],
        "client_secret": cfg["app_secret"],
        **grant_params,
    }
    return _http_post_form(url, form)


def _persist_token(result: dict) -> dict:
    existing = _read_token_file()
    token = {
        **existing,
        "access_token": result.get("access_token", ""),
        "refresh_token": result.get("refresh_token", existing.get("refresh_token", "")),
        "expires_in": result.get("expires_in"),
        "refresh_token_timeout": result.get("refresh_token_timeout"),
        "resource_owner": result.get("resource_owner") or result.get("loginId"),
        "loginId": result.get("loginId") or result.get("resource_owner"),
        "aliId": result.get("aliId"),
        "memberId": result.get("memberId"),
        "obtained_at": int(time.time() * 1000),
        "raw": result,
    }
    if existing.get("account_label") and not token.get("account_label"):
        token["account_label"] = existing["account_label"]
    _write_token_file(token)
    return token


def _patch_token_meta(
    *,
    owner: str | None = None,
    login_id: str | None = None,
    member_id: str | None = None,
    account_label: str | None = None,
) -> dict:
    token = _read_token_file()
    if not token.get("access_token"):
        raise RuntimeError("token 文件缺少 access_token")
    if owner:
        token["resource_owner"] = owner.strip()
        token["loginId"] = token["resource_owner"]
    if login_id:
        token["loginId"] = login_id.strip()
        if not token.get("resource_owner"):
            token["resource_owner"] = token["loginId"]
    if member_id:
        token["memberId"] = member_id.strip()
        token["aliId"] = token["memberId"]
    if account_label:
        token["account_label"] = account_label.strip()
    _write_token_file(token)
    return token


def _resolve_owner_from_token() -> dict[str, str]:
    """尝试用 access_token 反查买家登录名（需开放平台 API 权限）。"""
    cfg = _config()
    if not cfg["access_token"]:
        return {}

    candidates = [
        ("com.alibaba.account", "alibaba.account.basic", "1", {}),
        ("com.alibaba.account", "alibaba.account.get", "1", {}),
        ("com.alibaba.trade", "alibaba.trade.getBuyerView", "1", {"webSite": "1688"}),
    ]
    for namespace, name, version, params in candidates:
        try:
            result = api_call(namespace, name, params, version=version, use_token=True)
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
            continue
        if _extract_error(result):
            continue
        blob = json.dumps(result, ensure_ascii=False)
        for key in ("loginId", "resource_owner", "login_id", "memberLoginId"):
            if f'"{key}"' in blob or f"'{key}'" in blob:
                pass
        # 常见嵌套
        def walk(obj: Any, found: dict[str, str]) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    lk = str(k).lower()
                    if lk in ("loginid", "resource_owner", "login_id", "memberloginid") and v:
                        found["resource_owner"] = str(v)
                    if lk in ("memberid", "aliid", "userid") and v:
                        found["memberId"] = str(v)
                    walk(v, found)
            elif isinstance(obj, list):
                for item in obj:
                    walk(item, found)

        found: dict[str, str] = {}
        walk(result, found)
        if found.get("resource_owner"):
            return found
    return {}



def cmd_exchange_code(args: argparse.Namespace) -> None:
    cfg = _config()
    if not cfg["app_key"] or not cfg["app_secret"]:
        _output(False, "❌ 未配置 AppKey/AppSecret")
        return
    redirect = args.redirect or cfg["redirect_uri"]
    result = _oauth_token_request(
        {
            "grant_type": "authorization_code",
            "need_refresh_token": "true",
            "redirect_uri": redirect,
            "code": args.code,
        }
    )
    if not result.get("access_token"):
        _output(False, f"❌ 换取 token 失败：{json.dumps(result, ensure_ascii=False)}", {"result": result})
        return
    token = _persist_token(result)
    _output(
        True,
        "✅ 已获取并保存 access_token\n"
        f"- access_token：{_mask(token['access_token'], 6)}\n"
        f"- 有效期(秒)：{token.get('expires_in')}",
        {"token": {k: v for k, v in token.items() if k != "raw"}},
    )


def cmd_refresh(_args: argparse.Namespace) -> None:
    cfg = _config()
    if not cfg["refresh_token"]:
        _output(False, "❌ 无 refresh_token，请重新走 authorize_url → exchange_code")
        return
    result = _oauth_token_request(
        {"grant_type": "refresh_token", "refresh_token": cfg["refresh_token"]}
    )
    if not result.get("access_token"):
        _output(False, f"❌ 刷新失败：{json.dumps(result, ensure_ascii=False)}", {"result": result})
        return
    token = _persist_token(result)
    _output(True, "✅ access_token 已刷新", {"token": {k: v for k, v in token.items() if k != "raw"}})


def cmd_patch_meta(args: argparse.Namespace) -> None:
    owner = (args.owner or os.environ.get("ALIBABA_OPEN_RESOURCE_OWNER") or "").strip()
    login_id = (args.login_id or "").strip()
    member_id = (args.member_id or "").strip()
    label = (args.label or "").strip()

    if args.resolve:
        resolved = _resolve_owner_from_token()
        if resolved.get("resource_owner"):
            owner = owner or resolved["resource_owner"]
        if resolved.get("memberId"):
            member_id = member_id or resolved["memberId"]

    if not owner and not login_id and not member_id and not label:
        if args.resolve:
            _output(
                False,
                "❌ 未能从开放平台反查 loginId（可能缺 API 权限）。"
                "请用 --owner 指定 1688 买家登录名。",
            )
        else:
            _output(False, "❌ 请提供 --owner / --login-id / --member-id / --label，或加 --resolve 尝试反查")
        return

    try:
        token = _patch_token_meta(
            owner=owner or None,
            login_id=login_id or None,
            member_id=member_id or None,
            account_label=label or None,
        )
    except RuntimeError as exc:
        _output(False, f"❌ {exc}")
        return

    owner_out = token.get("resource_owner") or token.get("loginId")
    lines = ["✅ token 元数据已更新"]
    if token.get("account_label"):
        lines.append(f"- 账号备注：{token['account_label']}")
    if owner_out:
        lines.append(f"- 授权 1688 账号（resource_owner）：{owner_out}")
    if token.get("memberId"):
        lines.append(f"- memberId：{token['memberId']}")
    _output(True, "\n".join(lines), {"token": {k: v for k, v in token.items() if k != "raw"}})


def cmd_call(args: argparse.Namespace) -> None:
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        _output(False, f"❌ --params 不是合法 JSON：{exc}")
        return
    if not isinstance(params, dict):
        _output(False, "❌ --params 必须是 JSON 对象")
        return
    try:
        result = api_call(
            namespace=args.namespace,
            name=args.name,
            params=params,
            version=args.version,
            use_token=not args.no_token,
        )
    except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
        _output(False, f"❌ 调用失败：{exc}")
        return

    err = _extract_error(result)
    if err:
        _output(False, f"❌ 接口返回错误：{err}", {"result": result})
        return
    _output(
        True,
        f"✅ {args.namespace}:{args.name} 调用成功",
        {"result": result},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="1688 开放平台 param2 客户端")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="检查凭证/token")
    sub.add_parser("sign_test", help="签名自检")

    p_auth = sub.add_parser("authorize_url", help="生成授权 URL")
    p_auth.add_argument("--redirect", default=None)
    p_auth.add_argument("--state", default=None)

    p_exc = sub.add_parser("exchange_code", help="code 换 token")
    p_exc.add_argument("--code", required=True)
    p_exc.add_argument("--redirect", default=None)

    sub.add_parser("refresh", help="刷新 token")

    p_meta = sub.add_parser("patch_meta", help="补全 token 元数据（resource_owner / loginId）")
    p_meta.add_argument("--owner", default=None, help="1688 登录名 resource_owner")
    p_meta.add_argument("--login-id", default=None, dest="login_id")
    p_meta.add_argument("--member-id", default=None, dest="member_id")
    p_meta.add_argument("--label", default=None, help="账号备注 account_label")
    p_meta.add_argument(
        "--resolve",
        action="store_true",
        help="尝试用 access_token 调开放平台接口反查 loginId",
    )

    p_call = sub.add_parser("call", help="通用接口调用")
    p_call.add_argument("--namespace", required=True, help="如 com.alibaba.product")
    p_call.add_argument("--name", required=True, help="如 alibaba.product.get")
    p_call.add_argument("--version", default="1")
    p_call.add_argument("--params", default=None, help="业务参数 JSON")
    p_call.add_argument("--no-token", action="store_true", help="不带 access_token")

    args = parser.parse_args()

    handlers = {
        "status": cmd_status,
        "sign_test": cmd_sign_test,
        "authorize_url": cmd_authorize_url,
        "exchange_code": cmd_exchange_code,
        "refresh": cmd_refresh,
        "patch_meta": cmd_patch_meta,
        "call": cmd_call,
    }
    handler = handlers.get(args.command)
    if not handler:
        _output(False, f"❌ 未知命令：{args.command}")
        return
    try:
        handler(args)
    except Exception as exc:  # noqa: BLE001 - CLI 顶层兜底
        _output(False, f"❌ 执行失败：{exc}")


if __name__ == "__main__":
    main()
