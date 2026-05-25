"""blueprints/boost.py — ads, tasks, post boosts, earn, creator, analytics."""
import re
from datetime import datetime, timezone, timedelta
from flask import (
    Blueprint, jsonify, render_template,
    request, session, url_for, current_app
)
from helpers import (
    get_db, login_required, safe_float, safe_int,
    add_notification, add_transaction,
    check_and_award_referral_bonus, verify_task_completion,
    format_post, format_post_with_poll, recalc_post_score
)
from security import (
    limiter, csrf_exempt,
    LIMIT_TASK, LIMIT_AD, LIMIT_BOOST, LIMIT_TIP, LIMIT_SUBSCRIBE
)

bp = Blueprint('boost', __name__)


# ── Dashboard ────────────────────────────────────────────────────────────────

@bp.route('/dashboard')
@login_required
def dashboard():
    db  = get_db()
    uid = session['user_id']
    CURRENCY_SYMBOL        = current_app.config['CURRENCY_SYMBOL']
    WORKER_REWARD_PER_TASK = current_app.config['WORKER_REWARD_PER_TASK']
    LISTER_COST_PER_TASK   = current_app.config['LISTER_COST_PER_TASK']

    ads = [dict(a) for a in db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC LIMIT 5', (uid,)
    ).fetchall()]
    recent_tasks = db.execute(
        'SELECT tc.*, a.title as ad_title FROM task_completions tc '
        'JOIN ads a ON tc.ad_id=a.id WHERE tc.worker_id=? '
        'ORDER BY tc.submitted_at DESC LIMIT 5', (uid,)
    ).fetchall()
    total_earned = db.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="earn"', (uid,)
    ).fetchone()[0]
    total_spent = db.execute(
        'SELECT COALESCE(SUM(amount),0) FROM transactions WHERE user_id=? AND type="spend"', (uid,)
    ).fetchone()[0]
    unread = db.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0', (uid,)
    ).fetchone()[0]
    available_ads = [dict(a) for a in db.execute(
        'SELECT * FROM ads WHERE status="active" AND user_id!=? '
        'AND id NOT IN (SELECT ad_id FROM task_completions WHERE worker_id=?) '
        'AND budget_spent < budget ORDER BY created_at DESC LIMIT 10', (uid, uid)
    ).fetchall()]

    return render_template(
        'dashboard.html',
        ads=ads, recent_tasks=recent_tasks,
        total_earned=total_earned, total_spent=total_spent,
        unread=unread, available_ads=available_ads
    )


# ── Ads ──────────────────────────────────────────────────────────────────────

@bp.route('/ads')
@login_required
def ads():
    db = get_db()
    WORKER_REWARD_PER_TASK = current_app.config['WORKER_REWARD_PER_TASK']
    LISTER_COST_PER_TASK   = current_app.config['LISTER_COST_PER_TASK']
    user_ads = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC',
        (session['user_id'],)
    ).fetchall()
    return render_template('ads.html', ads=user_ads,
                           worker_reward=WORKER_REWARD_PER_TASK,
                           lister_cost=LISTER_COST_PER_TASK)


@bp.route('/ads/create', methods=['POST'])
@login_required
@limiter.limit(LIMIT_AD)
def create_ad():
    db  = get_db()
    uid = session['user_id']
    WORKER_REWARD_PER_TASK = current_app.config['WORKER_REWARD_PER_TASK']
    LISTER_COST_PER_TASK   = current_app.config['LISTER_COST_PER_TASK']
    user = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    title            = request.form.get('title', '').strip()
    platform         = request.form.get('platform', '').strip()
    task_type        = request.form.get('task_type', '').strip()
    target_url       = request.form.get('target_url', '').strip()
    followers_target = safe_int(request.form.get('followers_target'), 0)

    if not title or len(title) > 120:
        return jsonify({'success': False, 'error': 'Please enter a valid campaign title.'})
    if not platform or not task_type:
        return jsonify({'success': False, 'error': 'Please select a platform and task type.'})
    if not target_url.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'Please enter a valid target URL.'})
    if followers_target <= 0:
        return jsonify({'success': False, 'error': 'Please enter a valid followers target.'})

    budget = round(followers_target * LISTER_COST_PER_TASK, 2)
    if budget <= 0 or budget > user['balance']:
        return jsonify({'success': False,
                        'error': 'Insufficient balance for this followers target.'})

    ad_id = db.execute(
        'INSERT INTO ads (user_id,title,platform,target_url,task_type,'
        'reward_per_task,budget,followers_target) VALUES (?,?,?,?,?,?,?,?)',
        (uid, title, platform, target_url, task_type,
         WORKER_REWARD_PER_TASK, budget, followers_target)
    ).fetchone()['id']
    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (budget, uid))
    ad = db.execute('SELECT * FROM ads WHERE id=?', (ad_id,)).fetchone()
    add_transaction(db, uid, 'spend', budget, f'Budget for ad: {ad["title"]}')
    check_and_award_referral_bonus(db, uid)
    add_notification(db, uid, f'📢 Ad "{ad["title"]}" is now live!')

    users = db.execute('SELECT id FROM users WHERE id != ?', (uid,)).fetchall()
    for u in users:
        add_notification(db, u['id'],
                         f'📢 New task available: "{ad["title"]}" on {ad["platform"]}')
    db.commit()
    return jsonify({'success': True})


