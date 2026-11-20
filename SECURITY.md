# Security Policy

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/Rahhhdayz/Applywise/security/advisories/new)
rather than in a public issue. Include steps to reproduce and the impact you expect. You should get a reply within
a few days.

Never include real credentials, tokens or personal data in a report.

## Safety model

Applywise handles untrusted web content and personal data, so it is built around a few hard rules:

- **External text is data, never instructions.** Job descriptions, web pages and emails are passed to AI only
  inside delimited, labelled blocks. AI calls run in a restricted mode with no tools, a throwaway working
  directory and a JSON output schema.
- **AI output is validated before use.** Pydantic schemas and allow-lists (evidence IDs, a fixed set of bot
  actions, real form options) gate every AI result before trusted code acts on it.
- **No invented candidate claims.** Resume edits are deterministic and evidence-gated. Generated text must cite
  known evidence IDs.
- **Submission stays human.** The form filler never clicks Submit. Practice-mode navigation is limited to Next,
  Continue, and Save and Continue, and a submit-like click raises `SubmitBlocked`.
- **Least privilege for integrations.** Gmail access is read-only (EXAMINE and BODY.PEEK only, enforced in code).
  The Telegram bot serves one configured chat ID and never passes message text to a shell. Email verification
  links are followed only on Workday domains.
- **Secrets stay local.** Credentials live in `.env` (gitignored). Form snapshots redact passwords and security
  codes, and the Workday password is never sent to AI.
- **SSRF guard.** URL intake accepts only http(s), rejects private and loopback addresses after DNS resolution,
  and caps redirects and response size. DNS rebinding is not fully mitigated yet.

## Supported versions

Only the latest commit on `main` is supported.
