
#!/usr/bin/env python3
"""
final_webmap.py

Full integrated crawler + URL-path tree builder + D3 HTML viewer.

Usage:
    python final_webmap.py --url https://python.org --max-pages 50

Behavior:
- Uses Playwright (headed, browser visible)
- Scrolls and expands basic menus
- Extracts internal links from fully rendered HTML (BeautifulSoup)
- Builds hierarchical tree based on URL path depth (T3)
- Labels nodes with path only (e.g. "/downloads", "/downloads/windows")
- Root node label style: "domain/"
- Saves:
    outputs/sitemap_hier.json   (adjacency: url -> links)
    outputs/tree_final.json     (hierarchical tree)
    outputs/sitemap_tree_final.html  (D3 interactive view)
"""

import os
import json
import time
import argparse
import webbrowser
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError


# -------------------------------------------------------
# UTILS
# -------------------------------------------------------

def normalize(url: str) -> str:
    """Strip fragment, query, and trailing slash (except for bare domain)."""
    if not url:
        return url
    url = url.split("#", 1)[0]
    url = url.split("?", 1)[0]
    # Don't strip the single trailing slash off "https://example.com/"
    parts = url.split("://", 1)
    if len(parts) == 2:
        scheme, rest = parts
        if rest.endswith("/") and rest.count("/") > 1:
            rest = rest.rstrip("/")
        url = scheme + "://" + rest
    else:
        if url.endswith("/") and url.count("/") > 1:
            url = url.rstrip("/")
    return url


def root_domain(url: str) -> str:
    """Return root domain without leading www."""
    p = urlparse(url)
    net = p.netloc.lower()
    return net[4:] if net.startswith("www.") else net


# -------------------------------------------------------
# LINK EXTRACTION FROM RENDERED HTML
# -------------------------------------------------------

def extract_links_from_dom(tab, root_dom: str):
    """Extract internal links from rendered HTML via BeautifulSoup."""
    links = set()
    try:
        html = tab.content()
    except Exception as e:
        print("[dom-error]", e)
        return links

    soup = BeautifulSoup(html, "html.parser")

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        if href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        full = urljoin(tab.url, href)
        parsed = urlparse(full)
        netloc = parsed.netloc.lower()
        netcmp = netloc[4:] if netloc.startswith("www.") else netloc

        if netcmp.endswith(root_dom):
            links.add(normalize(full))

    return links


# -------------------------------------------------------
# MENU EXPANSION (GENERIC, LIGHTWEIGHT)
# -------------------------------------------------------

def expand_menus(tab):
    """Best-effort generic menu expansion (click + small waits)."""
    selectors = [
        ".w-dropdown-toggle",
        "button",
        "nav",
        ".menu",
        ".navbar",
        ".nav_humburg",
    ]
    for sel in selectors:
        try:
            elements = tab.query_selector_all(sel)
        except Exception:
            elements = []
        for el in elements:
            try:
                el.click(timeout=800)
                tab.wait_for_timeout(150)
            except Exception:
                continue


# -------------------------------------------------------
# CLICK DISCOVERY (SMALL, TO AVOID EXPLOSION)
# -------------------------------------------------------

def click_discover(tab, root_dom: str, max_clicks: int = 5):
    """
    Try a few clicks on obvious clickable elements to discover routes
    that only appear after interaction. Very limited by max_clicks.
    """
    discovered = set()
    try:
        candidates = tab.query_selector_all(
            "a, button, [role='button'], [onclick], [role='link'], [role='menuitem']"
        )
    except Exception:
        return discovered

    clicks = 0
    for el in candidates:
        if clicks >= max_clicks:
            break

        try:
            box = el.bounding_box()
            if not box:
                continue
        except Exception:
            continue

        before = tab.url
        nav_happened = False

        try:
            with tab.expect_navigation(timeout=7000):
                el.click()
            nav_happened = True
        except TimeoutError:
            if tab.url != before:
                nav_happened = True
        except Exception:
            pass

        clicks += 1

        if nav_happened:
            new = normalize(tab.url)
            parsed = urlparse(new)
            netloc = parsed.netloc.lower()
            netcmp = netloc[4:] if netloc.startswith("www.") else netloc

            if netcmp.endswith(root_dom) and new != normalize(before):
                print("  [click-nav]", before, "->", new)
                discovered.add(new)

            # try to go back so we don't drift away
            try:
                tab.go_back(timeout=7000)
            except Exception:
                try:
                    tab.goto(before, timeout=60000)
                except Exception as e:
                    print("[back-failed]", before, "->", e)
                    break

    return discovered


# -------------------------------------------------------
# CRAWLER
# -------------------------------------------------------

def crawl(start_url: str, max_pages: int = 50):
    """
    Simple BFS crawler using Playwright (headed).
    Returns adjacency dict: { url: [links...] }
    """
    visited = set()
    queue = deque([start_url])
    pages = {}

    base_root = root_domain(start_url)
    print("[root-domain]", base_root)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # H2: headed mode
        tab = browser.new_page()

        while queue and len(pages) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            print("\n[render]", url)
            try:
                tab.goto(url, timeout=60000, wait_until="domcontentloaded")
            except TimeoutError:
                print("[timeout]", url)
            except Exception as e:
                print("[goto-error]", url, "->", e)
                continue

            # Scroll a bit to trigger lazy loads
            try:
                last_height = 0
                for _ in range(10):
                    tab.mouse.wheel(0, 2000)
                    time.sleep(0.3)
                    new_height = tab.evaluate("document.body.scrollHeight")
                    if new_height == last_height:
                        break
                    last_height = new_height
            except Exception as e:
                print("[scroll-error]", url, "->", e)

            # Expand menus (best effort)
            expand_menus(tab)
            time.sleep(0.4)

            # Extract links from DOM
            html_links = extract_links_from_dom(tab, base_root)

            # Limited click-discovery
            click_links = click_discover(tab, base_root, max_clicks=5)

            all_links = html_links | click_links

            print(f"[page-links] total={len(all_links)}  (html={len(html_links)}, click={len(click_links)})")

            pages[url] = sorted(all_links)

            # Queue further URLs
            for link in all_links:
                if link not in visited and len(pages) + len(queue) < max_pages * 2:
                    queue.append(link)

        browser.close()

    return pages


