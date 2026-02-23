#!/usr/bin/env python3
"""
Generate DeepWiki content from CLI and store it into backend wiki cache.

This script bypasses FE generation flow and talks directly to backend APIs:
1) /local_repo/structure (for local repos) to get file tree + README
2) /chat/completions/stream to get wiki structure XML
3) /chat/completions/stream per page to generate markdown content
4) /api/wiki_cache to persist generated wiki

Usage example:
  python3 scripts/generate_wiki_cli.py \
    --backend-url http://127.0.0.1:8001 \
    --repo-path /home/vietlubu/projects/bear/oneplat-service \
    --owner local --repo oneplat-service --repo-type local \
    --language vi --provider openai --model gpt-5.3-codex \
    --exclude-dir ./vendor/ --exclude-dir ./public/
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_base_url(url: str) -> str:
    return url.rstrip("/").removesuffix("/api")


def infer_repo_type(repo_input: str) -> str:
    if repo_input.startswith("/"):
        return "local"
    if re.match(r"^[a-zA-Z]:\\", repo_input):
        return "local"
    parsed = urllib.parse.urlparse(repo_input)
    host = (parsed.netloc or "").lower()
    if "gitlab" in host:
        return "gitlab"
    if "bitbucket" in host:
        return "bitbucket"
    if host:
        return "github"
    return "local"


def infer_owner_repo(repo_input: str, repo_type: str) -> Tuple[str, str]:
    if repo_type == "local":
        repo = os.path.basename(repo_input.rstrip("/")) or "local-repo"
        return "local", repo
    parsed = urllib.parse.urlparse(repo_input)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1].removesuffix(".git")
    return "unknown", "unknown"


def http_request(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    timeout_sec: int = 600,
) -> Tuple[int, str]:
    payload: Optional[bytes] = None
    headers = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url=url, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            return resp.getcode(), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        return e.code, text


def http_stream_chat(
    backend_url: str, request_body: Dict[str, Any], timeout_sec: int
) -> str:
    url = f"{normalize_base_url(backend_url)}/chat/completions/stream"
    code, text = http_request("POST", url, request_body, timeout_sec=timeout_sec)
    if code < 200 or code >= 300:
        raise RuntimeError(f"chat/completions/stream failed: HTTP {code} - {text[:500]}")
    return text


def fetch_local_repo_structure(
    backend_url: str, local_path: str, timeout_sec: int
) -> Tuple[str, str]:
    qs = urllib.parse.urlencode({"path": local_path})
    url = f"{normalize_base_url(backend_url)}/local_repo/structure?{qs}"
    code, text = http_request("GET", url, timeout_sec=timeout_sec)
    if code < 200 or code >= 300:
        raise RuntimeError(f"local_repo/structure failed: HTTP {code} - {text[:500]}")
    data = json.loads(text)
    return data.get("file_tree", "") or "", data.get("readme", "") or ""


def summarize_file_tree(file_tree: str, max_lines: int = 1800, max_chars: int = 90000) -> Tuple[str, bool]:
    lines = [line.strip() for line in file_tree.splitlines() if line.strip()]
    out: List[str] = []
    size = 0
    for line in lines:
        next_size = size + len(line) + 1
        if len(out) >= max_lines or next_size > max_chars:
            return "\n".join(out), True
        out.append(line)
        size = next_size
    return "\n".join(out), False


def summarize_readme(readme: str, max_chars: int = 18000) -> Tuple[str, bool]:
    if len(readme) <= max_chars:
        return readme, False
    return readme[:max_chars], True


def extract_wiki_xml(text: str) -> str:
    m = re.search(r"<wiki_structure\b[\s\S]*?</wiki_structure>", text, flags=re.IGNORECASE)
    if m:
        return m.group(0)
    decoded = html.unescape(text)
    m2 = re.search(r"<wiki_structure\b[\s\S]*?</wiki_structure>", decoded, flags=re.IGNORECASE)
    if m2:
        return m2.group(0)
    preview = re.sub(r"\s+", " ", text[:800]).strip()
    raise RuntimeError(f"No <wiki_structure> XML found in model response. Preview: {preview}")


def parse_wiki_structure(xml_text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", xml_text)
    root = ET.fromstring(cleaned)
    title = (root.findtext("title") or "Project Wiki").strip()
    description = (root.findtext("description") or "").strip()

    pages_parent = root.find("pages")
    page_elements: List[ET.Element] = []
    if pages_parent is not None:
        page_elements = [el for el in list(pages_parent) if el.tag == "page"]
    if not page_elements:
        page_elements = [el for el in root.findall(".//page") if el.find("title") is not None]

    pages: List[Dict[str, Any]] = []
    for idx, page_el in enumerate(page_elements, start=1):
        page_id = page_el.attrib.get("id") or f"page-{idx}"
        page_title = (page_el.findtext("title") or f"Page {idx}").strip()
        importance_raw = (page_el.findtext("importance") or "medium").strip().lower()
        importance = importance_raw if importance_raw in {"high", "medium", "low"} else "medium"

        file_paths = []
        for fp in page_el.findall(".//relevant_files/file_path"):
            if fp.text and fp.text.strip():
                file_paths.append(fp.text.strip())
        if not file_paths:
            for fp in page_el.findall(".//file_path"):
                if fp.text and fp.text.strip():
                    file_paths.append(fp.text.strip())
        file_paths = list(dict.fromkeys(file_paths))

        related_pages = []
        for rel in page_el.findall(".//related_pages/related"):
            if rel.text and rel.text.strip():
                related_pages.append(rel.text.strip())
        if not related_pages:
            for rel in page_el.findall(".//related"):
                if rel.text and rel.text.strip():
                    related_pages.append(rel.text.strip())
        related_pages = list(dict.fromkeys(related_pages))

        pages.append(
            {
                "id": page_id,
                "title": page_title,
                "content": "",
                "filePaths": file_paths,
                "importance": importance,
                "relatedPages": related_pages,
            }
        )

    if not pages:
        raise RuntimeError("Parsed XML but found no <page> entries in wiki structure.")

    return {
        "id": "wiki",
        "title": title,
        "description": description,
        "pages": pages,
        "sections": [],
        "rootSections": [],
    }


def build_structure_prompt(
    owner: str,
    repo: str,
    language: str,
    file_tree: str,
    readme: str,
    max_pages: int,
    tree_truncated: bool,
    readme_truncated: bool,
) -> str:
    return f"""Analyze this repository {owner}/{repo} and create a wiki structure.

