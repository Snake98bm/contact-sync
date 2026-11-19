# contact-sync

A small self-hosted Docker container that keeps **Microsoft 365 (Outlook/Exchange) contacts** and
**Google Contacts** in step, after cleaning them up first.

> **Status: 0.13.** Backup, a one-time cleanup you review and approve, and two-way sync are all
> working. Nothing is written until **write mode** is switched on in Settings, and every change keeps
> a copy first (contact history, recycle bin), so it can be undone. Only the contact calls listed in
> `app/net.py` can write; everything else is refused in code.

## What you get

- A web page behind **passkey or password** sign-in, over HTTPS with the container's own certificate authority:
  sign-in for both accounts, contacts, cleanup, sync, recycle bin, backups, settings.
- **Cleanup** (build a plan, tick what you want, apply). Nothing changes until you press Apply.
  - **Tidy:** "Last, First" names, phone formatting, lowercase emails, notes and repeated addresses
    left behind by older sync tools. Any row can instead send that contact (both accounts) to the recycle bin.
  - **Combine** duplicates and link the copies in each account.
  - **Shared phones and emails:** decide per household; you can mark a number as rightly shared.
  - **Fix addresses:**
    - split an address typed into the street box into street, city, state and ZIP
    - fill a missing state from the ZIP
    - look up a missing ZIP or city: first the US Census Bureau geocoder, then OpenStreetMap
      (only the street line, city and state are sent; see [PRIVACY.md](PRIVACY.md))
    - correct a wrong ZIP or a jumbled or misspelled street
    - offer the ZIP your other contacts in a one-ZIP town have
    - treat the same address typed two ways as one
  - Unsure fixes are shown in yellow to check. Every address can be edited, removed, or its whole
    contact sent to the recycle bin.
  - Before applying, the contacts being changed are backed up. Up to 10 contacts are changed at once.
- **Two-way sync** every hour: the newest edit wins field by field, with a conflict log. Deletes
  carry over to the other account and go to the recycle bin (kept 2 years, photos included). More
  than 10 deletes in one run wait for your OK.
- **Nightly backup** of both sides: raw API data (`.json.gz`, what a restore uses), photos, and a
  `.vcf` per side that any contacts app can import. Kept 30 days by default.
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
docker run --rm --entrypoint sh local/contact-sync:0.13.11 -c "python /tests/test_clean.py && python /tests/test_model.py && python /tests/test_webauthn.py && python /tests/test_access.py && python /tests/test_writer.py && python /tests/test_sync.py && python /tests/test_cleanup.py && python /tests/test_addr.py && python /tests/test_pages.py"
```

Tests use made-up contacts only.

## License

MIT. See [PRIVACY.md](PRIVACY.md) for what happens to your data (it stays on your server).
