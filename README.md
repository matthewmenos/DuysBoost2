# DUYS Boost — Full Stack App (Enhanced)

A responsive social-media boost platform with ad campaigns, tasks marketplace,
wallet, referrals, and admin panel.

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Visit: http://localhost:5000

Default admin login: `admin` / `admin123` — **change this immediately.**

## Environment Variables

Set these before running in any real environment:

```bash
# Required in production
export FLASK_SECRET_KEY="<long-random-string>"
export COOKIE_SECURE=1          # enable when served over HTTPS

# Paystack (GHS deposits)
export PAYSTACK_PUBLIC_KEY="pk_..."
export PAYSTACK_SECRET_KEY="sk_..."

# Optional OAuth providers
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
export APPLE_CLIENT_ID="..."
export APPLE_CLIENT_SECRET="..."

# Runtime
export PORT=5000
export FLASK_DEBUG=0            # 0 in production
```

> ⚠️ **Security note:** The previous version of this app contained hardcoded
> live Paystack keys. Those keys have been removed from source. If they were
> ever committed, **rotate them in your Paystack dashboard immediately.**

## What's New in This Enhanced Version

### Security
- ✅ Paystack keys read from env only — no hardcoded secrets
- ✅ Session cookies: `HttpOnly`, `SameSite=Lax`, `Secure` in prod
- ✅ Passwords hashed with salted PBKDF2-SHA256 (legacy SHA-256 auto-upgraded)
- ✅ `FLASK_SECRET_KEY` from env with random fallback
- ✅ Session rotation on login (`session.clear()` before auth)
- ✅ Proper 403 on admin endpoints via `@admin_required` decorator
- ✅ Input validation and length limits across signup/ad/wallet routes

### Bug fixes
- ✅ Fixed `success: True` on error path in `create_ad`
- ✅ Replaced deprecated `datetime.utcnow()` with timezone-aware variant
- ✅ Guard against division-by-zero on progress bars
- ✅ Deposit double-credit guard now scoped per user
- ✅ Available-ads computed in SQL (single query, not Python filter)
- ✅ DB indexes on hot columns

### Responsive design — fully mobile-first
- ✅ Off-canvas sidebar with backdrop on mobile, fixed on desktop ≥ 900px
- ✅ New **bottom tab bar** on mobile (Home, Tasks, Ads, Wallet, Alerts)
- ✅ Stats grids scale: 1 col → 2 col → 4 col
- ✅ Tables scroll horizontally with proper touch handling
- ✅ Modals become bottom sheets on mobile, centered dialogs on desktop
- ✅ Forms stack on narrow screens, grid on wider
- ✅ Tap targets ≥ 44px, iOS-friendly 15px+ input font size (no zoom)
- ✅ Supports `dvh` / `env(safe-area-inset-bottom)` for notched devices
- ✅ `prefers-reduced-motion` respected

### UX polish
- ✅ Native share API for referral link on mobile, clipboard fallback
- ✅ Password strength meter on signup
- ✅ `?ref=` query param pre-fills referral code on signup
- ✅ Proper focus-visible rings for keyboard users
- ✅ Skip-to-content link
- ✅ `aria-label`s on icon-only buttons
- ✅ Close modals / sidebar with ESC
- ✅ Empty states with icons and CTAs
- ✅ Error pages (404 / 500)
- ✅ Network-error toasts on all fetch calls
- ✅ Disabled deposit button when Paystack is not configured

### Code quality
- ✅ CSS moved to `static/css/main.css` (single source of truth)
- ✅ Shared JS moved to `static/js/main.js`
- ✅ No per-template `<style>` soup
- ✅ Templates ~40 % shorter on average

## File Structure

```
DuysBoost/
├── app.py                      # Flask app, routes, models
├── requirements.txt
├── README.md
├── static/
│   ├── css/main.css            # Design system, responsive layout
│   └── js/main.js              # Sidebar, toasts, notifications, theme
└── templates/
    ├── base.html               # Shell: sidebar + topbar + bottom nav
    ├── index.html              # Landing page
    ├── auth.html               # Login + Signup
    ├── dashboard.html          # Main dashboard
    ├── ads.html                # Ad campaign management
    ├── tasks.html              # Task marketplace
    ├── wallet.html             # Deposits, withdrawals, history
    ├── referral.html           # Referral program
    ├── notifications.html      # Notification list
    ├── admin.html              # Admin panel
    └── error.html              # 404 / 500
```

## Breakpoints

| Width       | Layout                                                |
|-------------|-------------------------------------------------------|
| ≤ 360px     | Extra-compact spacing, stats and ad meta stack        |
| ≤ 480px     | Stats 1-col, forms stack, bottom nav shown            |
| ≤ 900px     | Off-canvas sidebar, mobile bottom nav                 |
| ≥ 900px     | Sticky sidebar (248px), topbar, no bottom nav         |
| ≥ 1200px    | Wider page padding                                    |
