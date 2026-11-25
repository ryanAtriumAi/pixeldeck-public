# Changelog

## 0.4.5 (2026-11-24)

- **Calmer CHRISTMAS header:** the candy-cane logo no longer scrolls its stripes (it read as flashing); the
  stripes are broad, softened and still, and the gold lights in it fade in and out slowly. Snowflakes are
  blended into the background (each one more or less see-through) and fall a little slower.

## 0.4.4 (2026-11-24)

- **CHRISTMAS theme:** pine, berry red and gold. Snow falls through the whole header (past the tree, the
  logo, the counters and the clock), the logo is a candy cane with twinkling gold lights, a Christmas tree
  replaces the invader (also on the title screen), and the rule lines become strings of fairy lights.
- The header sprite now follows a live theme switch (it kept its old colours until a restart).

## 0.4.3 (2026-11-24)

- **GitHub page REVIEWED** counts other people's PRs you reviewed, commented on or merged, not only formal
  reviews (GitHub's `reviewed-by:` search), which undercounted badly when reviewing ends in a comment or a merge.
  Each row says what you did; the panel splits the total and charts it per week.

## 0.4.2 (2026-11-24)

- **Live themes:** moving through the theme list (profile screen or Settings → Profile) restyles the
  whole app as you go, with no restart. Enter or SAVE keeps one; `esc`, or leaving the list without
  Enter, puts the saved one back. A theme set by hand in `config.toml` applies when the file is saved.
- **Pages open with numbers:** a section page shows its last numbers at once and updates them in place
  when its load finishes; the header says how old they are. Within a run the rows and side panel come
  back too; across restarts only the card numbers and hints are kept (`pages-cache.json`). RELEASES
  draws from what its tile already loaded, and the Linear page runs its three queries side by side.

## 0.4.1 (2026-11-24)

- **Profile:** the first start without one asks for your name, an optional email and a theme. Your name
  shows beside P1 in the header. It only adds `[profile]` to `config.toml`; an existing setup is left
  exactly as it was. Edit it in Settings → Profile.
- **Themes:** ARCADE (the original), PHOSPHOR, AMBER, OCEAN, FROST, MONO and DAYLIGHT (light). Pick one
  in the profile screen or Settings → Profile (`[ui] theme`); PIXELDECK restarts to apply it.

## 0.4.0 (2026-11-24)

- **Popups:** `Enter` on an email, ticket, PR, CI run, release or Slack row opens it in a popup over
  what you were looking at. `esc` or a click outside closes it, `n` / `p` step through the list, `f`
  fills the screen. `[ui] popups = false` (Settings → System) brings back the full page.
- **A page for every section:** pressing a tile's number twice opens its page: cards with that
  section's numbers, the list behind the card you pick, and a panel of charts. `/` filters the list.
  - **Linear (`88`):** outstanding work ranked urgent to low, new assignments, what you were assigned,
    created and closed in the last 7 / 30 / 90 days (`t`), your backlog and your team's backlog count.
  - **Mail (`55`):** unread, new, today, important and with-files slices, a preview of the highlighted
    email, who writes to you most.
  - **GitHub (`11` `22` `33`):** reviews waiting on you by how long they've waited, your open PRs with
    the ones that need you first, the inbox by who it's for (`e` marks one read), and the PRs you
    opened, merged and reviewed in the period.
  - **Releases (`44`):** what each environment runs and whether it's up, where the latest release is
    live, the commits production is behind, deploys per week.
  - **CI (`99`):** failing and running workflows, recent runs by day, pass rate overall, per repo and
    per workflow, median durations.
  - Slack (`66`), Agenda (`77`) and Agent (`00`) keep the pages they had.
- **Delete mail:** `d` on the Mail page or an open email moves it to Deleted Items (recoverable in
  Outlook); `z` undoes the last one. `space` selects several emails for `d` or `u` (read / unread).
- **Read in Outlook too:** opening a Microsoft 365 email now marks it read in the mailbox, not only in
  PIXELDECK (`[mail] mark_read`). Deleting and marking read need `Mail.ReadWrite`, which Microsoft asks
  you to approve once. IMAP and Apple Mail stay read-only.

## 0.3.4 (2026-11-18)

- **Offline doesn't crash it:** a network error in a background job (Wi-Fi drop, sleep, DNS) shows one
  `⚠ NETWORK` toast and the next sync retries. Before, it could quit the whole app. Other errors still do.
- **Slack page:** the background fill of unread state stops when it fails instead of erroring out, and
  it no longer loops forever on hidden app DMs (it kept calling Slack while the page was open).

## 0.3.3 (2026-11-12)

- **Linear "whole team"** works without team keys: it lists the teams you're on. Before, it stopped
  with a setup prompt whose Enter opened the API key form, as if the key were the problem.
- A tile whose setup problem is a setting, not a missing key, says `⏎ settings` and opens that
  section of Settings.

## 0.3.2 (2026-11-12)

- **Mouse keeps working:** PIXELDECK turns mouse reporting back on every 15 seconds and whenever the
  terminal window regains focus. Before, anything that switched it off mid-run left clicks dead until a restart.
- Terminal escape codes and control characters are stripped from tile rows and agent answers, so a stray
  code in an email, ticket or tool output can't reset the terminal.

## 0.3.1 (2026-11-11)

- **Linear NEW tag:** tickets assigned to you in the last 14 days (`[linear] new_days`) that you haven't
  opened come first in LINEAR QUESTS with the NEW tag, even if the filter hides their state (new tickets
  often land in Backlog). Opening one clears it; a ticket assigned while PIXELDECK runs pops a toast.

## 0.3.0 (2026-11-10)

- **Now playing page (`n`):** big pixel-art cover, a record that slides out and spins, dancing sprites
  and an 8-bit equalizer. Keys for play/pause, skip, ±10 seconds, volume, shuffle and repeat.
- **Page footers:** every page shows only its own keys; the dashboard's keys no longer show (or fire)
  on top of it. `esc` goes back from every page.
- **Ask the agent:** `ctrl+t` in Ask switches between the local model and your agent (Hermes); each keeps
  its own chat, and `?` on the AGENT page asks the agent. `[ask] default` picks which opens first.
- The model picker (`l`) loads models with 64K context (`[assist] context`), and reloads one loaded with less.

## 0.2.0 (2026-11-10)

- **Spotify:** now playing in the SYSTEM strip (pixel-art cover, progress, volume), with `p` play/pause,
  `<` `>` skip and `[` `]` volume. Uses the Spotify app on your Mac; no account setup.
- **Review queue:** shows every PR that asks for your review or is assigned to you, in any repo.
- **Agent:** tile 0 and the AGENT page (`00`) collect briefs, suggestions and tasks from a local agent
  through `pixeldeck mcp`. Suggestions open the usual confirm screen; nothing is sent on its own.
- **Ask (`?`):** chat with a local model that searches every connected source.
- **Linear:** `/` searches every ticket as you type; `f` filters by status and priority.
- **Agenda:** 5-day calendar, event details, schedule events.
- **Releases:** latest release on top, deploys in progress pulse, in-app release notes.
- **Mail:** open attachments on their own (images, PDFs, media, Office via Quick Look).
- Local-model reply suggestions in your own tone, LLM status and model picker (`l`).
- Mac-style word and line editing in every text box; quit needs `q q`.

## 0.1.0

First version: GitHub, mail, calendar, Slack, Linear, CI and system tiles; setup wizard; self-update.
