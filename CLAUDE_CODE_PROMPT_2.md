# DUYS Boost — Supplement Prompt (Part 2)

This is a continuation of the main `CLAUDE_CODE_PROMPT.md` already in this repo.
Read that file first for architecture, DB split rules, and env vars.
This prompt covers additional bugs and features not in Part 1.

---

## Architecture Reminder (Critical)

- `get_db()` → global.db (users, posts, follows, channels, groups, stories, etc.)
- `get_user_db()` → current user's personal `{uid}.db` (notifications, messages, transactions, withdrawals, tips, subscriptions)
- `_open_personal_db(uid)` → any user's personal DB (for cross-user writes)
- **Never query a personal table via `get_db()` or vice versa.**

---

## Additional Bugs to Fix

### Bug A — Post-card JS not globally available
**Problem:** `togglePostMenu`, `openReplyModal`, `openRepostModal`, `toggleLike`, `toggleBookmark`, `deletePost`, `openEditPost`, `copyPostLink`, `reportPost`, and the IntersectionObserver for view counting are all defined inside a `<script>` block in `feed.html`. They are undefined on `post_detail.html`, `bookmarks.html`, `profile.html`, and `hashtag_feed.html` — so the `⋯` menu, like button, bookmark button, and view counter silently do nothing on those pages.
**Fix:** Move all of those functions into `static/js/main.js`. Remove them from `feed.html`. Test that every page that renders `post_card.html` still works.

### Bug B — Google OAuth crashes on missing credentials
**Problem:** `blueprints/auth.py` calls `oauth.register(...)` at module level. If `GOOGLE_CLIENT_ID` is not set, `authlib` raises an error that prevents the entire auth blueprint from loading, taking down login and signup.
**Fix:** Wrap the `oauth.register` call in a conditional: only register if both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set. Hide the "Continue with Google" button in `auth.html` using a template variable `google_oauth_enabled`.

### Bug C — Typing indicator never clears in DMs
**Problem:** `set_typing` writes `is_typing=1` to the DB but there is no TTL, timeout, or cleanup. If a user closes the tab while typing, the other participant sees "typing…" indefinitely.
**Fix:** Add a `typing_updated_at` timestamp column to conversations. In the `is_typing` SSE poll endpoint, only emit `is_typing=true` if `typing_updated_at` is within the last 5 seconds. The client already calls `set_typing` on keydown — also call it with `typing=false` on blur and `beforeunload`.

### Bug D — SSE reconnect storm on server wake-up
**Problem:** Render free tier spins down after 15 minutes. When the server wakes, all connected `EventSource` clients reconnect simultaneously (default 3s retry). This floods the server before it is ready.
**Fix:** In the JS that opens the SSE connection, add randomised exponential backoff on the `onerror` handler: `setTimeout(() => reconnect(), 1000 + Math.random() * 4000 * attempt)`. Cap at 30 seconds. Set `reconnectDelay` to 15000ms in the SSE response headers: `retry: 15000\n\n`.

### Bug E — Channel post_count drifts
**Problem:** `channels.post_count` is updated on post create/delete but the migration default is 0. Channels created before the column was added show 0 regardless of actual post count.
**Fix:** After running the `ALTER TABLE channels ADD COLUMN post_count INTEGER DEFAULT 0` migration, add a one-time recalculation: `UPDATE channels SET post_count = (SELECT COUNT(*) FROM channel_posts WHERE channel_id = channels.id)`. Run this inside `run_schema_migrations()` guarded by checking if any channel has `post_count = 0` but `channel_posts` rows exist.

### Bug F — Story ring shows "Your story" when user has no story
**Problem:** The stories bar always renders the current user's "Your story" ring with a `+` button regardless of whether they have an active story.
**Fix:** In `stories_feed()`, include the current user's own stories in the response. In `stories_bar.html` JS, check if the current user appears in the feed response. If yes, render their ring with the seen/unseen state. If no, render the `+` (add story) button. Never render both simultaneously.

### Bug G — Admin withdrawals always empty
**Problem:** `admin_withdrawals()` queries `withdrawals` via `get_db()` (global DB). Withdrawals live in each user's personal DB. Admin always sees an empty list.
**Fix:** Add a `global_withdrawals` mirror table to `global.db`. When a user submits a withdrawal in `wallet.py → withdraw()`, also insert a summary row into `global_withdrawals(id, user_id, amount, method, network, account, status, created_at)`. Admin reads from `global_withdrawals` joined to `users`. When admin approves/rejects, update both `global_withdrawals` and the user's personal DB withdrawal record via `_open_personal_db(user_id)`.

