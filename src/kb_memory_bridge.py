from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCAL_MEMORY_SNAPSHOT_PATH = ROOT / "output" / "market_kb_memory_snapshot.md"


def candidate_export_paths() -> list[Path]:
    candidates: list[Path] = []

    env_path = os.getenv("MARKET_KB_EXPORT_PATH")
    if env_path:
        candidates.append(Path(env_path))

    candidates.extend(
        [
            ROOT / "knowledge_sources" / "market_kb_memory_export.json",
            ROOT / "cache" / "market_kb_memory_export.json",
        ]
    )
    return candidates


def find_market_kb_export(explicit_path: str | Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    candidates.extend(candidate_export_paths())

    seen: set[str] = set()
    for path in candidates:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.exists():
            return path
    return None


def load_market_kb_memory(explicit_path: str | Path | None = None) -> tuple[dict | None, Path | None]:
    export_path = find_market_kb_export(explicit_path)
    if not export_path:
        return None, None
    return json.loads(export_path.read_text(encoding="utf-8")), export_path


def compact_memory_snapshot(memory: dict | None, export_path: Path | None) -> dict:
    if not memory:
        return {
            "available": False,
            "export_path": str(export_path) if export_path else None,
            "message": "market-kb memory export not found",
        }

    return {
        "available": True,
        "generated_at": memory.get("generated_at"),
        "kb_root": memory.get("kb_root"),
        "memory_version": memory.get("memory_version"),
        "export_path": str(export_path) if export_path else None,
        "top_topics": (memory.get("top_topics") or [])[:5],
        "top_execution_cards": (memory.get("top_execution_cards") or [])[:5],
        "canonical_rules": (memory.get("canonical_rules") or [])[:5],
        "recent_documents": (memory.get("recent_documents") or [])[:5],
        "recommended_entrypoints": memory.get("recommended_entrypoints") or {},
    }


def _path_line(label: str, path_text: str | None) -> str:
    if not path_text:
        return f"- {label}：暂无"
    return f"- {label}：`{Path(path_text).as_posix()}`"


def render_market_kb_section(memory: dict | None, export_path: Path | None, snapshot_path: Path) -> str:
    lines = [
        "## 跨项目知识库联动",
        "",
    ]

    if not memory:
        lines.extend(
            [
                "- 当前未找到 `market-kb` 导出的共享记忆。",
                _path_line("预期导出路径", str(export_path) if export_path else None),
                _path_line("本地快照路径", str(snapshot_path)),
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"- 记忆更新时间：`{memory.get('generated_at', '-')}`",
            _path_line("共享导出", str(export_path) if export_path else None),
            _path_line("本地联动快照", str(snapshot_path)),
            "",
            "### 当前优先主线",
            "",
        ]
    )
    for row in (memory.get("top_topics") or [])[:3]:
        lines.append(
            f"- {row['name']}：近期命中 `{row['recent_count']}`，关联文档 `{row['doc_count']}`，最近日期 `{row['latest_date']}`"
        )

    lines.extend(["", "### 当前优先执行卡片", ""])
    for row in (memory.get("top_execution_cards") or [])[:3]:
        lines.append(f"- {row['name']}：关联命中 `{row['match_count']}`")

    lines.extend(["", "### 长期纪律规则", ""])
    for row in (memory.get("canonical_rules") or [])[:5]:
        lines.append(f"- {row['text']}")

    lines.extend(["", "### 最近应优先回看的资料", ""])
    for row in (memory.get("recent_documents") or [])[:3]:
        lines.append(f"- {row['date']}｜{row['title']}｜`{Path(row['source_path']).as_posix()}`")

    return "\n".join(lines) + "\n"


def render_local_memory_snapshot(memory: dict | None, export_path: Path | None) -> str:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# AI 量化研究系统联动记忆快照",
        "",
        f"- 生成时间：`{now_text}`",
        _path_line("共享导出", str(export_path) if export_path else None),
        "",
        "这份快照由 `src/kb_memory_bridge.py` 自动生成，用于将本地市场知识库与选股、回测和每日复盘串联。",
        "",
    ]

    if not memory:
        lines.extend(
            [
                "## 当前状态",
                "",
                "- 暂未读到市场知识导出；可通过 `MARKET_KB_EXPORT_PATH` 指定 JSON 文件。",
                "",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(
        [
            "## 主线记忆",
            "",
        ]
    )
    for row in (memory.get("top_topics") or [])[:5]:
        lines.append(
            f"- {row['name']}：近期 `{row['recent_count']}` / 全量 `{row['doc_count']}` / 最近 `{row['latest_date']}`"
        )

    lines.extend(["", "## 执行卡片", ""])
    for row in (memory.get("top_execution_cards") or [])[:5]:
        lines.append(f"- {row['name']}：匹配 `{row['match_count']}`")

    lines.extend(["", "## 长期规则", ""])
    for row in (memory.get("canonical_rules") or [])[:5]:
        lines.append(f"- {row['text']}")

    lines.extend(["", "## 推荐入口", ""])
    for key, path_text in (memory.get("recommended_entrypoints") or {}).items():
        lines.append(f"- {key}：`{Path(path_text).as_posix()}`")

    lines.extend(["", "## 最近资料", ""])
    for row in (memory.get("recent_documents") or [])[:5]:
        lines.append(f"- {row['date']}｜{row['title']}")

    return "\n".join(lines) + "\n"


def write_local_memory_snapshot(
    memory: dict | None,
    export_path: Path | None,
    snapshot_path: Path = LOCAL_MEMORY_SNAPSHOT_PATH,
) -> Path:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(render_local_memory_snapshot(memory, export_path), encoding="utf-8")
    return snapshot_path
