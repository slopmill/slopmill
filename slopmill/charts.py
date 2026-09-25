# SPDX-License-Identifier: AGPL-3.0-or-later
"""Charts: the writer supplies the data, slopmill draws the picture.

An image model cannot be trusted with numbers or labels, so a chart is never sent to one.
The writer answers a Chart: prompt with a CHART block (type, title, rows of label | number,
source, alt text, caption); parse_charts() reads it, check_spec() refuses anything that
would draw wrong, and draw() makes a PNG with matplotlib, in the design's accent colours.
"""
import math
import re

CHART_RE = re.compile(r"^=== CHART #?([A-Za-z][A-Za-z0-9_-]*) ===[ \t]*\n(.*?)\n=== END ===",
                      re.S | re.M)
TYPES = ("bar", "column", "line", "pie")
MAX_ROWS, MAX_PIE, MAX_SERIES = 30, 8, 4
MAX_MARKS = 60           # rows × series: past this no label can be read at 1600×900
MAX_COLUMNS = 12         # a column chart with more rows is drawn as horizontal bars
MAX_LINE_POINTS = 40     # a line chart prints every point's number
WIDTH, HEIGHT, DPI = 1600, 900, 200
# Neutral defaults as RGB, not a design's colours: the bars take the design's accents, and
# these only fill in for a design that has none (and for the ink, grid and paper).
FALLBACK = [(0.17, 0.35, 0.54), (0.61, 0.38, 0.04), (0.27, 0.40, 0.16), (0.65, 0.24, 0.17)]
INK, INK_SOFT, GRID, PAPER = (0.12, 0.12, 0.11), (0.37, 0.36, 0.33), (0.89, 0.88, 0.85), (1, 1, 1)
FIELDS = ("type", "title", "unit", "series", "scale", "source", "alt", "caption")

RULES = """
Charts:
A PROMPT marked CHART, or any PROMPT that asks for a chart or graph of numbers, is answered
with a CHART block instead of a BLOCK. slopmill draws it with exactly your labels and
numbers; you never draw or describe the picture.
=== CHART <id> ===
TYPE: column (vertical bars), bar (horizontal bars, for long labels), line (change over
time, labels in time order) or pie (parts of one whole, at most 8 rows)
TITLE: a short title
UNIT: what the numbers count, e.g. "billion parameters" or "$ billion" (or leave empty)
SERIES: name | name   (only when each row has more than one number)
DATA:
label | number
label | number
SCALE: log   (only if the values span more than 100 times and are all above zero)
SOURCE: where the numbers came from
ALT: one plain sentence saying what the chart shows, with the key numbers
CAPTION: a short caption in the author's voice, or nothing
=== END ===
Numbers are plain: 4000 or 4,000 or 12.5, no words after them (put units in UNIT). At most
30 rows. Use the numbers the author gave: in the prompt, the issue or the attached files.
You cannot look anything up. If the prompt asks for numbers it does not give, you may use
what you know, but then SOURCE must begin "The writer's memory, not checked" and your NOTE
must tell the author to check them before publishing. This is the one place numbers that
are not in the author's text are allowed, because the chart says where they came from.
Leave out a row rather than guess a number you do not know.
""".strip()


class ChartError(ValueError):
    pass


def parse_charts(text):
    """{id: spec} from a reply's CHART blocks. A spec is raw strings: check_spec() turns it
    into something drawable or says what is wrong."""
    out = {}
    for m in CHART_RE.finditer(text or ""):
        spec = {k: "" for k in FIELDS}
        spec["rows"] = []
        in_data = False
        for line in m.group(2).split("\n"):
            f = re.match(r"^\s*(TYPE|TITLE|UNIT|SERIES|SCALE|SOURCE|ALT|CAPTION|DATA)\s*:\s*(.*)$", line, re.I)
            if f:
                key = f.group(1).lower()
                in_data = key == "data"
                if not in_data:
                    spec[key] = " ".join(f.group(2).split())
                elif f.group(2).strip():          # DATA: label | 3 on the same line
                    spec["rows"].append(f.group(2).strip())
            elif in_data and line.strip():
                spec["rows"].append(line.strip())
        out[m.group(1)] = spec
    return out


