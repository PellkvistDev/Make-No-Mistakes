"""Everything the desktop does to reach the OTHER machine.

Split out of gui/app.py, where the Api class had grown to 190 methods across
3,600 lines and every new feature landed in the same file. This is the first
seam taken, and it is a real one rather than a line drawn at a convenient
length: sync, pairing, Web Push and the CI runner are all one subject —
talking to a device that is not this one — and they share their whole
vocabulary (the sync store, the device lock, the phone's subscription).

A MIXIN, not a collaborator object. These methods reach all over the Api
instance: `self._cfg`, `self._chats`, `self._active`, `self._store`,
`self._save_chat`, `self.list_sessions`. Threading that through a separate
object would mean either passing the Api in (the same coupling, with a longer
path) or moving state that other methods also use. The mixin changes nothing
about how they run and everything about where they live — which is the point,
and why the existing sync, push and CI tests are the proof: they were not
touched, and they still pass.

pywebview exposes the Api instance's public methods to JavaScript by
inspection, and inherited methods are found exactly like defined ones, so the
bridge is unchanged.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .. import githubsync
from .. import secretstore
from .. import syncstore
from ..sessions import new_id


class DeviceApi:
    """Sync, pairing, push and the CI runner. Mixed into Api."""

    def sync_env(self):
        """What the UI needs to render the sync controls. When encryption is
        unavailable we return WHY plus the command that fixes it, so the panel
        can tell the user what to do instead of just switching itself off."""
        state, why = syncstore.crypto_status()
        on = bool(syncstore.load_passphrase())
        return {
            "available": state == "ok",
            "crypto_state": state,
            "crypto_reason": why,
            "install_hint": syncstore.INSTALL_HINT,
            "passphrase_set": on,
            "token_present": bool(self._gh_token()),
            "enabled": bool(state == "ok" and on and self._gh_token()),
            "repo": syncstore.SYNC_REPO_NAME,
        }

    def sync_enable(self):
        """Turn shared chats on, without asking anyone to invent anything.

        The passphrase was never a password: it is the key the chats are
        encrypted under, and its only job is to be identical on both devices.
        Pairing already carries it to the phone, so making the user think one
        up bought nothing but a chance to choose something weak, or to mistype
        it on the second device and fork the history into two halves that can
        never read each other.

        So this machine generates one. The only case that still needs a human
        is a store some OTHER machine already created, where the key is decided
        and cannot be guessed -- that is what the recovery code is for, and it
        is reported here rather than surfacing as `Wrong sync passphrase` for a
        passphrase the user never chose.
        """
        state, why = syncstore.crypto_status()
        if state != "ok":
            return {"error": why}
        if not self._gh_token():
            return {"error": "Connect a GitHub token first (Settings → GitHub)."}
        try:
            if syncstore.central_has_store(token=self._gh_token()):
                return {"needs_code": True, **self.sync_env(),
                        "error": "Shared chats are already set up on another "
                                 "device. Open its settings, copy the recovery "
                                 "code, and paste it below."}
            return self.sync_set_passphrase(syncstore.make_passphrase())
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return {"error": str(e)}

    def sync_recovery_code(self):
        """The generated key, for bringing another computer in.

        Shown rather than hidden, and this is the trade the generation makes:
        nobody types a passphrase, but nobody has one memorised either, so the
        only copy lives in this machine's credential store. A phone that has
        been paired holds it too. Both gone and the chats are ciphertext
        forever -- which is worth being able to see and write down.
        """
        code = syncstore.load_passphrase() or ""
        return {"code": code} if code else {"error": "Shared chats aren't on yet."}

    def sync_set_passphrase(self, passphrase: str):
        """Turn sync on: create the private sync repo if it doesn't exist yet,
        VERIFY the passphrase against it, then remember it. Verifying first means
        a passphrase that disagrees with your phone is caught here instead of
        silently forking your history into two unreadable halves."""
        passphrase = (passphrase or "").strip()
        if len(passphrase) < 6:
            return {"error": "That recovery code is too short to be one."}
        state, why = syncstore.crypto_status()
        if state != "ok":
            return {"error": why}
        if not self._gh_token():
            return {"error": "Connect a GitHub token first (Settings → GitHub)."}
        try:
            syncstore.open_central(passphrase, token=self._gh_token())
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return {"error": str(e)}
        syncstore.save_passphrase(passphrase)
        return {"ok": True, **self.sync_env()}

    def sync_forget_passphrase(self):
        syncstore.forget_passphrase()
        return {"ok": True, **self.sync_env()}

    def _open_sync_store(self):
        """(store, error) for the one central sync repo. No repo to pick, and it
        works for projects that aren't on GitHub at all."""
        try:
            _key, store, _created = syncstore.open_central(token=self._gh_token())
            return store, None
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return None, str(e)

    def sync_list_chats(self):
        """Every synced chat (newest first), from this computer and your phone.
        Marked `local` when this machine already has that session."""
        store, err = self._open_sync_store()
        if err:
            return {"error": err}
        try:
            rows = store.list()
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return {"error": str(e)}
        local_ids = {s["id"] for s in self._store.list()}
        for r in rows:
            r["local"] = r.get("id") in local_ids
        return {"chats": rows}

    def sync_finish_interrupted(self):
        """Finish a turn a phone was suspended part-way through.

        The phone runs the agent in the page, so iOS killing the app kills the
        request with it. It marks the chat and syncs it; this machine has no
        such problem, so it can pick the turn up and have the answer waiting.

        One chat per call, and it returns rather than raising: this runs on a
        timer, so it reports "nothing to do" far more often than not, and a
        machine that is offline or has sync switched off must simply do
        nothing rather than log an error every time round.
        """
        quiet = {"ok": True, "picked": None}
        if not self._cfg.sync_finish_interrupted:
            return quiet
        if not (syncstore.crypto_available() and syncstore.load_passphrase()):
            return quiet
        store, err = self._open_sync_store()
        if err or store is None:
            return quiet
        # The phone cannot subscribe until it knows this desktop's push key,
        # and this timer is the only thing that runs regularly with the store
        # open. set_vapid_public_key writes only when it changed, so this is
        # not a commit per tick.
        try:
            store.set_vapid_public_key(self.webpush_keys()["public"])
        except Exception:
            pass
        try:
            rows = store.list()
        except (syncstore.SyncError, githubsync.GitHubError):
            return quiet                      # offline: try again next tick
        for row in syncstore.pickup_candidates(rows):
            cid = row.get("id") or ""
            live = self._chats.get(cid)
            # Already running here -- either this scan started it a moment ago
            # or the user is driving it themselves.
            if live is not None and live.turn_lock.locked():
                continue
            try:
                chat = store.load(cid)
            except (syncstore.SyncError, githubsync.GitHubError):
                continue
            # The index is a cache rebuilt on every save; the body is the truth.
            # Between the list above and here, the phone may have come back and
            # finished the turn itself.
            if not chat or not chat.get("interrupted"):
                continue
            res = self._finish_one_interrupted(cid, chat)
            if res is not None:
                return res
        return quiet

    def ci_status(self):
        """Whether this chat's repo can run the agent on a GitHub runner."""
        from .. import ci
        coords = self._active_repo_coords()
        if not coords:
            return {"ok": False, "reason": "This chat's folder has no GitHub remote."}
        _host, owner, repo = coords
        token = githubsync.load_token("github.com") or ""
        if not token:
            return {"ok": False, "reason": "Add a GitHub token in Settings first."}
        out = ci.workflow_status(token, owner, repo)
        out.update({"ok": True, "owner": owner, "repo": repo})
        return out

    def ci_install(self):
        from .. import ci
        coords = self._active_repo_coords()
        if not coords:
            return {"error": "This chat's folder has no GitHub remote."}
        _host, owner, repo = coords
        token = githubsync.load_token("github.com") or ""
        if not token:
            return {"error": "Add a GitHub token in Settings first."}
        return ci.install_workflow(token, owner, repo)

    def ci_dispatch(self, task: str):
        """Hand a task to a runner. Returns as soon as GitHub accepts it: the
        work lands as a draft pull request, which is the review gate."""
        from .. import ci
        coords = self._active_repo_coords()
        if not coords:
            return {"error": "This chat's folder has no GitHub remote."}
        _host, owner, repo = coords
        token = githubsync.load_token("github.com") or ""
        if not token:
            return {"error": "Add a GitHub token in Settings first."}
        return ci.dispatch(token, owner, repo, task)

    def webpush_keys(self) -> dict:
        """This desktop's VAPID keypair, made once and kept.

        The public half is handed to the phone at subscribe time and the push
        service pins the subscription to it -- so regenerating it silently
        invalidates every subscription already out there, and this must never
        be a "create if missing" that mistakes a read failure for missing.
        """
        from .. import webpush
        store = secretstore.get_store()
        raw = store.get(self.VAPID_ACCOUNT)
        if raw:
            try:
                keys = json.loads(raw)
                if keys.get("private") and keys.get("public"):
                    return keys
            except (json.JSONDecodeError, ValueError):
                pass          # unreadable: replaced below, subscriptions rebuild
        keys = webpush.generate_keys()
        store.set(self.VAPID_ACCOUNT, json.dumps(keys))
        return keys

    def webpush_public_key(self):
        """What the phone needs to subscribe. Never the private half."""
        try:
            from .. import webpush   # noqa: F401  (import guards cryptography)
        except Exception as e:
            return {"error": f"push needs the cryptography package: {e}"}
        try:
            return {"ok": True, "key": self.webpush_keys()["public"]}
        except Exception as e:
            return {"error": str(e)}

    def _notify_phone(self, title: str, body: str, chat_id: str = "") -> None:
        """Best effort, always. A missed notification must never affect the
        turn that produced it -- this runs at the end of a turn that has
        already done real work."""
        try:
            from .. import webpush
        except Exception:
            return
        if not self._cfg.sync_finish_interrupted:
            return
        store, err = self._open_sync_store()
        if err or store is None:
            return
        try:
            subs = store.push_subscriptions()
        except Exception:
            return
        if not subs:
            return
        keys = self.webpush_keys()
        message = {"title": title, "body": body[:180],
                   "chatId": chat_id, "tag": chat_id or "mnm"}
        for sub in subs:
            result = webpush.send(sub, message, keys)
            # A dead endpoint is forgotten rather than retried forever: the
            # phone uninstalled the app, or the browser rotated its keys.
            if result.get("gone"):
                try:
                    store.drop_push_subscription(sub.get("endpoint") or "")
                except Exception:
                    pass

    def _finish_one_interrupted(self, cid: str, chat: dict):
        """Take one abandoned chat and start its turn here. None = not taken."""
        # Pull first: this rebuilds the session locally, applies the handoff
        # marker so the model stops imitating the phone's tools, and repairs
        # any tool_call the phone never got to answer.
        res = self.sync_pull_chat(cid)
        if res.get("error"):
            return None
        cs = self._chats.get(cid)
        if cs is None:
            return None
        if not cs.turn_lock.acquire(blocking=False):
            return None
        # The courtesy lock, taken WITHOUT force. If the phone is awake and
        # holding this chat, it is finishing its own turn and this machine must
        # not start a second one -- that is the failure this whole path exists
        # to avoid, and it is worse than the turn not finishing at all.
        if self._try_acquire_device_lock(cid, force=False) is not None:
            cs.turn_lock.release()
            return None
        # A named method taking the same (cs, message, paths, plan) shape the
        # thread always took, rather than a closure: the turn's arguments stay
        # inspectable from outside, which is how the pickup tests check that
        # what gets started is the pickup note and not something else.
        threading.Thread(
            target=self._finish_picked_up_turn,
            args=(cs, syncstore.pickup_note(), [], False,
                  cid, chat.get("title") or "your chat"), daemon=True).start()
        return {"ok": True, "picked": cid, "title": chat.get("title") or ""}

    def _finish_picked_up_turn(self, cs: "ChatState", message, paths: list,
                               plan: bool, cid: str, title: str) -> None:
        self._run_send_turn(cs, message, paths, plan)
        # The point of the whole feature: the phone could not finish this, so
        # it is not running to notice that we did. Sent AFTER the turn -- "I
        # picked it up" is not news; "it is done" is.
        self._notify_phone("Finished on your desktop",
                           f"\u201c{title}\u201d is done \u2014 the answer is waiting.", cid)

    def sync_pull_chat(self, chat_id: str):
        """Download one synced chat into the local session store and open it."""
        store, err = self._open_sync_store()
        if err:
            return {"error": err}
        try:
            chat = store.load(chat_id)
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return {"error": str(e)}
        sess = syncstore.chat_to_session(chat)
        if not sess.get("messages"):
            return {"error": "That chat has no messages yet."}
        # Taking over a chat the phone was driving: tell the model the tools
        # changed, or it will keep imitating turns that can't work here.
        sess["messages"] = syncstore.apply_handoff(
            sess["messages"], sess.get("device", ""), "desktop")
        # Anything the phone couldn't run goes into context now, so this machine
        # opens already knowing what was left for it rather than the ask being
        # buried somewhere up the transcript.
        pending_note = syncstore.pending_note(sess.get("pending") or [])
        if pending_note:
            sess["messages"].append({"role": "system", "content": pending_note})
        # Land it in a folder that exists here: a phone-written chat has no
        # local folder, and another machine's cwd won't resolve on this one.
        cs = self._active
        cwd = sess.get("cwd") or ""
        if not cwd or not Path(cwd).is_dir():
            cwd = str(cs.agent.workdir) if cs else str(self._clone_root())
        res = self._activate_session(sess["id"], sess["messages"], cwd, 0, 0,
                                     sess.get("todos", []), sess.get("title", ""),
                                     model_provider=sess.get("model_provider", ""),
                                     model=sess.get("model", ""))
        live = self._chats.get(sess["id"])
        if live:
            live.synced_at = int(chat.get("updated") or 0)
        # Tell the user too, not just the agent -- otherwise the only sign is
        # the agent suddenly running something they didn't ask for.
        res["pending"] = len([p for p in (sess.get("pending") or [])
                              if str(p.get("task", "")).strip()])
        self._save_current()
        res["sessions"] = self.list_sessions()
        return res

    def sync_push_chat(self, sid: str = ""):
        """Upload a chat (default: the active one) to the sync repo."""
        sid = sid or self.session_id or ""
        if not sid:
            return {"error": "Open a chat first."}
        live = self._chats.get(sid)
        if live:
            self._save_chat(live)  # flush the newest turn before uploading
        data = self._store.load(sid)
        if not data:
            return {"error": "session not found"}
        store, err = self._open_sync_store()
        if err:
            return {"error": err}
        try:
            updated = store.save(syncstore.session_to_chat(
                data, self._repo_state(data.get("cwd")),
                repo=self._chat_repo(data.get("cwd"))))
        except syncstore.ChatDeletedElsewhere:
            # Deleted on the phone while this machine still had it open. Saving
            # would bring it back, and keep bringing it back after every turn.
            if live:
                live.synced_at = 0
            return {"error": "That chat was deleted on another device, so it wasn't uploaded."}
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return {"error": str(e)}
        # Remember our own write, so catch-up doesn't mistake it for the phone.
        if live:
            live.synced_at = updated or 0
        return {"ok": True, "id": sid}

    def sync_catch_up(self):
        """Adopt the open chat's synced copy if another device moved it on.

        The mirror of the phone's foreground catch-up: the whole point of sync
        is putting the phone down and finding the desktop already current. Quiet
        by design -- it reports "nothing to do" far more often than not, and is
        never allowed to interrupt a turn in progress or fail loudly.
        """
        cs = self._active
        if cs is None or not self.session_id:
            return {"ok": True, "changed": False}
        if not (syncstore.crypto_available() and syncstore.load_passphrase()):
            return {"ok": True, "changed": False}
        # Mid-turn: the agent owns the message list right now. Try again later.
        if cs.turn_lock.locked():
            return {"ok": True, "changed": False}
        store, err = self._open_sync_store()
        if err or not store:
            return {"ok": True, "changed": False}
        try:
            row = next((r for r in store.list() if r.get("id") == cs.sid), None)
        except (syncstore.SyncError, githubsync.GitHubError):
            return {"ok": True, "changed": False}   # offline: keep what we have
        if not row or not row.get("updated"):
            return {"ok": True, "changed": False}
        if row["updated"] <= (cs.synced_at or 0):
            return {"ok": True, "changed": False}   # our own push, or nothing new
        # Something else advanced this chat. sync_pull_chat re-activates it,
        # which also re-derives the handoff marker and kicks off a repo pull.
        res = self.sync_pull_chat(cs.sid)
        if res.get("error"):
            return {"ok": True, "changed": False}
        live = self._chats.get(cs.sid)
        if live:
            live.synced_at = row["updated"]
        res["changed"] = True
        res["from_device"] = row.get("device") or "another device"
        return res

    def sync_delete_chat(self, chat_id: str):
        """Remove a chat from the shared store (all devices). Local copy stays."""
        store, err = self._open_sync_store()
        if err:
            return {"error": err}
        try:
            store.remove(chat_id)
        except (syncstore.SyncError, githubsync.GitHubError) as e:
            return {"error": str(e)}
        return {"ok": True}

