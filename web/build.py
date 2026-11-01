#!/usr/bin/env python3
"""Builds the shared UI into both apps:
   app/src/main/assets/index.html  (Android)   and   desktop/light_studio.py  (Windows/Mac/Linux)"""
from pathlib import Path

root = Path(__file__).resolve().parent.parent
web = root / "web"
page = (web / "page.html").read_text(encoding="utf-8").replace("/*SCRIPT*/", (web / "app.js").read_text(encoding="utf-8"))
assert '"""' not in page
(root / "app/src/main/assets/index.html").write_text(page, encoding="utf-8")
server = (web / "desktop_server.py").read_text(encoding="utf-8").rstrip()
(root / "desktop/light_studio.py").write_text(
    server + '\n\n\n# ============================================================================ web app\n\n'
    'INDEX_HTML = r"""' + page + '"""\n\nif __name__ == "__main__":\n    main()\n', encoding="utf-8")
print("built index.html and light_studio.py")
