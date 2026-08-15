# Pi Chrome

Use this skill when the terminal user wants Pi to inspect or control open Chrome tabs/pages through Chrome DevTools MCP. `/chrome` is the durable browser-control directive.

Canonical directives:

```bash
pi /chrome list
pi /chrome start --auto-connect --no-usage-statistics
pi /chrome open https://example.com
pi /chrome select 1
pi /chrome navigate 1 https://example.com/settings
pi /chrome gather docs
pi /chrome read docs
pi /chrome read docs --ocr
pi /chrome summarize docs
pi /chrome decide docs --prompt "which tabs can I close?"
pi /chrome close-category docs
pi /chrome close-category docs --yes
pi /chrome eval 1 "() => document.title"
pi /chrome snapshot 1 /tmp/page.aria.json
pi /chrome screenshot 1 /tmp/page.png --fullPage
pi /chrome close 1
```

Execution semantics:

1. Bare `/chrome` aliases to `/chrome list`.
2. The directive runs the durable `chrome_control` workflow and records a `chrome_control_result.v1` artifact for every call.
3. Chrome operations are executed through the `chrome_devtools` local tool, which shells out to the official `chrome-devtools` CLI from `chrome-devtools-mcp`.
4. The default command is:

```bash
npx -y chrome-devtools-mcp@latest --auto-connect --no-usage-statistics
```

5. By default the tool talks to the MCP stdio server directly. The legacy `chrome-devtools` helper CLI can be selected with `LOCAL_AGENT_CHROME_DEVTOOLS_TRANSPORT=cli`.
6. To control already-open Chrome windows, Chrome 144+ remote debugging must be enabled from `chrome://inspect/#remote-debugging`, and the user must allow the Chrome control prompt.
7. Page ids come from `/chrome list`. Page-specific commands accept either a leading page id (`/chrome navigate 1 https://...`) or `--page <id>`.
8. `/chrome gather <category>` matches open pages by title/URL and records the current tab set.
9. `/chrome read <category>` captures text from DevTools accessibility snapshots and writes a durable `chrome_tab_text.v1` text artifact. Add `--ocr` to also screenshot matching pages and run the local OCR role over the images.
10. `/chrome summarize <category>` gathers matching pages plus captured text and asks the base model to group, summarize, and recommend close candidates.
11. `/chrome decide <category> --prompt "..."` feeds captured tab text and OCR text, when requested, to the base model for a concrete recommendation. The model may recommend tab ids; it never closes tabs itself.
12. `/chrome close-category <category>` is a dry run by default; add `--yes` after reviewing the matched tabs. Closing all tabs requires both `--all` and `--yes`.
13. Supported actions are `list`, `start`, `stop`, `status`, `open`, `select`, `navigate`, `reload`, `back`, `forward`, `close`, `gather`, `read`, `summarize`, `decide`, `close-category`, `eval`, `snapshot`, `screenshot`, `console`, and `network`.

Permission gates:

- `pi.permission_gate.chrome_devtools.v1` — `/chrome` routes through the `chrome` workspace policy, which only allows the `chrome_devtools` tool.
- JavaScript execution is only available through explicit `/chrome eval ...` directives.
- Browser writes are explicit actions: `open`, `select`, `navigate`, `reload`, `back`, `forward`, `close`, `eval`, and screenshots/snapshots that write a file path.
- Multi-tab category closes require `--yes`; without it, Pi records matched tabs without closing them.
- `read`, `summarize`, and `decide` are read-only browser operations unless `--ocr` is used, which writes screenshots under the configured spool directory before importing them as artifacts.

Design note: Chrome DevTools MCP models tabs as pages. `/chrome` therefore manipulates Chrome tabs/pages directly; full OS-level window arrangement remains outside this directive.
