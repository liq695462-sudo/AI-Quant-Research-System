from __future__ import annotations

import argparse
import csv
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pdfplumber


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = PROJECT_ROOT / "knowledge_sources"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "article_ingest"


THEME_RULES = {
    "大盘风险": ["大盘", "指数", "A股", "调整", "见顶", "风险", "崩", "分化", "4200", "点位"],
    "海外风险": ["美股", "纳斯达克", "标普", "美元", "美债", "降息", "海外", "全球资本"],
    "战争地缘": ["战争", "伊朗", "以色列", "霍尔木兹", "停战", "地缘", "谈判"],
    "中报业绩": ["中报", "业绩", "地雷", "预告", "利润", "亏损", "财报"],
    "资金主线": ["资金流向", "主线", "增量资金", "场内资本", "流动性", "成交量"],
    "行业机会": ["液冷", "AI", "算力", "半导体", "机器人", "军工", "资源", "有色", "黄金", "电力"],
    "交易纪律": ["别追高", "操作", "仓位", "止损", "低吸", "回调", "交易性机会", "战法"],
}

SECTOR_WORDS = [
    "液冷",
    "算力",
    "AI",
    "半导体",
    "芯片",
    "机器人",
    "军工",
    "电力",
    "有色",
    "黄金",
    "稀土",
    "煤炭",
    "银行",
    "证券",
    "保险",
    "地产",
    "消费",
    "医药",
    "新能源",
    "光伏",
    "储能",
    "汽车",
    "传媒",
    "游戏",
]


@dataclass
class Article:
    path: Path
    title: str
    modified: datetime
    size: int
    pages: int
    text_chars: int
    digest: str
    themes: list[str]
    sectors: list[str]
    risk_score: int
    opportunity_score: int
    execution_note: str
    excerpt: str


def stable_digest(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def normalize_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "")
    text = dedupe_pdf_text_layer(text)
    return text.strip()


def dedupe_pdf_text_layer(text: str) -> str:
    repeated = sum(1 for i in range(1, len(text)) if text[i] == text[i - 1])
    if len(text) < 80 or repeated / max(len(text), 1) < 0.08:
        return text
    out: list[str] = []
    for ch in text:
        if out and out[-1] == ch and ("\u4e00" <= ch <= "\u9fff" or ch in "，。！？：；（）《》"):
            continue
        out.append(ch)
    return "".join(out)


def extract_pdf_text(path: Path, max_pages: int = 12) -> tuple[str, int]:
    parts: list[str] = []
    with pdfplumber.open(path) as pdf:
        pages = len(pdf.pages)
        for page in pdf.pages[:max_pages]:
            parts.append(page.extract_text() or "")
    return normalize_text("\n".join(parts)), pages


def count_hits(text: str, words: list[str]) -> int:
    return sum(text.count(word) for word in words)


def classify(text: str, title: str) -> tuple[list[str], list[str], int, int]:
    haystack = f"{title} {text}"
    theme_hits = {
        theme: count_hits(haystack, words)
        for theme, words in THEME_RULES.items()
    }
    themes = [theme for theme, hits in sorted(theme_hits.items(), key=lambda x: (-x[1], x[0])) if hits > 0]
    sectors = [word for word in SECTOR_WORDS if word in haystack]
    risk_score = (
        theme_hits.get("大盘风险", 0)
        + theme_hits.get("海外风险", 0)
        + theme_hits.get("战争地缘", 0)
        + theme_hits.get("中报业绩", 0) * 2
    )
    opportunity_score = theme_hits.get("行业机会", 0) + theme_hits.get("资金主线", 0)
    return themes[:5], sectors[:10], risk_score, opportunity_score


def build_execution_note(themes: list[str], risk_score: int, opportunity_score: int) -> str:
    if risk_score >= 12 and risk_score > opportunity_score:
        return "风控优先：四池只做观察降级，B2/起爆必须等次日30分钟确认，放量阴线直接回避。"
    if "中报业绩" in themes:
        return "业绩排雷优先：候选股先查中报预告/业绩风险，再进入BBI-MA60形态判断。"
    if opportunity_score >= 8:
        return "主题可加权：只把文章主题当作板块加分，个股仍必须满足黄线/白线和30分钟确认。"
    if "交易纪律" in themes:
        return "纪律强化：入池不是买点，日线候选必须交给次日30分钟确认。"
    return "背景跟踪：文章用于修正市场环境，不直接生成买点。"


def summarize_excerpt(text: str, max_chars: int = 180) -> str:
    if not text:
        return "PDF未抽取到可读文本"
    return text[:max_chars]


def iter_pdfs(source: Path) -> list[Path]:
    return sorted(source.rglob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)


