# Writing a design file

A design is how an issue looks: every color, every HTML tag, every component. slopmill
turns your Markdown into HTML by handing each block to one of the design's templates. A
design file is that whole design in one text file, `NAME.design.toml`, that you can share
and upload.

**Easiest start: download one and change it.** In the app, **Design → Manage → Download**
the design you use now. Every section below is already in it. Edit, give it a new `name`,
and upload it under **Design → Manage → Upload**. slopmill checks it before storing it:
it must load, and it must draw a sample issue that uses every block and component it
declares. If anything is wrong you get the reason and nothing is saved.

From a terminal: `slopmill design export starter --out mine.design.toml` and
`slopmill design check mine.design.toml`.

## The sections

```toml
format = "slopmill-design/1"        # always this, first

[pack]
name = "my-letter"                    # lowercase letters, digits, dashes
asset_base = "https://example.com/newsletter/img/"   # where published images live (https)
read_url = "https://example.com/issues/{slug}"       # the web version of an issue
assets_dir = "images"                 # the folder next to each issue for its images

[meta]                                # the front matter each issue has
fields = ["title", "slug", "subject"]
required = ["title", "slug"]
types = { number = "int" }            # anything not listed is text

[inline]                              # attributes added to inline elements
link = 'style="color:#2255cc"'        # email needs inline styles, so most are style="..."
strong = ""
emphasis = ""
accent = 'style="color:{colour}"'    # **words**{.green} fills {colour} (the placeholder's spelling) from [accents]

[accents]
green = "#1f9a3f"

[blocks]                              # which template draws each plain block
paragraph = "paragraph.html"
quote = "quote.html"                  # optional
list = "list.html"                    # optional
heading = { 2 = "heading.html" }      # one per heading level you allow

[components.figure]                   # ::: {.figure} ... :::
shape = "figure"                      # one image or video, then an optional caption
template = "figure.html"

[components.note]                     # ::: {.note label="..."} ... :::
shape = "container"                   # one or more paragraphs
template = "callout.html"
attrs = { label = "optional" }        # or "required"
paragraphs = [1, 3]                   # at least 1, at most 3
params = { border = "#2255cc" }       # fixed values handed to the template
describe = 'a side note: ::: {.note} one short paragraph :::'   # offered to the writing model

[page]
css = '''...'''                       # the page around the issue in the editor's preview
site_head = '''<style>...</style>'''  # put in front of the site version of every issue

[templates]
"paragraph.html" = '''<p style="margin:0 0 18px">{{ content }}</p>'''
```

A component with a `describe` line is offered to the writing model; one without is yours
to place by hand. Picture prompts use the first component whose shape is `figure`, so a
design that should take generated pictures needs one.

## What each template receives

Templates are [Jinja](https://jinja.palletsprojects.com/). Every value is escaped for you;
content that is already HTML (a paragraph's text, a caption) is passed through as it is.

| Template | Gets |
|---|---|
| paragraph | `content` |
| heading | `content`, `level` |
| quote | `content` |
| list | `items` (list), `ordered` (true/false), `start` |
| figure | `media.kind` ("image" or "video"), `media.src`, `media.alt`, `media.light` (a light-mode twin, or none), `media.poster`, `media.mime`, `caption`, `attrs`, `params` |
| container | `paragraphs` (list), `attrs`, `params` |

Every template also gets `target` ("web" or "email": draw them differently where email
clients need it, for instance a video becomes its poster image), `read_url` and `meta`.

## Rules a design file has to follow

- Only the sections above. Anything else is refused, so a shared file cannot carry a
  setting that a later version of slopmill might act on.
- No commands, no file paths. `asset_base` and `read_url` must be `https://`.
- Templates run in a sandbox: they draw HTML and cannot reach files, the network or Python.
- Up to 512 KB for the file, 64 KB per template.
- The preview runs in a sandboxed frame where no script can run, so a template that
  includes a `<script>` shows nothing from it in the editor.

The designs that ship with slopmill (`starter`, and the `storybook` example) are also
kept as folders in `packs/`, so their templates can be read and edited as separate files;
`slopmill design export` turns a folder into one file.
