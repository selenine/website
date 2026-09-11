#!/usr/bin/env python3
"""
ssg.py — minimal static site generator

Usage:
  python ssg.py build          build site → docs/
  python ssg.py serve [port]   build + serve locally (default 8080)
  python ssg.py new "Title"    scaffold a new post

Source layout:
  config.yml                   site metadata + nav
  pages/about.md               static pages
  _posts/YYYY-MM-DD-slug/      post directory
    index.Rmd                  post content (YAML frontmatter + R Markdown)
    figures/                   knitr figure output (auto-generated)
    assets/                    static images, etc.

Markdown extras:
  :::theorem / :::proof / :::definition / :::remark / :::example / :::lemma
    → styled callout blocks (content is processed as markdown)
  $...$  and  $$...$$          → rendered by MathJax
  ```lang  fenced code         → syntax-highlighted via Pygments
"""

import datetime
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
from pathlib import Path

import markdown
import yaml
from markdown.extensions import Extension
from markdown.extensions.codehilite import CodeHiliteExtension
from markdown.extensions.fenced_code import FencedCodeExtension
from markdown.extensions.footnotes import FootnoteExtension
from markdown.extensions.md_in_html import MarkdownInHtmlExtension
from markdown.extensions.tables import TableExtension
from markdown.extensions.toc import TocExtension
from markdown.preprocessors import Preprocessor

ROOT = Path(__file__).parent
PAGES_DIR = ROOT / "pages"
THEME_DIR = ROOT / "theme"
OUT_DIR = ROOT / "docs"

# ── CSS ───────────────────────────────────────────────────────────────────────


def load_css():
    path = THEME_DIR / "theme.css"
    if path.exists():
        return path.read_text()
    raise FileNotFoundError("theme/theme.css not found")


# ── Markdown extension: fenced divs (:::class ... :::) ────────────────────────


class FencedDivPreprocessor(Preprocessor):
    """Convert :::class\n...\n::: to <div class='class' markdown='1'>...</div>."""

    OPEN = re.compile(r"^:::(\w+)\s*$")
    CLOSE = re.compile(r"^:::\s*$")

    def run(self, lines):
        new_lines, i = [], 0
        while i < len(lines):
            m = self.OPEN.match(lines[i])
            if m:
                cls = m.group(1)
                new_lines.append(f'<div class="{cls}" markdown="1">')
                new_lines.append("")
                i += 1
                depth = 1
                while i < len(lines) and depth > 0:
                    if self.OPEN.match(lines[i]):
                        depth += 1
                        new_lines.append(lines[i])
                    elif self.CLOSE.match(lines[i]):
                        depth -= 1
                        if depth > 0:
                            new_lines.append(lines[i])
                    else:
                        new_lines.append(lines[i])
                    i += 1
                new_lines.append("")
                new_lines.append("</div>")
            else:
                new_lines.append(lines[i])
                i += 1
        return new_lines


class FencedDivExtension(Extension):
    def extendMarkdown(self, md):
        md.preprocessors.register(FencedDivPreprocessor(md), "fenced_div", 175)


# ── Math protection ────────────────────────────────────────────────────────────

_PLACEHOLDER = "SSGMATH{}ENDMATH"


def protect_math(text):
    """Replace LaTeX math with placeholders so markdown can't mangle it."""
    stash = []

    def store_display(m):
        stash.append(m.group(0))
        # Surround display math with blank lines so it becomes its own block.
        # Otherwise the nl2br extension turns the newlines flanking the
        # collapsed placeholder into <br>, forcing a stray line break after
        # every display equation.
        return f"\n\n{_PLACEHOLDER.format(len(stash) - 1)}\n\n"

    def store_inline(m):
        stash.append(m.group(0))
        return _PLACEHOLDER.format(len(stash) - 1)

    text = re.sub(r"\$\$[\s\S]+?\$\$", store_display, text)
    text = re.sub(r"\$[^\$\n]+?\$", store_inline, text)
    return text, stash


def restore_math(html, stash):
    for i, orig in enumerate(stash):
        html = html.replace(_PLACEHOLDER.format(i), orig)
    return html