@bp.route('/ads/<int:ad_id>/toggle', methods=['POST'])
@login_required
def toggle_ad(ad_id):
    db = get_db()
    ad = db.execute('SELECT * FROM ads WHERE id=?', (ad_id,)).fetchone()
    if not ad or ad['user_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if ad['status'] == 'completed':
        return jsonify({'success': False, 'error': 'Campaign already completed.'})
    new_status = 'paused' if ad['status'] == 'active' else 'active'
    db.execute('UPDATE ads SET status=? WHERE id=?', (new_status, ad_id))
    db.commit()
    return jsonify({'success': True, 'status': new_status})


# ── Tasks ────────────────────────────────────────────────────────────────────

@bp.route('/tasks')
@login_required
def tasks():
    db  = get_db()
    uid = session['user_id']
    available = db.execute(
        'SELECT * FROM ads WHERE status="active" AND user_id!=? '
        'AND id NOT IN (SELECT ad_id FROM task_completions WHERE worker_id=?) '
        'AND budget_spent < budget ORDER BY created_at DESC',
        (uid, uid)
    ).fetchall()
    my_tasks = db.execute(
        'SELECT tc.*, a.title as ad_title FROM task_completions tc '
        'JOIN ads a ON tc.ad_id=a.id WHERE tc.worker_id=? '
        'ORDER BY tc.submitted_at DESC',
        (uid,)
    ).fetchall()
    return render_template('tasks.html', available=available, my_tasks=my_tasks)


@bp.route('/tasks/submit', methods=['POST'])
@login_required
@limiter.limit(LIMIT_TASK)
def submit_task():
    db  = get_db()
    uid = session['user_id']
    WORKER_REWARD_PER_TASK = current_app.config['WORKER_REWARD_PER_TASK']
    LISTER_COST_PER_TASK   = current_app.config['LISTER_COST_PER_TASK']
    CURRENCY_SYMBOL        = current_app.config['CURRENCY_SYMBOL']

    ad_id      = safe_int(request.form.get('ad_id'), 0)
    proof_link = request.form.get('proof_link', '').strip()

    ad = db.execute('SELECT * FROM ads WHERE id=?', (ad_id,)).fetchone()
    if not ad:
        return jsonify({'success': False, 'error': 'Ad not found.'})
    if ad['status'] != 'active':
        return jsonify({'success': False, 'error': 'This campaign is not active.'})
    if ad['user_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot complete your own ad.'})
    if db.execute('SELECT id FROM task_completions WHERE ad_id=? AND worker_id=?',
                  (ad_id, uid)).fetchone():
        return jsonify({'success': False, 'error': 'Already submitted for this ad.'})
    if not proof_link.startswith(('http://', 'https://')):
        return jsonify({'success': False, 'error': 'Please enter a valid proof URL.'})
    if ad['budget_spent'] + LISTER_COST_PER_TASK > ad['budget']:
        return jsonify({'success': False, 'error': 'This campaign has reached its budget.'})

    verification_result = verify_task_completion(ad, proof_link, uid)
    if not verification_result['valid']:
        return jsonify({'success': False, 'error': verification_result['error']})

    now    = datetime.now(timezone.utc).isoformat()
    reward = WORKER_REWARD_PER_TASK

    db.execute(
        'INSERT INTO task_completions (ad_id,worker_id,proof_link,status,reward,reviewed_at) '
        'VALUES (?,?,?,?,?,?)',
        (ad_id, uid, proof_link, 'completed', reward, now)
    )
    db.execute(
        'UPDATE ads SET budget_spent=budget_spent+?, followers_gained=followers_gained+1 WHERE id=?',
        (LISTER_COST_PER_TASK, ad_id)
    )
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (reward, uid))
    add_transaction(db, uid, 'earn', reward, f'Task completed: {ad["title"]}')
    add_notification(db, uid,
                     f'✅ Task completed! +{CURRENCY_SYMBOL}{reward:.2f} added to your wallet for "{ad["title"]}"')
    add_notification(db, ad['user_id'],
                     f'📈 New follower gained for "{ad["title"]}"!')
    db.commit()
    return jsonify({'success': True, 'message': f'Task completed! +{CURRENCY_SYMBOL}{reward:.2f} added to your wallet'})


