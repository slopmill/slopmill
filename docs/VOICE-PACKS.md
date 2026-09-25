# Building a voice pack

A voice pack is how slopmill learns to sound like you. It is a handful of text files the
writing model reads before every pass. You build one in the app: **Voice → Manage → New
voice pack**, then upload your files.

The `storybook` pack that ships with slopmill writes in the manner of A. A. Milne. It is
there to show the shape of a pack, not to be copied: the point is your own writing. Please
don't use slopmill to imitate other living writers.

## The three kinds of file

**Samples: the part that matters.** Three to five pieces you wrote yourself, start to
finish, with nobody's edits. Old issues, blog posts, long emails. The model copies what it
sees far more faithfully than anything it is told, so a good sample beats a page of rules.

- Your own words only. Nothing an AI drafted and you fixed up: it teaches the model to
  sound like the model.
- Pick pieces that are like what you are writing now: same audience, same length.
- Plain text or Markdown. Paste out of the web page, not the email HTML.
- 5 to 10 KB each (roughly 800 to 1,600 words) is plenty.

**The brief: one short file.** The standing instruction, sent first on every call. Who is
writing, who reads it, and the few rules that matter most. Half a page. slopmill starts
you with one to edit.

> You are ghost-writing part of a newsletter for its author. Write the way the author
> writes in the attached samples. Short paragraphs. No em-dashes. Never explain a joke.

**Rules: optional.** What you always do and never do, each backed by a line from your
samples. Words you never use. Keep it to one file.

- Quote yourself. "Starts paragraphs with And or But: *And so, maybe it all feels
  done.*" teaches more than "be conversational".
- Only rules you would actually enforce. A long list of "avoid" is noise the model
  half-follows.

## What does not go in

- **How it looks.** Colors, HTML, which component to use: that is the design's job, and
  slopmill already tells the model what markup it may use. A voice file that talks about
  `<strong>` tags or hex colors fights the design.
- **How it is published.** Upload steps, linters, file names, tools.
- **Notes to yourself.** Changelogs, "rule added on the 12th because...", calibration
  results. Useful to you, confusing to the model. Keep them somewhere else.

## Size

The panel shows what a pass will send against what your writer takes. The command route
takes about 115 KB per call, voice pack and issue together, so keep the pack under
about 60 KB to leave room for a long issue. API providers take much more (see
[Choosing a model](PROVIDERS.md)).

Turn files off rather than deleting them when you want to try the pack without one.
Every replaced or deleted file is kept in the pack's `.history` folder.

## Checking it works

Make a scratch issue, write one paragraph yourself, add a prompt under it ("Keep going,
about 150 words") and Generate. Read it out loud next to your own paragraph. If you can
hear the seam, add a sample that is closer to what you just wrote, or cut a rule that is
pulling the wrong way.