# ── Markdown rendering ─────────────────────────────────────────────────────────


def make_md():
    return markdown.Markdown(
        extensions=[
            FencedCodeExtension(),
            FencedDivExtension(),
            TableExtension(),
            TocExtension(permalink=False),
            FootnoteExtension(),
            CodeHiliteExtension(css_class="codehilite", guess_lang=False),
            MarkdownInHtmlExtension(),
            "nl2br",
        ]
    )


def render_markdown(text):
    text, stash = protect_math(text)
    md = make_md()
    html = md.convert(text)
    html = restore_math(html, stash)
    return html, md.toc


# ── Rmd support ───────────────────────────────────────────────────────────────


def knit_rmd(rmd_file: Path) -> Path:
    """Run knitr on an .Rmd file and return the path to the output .md file.

    HTML widgets (plotly, leaflet, etc.) are saved as PNG screenshots via
    webshot2 — install it in R with: install.packages('webshot2')
    Figures go into figures/ inside the post directory.
    """
    out_md = rmd_file.parent / (rmd_file.stem + ".knit.md")
    r_script = (
        "knitr::opts_chunk$set("
        "  dev = 'png',"
        "  fig.path = 'figures/',"
        "  screenshot.opts = list(delay = 2)"
        ");"
        f"knitr::knit('{rmd_file.name}', output = '{out_md.name}')"
    )
    result = subprocess.run(
        ["Rscript", "--vanilla", "-e", r_script],
        cwd=rmd_file.parent,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"knitr failed for {rmd_file.name}:\n{result.stderr}")
    text = out_md.read_text()
    out_md.unlink()
    return text


def extract_asides(html: str):
    """Pull <aside>…</aside> and <div class="aside">…</div> out of html.

    Returns (cleaned_html, [aside_html, ...]).
    """
    asides = []

    def _collect(m):
        asides.append(m.group(0))
        return ""

    html = re.sub(r"<aside\b[^>]*>.*?</aside>", _collect, html, flags=re.DOTALL)
    html = re.sub(
        r'<div\s+class=["\']aside["\'][^>]*>.*?</div>',
        _collect,
        html,
        flags=re.DOTALL,
    )
    return html, asides


def wrap_details(html: str) -> str:
    def _replace(m):
        summary = m.group(1)
        body = m.group(2)
        return f'<details>{summary}<div class="details-body">{body}</div></details>'

    return re.sub(
        r"<details\b[^>]*>(.*?</summary>)(.*?)</details>",
        _replace,
        html,
        flags=re.DOTALL,
    )


def number_asides(html: str) -> str:
    counter = [0]

    def _sub(m):
        counter[0] += 1
        n = counter[0]
        return (
            f'<sup class="aside-ref">{n}</sup>'
            f'<span class="aside-note"><span class="aside-num">{n}</span>{m.group(1)}</span>'
        )

    return re.sub(r"<aside\b[^>]*>(.*?)</aside>", _sub, html, flags=re.DOTALL)


_BLOCK_IMG_RE = re.compile(r"<p>\s*(<img\b[^>]*?>)\s*</p>")


def inline_figures(html: str, base_dir: Path) -> str:
    """Turn a standalone block image into a <figure> with a <figcaption> built
    from its alt text, so the caption renders visibly.

    A local .svg marked theme-aware (a `data-themed` attribute on its root) is
    inlined — its markup spliced into the page — so it inherits the page theme
    via style.css. Other images stay as <img> (grayscale diagrams rely on the
    light-mode invert filter, which would be lost if inlined).
    """

    def _repl(m):
        img = m.group(1)
        alt_m = re.search(r'alt="([^"]*)"', img)
        src_m = re.search(r'src="([^"]*)"', img)
        alt = alt_m.group(1) if alt_m else ""
        src = src_m.group(1) if src_m else ""
        caption = f"<figcaption>{alt}</figcaption>" if alt else ""

        if src.lower().endswith(".svg") and not src.startswith(
            ("http://", "https://", "//")
        ):
            svg_path = base_dir / src
            if svg_path.exists():
                svg = svg_path.read_text().strip()
                if "data-themed" in svg:
                    # drop any XML prolog so the markup splices cleanly into HTML
                    svg = re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
                    return f'<figure class="figure--svg">{svg}{caption}</figure>'

        return f"<figure>{img}{caption}</figure>"

    return _BLOCK_IMG_RE.sub(_repl, html)


