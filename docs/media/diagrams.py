# SPDX-License-Identifier: MIT
"""Draws the README's three diagrams, each in a light and a dark version (GitHub picks one
with <picture> and prefers-color-scheme). Edit here and run it; never edit the SVGs.

    python3 docs/media/diagrams.py
"""
import os
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

THEMES = {   # GitHub's own light and dark colors, so the figures sit on the page
    "light": dict(ink="#1f2328", muted="#59636e", line="#d1d9e0", card="#f6f8fa",
                  you="#1f3a5f", you_bg="#e3eaf3", ai="#c2381f", ai_bg="#fde9e4",
                  style="#1a7f37", style_bg="#dafbe1", voice="#9a6700", voice_bg="#fff8c5"),
    "dark": dict(ink="#e6edf3", muted="#9198a1", line="#3d444d", card="#151b23",
                 you="#9fc0ea", you_bg="#1d2d45", ai="#ff7a5c", ai_bg="#3a1d17",
                 style="#3fb950", style_bg="#132d1d", voice="#d29922", voice_bg="#2e2410"),
}


class Svg:
    def __init__(self, w, h, label, c, top=0):
        self.w, self.h, self.label, self.c, self.out, self.top = w, h, label, c, [], top

    def rect(self, x, y, w, h, fill, stroke, rx=8, dash=False, sw=1.5):
        d = ' stroke-dasharray="5 4"' if dash else ""
        self.out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>')

    def text(self, x, y, s, size=13, fill=None, weight=400, anchor="start", mono=False, italic=False):
        fam = MONO if mono else SANS
        st = ' font-style="italic"' if italic else ""
        self.out.append(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" '
                        f'font-weight="{weight}" fill="{fill or self.c["ink"]}" '
                        f'text-anchor="{anchor}"{st}>{escape(s)}</text>')

    def arrow(self, pts, color, dash=False, sw=1.6):
        """A polyline ending in a filled head; the head is drawn here, not as a marker, so it
        takes the line's color in every renderer."""
        (x1, y1), (x2, y2) = pts[-2], pts[-1]
        L = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        bx, by = x2 - ux * 9, y2 - uy * 9          # the line stops where the head starts
        line = pts[:-1] + [(bx, by)]
        d = ' stroke-dasharray="5 4"' if dash else ""
        self.out.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in line) +
                        f'" fill="none" stroke="{color}" stroke-width="{sw}" '
                        f'stroke-linejoin="round"{d}/>')
        px, py = -uy, ux
        head = [(x2, y2), (bx + px * 4.5, by + py * 4.5), (bx - px * 4.5, by - py * 4.5)]
        self.out.append('<polygon points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in head) +
                        f'" fill="{color}"/>')

    def raw(self, s):
        self.out.append(s)

    def svg(self):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 {self.top} {self.w} {self.h}" '
                f'width="{self.w}" height="{self.h}" role="img" aria-label="{escape(self.label)}">'
                f'<title>{escape(self.label)}</title>' + "".join(self.out) + "</svg>\n")


def lock(s, x, y, color):
    """A small padlock whose body's top-left corner is (x, y)."""
    s.raw(f'<path d="M{x + 2.5} {y} v-3 a3.5 3.5 0 0 1 7 0 v3" fill="none" stroke="{color}" '
          f'stroke-width="1.6"/>')
    s.rect(x, y, 12, 9, color, color, rx=2, sw=1)


def picture(s, x, y, w, h, c):
    s.rect(x, y, w, h, c["style_bg"], c["style"], rx=4, sw=1.2)
    s.raw(f'<polyline points="{x + 6},{y + h - 5} {x + w * .38},{y + h * .38} {x + w * .58},'
          f'{y + h * .62} {x + w * .72},{y + h * .48} {x + w - 6},{y + h - 5}" fill="none" '
          f'stroke="{c["style"]}" stroke-width="1.4" stroke-linejoin="round"/>')
    s.raw(f'<circle cx="{x + w * .8}" cy="{y + h * .28}" r="{h * .1:.1f}" fill="{c["style"]}"/>')


# ── 1. the three steps: your words go straight through, prompts go to the model ─────────

