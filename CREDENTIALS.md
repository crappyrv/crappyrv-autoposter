# CREDENTIALS.md — how to obtain every secret

This project never creates credentials for you. You get them once, paste them
into `.env` (copy from `.env.example`), and the app reads them from there.

There are **three** providers. Work through them top to bottom. Each ends with the
exact `.env` keys it fills.

> Time budget: Dropbox ~10 min · YouTube ~20 min · Anthropic ~2 min.
>
> Facebook was removed from this poster (it was suppressing the FB algorithm),
> so there are no more Meta/Graph credentials to provision.

---

## 1. Anthropic (metadata generation)

Fills: `ANTHROPIC_API_KEY`

1. Go to <https://console.anthropic.com/> and sign in.
2. **Settings → API Keys → Create Key**.
3. Name it (e.g. `video-autoposter`), copy the `sk-ant-...` value immediately
   (it is shown only once).
4. Make sure the account/workspace has billing/credits enabled.

```
ANTHROPIC_API_KEY=sk-ant-...
```

---

## 2. Dropbox (watch / download / move)

Fills: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`

### 2a. Create a scoped app
1. Go to <https://www.dropbox.com/developers/apps> → **Create app**.
2. Choose API: **Scoped access**.
3. Access type: **Full Dropbox** (so it can read `/incoming` and move to
   `/posted` and `/failed`). *(App folder access also works if you put all three
   folders inside the app folder — Full Dropbox is simpler.)*
4. Name the app (e.g. `crappyrv-video-autoposter`) → **Create app**.

### 2b. Set scopes (DO THIS BEFORE minting the token)
On the app's **Permissions** tab, check:
- `files.metadata.read`
- `files.content.read`
- `files.content.write`

Click **Submit**. (Scopes must be set *before* you generate the token, or the
token won't carry them.)

### 2c. Grab the app key/secret
On the **Settings** tab, copy **App key** and **App secret**.

```
DROPBOX_APP_KEY=<App key>
DROPBOX_APP_SECRET=<App secret>
```

### 2d. Mint a long-lived REFRESH token (not a short-lived access token)
Dropbox access tokens expire in ~4 hours; we need a **refresh** token. Use the
OAuth code flow with `token_access_type=offline`:

1. In a browser, visit (replace `<APP_KEY>`):
   ```
   https://www.dropbox.com/oauth2/authorize?client_id=<APP_KEY>&response_type=code&token_access_type=offline
   ```
2. Click **Allow**. Dropbox shows a one-time **authorization code** — copy it.
3. Exchange the code for a refresh token (run in a terminal; replace the three
   placeholders):
   ```bash
   curl https://api.dropboxapi.com/oauth2/token \
     -d code=<AUTH_CODE> \
     -d grant_type=authorization_code \
     -u <APP_KEY>:<APP_SECRET>
   ```
4. The JSON response includes `"refresh_token": "..."`. That value is permanent
   (until you revoke it).

```
DROPBOX_REFRESH_TOKEN=<refresh_token from the JSON>
```

### 2e. Create the folders
In your Dropbox, create the three folders the app expects (match `config.yaml`):
`/incoming`, `/posted`, `/failed`.

---

## 3. YouTube (Google Cloud OAuth — Desktop client)

Fills: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`

### 3a. Project + API
1. Go to <https://console.cloud.google.com/> → create (or pick) a project.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.

### 3b. OAuth consent screen — and the 7-day trap
1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → fill app name, your support email, developer email.
3. **Scopes**: add `https://www.googleapis.com/auth/youtube.upload`.
4. **Test users**: add the Google account that owns the YouTube channel.
5. **CRITICAL — publish to production.** Click **PUBLISH APP** so the publishing
   status is **In production**, not **Testing**.
   - In **Testing** mode, Google **expires the refresh token after 7 days**, and
     your cron job will silently start failing a week later.
   - "In production" with a sensitive scope normally needs Google verification,
     but for a **single-user** app you own you can leave it unverified — you'll
     just see an "unverified app" warning during the one-time consent in step 3d
     (click *Advanced → Go to … (unsafe)*). The token then does **not** expire on
     the 7-day clock.

### 3c. OAuth client (Desktop)
1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Desktop app** → name it → **Create**.
3. Copy the **Client ID** and **Client secret**.

```
YOUTUBE_CLIENT_ID=<...>.apps.googleusercontent.com
YOUTUBE_CLIENT_SECRET=<...>
```

### 3d. Mint the refresh token
After `youtube_auth.py` is built (a later step), run it once:
```bash
python youtube_auth.py
```
It opens a browser, you approve with the channel-owner Google account, and it
prints the refresh token to paste here:
```
YOUTUBE_REFRESH_TOKEN=<...>
```

---

## Final check

1. `cp .env.example .env`
2. Fill in all keys above.
3. Verify the loader (no secret values are printed):
   ```bash
   python config.py
   ```
   You should see "Config loaded OK." and the non-secret settings. A missing key
   fails loud telling you exactly which one.
