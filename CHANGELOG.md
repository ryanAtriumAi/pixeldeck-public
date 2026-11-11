# Changelog

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