1) File tree:
<file_tree>
{file_tree}
</file_tree>

{"NOTE: file tree was truncated for token limits." if tree_truncated else ""}

2) README:
<readme>
{readme}
</readme>

{"NOTE: README was truncated for token limits." if readme_truncated else ""}

IMPORTANT:
- Generate wiki in language: {language}
- Create 5-{max_pages} pages
- Output MUST be valid XML only
- Start with <wiki_structure> and end with </wiki_structure>

Format:
<wiki_structure>
  <title>...</title>
  <description>...</description>
  <pages>
    <page id="page-1">
      <title>...</title>
      <description>...</description>
      <importance>high|medium|low</importance>
      <relevant_files>
        <file_path>...</file_path>
      </relevant_files>
      <related_pages>
        <related>page-2</related>
      </related_pages>
    </page>
  </pages>
</wiki_structure>
"""


def build_page_prompt(title: str, language: str, file_paths: List[str]) -> str:
    paths_text = "\n".join(f"- {p}" for p in file_paths) if file_paths else "- (No file path provided)"
    return f"""Write a technical wiki page in Markdown.

Page title: {title}
Language: {language}
Relevant files:
{paths_text}

Rules:
- Start with '# {title}'
- Focus on architecture and behavior from relevant files
- Include clear sections (##, ###)
- Include source citations where possible, using format: Sources: [path]()
- Use mermaid diagrams when useful
- Return Markdown only
"""


def ensure_non_empty_content(text: str, context: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise RuntimeError(f"Empty response while generating {context}")
    if cleaned.startswith("Error:") or "Error with Openai API:" in cleaned:
        raise RuntimeError(f"Backend returned error for {context}: {cleaned[:500]}")
    return cleaned


def post_wiki_cache(
    backend_url: str,
    payload: Dict[str, Any],
    timeout_sec: int,
) -> None:
    url = f"{normalize_base_url(backend_url)}/api/wiki_cache"
    code, text = http_request("POST", url, payload, timeout_sec=timeout_sec)
    if code < 200 or code >= 300:
        raise RuntimeError(f"POST /api/wiki_cache failed: HTTP {code} - {text[:800]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate DeepWiki from CLI and store server cache.")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8001")
    parser.add_argument("--repo-path", required=True, help="Local folder path or git URL")
    parser.add_argument("--repo-type", default=None, help="local|github|gitlab|bitbucket")
    parser.add_argument("--owner", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--language", default="vi")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-5.3-codex")
    parser.add_argument("--token", default=None)
    parser.add_argument("--timeout", type=int, default=3600, help="Per request timeout (seconds)")
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--exclude-dir", action="append", default=[])
    parser.add_argument("--exclude-file", action="append", default=[])
    parser.add_argument("--include-dir", action="append", default=[])
    parser.add_argument("--include-file", action="append", default=[])
    args = parser.parse_args()

    backend_url = normalize_base_url(args.backend_url)
    repo_type = args.repo_type or infer_repo_type(args.repo_path)
    owner, repo = infer_owner_repo(args.repo_path, repo_type)
    owner = args.owner or owner
    repo = args.repo or repo

    if not owner or not repo:
        raise RuntimeError("Cannot determine owner/repo. Provide --owner and --repo explicitly.")

    log(f"[1/5] Preparing repository context for {owner}/{repo} ({repo_type})")
    if repo_type != "local":
        raise RuntimeError(
            "This CLI currently supports local path generation only. "
            "Use --repo-type local with --repo-path /absolute/path."
        )

    file_tree, readme = fetch_local_repo_structure(backend_url, args.repo_path, timeout_sec=args.timeout)
    tree_summary, tree_truncated = summarize_file_tree(file_tree)
    readme_summary, readme_truncated = summarize_readme(readme)

    structure_prompt = build_structure_prompt(
        owner=owner,
        repo=repo,
        language=args.language,
        file_tree=tree_summary,
        readme=readme_summary,
        max_pages=max(5, args.max_pages),
        tree_truncated=tree_truncated,
        readme_truncated=readme_truncated,
    )

    request_common: Dict[str, Any] = {
        "repo_url": args.repo_path,
        "type": repo_type,
        "provider": args.provider,
        "model": args.model,
        "language": args.language,
    }
    if args.token:
        request_common["token"] = args.token
    if args.exclude_dir:
        request_common["excluded_dirs"] = "\n".join(args.exclude_dir)
    if args.exclude_file:
        request_common["excluded_files"] = "\n".join(args.exclude_file)
    if args.include_dir:
        request_common["included_dirs"] = "\n".join(args.include_dir)
    if args.include_file:
        request_common["included_files"] = "\n".join(args.include_file)

    log("[2/5] Generating wiki structure XML")
    structure_body = dict(request_common)
    structure_body["messages"] = [{"role": "user", "content": structure_prompt}]
    structure_response = http_stream_chat(backend_url, structure_body, timeout_sec=args.timeout)
    xml_text = extract_wiki_xml(structure_response)
    wiki_structure = parse_wiki_structure(xml_text)
    pages = wiki_structure["pages"]
    log(f"Parsed {len(pages)} pages from wiki structure")

    log("[3/5] Generating page content")
    generated_pages: Dict[str, Dict[str, Any]] = {}
    for idx, page in enumerate(pages, start=1):
        page_title = page["title"]
        page_id = page["id"]
        file_paths = page.get("filePaths", [])
        log(f"  - ({idx}/{len(pages)}) {page_title}")
        page_prompt = build_page_prompt(page_title, args.language, file_paths)
        page_body = dict(request_common)
        page_body["messages"] = [{"role": "user", "content": page_prompt}]
        page_response = http_stream_chat(backend_url, page_body, timeout_sec=args.timeout)
        content = ensure_non_empty_content(page_response, f"page '{page_title}'")
        generated_pages[page_id] = {
            "id": page_id,
            "title": page_title,
            "content": content,
            "filePaths": file_paths,
            "importance": page.get("importance", "medium"),
            "relatedPages": page.get("relatedPages", []),
        }

    log("[4/5] Saving wiki cache to backend")
    cache_payload = {
        "repo": {
            "owner": owner,
            "repo": repo,
            "type": repo_type,
            "token": args.token,
            "localPath": args.repo_path,
            "repoUrl": None,
        },
        "language": args.language,
        "wiki_structure": wiki_structure,
        "generated_pages": generated_pages,
        "provider": args.provider,
        "model": args.model,
    }
    post_wiki_cache(backend_url, cache_payload, timeout_sec=args.timeout)

    log("[5/5] Done")
    log(
        "Open FE with query params:\n"
        f"  /{owner}/{repo}?type={repo_type}&local_path={urllib.parse.quote(args.repo_path)}&language={args.language}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log("Interrupted.")
        raise SystemExit(130)
    except Exception as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)

