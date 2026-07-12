# Setting up paperboy

This guide walks through getting paperboy running for yourself: the
accounts and credentials each feature needs, where to find them, and how
to run the server. Every step here points at the official page for the
service involved, so you can check the source if a screen looks
different by the time you read this.

Most people only need the first two sections. The Cloud Run section at
the end is for reaching paperboy from claude.ai or a phone, and you can
skip it until you want that.

## What you'll need

paperboy delivers papers to an e-reader and, optionally, files them in
Zotero. What you set up depends on your device:

- **Kindle**: your Send-to-Kindle email address and an email account to
  send from (a Gmail App Password is the common choice).
- **Kobo**: a Dropbox app, because Kobo syncs files from Dropbox.
- **PocketBook**: same as Kindle — its Send-to-PocketBook address and a
  sending account.
- **Zotero** (optional): an API key, if you want the reading queue,
  collections, and duplicate protection.

The interactive wizard (`uv run paperboy setup`) collects these values
and checks each one as you enter it. This guide explains how to obtain
each value; the wizard handles the rest.

## Zotero (optional)

paperboy uses Zotero as its memory: papers land in a Reading Queue
collection, delivery is recorded as a tag, and duplicates are caught
across sessions. Without Zotero you can still search and send papers
one at a time.

You need an **API key**, and if you're editing `.env` by hand rather
than using the wizard, your numeric **userID** as well. Both are on
[zotero.org/settings/keys](https://www.zotero.org/settings/keys).

To create the key, click **Create new private key**, give it a name,
then check **Allow library access** and **Allow write access**.
paperboy needs write access to create the queue and tag items; without
it, queueing fails. Save the key and copy it — Zotero shows the full
key only once.

The wizard looks up your userID from the key automatically, so you can
skip it. If you're filling in `.env` yourself, the top of the same page
reads "Your userID for use in API calls is `<number>`" — that number is
your `ZOTERO_LIBRARY_ID`, and it is not your username.

Reference:
[Zotero Web API basics](https://www.zotero.org/support/dev/web_api/v3/basics).

## Kindle

Two pieces: the address Amazon delivers to, and an account to send from.

### Your Send-to-Kindle address

Go to **Manage Your Content and Devices**
([amazon.com/hz/mycd/digital-console/alldevices](https://www.amazon.com/hz/mycd/digital-console/alldevices)),
open the **Preferences** tab, and find **Personal Document Settings**.
Under **Send to Kindle Email Settings** each device has an address that
looks like `yourname_a1b2c3@kindle.com`. That is where paperboy sends.

### Approve your sending address

On the same Personal Document Settings screen there is an **Approved
Personal Document E-mail List**. Amazon drops any document from an
address that is not on this list, silently, so this step is the one
people most often miss. Select **Add a new approved e-mail address**,
enter the exact address paperboy will send from (the `FROM_EMAIL` you
give the wizard), and save.

Amazon accepts up to 25 attachments and 50 MB per email; paperboy splits
larger batches to stay under that. Reference:
[Approve an email address for Send to Kindle](https://www.amazon.com/gp/help/customer/display.html?nodeId=GX9XLEVV8G4DB28H).

### An account to send from

paperboy sends over SMTP. Any SMTP account works; Gmail is the common
choice, and Gmail needs an **App Password** rather than your normal
password.

An App Password is a 16-character code, and Google only offers it once
you have **2-Step Verification** turned on. Create one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
(you may be asked to sign in again). Use it as the SMTP password. The
Gmail server settings are `smtp.gmail.com`, port 465 for SSL or 587 for
TLS — the wizard defaults to 465.

App Passwords are unavailable if your only second factor is a security
key, if your account is a work or school (Google Workspace) account
where an admin disabled them, or if you have Advanced Protection on.
References:
[Sign in with app passwords](https://support.google.com/accounts/answer/185833),
[Gmail SMTP settings](https://developers.google.com/workspace/gmail/imap/imap-smtp).

## Kobo

Kobo reads files from a linked Dropbox account rather than from email,
so the setup is a Dropbox app instead of a sending address. It takes a
few more steps than Kindle, mostly because Kobo only syncs one fixed
folder.

### Create the Dropbox app

Go to the Dropbox App Console
([dropbox.com/developers/apps/create](https://www.dropbox.com/developers/apps/create))
and make an app:

1. **Choose an API**: Scoped access (the only option).
2. **Choose the type of access**: **Full Dropbox**. This matters. Kobo
   syncs the folder `/Apps/Rakuten Kobo`, which belongs to Kobo's own
   app. An "App folder" app is locked to its own `/Apps/<your app>`
   sandbox and can never write into Kobo's folder, so Full Dropbox is
   the only choice that works here. You cannot change this after the app
   is created, so if you picked App folder, make a new app.
3. **Name it** anything unique.

### Turn on the write permission

Open the app, go to the **Permissions** tab, check
**files.content.write**, and click **Submit**. Do this before the next
step: a token created before a scope is enabled does not carry that
scope, and uploads then fail with a scope error.

### Get a refresh token

The app's **Settings** tab shows an **App key** and **App secret**.
paperboy also needs a long-lived refresh token, which the wizard
obtains for you: it opens a Dropbox authorize page, you click **Allow**,
you paste the code back, and it exchanges the code for the token.

If you set this up by hand instead, the authorize URL must include
`token_access_type=offline` — that is what makes Dropbox return a
refresh token rather than a short-lived one:

```
https://www.dropbox.com/oauth2/authorize?client_id=<APP_KEY>&response_type=code&token_access_type=offline
```

Then exchange the code at `https://api.dropboxapi.com/oauth2/token` with
`grant_type=authorization_code` and your app key and secret, and keep
the `refresh_token` from the response. Reference:
[Dropbox OAuth guide](https://developers.dropbox.com/oauth-guide).

Set `DROPBOX_FOLDER="/Apps/Rakuten Kobo"` so uploads land where Kobo
looks. On the device, link Dropbox under **More → Settings → Accounts →
Dropbox → Get Started**; the Kobo shows a code, you enter it at
[kobo.com/dropbox](https://www.kobo.com/dropbox) and sign in to
authorize. After that, tap **Sync** on the device to pull new files
down. Reference:
[Add books to your Kobo eReader using Dropbox](https://help.kobo.com/hc/en-us/articles/360033830114-Add-books-to-your-eReader-using-Dropbox).

## Running locally

paperboy is a Python project managed with
[uv](https://docs.astral.sh/uv/getting-started/installation/). Install
uv, then clone the repo and work from its directory:

```bash
git clone https://github.com/michaelellis003/paperboy
cd paperboy
```

With the credentials above, the wizard writes a `.env` file and you're
ready:

```bash
uv sync
uv run paperboy setup
```

If you enabled the Dropbox write permission *after* the wizard already
ran, re-run `uv run paperboy setup` so it mints a token that carries the
new scope; otherwise uploads fail with a scope error.

The wizard asks which device you have and only walks through the
credentials that device needs, checking each one as you go: it logs in
to your SMTP server, confirms your Zotero key has write access, and
completes the Dropbox exchange. Then register paperboy with Claude Code:

```bash
claude mcp add paperboy -- uv run --directory /path/to/paperboy paperboy
```

Replace `/path/to/paperboy` with the absolute path to your clone (run
`pwd` in the repo to get it). `--directory` matters: paperboy reads
`.env` from that directory. Now
ask Claude to find a paper and send it to your device.

If you'd rather fill in credentials by hand, copy `.env.example` to
`.env` and edit it — every variable is documented there. If paperboy is
added but only half-configured, ask Claude to "check my paperboy setup"
and the `setup_status` tool reports what's still missing.

## Reaching paperboy from claude.ai or your phone

Running locally covers Claude Code and Claude Desktop. To use paperboy
from claude.ai or the Claude mobile app, deploy it to Google Cloud Run —
your own instance, your own secrets, normally free. That is covered in
[docs/deploy.md](deploy.md), including the one-time Google sign-in setup
that lets the mobile and web apps connect.