def number(text):
    """A plain number: thousands commas, a leading currency sign and a trailing % allowed."""
    t = text.strip().replace("−", "-").replace(" ", "").replace(" ", "")
    t = re.sub(r"^(-?)[$€£¥]", r"\1", t)
    t = t[:-1] if t.endswith("%") else t
    if not re.fullmatch(r"-?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?|-?\.\d+", t):
        raise ChartError(f"{text.strip()!r} is not a plain number")
    v = float(t.replace(",", ""))
    if not math.isfinite(v):
        raise ChartError(f"{text.strip()!r} is not a plain number")
    return v


def _pretty(cell):
    """A number as the writer wrote it (every digit, its $ or %), with thousands commas
    added to a long whole part: 1234567.5 -> 1,234,567.5."""
    t = " ".join(cell.split())
    m = re.fullmatch(r"([$€£¥]?)(-?)(\d+)(\.\d+)?(%?)", t)
    if not m:
        return t
    cur, sign, whole, frac, pct = m.groups()
    return f"{cur}{sign}{int(whole):,}{frac or ''}{pct}"


def check_spec(raw):
    """A drawable chart from a parsed CHART block, or ChartError with the reason."""
    kind = raw.get("type", "").strip().lower()
    kind = {"vertical bar": "column", "columns": "column", "bars": "bar", "horizontal bar": "bar",
            "lines": "line"}.get(kind, kind)
    if kind not in TYPES:
        raise ChartError(f"the chart type must be one of {', '.join(TYPES)}, not {raw.get('type')!r}")
    title, unit = raw.get("title", ""), raw.get("unit", "")
    source, alt, caption = raw.get("source", ""), raw.get("alt", ""), raw.get("caption", "")
    if not title:
        raise ChartError("the chart has no title")
    if not source:
        raise ChartError("the chart does not say where its numbers came from (SOURCE)")
    if not alt:
        raise ChartError("the chart has no alt text")
    for name, value, cap in (("title", title, 120), ("unit", unit, 60), ("source", source, 200),
                             ("alt text", alt, 400), ("caption", caption, 600)):
        if len(value) > cap:
            raise ChartError(f"the chart's {name} is over {cap} characters")
    series = [s.strip() for s in raw.get("series", "").split("|") if s.strip()]
    rows = raw.get("rows") or []
    if not rows:
        raise ChartError("the chart has no data rows")
    if len(rows) > MAX_ROWS:
        raise ChartError(f"the chart has {len(rows)} rows; at most {MAX_ROWS}")
    labels, values, shown = [], [], []
    for r in rows:
        cells = [c.strip() for c in r.strip().strip("|").split("|")]
        if len(cells) < 2:
            raise ChartError(f"the row {r!r} is not label | number")
        label = cells[0]
        if not label or len(label) > 60:
            raise ChartError(f"the row {r!r} needs a label of 1 to 60 characters")
        values.append([number(c) for c in cells[1:]])
        shown.append([_pretty(c) for c in cells[1:]])     # printed as written, commas added
        labels.append(label)
    width = len(values[0])
    if any(len(v) != width for v in values):
        raise ChartError("every row must have the same number of numbers")
    if width > MAX_SERIES:
        raise ChartError(f"at most {MAX_SERIES} numbers per row")
    if series and len(series) != width:
        raise ChartError(f"SERIES names {len(series)} series but the rows have {width} numbers each")
    if width > 1 and not series:
        raise ChartError("rows with more than one number need SERIES names")
    if len(rows) * width > MAX_MARKS:
        raise ChartError(f"{len(rows)} rows of {width} numbers is too many to read in one chart "
                         f"(at most {MAX_MARKS} numbers)")
    if kind == "column" and (len(rows) > MAX_COLUMNS or len(rows) * width > 36):
        kind = "bar"             # long lists read better sideways, and every number fits
    if kind == "line" and len(rows) * width > MAX_LINE_POINTS:
        raise ChartError(f"a line chart prints every point's number: at most {MAX_LINE_POINTS} "
                         f"points in all ({len(rows)} rows of {width} is too many to read)")
    if kind == "pie":
        if width != 1:
            raise ChartError("a pie chart has one number per row")
        if len(rows) > MAX_PIE:
            raise ChartError(f"a pie chart has at most {MAX_PIE} slices")
        if any(v[0] < 0 for v in values) or sum(v[0] for v in values) <= 0:
            raise ChartError("a pie chart's numbers must be zero or more, and not all zero")
    scale = raw.get("scale", "").strip().lower()
    if scale not in ("", "linear", "log"):
        raise ChartError(f"SCALE must be log or nothing, not {raw.get('scale')!r}")
    log = scale == "log"
    if log and (kind == "pie" or any(x <= 0 for v in values for x in v)):
        raise ChartError("a log scale needs every value above zero, and no pie")
    return {"type": kind, "title": title, "unit": unit, "series": series or [""],
            "labels": labels, "values": values, "shown": shown, "log": log, "source": source,
            "alt": alt, "caption": caption}


