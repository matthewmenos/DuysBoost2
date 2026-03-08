# DUYS Boost Algorithm — Full Stack App

## Quick Start

```bash
pip install -r requirements.txt
python app.py
```

Visit: http://localhost:5000


## Features Implemented
- ✅ Login & Signup (with referral code support)
- ✅ Add & Withdraw Funds (wallet system)
- ✅ Dashboard with stats and live activity feed
- ✅ Ad Campaign posting (platform, task type, budget, reward/task)
- ✅ Task Completion with proof link submission
- ✅ Auto-management of followers gained/left
- ✅ Ad boosting engine (auto-pay + auto-complete on goal)
- ✅ Auto pay for task completed (instant wallet credit)
- ✅ Verification by proof link
- ✅ Admin dashboard (deposit, approve/reject withdrawals, user management)
- ✅ SQLite database (swap to PostgreSQL for production)
- ✅ Responsive mobile + desktop design
- ✅ Real-time notifications (polling every 30s)
- ✅ Light & Dark mode (per-user, persisted)
- ✅ Password validation & error handling
- ✅ Referral system ($1.00 per signup reward)
- ✅ Live activity feed (auto-refreshes every 10s)

## File Structure
```
duys_boost/
├── app.py              # Flask app + all routes + models
├── requirements.txt
├── templates/
│   ├── base.html       # Layout, sidebar, CSS design system
│   ├── index.html      # Landing page
│   ├── auth.html       # Login + Signup
│   ├── dashboard.html  # Main dashboard
│   ├── ads.html        # Ad campaign management
│   ├── tasks.html      # Task browsing + submission
│   ├── wallet.html     # Deposits, withdrawals, history
│   ├── referral.html   # Referral program
│   ├── notifications.html
│   └── admin.html      # Admin panel (subdomain-ready)
```


