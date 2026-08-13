#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献日报推送脚本（PubMed + arXiv -> Server酱 -> 手机微信）

由 GitHub Actions 每 3 天定时触发，也可本地手动运行测试：

    SENDKEY=你的SendKey python literature_push.py

环境变量：
    SENDKEY    Server酱 SendKey（必填）
    DAYS       检索最近 N 天（默认 3）
    TOP_N      推送条数（默认 5）

仅用 Python 标准库（urllib / json / xml），无需 pip install，云端跑得干净。
"""

import os
import sys
import json
import time
import datetime
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

# ==================== 可配置区（改这里即可） ====================

# PubMed 检索式（每条对应一个研究子方向，覆盖食管癌影像AI/新辅助/病理WSI/单细胞/pCR）
PUBMED_QUERIES = [
    '("esophageal squamous cell carcinoma"[Title/Abstract] OR "esophageal cancer"[Title/Abstract]) AND ("deep learning"[Title/Abstract] OR radiomics[Title/Abstract] OR "medical imaging"[Title/Abstract])',
    '"esophageal cancer"[Title/Abstract] AND neoadjuvant[Title/Abstract] AND (chemoradiotherapy[Title/Abstract] OR immunotherapy[Title/Abstract])',
    '("whole slide image"[Title/Abstract] OR "digital pathology"[Title/Abstract]) AND "deep learning"[Title/Abstract]',
    '("single-cell RNA-seq"[Title/Abstract] OR "single-cell RNA sequencing"[Title/Abstract]) AND ("T cell"[Title/Abstract] OR "tumor microenvironment"[Title/Abstract])',
    '"pathological complete response"[Title/Abstract] AND neoadjuvant[Title/Abstract] AND (esophageal[Title/Abstract] OR gastric[Title/Abstract])',
]

# arXiv 检索式（医学影像 AI 方向的预印本，往往比正式发表早 3-6 个月）
ARXIV_QUERIES = [
    'all:"esophageal" AND all:"deep learning"',
    'all:"whole slide image"',
    'all:"radiomics"',
]

DAYS = int(os.environ.get("DAYS", "3"))      # 检索最近 N 天
TOP_N = int(os.environ.get("TOP_N", "5"))    # 推送条数
SENDKEY = os.environ.get("SENDKEY", "")       # Server酱 SendKey

# ==================== 工具函数 ====================


def http_get(url, timeout=30):
    """带 UA 的 GET 请求，返回 bytes。"""
    req = urllib.request.Request(url, headers={"User-Agent": "literature-push/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_pubmed():
    """检索 PubMed 最近 DAYS 天的新文献，返回 paper 字典列表。"""
    seen_pmids = set()
    papers = []

    for query in PUBMED_QUERIES:
        try:
            # esearch：拿最近 N 天的 PMID
            es_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
                "db=pubmed&retmode=json&retmax=30&sort=date"
                f"&reldate={DAYS}&datetype=pdat&term={urllib.parse.quote(query)}"
            )
            es = json.loads(http_get(es_url).decode("utf-8"))
            pmids = es.get("esearchresult", {}).get("idlist", [])
        except Exception as e:
            print(f"[warn] PubMed esearch 失败: {e}")
            continue

        if not pmids:
            continue

        # esummary：拿元数据（标题/期刊/日期/作者/DOI）
        try:
            su_url = (
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
                "db=pubmed&retmode=json&id=" + ",".join(pmids)
            )
            su = json.loads(http_get(su_url).decode("utf-8"))
            res = su.get("result", {})
        except Exception as e:
            print(f"[warn] PubMed esummary 失败: {e}")
            continue

        for pmid in pmids:
            if pmid in seen_pmids:
                continue
            seen_pmids.add(pmid)
            rec = res.get(pmid, {})
            if not rec:
                continue

            doi = ""
            for aid in rec.get("articleids", []):
                if aid.get("idtype") == "doi":
                    doi = aid.get("value", "")
                    break

            authors = [a.get("name", "") for a in rec.get("authors", []) if a.get("name")]
            first_author = authors[0] if authors else "Unknown"

            papers.append({
                "source": "PubMed",
                "title": rec.get("title", "No title").strip(),
                "journal": rec.get("fulljournalname") or rec.get("source", ""),
                "date": rec.get("pubdate", ""),
                "first_author": first_author,
                "doi": doi,
                "link": f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "query": query,
            })
        time.sleep(0.4)  # NCBI 限速，礼貌访问

    return papers


def fetch_arxiv():
    """检索 arXiv 最近提交的论文，返回 paper 字典列表。"""
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DAYS)

    for query in ARXIV_QUERIES:
        try:
            url = (
                "http://export.arxiv.org/api/query?"
                f"search_query={urllib.parse.quote(query)}&start=0&max_results=15"
                "&sortBy=submittedDate&sortOrder=descending"
            )
            tree = ET.fromstring(http_get(url).decode("utf-8"))
        except Exception as e:
            print(f"[warn] arXiv 检索失败: {e}")
            continue

        for entry in tree.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            published = entry.findtext("atom:published", default="", namespaces=ns)
            link = ""
            for l in entry.findall("atom:link", ns):
                if l.get("rel") == "alternate":
                    link = l.get("href", "")
                    break
            authors = [a.findtext("atom:name", default="", namespaces=ns) for a in entry.findall("atom:author", ns)]
            first_author = (authors[0] if authors else "Unknown").strip()

            # 只保留最近 DAYS 天提交的
            if published:
                try:
                    pub_dt = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if pub_dt < since:
                        continue
                except ValueError:
                    pass

            papers.append({
                "source": "arXiv",
                "title": title,
                "journal": "arXiv (preprint)",
                "date": published[:10] if published else "",
                "first_author": first_author,
                "doi": "",
                "link": link,
                "query": query,
            })

    return papers


def rank_papers(papers):
    """简单排序：同一篇去重（按标题小写），按日期新到旧排。"""
    dedup = {}
    for p in papers:
        key = p["title"].lower()
        if key not in dedup:
            dedup[key] = p
    ranked = sorted(dedup.values(), key=lambda x: x.get("date", ""), reverse=True)
    return ranked[:TOP_N]


def format_digest(papers):
    """把 top 文献拼成 Markdown 摘要。"""
    today = datetime.date.today().isoformat()
    lines = [f"## 📅 {today} 文献日报", "", f"最近 {DAYS} 天 · 共筛出 {len(papers)} 篇", ""]

    if not papers:
        lines.append("本期无新增匹配文献。")
        return "\n".join(lines)

    for i, p in enumerate(papers, 1):
        lines.append(f"### 🏅 #{i} {p['title']}")
        meta = f"{p['journal']}"
        if p["date"]:
            meta += f", {p['date']}"
        meta += f" | {p['first_author']} et al."
        lines.append(meta)
        if p["doi"]:
            lines.append(f"DOI: {p['doi']}")
        lines.append(f"📎 {p['link']}")
        lines.append("")

    lines.append("---")
    lines.append("_来源：PubMed / arXiv 自动检索 · 由 GitHub Actions 定时推送_")
    return "\n".join(lines)


def push_to_wechat(title, desp):
    """通过 Server酱推送到微信。"""
    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8")


def main():
    if not SENDKEY:
        print("[error] 缺少 SENDKEY 环境变量。本地测试请：SENDKEY=xxx python literature_push.py")
        sys.exit(1)

    print(f"[info] 开始检索，范围最近 {DAYS} 天 ...")
    pubmed_papers = fetch_pubmed()
    print(f"[info] PubMed 命中 {len(pubmed_papers)} 篇")
    arxiv_papers = fetch_arxiv()
    print(f"[info] arXiv 命中 {len(arxiv_papers)} 篇")

    all_papers = pubmed_papers + arxiv_papers
    top = rank_papers(all_papers)
    print(f"[info] 去重排序后推送 top {len(top)} 篇")

    digest = format_digest(top)
    title = f"📅 {datetime.date.today().isoformat()} 文献日报"
    resp = push_to_wechat(title, digest)
    print(f"[info] Server酱 响应: {resp[:200]}")


if __name__ == "__main__":
    main()
