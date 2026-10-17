# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 3.x | yes |
| 2.x | no |
| < 2.0 | no |

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Report privately via GitHub's
[Report a vulnerability](https://github.com/zaktecs-ai/every-site-scraping/security/advisories/new)
flow, or contact the maintainers through the repository's contact channels.

Include:

- what the issue is and where it lives (file and function),
- how to reproduce it (a minimal input is ideal),
- what an attacker could achieve,
- any suggested fix.

You can expect an acknowledgement within a few days. Please give us a reasonable
window to ship a fix before any public disclosure.

## Scope

This tool runs locally and fetches public pages. Areas that matter most:

- **Data integrity** — a path where a crash or a malformed input can silently
  corrupt or lose already-written output rows.
- **Untrusted input handling** — parsing hostile HTML, CSV or sitemap XML
  (entity expansion, path traversal in filenames derived from input, unbounded
  memory growth from a huge or endless response).
- **Credential handling** — anything that could leak a proxy password or a
  user-supplied header into a log, summary, or committed file.
- **Decompression bombs** — a response that expands far beyond
  `--max-response-bytes` in memory.

## What is *not* a vulnerability here

- The tool honouring (or, when explicitly told to, ignoring) `robots.txt`. That
  is intended, documented behaviour and a legal choice for the operator.
- A site's bot protection returning 403/429 — that is reported honestly, not a
  defect.
- The absence of authentication: this is a local CLI, not a service.

## Safe-use reminder

You are responsible for how you use this tool. Respect each site's terms of
service, applicable law (including data-protection law), and rate limits. Do not
use it to overload a service, bypass authentication, or harvest personal data
you have no lawful basis to hold.
