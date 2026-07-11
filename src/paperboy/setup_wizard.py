"""Interactive, device-first setup wizard.

Run as ``paperboy setup``. Asks which e-reader the user owns and walks
through only the credentials that device needs, validating each one
live (SMTP login, Zotero key, Dropbox OAuth exchange) before writing
``.env``. Secrets are collected in the terminal — never through chat,
per MCP elicitation security guidance.
"""

import argparse
import getpass
import os
import re
import secrets
import smtplib
import webbrowser

from .net import client

_ZOTERO_KEYS_URL = "https://www.zotero.org/settings/keys/new"
_ZOTERO_CURRENT_KEY = "https://api.zotero.org/keys/current"
_AMAZON_PREFS_URL = "https://www.amazon.com/hz/mycd/digital-console/preferences"
_GMAIL_APP_PASSWORDS = "https://myaccount.google.com/apppasswords"
_DROPBOX_APPS_URL = "https://www.dropbox.com/developers/apps/create"
_DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
_KOBO_FOLDER = "/Apps/Rakuten Kobo"


# --- .env writing ---------------------------------------------------------


def quote(value: str) -> str:
    """Quote a value for .env if it contains whitespace or quotes."""
    if re.search(r"""[\s"']""", value):
        return '"' + value.replace('"', '\\"') + '"'
    return value


