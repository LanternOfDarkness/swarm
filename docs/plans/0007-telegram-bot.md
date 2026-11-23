# 0007 — Telegram bot: notifications, answers, goals, status

Spec: `docs/SPEC.md` §5 ("Phone"). Builds on the earlier stages (merged), including
archived projects and asks without a project. Baseline: `1f01ded`. Written for an
implementation agent: every decision is made here. If something is not covered, choose the
simplest option and list it under DEVIATIONS. Do not add features that are not listed.

## Goal

The operator follows and steers the swarm from the phone. A bot sends new asks, blocks,
merges, finished plans and daemon on/off. The operator answers an ask by replying to its
message, adds goals, and reads status, asks, runs and the brief.

## Global decisions

- Stdlib only: `urllib.request` + `json`, long polling (`getUpdates`); no webhook, no open
  port. One process: `swarm telegram`, a long-running command like `swarm daemon`.
- **Config:** `$SWARM_HOME/telegram.toml` with `bot_token = "..."` and
  `chat_id = "..."` (string). The operator creates it (mode 600). It is never
  written by swarm, never copied into the repo, and the token is **never printed or
  logged**: every error text that could contain the request URL goes through
  `_mask(text)`, which replaces the token with `<token>`.
- **Only one chat:** updates from any other chat id are ignored without a reply.
- **Plain text only:** `sendMessage` without `parse_mode` (no Markdown/HTML injection from
  board text). Messages longer than 4000 characters are cut, ending with
  `… (cut — /status in the terminal for more)`.
- **What the phone may change:** answer non-destructive asks, add goals. Nothing else: no
  project settings, no daemon control, no plan moves. Destructive asks
  (`asks.destructive = 1`) are refused: `Destructive ask: answer it in the terminal.`
- **State file:** `$SWARM_HOME/telegram.json`, written atomically (`.tmp` + `os.replace`):
  `offset` (int, next update id), `last_ask_id` (int|null), `last_event_id` (int|null),
  `daemon_on` (bool|null), `ask_messages` (object: Telegram message id as a string → ask
  id; keep the newest 500).
- **Lock:** `telegram`, holder `pid:<pid>`, same rule as the daemon lock (dead holder →
  force-take; alive → exit 1 `telegram bot already running (pid N)`).
- Tests never call the real Telegram API: the loop takes an `api` object, tests pass a fake.
- Verify command: `python3 -m unittest discover -s tests -v`

## Task 1 — API client and config

Files: `swarm/telegram.py` (new), `tests/test_telegram.py` (new).

- `load_config(home) -> (token, chat_id)` — reads `telegram.toml` with `tomllib`; missing
  file or empty value → `ValueError("telegram.toml needs bot_token and chat_id: <path>")`.
- `class TelegramApi(token)`:
  - `call(method, params) -> object` — POST JSON to
    `https://api.telegram.org/bot<token>/<method>`, timeout `params.get("timeout", 0) + 15`
    s; returns `result` when `ok` is true; otherwise raises `TelegramError(code, description)`
    (`code` = `error_code` or the HTTP status). `URLError`/`OSError`/timeouts →
    `TelegramError(0, _mask(str(exc)))`.
  - `get_updates(offset, timeout=25) -> list[dict]` — `allowed_updates=["message"]`.
  - `send(chat_id, text, reply_to=None) -> int` — returns the new `message_id`; applies
    the 4000-char cut.
- `_mask(text)` uses the token of the module-level `_TOKEN` set by `TelegramApi.__init__`.

Tests: `_mask`; `load_config` good/missing/empty; `send` cuts long text (patch
`urllib.request.urlopen` with a fake returning `{"ok": true, "result": {"message_id": 5}}`);
an `ok: false` reply raises `TelegramError` with the code.

## Task 2 — notifications

Files: `swarm/telegram.py`, `tests/test_telegram.py`.

`notify(conn, cfg, api, chat_id, state) -> int` (number of messages sent). In this order:

1. **First run** (`last_ask_id` is null): set `last_ask_id` = max ask id (0 if none),
   `last_event_id` = max event id (0), `daemon_on` = current; send one message
   `Swarm bot is on. Open asks: N. /help for commands.` and return.
2. **New asks:** `OPEN` asks with `id > last_ask_id`, by id, skipping asks whose project
   is archived (asks with no project are kept, as in `swarm status`). Text:
   ```
   ASK #<id> · <plan label or project or "swarm">
   <question>

   Reply to this message to answer.          (destructive: Destructive: answer it in the terminal with swarm answer <id> "...")
   ```
   Store `ask_messages[str(message_id)] = id`. Advance `last_ask_id` after each send.
3. **Plan moves:** events with `id > last_event_id` and `kind = 'move'`, from projects that
   are not archived, whose text contains
   `→ BLOCKED`, `→ MERGED` or `→ DONE`; send `BLOCKED: <text>` / `MERGED: <text>` /
   `DONE: <text>` (the event text, e.g. `demo/0042 VERIFYING → MERGED`). Advance
   `last_event_id` to the max event id seen (all kinds), even when nothing is sent.
4. **Daemon:** `status._daemon_state(cfg)["on"]`; when it differs from `daemon_on`: send
   `Daemon is OFF (last tick <age_s>s ago).` or `Daemon is on.`; store the new value.

A `TelegramError` during a send stops this `notify` call (the state keeps what was already
sent, so the rest is retried next loop).

