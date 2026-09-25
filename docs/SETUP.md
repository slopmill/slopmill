# Setting up slopmill

About ten minutes, once. You will paste a few lines into a terminal (the window where you
type commands); each one is below, ready to copy.

## 1. Get an API key

slopmill uses an AI service through your own account. Pick one:

- **OpenAI** (writes with GPT-6 Sol and can draw pictures): go to
  [platform.openai.com/api-keys](https://platform.openai.com/api-keys), sign in, click
  **Create new secret key**, and copy it somewhere safe for a minute. Then add some credit
  under **Settings → Billing**: a ChatGPT subscription does not pay for API use, it is a
  separate account balance. $5 goes a long way.
- **Anthropic** (writes with Claude; no pictures): go to
  [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys),
  create a key, and add credit under **Billing**.

A key is like a password that can spend money: do not share it or paste it anywhere else.

## 2. Install uv

`uv` fetches the right version of Python and everything slopmill needs, without touching
the rest of your computer.

**Mac**: open **Terminal** (press Cmd+Space, type `Terminal`, press Return) and paste:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows**: open **PowerShell** (Start menu, type `PowerShell`, press Enter) and paste:

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Linux**: in a terminal, the same line as the Mac.

Then **close the terminal window and open a new one**, so it can find `uv`.

## 3. Get slopmill

On the GitHub page you were sent, click the green **Code** button, then **Download ZIP**.
Unzip it and move the `slopmill-main` folder somewhere you will find it again, such as
your Documents folder.

Then point the terminal at that folder. The sure way on any computer: type `cd` and a
space, **drag the folder from Finder or File Explorer onto the terminal window** (its
location is typed in for you), and press Return / Enter.

Also on Windows: open the folder in File Explorer, click the address bar at the top, type
`powershell` and press Enter. A PowerShell window opens already inside the folder.

(If you use git: `git clone` the repository and `cd` into it instead.)

## 4. Set it up

```
uv run slopmill setup
```

The first run takes a minute while `uv` downloads what it needs. Then it asks:

1. **Which AI service**: 1 for OpenAI, 2 for Anthropic, 3 for another one.
2. **Your API key**: paste it and press Return. Nothing shows while you paste; that is on
   purpose.
3. **Your newsletter's name** and **your name**: used to tell the model whose voice it is
   writing in. Press Return to skip either.

It saves your settings in a folder called `workspace`, your key in `workspace/.env`
(on a Mac or Linux readable only by your user account; on Windows it sits in your own user
folder), and a practice issue to try things on.

## 5. Start it

```
uv run slopmill start
```

Your browser opens on slopmill. If it does not, copy the link the terminal prints (it
starts with `http://127.0.0.1:`) into your browser; the link includes a private access
code, so use that exact link the first time. Treat the link like a password: do not post
a screenshot of the terminal with it showing.

**To stop it**, go back to the terminal and press **Ctrl+C**. **To start it again later**,
open a terminal in the folder (step 3) and run `uv run slopmill start`.

## Your first issue

Open **In Which We Begin a Newsletter** (it is already selected). It is written in the example voice and design, which borrow from A. A. Milne; your own newsletter uses your own. On **Plan**, press **Write the
drafts**. On **Draft**, read what came back and press **Edit** on anything you want to
change. On **Proof**, see it as your readers will.

Then make it sound like you: **Voice → Manage**, upload three to five things you wrote
yourself (old newsletters, blog posts, long emails), and edit `VOICE.md` and `writer.md` in
the same panel. See [VOICE-PACKS.md](VOICE-PACKS.md).

## Where your things are

Everything lives in the `workspace` folder inside slopmill's folder:

| What | Where |
|---|---|
| Each issue's text | `workspace/issues/<issue>/issue.md` (plain text you can open anywhere) |
| Its pictures | `workspace/issues/<issue>/images/` |
| Your voice packs | `workspace/voices/` |
| Settings | `workspace/slopmill.toml` |
| Your API key | `workspace/.env` |

**Back up the `workspace` folder**; that is all of your work. It also holds your API key
(`workspace/.env`), so keep backups private, or leave that one file out and paste the key
again with `setup --force` after a restore. Nothing is stored anywhere else, and nothing
leaves your computer except what is sent to the AI service to write.

## Getting an issue out

On **Proof**, under **Download**:

- **Web page**: one `.html` file with the pictures inside. Open it, print it to PDF, or post it.
- **Word**: a `.docx` for Word, Google Docs or Pages (the words and pictures, not the colours).
- **All files**: a zip with the web page, `-email.html` (paste into your email tool's HTML
  editor), Markdown, Word, and the pictures in `images/`.

An issue with a prompt not written yet is not downloaded: write it on Draft, or delete it.
Pictures in an email must live on the web: upload them and set `asset_base` in your design
to where they are ([DESIGN-FILES.md](DESIGN-FILES.md)).

From a terminal, `uv run slopmill build workspace/issues/001-welcome/issue.md --no-asset-check`
writes the HTML pieces (`body-email.html`, `body-site.html`) next to the issue.

## Changing things later

- **A different key or service**: `uv run slopmill setup --force`. It tells you if an
  older key is still in `workspace/.env`; delete that line if you do not want it kept.
- **A different model**: edit `workspace/slopmill.toml` (see [PROVIDERS.md](PROVIDERS.md)),
  then stop and start slopmill.
- **Updating slopmill**: download the new ZIP, unzip it, and move your old `workspace`
  folder into the new folder. (With git: `git pull`.)

## If something goes wrong

| You see | What to do |
|---|---|
| `uv: command not found` (or "not recognized") | Close the terminal and open a new one. If it persists, repeat step 2. |
| `slopmill is not set up here yet` | You are in the wrong folder, or setup has not run: do step 3's `cd`, then step 4. |
| The browser shows **Locked** | Use the full link printed in the terminal; it carries the access code. |
| `the model returned HTTP 401` | The key is wrong or was deleted: `uv run slopmill setup --force` |
| `HTTP 429` or `insufficient_quota` | The API account needs credit (step 1). |
| `port 8440 is in use` | slopmill is probably already running in another terminal window. `start` moves to the next free port on its own. |
| Pictures never appear | Pictures need OpenAI. With Anthropic they are off (Models, in the top bar, shows what is on). |

slopmill only listens to your own computer: nobody else on your network or the internet
can open it.
