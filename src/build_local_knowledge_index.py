from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pdfplumber
from docx import Document


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(os.getenv("AQRS_KNOWLEDGE_ROOT", PROJECT_ROOT)).resolve()
EXTERNAL_NOTES_ROOT = Path(
    os.getenv("AQRS_EXTERNAL_NOTES", PROJECT_ROOT / "knowledge_sources")
).resolve()
OUT_DIR = PROJECT_ROOT / "output" / "knowledge_index"
OUT_JSON = OUT_DIR / "local_knowledge_index.json"
OUT_MD = OUT_DIR / "local_knowledge_index.md"

TEXT_EXTS = {".md", ".txt"}
DOC_EXTS = {".docx"}
PDF_EXTS = {".pdf"}

CATEGORY_KEYWORDS = {
    "技术形态": ["B1", "B2", "BBI", "MA60", "60日", "60分钟", "均线", "黄线", "白线", "回踩", "起爆", "缩量", "放量", "单针", "知行趋势线", "异动"],
    "资金主线": ["资金", "主线", "成交额", "量能", "资金流向", "峰值", "55%", "7到13", "持续", "承接", "分歧", "淘汰"],
    "题材产业": ["AI", "算力", "液冷", "PCB", "CPO", "半导体", "商业航天", "机器人", "储能", "电网", "有色", "黄金", "油气", "化工"],
    "基本面验证": ["订单", "业绩", "利润", "现金流", "财报", "毛利率", "客户", "产能", "涨价", "兑现"],
    "风险纪律": ["风控", "止损", "出货", "假反转", "舒服", "风险", "仓位", "防守", "退潮", "高位", "暴雷"],
    "回测工程": ["回测", "因子", "缓存", "问财", "筛选", "fast", "numpy", "参数", "候选", "优化"],
}


def read_text_file(path: Path, limit: int = 80000) -> str:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=enc, errors="ignore")[:limit]
        except Exception:
            continue
    return ""


def read_docx(path: Path, limit: int = 50000) -> str:
    try:
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text)
        return text[:limit]
    except Exception:
        return ""


def read_pdf(path: Path, limit: int = 50000) -> tuple[str, int, str | None]:
    # Large scanned PDFs are expensive and often image-only. Sample them enough for classification.
    max_pages = 25 if path.stat().st_size <= 5_000_000 else 12
    chunks: list[str] = []
    page_count = 0
    err: str | None = None
    try:
        with pdfplumber.open(str(path)) as pdf:
            page_count = len(pdf.pages)
            for page in pdf.pages[:max_pages]:
                if sum(len(x) for x in chunks) >= limit:
                    break
                chunks.append(page.extract_text() or "")
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    return "\n".join(chunks)[:limit], page_count, err


def source_bucket(path: Path) -> str:
    s = str(path)
    if s.startswith(str(EXTERNAL_NOTES_ROOT)):
        return "外部研究笔记"
    if "\\output\\wencai_dynamic\\" in s:
        return "问财四池迭代"
    if "\\output\\lightweight_bbi_backtest\\" in s or path.name.endswith(".py"):
        return "本地回测工程"
    return "本地交易体系"


def score_categories(text: str, name: str) -> dict[str, int]:
    src = f"{name}\n{text}"
    return {cat: sum(src.count(k) for k in keys) for cat, keys in CATEGORY_KEYWORDS.items()}


def compact_excerpt(text: str, max_len: int = 450) -> str:
    cleaned = " ".join(text.replace("\r", "\n").split())
    return cleaned[:max_len]


def collect_files() -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in [ROOT, EXTERNAL_NOTES_ROOT]:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or any(part in {".git", ".venv", "__pycache__", "output"} for part in p.parts):
                continue
            if p.suffix.lower() in TEXT_EXTS | DOC_EXTS | PDF_EXTS:
                resolved = p.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    paths.append(p)
    return sorted(paths, key=lambda p: str(p).lower())


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    category_totals: dict[str, Counter[str]] = defaultdict(Counter)
    bucket_counts = Counter()
    unreadable: list[dict[str, Any]] = []

    for path in collect_files():
        suffix = path.suffix.lower()
        text = ""
        pages: int | None = None
        error: str | None = None
        if suffix in TEXT_EXTS:
            text = read_text_file(path)
        elif suffix in DOC_EXTS:
            text = read_docx(path)
        elif suffix in PDF_EXTS:
            text, pages, error = read_pdf(path)
        bucket = source_bucket(path)
        bucket_counts[bucket] += 1
        scores = score_categories(text, path.name)
        for cat, value in scores.items():
            if value:
                category_totals[cat][str(path)] = value
        row = {
            "path": str(path),
            "bucket": bucket,
            "suffix": suffix,
            "size": path.stat().st_size,
            "last_write": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "pages": pages,
            "text_chars": len(text),
            "scores": scores,
            "excerpt": compact_excerpt(text),
            "error": error,
        }
        if not text and (suffix in PDF_EXTS | DOC_EXTS):
            unreadable.append(row)
        rows.append(row)

    top_by_category = {
        cat: [{"path": p, "score": s} for p, s in counter.most_common(12)]
        for cat, counter in category_totals.items()
    }
    payload = {
        "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "roots": [str(ROOT), str(EXTERNAL_NOTES_ROOT)],
        "file_count": len(rows),
        "bucket_counts": dict(bucket_counts),
        "top_by_category": top_by_category,
        "unreadable_or_image_pdf": unreadable[:80],
        "files": rows,
    }
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 本地交易知识库索引",
        "",
        f"- 生成时间：{payload['run_time']}",
        f"- 扫描文件数：{payload['file_count']}",
        "",
        "## 来源分布",
        "",
    ]
    for bucket, count in bucket_counts.most_common():
        lines.append(f"- {bucket}: {count}")
    lines += ["", "## 主题最高相关文件", ""]
    for cat in CATEGORY_KEYWORDS:
        lines += [f"### {cat}", ""]
        for item in top_by_category.get(cat, [])[:10]:
            lines.append(f"- {item['score']} | {item['path']}")
        lines.append("")
    lines += ["## 文本抽取异常或疑似图片型文件", ""]
    for item in unreadable[:30]:
        lines.append(f"- {item['path']} | pages={item['pages']} | error={item['error']}")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "out_json": str(OUT_JSON),
        "out_md": str(OUT_MD),
        "file_count": len(rows),
        "bucket_counts": dict(bucket_counts),
        "unreadable_count": len(unreadable),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
