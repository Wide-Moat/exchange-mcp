"""The sign-in page an MCP client opens in the person's browser.

`password` mode: one form, login and password, posted to this server. The login is normalised
(`auth.login`), the attempt counted (`auth.limiter`), the password checked once against EWS
(`auth.password`), and the credential handed to the OAuth provider, which redirects the browser back to
the client with a one-time code. The password exists in this request and inside sealed tokens only.

`adfs` mode: the page is never shown; `/signin` sends the browser to AD FS (`auth.adfs`), and
`/adfs/callback` finishes there.

The page loads nothing from outside this server: fonts are served from /static, and the
Content-Security-Policy says so to the browser. It is never cached and never framed.
"""

from __future__ import annotations

import html
import logging
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from exchange_mcp.auth.limiter import Limited, Limiter, network_of
from exchange_mcp.auth.login import BadLogin, normalise
from exchange_mcp.auth.password import SignInFailed, check
from exchange_mcp.core.context import runtime
from exchange_mcp.core.errors import ExchangeError

log = logging.getLogger("exchange_mcp.signin")

STATIC = Path(__file__).resolve().parent / "static"
_STATIC_TYPES = {".woff2": "font/woff2", ".css": "text/css; charset=utf-8", ".txt": "text/plain; charset=utf-8"}
MAX_PASSWORD = 256


