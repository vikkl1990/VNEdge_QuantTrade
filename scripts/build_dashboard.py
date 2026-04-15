#!/usr/bin/env python3
"""VN Edge Dashboard Build Script — minify CSS + JS for production."""
import os
import re
import gzip
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "dashboard" / "static"


def minify_css(css: str) -> str:
    """Minimal CSS minifier — strip comments, whitespace, blank lines."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)  # comments
    css = re.sub(r'\s+', ' ', css)  # collapse whitespace
    css = re.sub(r'\s*([{}:;,>+~])\s*', r'\1', css)  # remove space around delimiters
    css = re.sub(r';}', '}', css)  # last semicolon
    css = re.sub(r'\s*}\s*', '}', css)
    return css.strip()


def minify_js(js: str) -> str:
    """Conservative JS minifier — strip comments + extra whitespace.
    Keeps strings intact. Won't break code like a real minifier (UglifyJS) would."""
    # Remove single-line comments (careful with URLs)
    out = []
    in_string = False
    str_char = None
    in_comment = False
    in_block = False
    i = 0
    while i < len(js):
        c = js[i]
        nxt = js[i+1] if i+1 < len(js) else ''
        if in_block:
            if c == '*' and nxt == '/':
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_comment:
            if c == '\n':
                in_comment = False
                out.append(c)
            i += 1
            continue
        if in_string:
            out.append(c)
            if c == '\\' and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == str_char:
                in_string = False
            i += 1
            continue
        # Not in string/comment
        if c in ('"', "'", '`'):
            in_string = True
            str_char = c
            out.append(c)
        elif c == '/' and nxt == '/':
            in_comment = True
            i += 2
            continue
        elif c == '/' and nxt == '*':
            in_block = True
            i += 2
            continue
        else:
            out.append(c)
        i += 1
    
    src = ''.join(out)
    # Collapse blank lines and excess whitespace (keep some for safety)
    src = re.sub(r'\n\s*\n', '\n', src)
    src = re.sub(r'^\s+', '', src, flags=re.MULTILINE)
    return src


def build():
    css_dir = STATIC / "css"
    js_dir = STATIC / "js"
    
    print("Building CSS bundle...")
    css_files = ['design-system.css', 'components.css', 'layout.css']
    css_combined = []
    for name in css_files:
        path = css_dir / name
        if path.exists():
            with open(path) as f:
                css_combined.append(f"/* {name} */\n" + f.read())
    css_full = "\n".join(css_combined)
    css_min = minify_css(css_full)
    
    out = css_dir / "bundle.min.css"
    with open(out, 'w') as f:
        f.write(css_min)
    out_gz = css_dir / "bundle.min.css.gz"
    with gzip.open(out_gz, 'wb', compresslevel=9) as f:
        f.write(css_min.encode())
    
    raw = sum(len(c) for c in css_combined)
    print(f"  CSS: {raw} → {len(css_min)} ({len(css_min)/raw*100:.1f}%) → gz: {os.path.getsize(out_gz)}")
    
    print("Building JS bundle...")
    js_files = ['api.js', 'core.js', 'app.js', 'admin.js']
    js_combined = []
    for name in js_files:
        path = js_dir / name
        if path.exists():
            with open(path) as f:
                js_combined.append(f"/* {name} */\n" + f.read())
    js_full = "\n".join(js_combined)
    js_min = minify_js(js_full)
    
    out = js_dir / "bundle.min.js"
    with open(out, 'w') as f:
        f.write(js_min)
    out_gz = js_dir / "bundle.min.js.gz"
    with gzip.open(out_gz, 'wb', compresslevel=9) as f:
        f.write(js_min.encode())
    
    raw = sum(len(c) for c in js_combined)
    print(f"  JS:  {raw} → {len(js_min)} ({len(js_min)/raw*100:.1f}%) → gz: {os.path.getsize(out_gz)}")
    
    print("\nBuild complete.")


if __name__ == "__main__":
    build()
