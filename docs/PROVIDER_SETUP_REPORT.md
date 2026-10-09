# API provider setup and cost control — implementation and acceptance report

**Date:** 2026-10-09 · **For:** Michael · **Scope:** issue #3 "OWNER AUTHORIZED TASK — OPENAI + ANTHROPIC API PROVIDER SETUP / COST CONTROL" and the "OpenAI API setup" directive. No paid API call was made for any of this.

## 1. What exists on the platform today (verified)

| Piece | State | Evidence |
|---|---|---|
| Executors doing the actual work | `claude_headless` (Claude Code on the Claude **subscription**) and `codex_exec` (Codex CLI on the ChatGPT **subscription**) | 8 verified tasks today; `docs/PROJECT_STATE.md` |
| Separately billed API-key executor | **Not operational.** A key alone does not change the executor: a direct-API worker loop (OpenHands SDK adapter or own loop) is Stage 2 work still to do | `IMPLEMENTATION_PLAN.md` §5 |
| Control-plane model calls (planner, Aria) | Provider-neutral: `openai_api` → `anthropic_api` → `claude_cli` (subscription fallback), first available wins, re-checked on every call | `control/llm.py`, 63 tests |
| Credential handling | Keys read only from a 0600 file in the control plane's config dir (`~/.config/desktop-agent/<provider>.key`) or an env var; never passed to workers (worker environment built from scratch, `*_API_KEY` stripped); never committed; status reported redacted (present / mode / configured) | `config.py`, `sandbox.py`, `test_providers.py` |
| Cost accounting | Priced API usage → USD from an owner-provided price table; subscription or unpriced usage → tokens recorded, cost **UNKNOWN**, never estimated | `store.cost_summary`, panel cost card |
| Budgets | Control-plane caps $5/hour, $20/day, $3 per task default (software gates, not approved spend); hold reason shown when a gate stops dispatch | `scheduler.admission`, `/v0/state.budgets` |
| Free verification | `python -m desktop_agent --check-providers`: key presence, configuration, and the usable OpenAI model ids via `GET /v1/models` (no charge) | `__main__.py` |

## 2. Official-source comparison (read 2026-10-09)

### Billing is separate from subscriptions, on both sides
- **Anthropic:** paid Claude plans and the Console/API are "separate products designed for different purposes" and the API is "billed separately"; setting up Console access is a separate step. **Claude Max and Team plans include monthly credits for the Claude API** (Pro and Enterprise are not mentioned). Source: https://support.claude.com/en/articles/9876003-i-have-a-paid-claude-subscription-pro-max-team-or-enterprise-plans-why-is-claude-api-usage-billed-separately-from-my-paid-claude-plan
- **OpenAI:** the Platform (API) is billed separately from ChatGPT; the help article could not be fetched from this host (HTTP 403), so treat the separation as the documented norm and confirm in the Console. Source: https://help.openai.com/en/articles/9039756-billing-settings-in-chatgpt-vs-platform

### Spend limits that actually stop requests
- **OpenAI:** two controls. *Spend alerts* notify only ("Spend alerts do not enforce a cap"). *Hard spend limit* (organization or project, "Enforce a hard limit" toggle) makes requests fail with `429` once tracked monthly spend reaches it. **"Enforcement is not instantaneous, so recorded spend can slightly exceed the configured amount."** A separate OpenAI-assigned monthly usage-tier limit also applies. Source: https://developers.openai.com/api/docs/guides/spend-limits
- **Anthropic:** every tier has a monthly spend cap (Start **$500**, Build **$1,000**, Scale $200,000); at the cap, requests return `429` with `error_code: enforced_spend_limit_reached` until the first of next month. You can set your own lower limit on the Billing page (requests then return `400 invalid_request_error` "You have reached your specified API usage limits"), and per-workspace spend and rate limits (not on the default workspace). New organizations may start in an Evaluation tier with lower limits. Source: https://platform.claude.com/docs/en/api/rate-limits

