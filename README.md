# PIXELDECK

An 8-bit, purple/blue/pink terminal dashboard for everything that wants your attention:
GitHub review requests, your open PRs (CI + conflicts + review state), GitHub notifications,
CI runs, mail (Microsoft 365, IMAP, Apple Mail), your calendar, Slack mentions and DMs,
Linear issues, plus live CPU/memory/network/battery, Docker and git repo/worktree status.

## Install

Made for macOS (Spotify, Apple Mail and the keychain are Mac-only). Needs Python 3.14 and
[uv](https://docs.astral.sh/uv/); the GitHub tiles use the [GitHub CLI](https://cli.github.com) (`gh auth login`).

```bash
git clone https://github.com/ryanAtriumAi/pixeldeck-public.git pixeldeck
cd pixeldeck
uv tool install --editable .   # puts `pixeldeck` on PATH
pixeldeck setup                # connect your sources
```

Keep the clone: updates come from it (`u` in the app pulls new commits).

## Run

```bash
pixeldeck              # title screen, then the dashboard
pixeldeck --no-splash
pixeldeck doctor       # which sources are connected
```

Keys: `0`-`9` jump to a tile (press the number twice, e.g. `88`, to open that tile's page: see
[Pages and popups](#pages-and-popups)), arrows move, `Enter` opens the item in a popup, `o` opens it in the browser, `c` copy URL,
`r` re-sync everything, `?` ask (chat across every source), `+` add a task, `p` play/pause Spotify (`<` `>` skip, `[` `]` volume), `n` the now-playing page, `s` setup, `u` update now, `ctrl+p` command palette, `q q` quit (a single `q` just asks; `ctrl+q` quits at once).
On a page, the footer lists that page's own keys, and `esc` always goes back.

## Editing text

Every text box edits like a Mac field, whichever way your terminal sends the keys: Option+←/→ jumps
by word (add Shift to select), Option+Delete deletes the word before the cursor, Fn+Option+Delete the
word after, Cmd+←/→ goes to the start/end of the line and Cmd+Delete clears to the line start. If
Option+Delete only removes one character, iTerm2 isn't sending Option at all: Settings → Profiles →
Keys → Key Mappings → Presets → "Natural Text Editing".

## Settings

`s` opens Settings: every connection in one list with a live check beside it (who you're signed in
as, missing Slack permissions, how much the mail filter hides, whether a release environment answers).
Edits save to `~/.config/pixeldeck/config.toml` as you make them, comments kept; `ctrl+z` undoes,
"reset section" restores defaults, `ctrl+o` opens the file. Tokens show masked and stay in the keychain.

## Profile and themes

The first time PIXELDECK starts without a profile it asks for one: your **name** (shown beside P1 in
the header, and what reply assist signs drafts with), an optional **email** (its domain tells reply
assist who is a colleague and who is outside your company), and a **theme**. `ctrl+s` saves, `esc`
puts it off until the next start. Saving writes `[profile]` to `config.toml` (and `[ui] theme` if you
picked another one) and changes nothing else, so a setup that already works stays as it is. On a
brand-new install the connect-your-accounts menu opens next.

Themes: **ARCADE** (the original purple, pink and cyan), **PHOSPHOR** (green CRT), **AMBER**,
**OCEAN** (navy, teal and coral), **FROST** (soft blue-grey), **MONO** (greyscale) and **DAYLIGHT**
(light, for bright rooms), plus **CHRISTMAS**: pine, berry red and gold, with snow falling through the
header, a candy-cane logo with softly glowing lights, a tree in place of the invader, and fairy lights for
the rule lines. Each row in the picker is drawn in that theme's own colours, and moving
through the list tries each one on: the whole dashboard restyles at once, nothing restarts. In the
profile screen SAVE keeps the one you stopped on and `esc` puts the old one back. Change name, email or
theme any time in Settings (`s`) → Profile: Enter keeps a theme, moving away without Enter puts the
saved one back, `ctrl+z` undoes. Or set `[ui] theme = "ocean"` in `config.toml`; a running PIXELDECK
picks it up when the file is saved.

## Setup

Press `s` (or `Enter` on any tile showing SETUP, or run `pixeldeck setup`). Each source walks you
through getting its key, tests it against the real API, then stores it in the macOS keychain.
Microsoft 365 signs in with a device code without leaving the terminal.

## Pages and popups

**Popups.** `Enter` on any row opens it in a popup over what you were looking at: an email, a ticket,
a PR, a CI run, a release, a Slack conversation. `esc` (or a click outside it) closes it, `n` / `p`
step to the next and previous row, `o` opens it in the browser, and `f` makes it fill the screen
(`ctrl+l` in a Slack popup, which brings the conversation list back). Settings → System → POPUPS
off (`[ui] popups = false`) opens the full page every time instead.

**Pages.** Pressing a tile's number twice opens that tile's page. Each page starts with a strip of
cards (the numbers that matter for that section) and lists whatever the selected card counts:
`←` / `→`, the card's number, or a click picks a card. `Enter` opens a row in a popup, `/` filters
the list as you type, `o` opens the row in the browser, `ctrl+r` reloads, `esc` goes back. A panel
on the right charts the section. The footer lists each page's own keys.

A page opens with numbers straight away and updates them in place when its load finishes; the header
says which you are looking at (`numbers from 3m ago · updating…`, then `synced 12s ago`). Pages opened
before in this run come back whole (rows and side panel too); RELEASES draws from what its tile already
loaded. After a restart a page shows its last numbers until the load arrives: only the card numbers
and hints are kept on disk (`~/.config/pixeldeck/pages-cache.json`), never the rows under them.

| Press | Page | Cards |
| --- | --- | --- |
| `11` `22` `33` | GITHUB | reviews waiting on you, your open PRs, inbox, PRs you opened / merged / reviewed |
| `44` | RELEASES | one per environment, latest release, drift, releases |
| `55` | MAIL | inbox, unread, new since you looked, today, important, with files |
| `66` | Slack console | (conversations and messages, see [Slack](#slack)) |
| `77` | Agenda | (day columns, see [Agenda](#agenda)) |
| `88` | LINEAR | outstanding by priority, new, assigned, created, closed, backlog |
| `99` | CI | failing, running, recent runs, pass rate, one per repo |
| `00` | AGENT | (suggestions and tasks, see [Agent](#agent-hermes-optional)) |

What each page is for:

- **GITHUB (`11`, `22`, `33`: it opens on the card for the tile you came from).** REVIEWS lists the
  PRs waiting on you, longest wait first. MY PRS puts the ones that need you first (CI failing, a
  conflict, changes requested), then approved, waiting and drafts. INBOX groups notifications into
  for you, participating and watching; `e` marks the highlighted one read on GitHub. OPENED and MERGED
  count your PRs over 7, 30 or 90 days (`t`). REVIEWED counts other people's PRs you reviewed,
  commented on or merged (GitHub's own `reviewed-by:` only sees formal reviews, which misses most of it);
  each row says which. The panel charts the health of your open
  PRs, how long reviews have waited, the inbox by reason, and opened against merged for eight weeks.
- **RELEASES (`44`).** A card per environment shows the build it runs, whether it is up, and how long
  it has been live; its list is that environment's health and deploy history. LATEST says where the
  newest release runs, DRIFT lists the commits production doesn't have yet, RELEASES lists every
  release. The panel charts uptime, deploys per week, and how far behind production is. Read-only.
- **CI (`99`).** FAILING lists the workflows whose latest run on the watched branch failed and since
  when, RUNNING what is in progress or queued, RECENT every run by day. PASS RATE covers the last 50
  runs per repo, and each repo gets its own card when you watch two or three. The panel ranks
  workflows by pass rate and by median duration. Read-only: nothing is re-run from here.
- **MAIL (`55`)** and **LINEAR (`88`)** are described under
  [Repos, reading and replying](#repos-reading-and-replying) and [Linear](#linear).

## Repos, reading and replying

- **GitHub repos:** `s` → GITHUB lists every repo your `gh` account can see. Tick the repos to
  MONITOR (your PRs, CI) and, separately, the ones for the INBOX (notifications).
  Nothing ticked means everything. The REVIEW QUEUE ignores MONITOR: a PR that asks for your
  review, or is assigned to you, shows up whatever repo it's in.
- **Reading:** `Enter` opens an item inside the app, in a popup; `o` expands to the browser, `n`/`p` step
  through the list it came from, `f` fills the screen, `ctrl+r` reloads.
- **Mail page (`55`):** the newest 150 emails after your filters, sliced by the cards. The panel on the
  right previews the highlighted email, then shows who writes to you most and the last seven days.
  `d` deletes, `u` flips read / unread, `space` selects several emails for `d` or `u`, `r` opens the
  email with the reply box ready, `z` undoes the last delete.
- **Read in Outlook too:** opening a Microsoft 365 email marks it read in the mailbox, so Outlook on
  every device agrees (Settings → Mail → MARK READ IN OUTLOOK, `[mail] mark_read`).
- **Delete:** `d` on the Mail page or on an open email moves it to Deleted Items, the same as Delete in
  Outlook, so it can be recovered there; `z` puts the last one back in the inbox.
- Marking read and deleting change your mailbox, so Microsoft asks once for `Mail.ReadWrite` the first
  time (until then PIXELDECK only has read access). Say no and mail you open here stays unread in
  Outlook. Both work for Microsoft 365 mail; IMAP and Apple Mail stay read-only here.
- **Reply:** on an email page `r` replies, `R` replies all. SEND needs a second press. Microsoft 365
  asks once for `Mail.Send` the first time you send; Apple Mail / IMAP open a reply in your mail
  app with your text on the clipboard.
- **Attachments:** an email page links every attachment, including images pasted into the body
  (tiny signature logos are hidden). Click one, or press `a` for a picker, to open just that file:
  - images, PDFs, video, audio and text files open in your browser (text, HTML included, is
    shown as text, never run);
  - Word, Excel, PowerPoint, Pages, Numbers, Keynote and RTF open **read-only** in your browser,
    rendered by macOS Quick Look, so macros never run;
  - in the picker, `o` opens the file in its app instead (to edit), and `f` shows it in Finder;
  - archives, installers, scripts and other executables are only shown in Finder, never opened.

  Files are fetched on demand into `~/.cache/pixeldeck/attachments` (private, marked as
  downloaded) and swept after a day. Microsoft 365 and IMAP mail are covered; Apple Mail is not.

## Releases

The RELEASES tile opens with **LATEST**, the newest GitHub release (pre-releases count, so a new
`dev-*` build shows up right away). Its tag stays **pink** until every env runs it, and turns
**green** once they all do; the line under it says which
envs have it. `Enter` opens its release notes in the app (PR numbers and ticket ids are links,
`]`/`[` jump between sections, `o` opens the release on GitHub). Below it, each env's deployed
version and the build tag it came from: **pink** on the envs already running LATEST while it's
still rolling out, **green** otherwise, and everything goes green once test, staging and prod all
run it. Thin lines split the tile into latest · environments · stats.

While an env is deploying, its versions pulse green ↔ pink, and LATEST pulses too when it's the
build going out. TEST's deploy is its running GitHub workflow. STAGING and PROD ship from this Mac,
so an env with `deploy_cmd` (a regex) counts as deploying while a process whose command line
matches is running; only that process's `REF=` is read, to name the build. Checked every 5s,
and RELEASES refreshes every 30s while anything is deploying.

```toml
[[releases.env]]
name = "STAGING"
pin_file = "deploy/staging.tfvars"
deploy_cmd = "scripts/(ship|deploy)-staging"
```

## Linear

LINEAR QUESTS lists the issues assigned to you (all of them, paged), in-progress first, then by
priority; the title counts every match and the ▶ ACTIVE chip counts what's in progress. Settings →
Linear: which issues (assigned to me / created by me / subscribed / whole team, which means the teams
you're on unless you set team keys), which states
(in progress, todo, triage, backlog), sort (status, priority, updated, due), how many rows to show,
and team keys. Press **`f`** to filter by status (your workflow's own states: In Progress, In Review,
Todo…) and priority, multi-select; the tile says what's hidden. Ticket pages render Linear's
Markdown: headings, tables, code, ☐/☑ checklists, ticket references as links, each comment as its own
block.

**The Linear page (`88`)** counts your work. OUTSTANDING is what's open and assigned to you outside
the backlog, listed urgent first, then high, medium, low and no priority. NEW is what was assigned to
you and not opened yet. ASSIGNED, CREATED and CLOSED count the tickets assigned to you, created by you,
and yours that were completed in the period (`t` switches between 7, 30 and 90 days). BACKLOG is your
own backlog, with the count for your whole team (`[linear] teams`, else the teams you're on) under it.
The panel charts outstanding work by priority and by status, tickets created against closed over the
last eight weeks, and what's overdue or due this week. `s` searches Linear from the page.
ASSIGNED counts tickets that are still yours: one handed to you and then to someone else isn't in it.

Tickets assigned to you in the last two weeks that you haven't opened yet come first with a pulsing
**NEW** tag (the tile counts them), even when the state or status filter would hide them, as with a new
ticket landing in Backlog. Opening one (its page, the browser, or from search) clears the tag; being
assigned it again brings it back. A ticket assigned while PIXELDECK is open also pops a toast.
Settings → Linear → NEW FOR sets the window (0 turns it off). Nothing changes in Linear: what you've
opened is kept in `linear-state.json`.

### Search

`/` searches every Linear ticket with Linear's own search (titles, descriptions, ids), as you
type. A ticket id like `ABC-123` jumps that ticket to the top. `ctrl+o` keeps open tickets only,
`ctrl+t` only yours. `↓` moves into the results; `Enter` opens the ticket in the app (`n`/`p` step
through the results), `ctrl+b` opens it in Linear, `ctrl+y` copies its link. Typing while in the
results goes back to the search box, and `/` reopens your last search.

## Ask (local model, every source)

`?` opens a chat with your local model (LM Studio, the same server reply assist uses). Ask in plain
words: "what needs my attention today?", "anything from Sam about the launch plan?", "which open PRs
touch RBAC?", "when am I meeting Dana?". The model searches for itself, then answers with citations:

- **dashboard**: what every tile shows right now (review queue, your PRs, inbox, Slack, agenda,
  Linear, CI, releases per env);
- **GitHub**: PRs and issues in your monitored repos (GitHub search syntax works);
- **Microsoft 365 mail** (KQL like `from:` works) and **calendar** (any date range);
- **Slack** (`from:@name`, `in:#channel`, `after:` work) and **Linear**;
- **read**: opens any hit in full (an email body, a ticket with comments, a PR, a Slack thread).

You see each search as it runs (⌕ Slack "staging deploy" · 12 hits), then the answer as it's written.
Every fact is cited like `[s3]`: click it, or press `↓` and Enter in SOURCES, to open that item in the
app (`ctrl+b` opens it in the browser). `esc` stops an answer, then closes; the chat is still there
when you come back, `ctrl+n` starts over, `ctrl+y` copies the last answer.

It is read-only: it cannot send, reply, post, create or change anything. Everything stays on this Mac.
Questions only go to the local server (`assist.allow_remote` is off by default). Use a model trained for
tool use (Qwen 3.x, Ministral 3). The model's context (shown in the header) limits how much it keeps:
older search results get trimmed first. Loading the model with a bigger context gives longer chats.

**Ask the agent instead.** `ctrl+t` in Ask switches to your agent (Hermes, see below): the question goes
to `hermes chat`, which answers with its own tools and memory plus PIXELDECK's through `pixeldeck mcp`.
You see each tool it uses (⚙ …), then the answer; follow-ups continue the same Hermes session until
`ctrl+n`. Hermes can act, not just read, so each question tells it not to send, post or change anything
unless you ask it to. Each mode keeps its own chat, `?` reopens the one you used last, `[ask] default =
"agent"` makes the agent the first choice, and `?` on the AGENT page asks the agent directly. Hermes needs
its model loaded with at least 64K context: PIXELDECK's model picker (`l`) loads models with 64K
(`[assist] context`), and reloads one that's loaded with less.

## Agent (Hermes, optional)

Tile **0, AGENT** (top left) is what your agent left you: the latest **brief**, **suggestions**, and your
**tasks**. `00` opens the full AGENT page: the list on the left, the selected item on the right.

- **Suggestions** never act on their own. Enter (or the pink button) opens the screen you'd use anyway,
  already filled in: a reply with the agent's draft, the Slack box with a message, or NEW EVENT. You still
  press SEND / Enter / CREATE. `z` snoozes for 2 hours, `x` dismisses (the agent sees what you dismissed
  and stops suggesting it), `o` opens what it's about.
- **Tasks:** Enter marks done, `t` moves to tomorrow, `+` adds one (`Call Sam @ fri 2pm`). Overdue
  tasks are red, due within a day yellow. The agent can add and complete tasks too.
- New briefs and suggestions pulse and ping until you open them.

The agent is [Hermes](https://github.com/NousResearch/hermes-agent), running on the same local model.
`pixeldeck mcp` gives it PIXELDECK's sources as an MCP server: the dashboard (a snapshot the running app
writes to `dashboard.json`), GitHub, mail, calendar, Slack, Linear and `read`, plus `propose`, the task
tools and `post_update`. That's all it can do: it can't send, post, reply or invite, and your tokens stay
in PIXELDECK. Suggestions, tasks and briefs live in `~/.config/pixeldeck/agent.db` (private).

Hermes setup (`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  pixeldeck:
    command: /Users/you/.local/bin/pixeldeck
    args: ["mcp"]
platform_toolsets:      # scheduled jobs and the API get no shell, files or browser
  cron: [memory, session_search, todo]
  api_server: [memory, session_search, todo]
```

Jobs (`hermes cron list`): **pixeldeck: brief** weekdays 8:30, **pixeldeck: watch** hourly 9:15-17:15
on weekdays (suggests at most 2 things, only when something new needs you). They run while the Hermes
gateway runs (`hermes gateway install` once). Hermes needs the model loaded with at least 64K context.

## Agenda

`a`, `77` or Enter on an AGENDA row opens the agenda: five day columns (today first; `w` hides
weekends) above a details pane for the selected event (time, where, organiser, your RSVP, attendees
with theirs, the description). ←/→ change day, ↑/↓ change event, click works too; `j` joins, `o`
opens in Outlook, `c` copies the link, `,` / `.` move five days, `t` jumps to today.

`n` schedules an event: title, day (`today`, `fri`, `11/14`), start (`2pm`, `9:30`), length (`45m`,
`1h30`), invitees, place, Teams link, notes. The preview shows exactly what will be created and
warns about overlaps; with invitees, CREATE needs a second press because invites go out. The first
time, Microsoft asks once for Calendars.ReadWrite.

## Slack

Enter on a SLACK row opens that conversation in a popup (messages and the message box; `ctrl+l` opens
the whole console). `m` or `66` opens the Slack console: conversations on the left (unread first,
then DMs, then channels; ctrl+f to find), messages on the right, a message box at the bottom
(Enter sends). On a message: `t` thread, `+` react, `e` edit yours, `x` delete yours (press twice),
`o` open in Slack. ctrl+n starts a DM. Opening a conversation marks it read in Slack. New DMs and
mentions pulse on the dashboard until you open them.

Order everywhere: DMs first, then ★ starred channels, then everything else (unread first within
each). Star a channel with `*` in the console; stars are saved as `[slack] starred` in config.toml
(Slack doesn't let apps read the sidebar's Starred section). App and bot traffic is hidden by
default (`[slack] hide_apps = true`): app DMs, app posts and app mentions stay out of the dashboard
and unread counts; `ctrl+b` in the console flips it and saves the setting.

Slack state lives in a local SQLite cache (`~/.config/pixeldeck/slack.db`, private): people,
conversations, unread state and the last ~100 messages of what you've opened. Restarts and reloads
start from it instantly, then refresh what's stale; DMs that arrived while pixeldeck was closed still
pulse. DMs quiet for more than 7 days (`[slack] dm_days`, 0 = never) drop off the lists unless
unread, and stay findable by name in the console.

Setup: `s` → SLACK → COPY MANIFEST, paste it under your app's App Manifest, reinstall, then paste
the User OAuth Token (xoxp-). The app has only user scopes and no bot: it can do exactly what you
can, as you, and nothing else. Keep "Manage Distribution" off so nobody else can install it.

## Reply assist (local model, your tone)

With LM Studio's server running (Developer → Start Server), pink suggestions appear in the Slack
message box and in email replies; **→** takes one, typing past it ignores it. An empty box gets a
whole-reply suggestion. Nothing is ever sent for you: Enter / SEND still do that.

The bottom strip's **LLM** block shows the reply model, whether LM Studio has it loaded, its size
and the last suggestion's speed (✖ when LM Studio's server is off). Press **`l`** to pick a model:
Enter loads it and makes it the reply model (optionally unloading others to free memory), `u`
unloads one, `U` unloads all.

`s` → Reply assist: on/off, server (default `http://localhost:1234/v1`, local only unless
`assist.allow_remote`), model (any chat model LM Studio has), TEST and WARM UP. **LEARN MY TONE**
reads your recent Slack messages and sent mail, has the local model summarise how you write, and
keeps notes + a few examples in `~/.config/pixeldeck/assist-style.json` (private, editable). Mail to
people outside your company domain stays casual-professional.

## Claude usage and limits

The bottom strip shows Claude Code token use (5 hours, today, 7 days) read from
`~/.claude/projects`. Plan limits (5-hour and weekly %) only reach Claude Code's status line, so
`s` → CLAUDE → ENABLE wraps your existing status-line command: the wrapper saves `rate_limits` to
`~/.config/pixeldeck/claude-limits.json` and passes the same input to the original command. TURN OFF
restores it; `settings.json.pixeldeck-bak` is kept as a backup.

## Spotify

The SYSTEM strip shows what Spotify is playing: the cover as 8×8 pixel art, the track, artist and
album, a progress bar, volume and shuffle/repeat. `p` plays or pauses (on a closed Spotify it opens
the app and plays), `<` / `>` skip back and forward, `[` / `]` change the volume.

`n` opens the now-playing page: the cover as big pixel art, with the record sliding out of its sleeve
and spinning while the track plays, a row of sprites dancing on the beat and an 8-bit equalizer along
the bottom. Spotify doesn't share the audio, so the equalizer follows a steady beat rather than the
song itself. On that page `p` or space plays and pauses, `<` `>` skip, `←` `→` jump 10 seconds,
`[` `]` change the volume, `s` toggles shuffle, `r` repeat, `o` brings Spotify to the front.

It talks to the Spotify app on this Mac through AppleScript, so there's no Spotify account setup or
API key. macOS asks once to let your terminal control Spotify (System Settings › Privacy & Security ›
Automation). Checking what's playing never launches Spotify. On narrower windows the block gives way
to the Claude panel. Turn it off with `[spotify] enabled = false` in `config.toml`.

## Updates

Edits to PIXELDECK's own source reload it within a few seconds (a change that doesn't compile is
refused and the running build stays up). If the checkout has a git upstream, it checks every 15
minutes and shows `⬆ N UPDATE · U` in the header when there are new commits; `u` fast-forwards,
reinstalls when dependencies changed, and restarts in place. Set `[update] auto = true` to apply
updates on their own, without pressing `u`.

## Connect sources by hand

Config: `~/.config/pixeldeck/config.toml` (`pixeldeck init` writes a commented template).
Tokens live in the macOS keychain, never in the config.

| Source | Setup |
| --- | --- |
| GitHub | nothing: uses the active `gh auth` account |
| Microsoft 365 mail + calendar | `[outlook] enabled = true`, then `pixeldeck login outlook` (device code) |
| IMAP (Gmail, iCloud, Fastmail) | add an `[[imap]]` block, then `pixeldeck secret imap-<name>` with an app password |
| Apple Mail | `[apple_mail] enabled = true` (macOS asks for Automation access once) |
| Slack | user token `xoxp-` with `search:read`: `pixeldeck secret slack` |
| Linear | personal API key: `pixeldeck secret linear` |

## Develop

```bash
uv run pixeldeck
uv tool install --editable .   # puts `pixeldeck` on PATH
```