# ── Post boosts ──────────────────────────────────────────────────────────────

@bp.route('/post/<int:post_id>/boost', methods=['POST'])
@login_required
@limiter.limit(LIMIT_BOOST)
def boost_post(post_id):
    db  = get_db()
    uid = session['user_id']
    WORKER_REWARD_PER_TASK = current_app.config['WORKER_REWARD_PER_TASK']
    LISTER_COST_PER_TASK   = current_app.config['LISTER_COST_PER_TASK']

    post = db.execute('SELECT * FROM posts WHERE id=? AND user_id=?', (post_id, uid)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found or not yours.'}), 404

    engage_type  = (request.form.get('engage_type') or 'like').strip().lower()
    target_count = safe_int(request.form.get('target_count'), 0)
    reward_each  = safe_float(request.form.get('reward_each'), WORKER_REWARD_PER_TASK)

    if engage_type not in ('like', 'follow', 'comment', 'share'):
        return jsonify({'success': False, 'error': 'Invalid engagement type.'}), 400
    if target_count < 1:
        return jsonify({'success': False, 'error': 'Target must be at least 1.'}), 400
    if reward_each < 0.01:
        return jsonify({'success': False, 'error': 'Reward must be at least $0.01.'}), 400

    budget = round(target_count * LISTER_COST_PER_TASK, 2)
    user   = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()
    if budget > user['balance']:
        return jsonify({'success': False,
                        'error': f'Insufficient balance. Need ${budget:.2f}, have ${user["balance"]:.2f}.'}), 400

    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (budget, uid))
    boost_id = db.execute(
        "INSERT INTO post_boosts (post_id, user_id, budget, reward_per_engage, "
        "engage_type, target_count, status) VALUES (?,?,?,?,?,?,'active')",
        (post_id, uid, budget, WORKER_REWARD_PER_TASK, engage_type, target_count)
    ).fetchone()['id']
    db.execute('UPDATE posts SET is_boosted=1 WHERE id=?', (post_id,))
    add_transaction(db, uid, 'spend', budget,
                    f'Boost post #{post_id} — {target_count}x {engage_type}')
    add_notification(db, uid,
        f'📣 Your post is now boosted! ${budget:.2f} budget, {target_count} target engagements.')
    db.commit()
    return jsonify({'success': True, 'boost_id': boost_id, 'budget': budget})


@bp.route('/post/<int:post_id>/boost/cancel', methods=['POST'])
@login_required
def cancel_boost(post_id):
    db  = get_db()
    uid = session['user_id']
    boost = db.execute(
        "SELECT * FROM post_boosts WHERE post_id=? AND user_id=? AND status='active'",
        (post_id, uid)
    ).fetchone()
    if not boost:
        return jsonify({'success': False, 'error': 'No active boost found.'}), 404

    refund = round(float(boost['budget']) - float(boost['budget_spent']), 6)
    db.execute("UPDATE post_boosts SET status='cancelled' WHERE id=?", (boost['id'],))
    if refund > 0:
        db.execute('UPDATE users SET balance=balance+? WHERE id=?', (refund, uid))
        add_transaction(db, uid, 'deposit', refund, f'Boost refund for post #{post_id}')
        add_notification(db, uid, f'↩️ Boost cancelled. ${refund:.2f} refunded to your wallet.')

    other = db.execute(
        "SELECT id FROM post_boosts WHERE post_id=? AND status='active' AND id!=?",
        (post_id, boost['id'])
    ).fetchone()
    if not other:
        db.execute('UPDATE posts SET is_boosted=0 WHERE id=?', (post_id,))
    db.commit()
    return jsonify({'success': True, 'refund': refund})


