#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文献日报推送脚本（PubMed + arXiv -> LLM 智能解读 -> Server酱 -> 手机微信）

由 GitHub Actions 每 3 天定时触发，也可本地手动运行测试：

    SENDKEY=你的SendKey LLM_API_KEY=你的Key python literature_push.py

环境变量：
    SENDKEY       Server酱 SendKey（必填）
    LLM_API_KEY   大模型 API Key（可选，配置后启用「一句话/方法/结果/点评」智能解读）
    LLM_BASE_URL  API 地址（默认 Moonshot Kimi：https://api.moonshot.cn/v1）
    LLM_MODEL     模型名（默认 moonshot-v1-8k；DeepSeek 用 deepseek-chat）
    DAYS          检索最近 N 天（默认 3）
    TOP_N         推送条数（默认 5）

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

DAYS = int(os.environ.get("DAYS", "3"))          # 检索最近 N 天
TOP_N = int(os.environ.get("TOP_N", "5"))        # 推送条数
SENDKEY = os.environ.get("SENDKEY", "")           # Server酱 SendKey

# LLM 智能解读配置（可选；空字符串回退默认值，避免未配 secret 时拿到空串）
LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or ""
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "") or "https://api.moonshot.cn/v1"
LLM_MODEL = os.environ.get("LLM_MODEL", "") or "moonshot-v1-8k"

# ==================== 工具函数 ====================


