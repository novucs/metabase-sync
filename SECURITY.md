# Security policy

## Supported versions

Only the latest minor release receives security fixes. We're pre-1.0; expect
this policy to tighten when we cut 1.0.

## Reporting a vulnerability

**Don't open a public GitHub issue for a security vulnerability.**

Use GitHub's [private vulnerability reporting](https://github.com/novucs/metabase-sync/security/advisories/new) for this repository (Security tab → Advisories → "Report a vulnerability"). Please include:

- A short description of the issue.
- The repository commit or PyPI release it affects.
- Proof of concept (a `.plan.json` snippet, an HTTP trace, or a minimal script).
- Your preferred public credit name, if you'd like attribution.

We aim to acknowledge within 72 hours and ship a fix within 14 days for high-severity issues.

## Threat model

The tool handles:

- An admin-level Metabase API key (`METABASE_API_KEY`).
- Card SQL bodies, which may contain credentials a user typed inline (poor
  hygiene but it happens).
- Database connection metadata (names + engines, never credentials).

### Things in scope

- Code execution via a malicious state tree (we accept user-controlled YAML
  and SQL; pyyaml.safe_load is used, but any path that calls `eval`, `exec`,
  `compile`, or untrusted deserialisation is in scope).
- Privilege escalation via a forged HTTP response.
- Credential exposure in `.plan.json` / `.last-apply.json` beyond what the
  user could see by reading the source `.sql` themselves.
- API request smuggling that lets an attacker hit unintended Metabase
  endpoints via a crafted card path.

### Things out of scope

- An attacker who already controls the user's shell environment (so already
  has the API key).
- Compromise of upstream dependencies (pydantic, httpx, etc.) — please report
  to them, but we'll cut a release once they ship a fix.
- Misuse of `--force` / `--allow-missing-recipients` to overwrite remote
  state. These flags are explicit user opt-ins.
- SOC-2-style organisational controls around how you manage the API key
  (that's between you and your secrets manager).