@bp.route('/post/<int:post_id>/earn', methods=['POST'])
@login_required
@limiter.limit(LIMIT_TASK)
@csrf_exempt   # JSON POST
def earn_engagement(post_id):
    db  = get_db()
    uid = session['user_id']

    boost = db.execute("""
        SELECT pb.* FROM post_boosts pb
        WHERE pb.post_id=? AND pb.status='active'
          AND pb.budget_spent < pb.budget
          AND pb.user_id != ?
          AND NOT EXISTS (
            SELECT 1 FROM boost_engagements be
            WHERE be.boost_id=pb.id AND be.worker_id=?
          )
        ORDER BY pb.created_at DESC LIMIT 1
    """, (post_id, uid, uid)).fetchone()
    if not boost:
        return jsonify({'success': False,
                        'error': 'No earnable boost available on this post.'}), 400

    reward = float(boost['reward_per_engage'])
    db.execute(
        "INSERT INTO boost_engagements (boost_id, post_id, worker_id, reward, earned_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (boost['id'], post_id, uid, reward)
    )
    db.execute("""
        UPDATE post_boosts
        SET budget_spent  = budget_spent + ?,
            engaged_count = engaged_count + 1,
            status = CASE
              WHEN budget_spent + ? >= budget THEN 'completed'
              WHEN engaged_count + 1 >= target_count THEN 'completed'
              ELSE status
            END
        WHERE id=?
    """, (reward, reward, boost['id']))

    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (reward, uid))
    add_transaction(db, uid, 'earn', reward,
                    f'Earned from boosted post #{post_id} ({boost["engage_type"]})')

    updated_boost = db.execute('SELECT * FROM post_boosts WHERE id=?', (boost['id'],)).fetchone()
    if updated_boost and updated_boost['status'] == 'completed':
        db.execute('UPDATE posts SET is_boosted=0 WHERE id=?', (post_id,))
        add_notification(db, boost['user_id'],
            f'🎉 Your boost on post #{post_id} completed! '
            f'{updated_boost["engaged_count"]} engagements reached.')

    check_and_award_referral_bonus(db, uid)
    db.commit()

    new_balance = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    return jsonify({
        'success': True, 'reward': reward, 'balance': new_balance,
        'message': f'+${reward:.2f} earned!'
    })


@bp.route('/boosts')
@login_required
def my_boosts():
    db  = get_db()
    uid = session['user_id']
    boosts = db.execute("""
        SELECT pb.*, p.body, p.like_count, p.reply_count
        FROM post_boosts pb JOIN posts p ON p.id=pb.post_id
        WHERE pb.user_id=? ORDER BY pb.created_at DESC LIMIT 50
    """, (uid,)).fetchall()

    total_spent   = sum(float(b['budget_spent']) for b in boosts)
    total_budget  = sum(float(b['budget']) for b in boosts)
    total_engaged = sum(int(b['engaged_count']) for b in boosts)
    earned = db.execute(
        'SELECT COALESCE(SUM(be.reward),0) as total FROM boost_engagements be WHERE be.worker_id=?',
        (uid,)
    ).fetchone()['total']

    return render_template('my_boosts.html',
                           boosts=[dict(b) for b in boosts],
                           total_spent=total_spent, total_budget=total_budget,
                           total_engaged=total_engaged,
                           earned_from_boosts=float(earned))


@bp.route('/api/earn/posts')
@login_required
def api_earn_posts():
    db   = get_db()
    uid  = session['user_id']
    page = safe_int(request.args.get('page'), 1)
    per  = 10
    off  = (page - 1) * per

    rows = db.execute("""
        SELECT DISTINCT p.* FROM posts p
        JOIN post_boosts pb ON pb.post_id = p.id
        WHERE pb.status='active'
          AND pb.budget_spent < pb.budget
          AND pb.user_id != ?
          AND NOT EXISTS (
            SELECT 1 FROM boost_engagements be
            WHERE be.boost_id=pb.id AND be.worker_id=?
          )
        ORDER BY pb.reward_per_engage DESC, p.created_at DESC LIMIT ? OFFSET ?
    """, (uid, uid, per, off)).fetchall()

    posts    = [format_post(r, uid, db) for r in rows]
    has_more = len(rows) == per
    return jsonify({'posts': posts, 'has_more': has_more})


# ── Analytics ────────────────────────────────────────────────────────────────

@bp.route('/analytics')
@login_required
def analytics():
    db  = get_db()
    uid = session['user_id']
    LISTER_COST_PER_TASK = current_app.config['LISTER_COST_PER_TASK']
    CURRENCY_SYMBOL      = current_app.config['CURRENCY_SYMBOL']

    ads_rows = db.execute(
        'SELECT * FROM ads WHERE user_id=? ORDER BY created_at DESC', (uid,)
    ).fetchall()
    ads = [dict(a) for a in ads_rows]

    if not ads:
        return render_template('analytics.html', ads=[], summary=None, currency=CURRENCY_SYMBOL)

    total_budget    = sum(float(ad['budget'] or 0) for ad in ads)
    total_spent     = sum(float(ad['budget_spent'] or 0) for ad in ads)
    total_followers = sum(int(ad['followers_gained'] or 0) for ad in ads)
    active_campaigns = sum(1 for ad in ads if ad['status'] == 'active')
    total_completions = db.execute(
        'SELECT COUNT(*) FROM task_completions WHERE ad_id IN '
        '(SELECT id FROM ads WHERE user_id=?) AND status="completed"', (uid,)
    ).fetchone()[0]

    roi = round((total_followers * LISTER_COST_PER_TASK - total_spent) / total_spent * 100, 2) \
          if total_spent > 0 else 0.0
    avg_cost = round(total_spent / total_followers, 4) if total_followers > 0 else 0.0

    summary = {
        'total_ads': len(ads), 'active_campaigns': active_campaigns,
        'total_budget': round(total_budget, 2), 'total_spent': round(total_spent, 2),
        'total_followers': total_followers, 'total_completions': total_completions,
        'roi': roi, 'avg_cost_per_follower': avg_cost,
    }
    return render_template('analytics.html', ads=ads, summary=summary,
                           currency=CURRENCY_SYMBOL, cost_per_task=LISTER_COST_PER_TASK)


