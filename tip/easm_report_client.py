#!/usr/bin/env python3
"""从 EASM 自动研判服务获取报告详情及全部研判记录。"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ==================== 请填写以下配置 ====================

# 部署服务的 170 主机完整 IP，例如：192.168.1.170
SERVICE_IP = "请填写170主机的完整IP"

# 服务端口，文档默认值为 8080
SERVICE_PORT = 8080

# 170 服务端 DOWNSTREAM_API_TOKEN 对应的实际值
DOWNSTREAM_API_TOKEN = "请填写下游API令牌"

# =======================================================

REQUEST_TIMEOUT_SECONDS = 30
RECORD_PAGE_SIZE = 1000
OUTPUT_DIR = Path("easm_reports")


class ApiError(RuntimeError):
    """API 请求或响应不符合预期。"""


def validate_config() -> None:
    """在发起请求前检查需要人工填写的配置。"""
    if not SERVICE_IP.strip() or "请填写" in SERVICE_IP:
        raise ApiError("请先填写 SERVICE_IP，例如 192.168.1.170")
    if not isinstance(SERVICE_PORT, int) or not 1 <= SERVICE_PORT <= 65535:
        raise ApiError("SERVICE_PORT 必须是 1～65535 之间的整数")
    if not DOWNSTREAM_API_TOKEN.strip() or "请填写" in DOWNSTREAM_API_TOKEN:
        raise ApiError("请先填写 DOWNSTREAM_API_TOKEN")


class EasmTriageClient:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.base_url = f"http://{host.strip()}:{port}"
        self.token = token.strip()
        # 内网 177 到 170 直接连接，避免系统代理错误接管内网请求。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _get_json(
        self,
        path: str,
        params: dict[str, object] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)

        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"

        request = urllib.request.Request(
            self.base_url + path + query,
            headers=headers,
            method="GET",
        )

        try:
            with self.opener.open(
                request, timeout=REQUEST_TIMEOUT_SECONDS
            ) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                data = json.loads(response.read().decode(charset))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(
                f"请求 {path} 失败：HTTP {exc.code}，响应：{body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ApiError(
                f"无法连接 {self.base_url}：{exc.reason}；"
                "请检查 170 服务监听地址、防火墙和端口"
            ) from exc
        except TimeoutError as exc:
            raise ApiError(f"请求 {path} 超时") from exc
        except json.JSONDecodeError as exc:
            raise ApiError(f"请求 {path} 返回的内容不是合法 JSON") from exc

        if not isinstance(data, dict):
            raise ApiError(f"请求 {path} 返回的 JSON 顶层不是对象")
        return data

    def health(self) -> dict[str, Any]:
        return self._get_json("/health", authenticated=False)

    def get_latest_completed_report(self) -> dict[str, Any]:
        page = self._get_json(
            "/reports",
            {
                "processing_status": "completed",
                "limit": 1,
                "offset": 0,
            },
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise ApiError("/reports 响应中缺少合法的 items 数组")
        if not items:
            raise ApiError("170 服务中暂时没有处理完成的报告")
        if not isinstance(items[0], dict):
            raise ApiError("/reports 返回的报告格式不正确")
        return items[0]

    def get_report(self, report_id: int) -> dict[str, Any]:
        return self._get_json(f"/reports/{report_id}")

    def get_stats(self, report_id: int) -> dict[str, Any]:
        return self._get_json(f"/reports/{report_id}/stats")

    def get_all_records(self, report_id: int) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset = 0

        while True:
            page = self._get_json(
                f"/reports/{report_id}/records",
                {"limit": RECORD_PAGE_SIZE, "offset": offset},
            )
            items = page.get("items")
            total = page.get("total")

            if not isinstance(items, list):
                raise ApiError("研判记录响应中缺少合法的 items 数组")
            if not isinstance(total, int) or total < 0:
                raise ApiError("研判记录响应中的 total 不合法")
            if any(not isinstance(item, dict) for item in items):
                raise ApiError("研判记录 items 中存在非对象数据")

            records.extend(items)
            print(f"研判明细：已获取 {len(records)} / {total} 条")

            if len(records) >= total:
                break
            if not items:
                raise ApiError(
                    f"分页提前结束：应获取 {total} 条，实际仅获取 {len(records)} 条"
                )
            offset += len(items)

        return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="获取最新或指定的 EASM 自动研判报告"
    )
    parser.add_argument(
        "--report-id",
        type=int,
        help="指定本服务的数字报告 ID；不填写时获取最新已完成报告",
    )
    return parser.parse_args()


def safe_filename(value: object) -> str:
    text = str(value or "unknown")
    return re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("._") or "unknown"


def save_report(payload: dict[str, Any], report_id: int, task_id: object) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / (
        f"report_{report_id}_{safe_filename(task_id)}.json"
    )
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path.resolve()


def main() -> int:
    args = parse_args()
    validate_config()

    client = EasmTriageClient(
        SERVICE_IP,
        SERVICE_PORT,
        DOWNSTREAM_API_TOKEN,
    )

    health = client.health()
    print(
        "服务状态：",
        health.get("status", "unknown"),
        f"(版本 {health.get('version', 'unknown')})",
    )

    if args.report_id is None:
        report_summary = client.get_latest_completed_report()
        report_id = report_summary.get("id")
        if not isinstance(report_id, int):
            raise ApiError("最新报告缺少合法的数字 id")
    else:
        if args.report_id <= 0:
            raise ApiError("--report-id 必须是正整数")
        report_id = args.report_id

    print(f"正在获取报告 {report_id}……")
    report = client.get_report(report_id)
    stats = client.get_stats(report_id)
    records = client.get_all_records(report_id)

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "report": report,
        "stats": stats,
        "records": records,
    }
    output_path = save_report(payload, report_id, report.get("task_id"))
    print(f"获取完成：{len(records)} 条研判明细")
    print(f"结果文件：{output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("操作已取消", file=sys.stderr)
        raise SystemExit(130)
