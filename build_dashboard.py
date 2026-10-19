"""
build_dashboard.py

Converts static_dashboard/<slug>.html (mock-data mockup, provided per
project) into dashboard.html (API-connected live version, generated,
gitignored).

This file is a SCAFFOLD, not a working script — the actual mock-data ->
live-data replacement steps depend entirely on the specific mockup's JS
structure, which doesn't exist yet in this template. What IS reusable
as-is: the Cognito auth-gate injection, the `_replace_once` helper (fails
loudly with a clear message if an anchor isn't found, instead of silently
shipping stale JS), and the overall script structure.

Contract this script's injected AUTH_SCRIPT assumes your rewired mockup
JS provides:
  - a JS function `_isLocalDev()` — true for file://, localhost, 127.0.0.1
    (checking only 'localhost' misses 127.0.0.1 and sends local testers
    into a real, likely-undeployed Cognito redirect)
  - a JS function `initDashboard()` — called once, after the auth check
    passes, to fetch the initial data and render the page

See CLAUDE.md's "Frontend build script" section for the typical sequence
of replacement steps (mock arrays -> live globals, null-safety patches,
Apply-button rewrite, init-block rewrite) before filling in the TODOs below.

Dev mode:
    python build_dashboard.py
    python -m uvicorn api.main:app --reload
    # Open dashboard.html in browser
"""

import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# TODO: rename <slug> to this project's slug (matches infra/variables.tf's
# project_slug and the static_dashboard filename you were given).
SRC = Path(__file__).parent / "static_dashboard" / "<slug>.html"
DEST = Path(__file__).parent / "dashboard.html"

# Cognito hosted-UI auth. After the CloudFront function redirects an
# unauthenticated viewer to the Cognito /login page, Cognito redirects back
# to the dashboard with a ?code=. The browser must exchange that code for
# tokens and drop an id_token cookie — otherwise the CloudFront function
# (which only checks for that cookie) bounces the viewer straight back to
# login. redirect_uri must match the CloudFront function exactly:
# window.location.origin (no trailing slash).
#
# Both of these are placeholders until the first `terraform apply` — copy
# the real values from `terraform output` (cognito_client_id) once the
# Cognito resources exist, then rebuild. This is a two-pass deploy; see
# CLAUDE.md.
COGNITO_DOMAIN = "https://teg-<slug>.auth.us-east-2.amazoncognito.com"
COGNITO_CLIENT = "REPLACE_AFTER_TERRAFORM_APPLY"

AUTH_SCRIPT = f"""\
<script>
// Cognito auth ────────────────────────────────────────────────────────────────
const COGNITO_DOMAIN   = '{COGNITO_DOMAIN}';
const COGNITO_CLIENT   = '{COGNITO_CLIENT}';
const COGNITO_REDIRECT = window.location.origin;  // must match the CloudFront function's redirect_uri

async function _handleAuth() {{
  // Local dev (file://, localhost, or 127.0.0.1) skips auth entirely.
  if (_isLocalDev()) return true;

  const params = new URLSearchParams(window.location.search);
  const code = params.get('code');

  if (code) {{
    const resp = await fetch(COGNITO_DOMAIN + '/oauth2/token', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
      body: new URLSearchParams({{
        grant_type:   'authorization_code',
        client_id:    COGNITO_CLIENT,
        code:         code,
        redirect_uri: COGNITO_REDIRECT,
      }}),
    }});
    if (resp.ok) {{
      const tokens = await resp.json();
      const exp = new Date(Date.now() + tokens.expires_in * 1000).toUTCString();
      document.cookie = 'id_token=' + tokens.id_token + '; expires=' + exp + '; path=/; SameSite=Lax';
      window.history.replaceState({{}}, '', '/');  // strip ?code= from the URL
      return true;
    }}
    console.error('Token exchange failed', await resp.text());
    return false;
  }}

  const hasCookie = document.cookie.split(';').some(c => c.trim().startsWith('id_token='));
  if (!hasCookie) {{
    window.location.href = COGNITO_DOMAIN + '/login?client_id=' + COGNITO_CLIENT +
      '&response_type=code&scope=openid+email+profile' +
      '&redirect_uri=' + encodeURIComponent(COGNITO_REDIRECT);
    return false;  // navigating away
  }}
  return true;
}}

async function _bootAfterAuth() {{
  const authed = await _handleAuth();
  if (!authed) return;
  initDashboard();
}}

document.addEventListener('DOMContentLoaded', _bootAfterAuth);
</script>"""


def _replace_once(html: str, pattern: str, replacement: str, label: str, flags=re.DOTALL) -> str:
    new_html, n = re.subn(pattern, replacement, html, count=1, flags=flags)
    if n == 0:
        sys.exit(f"ERROR: could not find anchor for step '{label}' — has the mockup's structure changed?")
    return new_html


def main() -> None:
    if not SRC.exists():
        sys.exit(f"Source not found: {SRC}\n"
                  f"Drop the mockup you were given at static_dashboard/<slug>.html "
                  f"and rename SRC above to match.")

    html = SRC.read_text(encoding="utf-8")
    steps: list[str] = []

    # TODO: the mock-data -> live-data replacement steps. Typical sequence
    # (see CLAUDE.md "Frontend build script"):
    #
    #   1. Mock data arrays/functions -> `let` globals populated by fetch.
    #      Include an `_isLocalDev()` helper here (see module docstring).
    #   2. Null-safety patches for any calc that can divide by zero.
    #   3. Any ranking/sorting the live API now does server-side -> render
    #      the server's result instead of re-deriving it client-side.
    #   4. The Apply-button handler -> build real query params, fetch live
    #      endpoints, surface fetch failures instead of failing silently.
    #   5. The init block -> fetch filter options + initial data, exposed
    #      as a function named exactly `initDashboard()` (the injected
    #      AUTH_SCRIPT below calls it after the auth check passes).
    #
    # Each step should look like:
    #
    # html = _replace_once(
    #     html,
    #     r"<anchor regex matching the mockup's current mock-data block>",
    #     """<replacement JS>""",
    #     "step label",
    # )
    # steps.append("Rewired:   <what changed>")

    # Cognito auth gate — reusable as-is, always the last step.
    if "</body>" not in html:
        sys.exit("ERROR: could not find </body> — is this the right source file?")
    html = html.replace("</body>", AUTH_SCRIPT + "\n</body>", 1)
    steps.append("Injected:  Cognito auth-gate script before </body>")

    DEST.write_text(html, encoding="utf-8")

    src_kb = SRC.stat().st_size // 1024
    dest_kb = DEST.stat().st_size // 1024

    print(f"\nSource:  {SRC.name}  ({src_kb} KB)")
    print(f"Output:  {DEST.name}  ({dest_kb} KB)")
    print()
    for s in steps:
        print(f"  {s}")
    print()
    print("Next steps:")
    print("  1. cp .env.example .env  # fill in Snowflake credentials")
    print("  2. pip install -r requirements.txt")
    print("  3. python -m uvicorn api.main:app --reload")
    print(f"  4. Open {DEST.name} in your browser (or serve via: python -m http.server 8080)")
    print()
    print("  After the first `terraform apply`, copy the real Cognito client id from")
    print("  `terraform output cognito_client_id` into COGNITO_CLIENT at the top of")
    print("  this script and rebuild.")


if __name__ == "__main__":
    main()