@bp.route('/api/analytics/<int:ad_id>')
@login_required
def api_analytics(ad_id):
    db  = get_db()
    uid = session['user_id']
    LISTER_COST_PER_TASK = current_app.config['LISTER_COST_PER_TASK']

    ad = db.execute('SELECT * FROM ads WHERE id=? AND user_id=?', (ad_id, uid)).fetchone()
    if not ad:
        return jsonify({'success': False, 'error': 'Ad not found'}), 404

    completions      = db.execute(
        'SELECT * FROM task_completions WHERE ad_id=? ORDER BY submitted_at DESC', (ad_id,)
    ).fetchall()
    total_tasks      = len(completions)
    completed_tasks  = sum(1 for c in completions if c['status'] == 'completed')
    rejected_tasks   = sum(1 for c in completions if c['status'] == 'rejected')
    completion_rate  = round(completed_tasks / total_tasks * 100, 2) if total_tasks > 0 else 0
    total_paid       = sum(float(c['reward'] or 0) for c in completions if c['status'] == 'completed')

    trend_data = {}
    for c in completions:
        d = c['submitted_at'][:10]
        if d not in trend_data:
            trend_data[d] = {'total': 0, 'completed': 0}
        trend_data[d]['total'] += 1
        if c['status'] == 'completed':
            trend_data[d]['completed'] += 1

    budget_spent      = float(ad['budget_spent'] or 0)
    followers_gained  = int(ad['followers_gained'] or 0)
    followers_target  = int(ad['followers_target'] or 1)
    roi = round((followers_gained * LISTER_COST_PER_TASK - budget_spent) / budget_spent * 100, 2) \
          if budget_spent > 0 else 0.0

    return jsonify({
        'success': True,
        'ad': {
            'id': ad['id'], 'title': ad['title'], 'platform': ad['platform'],
            'task_type': ad['task_type'], 'budget': ad['budget'],
            'budget_spent': budget_spent, 'followers_target': followers_target,
            'followers_gained': followers_gained, 'status': ad['status'],
        },
        'metrics': {
            'total_tasks': total_tasks, 'completed_tasks': completed_tasks,
            'rejected_tasks': rejected_tasks, 'completion_rate': completion_rate,
            'total_paid': round(total_paid, 2),
            'avg_reward': round(total_paid / completed_tasks, 4) if completed_tasks > 0 else 0,
            'target_completion_rate': round(followers_gained / followers_target * 100, 2),
            'roi': roi,
        },
        'trend': sorted(trend_data.items()),
    })


@bp.route('/api/analytics/performance')
@login_required
def api_analytics_performance():
    db  = get_db()
    uid = session['user_id']
    LISTER_COST_PER_TASK = current_app.config['LISTER_COST_PER_TASK']

    ads_rows = db.execute(
        'SELECT id, title, platform, task_type, followers_target, followers_gained, '
        'budget, budget_spent, status FROM ads WHERE user_id=? '
        'ORDER BY followers_gained DESC LIMIT 10', (uid,)
    ).fetchall()

    perf = []
    for ad in ads_rows:
        fg  = int(ad['followers_gained'] or 0)
        ft  = int(ad['followers_target'] or 1)
        bs  = float(ad['budget_spent'] or 0)
        roi = round((fg * LISTER_COST_PER_TASK - bs) / bs * 100, 2) if bs > 0 else 0.0
        perf.append({
            'title': ad['title'], 'platform': ad['platform'],
            'followers_target': ft, 'followers_gained': fg,
            'completion_rate': round(fg / ft * 100, 1) if ft > 0 else 0,
            'budget': ad['budget'],
            'cost_per_follower': round(bs / fg, 4) if fg > 0 else 0,
            'roi': roi, 'status': ad['status'],
        })
    return jsonify({'success': True, 'performance': perf})


@bp.route('/api/activity')
@login_required
def activity_feed():
    db = get_db()
    rows = db.execute(
        'SELECT tc.reward, tc.submitted_at, u.username, a.title '
        'FROM task_completions tc '
        'JOIN users u ON tc.worker_id=u.id '
        'JOIN ads a ON tc.ad_id=a.id '
        'ORDER BY tc.submitted_at DESC LIMIT 10'
    ).fetchall()
    return jsonify([
        {'worker': r['username'], 'ad': r['title'],
         'reward': r['reward'], 'time': r['submitted_at'][11:19]}
        for r in rows
    ])


# ── Creator monetisation ─────────────────────────────────────────────────────

