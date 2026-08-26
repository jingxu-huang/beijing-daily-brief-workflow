#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send a finished daily report to Feishu with lark-cli.

Flow (mode=new, 当天首发):
1. import the Markdown report as a Feishu Docx with user identity;
2. record {ymd: doc_token/url} into 日报状态/feishu-docs.json (registry);
3. grant the daily report chat edit permission on the Docx;
4. send the Docx link to the chat with the configured IM identity.

Flow (mode=update, 当天已发过 —— 默认行为，2026-07-08 阶段1加固):
1. look up doc_token in the registry;
2. `docs +update --command overwrite` 原地覆盖同一篇 Docx（URL 不变）;
3. re-verify chat permission; do NOT send a new chat message.

注册表先写、群消息后发：即使发消息失败，重跑也会走同链更新，
从结构上杜绝"同一天重复导入新 Docx / 重复发链接"。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
DEFAULT_ENV = SCRIPT_DIR / "feishu_bot.env"
DATA_ROOT = Path(os.environ.get("RIBAO_DATA_ROOT") or PROJECT_ROOT)
STATE_DIR = Path(os.environ.get("RIBAO_STATE_DIR") or (DATA_ROOT / "日报状态"))
REGISTRY_PATH = STATE_DIR / "feishu-docs.json"
MANIFEST_HELPER = PROJECT_ROOT / "资讯日报-skill" / "manifest.py"
STAGING_DIR = SCRIPT_DIR / ".staging"


def load_registry():
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[feishu-cli] WARN: 注册表读取失败（{exc}），按空处理", file=sys.stderr)
        return {}