### Bug H — Mention notifications never fire
**Problem:** `create_post()` saves the post body but never parses `@username` mentions. Users are never notified when mentioned.
**Fix:** After inserting the post, run a regex `re.findall(r'@(\w+)', body)` on the body. For each matched username, look up the user in global DB, call `add_notification(db, mentioned_uid, f'@{author_username} mentioned you', icon='mention', link=f'/post/{post_id}')`. Cap at 5 mentions per post to prevent spam.

### Bug I — Post edit history not stored
**Problem:** `posts.edited_at` is updated when a post is edited, but the old body is discarded. There is no way to see what was changed.
**Fix:** Create a `post_edits(id, post_id, body, edited_at)` table in `GLOBAL_SCHEMA`. Before updating the post body in `edit_post()`, insert the current body into `post_edits`. Add a "View edit history" option to the post menu that opens a modal listing previous versions.

### Bug J — Username change cooldown not shown in UI
**Problem:** `username_changes` and `username_last_changed` columns exist and the backend enforces the cooldown, but the edit profile form shows no indication of how many changes remain or when the next change is allowed.
**Fix:** In `edit_profile()` route, pass `username_changes_left = max(0, 3 - user['username_changes'])` and `username_next_change = user['username_last_changed']` to the template. In `edit_profile.html`, show a small note under the username field: "2 changes remaining" or "Next change available on YYYY-MM-DD".

---

## Features to Implement

Work through these in order. Implement one at a time, run the app, confirm it works, then proceed.

---

### Feature 1 — Move all post-card JS to main.js (prerequisite for everything else)
This must be done first. It unblocks likes, bookmarks, the options menu, and view counts on all pages.

Move from `feed.html` into `static/js/main.js`:
- `toggleLike(postId, btn)`
- `toggleBookmark(postId, btn)`
- `togglePostMenu(postId, e)` — keep the `position:fixed` implementation from the current `feed.html`
- `copyPostLink(postId)`
- `deletePost(postId)`
- `openEditPost(postId, body)`
- `openReplyModal(postId, username)`
- `openRepostModal(postId, isReposted)`
- `reportPost(postId)`
- The IntersectionObserver setup (view counting)
- `sharePost(postId, body)` — already in `main.js`, confirm no duplicate in `feed.html`

After moving, remove them from `feed.html` and verify every page still works.

---

### Feature 2 — Hashtag & @mention autocomplete in compose
**Hashtag autocomplete:**
- Listen for `#` keypress in the compose textarea
- Debounce 300ms then fetch `/api/trending/tags?q=<typed>`
- Show a floating dropdown below the cursor with up to 6 matches
- Click/tap inserts the tag and closes the dropdown

**Mention autocomplete:**
- Listen for `@` keypress
- Fetch `/api/search/users?q=<typed>` (already exists — check its response shape)
- Show avatar + username in the dropdown
- Insert `@username` on select

**Linkification:**
- In `format_post()` in `helpers.py`, after setting `p['body']`, run a regex to wrap bare URLs and `@username` mentions in `<a>` tags
- Store as `p['body_html']` (safe-escaped HTML)
- In `post_card.html` and `post_detail.html`, render `{{ p.body_html | safe }}` instead of `{{ p.body }}`

---

### Feature 3 — Draft auto-save in compose
- Every 5 seconds, if the compose textarea has content, write it to `localStorage` under the key `compose_draft`
- On page load, if a draft exists, show a dismissable banner: "You have an unsaved draft — [Restore] [Discard]"
- Restore fills the textarea; Discard deletes the key
- Clear the draft on successful post submission

---

### Feature 4 — Pinned posts on profile
**Backend:**
- Add `is_pinned INTEGER DEFAULT 0` to `posts` in `REQUIRED_COLUMNS` migration (already in schema, confirm migration runs)
- Add `POST /post/<id>/pin` route that toggles `is_pinned` — only post owner can pin, only one post can be pinned at a time (unpin others first)
- In `profile()` route, fetch pinned post separately and prepend it to the posts list

**Frontend:**
- Add "Pin to profile" / "Unpin" to the `⋯` options menu in `post_card.html` (show only on own profile)
- Show a 📌 pin indicator on the pinned post card

---

### Feature 5 — Message read receipts
**Backend:**
- `messages.is_read` column already exists
- Add `POST /api/messages/<conversation_id>/read` route that marks all messages in the conversation as `is_read=1` where `sender_id != current_uid`
- The route returns `{'ok': true}`

**Frontend:**
- In `message_thread.html`, call the mark-read endpoint when the thread opens and when a new SSE message arrives
- Show a single tick (sent) vs double tick (read) on each message bubble
- Use CSS: single grey tick for unread by recipient, double blue tick for read

---

