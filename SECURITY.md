# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub's private vulnerability reporting](https://github.com/michaelellis003/paperboy/security/advisories/new)
rather than opening a public issue. You should get a response within a
few days.

## Supported versions

The latest release. There are no backports.

## What to know about paperboy's security model

paperboy handles real credentials: an SMTP password, a Zotero API key
with write access to your library, and (for remote deployments) a
bearer token and optional Dropbox tokens.

- **The bearer token is the whole gate.** Anyone holding it can send
  email as you, deliver files to your e-reader, and read and edit your
  Zotero library. There is no reduced-permission mode. Rotate it by
  generating a new token and redeploying.
- **Secrets live in the environment**, loaded from `.env` locally or
  Secret Manager on Cloud Run. They are never baked into the image,
  logged, or included in tool receipts. The `setup_status` tool reports
  configuration state only, by design.
- **Never paste secrets into issues or discussions.** If you
  accidentally commit or post a credential, treat it as compromised
  and rotate it.

## Scope notes for researchers

Things we'd consider vulnerabilities: secrets leaking into receipts,
logs, or error messages; auth bypass on the HTTP transport; a crafted
registry response or PDF causing code execution or resource exhaustion
beyond the documented caps.

Things that are by design: the single-token full-access model
(documented in the README), and the server trusting its configured
owner completely.