### Keys and rotation
- **Anthropic:** keys are created in Console → Settings → API keys; types are *personal* (acts as you), *service account* (for shared/unattended workloads; recommended), and legacy *workspace* keys; a key can be scoped to one workspace; expiration is chosen at creation (3 h to 30 d, custom, or never); Disable is reversible, Delete is permanent; keys are sent as `Authorization: Bearer` or `x-api-key`. Source: https://platform.claude.com/docs/en/manage-claude/authentication
- **OpenAI:** project-scoped keys from the Platform dashboard; spend limits can be set per project (above). The pricing and billing pages were not fetchable from this host (HTTP 403).

### Prices (USD per 1M tokens)
- **Anthropic** (https://platform.claude.com/docs/en/about-claude/pricing): Haiku 5.5 **$0.10 in / $0.50 out** (prompts ≤100k tokens; $0.50/$2.50 above), Sonnet 5.5 **$2 / $10**, Opus 5.5 **$4 / $20**, Fable 5.1 $10 / $50. Cache writes 1.25× (5 min) or 2× (1 h) of input; cache reads 0.1× (0.05× on Sonnet/Opus 5.5, 0.025× on Fable 5.1). Batch API −50%. Web search $10 per 1,000 searches; web fetch free beyond tokens. Tool definitions add a few hundred input tokens per request. These are now in `config/runtime.yaml` `pricing:` for the four Anthropic ids, so Anthropic API calls will be costed exactly from reported usage (cache discounts not modelled: recorded cost is at or above the true cost).
- **OpenAI:** **not available from this host** (pricing page returned HTTP 403 to automated fetch). Model ids this key can use will come from the free `/v1/models` call; prices must be read by Michael in the OpenAI Console or pricing page and entered into `pricing:` before any OpenAI call is costed as known. Until then OpenAI usage would be recorded as UNKNOWN cost, which is why no OpenAI call is made before approval.

### Retries, quotas, caps: what the platform does with them
- A provider limit (`429`, "hit your session limit", `enforced_spend_limit_reached`) pauses the scheduler with the reason and requeues the task; it never retries into a limit (verified 2026-10-08 against the Claude subscription limit).
- Control-plane budgets are preflight gates on dispatch (hourly, daily, per task). They cannot guarantee a strict $0 overrun because provider meters are asynchronous; the panel says so.

## 3. What Michael must click (one at a time)

1. **Install the OpenAI key** (inbox `d-58f742d0`): the transferred file is in your home directory, unreadable to the control plane's account. One command as `michaelos`:
   `sudo install -o caoscare-1 -g caoscare-1 -m 0600 /home/michaelos/.config/desktop-agent/incoming/openai-key-download /home/caoscare-1/.config/desktop-agent/openai.key && shred -u /home/michaelos/.config/desktop-agent/incoming/openai-key-download`
   Then the control plane lists the usable model ids (free) and shows "key present" on the panel.
2. **Set a hard spend limit in the OpenAI Platform** for the project the key belongs to (recommended: $20/month to start, "Enforce a hard limit" on). Platform-side caps are the only ones that stop requests at the provider; ours are preflight gates.
3. **Anthropic** (inbox `d-528e97b0`): if your Claude plan is Max or Team, it already includes monthly API credits; create a *service account* key scoped to a new workspace with its own spend limit, store it at `~/.config/desktop-agent/anthropic.key` (mode 0600) the same way. Otherwise answer "no" and the subscription fallback continues.

## 4. Verified with zero paid API usage
- 63 automated tests, including the OpenAI backend against a fake HTTP layer (request shape, JSON-schema response format, pricing, unknown-cost path, HTTP error surfacing), key loading from a 0600 file, backend selection order, budgets and unknown-usage accounting.
- `--check-providers` run live: backend `claude_cli`, OpenAI key absent, Anthropic not configured.
- No completion request was sent to either API.

## 5. Recommended first pilot (needs your explicit approval)
- **Budget:** $10 total OpenAI pilot, hard limit set in the OpenAI Platform at $20/month as the backstop; control-plane caps unchanged ($5/hour, $20/day).
- **Routing:** Luna-class (cheapest usable OpenAI model) for the planner, Aria and classification; Sol-class for coding workers stays on the subscription executors until the API-key worker loop exists; Astra-class only on evidence.
- **First paid call:** one planner call (a few hundred tokens) to confirm price accounting matches the Console's usage page; then normal operation.
- **Gate:** none of this runs until you answer the approval decision the control plane will file once the key is present and the model ids are known.
