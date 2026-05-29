# DUYS Boost — Claude Code Debugging & Feature Brief

## What This App Is

**DUYS Boost** is a social media + crypto earnings platform deployed at `duys-boost.onrender.com`.  
Think Twitter/X but users earn crypto (USDT) by engaging with boosted posts.

**Stack:** Flask (Python 3.14), Jinja2 SSR, SQLite, Cloudflare R2 (media + DB sync), Gunicorn on Render.  
**Start command:** `python -m gunicorn 'app:create_app()' --workers 1 --timeout 120 --bind 0.0.0.0:$PORT`

---

## Architecture — Critical to Understand First

### Dual-Database System (One User, One Database)

This is the most important architectural fact. There are **two database types**:

**1. `global.db`** — single shared database synced to/from R2 on startup.  
Contains: `users`, `posts`, `follows`, `post_likes`, `bookmarks`, `post_views`, `hashtags`, `post_hashtags`, `poll_options`, `poll_votes`, `ads`, `task_completions`, `post_boosts`, `boost_engagements`, `channels`, `channel_members`, `channel_posts`, `groups`, `group_members`, `group_messages`, `stories`, `reports`, `user_bans`, `platform_reviews`, `admin_audit_log`, `search_history`

**2. `{uid}.db`** — one SQLite file per user, stored in R2 as `user_dbs/{uid}.db`.  
Contains: `notifications`, `conversations`, `messages`, `transactions`, `withdrawals`, `crypto_deposits`, `subscription_tiers`, `subscriptions`, `tips`

**In code:**
- `get_db()` → global database connection
- `get_user_db()` → current user's personal database (requires active session)
- `_open_personal_db(uid)` → opens any user's personal DB directly (for cross-user writes like `add_notification`)

**Any query to a personal table (`notifications`, `messages`, `transactions`, etc.) MUST use `get_user_db()` or `_open_personal_db(uid)`, NEVER `get_db()`.**

### Key Files
```
app.py              — Flask factory, error handlers, inject_user context processor
db.py               — GLOBAL_SCHEMA, PERSONAL_SCHEMA, REQUIRED_COLUMNS migrations,
                      get_db(), get_user_db(), _open_personal_db(), run_personal_migrations()
helpers.py          — format_post(), format_post_with_poll(), add_notification(),
                      login_required, get_current_user()
security.py         — CSRF, Flask-Limiter setup
storage.py          — R2 upload/delete, _public_url_base() (never raises)
crypto_engine.py    — On-chain USDT verify & send (BSC, Avalanche, Aptos)
sse.py              — Server-Sent Events for real-time feed, DMs, notifications
blueprints/
  social.py         — 100+ routes: feed, posts, profile, messages, groups, channels, search
  auth.py           — login, signup, Google OAuth
  auth_reset.py     — forgot-password PIN flow (Brevo email)
  boost.py          — Facebook-Ads-style boost system, tasks/earn, creator tools
  wallet.py         — wallet, deposits, withdrawals, notifications, referral
  stories.py        — stories CRUD, viewer tracking, cleanup thread
  admin.py          — admin dashboard, user/post/report management
static/
  css/main.css      — single stylesheet (~2500 lines)
  js/main.js        — global JS (sharePost, showToast, badge counts, etc.)
templates/
  base.html         — sidebar, bottom nav, notification bell, toast container
  feed.html         — main feed, compose box, WTF widget, search panel, story bar
  post_card.html    — reusable post macro (used everywhere)
  stories_bar.html  — story rings + viewer overlay
```

