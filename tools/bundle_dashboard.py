"""构建单请求页面：内联 CSS/JS/SVG，使用系统字体，避免单连接 HTTPS 的资源并发争抢。"""
import base64
from pathlib import Path
import re


def bundle(source):
    source = Path(source)
    html = (source / "index.html").read_text()
    css = (source / "app.css").read_text()
    css = re.sub(r"@font-face\s*\{[^}]*\}\s*", "", css)
    icon = base64.b64encode((source / "noun_Fan_8413191.svg").read_bytes()).decode()
    css = css.replace('url("noun_Fan_8413191.svg")', 'url("data:image/svg+xml;base64,' + icon + '")')
    js = (source / "app.js").read_text()
    html = html.replace('<link rel="stylesheet" href="app.css">', "<style>" + css + "</style>")
    html = html.replace('<script src="app.js" defer></script>', "")
    return html.replace("</body>", "<script>" + js + "</script></body>").encode()
