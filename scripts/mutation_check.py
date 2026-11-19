"""Break each safety rule on purpose and require the tests to notice.

A green suite proves nothing about a guard unless the suite goes red without it. Each mutation below
removes one guard; the named tests must then fail. A mutation whose text is no longer found is itself a
failure, so a refactor cannot silently retire a check. When two checks hold one rule, both are removed.

    python scripts/mutation_check.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "exchange_mcp"

# (what it protects, file, [(original, replacement), ...], test file that must fail)
MUTATIONS = [
    (
        "the guard asks for the person's word",
        "core/guard.py",
        [("if user_confirmed is not True:", "if False:")],
        "tests/unit/test_core.py",
    ),
    ("the bucket", "core/guard.py", [("        if not taken:\n", "        if False:\n")], "tests/unit/test_core.py"),
    (
        "the in-process bucket runs out",
        "state/store.py",
        [("        taken = tokens >= 1\n", "        taken = True\n")],
        "tests/unit/test_core.py",
    ),
    (
        "the recipient cap in the guard",
        "core/guard.py",
        [("if recipients > MAX_RECIPIENTS:", "if False:")],
        "tests/unit/test_core.py",
    ),
    (
        "only the boolean true confirms",
        "server.py",
        [("if p in args and not isinstance(args[p], bool):", "if False:")],
        "tests/unit/test_server.py",
    ),
    (
        "unknown arguments are refused",
        "server.py",
        [("        if unknown:\n", "        if False:\n")],
        "tests/unit/test_server.py",
    ),
    (
        "ids are strings",
        "server.py",
        [
            (
                "            if (\n                isinstance(v, (int, float))",
                "            if False and (\n                isinstance(v, (int, float))",
            )
        ],
        "tests/unit/test_server.py",
    ),
    (
        "send_draft needs the person's word",
        "tools/mail.py",
        [
            ('        c.rt.guard.confirmed(kind="mail", user_confirmed=user_confirmed)\n', ""),
            (
                'receipt = await c.guard(kind="mail", user_confirmed=user_confirmed,',
                'receipt = await c.guard(kind="mail", user_confirmed=True,',
            ),
        ],
        "tests/unit/test_mail.py",
    ),
    (
        "only drafts made here are sent",
        "tools/mail.py",
        [("        if not d.ours or not d.is_draft:\n", "        if False:\n")],
        "tests/unit/test_mail.py",
    ),
    (
        "only drafts made here are changed",
        "tools/mail.py",
        [("            if not current.ours or not current.is_draft:\n", "            if False:\n")],
        "tests/unit/test_mail.py",
    ),
    (
        "at most 20 recipients in a draft",
        "tools/mail.py",
        [("if len(to_l) + len(cc_l) + len(bcc_l) > MAX_RECIPIENTS:", "if False:")],
        "tests/unit/test_mail.py",
    ),
    (
        "an automatic reply needs the person's word",
        "tools/mail.py",
        [
            (
                'c.guard(kind="auto_reply", user_confirmed=user_confirmed)',
                'c.guard(kind="auto_reply", user_confirmed=True)',
            )
        ],
        "tests/unit/test_mail.py",
    ),
    (
        "deleting a folder needs the person's word",
        "tools/mail.py",
        [('        c.rt.guard.confirmed(kind="folder", user_confirmed=user_confirmed, target=folder)\n', "")],
        "tests/unit/test_mail.py",
    ),
    (
        "attachments are bare names",
        "tools/mail.py",
        [('if not _FILE_NAME.match(f) or f.startswith((".", "-", "~")) or f in (".", ".."):', "if False:")],
        "tests/unit/test_mail.py",
    ),
    (
        "distribution lists are refused",
        "core/resolve.py",
        [("if p.kind in GROUPS:", "if False:")],
        "tests/unit/test_mail.py",
    ),
    (
        "ambiguity lists candidates",
        "core/resolve.py",
        [("    if len(pool) > 1:\n", "    if False:\n")],
        "tests/unit/test_mail.py",
    ),
    ("the EWS allowlist", "ews/client.py", [("if operation not in ALLOWED:", "if False:")], "tests/unit/test_ews.py"),
    (
        "staged keys are readable by the server's user only",
        "keys/stage.py",
        [("            os.fchmod(fd, 0o400)", "            os.fchmod(fd, 0o440)")],
        "tests/unit/test_stage.py",
    ),
    (
        "an empty Secret stops the pod",
        "keys/stage.py",
        [("    if n == 0:", "    if False:")],
        "tests/unit/test_stage.py",
    ),
    (
        "the process hides its memory before reading settings",
        "app.py",
        [("    if not non_dumpable():", "    if False:")],
        "tests/unit/test_harden.py",
    ),
    (
        "NTLM: a failed handshake is tried once more",
        "ews/client.py",
        [("        for attempt in (1, 2):", "        for attempt in (1,):")],
        "tests/unit/test_ews.py",
    ),
    (
        "NTLM: a failed handshake names the next step, not a raw code",
        "ews/client.py",
        [
            (
                '        raise ExchangeError("handshake", ref=ref)',
                '        raise ExchangeError("failed", code="ntlm", ref=ref)',
            )
        ],
        "tests/unit/test_ews.py",
    ),
    (
        "errors inside HTTP 200",
        "ews/client.py",
        [('errors = [m for m in checked if m.get("ResponseClass") == "Error"]', "errors = []")],
        "tests/unit/test_ews.py",
    ),
    (
        "no key material from the environment",
        "config.py",
        [("    if named:\n", "    if False:\n")],
        "tests/unit/test_keys.py",
    ),
    (
        "key files are closed",
        "keys/providers.py",
        [("    if mode & stat.S_IRWXO:\n        return True\n", "")],
        "tests/unit/test_keys.py",
    ),
    (
        "a blob opens only as its own kind",
        "keys/seal.py",
        [('if not isinstance(body, dict) or body.get("k") != kind:', "if not isinstance(body, dict):")],
        "tests/unit/test_keys.py",
    ),
    (
        "blobs expire",
        "keys/seal.py",
        [("if not isinstance(exp, int) or exp < now:", "if not isinstance(exp, int):")],
        "tests/unit/test_keys.py",
    ),
    (
        "a code is used once",
        "oauth/provider.py",
        [
            ("        if not first:\n", "        if False:\n"),
        ],
        "tests/unit/test_oauth.py",
    ),
    (
        "a code belongs to its client",
        "oauth/provider.py",
        [
            (
                '        if p is None or p["client_id"] != client.client_id:\n'
                '            metrics.auth_refused("invalid_grant")\n'
                "            return None\n        return AuthorizationCode(",
                '        if p is None:\n            metrics.auth_refused("invalid_grant")\n'
                "            return None\n        return AuthorizationCode(",
            )
        ],
        "tests/unit/test_oauth.py",
    ),
    (
        "redirect URIs are https or loopback",
        "oauth/provider.py",
        [("if not redirect_allowed(str(uri)):", "if False:")],
        "tests/unit/test_oauth.py",
    ),
    (
        "sign-in attempts are limited",
        "auth/limiter.py",
        [("        if not taken:\n            raise Limited", "        if False:\n            raise Limited")],
        "tests/unit/test_signin.py",
    ),
    (
        "the in-process window counts to its limit",
        "state/store.py",
        [("                if len(q) >= limit:\n", "                if False:\n")],
        "tests/unit/test_signin.py",
    ),
    (
        "logins of another domain are refused",
        "auth/login.py",
        [
            (
                "        if given.lower() not in accepted:\n            raise BadLogin\n    elif",
                "        if False:\n            raise BadLogin\n    elif",
            ),
            (
                "        if given.lower() not in accepted:\n            raise BadLogin\n    else",
                "        if False:\n            raise BadLogin\n    else",
            ),
        ],
        "tests/unit/test_signin.py",
    ),
    (
        "password mode refresh is capped",
        "config.py",
        [('if mode == "password" and not 1 <= refresh_hours <= PASSWORD_REFRESH_MAX_HOURS:', "if False:")],
        "tests/unit/test_config.py",
    ),
    (
        "upstreams are https",
        "config.py",
        [('if u.scheme != "https" or not u.hostname or u.username or u.password:', "if False:")],
        "tests/unit/test_config.py",
    ),
    (
        "a link opens only for its person",
        "links/routes.py",
        [('if cred is None or person_key(cred.login) != link.get("p"):', "if cred is None:")],
        "tests/unit/test_links.py",
    ),
    (
        "logs drop content",
        "observability.py",
        [("if _SHAPES.search(message) or (record.exc_text and _SHAPES.search(record.exc_text)):", "if False:")],
        "tests/unit/test_observability.py",
    ),
    (
        "the AD FS key file is closed",
        "auth/adfs.py",
        [("        if exposed(path.stat()):\n", "        if False:\n")],
        "tests/unit/test_adfs.py",
    ),
    (
        "a refresh cannot switch people",
        "auth/adfs.py",
        [("if previous is not None and previous.login != login:", "if False:")],
        "tests/unit/test_adfs.py",
    ),
    (
        "only the organiser changes a meeting",
        "tools/calendar.py",
        [("            if not existing.organised_by(c.me):\n", "            if False:\n")],
        "tests/unit/test_calendar.py",
    ),
    (
        "only the organiser cancels",
        "tools/calendar.py",
        [
            (
                '        if not ev.organised_by(c.me):\n            raise ExchangeError("not_organiser")',
                '        if False:\n            raise ExchangeError("not_organiser")',
            )
        ],
        "tests/unit/test_calendar.py",
    ),
    (
        "invitations need the person's word",
        "tools/calendar.py",
        [
            ('            c.rt.guard.confirmed(kind="invite", user_confirmed=user_confirmed)\n', "            pass\n"),
            (
                'receipt = await c.guard(kind="invite", user_confirmed=user_confirmed, recipients=told)',
                'receipt = await c.guard(kind="invite", user_confirmed=True, recipients=told)',
            ),
        ],
        "tests/unit/test_calendar.py",
    ),
    (
        "cancelling needs the person's word",
        "tools/calendar.py",
        [
            (
                'receipt = await c.guard(kind="cancel", user_confirmed=user_confirmed, recipients=told)',
                'receipt = await c.guard(kind="cancel", user_confirmed=True, recipients=told)',
            )
        ],
        "tests/unit/test_calendar.py",
    ),
    (
        "answering an invitation draws from the bucket",
        "tools/calendar.py",
        [('receipt = await c.rt.guard.spend(c.person, kind="invite") if send_response else {}', "receipt = {}")],
        "tests/unit/test_calendar.py",
    ),
    (
        "the organiser does not answer their own meeting",
        "tools/calendar.py",
        [("        if ev.organised_by(c.me):\n", "        if False:\n")],
        "tests/unit/test_calendar.py",
    ),
    (
        "at most 20 attendees and rooms",
        "tools/calendar.py",
        [("if given > MAX_RECIPIENTS:", "if False:")],
        "tests/unit/test_calendar.py",
    ),
    (
        "a repeat must end",
        "tools/calendar.py",
        [("if repeat and not (repeat_until or repeat_count):", "if False:")],
        "tests/unit/test_calendar.py",
    ),
    (
        "only meeting responses leave from the calendar sender",
        "ews/calendar.py",
        [("if etree.QName(response).localname not in MEETING_RESPONSES:", "if False:")],
        "tests/unit/test_calendar.py",
    ),
    (
        "a contact id must be a contact",
        "ews/people.py",
        [('if etree.QName(found[0]).localname != "Contact":', "if False:")],
        "tests/unit/test_contacts.py",
    ),
    (
        "contacts have three address and four phone slots",
        "tools/contacts.py",
        [("if len(emails or []) > len(ep.EMAIL_KEYS) or len(phones or []) > len(ep.PHONE_KEYS):", "if False:")],
        "tests/unit/test_contacts.py",
    ),
    (
        "NTLM: the authenticated request closes its connection",
        "ews/transport.py",
        [('        request.headers["Connection"] = "close"\n', "")],
        "tests/unit/test_ews.py",
    ),
    (
        "NTLM: an unchallenged probe is refused",
        "ews/transport.py",
        [
            (
                "        if response.status_code != 401:\n            raise HandshakeFailed",
                "        if False:\n            raise HandshakeFailed",
            )
        ],
        "tests/unit/test_ews.py",
    ),
    (
        "the limiter does not grow without bound",
        "state/store.py",
        [("                raise StoreFull  # a flood", "                pass  # a flood")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "the bucket table does not grow without bound",
        "state/store.py",
        [("                raise StoreFull\n        tokens, at", "                pass\n        tokens, at")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "an exception is checked before it is logged",
        "observability.py",
        [("            record.exc_text = logging.Formatter().formatException(record.exc_info)", "            pass")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "blind copies count against the cap",
        "tools/mail.py",
        [("count = len(d.to) + len(d.cc) + len(d.bcc)", "count = len(d.to) + len(d.cc)")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "turning the automatic reply off keeps its texts",
        "tools/mail.py",
        [
            (
                '                internal=before["internal"],',
                '                internal="",',
            )
        ],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "categories are not cut silently",
        "tools/mail.py",
        [("            if len(cats) > MAX_CATEGORIES:", "            if False:")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "a live code is never forgotten",
        "state/store.py",
        [
            (
                "                # Forgetting a live mark would let a code be redeemed twice: refuse instead.\n"
                "                raise StoreFull",
                "                self._once.clear()",
            )
        ],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "an ended AD FS sign-in gets no token",
        "oauth/provider.py",
        [("            if left <= 0:", "            if False:")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "key ids are unique in a key file",
        "keys/providers.py",
        [("        if any(kid == k for k, _ in keys):", "        if False:")],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "the AD FS callback belongs to the browser that agreed",
        "auth/adfs.py",
        [
            (
                'if state is None or not code or not browser or not hmac.compare_digest(browser, state.get("b", "")):',
                "if state is None or not code:",
            )
        ],
        "tests/unit/test_adfs.py",
    ),
    (
        "a refresh does not extend the session",
        "oauth/provider.py",
        [
            (
                '            refresh_token=await self.sealer.seal("refresh", body, session_left),',
                '            refresh_token=await self.sealer.seal("refresh", body, self._refresh_ttl),',
            ),
            (
                "        session_left = int(started + self._refresh_ttl - self._clock())",
                "        session_left = self._refresh_ttl",
            ),
        ],
        "tests/unit/test_oauth.py",
    ),
    (
        "only the operator's client hosts register",
        "oauth/provider.py",
        [("            if self._client_hosts and host not in self._client_hosts:", "            if False:")],
        "tests/unit/test_oauth.py",
    ),
    (
        "a namesake is not the person",
        "auth/password.py",
        [
            (
                "        mine = [p for p in found if p.address.lower() == login_l]",
                "        mine = [p for p in found if p.address.lower() == login_l"
                " or p.address.split('@')[0].lower() == me_l]",
            )
        ],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "a list behind a secondary address is refused",
        "core/resolve.py",
        [
            (
                "primary address.\n            return _no_group(found[0])",
                "primary address.\n            return found[0]",
            )
        ],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "an opened blob does not expand without bound",
        "keys/seal.py",
        [
            ("        plain = d.decompress(raw, MAX_PLAIN)", "        plain = zlib.decompress(raw)"),
            ("        if d.unconsumed_tail or not d.eof:", "        if False:"),
        ],
        "tests/unit/test_review_fixes.py",
    ),
    (
        "the sign-in page names the application",
        "signin.py",
        [
            (
                "        page = _page(lang, form=consent + _form(lang, rid, token=token), redirect=_origin(p))",
                "        page = _page(lang, form=_form(lang, rid, token=token), redirect=_origin(p))",
            )
        ],
        "tests/unit/test_signin.py",
    ),
    (
        "the sign-in form must come from this site's page",
        "signin.py",
        [
            (
                "        if not _same_origin(request) or not token or not hmac.compare_digest(token.encode(), posted):",
                "        if False:",
            )
        ],
        "tests/unit/test_signin.py",
    ),
    (
        "a cross-site post is refused even with a token",
        "signin.py",
        [("        return _origin_of(origin) == _origin_of(runtime().settings.public_url)", "        return True")],
        "tests/unit/test_signin.py",
    ),
    (
        "the session starts at sign-in",
        "oauth/provider.py",
        [('            started=int(p.get("s0", now)),', "            started=int(now),")],
        "tests/unit/test_oauth.py",
    ),
    (
        "an upload link attaches its file once",
        "links/routes.py",
        [("            if any(a.name == name and not a.inline for a in draft.attachments):", "            if False:")],
        "tests/unit/test_links.py",
    ),
    (
        "a draft with an element marked hidden is refused",
        "core/html.py",
        [("    if tokens.found:", "    if False:")],
        "tests/unit/test_html.py",
    ),
    (
        "a contact's own name and title are read from the contact",
        "tools/people.py",
        [("            shown = await _with_details(c, shown)", "            pass")],
        "tests/unit/test_people.py",
    ),
    (
        "a sender address is held to the exact address",
        "tools/mail.py",
        [("        if from_address:", "        if False:")],
        "tests/unit/test_qa_mail.py",
    ),
    (
        "previews never carry hidden text",
        "tools/mail.py",
        [
            (
                "        html, got = await _previews(c, found)",
                "        html, got = {m.id: m.preview for m in found}, True",
            )
        ],
        "tests/unit/test_qa_mail.py",
    ),
    # ---------------------------------------------------------------- many replicas (0.2.0)
    (
        "shared: a code is marked once in Valkey",
        "state/remote.py",
        [("  if redis.call('EXISTS', k) == 1 then return 0 end\n", "")],
        "tests/unit/test_state_contract.py",
    ),
    (
        "a code stays redeemed across a key rotation",
        "keys/seal.py",
        [
            (
                "for raw in self._raw]",
                "for raw in self._raw[:1]]",
            )
        ],
        "tests/unit/test_state_contract.py",
    ),
    (
        "shared: the Valkey window stops at its limit",
        "state/remote.py",
        [("  if redis.call('ZCARD', k) >= tonumber(ARGV[i + 2]) then return 0 end\n", "")],
        "tests/unit/test_state_contract.py",
    ),
    (
        "a store out of memory refuses the window script before it runs",
        "state/remote.py",
        [('WINDOW = """#!lua\n', 'WINDOW = """\n')],
        "tests/unit/test_state_contract.py",
    ),
    (
        "shared: the Valkey bucket runs out",
        "state/remote.py",
        [("if tokens >= 1 then", "if true then")],
        "tests/unit/test_state_contract.py",
    ),
    (
        "shared: a code redeemed on one replica is refused on another",
        "app.py",
        [
            (
                "        store,\n        public_url",
                "        MemoryStore(Namer(sealer.derive, 'own:')),\n        public_url",
            ),
            (
                "from exchange_mcp.state.store import Namer, StateStore",
                "from exchange_mcp.state.store import MemoryStore, Namer, StateStore",
            ),
        ],
        "tests/unit/test_replicas.py",
    ),
    (
        "shared: sign-in attempts on one replica count on another",
        "signin.py",
        [
            (
                "        self.limiter = Limiter(rt.store,",
                "        from exchange_mcp.state.store import MemoryStore, Namer\n\n"
                "        self.limiter = Limiter(MemoryStore(Namer(rt.sealer.derive, 'own:')),",
            )
        ],
        "tests/unit/test_replicas.py",
    ),
    (
        "no identifier reaches the store in clear",
        "state/store.py",
        [
            (
                '{hmac.new(k, message, hashlib.sha256).hexdigest()[:40]}" for k in self._keys]',
                '{ident}" for k in self._keys]',
            )
        ],
        "tests/unit/test_state_contract.py",
    ),
    (
        "fail closed: sign-in refuses when the store is down",
        "auth/limiter.py",
        [
            (
                "from exchange_mcp.state.store import StateStore, StoreFull",
                "from exchange_mcp.state.store import StateStore, StoreFull, StoreUnavailable",
            ),
            (
                "        except StoreFull:\n            raise Limited",
                "        except StoreUnavailable:\n            return\n"
                "        except StoreFull:\n            raise Limited",
            ),
        ],
        "tests/unit/test_replicas.py",
    ),
    (
        "fail closed: sending refuses when the store is down",
        "core/guard.py",
        [
            (
                '            metrics.guard_refused("state_unavailable")\n            raise ExchangeError(',
                "            taken, tokens = True, 0.0\n            if False:\n                raise ExchangeError(",
            )
        ],
        "tests/unit/test_replicas.py",
    ),
    (
        "fail closed: a code is not redeemed when the store is down",
        "oauth/provider.py",
        [
            (
                '            metrics.auth_refused("state_unavailable")\n            raise TokenError(',
                "            first = True\n            if False:\n                raise TokenError(",
            )
        ],
        "tests/unit/test_replicas.py",
    ),
    (
        "readiness follows the key, not the store",
        "app.py",
        [("status_code=200 if key else 503", "status_code=200 if key and store_ok else 503")],
        "tests/unit/test_replicas.py",
    ),
    (
        "readiness refuses without a working key",
        "app.py",
        [("status_code=200 if key else 503", "status_code=200")],
        "tests/unit/test_replicas.py",
    ),
    (
        "an upload link takes one upload at a time",
        "links/links.py",
        [('if body is None or not await self._store.once("upload", body["u"], UPLOAD_TTL):', "if body is None:")],
        "tests/unit/test_links.py",
    ),
    (
        "two Secrets never stage the same file",
        "keys/stage.py",
        [("    if twice:\n", "    if False:\n")],
        "tests/unit/test_stage.py",
    ),
    (
        "logs: every record is one JSON object",
        "observability.py",
        [
            (
                "        return json.dumps(out, ensure_ascii=False, default=str)",
                "        return f\"{out['level']} {out['msg']}\"",
            )
        ],
        "tests/unit/test_logs_json.py",
    ),
    (
        "logs: every record has its time",
        "observability.py",
        [('            "ts": ts,\n', "")],
        "tests/unit/test_logs_json.py",
    ),
    (
        "logs: key staging writes JSON",
        "keys/stage.py",
        [("    configure()\n", "")],
        "tests/unit/test_logs_json.py",
    ),
    (
        "logs: an uncaught exception is JSON",
        "observability.py",
        [("    sys.excepthook = _uncaught\n", "")],
        "tests/unit/test_logs_json.py",
    ),
    (
        "logs: a refused start is JSON",
        "app.py",
        [("    except ConfigError as e:\n", "    except ZeroDivisionError as e:\n")],
        "tests/unit/test_logs_json.py",
    ),
    (
        "logs: an exception's message is withheld",
        "observability.py",
        [
            (
                '            out["exc"] = bare_traceback(record.exc_info)',
                '            out["exc"] = self.formatException(record.exc_info)',
            )
        ],
        "tests/unit/test_logs_json.py",
    ),
    (
        "a new message is signed only when OWA signs new messages",
        "core/signature.py",
        [('    if kind == "new" and not owa.on_new:\n', "    if False:\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "a reply or forward is signed only when OWA signs replies",
        "core/signature.py",
        [('    if kind == "reply" and not owa.on_reply:\n', "    if False:\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "signature=false leaves the signature out",
        "core/signature.py",
        [("    if not wanted:\n", "    if False:\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "the server switch turns signatures off",
        "core/signature.py",
        [('    if setting != "owa":\n', "    if False:\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "the signature passes the same sanitiser as a body",
        "core/signature.py",
        [("    prepared = prepare_outgoing(source)\n", '    prepared = type(prepare_outgoing(""))(html=source)\n')],
        "tests/unit/test_signature.py",
    ),
    (
        "a body that carries the signature is not signed again",
        "core/signature.py",
        [("    if sig.added and carries(body_html, sig.html):\n", "    if False:\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "an edit signs again only a draft saved with a signature",
        "tools/mail.py",
        [("            elif not current.signed:\n", "            elif False:\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "an edit that keeps the body leaves the signature alone",
        "tools/mail.py",
        [
            (
                "            if html_body is None:\n                sig = sigs.Signature(",
                "            if False:\n                sig = sigs.Signature(",
            )
        ],
        "tests/unit/test_signature.py",
    ),
    (
        "the metrics server's start and stop lines are not written twice",
        "app.py",
        [("    task = asyncio.create_task(_side_serve(side))\n", "    task = asyncio.create_task(side.serve())\n")],
        "tests/unit/test_startup.py",
    ),
    (
        "the store is measured from the start, before any request",
        "app.py",
        [
            (
                "            await ping_store(store)  # before the first request",
                "            pass  # before the first request",
            )
        ],
        "tests/unit/test_startup.py",
    ),
    (
        "an edit that puts the signature back does not attach its images again",
        "core/signature.py",
        [("    seen = set(attached)\n", "    seen = set()\n")],
        "tests/unit/test_signature.py",
    ),
    (
        "an image-only signature is recognised in a body that carries it",
        "core/signature.py",
        [("    return bool(images) and images <= set(_SRC.findall(body_html))\n", "    return False\n")],
        "tests/unit/test_signature.py",
    ),
]


def run(selector: str) -> int:
    """pytest's exit code: 0 passed, 1 a test failed. Anything else (2 collection error, 4 usage, 5 no
    tests) is not evidence that a test caught the mutation, so it is reported as its own problem."""
    return subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", selector],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).returncode


def main() -> int:
    failures = []
    for what, rel, edits, selector in MUTATIONS:
        path = SRC / rel
        original = path.read_text()
        mutated = original
        for old, new in edits:
            if mutated.count(old) != 1:
                failures.append(f"{what}: the guarded code was not found exactly once in {rel}")
                break
            mutated = mutated.replace(old, new)
        else:
            try:
                path.write_text(mutated)
                code = run(selector)
            finally:
                path.write_text(original)
            status = {0: "NOT CAUGHT", 1: "caught"}.get(code, f"BROKEN({code})")
            print(f"{status:10} {what}")
            if code == 0:
                failures.append(f"{what}: tests still pass with the guard removed")
            elif code != 1:
                failures.append(f"{what}: pytest exited {code}, which is not a failing test")
    restored = subprocess.run(  # noqa: S603 - fixed arguments
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/unit"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if restored.returncode != 0:
        print("\n".join(restored.stdout.splitlines()[-30:]))
        failures.append("the suite does not pass after restoring the code")
    print(f"\n{len(MUTATIONS)} mutations, {len(failures)} problems")
    for f in failures:
        print(" -", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
