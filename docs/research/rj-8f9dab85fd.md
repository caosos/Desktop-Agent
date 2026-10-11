# Research: What do the Playwright and Claude Code CLI documentation say about running headless Chrome and about the --allowedTools and --disallowedTools flags?
job: rj-8f9dab85fd  ·  project: desktop_agent  ·  sources: web + local repository  ·  2026-10-11

## Question
What do the Playwright and Claude Code CLI documentation say about running headless Chrome and about the `--allowedTools` and `--disallowedTools` flags, and how does this repository use them?

## Findings
1. Playwright's `headless` launch option runs the browser in headless mode and defaults to true. [1]
2. Playwright ships a regular Chromium build for headed runs and a separate headless shell; `--only-shell` at install skips the full browser. [2]
3. The `chromium` channel opts into the new headless mode, which the docs describe as the real Chrome browser. [2][1]
4. Branded channels such as `chrome` and `msedge` are not installed by default and use the new headless mode, which may behave differently from the default headless shell. [2]
5. This repository's browser tests launch system Chrome headless through Playwright with `channel="chrome", headless=True`. [3]
6. In the Claude Code CLI, `--allowedTools` lists tools that run without a permission prompt; it does not restrict which tools exist (that is `--tools`). [4]
7. `--disallowedTools` holds deny rules: a bare tool name removes the tool from Claude's context, while a scoped rule such as `Bash(rm *)` leaves the tool available and denies only matching calls. [5]
8. `-p`/`--print` is non-interactive mode, and `--max-turns` and `--max-budget-usd` apply only in print mode. [6][7][8]
9. This repository's headless worker adapter passes `--allowedTools` from the task contract and a fixed `--disallowedTools` list, withholding WebFetch/WebSearch unless the contract allowlists them. [9][10]

## Analysis
Playwright documents two distinct headless implementations: the default headless shell and the new headless mode reached through the `chromium` or branded `chrome` channels [2]. The repository's tests use the `chrome` channel [3], so by the documentation they run the new headless mode rather than the shell [2].

For Claude Code, allow and deny are separate mechanisms. `--allowedTools` only removes prompts [4]; `--disallowedTools` can remove a tool entirely or deny scoped calls [5]. The adapter combines them: an allowlist built from the contract, and a deny list that always blocks Task, SendMessage, push, curl and similar [9][10]. Because a bare name removes a tool while a scoped rule only denies matching calls, the web tools are removed outright when not allowlisted, and the Bash rules deny only matching commands [5][10].

## Limits
Web retrieval was used. Pages fetched: playwright.dev/python/docs/browsers, playwright.dev/python/docs/api/class-browsertype and code.claude.com/docs/en/cli-reference. No searches were run with the search tool. The fetch tool returns a model-summarised view, so Playwright quotes are short fragments of that view and not checked against raw HTML. Not covered: Claude Code permission rule syntax pages, the headless/SDK page, and Playwright's Node or Java docs. The Playwright pages were read in their Python flavour only.

## Sources
[1] https://playwright.dev/python/docs/api/class-browsertype — "Whether to run browser in headless mode." (accessed 2026-10-11)
[2] https://playwright.dev/python/docs/browsers — "You can opt into the new headless mode by using 'chromium' channel." (accessed 2026-10-11)
[3] tests/test_panel_browser.py:62 — "browser = p.chromium.launch(channel="chrome", headless=True)"
[4] https://code.claude.com/docs/en/cli-reference — "Tools that execute without prompting for permission" (accessed 2026-10-11)
[5] https://code.claude.com/docs/en/cli-reference — "A bare tool name removes the matching tools from Claude's context" (accessed 2026-10-11)
[6] https://code.claude.com/docs/en/cli-reference — "Print response without interactive mode" (accessed 2026-10-11)
[7] https://code.claude.com/docs/en/cli-reference — "Limit the number of agentic turns (print mode only)." (accessed 2026-10-11)
[8] https://code.claude.com/docs/en/cli-reference — "Stop the run once estimated spend on API calls reaches this amount (print mode only)." (accessed 2026-10-11)
[9] desktop_agent/control/adapters/claude_headless.py:58 — "argv += ["--allowedTools", *contract.allowed_tools]"
[10] desktop_agent/control/adapters/claude_headless.py:62 — "argv += ["--disallowedTools", *[t for t in ("WebFetch", "WebSearch") if t not in web_ok], "Task", "SendMessage","

## Review
1. OK — option text and default match [1].
2. OK — matches [2]; `--only-shell` is stated there.
3. OK — [2] states the opt-in; "real Chrome browser" is from the page's quoted Chrome docs; [1] repeats the channel hint.
4. OK — matches [2] on channels, non-default install and behavioural difference.
5. OK — line 62 of the test file contains the call [3].
6. OK — [4] states both parts.
7. OK — matches [5].
8. OK — `-p` and the two print-only flags match [6][7][8].
9. OK — lines 58 and 62 confirm the allowlist and deny list; the conditional web removal is in lines 59-62 [9][10].