def steps(c):
    s = Svg(960, 390, "Your paragraphs pass from Plan to Draft unchanged; only the prompts "
            "go to the AI model, with your voice pack, and come back as drafts in the same "
            "places. Proof draws the issue with your style file.", c, top=76)
    top, rows = 112, [150, 202, 254, 306]

    def card(x, title):
        s.text(x, 98, title, 15, weight=700)
        s.rect(x, top, 200, 252, c["card"], c["line"], rx=10)

    def block(x, y, kind, label, dash=False):
        fill, stroke = (c["you_bg"], c["you"]) if kind == "you" else (c["ai_bg"], c["ai"])
        s.rect(x + 14, y, 172, 40, fill, stroke, rx=6, dash=dash)
        s.text(x + 28, y + 25, label, 13)

    # 1 · Plan
    card(20, "1 · Plan")
    block(20, rows[0], "you", "Your paragraph")
    block(20, rows[1], "ai", "Prompt: what to write", dash=True)
    block(20, rows[2], "you", "Your paragraph")
    block(20, rows[3], "ai", "Image: what to draw", dash=True)

    # the model, fed by the voice pack from below
    s.rect(290, 200, 140, 140, c["ai_bg"], c["ai"], rx=14)
    s.text(360, 262, "the AI model", 14, weight=700, anchor="middle")
    s.text(360, 284, "writes only", 12, c["muted"], anchor="middle")
    s.text(360, 300, "the prompts", 12, c["muted"], anchor="middle")

    s.rect(260, 396, 200, 58, c["voice_bg"], c["voice"], rx=10)
    s.text(360, 420, "Your voice pack", 13, weight=700, anchor="middle")
    s.text(360, 440, "writer file · voice file · samples", 12, c["muted"], anchor="middle")
    s.arrow([(360, 396), (360, 340)], c["voice"])
    s.text(368, 372, "with every pass", 12, c["voice"])

    # 2 · Draft
    card(500, "2 · Draft")
    block(500, rows[0], "you", "Your paragraph")
    block(500, rows[1], "ai", "Draft")
    s.text(672, rows[1] + 25, "Edit, lock", 12, c["muted"], anchor="end")
    lock(s, 570, rows[1] + 16, c["ai"])
    block(500, rows[2], "you", "Your paragraph")
    block(500, rows[3], "ai", "")
    picture(s, 528, rows[3] + 7, 44, 26, c)
    s.text(582, rows[3] + 25, "Picture", 13)

    # the flow between the steps, drawn last so no card covers an arrowhead
    for y in (222, 326):
        s.arrow([(206, y), (290, y)], c["ai"])
        s.arrow([(430, y), (514, y)], c["ai"])
    s.text(248, 214, "prompt", 12, c["ai"], anchor="middle")
    s.text(472, 214, "draft", 12, c["ai"], anchor="middle")
    s.arrow([(206, 170), (514, 170)], c["you"], sw=2)     # your words: straight across
    s.text(360, 146, "your words", 12, c["you"], weight=700, anchor="middle")
    s.text(360, 161, "read for context, never rewritten", 12, c["you"], anchor="middle")

    # 3 · Proof
    s.arrow([(700, 238), (740, 238)], c["ink"])
    s.text(740, 98, "3 · Proof", 15, weight=700)
    s.rect(740, top, 200, 252, c["card"], c["line"], rx=10)
    s.rect(758, 132, 164, 214, c["card"], c["line"], rx=4, sw=1)       # the page
    s.rect(772, 148, 110, 9, c["ink"], c["ink"], rx=2, sw=0)           # heading
    for i, w in enumerate((136, 128, 96)):
        s.rect(772, 168 + i * 13, w, 5, c["muted"], c["muted"], rx=2, sw=0)
    s.rect(772 + 60, 181, 34, 5, c["you"], c["you"], rx=2, sw=0)       # an accent word
    picture(s, 772, 214, 136, 58, c)
    for i, w in enumerate((136, 104)):
        s.rect(772, 284 + i * 13, w, 5, c["muted"], c["muted"], rx=2, sw=0)
    s.text(840, 333, "email + web HTML", 12, c["muted"], anchor="middle")

    s.rect(740, 396, 200, 58, c["style_bg"], c["style"], rx=10)
    s.text(840, 420, "Your style file", 13, weight=700, anchor="middle")
    s.text(840, 440, "colors + HTML templates", 12, c["muted"], anchor="middle")
    s.arrow([(840, 396), (840, 364)], c["style"])

    # legend
    s.rect(20, 404, 16, 16, c["you_bg"], c["you"], rx=4)
    s.text(44, 417, "your words", 13)
    s.rect(20, 432, 16, 16, c["ai_bg"], c["ai"], rx=4)
    s.text(44, 445, "the model's (dashed: still a prompt)", 13)
    return s.svg()