def load_articles(source: Path) -> list[Article]:
    articles: list[Article] = []
    for path in iter_pdfs(source):
        text, pages = extract_pdf_text(path)
        themes, sectors, risk_score, opportunity_score = classify(text, path.stem)
        articles.append(
            Article(
                path=path,
                title=path.stem,
                modified=datetime.fromtimestamp(path.stat().st_mtime),
                size=path.stat().st_size,
                pages=pages,
                text_chars=len(text),
                digest=stable_digest(path),
                themes=themes,
                sectors=sectors,
                risk_score=risk_score,
                opportunity_score=opportunity_score,
                execution_note=build_execution_note(themes, risk_score, opportunity_score),
                excerpt=summarize_excerpt(text),
            )
        )
    return articles


def write_csv(articles: list[Article], output: Path) -> Path:
    path = output / "research_articles_index.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "modified",
                "title",
                "themes",
                "sectors",
                "risk_score",
                "opportunity_score",
                "pages",
                "text_chars",
                "digest",
                "path",
                "execution_note",
            ]
        )
        for a in articles:
            writer.writerow(
                [
                    a.modified.strftime("%Y-%m-%d %H:%M:%S"),
                    a.title,
                    "、".join(a.themes),
                    "、".join(a.sectors),
                    a.risk_score,
                    a.opportunity_score,
                    a.pages,
                    a.text_chars,
                    a.digest,
                    str(a.path),
                    a.execution_note,
                ]
            )
    return path


def write_report(articles: list[Article], output: Path, source: Path) -> Path:
    path = output / "research_articles_system_report.md"
    latest = articles[:8]
    all_themes = Counter(theme for a in articles for theme in a.themes)
    all_sectors = Counter(sector for a in articles for sector in a.sectors)
    lines: list[str] = []
    lines.append("# 外部研究资料 - 交易体系接入报告")
    lines.append("")
    lines.append(f"- 扫描目录：`{source}`")
    lines.append(f"- 文章数量：{len(articles)}")
    lines.append(f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## 接入原则")
    lines.append("")
    lines.append("1. 外部研究资料只用于判断市场环境、风险温度和板块方向，不直接生成买点。")
    lines.append("2. 个股仍按本地黄白线体系执行：黄线=BBI，白线=MA60，四池先筛候选。")
    lines.append("3. 入池不是买点；B1、B2、起爆和趋势预备都需要30分钟结构确认。")
    lines.append("4. 文章若提示大盘、中报、海外或战争风险，四池信号自动降一档处理。")
    lines.append("")
    lines.append("## 当前文章主题画像")
    lines.append("")
    lines.append("| 主题 | 命中篇数 |")
    lines.append("| --- | ---: |")
    for theme, count in all_themes.most_common():
        lines.append(f"| {theme} | {count} |")
    lines.append("")
    lines.append("## 高频板块词")
    lines.append("")
    if all_sectors:
        lines.append("、".join([f"{k}({v})" for k, v in all_sectors.most_common(20)]))
    else:
        lines.append("暂无明显板块词。")
    lines.append("")
    lines.append("## 最新文章接入清单")
    lines.append("")
    lines.append("| 时间 | 文章 | 主题 | 板块词 | 体系处理 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for a in latest:
        lines.append(
            "| "
            + " | ".join(
                [
                    a.modified.strftime("%Y-%m-%d %H:%M"),
                    a.title.replace("|", " "),
                    "、".join(a.themes) or "未识别",
                    "、".join(a.sectors) or "无",
                    a.execution_note.replace("|", " "),
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## 今日执行口径")
    lines.append("")
    risk_total = sum(a.risk_score for a in latest)
    opp_total = sum(a.opportunity_score for a in latest)
    if risk_total > opp_total:
        lines.append("最新资料整体偏风控：优先降低追高冲动，四池候选宁可少做，重点等30分钟确认。")
    elif opp_total > risk_total:
        lines.append("最新文章整体有主线线索：可以把高频板块作为自选加分项，但不替代BBI-MA60硬条件。")
    else:
        lines.append("最新文章多为背景跟踪：维持原黄白线体系，不因文章观点单独改变买点。")
    lines.append("")
    lines.append("## 单篇摘录")
    lines.append("")
    for a in latest:
        lines.append(f"### {a.title}")
        lines.append("")
        lines.append(f"- 文件：`{a.path}`")
        lines.append(f"- 抽取字数：{a.text_chars}，页数：{a.pages}")
        lines.append(f"- 摘录：{a.excerpt}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest research PDFs into the local BBI-MA60 system.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    articles = load_articles(args.source)
    csv_path = write_csv(articles, args.output)
    report_path = write_report(articles, args.output, args.source)
    print(f"articles={len(articles)}")
    print(f"csv={csv_path}")
    print(f"report={report_path}")


if __name__ == "__main__":
    main()
