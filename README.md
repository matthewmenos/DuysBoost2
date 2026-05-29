# DUYS Boost — Full Stack App (with Paystack Transfers)

A responsive social-media boost platform with ad campaigns, tasks marketplace,
wallet, referrals, and a complete Paystack integration for deposits and
mobile-money withdrawals in Ghana.

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Visit: http://localhost:5000


## Environment Variables

Set these before running in any real environment:

```bash
# Required
export FLASK_SECRET_KEY="<long-random-string>"
export COOKIE_SECURE=1          # enable when served over HTTPS

# Paystack (GHS deposits AND transfers)
export PAYSTACK_PUBLIC_KEY="pk_test_..."    # or pk_live_
export PAYSTACK_SECRET_KEY="sk_test_..."    # or sk_live_

# Optional OAuth
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."

# Runtime
export PORT=5000
export FLASK_DEBUG=0
```

Copy `.env.example` to `.env` for local development and do not commit `.env`.

This app loads environment variables from `.env` automatically using `python-dotenv`, so your Paystack secret key is kept out of source control.

> ⚠️ **Security note:** The previous version of this app shipped with
> hardcoded live Paystack keys. Those keys have been removed. If they were
> ever committed, **rotate them in your Paystack dashboard immediately.**

---

## Paystack Setup Guide

### 1. Get your keys