def has_math(text):
    return bool(re.search(r"\$", text))


# ── Frontmatter ───────────────────────────────────────────────────────────────


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        meta = yaml.safe_load(raw) or {}
    except Exception:
        meta = {}
    return meta, body


# ── Config ────────────────────────────────────────────────────────────────────


def load_config():
    cfg_file = ROOT / "config.yml"
    cfg = {}
    if cfg_file.exists():
        with open(cfg_file) as f:
            cfg = yaml.safe_load(f) or {}
    cfg.setdefault("title", "untitled")
    cfg.setdefault("description", "")
    cfg.setdefault("author", "")
    cfg.setdefault("base_url", "")
    cfg.setdefault(
        "nav",
        [
            {"text": "Home", "href": "index.html"},
            {"text": "About", "href": "about.html"},
        ],
    )
    return cfg


# ── HTML templates ─────────────────────────────────────────────────────────────


def nav_items_html(cfg, root_rel):
    items = ""
    for item in cfg["nav"]:
        href = root_rel + item["href"]
        items += f'<li><a href="{href}">{item["text"]}</a></li>'
    return items


_BASE_TEMPLATE = None


def _load_base_template():
    global _BASE_TEMPLATE
    if _BASE_TEMPLATE is None:
        _BASE_TEMPLATE = (THEME_DIR / "base.html").read_text()
    return _BASE_TEMPLATE


def render_page(title, body_html, cfg, root_rel="", math=False, page_id=""):
    math_snippet = ""
    if math:
        math_snippet = (
            '<script>MathJax={tex:{inlineMath:[["$","$"],["\\\\(","\\\\)"]]},'
            'options:{skipHtmlTags:["script","noscript","style","textarea","pre"]}};</script>'
            '<script id="MathJax-script" async '
            'src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>'
        )
    site_title = cfg["title"]
    full_title = f"{title} — {site_title}" if title != site_title else site_title
    brand = site_title
    year = datetime.date.today().year
    author = cfg.get("author", "")
    footer_left = f"&copy; {year}"
    if author:
        footer_left += f" &middot; {author}"
    footer_links = "".join(
        f'<a href="{root_rel}{item["href"]}">{item["text"].lower()}</a>'
        for item in cfg.get("nav", [])
    )
    return (
        _load_base_template()
        .replace("%%TITLE%%", full_title)
        .replace("%%ROOT_REL%%", root_rel)
        .replace("%%MATH%%", math_snippet)
        .replace("%%NAV_BRAND%%", brand)
        .replace("%%NAV_ITEMS%%", nav_items_html(cfg, root_rel))
        .replace("%%BODY%%", body_html)
        .replace("%%FOOTER_LEFT%%", footer_left)
        .replace("%%FOOTER_LINKS%%", footer_links)
        .replace("%%PAGE_ID%%", page_id)
    )


# ── Utilities ─────────────────────────────────────────────────────────────────


def read_time(text):
    """Estimate reading time in minutes (200 wpm)."""
    words = len(re.findall(r"\w+", text))
    minutes = max(1, round(words / 200))
    return f"{minutes} min read"


def tags_html(tags):
    if not tags:
        return ""
    if isinstance(tags, str):
        tags = tags.split()
    chips = "".join(f'<span class="tag">{t}</span>' for t in tags)
    return f'<div class="tag-list">{chips}</div>'