### Environment Variables Required
```
FLASK_SECRET_KEY, FLASK_DEBUG=0, FLASK_ENV=production
R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
R2_BUCKET_NAME      (media), R2_DB_BUCKET_NAME (sqlite files)
R2_PUBLIC_URL       (CDN URL for media — MUST be set or posts may fail)
ADMIN_USERNAME, ADMIN_EMAIL, ADMIN_PASSWORD
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET  (optional OAuth)
BREVO_API_KEY, BREVO_SENDER_EMAIL, BREVO_SENDER_NAME  (email)
BSC_RPC_URL, AVAX_RPC_URL, APTOS_RPC_URL  (crypto, optional — public fallbacks exist)
PLATFORM_WALLET_BSC, PLATFORM_WALLET_AVAX, PLATFORM_WALLET_APTOS  (receive deposits)
PLATFORM_PRIVATE_KEY_BSC, PLATFORM_PRIVATE_KEY_AVAX  (sign withdrawals)
```

---

## Known Bugs to Fix

### 1. Admin Withdrawals — Cross-DB Bug (Critical)
**File:** `blueprints/admin.py` — `admin_withdrawals()` and `process_withdrawal()`  
**Bug:** The route queries `db.execute("SELECT w.* FROM withdrawals w JOIN users u ...")` using the **global** DB. `withdrawals` is a personal-DB table. Result: admin always sees 0 withdrawals even when users have submitted them.  
**Fix needed:** Admin must iterate over all user personal DBs to aggregate withdrawals, OR maintain a withdrawal summary table in global.db that mirrors pending withdrawals. The latter is better for performance. When a user submits a withdrawal (`wallet.py → withdraw()`), also write a row to a global `pending_withdrawals` mirror table. Admin reads from the mirror; when processed, update both.

### 2. Post Options Menu Clips on Mobile (Partially Fixed)
**File:** `templates/feed.html` — `togglePostMenu()`  
**Status:** Fixed in the feed. But `post_detail.html`, `bookmarks.html`, `hashtag_feed.html`, and `profile.html` also render post cards — the `togglePostMenu` function defined in `feed.html` is **not available** on those pages because it's in a page-specific `<script>` block, not in `main.js`. The menu button renders but nothing happens on those pages.  
**Fix needed:** Move `togglePostMenu`, `copyPostLink`, `reportPost`, `deletePost`, `openEditPost`, `openRepostModal`, `openReplyModal`, `toggleLike`, `toggleBookmark` from `feed.html` into `static/js/main.js` so they work globally.

### 3. View Count Does Not Update the DOM on Non-Feed Pages
**File:** `templates/feed.html` — IntersectionObserver  
**Bug:** The IntersectionObserver that fires the view count API and updates `#view-count-{id}` is defined only in `feed.html`. Post cards on `post_detail.html`, `profile.html`, `bookmarks.html` etc. never record views or update the counter.  
**Fix needed:** Move the IntersectionObserver setup into `main.js` so it runs on every page that has `.post-card[data-post-id]` elements.

