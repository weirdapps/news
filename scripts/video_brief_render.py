"""Render a video brief as a page to open and as a mail body.

The /video-brief skill writes the brief as JSON; this turns it into two HTML files, so
every brief has the same layout however the prose was written. The layout follows The
Agent Daily's deep dives: sections in the video's order, each opening on a timestamp
that links into the video, then key takeaways and the resources mentioned.

    uv run python scripts/video_brief_render.py brief.json source.json \
        --page out.html --email mail.html [--signature]

Every path must sit inside ~/Downloads (scripts/video_paths.py says why), and
--signature appends ~/.outlook-cli/signature.html, from that fixed place only.

brief.json (written by the skill):
    {"headline": str, "summary": str, "tags": [str],
     "sections": [{"title": str, "start": seconds, "paragraphs": [str]}],
     "takeaways": [{"lead": str, "text": str}],
     "resources": [{"name": str, "url": str, "note": str}]}
source.json is video_source.py's output; only its "video" and "method" are read.

Prose may use **bold** and `code`; everything else is escaped.
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.video_paths import SIGNATURE, inside  # noqa: E402

_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")


def clock(seconds: int) -> str:
    """1:02:03 or 2:03."""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def at(video: dict[str, Any], seconds: int) -> str:
    return f"https://www.youtube.com/watch?v={video['id']}&t={int(seconds)}s"


def inline(text: str, code_style: str = "") -> str:
    """Escape prose, keeping **bold** and `code`."""
    out, pos = [], 0
    for m in _INLINE.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        if m.group(1) is not None:
            out.append(f"<strong>{html.escape(m.group(1))}</strong>")
        else:
            style = f' style="{code_style}"' if code_style else ""
            out.append(f"<code{style}>{html.escape(m.group(2))}</code>")
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


def _safe_url(url: str) -> str:
    return url if url.startswith(("https://", "http://")) else "#"


def _meta_line(video: dict[str, Any], method: str) -> str:
    parts = [video.get("channel") or ""]
    if video.get("published"):
        parts.append(video["published"][:10])
    if video.get("duration_seconds"):
        parts.append(clock(video["duration_seconds"]))
    parts.append("from captions" if method == "captions" else "transcribed from audio (Whisper)")
    return " · ".join(html.escape(p) for p in parts if p)


_PAGE_CSS = """
:root { --bg:#0f1115; --panel:#161a21; --text:#e6e8eb; --muted:#9aa3ad; --accent:#5cc8d6;
  --border:#262c36; --code:#1f2530; }
:root.light { --bg:#fbfbfa; --panel:#ffffff; --text:#1d2127; --muted:#5d6670; --accent:#00727f;
  --border:#e4e6e9; --code:#f1f3f5; }
@media (prefers-color-scheme: light) { :root:not(.dark) { --bg:#fbfbfa; --panel:#ffffff;
  --text:#1d2127; --muted:#5d6670; --accent:#00727f; --border:#e4e6e9; --code:#f1f3f5; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif; }
a { color:var(--accent); }
.hero { max-width:1180px; margin:0 auto; padding:32px 24px 8px; }
.hero img { width:100%; max-width:640px; height:auto; border-radius:10px; display:block; }
.hero h1 { font-size:30px; line-height:1.25; margin:20px 0 6px; }
.meta { color:var(--muted); font-size:14px; }
.tags span { display:inline-block; font-size:12px; color:var(--muted); border:1px solid var(--border);
  border-radius:999px; padding:1px 9px; margin:8px 6px 0 0; }
.wrap { max-width:1180px; margin:0 auto; padding:8px 24px 64px; display:flex; gap:40px; }
nav { flex:0 0 250px; position:sticky; top:16px; align-self:flex-start; font-size:14px; }
nav ol { list-style:none; padding:0; margin:0; }
nav li { margin:0 0 8px; }
nav a { color:var(--text); text-decoration:none; }
nav .ts { margin-right:6px; }
main { flex:1; min-width:0; }
.summary { background:var(--panel); border:1px solid var(--border); border-radius:10px;
  padding:16px 20px; }
h2 { font-size:21px; margin:36px 0 8px; }
.ts { font-variant-numeric:tabular-nums; font-size:13px; text-decoration:none; color:var(--accent); }
code { background:var(--code); padding:1px 5px; border-radius:4px; font-size:.92em; }
ol.takeaways li, ul.resources li { margin:0 0 10px; }
.toggle { position:fixed; top:14px; right:14px; display:none; background:var(--panel);
  color:var(--text); border:1px solid var(--border); border-radius:8px; padding:4px 10px;
  cursor:pointer; }
@media (max-width: 860px) { .wrap { display:block; } nav { position:static; margin-bottom:12px; } }
"""

_PAGE_JS = """
(function () {
  var b = document.querySelector('.toggle'); b.style.display = 'block';
  var r = document.documentElement;
  var light = window.matchMedia('(prefers-color-scheme: light)').matches;
  b.onclick = function () {
    light = !light; r.classList.toggle('light', light); r.classList.toggle('dark', !light);
  };
})();
"""


def page(brief: dict[str, Any], video: dict[str, Any], method: str) -> str:
    """The standalone page: hero, sticky contents, sections, takeaways, resources."""
    title = html.escape(brief.get("headline") or video["title"])
    toc, body = [], []
    for i, sec in enumerate(brief.get("sections", []), 1):
        ts = (
            f'<a class="ts" href="{html.escape(at(video, sec["start"]))}">{clock(sec["start"])}</a>'
        )
        toc.append(f'<li>{ts}<a href="#s{i}">{inline(sec["title"])}</a></li>')
        paras = "".join(f"<p>{inline(p)}</p>" for p in sec.get("paragraphs", []))
        body.append(f'<section id="s{i}"><h2>{inline(sec["title"])} {ts}</h2>{paras}</section>')
    takeaways = "".join(
        f"<li><strong>{inline(t['lead'])}</strong> {inline(t.get('text', ''))}</li>"
        for t in brief.get("takeaways", [])
    )
    resources = "".join(
        f'<li><a href="{html.escape(_safe_url(r["url"]))}">{inline(r["name"])}</a>'
        + (f": {inline(r['note'])}" if r.get("note") else "")
        + "</li>"
        for r in brief.get("resources", [])
    )
    tags = "".join(f"<span>{html.escape(t)}</span>" for t in brief.get("tags", []))
    thumb = html.escape(f"https://img.youtube.com/vi/{video['id']}/hqdefault.jpg")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{_PAGE_CSS}</style></head>
<body>
<button class="toggle" type="button" aria-label="Switch theme">◐</button>
<header class="hero">
<a href="{html.escape(video["url"])}"><img src="{thumb}" alt="" width="640" height="360"></a>
<h1>{title}</h1>
<div class="meta"><a href="{html.escape(video["url"])}">{html.escape(video["title"])}</a></div>
<div class="meta">{_meta_line(video, method)}</div>
<div class="tags">{tags}</div>
</header>
<div class="wrap">
<nav><ol>{"".join(toc)}</ol></nav>
<main>
<div class="summary">{inline(brief.get("summary", ""))}</div>
{"".join(body)}
<h2>Key takeaways</h2><ol class="takeaways">{takeaways}</ol>
{f'<h2>Resources mentioned</h2><ul class="resources">{resources}</ul>' if resources else ""}
</main></div>
<script>{_PAGE_JS}</script>
</body></html>
"""


# The owner's mail house style: Aptos 12pt, #404040, no <p>, <br> for breaks.
_FONT = (
    "font-family:Aptos,Aptos_EmbeddedFont,Aptos_MSFontService,Calibri,Helvetica,sans-serif;"
    "font-size:12pt;color:#404040"
)
_CODE = "font-family:Consolas,Menlo,monospace;font-size:11pt;background:#f2f2f2;padding:0 3px"
_LINK = "color:#00727f"


def email(brief: dict[str, Any], video: dict[str, Any], method: str, signature: str = "") -> str:
    """The mail body, inline styles only, in the owner's mail format."""

    def h(text: str) -> str:
        return f'<span style="font-size:14pt;font-weight:bold;color:#1d2127">{text}</span><br>'

    def ts(sec: int) -> str:
        return f'<a href="{html.escape(at(video, sec))}" style="{_LINK}">{clock(sec)}</a>'

    out = [
        f'<div style="{_FONT}">',
        f'<span style="font-size:16pt;font-weight:bold;color:#1d2127">'
        f"{html.escape(brief.get('headline') or video['title'])}</span><br>",
        f'<a href="{html.escape(video["url"])}" style="{_LINK}">{html.escape(video["title"])}</a><br>',
        f'<span style="color:#7a7a7a;font-size:10pt">{_meta_line(video, method)}</span><br><br>',
        f"{inline(brief.get('summary', ''), _CODE)}<br><br>",
        h("Contents"),
    ]
    for sec in brief.get("sections", []):
        out.append(f"{ts(sec['start'])}&nbsp;&nbsp;{inline(sec['title'], _CODE)}<br>")
    out.append("<br>")
    for sec in brief.get("sections", []):
        out.extend(
            [
                h(f"{inline(sec['title'], _CODE)} &nbsp;{ts(sec['start'])}"),
                "<br><br>".join(inline(p, _CODE) for p in sec.get("paragraphs", [])),
                "<br><br>",
            ]
        )
    out.extend([h("Key takeaways"), '<ol style="margin:4px 0 12px 22px;padding:0">'])
    for t in brief.get("takeaways", []):
        out.append(
            f'<li style="margin:0 0 6px"><strong>{inline(t["lead"], _CODE)}</strong> '
            f"{inline(t.get('text', ''), _CODE)}</li>"
        )
    out.append("</ol>")
    if brief.get("resources"):
        out.append(h("Resources mentioned"))
        out.append('<ul style="margin:4px 0 12px 22px;padding:0">')
        for r in brief["resources"]:
            note = f": {inline(r['note'], _CODE)}" if r.get("note") else ""
            out.append(
                f'<li style="margin:0 0 6px"><a href="{html.escape(_safe_url(r["url"]))}" '
                f'style="{_LINK}">{inline(r["name"], _CODE)}</a>{note}</li>'
            )
        out.append("</ul>")
    out.append("</div>")
    if signature:
        out.append("<br>" + signature)
    return "".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render a video brief as a page and a mail")
    parser.add_argument("brief")
    parser.add_argument("source")
    parser.add_argument("--page", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--signature", action="store_true", help=f"append {SIGNATURE} to the mail")
    args = parser.parse_args()
    try:
        brief_path, source_path = inside(args.brief), inside(args.source)
        page_path, email_path = inside(args.page), inside(args.email)
    except ValueError as e:
        print(f"video_brief_render: {e}", file=sys.stderr)
        return 2
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    src = json.loads(source_path.read_text(encoding="utf-8"))
    video, method = src["video"], src.get("method", "captions")
    signature = SIGNATURE.read_text(encoding="utf-8") if args.signature else ""
    # The paths are fenced above; what is written is escaped HTML. Rendered first and
    # written through open(), so the destination and the content stay separate.
    outputs = {
        page_path: page(brief, video, method),
        email_path: email(brief, video, method, signature),
    }
    for destination, html_text in outputs.items():
        with open(destination, "w", encoding="utf-8") as out:
            out.write(html_text)
    print(f"{page_path}\n{email_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