@bp.route('/post/<int:post_id>/tip', methods=['POST'])
@login_required
@limiter.limit(LIMIT_TIP)
def tip_post(post_id):
    db  = get_db()
    uid = session['user_id']
    post = db.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
    if not post:
        return jsonify({'success': False, 'error': 'Post not found.'}), 404
    if post['user_id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot tip your own post.'}), 400

    amount  = safe_float(request.form.get('amount'), 0)
    message = (request.form.get('message') or '').strip()[:120]
    if amount < 0.01:
        return jsonify({'success': False, 'error': 'Minimum tip is $0.01.'}), 400

    sender = db.execute('SELECT balance, username FROM users WHERE id=?', (uid,)).fetchone()
    if amount > sender['balance']:
        return jsonify({'success': False, 'error': 'Insufficient balance.'}), 400

    db.execute('UPDATE users SET balance=balance-?, total_tips_sent=total_tips_sent+? WHERE id=?',
               (amount, amount, uid))
    db.execute('UPDATE users SET balance=balance+?, total_tips_received=total_tips_received+? WHERE id=?',
               (amount, amount, post['user_id']))
    db.execute(
        'INSERT INTO tips (from_user_id, to_user_id, post_id, amount, message) VALUES (?,?,?,?,?)',
        (uid, post['user_id'], post_id, amount, message or None)
    )
    recipient = db.execute('SELECT username FROM users WHERE id=?', (post['user_id'],)).fetchone()
    add_transaction(db, uid, 'tip_sent', amount,
                    f'Tip to @{recipient["username"]} on post #{post_id}')
    add_transaction(db, post['user_id'], 'tip_received', amount,
                    f'Tip from @{sender["username"]} on post #{post_id}')
    tip_msg = f'💰 @{sender["username"]} tipped you ${amount:.2f} USDT'
    if message:
        tip_msg += f': "{message}"'
    add_notification(db, post['user_id'], tip_msg)
    db.commit()

    new_bal = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    return jsonify({'success': True, 'amount': amount, 'balance': new_bal,
                    'message': f'${amount:.2f} tip sent!'})


@bp.route('/user/<username>/tip', methods=['POST'])
@login_required
@limiter.limit(LIMIT_TIP)
def tip_user(username):
    db  = get_db()
    uid = session['user_id']
    target = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not target:
        return jsonify({'success': False, 'error': 'User not found.'}), 404
    if target['id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot tip yourself.'}), 400

    amount  = safe_float(request.form.get('amount'), 0)
    message = (request.form.get('message') or '').strip()[:120]
    if amount < 0.01:
        return jsonify({'success': False, 'error': 'Minimum tip is $0.01.'}), 400

    sender = db.execute('SELECT balance, username FROM users WHERE id=?', (uid,)).fetchone()
    if amount > sender['balance']:
        return jsonify({'success': False, 'error': 'Insufficient balance.'}), 400

    db.execute('UPDATE users SET balance=balance-?, total_tips_sent=total_tips_sent+? WHERE id=?',
               (amount, amount, uid))
    db.execute('UPDATE users SET balance=balance+?, total_tips_received=total_tips_received+? WHERE id=?',
               (amount, amount, target['id']))
    db.execute('INSERT INTO tips (from_user_id, to_user_id, amount, message) VALUES (?,?,?,?)',
               (uid, target['id'], amount, message or None))
    add_transaction(db, uid, 'tip_sent', amount, f'Tip to @{username}')
    add_transaction(db, target['id'], 'tip_received', amount, f'Tip from @{sender["username"]}')
    notif = f'💰 @{sender["username"]} tipped you ${amount:.2f} USDT'
    if message:
        notif += f': "{message}"'
    add_notification(db, target['id'], notif)
    db.commit()

    new_bal = db.execute('SELECT balance FROM users WHERE id=?', (uid,)).fetchone()['balance']
    return jsonify({'success': True, 'amount': amount, 'balance': new_bal,
                    'message': f'${amount:.2f} sent to @{username}!'})


