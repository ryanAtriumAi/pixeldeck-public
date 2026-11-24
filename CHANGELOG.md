# Changelog

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