def projects_grid_html(projects, full=False):
    """Render a grid of project cards. full=True for the standalone projects page."""
    cards = ""
    for proj in projects:
        name = proj.get("name", "")
        desc = proj.get("description", "")
        href = proj.get("href", "#")
        lang = proj.get("lang", "")
        links = proj.get("links", [])

        lang_tag = f'<span class="card-tag">{lang}</span>' if lang else ""

        gh_label = ""
        for lnk in links:
            if "github" in lnk.get("href", "").lower():
                gh_label = '<span class="card-gh-link">GitHub ↗</span>'
                break

        footer = (
            f'<div class="card-footer">{lang_tag}{gh_label}</div>'
            if (lang_tag or gh_label)
            else ""
        )
        card_class = "project-card project-card--full" if full else "project-card"
        cards += (
            f'<a class="{card_class}" href="{href}" target="_blank" rel="noopener">'
            f"<h3>{name}</h3>"
            f'<p class="card-desc">{desc}</p>'
            f"{footer}"
            f"</a>"
        )
    grid_class = "projects-page-grid" if full else "projects-grid"
    return f'<div class="{grid_class}">{cards}</div>'


# ── Post discovery ─────────────────────────────────────────────────────────────


def date_from_slug(slug):
    """Extract date from YYYY-MM-DD-slug or return None."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", slug)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return None


def find_posts():
    """Return list of post dicts sorted newest first.

    Posts are dated subdirectories inside pages/ (name starts with YYYY-MM-DD).
    """
    posts = []
    if not PAGES_DIR.exists():
        return posts

    for entry in PAGES_DIR.iterdir():
        if not entry.is_dir():
            continue
        slug = entry.name
        if not re.match(r"^\d{4}-\d{2}-\d{2}", slug):
            continue

        rmd_file = entry / "index.Rmd"
        if rmd_file.exists():
            source_file = rmd_file
        else:
            candidates = sorted(entry.glob("*.Rmd"))
            if not candidates:
                continue
            source_file = candidates[0]

        text = source_file.read_text()
        meta, _ = parse_frontmatter(text)

        date = None
        if "date" in meta:
            raw = meta["date"]
            if isinstance(raw, datetime.date):
                date = raw
            else:
                try:
                    date = datetime.date.fromisoformat(str(raw)[:10])
                except ValueError:
                    pass
        if date is None:
            date = date_from_slug(slug) or datetime.date(1970, 1, 1)

        posts.append(
            {
                "slug": slug,
                "path": source_file,
                "dir": source_file.parent,
                "meta": meta,
                "date": date,
            }
        )

    posts.sort(key=lambda p: p["date"], reverse=True)
    return posts


def out_path_for_post(post):
    """Return (out_dir, root_rel) for a post."""
    slug = post["slug"]
    out_dir = OUT_DIR / "posts" / slug
    return out_dir, "../../"


# ── Builders ──────────────────────────────────────────────────────────────────


def build_post(post, cfg, older=None, newer=None):
    text = knit_rmd(post["path"])
    meta, body = parse_frontmatter(text)

    content_html, toc_html = render_markdown(body)
    content_html = inline_figures(content_html, post["dir"])
    content_html = number_asides(content_html)
    content_html = wrap_details(content_html)
    math = has_math(body)

    title = meta.get("title", post["slug"])
    subtitle = meta.get("description", meta.get("subtitle", ""))
    author = meta.get("author", cfg.get("author", ""))
    if isinstance(author, list):
        author = ", ".join(a["name"] if isinstance(a, dict) else a for a in author)
    date_str = post["date"].strftime("%b %-d, %Y") if post["date"].year > 1970 else ""
    rtime = read_time(body)

    byline_parts = [x for x in [date_str, rtime] if x]
    sep = '<span class="sep">&middot;</span>'
    byline_html = sep.join(f"<span>{p}</span>" for p in byline_parts)
    if author:
        byline_html = f"<span>{author}</span>{sep}" + byline_html

    header = '<header class="post-header">'
    header += '<div class="post-header-body">'
    header += f"<h1>{title}</h1>"
    if subtitle:
        header += f'<p class="subtitle">{subtitle}</p>'
    if byline_html:
        header += f'<p class="byline">{byline_html}</p>'
    header += tags_html(meta.get("tags", []))
    header += "</div></header>"

    toc_sidebar = ""
    if toc_html and toc_html.strip():
        toc_sidebar = (
            f'<aside class="post-toc">'
            f'<div class="toc-inner">'
            f'<p class="toc-label">Contents</p>'
            f"{toc_html}"
            f"</div>"
            f"</aside>"
        )
    post_nav = ""
    if older or newer:
        older_html = ""
        newer_html = ""
        if older:
            older_title = older["meta"].get("title", older["slug"])
            older_href = f"../{older['slug']}/index.html"
            older_html = (
                f'<a class="post-nav-item post-nav-older" href="{older_href}">'
                f'<span class="post-nav-label">← Previous</span>'
                f'<span class="post-nav-title">{older_title}</span>'
                f"</a>"
            )
        if newer:
            newer_title = newer["meta"].get("title", newer["slug"])
            newer_href = f"../{newer['slug']}/index.html"
            newer_html = (
                f'<a class="post-nav-item post-nav-newer" href="{newer_href}">'
                f'<span class="post-nav-label">Next →</span>'
                f'<span class="post-nav-title">{newer_title}</span>'
                f"</a>"
            )
        post_nav = f'<nav class="post-nav">{older_html}{newer_html}</nav>'

    toc_toggle = ""
    if toc_sidebar:
        toc_toggle = '<button class="toc-toggle" id="toc-toggle" aria-label="Table of contents" aria-expanded="false">§</button>'

    body_html = (
        f'<div class="post-layout">'
        f"{toc_sidebar}"
        f'<article class="post-wrap">{header}<div class="post-body">{content_html}</div>{post_nav}</article>'
        f'<div class="post-asides"></div>'
        f"</div>"
        f"{toc_toggle}"
    )

    out_dir, root_rel = out_path_for_post(post)
    out_dir.mkdir(parents=True, exist_ok=True)

    _skip_names = {post["path"].name}

    for asset in post["dir"].iterdir():
        if asset.name in _skip_names:
            continue
        dst = out_dir / asset.name
        if asset.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(asset, dst)
        else:
            shutil.copy2(asset, dst)

    html = render_page(
        title, body_html, cfg, root_rel=root_rel, math=math, page_id="post"
    )
    (out_dir / "index.html").write_text(html)
    print(f"  post  → posts/{post['slug']}/index.html")


def build_index(posts, cfg):
    hero = cfg.get("hero", {})
    hero_html = ""
    if hero:
        h_title = hero.get("title", cfg["title"])
        h_tagline = hero.get("tagline", hero.get("subtitle", ""))
        kicker = hero.get("kicker", "whoami")
        email = cfg.get("email", "")
        # accent the comparison operator in a title like "p > 0.05"
        title_html = h_title.replace(">", '<span class="hero-op">&gt;</span>')
        hero_html = (
            '<section class="content-width hero">'
            f'<div class="hero-kicker">{kicker}</div>'
            f'<h1><span class="hero-type">{title_html}</span></h1>'
        )
        if h_tagline:
            hero_html += f'<p class="hero-tagline">{h_tagline}</p>'
        hero_html += (
            '<div class="hero-actions">'
            '<a class="btn btn--primary" href="projects.html">projects <span class="btn-arrow">→</span></a>'
            '<a class="btn btn--ghost" href="blog.html">read the blog →</a>'
        )
        if email:
            hero_html += (
                f'<a class="btn-inline" href="mailto:{email}">or email me ↗</a>'
            )
        hero_html += "</div></section>"

    items = ""
    total = len(posts)
    for i, p in enumerate(posts[:3]):
        num = total - i
        title = p["meta"].get("title", p["slug"])
        desc = p["meta"].get("description", p["meta"].get("subtitle", ""))
        date_str = p["date"].strftime("%b %-d, %Y") if p["date"].year > 1970 else ""
        rtime = read_time(p["path"].read_text())
        post_tags = p["meta"].get("tags", [])
        href = f"posts/{p['slug']}/index.html"
        sep = '<span class="sep">&middot;</span>'
        meta_parts = [x for x in [date_str, rtime] if x]
        meta_html = sep.join(f"<span>{x}</span>" for x in meta_parts)
        items += f'''
<a class="post-item" href="{href}">
  <span class="post-num">{num:02d}</span>
  <div class="post-item-body">
    <div class="post-meta">{meta_html}</div>
    <h3>{title}</h3>
    {f'<p class="post-desc">{desc}</p>' if desc else ""}
    {tags_html(post_tags)}
  </div>
</a>'''

    section_label = cfg.get("posts_label", "Latest Posts")
    section_hdr = f'<div class="section-row"><span class="section-label">{section_label}</span><a class="section-link" href="blog.html">all →</a></div>'
    posts_col = f'<div class="posts-section">{section_hdr}<div class="posts-list">{items}</div></div>'

    projects = cfg.get("projects", [])
    projects_col = ""
    if projects:
        grid_html = projects_grid_html(projects[:4], full=False)
        projects_col = (
            f'<div class="projects-col">'
            f'<div class="section-row">'
            f'<span class="section-label">Projects</span>'
            f'<a class="section-link" href="projects.html">all →</a>'
            f"</div>"
            f"{grid_html}"
            f"</div>"
        )

    index_grid = f'<div class="content-width"><div class="index-grid">{posts_col}{projects_col}</div></div>'
    body = hero_html + index_grid
    html = render_page(cfg["title"], body, cfg, root_rel="", page_id="index")
    (OUT_DIR / "index.html").write_text(html)
    print("  index → index.html")


def build_page(rmd_file, cfg, posts=None):
    """Build a static page from pages/name.md."""
    text = rmd_file.read_text()
    meta, body = parse_frontmatter(text)

    content_html, _ = render_markdown(body)
    math = has_math(body)
    title = meta.get("title", rmd_file.stem.replace("-", " ").title())

    if meta.get("layout") == "projects":
        intro = f'<div class="page-intro">{content_html}</div>' if body.strip() else ""
        grid = projects_grid_html(cfg.get("projects", []), full=True)
        kicker = '<div class="page-kicker">// things I build</div>'
        body_html = f'<div class="page-wrap page-wrap--wide">{kicker}<h1 class="page-title">{title}</h1>{intro}{grid}</div>'
    elif meta.get("layout") == "blog":
        items = ""
        _blog_posts = posts or []
        _blog_total = len(_blog_posts)
        featured_html = ""
        for i, p in enumerate(_blog_posts):
            num = _blog_total - i
            ptitle = p["meta"].get("title", p["slug"])
            desc = p["meta"].get("description", p["meta"].get("subtitle", ""))
            date_str = p["date"].strftime("%b %-d, %Y") if p["date"].year > 1970 else ""
            rtime = read_time(p["path"].read_text())
            post_tags = p["meta"].get("tags", [])
            href = f"posts/{p['slug']}/index.html"
            sep = '<span class="sep">&middot;</span>'
            meta_parts = [x for x in [date_str, rtime] if x]
            meta_html = sep.join(f"<span>{x}</span>" for x in meta_parts)
            if i == 0:
                featured_html = f'''
<a class="post-featured" href="{href}">
  <div>
    <div class="post-featured-kicker"><span class="section-label">latest</span><span class="post-featured-meta">{meta_html}</span></div>
    <h2>{ptitle}</h2>
    {f'<p class="post-featured-desc">{desc}</p>' if desc else ""}
    <span class="post-featured-cta">read the post &rarr;</span>
  </div>
</a>'''
                continue
            items += f'''
<a class="post-item" href="{href}">
  <span class="post-num">{num:02d}</span>
  <div class="post-item-body">
    <div class="post-meta">{meta_html}</div>
    <h3>{ptitle}</h3>
    {f'<p class="post-desc">{desc}</p>' if desc else ""}
    {tags_html(post_tags)}
  </div>
</a>'''
        intro = f'<div class="page-intro">{content_html}</div>' if body.strip() else ""
        kicker = f'<div class="page-kicker">// {_blog_total} post{"" if _blog_total == 1 else "s"} · newest first</div>'
        header_row = (
            f'<div class="page-header-row">'
            f'<div>{kicker}<h1 class="page-title">{title}</h1></div>'
            f"</div>"
        )
        body_html = (
            f'<div class="page-wrap">'
            f"{header_row}"
            f"{intro}"
            f"{featured_html}"
            f'<div class="posts-list">{items}</div>'
            f"</div>"
        )
    else:
        kicker = f'<div class="page-kicker">// {title.lower()}</div>'
        body_html = f'<div class="page-wrap">{kicker}<h1 class="page-title">{title}</h1>{content_html}</div>'

    html = render_page(
        title, body_html, cfg, root_rel="", math=math, page_id=rmd_file.stem
    )

    out_name = rmd_file.stem + ".html"
    (OUT_DIR / out_name).write_text(html)
    print(f"  page  → {out_name}")


# ── Build ─────────────────────────────────────────────────────────────────────


def cmd_build():
    cfg = load_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Write CSS
    (OUT_DIR / "style.css").write_text(load_css())
    print("  css   → style.css")

    # Copy theme assets (js, etc.) to output root
    for item in THEME_DIR.iterdir():
        if item.suffix in (".js",):
            shutil.copy2(item, OUT_DIR / item.name)
    print("  theme → copied")

    # Find posts first so blog page can reference them
    posts = find_posts()

    # Write search index (posts + projects) for cmd+k search
    search_items = []
    for p in posts:
        search_items.append(
            {
                "title": p["meta"].get("title", p["slug"]),
                "url": f"posts/{p['slug']}/index.html",
                "kind": "post",
            }
        )
    for proj in cfg.get("projects", []):
        search_items.append(
            {
                "title": proj.get("name", ""),
                "url": proj.get("href", proj.get("url", "#")),
                "kind": "project",
            }
        )
    (OUT_DIR / "search-index.js").write_text(
        "window.__SEARCH_INDEX__ = " + json.dumps(search_items) + ";"
    )
    print("  index → search-index.js")

    # Build pages
    if PAGES_DIR.exists():
        for rmd_file in sorted(PAGES_DIR.glob("*.md")):
            build_page(rmd_file, cfg, posts=posts)

    # Build posts (sorted newest-first, so older = higher index, newer = lower index)
    for i, post in enumerate(posts):
        build_post(
            post,
            cfg,
            older=posts[i + 1] if i + 1 < len(posts) else None,
            newer=posts[i - 1] if i > 0 else None,
        )

    # Build index
    build_index(posts, cfg)

    print(f"\nBuilt {len(posts)} post(s) → {OUT_DIR}/")


# ── Serve ─────────────────────────────────────────────────────────────────────


def cmd_serve(port=8080):
    cmd_build()
    os.chdir(OUT_DIR)
    handler = http.server.SimpleHTTPRequestHandler

    class QuietHandler(handler):
        def log_message(self, fmt, *args):
            pass  # suppress request logs

    with socketserver.TCPServer(("", port), QuietHandler) as httpd:
        print(f"\nServing at http://localhost:{port}/  (Ctrl+C to stop)\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


# ── New post scaffold ─────────────────────────────────────────────────────────


def cmd_new(title):
    today = datetime.date.today().isoformat()
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    name = f"{today}-{slug}"
    post_dir = PAGES_DIR / name
    post_dir.mkdir(parents=True, exist_ok=True)
    rmd_file = post_dir / "index.Rmd"
    if rmd_file.exists():
        print(f"Already exists: {rmd_file}")
        return
    rmd_file.write_text(f"""---
title: "{title}"
description: |
  One-line summary of the post.
author: ""
date: {today}
tags: []
---

Write your post here.

## Section heading

Body text. Inline math: $E = mc^2$. Display math:

$$
\\int_0^\\infty e^{{-x^2}} \\, dx = \\frac{{\\sqrt{{\\pi}}}}{{2}}
$$

```{{r example-plot, echo=FALSE}}
# R code chunks work here; JS widgets become PNG via webshot2
```

:::theorem
State a theorem here. *Markdown* works inside.
:::

:::proof
Proof follows.
:::
""")
    print(f"Created: {rmd_file}")


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]
    if not args or args[0] == "build":
        cmd_build()
    elif args[0] == "serve":
        port = int(args[1]) if len(args) > 1 else 8080
        cmd_serve(port)
    elif args[0] == "new":
        if len(args) < 2:
            print('Usage: python ssg.py new "Post Title"')
            sys.exit(1)
        cmd_new(" ".join(args[1:]))
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