Log in to [dashboard.paystack.com](https://dashboard.paystack.com) → **Settings → API Keys & Webhooks**. You'll see both test keys (`pk_test_`, `sk_test_`) and live keys (`pk_live_`, `sk_live_`). Start with **test** keys while developing — switch to live only when ready for real money.

### 2. Enable Transfers

Transfers are **disabled by default** on new Paystack accounts. Go to **Settings → Preferences → Transfers** and turn them on. You may also need to:

- **Fund your Paystack balance** — transfers draw from your Paystack wallet, not from individual customer deposits. Top up via the dashboard before your first live transfer. Top-ups are free for Ghana/Nigeria businesses.
- **Consider disabling OTP mode** for transfers if you want fully automatic payouts. With OTP mode on, Paystack SMSes you an OTP for every transfer and you must call a second API endpoint to finalize it. Most production apps turn this off.

### 3. Configure the webhook

The webhook makes the app reliable — if a user closes their browser after paying, the webhook still updates their wallet. Similarly, transfer success/failure updates only arrive via webhook.

1. **Expose your server publicly.** For local development use [ngrok](https://ngrok.com):
   ```bash
   ngrok http 5000
   ```
   Copy the `https://abc123.ngrok-free.app` URL it gives you.

2. In Paystack dashboard: **Settings → API Keys & Webhooks** → set **Webhook URL** to:
   ```
   https://yourdomain.com/webhooks/paystack
   ```
   Paystack will POST there for every event. The app verifies the `x-paystack-signature` HMAC-SHA512 header using your secret key and rejects anything unsigned.

3. Test it: in the Paystack dashboard, there's a **"Send test webhook"** button that fires a sample `charge.success` event. Your server log should show a 200 response.

### 4. Test the full flow

With test keys, use Paystack's test card to deposit:

- Card: `4084 0840 8408 4081`
- Expiry: any future date
- CVV: any 3 digits
- OTP: `123456`

For transfers in test mode, Paystack simulates payouts without moving real money. The `transfer.success` webhook fires after a short delay.

---

## How Withdrawals Work

1. **User adds a payout account** on `/wallet`. The app collects their name, mobile money network (MTN / Telecel / AirtelTigo), and 10-digit Ghana phone number. It calls Paystack's `/transferrecipient` endpoint and saves the returned `recipient_code` on the user record.

2. **User requests a withdrawal.** The app validates the amount (minimum 1 GHS, can't exceed balance, recipient must be set), debits the balance, and creates a `pending` withdrawal row. No money has left Paystack yet.

3. **Admin approves the withdrawal** on `/admin`. The app calls Paystack's `/transfer` endpoint with an idempotent reference (`duys_wdr_<id>_<random>`), saves the returned `transfer_code`, and marks the withdrawal `processing`.

4. **Paystack sends the webhook** when the transfer completes. The app updates the withdrawal to `approved` (success) or `failed` (reversed — and refunds the user automatically).

If Paystack is not configured, step 3 falls back to manual approval with no money movement — useful for running the app locally without keys.

### Withdrawal status reference

| Status       | Meaning                                                                  |
|--------------|--------------------------------------------------------------------------|
| `pending`    | User submitted, awaiting admin approval                                  |
| `processing` | Admin approved, Paystack transfer in flight (waiting for webhook)        |
| `approved`   | Transfer succeeded; money arrived in user's mobile money wallet          |
| `failed`     | Transfer failed; user's balance was automatically refunded               |
| `rejected`   | Admin rejected manually; user's balance was refunded                     |

### Ghana Mobile Money provider codes

| Provider        | Paystack code | Notes                          |
|-----------------|---------------|--------------------------------|
| MTN MoMo        | `MTN`         | Most popular network           |
| Telecel Cash    | `VOD`         | Rebranded from Vodafone Cash   |
| AirtelTigo Money| `ATL`         |                                |

---

## What's New in This Version

### Paystack Transfers (this release)
- ✅ `POST /wallet/recipient` — register a MoMo account as a Paystack recipient
- ✅ `DELETE /wallet/recipient` — clear saved recipient
- ✅ Wallet page shows saved payout account + "Change"/"Remove" controls
- ✅ Withdrawal form requires a saved recipient, asks only for amount
- ✅ Admin approve now calls Paystack `/transfer` API with idempotent references
- ✅ `POST /webhooks/paystack` with HMAC-SHA512 signature verification
- ✅ Handles `charge.success`, `transfer.success`, `transfer.failed`, `transfer.reversed`
- ✅ Automatic refund when transfer fails
- ✅ Idempotent webhook handling (replays don't double-charge or double-refund)
- ✅ Phone number normalization (accepts `+233...`, `233...`, `0...` formats)

### Security (previous releases)
- ✅ No hardcoded keys — all secrets from env
- ✅ PBKDF2-SHA256 password hashing (with legacy SHA-256 auto-migration)
- ✅ Session rotation on login, `HttpOnly` + `SameSite=Lax` cookies
- ✅ `@admin_required` decorator for admin endpoints
- ✅ Input validation on signup, ads, wallet, admin endpoints

### Responsive design
- ✅ Mobile-first CSS at 360 / 480 / 520 / 560 / 640 / 900 / 1200px
- ✅ Off-canvas sidebar with backdrop on mobile, fixed on desktop
- ✅ Bottom tab bar on mobile (Home / Tasks / Ads / Wallet / Alerts)
- ✅ Tables scroll horizontally with touch handling
- ✅ Modals are bottom sheets on mobile, centered on desktop
- ✅ 44px+ tap targets, iOS-zoom-resistant font sizes
- ✅ `dvh`, safe-area-inset, `viewport-fit=cover`
- ✅ `prefers-reduced-motion` support

---

## File Structure

```
DuysBoost/
├── app.py                      # Flask app, routes, Paystack integration
├── requirements.txt
├── README.md
├── static/
│   ├── css/main.css            # Design system, responsive layout
│   └── js/main.js              # Sidebar, toasts, modals, notifications
└── templates/
    ├── base.html               # Shell (sidebar, topbar, bottom nav)
    ├── index.html              # Landing page (full-viewport)
    ├── auth.html               # Login + Signup (full-viewport)
    ├── dashboard.html
    ├── ads.html
    ├── tasks.html
    ├── wallet.html             # Deposits + MoMo recipient + withdrawals
    ├── referral.html
    ├── notifications.html
    ├── admin.html              # Admin w/ transfer controls
    └── error.html              # 404 / 500
```

## Production Checklist

Before going live:

- [ ] Rotate any previously-committed Paystack keys
- [ ] Set `FLASK_SECRET_KEY` to a long random value (persistent across restarts)
- [ ] Set `COOKIE_SECURE=1` and serve over HTTPS
- [ ] Set `FLASK_DEBUG=0`
- [ ] Change the default admin password (`admin123`)
- [ ] Configure the webhook URL in Paystack dashboard
- [ ] Fund your Paystack balance for transfers
- [ ] Switch to live Paystack keys
- [ ] Use a single gunicorn worker to avoid global.db write contention
- [ ] Test end-to-end with a small real deposit + withdrawal
- [ ] Security headers (X-Frame-Options, CSP, Referrer-Policy) — enabled by default
- [ ] Configure VAPID keys for web push notifications (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`)
- [ ] Set `BREVO_API_KEY` for transactional email (password reset)
- [ ] Review verification badge applications at `/admin/verifications`
- [ ] Notification preferences available at `/edit-profile` (Notification Preferences section)

## Testing Without Paystack

The app works without Paystack keys for local development:

- Deposits show a warning and are disabled
- The admin panel shows a "Paystack not configured" banner
- Admin approve still works but just marks the withdrawal approved without moving money (useful for UI testing)
- Webhook endpoint rejects all unsigned requests with 401

## Quick Reference — Webhook Events

| Event                 | Effect                                                      |
|-----------------------|-------------------------------------------------------------|
| `charge.success`      | Credits deposit (idempotent, skipped if already processed)  |
| `transfer.success`    | Marks withdrawal `approved`, notifies user                  |
| `transfer.failed`     | Marks withdrawal `failed`, refunds balance, notifies user   |
| `transfer.reversed`   | Same as failed — refunds balance                            |
| anything else         | 200 OK, ignored                                             |