class SignIn:
    def __init__(self, provider, adfs=None) -> None:
        self.provider = provider
        self.adfs = adfs  # auth.adfs.Adfs in adfs mode
        s = runtime().settings
        self.limiter = Limiter(s.attempts_per_login, s.attempts_per_network)

    async def page(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        rid = request.query_params.get("request", "")
        p = await self.provider.sign_in_request(rid)
        if p is None:
            return _page(lang, heading=T[lang]["h1_again"], error=T[lang]["expired"], status=410)
        if self.adfs is not None:
            return RedirectResponse(await self.adfs.authorize_url(rid), status_code=302)
        return _page(lang, form=_form(lang, rid), redirect=_origin(p))

    async def submit(self, request: Request) -> Response:
        lang = pick_lang(request.headers.get("accept-language", ""))
        t = T[lang]
        if self.adfs is not None:
            return _page(lang, heading=t["h1_again"], error=t["expired"], status=405)
        form = await request.form()
        rid = str(form.get("request", ""))
        p = await self.provider.sign_in_request(rid)
        if p is None:
            return _page(lang, heading=t["h1_again"], error=t["expired"], status=410)
        typed = str(form.get("login", ""))
        password = str(form.get("password", ""))
        again = _form(lang, rid, typed)
        s = runtime().settings
        try:
            login = normalise(typed, s.login_format, s.login_domain, s.login_also)
        except BadLogin:
            login = None
        if login is None or not password or len(password) > MAX_PASSWORD:
            # Same answer as a wrong password: the page never says whether a login exists.
            return _page(lang, form=again, error=t["wrong"], status=400, redirect=_origin(p))
        try:
            self.limiter.take(login=login, network=network_of(request.client.host if request.client else None))
        except Limited:
            log.info("sign-in refused: attempt limit")
            return _page(lang, form=again, error=t["limited"], status=429, redirect=_origin(p))
        try:
            cred = await check(runtime().http, s.ews_url, login, password)
        except SignInFailed:
            log.info("sign-in refused: not accepted")
            return _page(lang, form=again, error=t["wrong"], status=400, redirect=_origin(p))
        except ExchangeError as e:
            log.info("sign-in failed: %s", e.key)
            return _page(lang, form=again, error=t["unavailable"], status=502, redirect=_origin(p))
        try:
            redirect = await self.provider.complete_sign_in(rid, cred)
        except LookupError:
            return _page(lang, heading=t["h1_again"], error=t["expired"], status=410)
        log.info("sign-in completed")
        return RedirectResponse(redirect, status_code=302)


def _origin(p: dict) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(p["redirect_uri"])
    return f"{u.scheme}://{u.netloc}"


async def static(request: Request) -> Response:
    """Fonts and their licences, from the package. Nothing outside `static/` is reachable."""
    rel = request.path_params.get("path", "")
    target = (STATIC / rel).resolve()
    if not target.is_relative_to(STATIC) or not target.is_file() or target.suffix not in _STATIC_TYPES:
        return Response("Not found", status_code=404)
    return FileResponse(
        target, media_type=_STATIC_TYPES[target.suffix], headers={"Cache-Control": "public, max-age=604800, immutable"}
    )


# ---------------------------------------------------------------- language

T: dict[str, dict[str, str]] = {
    "en": {
        "title": "Sign in to Exchange",
        "description": "Sign in to your work mailbox to connect it to your AI app.",
        "h1": "Sign in to your mailbox",
        "lede": "Use the login and password you use for work e-mail.",
        "login": "Login",
        "login_hint": "name, DOMAIN\\name or name@domain",
        "password": "Password",
        "connect": "Connect",
        "note": (
            "This connector keeps your password encrypted inside your app's connection and uses it only to reach "
            "your mailbox. Mail goes out only after you confirm it."
        ),
        "wrong": "The login or password was not accepted.",
        "limited": "Too many attempts. Wait 15 minutes before trying again.",
        "unavailable": "Exchange did not answer. Try again in a few minutes.",
        "h1_again": "Connect again",
        "expired": "This sign-in link has expired or was already used. Connect again from your AI app.",
        "unknown_client": (
            "This server no longer knows your AI app's connection: the server was updated or its key changed. "
            "In your AI app, clear the authorization for this connector and connect again."
        ),
    },
    "ru": {
        "title": "Вход в почту Exchange",
        "description": "Войдите в рабочую почту, чтобы подключить её к ИИ-приложению.",
        "h1": "Вход в рабочую почту",
        "lede": "Логин и пароль те же, что для рабочей почты.",
        "login": "Логин",
        "login_hint": "имя, ДОМЕН\\имя или имя@домен",
        "password": "Пароль",
        "connect": "Подключить",
        "note": (
            "Коннектор хранит пароль зашифрованным внутри подключения вашего приложения и использует его только "
            "для доступа к вашей почте. Письма уходят только после вашего подтверждения."
        ),
        "wrong": "Логин или пароль не подошли.",
        "limited": "Слишком много попыток. Подождите 15 минут и попробуйте снова.",
        "unavailable": "Exchange не ответил. Попробуйте через несколько минут.",
        "h1_again": "Подключитесь заново",
        "expired": "Ссылка для входа устарела или уже использована. Подключитесь заново из ИИ-приложения.",
        "unknown_client": (
            "Сервер больше не узнаёт подключение вашего ИИ-приложения: его обновили или сменили ключ. "
            "В ИИ-приложении сбросьте авторизацию этого коннектора и подключитесь заново."
        ),
    },
    "es": {
        "title": "Iniciar sesión en Exchange",
        "description": "Inicie sesión en su correo de trabajo para conectarlo a su aplicación de IA.",
        "h1": "Iniciar sesión en el correo",
        "lede": "Use el usuario y la contraseña de su correo de trabajo.",
        "login": "Usuario",
        "login_hint": "nombre, DOMINIO\\nombre o nombre@dominio",
        "password": "Contraseña",
        "connect": "Conectar",
        "note": (
            "Este conector guarda su contraseña cifrada dentro de la conexión de su aplicación y solo la usa para "
            "acceder a su buzón. Los correos se envían solo después de que usted lo confirme."
        ),
        "wrong": "El usuario o la contraseña no se aceptaron.",
        "limited": "Demasiados intentos. Espere 15 minutos antes de volver a intentarlo.",
        "unavailable": "Exchange no respondió. Inténtelo de nuevo en unos minutos.",
        "h1_again": "Vuelva a conectar",
        "expired": "Este enlace de inicio de sesión caducó o ya se usó. Vuelva a conectar desde su aplicación de IA.",
        "unknown_client": (
            "Este servidor ya no reconoce la conexión de su aplicación de IA: el servidor se actualizó o cambió su "
            "clave. En su aplicación de IA, borre la autorización de este conector y vuelva a conectar."
        ),
    },
    "de": {
        "title": "Bei Exchange anmelden",
        "description": "Melden Sie sich bei Ihrem dienstlichen Postfach an, um es mit Ihrer KI-App zu verbinden.",
        "h1": "Am Postfach anmelden",
        "lede": "Verwenden Sie Benutzername und Kennwort Ihrer dienstlichen E-Mail.",
        "login": "Benutzername",
        "login_hint": "name, DOMÄNE\\name oder name@domäne",
        "password": "Kennwort",
        "connect": "Verbinden",
        "note": (
            "Dieser Connector speichert Ihr Kennwort verschlüsselt in der Verbindung Ihrer App und nutzt es nur für "
            "den Zugriff auf Ihr Postfach. E-Mails werden erst nach Ihrer Bestätigung gesendet."
        ),
        "wrong": "Benutzername oder Kennwort wurden nicht akzeptiert.",
        "limited": "Zu viele Versuche. Warten Sie 15 Minuten, bevor Sie es erneut versuchen.",
        "unavailable": "Exchange hat nicht geantwortet. Versuchen Sie es in einigen Minuten erneut.",
        "h1_again": "Erneut verbinden",
        "expired": "Dieser Anmeldelink ist abgelaufen oder wurde bereits verwendet. Verbinden Sie sich erneut aus "
        "Ihrer KI-App.",
        "unknown_client": (
            "Dieser Server erkennt die Verbindung Ihrer KI-App nicht mehr: Der Server wurde aktualisiert oder sein "
            "Schlüssel geändert. Entfernen Sie in Ihrer KI-App die Autorisierung dieses Connectors und verbinden "
            "Sie sich erneut."
        ),
    },
    "fr": {
        "title": "Connexion à Exchange",
        "description": "Connectez-vous à votre messagerie professionnelle pour la relier à votre application d'IA.",
        "h1": "Connexion à la messagerie",
        "lede": "Utilisez l'identifiant et le mot de passe de votre messagerie professionnelle.",
        "login": "Identifiant",
        "login_hint": "nom, DOMAINE\\nom ou nom@domaine",
        "password": "Mot de passe",
        "connect": "Se connecter",
        "note": (
            "Ce connecteur conserve votre mot de passe chiffré dans la connexion de votre application et ne "
            "l'utilise que pour accéder à votre boîte aux lettres. Les messages ne partent qu'après votre "
            "confirmation."
        ),
        "wrong": "L'identifiant ou le mot de passe n'a pas été accepté.",
        "limited": "Trop de tentatives. Attendez 15 minutes avant de réessayer.",
        "unavailable": "Exchange n'a pas répondu. Réessayez dans quelques minutes.",
        "h1_again": "Reconnectez-vous",
        "expired": "Ce lien de connexion a expiré ou a déjà été utilisé. Reconnectez-vous depuis votre application "
        "d'IA.",
        "unknown_client": (
            "Ce serveur ne reconnaît plus la connexion de votre application d'IA : le serveur a été mis à jour ou "
            "sa clé a changé. Dans votre application d'IA, supprimez l'autorisation de ce connecteur et "
            "reconnectez-vous."
        ),
    },
}


def pick_lang(accept: str) -> str:
    """The first of ru and en the browser asks for, by quality; English when it names neither."""
    best, best_q = "en", -1.0
    for part in accept.split(",")[:20]:
        tag, _, params = part.strip().partition(";")
        base = tag.strip().lower().split("-")[0]
        if base not in T:
            continue
        q = 1.0
        if params.strip().startswith("q="):
            try:
                q = float(params.strip()[2:])
            except ValueError:
                q = 0.0
        if q > 0 and q > best_q:
            best, best_q = base, q
    return best


# ---------------------------------------------------------------- HTML


def _form(lang: str, rid: str, login: str = "") -> str:
    t = T[lang]
    return f"""
  <p class="lede">{t["lede"]}</p>
  <form method="post" action="/signin" novalidate>
    <input type="hidden" name="request" value="{html.escape(rid)}">
    <label for="login">{t["login"]}</label>
    <input id="login" name="login" value="{html.escape(login)}" autocomplete="username" spellcheck="false"
           autocapitalize="off" aria-describedby="login-hint" required autofocus>
    <p class="hint" id="login-hint">{html.escape(t["login_hint"])}</p>
    <label for="password">{t["password"]}</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">{t["connect"]}</button>
  </form>
  <p class="note">{t["note"]}</p>"""


_STYLE = """
:root{
--paper:#f5f5f4;--surface:#ffffff;--ink:#0c0c0d;--ink-2:#2c2c2e;--muted:#5f5f63;--line:#e3e3e4;--line-2:#cfcfd1;
--accent:#f95801;--accent-ink:#b83e00;--on-ink:#fafaf9;--error-bg:#fff1e8;--error-ink:#8a2f00;--dot:rgba(12,12,13,.10);
--focus:#f95801;
--display:'Space Grotesk',ui-sans-serif,system-ui,sans-serif;
--body:'Instrument Sans',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:20px;--space-6:24px;--space-8:32px;--space-10:40px;
--r-card:20px;--r-field:10px;--ease:cubic-bezier(.2,.7,.2,1);color-scheme:light}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;overflow-x:clip}
body{background-color:var(--paper);background-image:radial-gradient(var(--dot) 1px,transparent 1.2px);
background-size:24px 24px;color:var(--ink);font:16px/1.55 var(--body);-webkit-font-smoothing:antialiased}
.wrap{max-width:440px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-6);
padding:max(var(--space-8),env(safe-area-inset-top)) max(var(--space-4),env(safe-area-inset-right)) var(--space-10)
max(var(--space-4),env(safe-area-inset-left))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:var(--space-8);
display:grid;gap:var(--space-4)}
h1{font:600 32px/1.08 var(--display);letter-spacing:-.025em;margin:0;text-wrap:balance;overflow-wrap:break-word;min-width:0}
.lede{margin:0;color:var(--ink-2)}
.note{margin:0;font-size:14px;color:var(--muted)}
form{display:grid;grid-template-columns:minmax(0,1fr);gap:var(--space-2)}
label{font-size:14px;font-weight:600;color:var(--ink);margin-top:var(--space-2)}
input{width:100%;min-width:0;font:inherit;font-size:16px;color:var(--ink);background:var(--surface);border:1px solid var(--line-2);
border-radius:var(--r-field);padding:var(--space-3) var(--space-4);transition:border-color .15s var(--ease)}
input:hover{border-color:var(--muted)}
input:focus-visible,input:focus{outline:2px solid var(--focus);outline-offset:1px;border-color:var(--ink)}
.hint{margin:0;font-size:14px;color:var(--muted)}
button{margin-top:var(--space-4);font:600 16px/1 var(--body);color:var(--on-ink);background:var(--ink);border:0;
border-radius:var(--r-field);padding:var(--space-4) var(--space-5);cursor:pointer;transition:background-color .15s var(--ease)}
button:hover{background:var(--accent-ink)}
button:active{background:var(--ink-2)}
button:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
a{color:var(--accent-ink);text-underline-offset:3px}
.error{margin:0;border-radius:var(--r-field);padding:var(--space-3) var(--space-4);background:var(--error-bg);
color:var(--error-ink);font-size:15px}
@media (max-width:420px){.card{padding:var(--space-6)}h1{font-size:28px}}
@media (prefers-reduced-motion:reduce){input,button{transition:none}}
"""


def _page(
    lang: str,
    *,
    form: str = "",
    redirect: str | None = None,
    error: str | None = None,
    status: int = 200,
    heading: str | None = None,
) -> HTMLResponse:
    t = T[lang]
    err = f'<p class="error" id="error" role="alert">{html.escape(error)}</p>' if error else ""
    body = f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="description" content="{t["description"]}">
<title>{t["title"]}</title>
<link rel="stylesheet" href="/static/fonts/fonts.css"><style>{_STYLE}</style></head>
<body><main class="wrap">
<section class="card">
  <h1>{heading or t["h1"]}</h1>{err}{form}</section>
</main></body></html>"""
    actions = "'self'" + (f" {redirect}" if redirect else "")  # Chrome applies form-action to the redirect too
    csp = (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; "
        f"form-action {actions}; frame-ancestors 'none'; base-uri 'none'"
    )
    return HTMLResponse(
        body,
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": csp,
            "X-Content-Type-Options": "nosniff",
            "Content-Language": lang,
            "Vary": "Accept-Language",
        },
    )


class AuthorizeErrorPage:
    """ASGI wrapper: the one answer `/authorize` gives straight to the browser (an error it cannot send back
    to an unknown client) becomes a page in the person's language instead of JSON."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].rstrip("/") != "/authorize":
            await self.app(scope, receive, send)
            return
        replaced = False

        async def guard(message) -> None:
            nonlocal replaced
            if message["type"] == "http.response.start" and message["status"] == 400:
                replaced = True
                lang = pick_lang(_header(scope, b"accept-language"))
                t = T[lang]
                page = _page(lang, heading=t["h1_again"], error=t["unknown_client"], status=400)
                await page(scope, receive, send)
                return
            if not replaced:
                await send(message)

        await self.app(scope, receive, guard)


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode("latin-1")
    return ""
