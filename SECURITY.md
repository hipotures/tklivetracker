# Security policy

## Supported version

Security fixes are applied to the latest maintained branch. Older commits,
tags, and deployment snapshots receive only best-effort support.

## Reporting a vulnerability

Do not publish vulnerability details, credentials, cookies, or proof-of-concept
payloads in a GitHub issue or discussion.

Use GitHub's private **Report a vulnerability** feature when it is enabled for
the repository. If private reporting is unavailable, open a minimal public
issue asking the maintainers to provide a private communication channel. Do not
include vulnerability details in that issue.

A private report should include the affected version, impact, reproduction
steps, and any suggested mitigation. Response time depends on severity and
maintainer availability; the project does not promise a fixed response SLA.

## Web monitor security boundary

The web monitor is an administrative interface intended for localhost or a
trusted network. It has no built-in authentication.

`--read-only` blocks application mutations on the server. It does not hide
dashboard data, encrypt traffic, identify users, or provide access control. A
writable dashboard exposed outside a trusted LAN must be protected with normal
network controls such as firewall rules, a VPN, or an authenticated HTTPS
reverse proxy. The same protections are appropriate for remotely visible
read-only deployments when the displayed information is sensitive.

## Credential handling

Never commit or attach TikTok cookies, browser profiles, Telegram tokens/chat
IDs, proxy credentials, private configuration, runtime databases, or captured request headers.
If a credential is exposed, revoke or rotate it; removing it from the latest
source tree does not invalidate it or remove it from existing Git history.