# ── 2. what one writing pass sends ────────────────────────────────────────────────────

def voice_pass(c):
    s = Svg(960, 470, "One writing pass sends your writer file as the model's instructions, "
            "then attaches your voice file, your samples and the issue as it stands. The "
            "model sends back a block for each prompt and a short note.", c)
    # the voice pack folder
    s.rect(20, 30, 290, 330, c["card"], c["line"], rx=10)
    s.text(38, 56, "workspace/voices/my-voice/", 12, c["muted"], mono=True)

    def tile(y, h, name, desc):
        s.rect(36, y, 258, h, c["voice_bg"], c["voice"], rx=6)
        s.text(50, y + 21, name, 13, weight=700, mono=True)
        if desc:
            s.text(50, y + 40, desc, 12, c["muted"])

    tile(72, 54, "writer.md", "the brief: who, for whom, hard rules")
    tile(138, 54, "VOICE.md", "your moves, each with a line you wrote")
    for i, n in enumerate(("001.md", "002.md", "003.md")):
        tile(204 + i * 40, 32, n, "")
        s.text(128, 204 + i * 40 + 21, "something you wrote", 12, c["muted"])
    s.text(38, 340, "The samples count most: the model copies", 12, c["voice"])
    s.text(38, 355, "what it sees more than what it is told.", 12, c["voice"])

    # the request
    s.rect(370, 30, 260, 330, c["card"], c["line"], rx=10)
    s.text(388, 58, "One writing pass", 15, weight=700)
    s.text(388, 88, "INSTRUCTIONS", 11, c["muted"], weight=700)
    s.rect(388, 96, 224, 56, c["ai_bg"], c["line"], rx=6, sw=1)
    s.text(402, 118, "writer.md", 13, weight=700, mono=True)
    s.text(402, 139, "+ how the job works", 12, c["muted"])
    s.text(388, 184, "ATTACHED", 11, c["muted"], weight=700)
    s.rect(388, 192, 224, 150, c["ai_bg"], c["line"], rx=6, sw=1)
    s.text(402, 216, "VOICE.md", 13, weight=700, mono=True)
    s.text(402, 250, "001.md  002.md  003.md", 13, weight=700, mono=True)
    s.text(402, 290, "DOCUMENT.md", 13, weight=700, mono=True)
    s.text(402, 310, "your issue as it stands:", 12, c["muted"])
    s.text(402, 326, "paragraphs, prompts, drafts", 12, c["muted"])

    s.arrow([(294, 99), (388, 118)], c["voice"])
    s.arrow([(294, 165), (340, 165), (340, 212), (402 - 14, 212)], c["voice"])
    s.raw(f'<polyline points="300,208 306,208 306,312 300,312" fill="none" '
          f'stroke="{c["voice"]}" stroke-width="1.6"/>')
    s.arrow([(306, 246), (388, 246)], c["voice"])

    s.rect(390, 396, 220, 58, c["you_bg"], c["you"], rx=10)
    s.text(500, 420, "Your issue", 13, weight=700, anchor="middle")
    s.text(500, 440, "what you wrote in Plan", 12, c["muted"], anchor="middle")
    s.arrow([(500, 396), (500, 342)], c["you"])

    # the model and its reply
    s.arrow([(630, 124), (690, 124)], c["ai"])
    s.rect(690, 90, 250, 68, c["ai_bg"], c["ai"], rx=14)
    s.text(815, 120, "the AI model", 14, weight=700, anchor="middle")
    s.text(815, 140, "one call", 12, c["muted"], anchor="middle")
    s.arrow([(815, 158), (815, 214)], c["ai"])
    s.rect(690, 214, 250, 146, c["card"], c["line"], rx=10)
    s.text(708, 240, "What comes back", 15, weight=700)
    s.text(708, 268, "=== BLOCK b-p1 ===", 12, c["ai"], mono=True)
    s.text(708, 286, "a draft for each prompt", 12, c["muted"])
    s.text(708, 316, "=== NOTE ===", 12, c["ai"], mono=True)
    s.text(708, 334, "what it wrote, what it wasn't sure of", 12, c["muted"])
    s.text(815, 392, "The drafts land where their prompts were.", 12, c["muted"], anchor="middle")
    s.text(815, 409, "Your paragraphs come back untouched.", 12, c["muted"], anchor="middle")
    return s.svg()