def update_env(values: dict[str, str], path: str = ".env") -> None:
    """Set keys in the .env file, preserving unrelated lines/comments."""
    try:
        with open(path) as handle:
            lines = handle.read().splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(values)
    updated = []
    for line in lines:
        match = re.match(r"^([A-Z_]+)=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            updated.append(f"{key}={quote(remaining.pop(key))}")
        else:
            updated.append(line)
    for key, value in remaining.items():
        updated.append(f"{key}={quote(value)}")
    with open(path, "w") as handle:
        handle.write("\n".join(updated) + "\n")
    # The file holds credentials — restrict it to the owner.
    os.chmod(path, 0o600)


# --- validators -----------------------------------------------------------


def validate_smtp(host: str, port: int, user: str, password: str) -> str | None:
    """Try an SMTP login; return an error message or None on success."""
    try:
        if port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            smtp = smtplib.SMTP(host, port, timeout=15)
            smtp.starttls()
        smtp.login(user, password)
        smtp.quit()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def validate_zotero_key(key: str) -> tuple[str | None, str | None]:
    """Check a Zotero key; return (userID, None) or (None, error)."""
    response = client.get(_ZOTERO_CURRENT_KEY, headers={"Zotero-API-Key": key})
    if response.status_code != 200:
        return None, f"Zotero rejected the key (HTTP {response.status_code})"
    info = response.json()
    access = info.get("access", {}).get("user", {})
    if not (access.get("library") and access.get("write")):
        return None, (
            "Key is valid but lacks library and/or write access — "
            "recreate it with both boxes checked."
        )
    return str(info["userID"]), None


def dropbox_authorize_url(app_key: str) -> str:
    """Authorize URL that yields an offline (refresh-token) grant."""
    return (
        "https://www.dropbox.com/oauth2/authorize"
        f"?client_id={app_key}&response_type=code&token_access_type=offline"
    )


def exchange_dropbox_code(
    app_key: str, app_secret: str, code: str
) -> tuple[str | None, str | None]:
    """Exchange an OAuth code; return (refresh_token, None) or (None, error)."""
    response = client.post(
        _DROPBOX_TOKEN_URL,
        data={
            "code": code,
            "grant_type": "authorization_code",
            "client_id": app_key,
            "client_secret": app_secret,
        },
    )
    if response.status_code != 200:
        return None, f"Dropbox rejected the code: {response.text[:200]}"
    return response.json()["refresh_token"], None


# --- prompting ------------------------------------------------------------


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_secret(prompt: str) -> str:
    return getpass.getpass(f"{prompt}: ").strip()


def _confirm(prompt: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer.startswith("y")


def _offer_browser(url: str) -> None:
    print(f"  -> {url}")
    if _confirm("Open in your browser?"):
        webbrowser.open(url)


# --- device flows ---------------------------------------------------------


def _setup_smtp(default_user: str) -> dict[str, str]:
    print("\nSMTP is used to email documents to the device.")
    print("For Gmail, create an App Password (not your real password):")
    _offer_browser(_GMAIL_APP_PASSWORDS)
    while True:
        values = {
            "SMTP_HOST": _ask("SMTP host", "smtp.gmail.com"),
            "SMTP_PORT": _ask("SMTP port", "465"),
            "SMTP_USER": _ask("SMTP username", default_user),
        }
        values["SMTP_PASSWORD"] = _ask_secret("SMTP password")
        print("Validating SMTP login...")
        error = validate_smtp(
            values["SMTP_HOST"],
            int(values["SMTP_PORT"]),
            values["SMTP_USER"],
            values["SMTP_PASSWORD"],
        )
        if error is None:
            print("SMTP login OK.")
            return values
        print(f"Login failed: {error}\nLet's try again.")


def _setup_kindle() -> dict[str, str]:
    print("\n[Kindle] Two things from Amazon's Preferences page")
    print("(scroll to 'Personal Document Settings'):")
    print("  1. Your Send-to-Kindle address (…@kindle.com)")
    print("  2. Add your sending email to the Approved E-mail List")
    _offer_browser(_AMAZON_PREFS_URL)
    device = _ask("Send-to-Kindle address")
    sender = _ask("Your sending email (must be on the approved list)")
    values = {
        "DELIVERY_METHOD": "email",
        "DEVICE_EMAIL": device,
        "FROM_EMAIL": sender,
    }
    values.update(_setup_smtp(default_user=sender))
    return values


def _setup_pocketbook() -> dict[str, str]:
    print("\n[PocketBook] Find your Send-to-PocketBook address")
    print("(set up in the device's or cloud's Send-to-PocketBook settings),")
    print("and make sure your sending email is authorized there.")
    device = _ask("Send-to-PocketBook address")
    sender = _ask("Your sending email (must be authorized)")
    values = {
        "DELIVERY_METHOD": "email",
        "DEVICE_EMAIL": device,
        "FROM_EMAIL": sender,
    }
    values.update(_setup_smtp(default_user=sender))
    return values


def _setup_dropbox(folder: str, full_access: bool) -> dict[str, str]:
    print("\nCreate a Dropbox app (one-time):")
    _offer_browser(_DROPBOX_APPS_URL)
    access = "Full Dropbox" if full_access else "App folder"
    print(f"  1. Scoped access -> {access} -> pick any unique name")
    print("  2. Permissions tab: check files.content.write, click Submit")
    print("     (do this BEFORE the next step — scopes freeze into tokens)")
    print("  3. Settings tab: copy the App key and App secret")
    app_key = _ask("App key")
    app_secret = _ask_secret("App secret")
    print("\nNow authorize the app:")
    print(f"  -> {dropbox_authorize_url(app_key)}")
    if _confirm("Open in your browser?"):
        webbrowser.open(dropbox_authorize_url(app_key))
    while True:
        code = _ask("Paste the code Dropbox shows after you click Allow")
        token, error = exchange_dropbox_code(app_key, app_secret, code)
        if token:
            print("Dropbox authorized — refresh token stored.")
            print(
                "\nOpen-access PDF lookup (Unpaywall) requires a "
                "contact email; without one, non-arXiv papers cannot "
                "be delivered."
            )
            contact = ""
            while not contact:
                contact = _ask("Contact email (any address you own)")
            return {
                "DELIVERY_METHOD": "dropbox",
                "DROPBOX_APP_KEY": app_key,
                "DROPBOX_APP_SECRET": app_secret,
                "DROPBOX_REFRESH_TOKEN": token,
                "DROPBOX_FOLDER": folder,
                "CONTACT_EMAIL": contact,
            }
        print(f"{error}\nLet's try again.")


def _setup_kobo() -> dict[str, str]:
    print("\n[Kobo] Kobo only syncs the fixed folder Apps/Rakuten Kobo,")
    print("so the Dropbox app must have FULL Dropbox access.")
    values = _setup_dropbox(folder=_KOBO_FOLDER, full_access=True)
    print("\nOn the Kobo itself (whenever you're ready):")
    print("  More -> Settings -> Accounts -> Dropbox -> Get Started")
    return values


def _setup_cloud_folder() -> dict[str, str]:
    print("\n[Cloud folder] Papers land in a Dropbox folder that any")
    print("device with a Dropbox client can read.")
    folder = _ask("Folder path inside the app sandbox", "/Books")
    return _setup_dropbox(folder=folder, full_access=False)


def _setup_zotero() -> dict[str, str]:
    print("\n[Zotero] Create an API key with library + write access:")
    _offer_browser(_ZOTERO_KEYS_URL)
    while True:
        key = _ask_secret("Zotero API key (Enter to skip)")
        if not key:
            return {}
        print("Validating key...")
        user_id, error = validate_zotero_key(key)
        if user_id:
            print(f"Key OK — your library ID {user_id} was auto-detected.")
            return {
                "ZOTERO_API_KEY": key,
                "ZOTERO_LIBRARY_ID": user_id,
                "ZOTERO_LIBRARY_TYPE": "user",
            }
        print(f"{error}\nLet's try again (or press Enter to skip).")


_DEVICES = {
    "1": ("Kindle — delivery by email (simplest)", _setup_kindle),
    "2": ("Kobo — delivery via Dropbox sync", _setup_kobo),
    "3": ("PocketBook — delivery by email", _setup_pocketbook),
    "4": ("Other / cloud folder — Dropbox folder", _setup_cloud_folder),
}


def run(argv: list[str] | None = None) -> None:
    """Run the interactive wizard and write .env."""
    try:
        _run(argv)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup aborted — nothing was written.")


def _run(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="paperboy setup")
    parser.add_argument("--env", default=".env", help="path to .env file")
    args = parser.parse_args(argv)

    print("Welcome to paperboy setup.\n\nWhich e-reader do you use?")
    for key, (label, _) in _DEVICES.items():
        print(f"  {key}) {label}")
    choice = ""
    while choice not in _DEVICES:
        choice = _ask("Choose 1-4")
    values = _DEVICES[choice][1]()

    if _confirm("\nSet up a Zotero reading queue (recommended)?"):
        values.update(_setup_zotero())

    if _confirm(
        "\nWill you use paperboy remotely (claude.ai / mobile)?",
        default=False,
    ):
        token = secrets.token_urlsafe(32)
        values["MCP_AUTH_TOKEN"] = token
        print("Generated MCP_AUTH_TOKEN (paste into your claude.ai")
        print(f"connector as the bearer token):\n  {token}")

    update_env(values, path=args.env)
    print(f"\nWrote {len(values)} value(s) to {args.env}. Next steps:")
    print("  - Local use:  claude mcp add paperboy -- \\")
    print("      uv run --directory <path-to-this-repo> paperboy")
    print("    (--directory matters: the server loads .env from there)")
    print("  - Try it:     ask Claude to send a paper to your device")
    if "MCP_AUTH_TOKEN" in values:
        print("  - Remote use: deploy (see README) and add a claude.ai")
        print("    connector with your MCP_AUTH_TOKEN as the bearer token")