### Feature 6 — Notification preferences
**Backend:**
- Add `notif_prefs TEXT DEFAULT '{}'` column to `users` via `REQUIRED_COLUMNS`
- Add `POST /api/settings/notifications` route that saves a JSON object of preferences: `{"likes": true, "follows": true, "mentions": true, "dms": true, "boosts": true, "tips": true, "system": true}`
- In `add_notification()`, before inserting, check the target user's `notif_prefs` — skip if that notification type is disabled

**Frontend:**
- Add a "Notification settings" section to the settings/edit profile page
- Toggle switches for each type, saved via AJAX on change

---

### Feature 7 — Link previews in posts
**Backend:**
- Add `og_title TEXT, og_description TEXT, og_image TEXT, og_url TEXT` columns to `posts` via `REQUIRED_COLUMNS`
- In `create_post()`, after saving the post, check if the body contains a URL (regex). If yes, spawn a background thread to fetch the URL's Open Graph tags using `requests` + `BeautifulSoup`. Update the post row with the OG data.
- In `format_post()`, include these fields in the returned dict

**Frontend:**
- In `post_card.html`, after the post body, add a conditional block: if `p.og_url` is set, render a link preview card (thumbnail image left, title + description right, domain URL footer)
- Style it like Twitter's link cards: rounded border, muted background

---

### Feature 8 — Content warnings / sensitive media
**Backend:**
- Add `is_sensitive INTEGER DEFAULT 0` column to `posts` via `REQUIRED_COLUMNS`
- Add a "Mark as sensitive" checkbox to the compose form
- In `create_post()`, save `is_sensitive` from the form
- Pass `is_sensitive` through `format_post()`

**Frontend:**
- In `post_card.html`, if `p.is_sensitive` is true and the post has media, wrap the media in a blurred overlay div with a "Tap to reveal" button
- The button toggles a class that removes the blur
- Add a user setting `auto_show_sensitive INTEGER DEFAULT 0` — if enabled, skip the blur

---

### Feature 9 — Deposit QR codes
**Backend:**
- No backend change needed — the platform wallet addresses are already displayed on the deposit page

**Frontend:**
- Add `qrcodejs` or `qrcode-svg` (available from cdnjs.cloudflare.com) to `deposit.html`
- For each network's wallet address, render a QR code below the copy-able address
- Add a "Save QR" button that triggers a canvas download

---

### Feature 10 — Infinite scroll on feed
**Replace** the "Load more" button at the bottom of `feed.html` with IntersectionObserver-based infinite scroll:
- Observe a sentinel `<div id="feed-sentinel">` placed after the last post
- When it enters the viewport, fetch the next page: `GET /feed?page=N&tab=<tab>` with `X-Requested-With: fetch` header
- The server should detect this header and return JSON `{posts: [...], has_more: bool}` instead of full HTML
- Append new post cards to the feed container using the existing `prependPost` / card-building logic (but appending, not prepending)
- Remove the sentinel and re-add it after the new posts if `has_more` is true

---

### Feature 11 — Health check endpoint
Add to `app.py`:
```python
@app.route('/health')
def health():
    from db import _global_synced
    return jsonify({'ok': True, 'db_synced': _global_synced}), 200
```
This lets Render's health check confirm the app is running and the DB is loaded.

---

### Feature 12 — Compress responses
Add `flask-compress` to `requirements.txt`. In `app.py` factory:
```python
from flask_compress import Compress
Compress(app)
```
Configure: `app.config['COMPRESS_MIMETYPES'] = ['text/html','text/css','application/javascript','application/json']`
`app.config['COMPRESS_MIN_SIZE'] = 500`

---

### Feature 13 — Lazy-load images
In `post_card.html`, `profile.html`, `explore.html`, `messages.html`, and anywhere an `<img>` tag appears for avatars or post media:
- Add `loading="lazy"` attribute to all `<img>` tags that are not above the fold
- Add `decoding="async"` as well
- For the stories bar avatars, add `loading="eager"` since they are always above the fold

---

### Feature 14 — Two-factor authentication (2FA)
**Backend:**
- Add `pip install pyotp qrcode[pil]` to `requirements.txt`
- Add `totp_secret TEXT, totp_enabled INTEGER DEFAULT 0` columns to `users` via `REQUIRED_COLUMNS`
- Add routes in `auth.py`:
  - `GET /settings/2fa` — show QR code for setup (generate secret, store temporarily in session)
  - `POST /settings/2fa/enable` — verify the 6-digit code, save `totp_secret` and `totp_enabled=1`
  - `POST /settings/2fa/disable` — verify code, set `totp_enabled=0`, clear `totp_secret`
- In the login route, after password check: if `totp_enabled=1`, redirect to a 2FA verification step before setting `session['user_id']`