@bp.route('/creator/setup', methods=['GET', 'POST'])
@login_required
def creator_setup():
    db  = get_db()
    uid = session['user_id']
    if request.method == 'POST':
        price       = safe_float(request.form.get('price_usd'), 0)
        title       = (request.form.get('title') or '').strip()[:60]
        description = (request.form.get('description') or '').strip()[:300]
        perks       = (request.form.get('perks') or '').strip()[:500]
        is_active   = 1 if request.form.get('is_active') else 0

        if price < 0.10:
            return jsonify({'success': False,
                            'error': 'Minimum subscription price is $0.10/month.'}), 400
        if not title:
            return jsonify({'success': False, 'error': 'Tier title is required.'}), 400

        existing = db.execute(
            'SELECT id FROM subscription_tiers WHERE creator_id=?', (uid,)
        ).fetchone()
        if existing:
            db.execute(
                'UPDATE subscription_tiers SET price_usd=?,title=?,description=?,perks=?,is_active=? '
                'WHERE creator_id=?',
                (price, title, description, perks, is_active, uid)
            )
        else:
            db.execute(
                'INSERT INTO subscription_tiers (creator_id,price_usd,title,description,perks,is_active) '
                'VALUES (?,?,?,?,?,?)',
                (uid, price, title, description, perks, is_active)
            )
        db.commit()
        me = db.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
        return jsonify({'success': True,
                        'redirect': url_for('social.profile', username=me['username'])})

    tier = db.execute('SELECT * FROM subscription_tiers WHERE creator_id=?', (uid,)).fetchone()
    return render_template('creator_setup.html', tier=dict(tier) if tier else None)


@bp.route('/user/<username>/subscribe', methods=['POST'])
@login_required
@limiter.limit(LIMIT_SUBSCRIBE)
def subscribe(username):
    db  = get_db()
    uid = session['user_id']
    creator = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return jsonify({'success': False, 'error': 'User not found.'}), 404
    if creator['id'] == uid:
        return jsonify({'success': False, 'error': 'Cannot subscribe to yourself.'}), 400

    tier = db.execute(
        "SELECT * FROM subscription_tiers WHERE creator_id=? AND is_active=1", (creator['id'],)
    ).fetchone()
    if not tier:
        return jsonify({'success': False,
                        'error': 'This creator has no active subscription tier.'}), 400

    existing = db.execute(
        "SELECT * FROM subscriptions WHERE subscriber_id=? AND creator_id=?",
        (uid, creator['id'])
    ).fetchone()
    if existing and existing['status'] == 'active':
        return jsonify({'success': False, 'error': 'Already subscribed.'}), 400

    subscriber = db.execute('SELECT balance, username FROM users WHERE id=?', (uid,)).fetchone()
    price = float(tier['price_usd'])
    if price > subscriber['balance']:
        return jsonify({'success': False,
                        'error': f'Insufficient balance. Need ${price:.2f}.'}), 400

    now     = datetime.now(timezone.utc)
    expires = (now + timedelta(days=30)).isoformat()

    db.execute('UPDATE users SET balance=balance-? WHERE id=?', (price, uid))
    db.execute('UPDATE users SET balance=balance+? WHERE id=?', (price, creator['id']))

    if existing:
        db.execute(
            "UPDATE subscriptions SET status='active',started_at=?,expires_at=?,tier_id=? "
            "WHERE subscriber_id=? AND creator_id=?",
            (now.isoformat(), expires, tier['id'], uid, creator['id'])
        )
    else:
        db.execute(
            'INSERT INTO subscriptions (subscriber_id,creator_id,tier_id,started_at,expires_at) '
            'VALUES (?,?,?,?,?)',
            (uid, creator['id'], tier['id'], now.isoformat(), expires)
        )
        db.execute(
            'UPDATE users SET subscriber_count=('
            'SELECT COUNT(*) FROM subscriptions WHERE creator_id=? AND status=\'active\') WHERE id=?',
            (creator['id'], creator['id'])
        )

    add_transaction(db, uid, 'subscription', price,
                    f'Subscription to @{username} ({tier["title"]})')
    add_transaction(db, creator['id'], 'earn', price,
                    f'Subscription from @{subscriber["username"]} ({tier["title"]})')
    add_notification(db, creator['id'],
        f'🎉 @{subscriber["username"]} subscribed to your {tier["title"]} tier — ${price:.2f}/month!')
    add_notification(db, uid,
        f'✅ You\'re subscribed to @{username}\'s {tier["title"]} tier. Expires in 30 days.')
    db.commit()
    return jsonify({'success': True, 'price': price, 'message': f'Subscribed to @{username}!'})


@bp.route('/user/<username>/unsubscribe', methods=['POST'])
@login_required
def unsubscribe(username):
    db  = get_db()
    uid = session['user_id']
    creator = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return jsonify({'success': False, 'error': 'User not found.'}), 404

    sub = db.execute(
        "SELECT * FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
        (uid, creator['id'])
    ).fetchone()
    if not sub:
        return jsonify({'success': False, 'error': 'No active subscription found.'}), 404

    db.execute(
        "UPDATE subscriptions SET status='cancelled' WHERE subscriber_id=? AND creator_id=?",
        (uid, creator['id'])
    )
    db.execute(
        'UPDATE users SET subscriber_count=('
        'SELECT COUNT(*) FROM subscriptions WHERE creator_id=? AND status=\'active\') WHERE id=?',
        (creator['id'], creator['id'])
    )
    add_notification(db, uid,
        f'↩️ Subscription to @{username} cancelled. Access lasts until {sub["expires_at"][:10]}.')
    db.commit()
    return jsonify({'success': True, 'expires_at': sub['expires_at'][:10],
                    'message': f'Subscription cancelled. Access until {sub["expires_at"][:10]}.'})