def as_block(spec, bid):
    """The chart written back as the CHART block it came from: shown to the writer on a
    later pass so it can change the data rather than start again."""
    lines = [f"=== CHART {bid} ===", f"TYPE: {spec['type']}", f"TITLE: {spec['title']}",
             f"UNIT: {spec['unit']}"]
    if len(spec["series"]) > 1:
        lines.append("SERIES: " + " | ".join(spec["series"]))
    lines.append("DATA:")
    for i, (label, vals) in enumerate(zip(spec["labels"], spec["values"])):
        texts = (spec.get("shown") or [None] * len(spec["labels"]))[i] or [fmt(v) for v in vals]
        lines.append(label + " | " + " | ".join(texts))
    if spec.get("log"):
        lines.append("SCALE: log")
    lines += [f"SOURCE: {spec['source']}", f"ALT: {spec['alt']}", f"CAPTION: {spec['caption']}",
              "=== END ==="]
    return "\n".join(lines)


def fmt(v):
    """A number as a reader expects it: 4,000 · 12.5 · 0.0004. Never rounded: repr keeps
    every digit the float holds, and only whole numbers get thousands commas."""
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    text = repr(float(v))
    if "e" in text:
        return text
    whole, _, frac = text.lstrip("-").partition(".")
    return ("-" if v < 0 else "") + f"{int(whole):,}." + frac


def _scale_for(values):
    """One divisor for every tick on an axis, so 8,000 and 10k never sit side by side."""
    top = max((abs(x) for v in values for x in v), default=0)
    for n, s, at in ((1e12, "T", 1e12), (1e9, "B", 1e9), (1e6, "M", 1e6), (1e3, "k", 1e4)):
        if top >= at:            # 4,000 stays 4,000; 10,000 is 10k; 8.6 million is 8M
            return n, s
    return 1, ""


def _wrap(label, width):
    import textwrap
    return "\n".join(textwrap.wrap(label, width, break_long_words=True)) or label


FALLBACK_FONTS = ["Noto Sans CJK SC", "Noto Sans CJK JP", "Noto Sans CJK KR", "Noto Sans",
                  "Noto Sans Arabic", "Noto Sans Hebrew", "Noto Sans Devanagari", "Noto Sans Thai",
                  "Microsoft YaHei", "PingFang SC", "Hiragino Sans", "Apple SD Gothic Neo",
                  "Arial Unicode MS", "Segoe UI"]


def _fonts():
    """DejaVu Sans, then whichever fonts for other scripts this computer has, so a label in
    Chinese or Arabic is drawn rather than shown as boxes."""
    from matplotlib import font_manager
    have = {f.name for f in font_manager.fontManager.ttflist}
    return ["DejaVu Sans"] + [f for f in FALLBACK_FONTS if f in have]


