# contact-sync

A small self-hosted Docker container that keeps **Microsoft 365 (Outlook/Exchange) contacts** and
**Google Contacts** in step, after cleaning them up first.

> **Status: 0.1, read-only.** This version signs in to both accounts, takes a full backup and
> builds a cleanup report (duplicates, possible matches, junk, phone/name formatting, notes left
> behind by older sync tools). It **cannot change either account**: every request other than a
> read or a sign-in is refused in code (`app/net.py`). Cleanup and two-way sync come next.

## What you get

- A web page behind **passkey or password** sign-in, over HTTPS with the container's own certificate authority:
  sign-in for both accounts, backups, the cleanup report, settings.
- Nightly backup of both sides: raw API data (`.json.gz`, what a restore uses) plus a `.vcf` per side
  that any contacts app can import. Kept 30 days by default.
- An optional API key for scripts (`/api/status`, `/api/report`, `/api/backups`, `POST /api/run`).
- No dependencies beyond the Python standard library.

## Setup

### 1. Microsoft (Entra app registration)

1. [entra.microsoft.com](https://entra.microsoft.com) > **App registrations** > **New registration**.
   Name it, choose **Single tenant**, leave the redirect URI blank.
2. Copy the **Application (client) ID** into `MS_CLIENT_ID`, and your tenant id or domain into `MS_TENANT`.
3. **Authentication** > **Allow public client flows** = **Yes** (sign-in uses a device code; no secret).
4. **API permissions** > Microsoft Graph > Delegated: `Contacts.ReadWrite`, `offline_access`
   (`User.Read` is already there). Then **Grant admin consent**.

### 2. Google (Cloud project + OAuth client)

1. [console.cloud.google.com](https://console.cloud.google.com): new project, enable **People API**.
2. **Google Auth Platform** > Get started: External audience.
3. **Data access**: add the scope `https://www.googleapis.com/auth/contacts`.
4. **Branding**: a home page and privacy policy link (this repo and its `PRIVACY.md` are fine).
5. **Audience** > **Publish app** so it says *In production*. In *Testing*, Google ends the sign-in
   every 7 days. You'll see an "unverified app" warning when signing in; that's expected for your own app.
6. **Clients** > Create client > **Desktop app**. Put its id and secret in `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET`.

### 3. Run it

```sh
cp .env.example .env      # fill it in
mkdir -p data && sudo chown 1000:1000 data
docker compose up -d --build
```

1. Open `http://<server>:8080/cert`, download the certificate and install it as a trusted root on
   each device you'll use (steps for Windows, iPhone, Android and Mac are on that page).
2. Open `https://<name in TLS_NAMES>:8443`. Create a passkey or a password with the one-time **setup code**
   from the container log (`docker logs contact-sync`).
3. Sign in to each account from the dashboard:

- **Microsoft:** the page shows a code; enter it at microsoft.com/devicelogin.
- **Google:** open the link, sign in, allow contacts. The browser then lands on an
  `http://localhost/?state=…&code=…` address that doesn't load. Copy that address and paste it
  into the page. (The container can't receive Google's redirect itself.)

Then click **Back up and build report now**.

### Sign-in and HTTPS

- **Passkeys and/or a password.** The first one needs the setup code from the log; manage both (and
  more passkeys, one per device) under Settings. Locked out? Delete `data/auth.json` and restart; a new code is logged.
- **Private certificate authority.** On first start the container makes its own CA (10 years) and a
  site certificate from it (2 years, renewed automatically). The CA carries name constraints, so it
  can only vouch for the names in `TLS_NAMES`. Passkeys need a certificate the browser trusts,
  which is why it gets installed rather than clicked past. The site is always used at one name
  (`PUBLIC_HOST`, or the first dotted name in `TLS_NAMES`); other names redirect there.
- Plain HTTP only redirects, except `/healthz` and the certificate download. To start over with a
  new CA, delete `data/tls/` and restart (then install the new one).
- **API keys**: make and revoke named keys under Settings (stored only as a hash), or set `API_KEY`. Send
  `Authorization: Bearer <key>` to `/api/status`, `/api/report`, `/api/backups`, `/api/setup-code`, `POST /api/run`.

On a macvlan network the container can take its address by DHCP and register its name with your
router (`DHCP=1`, `DHCP_HOSTNAME`); see the comment at the top of `compose.yaml`.

## Tests

```sh
docker run --rm --entrypoint sh local/contact-sync:0.13.7 -c "python /tests/test_clean.py && python /tests/test_model.py && python /tests/test_webauthn.py && python /tests/test_access.py && python /tests/test_writer.py && python /tests/test_sync.py && python /tests/test_cleanup.py && python /tests/test_addr.py && python /tests/test_pages.py"
```

Tests use made-up contacts only.

## License

MIT. See [PRIVACY.md](PRIVACY.md) for what happens to your data (it stays on your server).
