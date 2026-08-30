"""Is any of this actually working? — the one panel that asks everything.

Split out of gui/app.py for the same reason as the seams before it, and by the
same rule: this is a subject with its own vocabulary (a check, a status, a
fix), not a line drawn at a convenient length. It reaches across sync, GitHub,
the browser extension, credentials and backups, but it is *about* none of
them — it is about telling someone what is wrong before it costs them
something.

That it reaches into every other mixin is not a smell here; it is what a mixin
is for. `self.sync_env`, `self._open_sync_store` and `self.ci_status` live on
DeviceApi, `self._active_repo_coords` on GitHubApi, and this is one instance
that has all of them.

A MIXIN, not a collaborator object — pywebview exposes the Api instance's
public methods by inspection, and an inherited method is found exactly like a
defined one, while a method moved onto a collaborator would not be: it would
fail only at runtime, in the app, on the one path nobody re-tests.

The feature itself, and why it exists, is documented at self_check.
"""

from __future__ import annotations

from pathlib import Path

from .. import backup as backup_module
from .. import evalprofile as evalprofile_module
from .. import ledger as ledger_module
from .. import browser_extension
from .. import githubsync
from .. import secretstore
from .. import syncstore
from ..config import all_providers, provider_key as cfg_provider_key
from ..config import save_config