# -------------------------------------------------------
# BUILD TREE (T3, F1, R3)
# -------------------------------------------------------

def build_tree_from_pages(pages):
    """
    Build hierarchical tree from adjacency dict.

    Tree format:
        {
          "name": "domain/",
          "url": "https://domain/",
          "children": [...]
        }
    """
    if not pages:
        return {}

    first_url = next(iter(pages.keys()))
    p = urlparse(first_url)
    scheme = p.scheme or "https"
    domain = p.netloc

    root = {
        "name": f"{domain}/",
        "url": f"{scheme}://{domain}/",
        "children": [],
        "_path": "",
    }

    # Collect all URLs (keys + adjacency)
    all_urls = set(pages.keys())
    for links in pages.values():
        all_urls |= set(links)

    # Filter to same domain exactly
    domain_urls = set()
    for url in all_urls:
        if not url:
            continue
        pu = urlparse(url)
        if pu.netloc == domain:
            domain_urls.add(url)

    def find_or_create_child(parent, seg_path, label):
        for child in parent["children"]:
            if child["_path"] == seg_path:
                return child
        new_child = {
            "name": label,   # path-only label, e.g. "/downloads/windows"
            "url": None,
            "children": [],
            "_path": seg_path,
        }
        parent["children"].append(new_child)
        return new_child

    for url in sorted(domain_urls):
        pu = urlparse(url)
        path = pu.path or "/"
        path = path.split("#", 1)[0]

        # root-level path is represented by root node already
        if path == "/" or path == "":
            continue

        segments = [seg for seg in path.strip("/").split("/") if seg]
        node = root
        accum = ""

        for seg in segments:
            accum = accum + "/" + seg if accum else "/" + seg
            seg_path = accum
            label = seg_path  # F1: label is the full path (starting with "/")
            node = find_or_create_child(node, seg_path, label)

        node["url"] = url

    # Cleanup internal key
    def clean(n):
        n.pop("_path", None)
        for c in n.get("children", []):
            clean(c)

    clean(root)
    return root


# -------------------------------------------------------
# D3 HTML VIEWER
# -------------------------------------------------------

def save_tree_html(tree, out_html: str):
    """Save D3 left-to-right tree viewer HTML."""
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    json_str = json.dumps(tree, indent=2, ensure_ascii=False)

    template = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Sitemap Tree</title>
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
    font-size:11px;
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
</style>
</head>
<body>
<div id="tree"></div>
<div id="tooltip" class="tooltip"></div>

<script id="sitedata" type="application/json">
__JSON__
</script>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const raw = document.getElementById('sitedata').textContent;
const treeData = JSON.parse(raw);

const width = window.innerWidth;
const height = window.innerHeight;

const svg = d3.select("#tree").append("svg")
  .attr("width", width)
  .attr("height", height);

const g = svg.append("g").attr("transform", "translate(80,40)");
const tooltip = document.getElementById("tooltip");

const zoom = d3.zoom()
  .scaleExtent([0.2, 3])
  .on("zoom", e => g.attr("transform", e.transform));

svg.call(zoom);

let root = d3.hierarchy(treeData);
root.x0 = height / 2;
root.y0 = 0;

// collapse children initially
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

function update(source) {
  const treeLayout = d3.tree().nodeSize([26, 160]);
  treeLayout(root);

  const nodes = root.descendants();
  const links = root.links();

  nodes.forEach(d => d.y = d.depth * 160);

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
    .attr("r", 6)
    .attr("class", d => {
      if (d._children) return "internal collapsed";
      if (d.children) return "internal";
      return "leaf";
    });

  nodeEnter.append("text")
    .attr("dy", 3)
    .attr("x", 10)
    .text(d => d.data.name || "");

  const nodeUpdate = nodeEnter.merge(node);

  nodeUpdate.transition()
    .duration(250)
    .attr("transform", d => `translate(${d.y},${d.x})`);

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
</script>
</body>
</html>
"""

    html = template.replace("__JSON__", json_str)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)

    print("✔ HTML saved →", out_html)


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Start URL (e.g., https://python.org)")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum pages to crawl")
    args = parser.parse_args()

    start_url = normalize(args.url)
    if not start_url.startswith("http://") and not start_url.startswith("https://"):
        start_url = "https://" + start_url

    print("[start-url]", start_url)
    pages = crawl(start_url, max_pages=args.max_pages)

    os.makedirs("outputs", exist_ok=True)

    raw_json_path = os.path.join("outputs", "sitemap_hier.json")
    with open(raw_json_path, "w", encoding="utf-8") as f:
        json.dump(pages, f, indent=2, ensure_ascii=False)
    print("✔ Raw adjacency JSON saved →", raw_json_path)

    tree = build_tree_from_pages(pages)
    tree_json_path = os.path.join("outputs", "tree_final.json")
    with open(tree_json_path, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)
    print("✔ Tree JSON saved →", tree_json_path)

    html_path = os.path.join("outputs", "sitemap_tree_final.html")
    save_tree_html(tree, html_path)

    webbrowser.open("file://" + os.path.abspath(html_path))


if __name__ == "__main__":
    main()