**Frontend:**
- Add a 2FA section to the security/settings page
- Show setup QR code in a modal
- Show a 6-digit input for verification

---

### Feature 15 — Login history
**Backend:**
- Create `login_history(id, user_id, ip_address, user_agent, created_at)` table in `GLOBAL_SCHEMA`
- In the login route (both password and OAuth), after setting `session['user_id']`, insert a row into `login_history`
- Add `GET /settings/security` route that returns the last 10 login records for the current user

**Frontend:**
- Render a table in the security settings page: Date, IP, Device (parsed from user agent), Location (optional — use a free IP geolocation API)
- Flag the current session's row as "Current session"

---

### Feature 16 — Verified badge application flow
**Backend:**
- Create `verification_requests(id, user_id, reason, evidence_url, status, reviewed_by, reviewed_at, created_at)` table in `GLOBAL_SCHEMA`
- Add `POST /verify/apply` route — saves the request (one active request per user allowed)
- In `admin.py`, add `/admin/verifications` route listing pending requests with approve/reject actions
- On approve: set `users.is_verified=1` and `users.verified_tier='blue'` (default), add notification

**Frontend:**
- Add an "Apply for verification" card in the profile edit page (shown only if not already verified and no pending request)
- Simple form: reason for request (dropdown: public figure / brand / creator / journalist) + supporting URL
- Admin verification queue page with approve / reject buttons and a tier selector (blue/gold/grey)

---

### Feature 17 — Group invite links
**Backend:**
- Create `group_invites(id, group_id, token, created_by, expires_at, uses, max_uses)` table in `GLOBAL_SCHEMA`
- Add `POST /group/<slug>/invite/create` — generates a random token, saves it
- Add `GET /join/<token>` — validates token, adds current user to the group, redirects to group page
- Token expires after 7 days or after `max_uses` redemptions

**Frontend:**
- In `group_detail.html` (members can see this if `show_invite_link` setting is on), add an "Invite link" section for group admins
- Show the link with a copy button and a "Regenerate" option
- Show uses remaining

---

## Code Quality Tasks

### CQ-1 — Add pytest tests
Create `tests/` directory with:
- `test_auth.py` — signup, login, logout, duplicate username
- `test_posts.py` — create post, delete post, like/unlike, format_post with None author
- `test_notifications.py` — add_notification writes to personal DB not global DB
- `test_db_routing.py` — assert that notifications/messages/transactions are never queried from get_db()
- `test_storage.py` — _public_url_base() never raises, returns a string in all env var combinations

Use `pytest-flask` and mock R2 calls with `unittest.mock.patch`.

### CQ-2 — Add Content Security Policy headers
In `app.py` after creating the app, add:
```python
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response
```
For CSP: the app uses many inline scripts, so start with `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'`. This can be tightened later by moving inline scripts to `main.js` (Feature 1 above) and using nonces.

### CQ-3 — Database connection teardown
Verify `get_db()` and `get_user_db()` connections are properly closed via `flask.g` teardown. In `app.py`:
```python
@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()
    udb = g.pop('udb', None)
    if udb is not None:
        udb_conn.close()
```
Confirm no "database is locked" errors in logs under concurrent requests.

### CQ-4 — Response compression
See Feature 12 above.

### CQ-5 — Lazy loading images
See Feature 13 above.

---

## Suggested Order of Work

1. **Bug A** — move all JS to `main.js` (unblocks everything on non-feed pages)
2. **Feature 11** — health check (2 minutes, unblocks Render zero-downtime deploys)
3. **Feature 12 + 13** — compression + lazy images (easy wins, big performance impact)
4. **Bug G** — admin withdrawals (business-critical, users can't get paid otherwise)
5. **Bug H** — mention notifications (core social feature)
6. **Feature 2** — hashtag/mention autocomplete (high engagement impact)
7. **Feature 5** — message read receipts (messaging completeness)
8. **Feature 10** — infinite scroll (feed UX)
9. **Bug C + D** — typing indicator cleanup + SSE backoff (stability)
10. **Feature 7** — link previews (content richness)
11. **Feature 16** — verification badge applications (trust system)
12. **Feature 14** — 2FA (security)
13. **Feature 15** — login history (security)
14. **Feature 4** — pinned posts (creator tools)
15. **Feature 8** — sensitive content warnings (safety)
16. **Feature 6** — notification preferences (user control)
17. **Feature 9** — deposit QR codes (crypto UX)
18. **Feature 17** — group invite links (groups growth)
19. **Feature 3** — draft auto-save (compose UX)
20. **CQ-1 through CQ-5** — tests and security headers (ongoing, do alongside features)
21. **Remaining bugs B, E, F, I, J** — lower severity, fix during relevant feature work