Tests (fake api recording sends, temp board): first run sends one hello and nothing else;
an ask and a move of an archived project send nothing; an ask with no project is sent;
a new ask → one message, mapped in `ask_messages`; a destructive ask → the terminal text;
moves to BLOCKED/MERGED/DONE → three messages, a move to REVIEW → none; daemon on→off→on
→ two messages; a send failure mid-way → the next call sends only the rest.

## Task 3 — commands and answers

Files: `swarm/telegram.py`, `tests/test_telegram.py`.

`handle(conn, cfg, api, chat_id, state, update) -> None`. Ignore updates without `message`,
or whose `message.chat.id` (as a string) differs from `chat_id`. Text is `message.text`
stripped; non-text messages → `Text messages only.`. Each reply is sent with
`reply_to = message.message_id`.

- **Reply to an ask message** (`reply_to_message.message_id` is in `ask_messages`):
  answer that ask with the full text (rules below).
- `/answer <id> <text>` → answer ask `<id>`. Bad form → `Usage: /answer ID text`.
- **Answer rules:** ask missing → `No ask #<id>.`; not `OPEN` → `Ask #<id> is already
  <state>.`; destructive → `Destructive ask: answer it in the terminal.`; else
  `board.answer_ask(conn, id, text)` and reply `Answered #<id>. The daemon picks it up on its
  next pass.`
- `/goal <project> | <title> | <done when> [| <est minutes>]` → `board.add_goal(conn,
  project, title, done_when, est_minutes=int or None)`; reply `Goal #<id> queued for
  <project>: <title>.` Fewer than 3 parts, an empty part, or a non-integer estimate →
  `Usage: /goal project | title | done when [| est minutes]`. Unknown project → the
  `board` error text.
- `/status` → `status.format_status(status.build_status(conn, cfg))`.
- `/brief` → `status.format_brief(conn, cfg)`.
- `/asks` → open asks, one line each `#<id> <plan label or project>: <first line, 100
  chars>`; none → `No open asks.`
- `/runs` → running runs, one line each `#<id> <kind>/<purpose> <plan label> <age>m
  $<cost>`; none → `No workers running.`
- `/start` or `/help` → the command list: the six commands above, one line each, plus
  `Reply to an ASK message to answer it.`
- Commands may carry a bot suffix (`/status@name`): strip `@…` from the first word.
- Anything else → `Unknown command. /help`.

Tests: each command's reply on a temp board; a reply to a mapped ask answers it (ask
`ANSWERED`, answer text stored); a destructive ask is refused and stays `OPEN`; a stranger's
chat id gets no reply and changes nothing; `/goal` happy path and each usage error.

## Task 4 — CLI loop, launcher, docs

Files: `swarm/telegram.py`, `swarm/cli.py`, `scripts/start.sh`, `tests/test_cli.py`,
`README.md`, `docs/SPEC.md`.

- `telegram.run(conn, cfg, api, chat_id, state_path, once=False, stop=lambda: False)`:
  loop: `notify`; `get_updates(offset, timeout=25 if not once else 0)`; `handle` each,
  setting `offset = update_id + 1` **before** handling (a crashing update is not repeated);
  save state; repeat until `once` or `stop()`. `TelegramError` with code 409 → raise
  (another bot instance polls); 401 → raise; other `TelegramError` → print the masked text to
  stderr, wait 10 s (in 0.5 s steps checking `stop()`), continue. Any other exception in
  `handle` → reply `Error: <ExcType>: <msg>` if possible, print the traceback, continue.
- CLI `swarm telegram [--once]`: `load_config` (error → exit 1 with its message); take the
  `telegram` lock; SIGINT/SIGTERM set a stop flag (as the daemon does); 409 → exit 1
  `another process is polling this bot`; 401 → exit 1 `bot token rejected`; always drop the
  lock. Generalise the daemon's lock helper to `_take_pid_lock(conn, name, holder)` and use
  it for both.
- `scripts/start.sh`: when `$HOME/.local/state/swarm/telegram.toml` exists (respect
  `SWARM_HOME` if set), also create a tmux window `telegram` running `swarm telegram`.
- README: "Telegram" section: the config file (two keys, mode 600, never in the repo), the
  commands, reply-to-answer, destructive asks stay in the terminal.
- SPEC §5: replace the **Phone** bullet (`docs/SPEC.md:96`) with `Phone: Telegram bot — notifies
  asks, blocks, merges, done, daemon on/off; answers non-destructive asks; adds goals;
  read-only status. Only the operator's chat id is accepted.`

Tests: `swarm telegram --once` on a temp board with a fake api (patch
`telegram.TelegramApi`) sends the hello and exits 0; a second instance is refused while the
lock holder pid is alive; missing config → exit 1 with the message; a 409 → exit 1.

## Done when

- `python3 -m unittest discover -s tests -v` passes.
- `grep -rn "bot_token\|api.telegram.org" tests/ swarm/` shows the token value nowhere (only
  the key name and the URL template).
- The worker does **not** run `swarm telegram` against the real config or network. The
  operator does the live check: `swarm telegram`, then `/status` and `/help` on the phone.

## Out of scope

Webhooks; buttons/inline keyboards; plan moves, daemon control or project settings from the
phone; several chats; sending files or logs.

## Revision log

| Date | Change |
|---|---|
| 2026-11-20 | Review at `1f01ded`: archived projects' asks and moves are not sent (project-less asks are); SPEC Phone bullet without a plan number; neutral `demo` example; config text no longer says the operator's file exists. |
