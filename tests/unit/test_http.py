"""The OAuth endpoints over HTTP: PKCE is checked by the token endpoint."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from test_signin import RESOLVED, Flow, _pkce


async def test_a_wrong_pkce_verifier_is_refused(ews):
    ews.on("ResolveNames", RESOLVED)
    f = Flow(ews)
    cid = await f.register()
    verifier, challenge = _pkce()
    rid = await f.request_id(cid, challenge)
    r = await f.submit(rid, "alex", "pw-not-in-logs-1")
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://client.example.org/cb",
        "client_id": cid,
    }
    wrong = await f.client.post("/token", data={**form, "code_verifier": "w" * 64})
    assert wrong.status_code == 400 and "access_token" not in wrong.text
    right = await f.client.post("/token", data={**form, "code_verifier": verifier})
    assert right.status_code == 200 and right.json()["access_token"]
