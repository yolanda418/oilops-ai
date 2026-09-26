# Deploying the Public Demo to Streamlit Community Cloud

This document is the step-by-step you follow in the browser at
share.streamlit.io. **No code changes are needed before this step.** All
configuration required by Streamlit Cloud is already committed:

* `public_demo.py` - the public-only entrypoint (forces mock AI,
  disables uploads, session-scoped storage).
* `.streamlit/config.toml` - theme, `enableCORS`, `headless`, etc.
* `runtime.txt` - pins Python 3.11.
* `requirements.txt` - already declares `streamlit>=1.30`, `pypdf`,
  `reportlab`, `python-dotenv`, `pytest`.

## One-time setup (do this once per Streamlit account)

1. Sign in at <https://share.streamlit.io> with the GitHub account
   that owns `yolanda418/oilops-ai`. Streamlit Cloud will ask for the
   usual GitHub OAuth permission to read your repos.
2. Optionally 2FA / TOTP as your account requires.

## Per-app deploy

1. From the Streamlit Cloud workspace, click **"New app"**.
2. Fill in the four required fields exactly:

   | Field            | Value                  |
   | ---------------- | ---------------------- |
   | Repository       | `yolanda418/oilops-ai` |
   | Branch           | `main`                 |
   | Main file path   | `public_demo.py`       |
   | Python version   | `3.11`                 |

3. **Advanced settings ...** - leave Secrets empty. There are no
   secrets for this demo. The `OPENAI_API_KEY` is intentionally not
   provided; the app strips it from the environment at import time and
   forces the deterministic mock classifier.
4. Click **"Deploy"**. Streamlit Cloud will install the requirements,
   apply `runtime.txt`, run `streamlit run public_demo.py`, and
   publish a public HTTPS URL of the form
   `https://yolanda418-oilops-ai-public-demo-<hash>.streamlit.app/`.
5. First boot takes 1-3 minutes. Watch the logs for
   `You can now view your Streamlit app in your browser.`.

## After deploy - what to verify (unauthenticated browser)

1. Open the public URL in a private/incognito window (no login cookies).
2. The yellow **"Public Demo - Synthetic Data Only"** banner is visible.
3. **Dashboard** page shows 3 seeded invoices (one already paid).
4. **Process sample invoices** page lets you pick from the 15 committed
   synthetic PDFs and click **Process**. The intake queue updates with
   the rows, all classified by `mock` (no real LLM call).
5. **Invoice tracker** lists the rows; Approve / Reject / Mark paid
   buttons work and the changes persist for the duration of the
   browser session only.
6. **Weekly summary** returns a deterministic mock narrative.
7. **CSV export** downloads a `public_demo_tracker.csv` containing only
   synthetic vendor names; the download button never exposes real
   identifiers.
8. Refresh the tab. The session id in the sidebar stays stable; a new
   browser session gets a different id and starts with the 3 seeded
   rows again.
9. Open a second incognito window to the same URL. Confirm that
   Approving an invoice in window A does NOT affect window B (per-
   session isolation).
10. View source on any page; confirm there is no `sk-`, no `.env`
    content, and no absolute local path leaked into the page.

## Limits and disclaimers

* **No real money.** The Mark-paid button is a tracking flag only. No
  bank integration exists.
* **Synthetic data only.** Vendor names are tagged `(synthetic)` and
  invoice numbers are explicitly synthetic (`DEMO-PPR-001` etc.).
* **Not a production system.** The repo's README still lists this as
  a single-user, local-only portfolio piece; the cloud build is a
  convenience for portfolio reviewers.

## Re-deploy / rollback

* Any push to `main` triggers an automatic rebuild. If the build
  fails, the previous deployment keeps serving until the new build
  passes.
* To roll back, push a follow-up commit that fixes the regression;
  Streamlit Cloud will redeploy with the new commit.
* To fully delete the cloud app, go to the app's settings page on
  share.streamlit.io and click "Delete app".