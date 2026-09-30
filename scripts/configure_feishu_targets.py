#!/usr/bin/env python3
"""Persist approved Feishu delivery switches without printing private targets."""

import argparse
import json
import re
from pathlib import Path


CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9]+$")
MANAGED_KEYS = (
    "RIBAO_ENABLE_FEISHU",
    "FEISHU_REPORT_CHAT_ID",
    "FEISHU_REPORT_CHAT_PERM",
    "FEISHU_CLI_SEND_AS",
    "FEISHU_CLI_IMPORT_AS",
    "FEISHU_CLI_PERMISSION_AS",
    "FEISHU_DOC_ONLY",
    "FEISHU_DAILY_INDEX",
)


def configure(env_path, chat_id, enable_index, index_only=False):
    if not CHAT_ID_RE.fullmatch(chat_id):
        raise RuntimeError("飞书群会话 ID 格式无效")
    path = Path(env_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {
        "RIBAO_ENABLE_FEISHU": "1",
        "FEISHU_REPORT_CHAT_ID": chat_id,
        "FEISHU_REPORT_CHAT_PERM": "full_access",
        "FEISHU_CLI_SEND_AS": "user" if index_only else "bot",
        "FEISHU_CLI_IMPORT_AS": "user" if index_only else "bot",
        "FEISHU_CLI_PERMISSION_AS": "user",
        "FEISHU_DOC_ONLY": "1" if index_only else "0",
        "FEISHU_DAILY_INDEX": "1" if enable_index else "0",
        "RIBAO_GROUP_MESSAGES": "0" if index_only else "1",
    }
    seen = set()
    output = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            if key in seen:
                raise RuntimeError(f"配置项重复：{key}")
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    missing = set(MANAGED_KEYS) - seen
    if missing:
        raise RuntimeError("配置文件缺少受控字段：" + ", ".join(sorted(missing)))
    if "RIBAO_GROUP_MESSAGES" not in seen:
        output.append(f"RIBAO_GROUP_MESSAGES={values['RIBAO_GROUP_MESSAGES']}")
        seen.add("RIBAO_GROUP_MESSAGES")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(output) + "\n", encoding="utf-8")
    tmp.chmod(path.stat().st_mode)
    tmp.replace(path)
    return sorted(seen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--enable-index", action="store_true")
    parser.add_argument("--index-only", action="store_true")
    args = parser.parse_args()
    keys = configure(args.env, args.chat_id, args.enable_index, index_only=args.index_only)
    print(json.dumps({"ok": True, "configured_keys": keys}, ensure_ascii=False))


if __name__ == "__main__":
    main()