# ── 3. how the style file turns an issue into HTML ────────────────────────────────────

def style_file(c):
    s = Svg(960, 400, "Each kind of block in your issue is drawn by the template your style "
            "file names for it: a heading by heading.html, a paragraph by paragraph.html with "
            "the accent color filled in, a note box by note.html. Out comes HTML with every "
            "style written inline.", c)
    heads = [(20, "Your issue"), (350, "Your style file"), (680, "The HTML it makes")]
    for x, t in heads:
        s.text(x, 30, t, 15, weight=700)
    s.rect(20, 44, 280, 300, c["card"], c["line"], rx=10)
    s.rect(680, 44, 260, 300, c["card"], c["line"], rx=10)
    s.rect(336, 44, 288, 300, c["card"], c["line"], rx=10)

    kinds = [   # (color, issue lines, style tile lines, html lines)
        ("style", ["## In Which We Begin"],
         ["[blocks] heading", "→ heading.html"],
         ['<h2 style="…">', "In Which We Begin</h2>"]),
        ("you", ["Some **words**{.green}", "of mine."],
         ["[blocks] paragraph", "→ paragraph.html", "[accents] green = #1f8a4c"],
         ['<p style="…">Some', '<span style="color:#1f8a4c">', "words</span> of mine.</p>"]),
        ("voice", ['::: {.note label="Aside"}', "One short paragraph.", ":::"],
         ["[components.note]", "→ note.html"],
         ['<table style="…"><tr><td>', "<b>Aside</b> One short", "paragraph.</td></tr></table>"]),
    ]
    ys = [64, 150, 256]
    hs = [60, 84, 70]
    for (kind, md, tile, html), y, h in zip(kinds, ys, hs):
        col, bg = c[kind], c[kind + "_bg"]
        s.rect(34, y, 252, h, bg, col, rx=6)
        for i, line in enumerate(md):
            s.text(46, y + 24 + i * 18, line, 12.5, mono=True)
        s.rect(350, y, 260, h, bg, col, rx=6)
        for i, line in enumerate(tile):
            s.text(364, y + 24 + i * 18, line, 12.5, weight=700 if i == 0 else 400, mono=True)
        s.rect(694, y, 232, h, bg, col, rx=6)
        for i, line in enumerate(html):
            s.text(706, y + 24 + i * 18, line, 12, mono=True)
        mid = y + h / 2
        s.arrow([(286, mid), (350, mid)], col, sw=1.8)
        s.arrow([(610, mid), (694, mid)], col, sw=1.8)
    s.text(480, 372, "[templates] holds the HTML for each of these.", 12, c["muted"], anchor="middle")
    s.text(810, 372, "Every style inline, as email needs.", 12, c["muted"], anchor="middle")
    s.text(160, 372, "Markdown, as you write it.", 12, c["muted"], anchor="middle")
    return s.svg()


def main():
    for name, draw in (("diagram-steps", steps), ("diagram-voice", voice_pass),
                       ("diagram-style", style_file)):
        for theme, c in THEMES.items():
            path = os.path.join(HERE, f"{name}-{theme}.svg")
            with open(path, "w", encoding="utf-8") as f:
                f.write(draw(c))
            print("wrote", os.path.relpath(path))


if __name__ == "__main__":
    main()