def http_get(url, timeout=30):
    """带 UA 的 GET 请求，返回 bytes。"""
    req = urllib.request.Request(url, headers={"User-Agent": "literature-push/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post_json(url, payload, api_key, timeout=90):
    """POST JSON 到 OpenAI 兼容接口，返回解析后的 dict。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_pubmed():
    """检索 PubMed 最近 DAYS 天的新文献，返回 paper 字典列表。"""
    seen_pmids = set()
    papers = []

    for query in PUBMED_QUERIES:
        try:
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
                "pmid": pmid,
                "title": rec.get("title", "No title").strip(),
                "journal": rec.get("fulljournalname") or rec.get("source", ""),
                "date": rec.get("pubdate", ""),
                "first_author": first_author,
                "doi": doi,
                "link": f"https://doi.org/{doi}" if doi else f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                "abstract": "",   # 稍后由 enrich_abstracts 填充
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

            if published:
                try:
                    pub_dt = datetime.datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if pub_dt < since:
                        continue
                except ValueError:
                    pass

            papers.append({
                "source": "arXiv",
                "pmid": "",
                "title": title,
                "journal": "arXiv (preprint)",
                "date": published[:10] if published else "",
                "first_author": first_author,
                "doi": "",
                "link": link,
                "abstract": summary,   # arXiv 的 summary 即摘要
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


def fetch_abstracts(pmids):
    """用 efetch 批量抓 PubMed 摘要，返回 {pmid: abstract_text}。"""
    if not pmids:
        return {}
    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
        "db=pubmed&rettype=abstract&retmode=xml&id=" + ",".join(pmids)
    )
    data = http_get(url).decode("utf-8")
    root = ET.fromstring(data)
    abstracts = {}
    for article in root.findall(".//PubmedArticle"):
        pmid = article.findtext(".//PMID", default="")
        parts = []
        for at in article.findall(".//Abstract/AbstractText"):
            label = at.get("Label")
            text = "".join(at.itertext()).strip()
            parts.append(f"{label}: {text}" if label else text)
        abstracts[pmid] = " ".join(parts).strip()
    return abstracts


def enrich_abstracts(papers):
    """给 top N 篇 PubMed 论文补 abstract 字段。"""
    pmids = [p["pmid"] for p in papers if p["source"] == "PubMed" and p["pmid"]]
    if not pmids:
        return
    try:
        abstracts = fetch_abstracts(pmids)
        for p in papers:
            if p["source"] == "PubMed":
                p["abstract"] = abstracts.get(p.get("pmid", ""), "")
    except Exception as e:
        print(f"[warn] 抓取摘要失败（不影响推送，仅无摘要文本）: {e}")


def summarize_with_llm(papers):
    """调用 LLM 为每篇论文生成中文解读，返回 {index: 解读dict}。失败抛异常。"""
    papers_text = []
    for i, p in enumerate(papers, 1):
        abs_text = (p.get("abstract") or "").strip() or "（无摘要）"
        if len(abs_text) > 1200:
            abs_text = abs_text[:1200] + "…"
        papers_text.append(
            f"[{i}] 标题：{p['title']}\n期刊：{p['journal']} | {p['date']}\n摘要：{abs_text}"
        )
    papers_block = "\n\n".join(papers_text)

    prompt = f"""你是医学影像 / 肿瘤（食管癌方向）领域的科研助手。下面是今天检索到的 {len(papers)} 篇论文。

请为每一篇生成简洁的中文解读，**严格只输出一个 JSON 对象**（不要任何多余文字、不要 markdown 代码块围栏），格式如下：

{{
  "papers": [
    {{
      "index": 1,
      "one_liner": "一句话：这篇做了什么、核心贡献是什么（15秒判断是否值得点开）",
      "methods": "方法：关键数据来源 / 模型 / 技术路线（一句话）",
      "key_results": "关键结果：具体数字或明确结论，禁止空泛",
      "comment": "点评：对食管鳞癌早诊 / 新辅助疗效 / 影像AI / 单细胞免疫这条研究主线的价值、局限、是否值得精读"
    }}
  ]
}}

index 必须与论文列表里的序号一一对应。

论文列表：
{papers_block}
"""

    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4000,
    }
    result = http_post_json(url, payload, LLM_API_KEY)
    content = result["choices"][0]["message"]["content"]

    # 容错解析：去掉可能的 markdown 围栏，截取首个 { 到末个 }
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`").strip()
        if content.lower().startswith("json"):
            content = content[4:]
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1 and end > start:
        content = content[start:end + 1]

    parsed = json.loads(content)
    insights = {}
    for item in parsed.get("papers", []):
        idx = item.get("index")
        if idx is not None:
            insights[int(idx)] = item
    return insights


def format_digest(papers, insights=None):
    """把 top 文献拼成 Markdown 摘要。insights 为 {index: 解读dict}，缺省则纯检索版。"""
    insights = insights or {}
    today = datetime.date.today().isoformat()
    has_ai = bool(insights)
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

        ins = insights.get(i)
        if ins:
            if ins.get("one_liner"):
                lines.append(f"💡 {ins['one_liner']}")
            if ins.get("methods"):
                lines.append(f"🔬 方法：{ins['methods']}")
            if ins.get("key_results"):
                lines.append(f"📊 关键结果：{ins['key_results']}")
            if ins.get("comment"):
                lines.append(f"🧭 点评：{ins['comment']}")

        lines.append(f"📎 {p['link']}")
        lines.append("")

    lines.append("---")
    footer = "来源：PubMed / arXiv 自动检索"
    footer += " · LLM 智能解读" if has_ai else ""
    footer += " · 由 GitHub Actions 定时推送"
    lines.append(f"_{footer}_")
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

    # 抓摘要（供 LLM 解读使用）
    enrich_abstracts(top)

    # LLM 智能解读（可选，失败回退纯检索版）
    insights = {}
    if LLM_API_KEY:
        try:
            insights = summarize_with_llm(top)
            print(f"[info] LLM 解读完成 {len(insights)} 篇")
        except Exception as e:
            print(f"[warn] LLM 解读失败，回退纯检索版: {e}")

    digest = format_digest(top, insights)
    title = f"📅 {datetime.date.today().isoformat()} 文献日报"
    resp = push_to_wechat(title, digest)
    print(f"[info] Server酱 响应: {resp[:200]}")


if __name__ == "__main__":
    main()