### 4. `add_notification()` Blocks on Cross-User Personal DB Writes
**File:** `helpers.py` — `add_notification()`  
**Bug:** When notifying a different user (e.g., user A likes user B's post), `add_notification()` calls `_download_personal_db(user_id)` → write → `_upload_personal_db(user_id)`. This is a **synchronous R2 download + upload** happening inside every like/follow/repost HTTP request. On Render free tier this adds 200-800ms per notification.  
**Fix needed:** Move notification writes to a background thread or queue. Or add a `pending_notifications` table in global.db that the SSE stream flushes into personal DBs lazily.

### 5. Post Detail Page Has No Options Menu JS
**File:** `templates/post_detail.html`  
**Bug:** Post detail renders `post_card.html` which has the `⋯` button calling `togglePostMenu()`, but that function is not available outside `feed.html`.  
**Fix:** See bug #2 above — move to `main.js`.

### 6. Group Message Send Route Is Registered Wrong
**File:** `blueprints/social.py`  
**Bug:** `@bp.route('/group/<slug>/poll')` is mapped to `group_send_message` — the route URL says "poll" but it's the send endpoint. The actual group poll endpoint conflicts. Check `grep "@bp.route.*group" blueprints/social.py`.

### 7. Stories Cleanup Thread Uses Hard-Coded Path
**File:** `blueprints/stories.py` — `_run_cleanup()`  
**Bug:** The cleanup thread opens `sqlite3.connect(global.db)` with a relative path. On Render, the working directory may differ between threads. Use `app.config['GLOBAL_DB_PATH']` or an absolute path from an env var.

### 8. Subscription/Locked Content Shows Lock But No Unlock Flow
**File:** `templates/post_card.html`, `blueprints/boost.py` — `subscribe()`  
**Bug:** Posts marked `is_subscriber_only=1` show as locked with a blurred preview, but there is no "Subscribe" button rendered on the card itself. Users can't discover how to unlock content from the feed.  
**Fix needed:** Add a subscribe CTA button to locked post cards.

### 9. No Pagination on Profile Posts
**File:** `blueprints/social.py` — `profile()`, `templates/profile.html`  
**Bug:** The profile route loads all posts in one query with no `LIMIT`/`OFFSET`. Prolific users will cause slow page loads or crashes.

### 10. `recalc_post_score()` Called on Every View — Performance
**File:** `blueprints/social.py` — `record_post_view()`  
After every view, `recalc_post_score(db, post_id)` recalculates the post's feed ranking score. This fires hundreds of times per minute on active posts. Move to a periodic background task or only recalculate every Nth view.

---

## Features to Add

### Priority 1 — Core Social

**A. Push Notifications (Web Push / PWA)**  
Register service worker, store VAPID subscription tokens in global.db (`push_subscriptions` table), send push on like/follow/mention/DM. Use `pywebpush` library.

**B. Post Scheduling**  
Add `scheduled_at` column to `posts` table. Add a datetime picker to the compose modal. Background thread checks for scheduled posts every minute and publishes them. Store scheduled posts with `status='scheduled'`.

**C. Poll Expiry**  
Add `expires_at` column to `post_boosts`/polls. Currently polls never close. Show a countdown and lock voting after expiry.

**D. Repost with Comment (Quote Tweet style)**  
Currently reposts are supported but quoting (repost + your own text) needs the UI to work end-to-end. The data model has `quote_of_id` but the compose flow doesn't surface it cleanly.

**E. Thread/Reply Chains**  
Currently replies show under a post but aren't displayed as threaded conversations. Add indented reply-to-reply display in `post_detail.html`.

**F. Post Reactions (Beyond Like)**  
Add emoji reaction system: 🔥❤️😂🎯💰 mapped to `reaction_type` column. Display reaction counts per type. This replaces the single heart with a reaction bar.

### Priority 2 — Monetisation

**G. Subscription Paywalls — Complete the Flow**  
The backend for subscriptions exists (`subscription_tiers`, `subscriptions` tables, `subscribe()` route). What's missing:
- Creator can set up tiers with prices in `creator_setup.html` (partially done)
- Subscriber-only post toggle in compose UI works but no paywall UI on locked posts
- Payment processing: deduct from subscriber wallet → credit creator wallet → write subscription row
- Subscription management page: list active subs, cancel button

**H. In-App Tips — Complete the Flow**  
`tips` table exists, `tip_post()` and `tip_user()` routes exist but:
- No tip button visible on post cards (only accessible if you know the URL)
- No tip amount selector modal
- No tipping history page for creators
Add a 💰 tip button to post cards (only visible on other users' posts), a tip modal, and a tips received widget on the profile/earnings page.

**I. Boost Reward Distribution**  
When a user earns from a boosted post engagement (`earn_engagement()`), the USDT reward is added to their wallet in the DB. But the actual on-chain settlement is not automated — admin must manually process bulk payouts. Add a scheduled task that batches earned rewards and triggers `crypto_engine.send_usdt()` daily above a minimum threshold (e.g. $1).

**J. Affiliate / Referral Tracking**  
The referral page exists (`/referral`) and `referred_by` column exists on users. But:
- Referral link sharing works
- Bonus is awarded at signup (`check_and_award_referral_bonus`)
- Missing: referral dashboard showing earnings per referral, click tracking, payout history

### Priority 3 — UX Polish

**K. Infinite Scroll**  
The feed currently has a "Load more" button. Replace with IntersectionObserver-based infinite scroll that fetches the next page automatically when the user nears the bottom.

**L. Image/Video Viewer Modal**  
Tapping a media attachment opens it inline (small). Add a full-screen lightbox modal with pinch-to-zoom on mobile.

**M. Draft Saving**  
Auto-save the compose box content to `localStorage` every 5 seconds. Show a "Restore draft?" banner when the user returns.

**N. Online Presence**  
`check_online` route exists and `show_online` column exists on users. Hook it up: show green dot on avatars in DMs and profile pages, update `last_seen_at` on every authenticated request via the context processor.

**O. Dark/Light Theme Toggle**  
The toggle exists and `data-theme` attribute switches CSS variables. But the preference isn't persisted server-side — it resets on page reload. Save to `users.theme` column via AJAX and read it in `inject_user()` context processor.

**P. Hashtag Autocomplete in Compose**  
When the user types `#` in the compose box, fetch suggestions from `/api/trending/tags` and show a dropdown. Insert the selected tag.

**Q. @Mention Autocomplete in Compose**  
When the user types `@`, fetch user suggestions from `/api/search/users?q=...` and show a dropdown. Insert the username. Parse `@username` in post body and link them in `format_post()`.

### Priority 4 — Admin Tools

**R. Admin Analytics Dashboard**  
Add charts to `admin/overview.html`: daily signups, daily posts, revenue (deposits - withdrawals), top earners, top posters. Use Chart.js (already imported).

**S. Admin Bulk Actions**  
Post list in `/admin/posts` — add checkboxes and "Delete selected" action. User list — add "Ban selected" bulk action.

**T. Content Moderation Queue**  
Reports are stored but there's no triage workflow. Add: report status (`new` → `reviewing` → `actioned`/`dismissed`), assignment to admin, notes field, action log.

---

## Code Quality / Tech Debt to Address

1. **No tests** — zero test coverage. Add `pytest` with at minimum: auth flow, post creation, notification routing to correct DB, format_post with None author.

2. **Rate limiter in-memory** — Flask-Limiter uses in-memory store (single Gunicorn worker). Fine for now but if you scale to multiple workers, switch to Redis: set `REDIS_URL` env var (code already checks for it in `security.py`).

3. **R2 DB sync is synchronous** — `global.db` is downloaded from R2 on first request and uploaded after every write. With many concurrent users this creates race conditions. Consider using SQLite WAL mode and accepting eventual consistency, or migrating to Turso/PlanetScale for a proper hosted SQLite.

4. **`static/js/main.js`** — Many JS functions that belong globally are still in `feed.html`'s `<script>` block. Complete the migration: `togglePostMenu`, `openReplyModal`, `openRepostModal`, `toggleLike`, `toggleBookmark`, `deletePost`, `openEditPost`, the IntersectionObserver for view counting.

5. **`post_card.html` is a macro** — It's used as a Jinja2 `{% macro render_post(p, current_user) %}` but also included as `{% include %}` in some places. Standardise to macro-only and ensure `current_user` is always explicitly passed.

6. **Missing `nl2br` and `linkify` filters** — `nl2br` is registered in `app.py` but `linkify` (convert bare URLs in post text to `<a>` tags) is not. Post bodies show raw URLs.

7. **No CSP headers** — Add `Content-Security-Policy` headers in `app.py` to prevent XSS. The app uses inline scripts extensively, so use nonces or switch to external script files.

---

## Deployment Notes

- Platform: **Render.com** free tier (spins down after 15 min inactivity)
- On cold start: R2 sync downloads `global.db` before first request is served
- Single Gunicorn worker (multi-worker is unsafe with the current R2-sync DB model)
- Media bucket: Cloudflare R2 — set `R2_PUBLIC_URL` to your `r2.dev` subdomain or custom domain. Without it, uploaded media will be stored but served from the private endpoint (inaccessible to users).
- `FLASK_DEBUG` must be `0` in production