class CheckupApi:
    # "Check my setup"
    #
    # Every bug this app has had that was expensive to find had the same
    # shape: something was off, or half-configured, or quietly damaged, and
    # NOTHING said so. A phone whose chats stopped listing. A sync index that
    # could not be read. A browser extension that reported Connected while
    # every command timed out. A credential blob that would not decrypt, which
    # you found out about by losing the rest of them.
    #
    # Each of those has its own panel, and each panel is somewhere you only go
    # once you already suspect that thing. This is the one place that asks all
    # of them at once, without being asked about any of them in particular.
    #
    # Three rules, and the third is the one this whole feature exists for:
    #
    # 1. A check says what to DO, not only what is wrong. "Sync is broken" is
    #    a dead end; the fix belongs beside it.
    # 2. One check failing must never stop the others. They run independently
    #    and an unexpected error becomes that check's own result.
    # 3. A check that could not run reports UNKNOWN, never ok. Reporting "fine"
    #    for something untested is the exact mistake behind half the entries in
    #    CLAUDE.md, and it would be a poor thing for the diagnostic itself to
    #    make.

    # The account name probed to prove the credential store can be read at all.
    # Nothing is ever stored under it; the read is the whole point.
    _HEALTH_ACCOUNT = "mnm-health-probe"

    def self_check(self) -> dict:
        """Everything that can be quietly half-working, asked all at once."""
        checks = []
        # The id is named here rather than derived from the method, so a row
        # that fails still knows which check it IS -- deriving it from
        # __name__ meant the one case that matters most came back unlabelled.
        for cid, fn in (("git", self._check_git),
                        ("backups", self._check_backups),
                        ("credentials", self._check_credentials),
                        ("model", self._check_model),
                        ("sync", self._check_sync),
                        ("extension", self._check_extension),
                        ("browser_model", self._check_browser_model),
                        ("ci", self._check_ci)):
            try:
                got = fn()
            except Exception as e:
                # Rule 2. A diagnostic that dies half way through is worse than
                # no diagnostic, because the rows it did print look complete.
                got = {"id": cid, "label": "This check failed to run",
                       "status": "unknown", "detail": f"{type(e).__name__}: {e}",
                       "fix": ""}
            if got:
                checks.append(got)
        rank = {"fail": 0, "warn": 1, "unknown": 2, "ok": 3}
        checks.sort(key=lambda c: rank.get(c["status"], 3))
        worst = checks[0]["status"] if checks else "ok"
        return {"checks": checks, "worst": worst,
                "problems": sum(1 for c in checks if c["status"] != "ok")}

    # ------------------------------------------------------------------ #
    # "What goes wrong here" -- self_check for the agent's BEHAVIOUR rather
    # than its configuration.
    #
    # Same reasoning as the panel above, one level in: every expensive bug in
    # this app was something quietly wrong that nothing said out loud, and
    # "this model keeps making the same mistake in this project" was nowhere
    # at all. The ledger has counted it all along; this is the only place a
    # person can read it.

    def ledger_report(self, all_projects: bool = False) -> dict:
        """Recorded tool failures for this project (or every project).

        Shows patterns BELOW the injection thresholds too. The panel's job is
        to say what happened, not only what is currently loud enough to have
        become a rule -- a pattern at two hits is exactly the one a person can
        still recognise and explain, and it is invisible to the model.
        """
        rows = ledger_module.report(None if all_projects else Path.cwd())
        return {
            "enabled": bool(getattr(self._cfg, "learn_from_mistakes", True)),
            "project": str(Path.cwd()),
            "all_projects": bool(all_projects),
            "file": str(ledger_module.LEDGER_FILE),
            "rows": rows[:200],
            "total": len(rows),
            # The number that says whether any of this WORKS. A pattern that
            # keeps firing with its own warning already in the prompt is not a
            # lesson the model failed to learn, it is a rule that does not
            # work -- and no amount of repeating it louder will change that.
            "warned_and_failed": sum(r["warned_and_failed"] for r in rows),
        }

    def ledger_forget(self, all_projects: bool = False) -> dict:
        """Clear the record. The user's own call -- nothing in the app does
        this on its own, and a success never deletes a pattern either."""
        ledger_module.forget(None if all_projects else Path.cwd())
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # The measured scaffold profile.
    #
    # evals.py could always answer "does this flag help"; nothing in the app
    # had ever read the answer, so the suite printed a number to a terminal
    # and the settings stayed wherever they happened to be. These two methods
    # are the whole of the wire, and there are two of them on purpose: one
    # says what was measured, the other changes something, and a measurement
    # that silently rewrote how every chat behaves would be the worst version
    # of this feature.

    def scaffold_profile(self) -> dict:
        """What the eval suite measured for the model this chat is using.

        `matches` is the important field: a profile that agrees with the
        current settings needs no prompt, and one that differs is the only
        reason to put anything on screen at all.
        """
        c = self._cfg
        row = evalprofile_module.get(c.model, c.base_url)
        if not row:
            return {"measured": False, "model": c.model,
                    "how": "python -m glmcode.evals --grid auto_fix_tests=false,true "
                           "--save-profile"}
        settings = evalprofile_module.sanitize(row.get("settings") or {})
        differs = {}
        for key, value in settings.items():
            if not hasattr(c, key):
                continue
            current = getattr(c, key)
            if isinstance(current, bool):
                value = str(value).lower() in ("1", "true", "yes", "on")
            elif isinstance(current, int) and not isinstance(current, bool):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            if current != value:
                differs[key] = {"now": current, "measured": value}
        return {
            "measured": True,
            "model": c.model,
            "summary": evalprofile_module.describe(c.model, c.base_url),
            "settings": settings,
            "rate": row.get("rate"),
            "baseline_rate": row.get("baseline_rate"),
            "baseline": row.get("baseline"),
            "cases": row.get("cases"),
            "at": row.get("at"),
            "matches": not differs,
            "differs": differs,
        }

    def apply_scaffold_profile(self) -> dict:
        """Put the measured settings into effect. An explicit action, never
        automatic: `Api._cfg` is one object shared by every chat and persisted
        by save_config, so anything that wrote to it on the agent's own
        initiative would change other chats and outlive the session that did
        it."""
        c = self._cfg
        changed = evalprofile_module.apply_to(c, c.model, c.base_url)
        if not changed:
            return {"ok": True, "changed": [],
                    "detail": "Nothing to change — the settings already match "
                              "what was measured."}
        save_config(c)
        if self._agent:
            self._agent.rebuild_system_prompt()
        return {"ok": True, "changed": changed,
                "detail": "Applied: " + ", ".join(changed)}

    def _check_git(self) -> dict:
        ok = githubsync.available()
        return {
            "id": "git", "label": "git",
            "status": "ok" if ok else "fail",
            "detail": "Found on your PATH." if ok else
                      "Not installed, or not on your PATH.",
            "fix": "" if ok else
                   "Install git. Without it there are no file backups, and no "
                   "cloning, pulling or pushing.",
        }

    def _check_backups(self) -> dict:
        """The shadow repo behind edit-and-resend, fork, and undo."""
        if not backup_module.available():
            return {"id": "backups", "label": "File backups", "status": "fail",
                    "detail": "Unavailable: they are git repositories, and git "
                              "isn't installed.",
                    "fix": "Install git, then reopen this chat."}
        if not self.auto_backup:
            return {"id": "backups", "label": "File backups", "status": "warn",
                    "detail": "Off for this chat, so nothing is snapshotted "
                              "before a turn.",
                    "fix": "Turn them on in Settings. Without a snapshot, "
                           "editing an earlier message cannot put the files back."}
        n = 0
        if self._backup_repo is not None:
            n = len(self._backup_repo.list_snapshots())
        return {"id": "backups", "label": "File backups", "status": "ok",
                "detail": f"On. {n} restore point{'' if n == 1 else 's'} for "
                          f"this chat.", "fix": ""}

    def _check_credentials(self) -> dict:
        """Whether the store can be READ, which is not the same as which
        backend it is. An unreadable one used to be discovered by saving a
        credential and losing the rest."""
        store = secretstore.get_store()
        try:
            store.read(self._HEALTH_ACCOUNT)
        except secretstore.SecretsUnreadable as e:
            return {"id": "credentials", "label": "Saved credentials",
                    "status": "fail", "detail": str(e),
                    "fix": "Nothing will be written until this is resolved, so "
                           "the credentials still in there are safe. If the key "
                           "file beside it is gone they cannot be recovered, and "
                           "deleting the file lets you start over."}
        except Exception as e:
            return {"id": "credentials", "label": "Saved credentials",
                    "status": "unknown",
                    "detail": f"Couldn't check: {type(e).__name__}: {e}", "fix": ""}
        secure = store.is_secure
        return {"id": "credentials", "label": "Saved credentials",
                "status": "ok" if secure else "warn",
                "detail": f"Readable, stored via {store.backend_name}."
                          + ("" if secure else
                             " The key sits on this disk beside them, so this "
                             "protects against a stray commit or a shoulder, "
                             "not against another program running as you."),
                "fix": "" if secure else
                       "Install the `keyring` package to use your operating "
                       "system's own credential store instead."}

    def _check_model(self) -> dict:
        """A provider with no key answers every turn with a 401."""
        provs = all_providers(self._cfg)
        if not provs:
            return {"id": "model", "label": "Model provider", "status": "fail",
                    "detail": "None configured.",
                    "fix": "Add one in Settings -> Models."}
        missing = [p.get("name") or p.get("base_url") or "?"
                   for p in provs if not cfg_provider_key(p)]
        if len(missing) == len(provs):
            return {"id": "model", "label": "Model provider", "status": "fail",
                    "detail": "No API key for any configured provider.",
                    "fix": "Paste a key in Settings -> Models. Every turn fails "
                           "with 401 until one is there."}
        if missing:
            return {"id": "model", "label": "Model provider", "status": "warn",
                    "detail": f"No API key for: {', '.join(missing)}.",
                    "fix": "A chat set to one of these fails on its first turn, "
                           "and a fallback chain that reaches one skips it."}
        return {"id": "model", "label": "Model provider", "status": "ok",
                "detail": f"{len(provs)} configured, all with a key.", "fix": ""}

    def _check_sync(self) -> dict:
        """Off, working, damaged, or unreachable -- four answers, because
        collapsing the last two is what cost the chat list in the first place."""
        env = self.sync_env()
        if not env.get("available"):
            return {"id": "sync", "label": "Shared chats", "status": "warn",
                    "detail": env.get("crypto_reason") or "Encryption unavailable.",
                    "fix": env.get("install_hint") or ""}
        if not env.get("enabled"):
            return {"id": "sync", "label": "Shared chats", "status": "ok",
                    "detail": "Off. Chats stay on this computer.", "fix": ""}
        store, err = self._open_sync_store()
        if err or store is None:
            return {"id": "sync", "label": "Shared chats", "status": "unknown",
                    "detail": err or "Couldn't open the store.",
                    "fix": "Usually the network. Nothing is wrong with your "
                           "chats -- try again when you are back online."}
        try:
            rows = store.list()
        except syncstore.SyncError as e:
            return {"id": "sync", "label": "Shared chats", "status": "fail",
                    "detail": str(e),
                    "fix": "Settings -> Your phone -> Rebuild the list. Your "
                           "chats are each stored separately and are still "
                           "there; it is the one small file naming them that "
                           "is damaged."}
        except githubsync.GitHubError as e:
            return {"id": "sync", "label": "Shared chats", "status": "unknown",
                    "detail": str(e), "fix": "Try again when you are online."}
        return {"id": "sync", "label": "Shared chats", "status": "ok",
                "detail": f"Working. {len(rows)} chat{'' if len(rows) == 1 else 's'} "
                          f"in the shared store.", "fix": ""}

    def _check_extension(self) -> dict:
        """Connected-but-deaf is the state this feature keeps being reported
        in, so it gets its own answer rather than folding into "connected"."""
        if self._cfg.browser_own == "off":
            return {"id": "extension", "label": "Your own browser", "status": "ok",
                    "detail": "Off. The agent uses its own browser.", "fix": ""}
        st = browser_extension.status(self._cfg, listen=False)
        if not st.get("port"):
            return {"id": "extension", "label": "Your own browser",
                    "status": "warn", "detail": "The port isn't open.",
                    "fix": "Open Settings -> Browser; it opens while that panel "
                           "is on screen."}
        if not st.get("connected"):
            return {"id": "extension", "label": "Your own browser",
                    "status": "warn",
                    "detail": f"Listening on {st['port']}, nothing connected.",
                    "fix": "Install the extension from Settings -> Browser. "
                           "Until then the agent launches a separate browser, "
                           "which is not signed in to anything."}
        who = st.get("browser") or "a browser"
        return {"id": "extension", "label": "Your own browser", "status": "ok",
                "detail": f"Connected: {who}.", "fix": ""}

    def _check_browser_model(self) -> dict:
        """Driving a page is the hardest thing a small model does here, and
        nothing said which model was doing it."""
        if not self._cfg.browser_model:
            return {"id": "browser_model", "label": "Browser agent model",
                    "status": "warn",
                    "detail": "Not set, so it uses whichever model the chat is on.",
                    "fix": "Set a stronger one in Settings -> Browser. Driving a "
                           "page is the hardest thing a small model does here, "
                           "and a weak one reads as the feature being broken."}
        return {"id": "browser_model", "label": "Browser agent model",
                "status": "ok", "detail": self._cfg.browser_model, "fix": ""}

    def _check_ci(self) -> dict:
        """Only meaningful for a chat whose folder is a GitHub repo."""
        if not self._active_repo_coords():
            return {}
        res = self.ci_status()
        if not res.get("ok"):
            return {"id": "ci", "label": "Run on a GitHub runner",
                    "status": "warn", "detail": res.get("reason") or "Unavailable.",
                    "fix": ""}
        if not res.get("installed"):
            return {"id": "ci", "label": "Run on a GitHub runner", "status": "warn",
                    "detail": "The workflow isn't installed in this repository.",
                    "fix": "Settings -> Tasks -> install it. A runner is the one "
                           "machine that is never asleep."}
        if res.get("outdated"):
            return {"id": "ci", "label": "Run on a GitHub runner", "status": "warn",
                    "detail": "Installed, but predates being startable from here.",
                    "fix": "Reinstall it from Settings -> Tasks."}
        return {"id": "ci", "label": "Run on a GitHub runner", "status": "ok",
                "detail": "Installed and up to date.", "fix": ""}