@bp.route('/user/<username>/subscribers')
@login_required
def subscriber_list(username):
    db  = get_db()
    uid = session['user_id']
    creator = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return render_template('error.html', code=404, message='User not found.'), 404

    viewer = db.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()
    if creator['id'] != uid and not viewer['is_admin']:
        return render_template('error.html', code=403, message='Access denied.'), 403

    subs = db.execute("""
        SELECT s.*, u.username, u.display_name, u.avatar_url, u.is_verified,
               u.follower_count, t.title as tier_title, t.price_usd
        FROM subscriptions s
        JOIN users u ON u.id=s.subscriber_id
        JOIN subscription_tiers t ON t.id=s.tier_id
        WHERE s.creator_id=? ORDER BY s.started_at DESC
    """, (creator['id'],)).fetchall()

    active_count    = sum(1 for s in subs if s['status'] == 'active')
    monthly_revenue = sum(float(s['price_usd']) for s in subs if s['status'] == 'active')
    return render_template('subscriber_list.html',
                           creator=dict(creator), subs=[dict(s) for s in subs],
                           active_count=active_count, monthly_revenue=monthly_revenue)


@bp.route('/creator/earnings')
@login_required
def creator_earnings():
    db  = get_db()
    uid = session['user_id']
    tier = db.execute('SELECT * FROM subscription_tiers WHERE creator_id=?', (uid,)).fetchone()

    tips_received = db.execute("""
        SELECT t.*, u.username as sender_name, u.avatar_url as sender_avatar, p.body as post_body
        FROM tips t JOIN users u ON u.id=t.from_user_id LEFT JOIN posts p ON p.id=t.post_id
        WHERE t.to_user_id=? ORDER BY t.created_at DESC LIMIT 50
    """, (uid,)).fetchall()
    tips_sent = db.execute("""
        SELECT t.*, u.username as recipient_name FROM tips t
        JOIN users u ON u.id=t.to_user_id WHERE t.from_user_id=? ORDER BY t.created_at DESC LIMIT 20
    """, (uid,)).fetchall()
    subscribers = db.execute("""
        SELECT s.*, u.username, u.display_name, u.avatar_url, t.title as tier_title, t.price_usd
        FROM subscriptions s JOIN users u ON u.id=s.subscriber_id
        JOIN subscription_tiers t ON t.id=s.tier_id
        WHERE s.creator_id=? AND s.status='active' ORDER BY s.started_at DESC
    """, (uid,)).fetchall()

    sub_revenue = db.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions "
        "WHERE user_id=? AND type='earn' AND description LIKE 'Subscription from %'", (uid,)
    ).fetchone()[0]
    boost_earned = db.execute(
        'SELECT COALESCE(SUM(reward),0) FROM boost_engagements WHERE worker_id=?', (uid,)
    ).fetchone()[0]
    me = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

    return render_template('creator_earnings.html',
                           tier=dict(tier) if tier else None,
                           tips_received=[dict(t) for t in tips_received],
                           tips_sent=[dict(t) for t in tips_sent],
                           subscribers=[dict(s) for s in subscribers],
                           total_tips=float(me['total_tips_received'] or 0),
                           sub_revenue=float(sub_revenue),
                           boost_earned=float(boost_earned),
                           monthly_subs=sum(float(s['price_usd']) for s in subscribers),
                           me=dict(me))


@bp.route('/api/creator/stats/<username>')
@login_required
def api_creator_stats(username):
    db  = get_db()
    uid = session['user_id']
    creator = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not creator:
        return jsonify({'success': False}), 404

    tier = db.execute(
        "SELECT * FROM subscription_tiers WHERE creator_id=? AND is_active=1", (creator['id'],)
    ).fetchone()
    is_subscribed = bool(db.execute(
        "SELECT 1 FROM subscriptions WHERE subscriber_id=? AND creator_id=? AND status='active'",
        (uid, creator['id'])
    ).fetchone()) if uid != creator['id'] else False
    top_tips = db.execute("""
        SELECT t.amount, t.message, u.username, u.avatar_url
        FROM tips t JOIN users u ON u.id=t.from_user_id
        WHERE t.to_user_id=? ORDER BY t.amount DESC LIMIT 3
    """, (creator['id'],)).fetchall()

    return jsonify({
        'success': True, 'tier': dict(tier) if tier else None,
        'is_subscribed': is_subscribed,
        'subscriber_count': creator['subscriber_count'] or 0,
        'total_tips': float(creator['total_tips_received'] or 0),
        'top_tips': [dict(t) for t in top_tips],
    })
