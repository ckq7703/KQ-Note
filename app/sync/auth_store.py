"""Credential storage for cloud sync.

Tokens go through the OS keyring (Windows Credential Manager) rather than
config.json, so they never sit on disk in plaintext.
"""

import keyring
import keyring.errors

SERVICE = "KQNote"


def set_tokens(access_token, refresh_token):
    keyring.set_password(SERVICE, "access_token", access_token)
    keyring.set_password(SERVICE, "refresh_token", refresh_token)


def set_access_token(access_token):
    keyring.set_password(SERVICE, "access_token", access_token)


def get_access_token():
    return keyring.get_password(SERVICE, "access_token")


def get_refresh_token():
    return keyring.get_password(SERVICE, "refresh_token")


def set_account_email(email):
    keyring.set_password(SERVICE, "account_email", email or "")


def get_account_email():
    return keyring.get_password(SERVICE, "account_email")


def is_logged_in():
    return bool(get_access_token())


def clear():
    for key in ("access_token", "refresh_token", "account_email"):
        try:
            keyring.delete_password(SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass
