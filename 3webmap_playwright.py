
#!/usr/bin/env python3
"""
webmap_playwright.py
Crawl a website (with JavaScript rendering via Playwright),
build a hierarchical tree from URL paths, and visualize it
as a collapsible left-to-right D3 tree.

Key behaviour:
- Headed Chromium (browser visible)
- Deep scroll per page
- Reuse same tab
- Click up to 50 clickable elements per page to discover routes
- Extract links from fully rendered HTML via BeautifulSoup
"""

import os
import json
import time
import argparse
import webbrowser
from collections import deque
from urllib.parse import urljoin, urlparse

import re
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError


# -------------------------------------------------------
# BASIC STATIC FETCH + LINK EXTRACT (FALLBACK)
# -------------------------------------------------------

def fetch_html(url, timeout=15):
    """Simple requests-based fetch used as a fallback when Playwright fails."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; WebMapBot/1.0)"}
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"[fallback-fetch-error] {url} -> {e}")
    return None


def extract_links_static(base_url, html):
    """Extract internal links from static HTML using BeautifulSoup."""
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    domain = urlparse(base_url).netloc

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc == domain:
            links.add(normalize(absolute))

    return links


# -------------------------------------------------------
# NORMALIZATION
# -------------------------------------------------------

def normalize(link: str) -> str:
    """Normalize URLs by stripping anchors, query params, and trailing slash."""
    if not link:
        return link
    link = link.split("#", 1)[0]
    link = link.split("?", 1)[0]
    return link.rstrip("/")


# -------------------------------------------------------
# UPGRADED PLAYWRIGHT CRAWLER
# -------------------------------------------------------

def crawl(start_url, max_pages=150, delay=0.3, max_clicks_per_page=50):
    """
    Crawl site using Playwright (headed Chromium, one tab reused).

    Per page:
      - Load with JS (domcontentloaded)
      - Deep scroll to trigger lazy loads
      - Extract links from fully rendered HTML using BeautifulSoup
      - Additionally inspect onclick / data-url attributes
      - Click up to `max_clicks_per_page` clickable elements
        to discover routes that only appear after interaction.

    Fallback to static HTML if Playwright completely fails.
    """
    visited = set()
    queue = deque([start_url])
    pages = {}
    domain = urlparse(start_url).netloc

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        tab = browser.new_page()

        while queue and len(pages) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue

            visited.add(url)
            print("\n[render]", url)

            all_links = set()
            page_ok = False

            # 1) Load page with Playwright
            try:
                tab.goto(url, timeout=60000, wait_until="domcontentloaded")
                page_ok = True
            except TimeoutError:
                print("[timeout]", url)
            except Exception as e:
                print("[playwright-error]", url, "->", e)

            if page_ok:
                # 2) Deep scroll to trigger lazy-loaded content
                try:
                    last_height = 0
                    for _ in range(20):
                        tab.mouse.wheel(0, 2500)
                        time.sleep(0.4)
                        new_height = tab.evaluate("document.body.scrollHeight")
                        if new_height == last_height:
                            break
                        last_height = new_height
                except Exception as e:
                    print("[scroll-error]", url, "->", e)

                time.sleep(0.5)

                # 3) Expand common menus (click + hover) before extracting links
                try:
                    expand_menus(tab)
                    time.sleep(0.5)
                except Exception as e:
                    print("[menu-expand-error]", url, "->", e)

                # 4) Extract links from fully rendered HTML via BeautifulSoup
                html_links = extract_links_from_dom(tab, domain)
                onclick_links, router_links = extract_js_nav_links(tab, url, domain)
                all_links |= html_links | onclick_links | router_links

                print(f"[links-basic] anchors={len(html_links)} onclick={len(onclick_links)} data-url={len(router_links)}")

                # 5) Click-discovery of navigation (up to N clicks)
                discovered_via_clicks = click_discover_links(
                    tab, url, domain, max_clicks=max_clicks_per_page
                )
                print(f"[links-click] discovered={len(discovered_via_clicks)}")
                all_links |= discovered_via_clicks

            # ---- Fallback path: Playwright unreachable AND no links ----
            if not all_links and not page_ok:
                html = fetch_html(url)
                if html:
                    print("[fallback-static]", url)
                    static_links = extract_links_static(url, html)
                    all_links = set(static_links)
                else:
                    print("[skip] no content available for", url)

            pages[url] = {"links": list(all_links)}

            # Queue discovered links
            for link in all_links:
                if link not in visited and len(pages) + len(queue) < max_pages * 2:
                    queue.append(link)

            time.sleep(delay)

        browser.close()

    return pages


def extract_links_from_dom(tab, domain):
    """
    Extract internal links from the fully rendered HTML using BeautifulSoup.

    This is more robust than querying DOM via JS because it sees the
    final HTML after Webflow/JS manipulations.
    """
    links = set()
    try:
        html = tab.content()
    except Exception as e:
        print("[dom-content-error]", "->", e)
        return links

    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(tab.url, href)
        parsed = urlparse(absolute)
        if parsed.netloc == domain:
            links.add(normalize(absolute))

    return links


def extract_js_nav_links(tab, base_url, domain):
    """
    Extract navigation URLs from onclick handlers and data-url attributes.
    Handles:
      - onclick="location.href('/path')"
      - onclick="goToPage('/path')" (generic function call with path)
      - data-url="/path"
    """
    onclick_links = set()
    router_links = set()

    # onclick handlers
    try:
        onclick_attrs = tab.eval_on_selector_all(
            "*[onclick]",
            "els => els.map(e => e.getAttribute('onclick'))"
        )
    except Exception as e:
        print("[onclick-extract-error]", "->", e)
        onclick_attrs = []

    for script in onclick_attrs:
        if not script:
            continue

        path_val = None

        # 1) Direct absolute or root-relative URL inside quotes
        m = re.search(r"['\"](https?://[^'\"]+)['\"]", script)
        if m:
            path_val = m.group(1)
        else:
            m = re.search(r"['\"](/[^'\"]+)['\"]", script)
            if m:
                path_val = m.group(1)

        # 2) Generic function call goToPage('/path')
        if not path_val:
            m = re.search(r"\((['\"])(/[^'\"]+)\1\)", script)
            if m:
                path_val = m.group(2)

        if path_val:
            full = urljoin(base_url, path_val)
            parsed = urlparse(full)
            if parsed.netloc == domain:
                onclick_links.add(normalize(full))

    # data-url-style attributes
    try:
        data_urls = tab.eval_on_selector_all(
            "*[data-url]",
            "els => els.map(e => e.getAttribute('data-url'))"
        )
    except Exception as e:
        print("[data-url-extract-error]", "->", e)
        data_urls = []

    for link in data_urls:
        if not link:
            continue
        full = urljoin(base_url, link)
        parsed = urlparse(full)
        if parsed.netloc == domain:
            router_links.add(normalize(full))

    return onclick_links, router_links


def expand_menus(tab):
    """
    Best-effort expansion of Webflow-style / nav menus using
    click + hover (M3 strategy).
    """
    try:
        # Click hamburger / nav toggles / dropdown toggles
        toggles = tab.query_selector_all(
            ".w-dropdown-toggle, .nav_humburg, .nav_humburg.home, .nav_humburg.no_gap"
        )
    except Exception:
        toggles = []

    for el in toggles:
        try:
            el.click(timeout=2000)
            time.sleep(0.2)
        except Exception:
            pass

    # Hover over dropdown toggles to open menus
    try:
        hover_targets = tab.query_selector_all(".w-dropdown, .w-dropdown-toggle")
    except Exception:
        hover_targets = []

    for el in hover_targets:
        try:
            tab.mouse.move(el.bounding_box()["x"] + 2, el.bounding_box()["y"] + 2)
            tab.wait_for_timeout(200)
        except Exception:
            pass


def click_discover_links(tab, base_url, domain, max_clicks=50):
    """
    Click up to `max_clicks` clickable elements on the page to discover
    new routes that only appear after interaction.

    Strategy:
      - Collect broadly clickable elements:
        a, button, [role=button], [onclick], [role=link], [role=menuitem]
      - Click each once, wait for possible navigation
      - If URL changes and stays on same domain, record it and go back
    """
    discovered = set()

    try:
        handles = tab.query_selector_all(
            "a, button, [role='button'], [onclick], [role='link'], [role='menuitem']"
        )
    except Exception as e:
        print("[click-targets-error]", "->", e)
        return discovered

    if not handles:
        return discovered

    print(f"[click-discover] candidates={len(handles)} (max {max_clicks})")
    clicks_done = 0

    for el in handles:
        if clicks_done >= max_clicks:
            break

        try:
            box = el.bounding_box()
            if not box:
                continue
        except Exception:
            continue

        before_url = tab.url
        nav_happened = False

        try:
            with tab.expect_navigation(wait_until="domcontentloaded", timeout=8000):
                el.click()
            nav_happened = True
        except TimeoutError:
            # Might be in-page JS, not full navigation
            if tab.url != before_url:
                nav_happened = True
        except Exception:
            pass

        clicks_done += 1

        if nav_happened:
            new_url = normalize(tab.url)
            parsed = urlparse(new_url)
            if parsed.netloc == domain and new_url != normalize(before_url):
                print("  [click-nav]", before_url, "->", new_url)
                discovered.add(new_url)

            # attempt to go back
            try:
                tab.go_back(timeout=8000, wait_until="domcontentloaded")
            except Exception:
                try:
                    tab.goto(before_url, timeout=60000, wait_until="domcontentloaded")
                except Exception as e:
                    print("[back-failed]", before_url, "->", e)
                    break

    return discovered


# -------------------------------------------------------
# BUILD TREE FROM URL PATHS
# -------------------------------------------------------

def build_tree(start_url, pages):
    """
    Build a hierarchical tree from URL paths.
    Domain is the root; each path segment becomes a node.
    """
    domain = urlparse(start_url).netloc

    url_by_path = {}
    for url in pages.keys():
        parsed = urlparse(url)
        norm = parsed.path.strip("/")
        url_by_path[norm] = url

    root = {
        "name": domain,
        "url": start_url,
        "children": [],
        "_path": ""
    }

    def find_or_create(parent, seg_path, label):
        for child in parent["children"]:
            if child["_path"] == seg_path:
                return child

        node_url = url_by_path.get(seg_path, None)
        new_child = {
            "name": label,
            "url": node_url,
            "children": [],
            "_path": seg_path,
        }
        parent["children"].append(new_child)
        return new_child

    for url in pages.keys():
        parsed = urlparse(url)
        norm_path = parsed.path.strip("/")
        if norm_path == "":
            continue

        segments = norm_path.split("/")
        node = root
        accumulated = []

        for seg in segments:
            accumulated.append(seg)
            seg_path = "/".join(accumulated)
            node = find_or_create(node, seg_path, seg)

        node["url"] = url

    # cleanup internal key
    def clean(n):
        n.pop("_path", None)
        for c in n.get("children", []):
            clean(c)

    clean(root)
    return root


# -------------------------------------------------------
# D3 COLLAPSIBLE TREE VIEWER (LEFT-TO-RIGHT)
# -------------------------------------------------------

def build_html(html_path, tree):
    os.makedirs(os.path.dirname(html_path), exist_ok=True)
    json_str = json.dumps(tree, indent=2, ensure_ascii=False)

    template = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Site Map Viewer</title>
<style>
  body {
    background:#0b1020;
    margin:0;
    overflow:hidden;
    font-family:Arial, sans-serif;
    color:white;
  }
  #tree {
    width:100%;
    height:100vh;
  }
  .node circle {
    stroke:white;
    stroke-width:1.3;
    cursor:pointer;
  }
  .node circle.leaf { fill:#1f6feb; }
  .node circle.internal { fill:#238636; }
  .node circle.collapsed { fill:#8e44ad; }
  .node text {
    font-size:12px;
    fill:#c9d1d9;
    cursor:pointer;
  }
  .link {
    fill:none;
    stroke:#8b949e;
    stroke-width:1.2px;
  }
  .tooltip {
    position:absolute;
    background:#111;
    color:white;
    padding:4px 8px;
    border:1px solid #444;
    border-radius:4px;
    opacity:0;
    pointer-events:none;
    font-size:11px;
    max-width:480px;
    z-index:10;
  }
  .message {
    position:absolute;
    top:50%;
    left:50%;
    transform:translate(-50%, -50%);
    color:#c9d1d9;
    font-size:18px;
    text-align:center;
  }
</style>
</head>
<body>
<div id="tree"></div>
<div id="tooltip" class="tooltip"></div>
<div id="message" class="message" style="display:none;"></div>

<script id="sitedata" type="application/json">
__JSON__
</script>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const raw = document.getElementById('sitedata').textContent;
const treeData = JSON.parse(raw);

if (!treeData.children || treeData.children.length === 0) {
  document.getElementById('message').style.display = 'block';
  document.getElementById('message').innerText =
    'No child pages found or crawl produced no hierarchy.\\n' +
    'Try increasing max-pages or checking crawler logs.';
} else {
  const width = window.innerWidth;
  const height = window.innerHeight;

  const svg = d3.select("#tree").append("svg")
    .attr("width", width)
    .attr("height", height);

  const g = svg.append("g").attr("transform", "translate(100,40)");
  const tooltip = document.getElementById("tooltip");

  const zoom = d3.zoom()
    .scaleExtent([0.2, 3])
    .on("zoom", e => g.attr("transform", e.transform));

  svg.call(zoom);

  let root = d3.hierarchy(treeData);
  root.x0 = height / 2;
  root.y0 = 0;

  if (root.children) {
    root.children.forEach(collapse);
  }

  update(root);

  function collapse(d) {
    if (d.children) {
      d._children = d.children;
      d._children.forEach(collapse);
      d.children = null;
    }
  }

  function prettyLabel(d) {
    if (!d.data || !d.data.name) return "";
    return d.data.name
      .replace(/[-_]+/g, " ")
      .replace(/\\b\\w/g, c => c.toUpperCase());
  }

  function update(source) {
    const treeLayout = d3.tree().nodeSize([30, 180]);
    treeLayout(root);

    const nodes = root.descendants();
    const links = root.links();

    nodes.forEach(d => d.y = d.depth * 200);

    const node = g.selectAll("g.node")
      .data(nodes, d => d.id || (d.id = Math.random()));

    const nodeEnter = node.enter().append("g")
      .attr("class", "node")
      .attr("transform", d => `translate(${source.y0},${source.x0})`)
      .on("click", (event, d) => {
        if (d.children) {
          d._children = d.children;
          d.children = null;
        } else {
          d.children = d._children;
          d._children = null;
        }
        update(d);
      })
      .on("dblclick", (event, d) => {
        if (d.data && d.data.url) {
          window.open(d.data.url, "_blank");
        }
        event.stopPropagation();
      })
      .on("mousemove", (event, d) => {
        if (!d.data || !d.data.url) {
          tooltip.style.opacity = 0;
          return;
        }
        tooltip.innerText = d.data.url;
        tooltip.style.left = (event.pageX + 12) + "px";
        tooltip.style.top = (event.pageY + 12) + "px";
        tooltip.style.opacity = 1;
      })
      .on("mouseleave", () => {
        tooltip.style.opacity = 0;
      });

    nodeEnter.append("circle")
      .attr("r", 1e-6)
      .attr("class", d => {
        if (d._children) return "internal collapsed";
        if (d.children) return "internal";
        return "leaf";
      });

    nodeEnter.append("text")
      .attr("dy", 3)
      .attr("x", 10)
      .text(d => prettyLabel(d));

    const nodeUpdate = nodeEnter.merge(node);

    nodeUpdate.transition()
      .duration(250)
      .attr("transform", d => `translate(${d.y},${d.x})`);

    nodeUpdate.select("circle")
      .attr("r", 6)
      .attr("class", d => {
        if (d._children) return "internal collapsed";
        if (d.children) return "internal";
        return "leaf";
      });

    const link = g.selectAll("path.link")
      .data(links, d => d.target.id);

    const linkEnter = link.enter().append("path")
      .attr("class", "link")
      .attr("d", d => {
        const o = {x: source.x0, y: source.y0};
        return diagonal({source: o, target: o});
      });

    const linkUpdate = linkEnter.merge(link);

    linkUpdate.transition()
      .duration(250)
      .attr("d", d => diagonal(d));

    link.exit().transition()
      .duration(250)
      .attr("d", d => {
        const o = {x: source.x, y: source.y};
        return diagonal({source: o, target: o});
      })
      .remove();

    nodes.forEach(d => {
      d.x0 = d.x;
      d.y0 = d.y;
    });
  }

  function diagonal(d) {
    return d3.linkHorizontal()
      .x(p => p.y)
      .y(p => p.x)(d);
  }

  window.addEventListener("resize", () => {
    const w = window.innerWidth;
    const h = window.innerHeight;
    svg.attr("width", w).attr("height", h);
  });
}
</script>
</body>
</html>
"""

    html = template.replace("__JSON__", json_str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("✔ HTML saved →", html_path)


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--max-pages", type=int, default=150)
    args = parser.parse_args()

    start_url = normalize(args.url)
    if not start_url.startswith("http://") and not start_url.startswith("https://"):
        start_url = "https://" + start_url

    print("[start-url]", start_url)

    pages = crawl(start_url, max_pages=args.max_pages)

    if not pages:
        print("⚠ No pages collected. Exiting without HTML generation.")
        return

    tree = build_tree(start_url, pages)

    os.makedirs("outputs", exist_ok=True)

    json_path = os.path.join("outputs", "tree_hybrid.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2)
    print("✔ JSON saved →", json_path)

    html_path = os.path.join("outputs", "site_map_view.html")
    build_html(html_path, tree)

    webbrowser.open("file://" + os.path.abspath(html_path))


if __name__ == "__main__":
    main()