def save_registry(reg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_name(REGISTRY_PATH.name + ".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)


def update_report_in_place(doc_token, report_path, update_as="user", dry_run=False):
    """docs +update overwrite：原地覆盖同一篇 Docx，URL 与群里已发的链接保持不变。"""
    return run_lark(
        [
            "docs",
            "+update",
            "--doc",
            doc_token,
            "--command",
            "overwrite",
            "--content",
            "@" + project_relative(report_path),
            "--doc-format",
            "markdown",
            "--as",
            update_as,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def record_manifest(ymd, kvs):
    """把发布结果写进 run-manifest（尽力而为，失败不影响发送流程）。"""
    if not MANIFEST_HELPER.exists():
        return
    try:
        cmd = [sys.executable, str(MANIFEST_HELPER), "--date", ymd, "--state-dir", str(STATE_DIR)]
        for k, v in kvs.items():
            cmd += ["--kv", f"{k}={v}"]
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


def load_env_file(path):
    path = Path(path)
    if not path.exists():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def check_verdict(check_path):
    text = read_text(check_path)
    m = re.search(r"结论：\s*(PASS|WARN|FAIL)", text)
    return m.group(1) if m else ""


def check_sendable(check_path):
    return check_verdict(check_path) in ("PASS", "WARN")


def title_from_path(report_path):
    stem = Path(report_path).stem
    m = re.search(r"(\d{1,2}\.\d{1,2})", stem)
    if m:
        return f"资讯类简报-{m.group(1)}日"
    return stem


def ymd_from_path(report_path):
    stem = Path(report_path).stem
    m = re.search(r"(\d{1,2})\.(\d{1,2})", stem)
    if not m:
        return time.strftime("%Y%m%d")
    year = time.strftime("%Y")
    return f"{year}{int(m.group(1)):02d}{int(m.group(2)):02d}"


def draft_path_for_report(report_path, ymd=""):
    ymd = ymd or ymd_from_path(report_path)
    return DATA_ROOT / "日报草稿" / f"资讯日报-草稿-{ymd}.md"


def wewe_warning_from_draft(report_path, ymd=""):
    draft = draft_path_for_report(report_path, ymd)
    if not draft.exists():
        return ""
    for raw in draft.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if "WeWe RSS unavailable" in line:
            return line.lstrip("- ").strip()
        if line.startswith("- 微信公众号(WeWe RSS):") or line.startswith("微信公众号(WeWe RSS):"):
            return line.lstrip("- ").strip()
    return ""


def project_relative(path):
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(PROJECT_ROOT)
    except ValueError as exc:
        raise RuntimeError(f"文件必须位于日报项目目录内：{resolved}") from exc
    return "./" + rel.as_posix()


def stage_report_for_lark(report_path, ymd):
    """lark-cli only accepts paths relative to cwd; stage external data-root reports locally."""
    resolved = Path(report_path).resolve()
    try:
        resolved.relative_to(PROJECT_ROOT)
        return str(resolved)
    except ValueError:
        pass
    if not resolved.exists():
        raise RuntimeError(f"成稿文件不存在：{resolved}")
    target_dir = STAGING_DIR / (ymd or "unknown")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / resolved.name
    shutil.copy2(resolved, target)
    return str(target)


def parse_json_output(stdout):
    text = stdout.strip()
    if not text:
        raise RuntimeError("lark-cli 未返回 JSON")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def summarize_completed(cmd, result):
    print("[feishu-cli] " + " ".join(cmd))
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)


def run_lark(args, dry_run=False):
    cmd = ["lark-cli", *args]
    if dry_run:
        print("[feishu-cli] dry-run:", " ".join(cmd))
        return {"ok": True, "dry_run": True}
    result = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        summarize_completed(cmd, result)
        raise RuntimeError(f"lark-cli 执行失败，退出码 {result.returncode}")
    data = parse_json_output(result.stdout)
    if data.get("ok") is False:
        raise RuntimeError("lark-cli 返回失败：" + json.dumps(data, ensure_ascii=False)[:800])
    return data


def dig(obj, *keys):
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def collect_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from collect_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from collect_strings(child)


def message_id_from_result(message, dry_run=False):
    message_id = dig(message, "data", "message_id") or message.get("message_id")
    if not message_id and dry_run:
        return "dry-run"
    if not message_id:
        raise RuntimeError("飞书发送返回成功但没有 message_id，不能判定日报已送达")
    return message_id


def verify_chat_message(message_id, doc_url, send_as, dry_run=False):
    """Read back the URL message; only this verification permits final=sent."""
    if dry_run:
        return True
    result = run_lark(
        [
            "im",
            "+messages-mget",
            "--as",
            send_as,
            "--message-ids",
            message_id,
            "--no-reactions",
            "--format",
            "json",
        ]
    )
    haystack = "\n".join(collect_strings(result))
    if doc_url not in haystack:
        raise RuntimeError(f"飞书消息回读未找到当日文档 URL（message_id={message_id}）")
    return True


def find_string_by_key(obj, key):
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
        for child in obj.values():
            found = find_string_by_key(child, key)
            if found:
                return found
    if isinstance(obj, list):
        for child in obj:
            found = find_string_by_key(child, key)
            if found:
                return found
    return ""


def imported_doc(data):
    token = (
        dig(data, "data", "token")
        or dig(data, "data", "result", "token")
        or dig(data, "result", "token")
        or data.get("token")
        or find_string_by_key(data, "token")
    )
    url = (
        dig(data, "data", "url")
        or dig(data, "data", "result", "url")
        or dig(data, "result", "url")
        or data.get("url")
        or find_string_by_key(data, "url")
    )
    ticket = (
        dig(data, "data", "ticket")
        or dig(data, "ticket")
        or find_string_by_key(data, "ticket")
    )
    return token, url, ticket


def import_report(report_path, title, import_as="user", dry_run=False):
    if dry_run:
        run_lark(
            [
                "drive",
                "+import",
                "--file",
                project_relative(report_path),
                "--type",
                "docx",
                "--name",
                title,
                "--as",
                import_as,
                "--format",
                "json",
            ],
            dry_run=True,
        )
        return "dry-run-doc-token", "https://example.com/docx/dry-run"

    data = run_lark(
        [
            "drive",
            "+import",
            "--file",
            project_relative(report_path),
            "--type",
            "docx",
            "--name",
            title,
            "--as",
            import_as,
            "--format",
            "json",
        ]
    )
    token, url, ticket = imported_doc(data)
    if token and url:
        return token, url
    if not ticket:
        raise RuntimeError("飞书导入结果缺少 doc token/url，且没有可继续查询的 ticket")

    deadline = time.time() + int(os.environ.get("FEISHU_DOC_IMPORT_TIMEOUT", "120"))
    while time.time() < deadline:
        data = run_lark(
            [
                "drive",
                "+task_result",
                "--scenario",
                "import",
            "--ticket",
            ticket,
            "--as",
            import_as,
            "--format",
            "json",
        ]
    )
        token, url, _ = imported_doc(data)
        if token and url:
            return token, url
        time.sleep(2)
    raise RuntimeError("飞书文档导入超时，未拿到 doc token/url")


def grant_chat_edit(doc_token, chat_id, perm, permission_as="user", dry_run=False):
    payload = json.dumps(
        {
            "member_type": "openchat",
            "member_id": chat_id,
            "perm": perm,
            "type": "chat",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return run_lark(
        [
            "drive",
            "permission.members",
            "create",
            "--token",
            doc_token,
            "--type",
            "docx",
            "--data",
            payload,
            "--as",
            permission_as,
            "--yes",
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def send_chat_message(chat_id, doc_url, send_as, idempotency_key, dry_run=False):
    return run_lark(
        [
            "im",
            "+messages-send",
            "--chat-id",
            chat_id,
            "--as",
            send_as,
            "--text",
            doc_url,
            "--idempotency-key",
            idempotency_key,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def send_wewe_warning(chat_id, warning, send_as, idempotency_key, dry_run=False):
    if not warning:
        return {}
    return run_lark(
        [
            "im",
            "+messages-send",
            "--chat-id",
            chat_id,
            "--as",
            send_as,
            "--text",
            f"WeWe RSS 公众号源本次未正常抓取：{warning}。本期日报会继续发送，但公众号源可能缺失。",
            "--idempotency-key",
            idempotency_key,
            "--format",
            "json",
        ],
        dry_run=dry_run,
    )


def with_idempotency_suffix(key, suffix):
    suffix = (suffix or "").strip()
    return f"{key}-{suffix}" if suffix else key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, help="生成后的日报 Markdown")
    parser.add_argument("--check", required=True, help="质检报告 Markdown")
    parser.add_argument("--env", default=str(DEFAULT_ENV), help="可选 env 文件")
    parser.add_argument("--chat-id", default="", help="日报接收群 chat_id")
    parser.add_argument("--perm", default="", help="授予日报接收群的文档权限，默认 edit")
    parser.add_argument("--send-as", default="", choices=("", "bot", "user"), help="IM 发送身份，默认 bot")
    parser.add_argument("--import-as", default="", choices=("", "bot", "user"), help="文档导入身份，默认 user")
    parser.add_argument("--permission-as", default="", choices=("", "bot", "user"), help="文档授权身份，默认跟随导入身份")
    parser.add_argument("--allow-fail", action="store_true", help="即使质检 FAIL 也发送")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的 lark-cli 命令")
    parser.add_argument("--doc-only", action="store_true", help="只导入/更新飞书 Docx，不授权群聊、不发送 IM 消息")
    parser.add_argument("--date", default="", help="目标日期 YYYYMMDD；默认从成稿文件名推断（会猜年份，wrapper 应显式传入）")
    parser.add_argument(
        "--mode",
        default="auto",
        choices=("auto", "new", "update"),
        help="auto=注册表有当天记录则同链更新、否则首发（默认）；new=强制新建 Docx 并发群消息（会覆盖注册表记录，旧链接不再被更新）；update=必须已有记录，原地更新不发消息",
    )
    args = parser.parse_args()

    load_env_file(args.env)

    if not shutil.which("lark-cli"):
        raise RuntimeError("找不到 lark-cli，请确认 @larksuite/cli 已安装并在 PATH 中")

    verdict = check_verdict(args.check)
    if not args.allow_fail and not check_sendable(args.check):
        print(f"[feishu-cli] 质检未通过发送门槛（结论：{verdict or '未知'}），跳过发送。")
        return 0

    chat_id = (
        args.chat_id
        or os.environ.get("FEISHU_REPORT_CHAT_ID", "").strip()
        or os.environ.get("FEISHU_CHAT_ID", "").strip()
    )
    perm = (
        args.perm
        or os.environ.get("FEISHU_REPORT_CHAT_PERM", "").strip()
        or "edit"
    )
    send_as = (
        args.send_as
        or os.environ.get("FEISHU_CLI_SEND_AS", "").strip()
        or "bot"
    )
    import_as = (
        args.import_as
        or os.environ.get("FEISHU_CLI_IMPORT_AS", "").strip()
        or "user"
    )
    permission_as = (
        args.permission_as
        or os.environ.get("FEISHU_CLI_PERMISSION_AS", "").strip()
        or import_as
    )

    if not chat_id and not args.doc_only:
        raise RuntimeError("缺少日报接收群 chat_id")
    if not args.doc_only and perm != "edit":
        raise RuntimeError("日报群成员必须可编辑，请使用 --perm edit")

    title = title_from_path(args.report)
    ymd = args.date.strip() or ymd_from_path(args.report)
    lark_report = stage_report_for_lark(args.report, ymd)
    idempotency_suffix = os.environ.get("FEISHU_IDEMPOTENCY_SUFFIX", "").strip()

    registry = load_registry()
    entry = dict(registry.get(ymd) or {})
    mode = args.mode
    if mode == "auto":
        mode = "update" if entry.get("doc_token") else "new"
    if mode == "update" and not entry.get("doc_token"):
        raise RuntimeError(
            f"注册表 {REGISTRY_PATH.name} 中没有 {ymd} 的 doc_token，无法同链更新；首发请用 --mode new 或 auto"
        )

    if mode == "update":
        doc_token = entry["doc_token"]
        doc_url = entry.get("url", "")
        print(f"[feishu-cli] {ymd} 已有当日文档，原地更新：{doc_url or doc_token}")
        update_report_in_place(doc_token, lark_report, update_as=import_as, dry_run=args.dry_run)
        if args.doc_only:
            message_id = entry.get("message_id", "")
        else:
            grant_chat_edit(doc_token, chat_id, perm, permission_as=permission_as, dry_run=args.dry_run)
            message_id = entry.get("message_id", "")
        # 正常同链更新不重复发群消息；若首发曾卡在“文档已登记、URL 消息未成功”之间，
        # 必须用同一幂等键补发，不能把只有文档、没有群消息的状态误判为已送达。
        if not args.doc_only and not message_id:
            message = send_chat_message(
                chat_id,
                doc_url,
                send_as,
                with_idempotency_suffix(f"ribao-{ymd}-cli-docx-{send_as}", idempotency_suffix),
                dry_run=args.dry_run,
            )
            message_id = message_id_from_result(message, dry_run=args.dry_run)
            entry["message_id"] = message_id
    else:
        doc_token, doc_url = import_report(lark_report, title, import_as=import_as, dry_run=args.dry_run)
        # 关键顺序：先写注册表、再授权发消息——后续任何一步失败，重跑都会走同链更新，
        # 不会再新建第二篇 Docx。
        entry = {
            "doc_token": doc_token,
            "url": doc_url,
            "title": title,
            "message_id": "",
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not args.dry_run:
            registry[ymd] = entry
            save_registry(registry)
        if args.doc_only:
            message_id = ""
        else:
            grant_chat_edit(doc_token, chat_id, perm, permission_as=permission_as, dry_run=args.dry_run)
            warning = wewe_warning_from_draft(args.report, ymd)
            if warning:
                send_wewe_warning(
                    chat_id,
                    warning,
                    send_as,
                    with_idempotency_suffix(f"ribao-{ymd}-wewe-warning-{send_as}", idempotency_suffix),
                    dry_run=args.dry_run,
                )
            message = send_chat_message(
                chat_id,
                doc_url,
                send_as,
                with_idempotency_suffix(f"ribao-{ymd}-cli-docx-{send_as}", idempotency_suffix),
                dry_run=args.dry_run,
            )
            message_id = message_id_from_result(message, dry_run=args.dry_run)
            entry["message_id"] = message_id

    if not args.doc_only:
        verify_chat_message(message_id, doc_url, send_as, dry_run=args.dry_run)

    entry["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    entry["last_verdict"] = verdict
    entry["doc_only"] = bool(args.doc_only)
    if not args.dry_run:
        registry[ymd] = entry
        save_registry(registry)
        record_manifest(ymd, {"doc_url": entry.get("url", ""), "publish_mode": "doc-only" if args.doc_only else mode})

    print(
        json.dumps(
            {
                "ok": True,
                "mode": mode,
                "ymd": ymd,
                "title": title,
                "doc_url": doc_url,
                "doc_token": doc_token,
                "chat_id": chat_id,
                "perm": perm,
                "import_as": import_as,
                "permission_as": permission_as,
                "send_as": send_as,
                "message_id": message_id,
                "doc_only": bool(args.doc_only),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"[feishu-cli] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