def readable(colours):
    """The design's colours a reader can see on white paper (3:1, the contrast a chart's
    marks need) and tell apart, topped up with the neutral defaults."""
    def lum(c):
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)) if isinstance(c, str) else c
        f = lambda x: x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def rgb(c):
        return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) if isinstance(c, str) else tuple(int(x * 255) for x in c)
    out = []
    given = [c for c in (colours or []) if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", c)]
    for apart in (60, 30, 0):        # as distinct as possible, but always four to choose from
        for c in given + FALLBACK:
            if len(out) >= MAX_SERIES or c in out or 1.05 / (lum(c) + 0.05) < 3:
                continue
            if any(sum((a - b) ** 2 for a, b in zip(rgb(c), rgb(o))) ** 0.5 < apart for o in out):
                continue
            out.append(c)
    return out


def _label_size(kind, n):
    """Tick labels shrink for a long list, so it fits without overlapping."""
    return 11 if kind != "bar" or n <= 15 else 9


def _height(kind, labels, nser):
    """1600 wide, and taller than 900 when a long list of bars needs the room: each row as
    tall as its wrapped label (points at 200 dpi, with line spacing) or its bars."""
    if kind != "bar":
        return HEIGHT
    line = _label_size(kind, len(labels)) * DPI / 72 * 1.3
    need = sum(max(line * (lab.count("\n") + 1) + 14, 18 * nser + 12) for lab in labels)
    return int(min(3000, max(HEIGHT, 340 + need)))


def draw(spec, out_path, colours=None):
    """Draw a checked spec to out_path as a PNG. Every number is printed on the chart."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.ticker import FuncFormatter

    colours = readable(colours)
    kind, labels, values = spec["type"], spec["labels"], spec["values"]
    nser = len(values[0])
    if kind == "bar":            # horizontal bars: long labels wrap, and get room on the left
        labels = [_wrap(x, 22 if len(labels) <= 15 else 30) for x in labels]
    elif kind == "column" and len(labels) > 3:
        labels = [_wrap(x, 14) for x in labels]
    height = _height(kind, labels, nser)
    plt.rcParams.update({"font.family": _fonts(), "font.size": 11, "text.color": INK,
                         "axes.labelcolor": INK_SOFT, "xtick.color": INK_SOFT, "ytick.color": INK_SOFT,
                         "svg.fonttype": "none"})
    fig = plt.figure(figsize=(WIDTH / DPI, height / DPI), dpi=DPI, facecolor=PAPER)
    px = lambda y: y / height            # a distance from the top or bottom, in pixels
    try:
        ax = fig.add_axes([0.08, px(160), 0.87, 1 - px(180) - px(160)])   # refitted once drawn
        ax.set_facecolor(PAPER)
        fig.text(0.04, 1 - px(56), spec["title"], fontsize=15, fontweight="bold", color=INK, va="top")
        if spec["unit"]:
            fig.text(0.04, 1 - px(124), spec["unit"], fontsize=10.5, color=INK_SOFT, va="top")
        fig.text(0.04, px(30), "Source: " + spec["source"], fontsize=8.5, color=INK_SOFT)
        div, suffix = _scale_for(values)
        shown = spec.get("shown") or [[fmt(x) for x in row] for row in values]
        # 6 significant figures take the float noise out of tick positions (0.30000000000000004)
        ticks = FuncFormatter(lambda v, _: fmt(float(f"{v / div:.6g}")) + (suffix if v else ""))
        n = len(labels)
        small = 9 if n * nser <= 16 else 8 if n * nser <= 32 else 7
        if kind == "pie":
            ax.set_position([0.04, px(110), 0.92, 1 - px(190) - px(110)])
            vals = [v[0] for v in values]
            total = sum(vals)
            wedges, _ = ax.pie(vals, colors=[colours[i % len(colours)] for i in range(len(vals))],
                               startangle=90, counterclock=False,
                               wedgeprops={"linewidth": 1.5, "edgecolor": PAPER})
            ax.legend(wedges, [f"{lab}  {row[0]} ({v / total:.0%})" for lab, v, row in zip(labels, vals, shown)],
                      loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False, fontsize=10)
            ax.set_aspect("equal")
            ax.set_anchor("W")
        else:
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            for side in ("left", "bottom"):
                ax.spines[side].set_color(GRID)
            ax.tick_params(length=0)
            if kind in ("bar", "column"):
                band = 0.8 / nser
                for s in range(nser):
                    pos = [i - 0.4 + band * (s + 0.5) for i in range(n)]
                    vals = [row[s] for row in values]
                    c = colours[s % len(colours)]
                    bars = (ax.barh(pos, vals, height=band * 0.92, color=c, label=spec["series"][s])
                            if kind == "bar" else
                            ax.bar(pos, vals, width=band * 0.92, color=c, label=spec["series"][s]))
                    ax.bar_label(bars, labels=[row[s] for row in shown], padding=3, fontsize=small, color=INK)
                if kind == "bar":
                    ax.set_yticks(range(n), labels, fontsize=_label_size(kind, n))
                    ax.set_ylim(n - 0.5, -0.5)
                    ax.xaxis.set_major_formatter(ticks)
                    ax.grid(axis="x", color=GRID, linewidth=0.8)
                    ax.margins(x=0.12)             # room at the end of the longest bar for its number
                    if spec["log"]:
                        ax.set_xscale("log")
                else:
                    ax.set_xticks(range(n), labels, rotation=0 if n <= 6 else 30,
                                  ha="center" if n <= 6 else "right")
                    ax.yaxis.set_major_formatter(ticks)
                    ax.grid(axis="y", color=GRID, linewidth=0.8)
                    ax.margins(y=0.1)
                    if spec["log"]:
                        ax.set_yscale("log")
            else:        # line: every point carries its number, series above and below in turn
                for s in range(nser):
                    vals = [row[s] for row in values]
                    ax.plot(range(n), vals, color=colours[s % len(colours)], linewidth=2.4,
                            marker="o", markersize=4.5, label=spec["series"][s])
                    for i, v in enumerate(vals):
                        up = s % 2 == 0
                        ax.annotate(shown[i][s], (i, v), textcoords="offset points",
                                    xytext=(0, 7 if up else -12), ha="center", fontsize=small,
                                    color=colours[s % len(colours)] if nser > 1 else INK)
                ax.set_xticks(range(n), labels, rotation=0 if n <= 8 else 30,
                              ha="center" if n <= 8 else "right")
                ax.yaxis.set_major_formatter(ticks)
                ax.grid(axis="y", color=GRID, linewidth=0.8)
                ax.margins(y=0.15)
                if spec["log"]:
                    ax.set_yscale("log")
            ax.set_axisbelow(True)
            if nser > 1:
                ax.legend(frameon=False, fontsize=9.5, loc="lower right", bbox_to_anchor=(1, 1.01),
                          ncol=nser)
        if kind != "pie":
            _fit(fig, ax, height)
        fig.savefig(out_path, format="png", dpi=DPI, facecolor=PAPER)
    finally:
        plt.close(fig)
    return out_path


def _fit(fig, ax, height):
    """Move the plot so its tick labels fit: measured once drawn, not guessed from lengths."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    ys = [t.get_window_extent(r).width for t in ax.get_yticklabels() if t.get_text()]
    xs = [t.get_window_extent(r).height for t in ax.get_xticklabels() if t.get_text()]
    left = min(0.45, max(0.08, (max(ys, default=0) + 36) / WIDTH))
    bottom = min(0.45, max(160 / height, (max(xs, default=0) + 70) / height))
    top = 1 - 180 / height
    ax.set_position([left, bottom, 0.95 - left, top - bottom])
