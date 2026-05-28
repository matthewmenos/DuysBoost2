/* =========================================================================
   DUYS Boost — Client JS (shared utilities)
   All wiring is done via addEventListener on DOMContentLoaded so it works
   regardless of script-tag position.
   ========================================================================= */
(function () {
  'use strict';

  // ── Utility: escape HTML ───────────────────────────────────────────────
  
// ── Notification icon SVGs (favicons replace emojis) ───────────────────────
const _NOTIF_ICONS = {
  like:    '<svg viewBox="0 0 24 24" width="20" height="20" fill="#f91880" stroke="#f91880" stroke-width="2"><path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/></svg>',
  reply:   '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#1d9bf0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg>',
  follow:  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#1d9bf0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="8.5" cy="7" r="4"/><line x1="20" y1="8" x2="20" y2="14"/><line x1="23" y1="11" x2="17" y2="11"/></svg>',
  repost:  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#00ba7c" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 014-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 01-4 4H3"/></svg>',
  mention: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#1d9bf0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 006 0v-1a10 10 0 10-3.92 7.94"/></svg>',
  message: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#1d9bf0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>',
  tip:     '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#fbbc04" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg>',
  wallet:  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#10b981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12V7H5a2 2 0 010-4h14v4"/><path d="M3 5v14a2 2 0 002 2h16v-5"/><path d="M18 12a2 2 0 000 4h4v-4z"/></svg>',
  verify:  '<svg viewBox="0 0 24 24" width="20" height="20" fill="#1d9bf0" stroke="none"><path d="M12 1l3 2 3-1 1 3 3 1-1 3 2 3-2 3 1 3-3 1-1 3-3-1-3 2-3-2-3 1-1-3-3-1 1-3-2-3 2-3-1-3 3-1 1-3 3 1 3-2z" /></svg>',
  boost:   '<svg viewBox="0 0 24 24" width="20" height="20" fill="#a855f7" stroke="none"><path d="M13 2L3 14h7v8l11-14h-7l-1-6z"/></svg>',
  channel: '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#1d9bf0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>',
  group:   '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#1d9bf0" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>',
  story:   '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#e91e63" stroke-width="2"><circle cx="12" cy="12" r="10"/></svg>',
  system:  '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#94a3b8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
};

function renderNotifItem(n) {
  const icon  = _NOTIF_ICONS[n.icon] || _NOTIF_ICONS.system;
  const cls   = 'notif-item' + (n.read ? '' : ' notif-unread');
  const inner = '<div class="notif-icon-wrap">' + icon + '</div>' +
                '<div class="notif-body">' +
                '<div class="notif-msg">' + escapeHtml(n.msg) + '</div>' +
                '<div class="notif-time">' + escapeHtml(n.time) + '</div>' +
                '</div>';
  if (n.link) {
    return '<a class="' + cls + '" href="' + escapeHtml(n.link) +
           '" data-notif-id="' + n.id + '" onclick="_markNotifRead(' + n.id + ')">' +
           inner + '</a>';
  }
  return '<div class="' + cls + '" data-notif-id="' + n.id + '">' + inner + '</div>';
}
window.renderNotifItem = renderNotifItem;

function _markNotifRead(id) {
  fetch('/api/notifications/' + id + '/read', { method: 'POST' }).catch(function(){});
}
window._markNotifRead = _markNotifRead;


function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  window.escapeHtml = escapeHtml;

  // ── Toast ─────────────────────────────────────────────────────────────
  const TOAST_CONTAINER_ID = 'toast-container';
  function showToast(msg, type) {
    type = type || 'success';
    let c = document.getElementById(TOAST_CONTAINER_ID);
    if (!c) {
      c = document.createElement('div');
      c.className = 'toast-container';
      c.id = TOAST_CONTAINER_ID;
      document.body.appendChild(c);
    }
    const t = document.createElement('div');
    t.className = 'toast ' + type;
    t.setAttribute('role', type === 'error' ? 'alert' : 'status');
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(function () {
      t.style.animation = 'slideOut .3s ease forwards';
      setTimeout(function () { t.remove(); }, 300);
    }, 3500);
  }
  window.showToast = showToast;

  // ── DOM-ready wrapper ──────────────────────────────────────────────────
  function ready(fn) {
    if (document.readyState !== 'loading') fn();
    else document.addEventListener('DOMContentLoaded', fn);
  }

  ready(function () {
    // Elements — looked up fresh after DOM is parsed
    const sidebar       = document.getElementById('sidebar');
    const backdrop      = document.getElementById('sidebar-backdrop');
    const hamburgerBtn  = document.getElementById('hamburger-btn');
    const sidebarClose  = document.getElementById('sidebar-close-btn');
    const themeBtn      = document.getElementById('theme-btn');
    const notifBtn      = document.getElementById('notif-btn');
    const notifDropdown = document.getElementById('notif-dropdown');

    // ── Sidebar open / close ────────────────────────────────────────────
    function openSidebar() {
      if (!sidebar) return;
      sidebar.classList.add('open');
      if (backdrop) backdrop.classList.add('show');
      document.body.classList.add('no-scroll');
      if (hamburgerBtn) hamburgerBtn.setAttribute('aria-expanded', 'true');
    }
    function closeSidebar() {
      if (!sidebar) return;
      sidebar.classList.remove('open');
      if (backdrop) backdrop.classList.remove('show');
      document.body.classList.remove('no-scroll');
      if (hamburgerBtn) hamburgerBtn.setAttribute('aria-expanded', 'false');
    }
    function toggleSidebar() {
      if (!sidebar) return;
      if (sidebar.classList.contains('open')) closeSidebar();
      else openSidebar();
    }
    // Expose for any leftover inline handlers
    window.openSidebar = openSidebar;
    window.closeSidebar = closeSidebar;
    window.toggleSidebar = toggleSidebar;

    if (hamburgerBtn) {
      hamburgerBtn.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        toggleSidebar();
      });
    }
    if (sidebarClose) {
      sidebarClose.addEventListener('click', function (e) {
        e.preventDefault();
        closeSidebar();
      });
    }
    if (backdrop) backdrop.addEventListener('click', closeSidebar);

    // ── Collapsible nav groups ────────────────────────────────────────────
    window.toggleNavGroup = function(groupId) {
      const group = document.getElementById(groupId);
      if (!group) return;
      const isOpen = group.classList.contains('open');
      // Close all other groups
      document.querySelectorAll('.nav-group.open').forEach(function(g) {
        if (g.id !== groupId) {
          g.classList.remove('open');
          const btn = g.querySelector('.nav-group-btn');
          if (btn) btn.setAttribute('aria-expanded', 'false');
        }
      });
      // Toggle this one
      group.classList.toggle('open', !isOpen);
      const btn = group.querySelector('.nav-group-btn');
      if (btn) btn.setAttribute('aria-expanded', String(!isOpen));
      // Persist in sessionStorage
      sessionStorage.setItem('navGroup', !isOpen ? groupId : '');
    };

    // Auto-open the group that contains an active link
    (function autoOpenActiveGroup() {
      // First try: find a sub-item with .active class
      let opened = false;
      document.querySelectorAll('.nav-group').forEach(function(group) {
        if (group.querySelector('.nav-sub-item.active')) {
          group.classList.add('open');
          const btn = group.querySelector('.nav-group-btn');
          if (btn) {
            btn.setAttribute('aria-expanded', 'true');
            btn.classList.add('active-group');
          }
          opened = true;
        }
      });
      // If nothing is active (e.g. fresh load), restore from sessionStorage
      if (!opened) {
        const saved = sessionStorage.getItem('navGroup');
        if (saved) {
          const g = document.getElementById(saved);
          if (g) {
            g.classList.add('open');
            const btn = g.querySelector('.nav-group-btn');
            if (btn) btn.setAttribute('aria-expanded', 'true');
          }
        } else {
          // Default: open Social
          const social = document.getElementById('grp-social');
          if (social) {
            social.classList.add('open');
            const btn = social.querySelector('.nav-group-btn');
            if (btn) btn.setAttribute('aria-expanded', 'true');
          }
        }
      }
    })();

    // ── Close sidebar when sub-item clicked on mobile ─────────────────
    if (sidebar) {
      sidebar.addEventListener('click', function(e) {
        const subLink = e.target.closest('a.nav-sub-item');
        if (subLink && window.innerWidth < 900) closeSidebar();
      });
    }

    // Close sidebar when a nav link is clicked on mobile
    if (sidebar) {
      sidebar.addEventListener('click', function (e) {
        const link = e.target.closest('a.nav-item');
        if (link && window.innerWidth < 900) closeSidebar();
      });
    }

    // Reset state when crossing the desktop breakpoint
    let resizeTimer;
    window.addEventListener('resize', function () {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        if (window.innerWidth >= 900) {
          if (sidebar) sidebar.classList.remove('open');
          if (backdrop) backdrop.classList.remove('show');
          document.body.classList.remove('no-scroll');
        }
      }, 120);
    });

    // ── Theme toggle ────────────────────────────────────────────────────
    async function toggleTheme() {
      try {
        const r = await fetch('/api/theme', { method: 'POST' });
        const d = await r.json();
        document.documentElement.setAttribute('data-theme', d.theme);
        document.querySelectorAll('[data-theme-icon]').forEach(function (el) {
          // SVG icons for theme — replaces emoji
          const sunPath = '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>';
          const moonPath = '<path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/>';
          const paths = d.theme === 'dark' ? sunPath : moonPath;
          el.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + paths + '</svg>';
        });
      } catch (_err) {
        showToast('Could not change theme.', 'error');
      }
    }
    window.toggleTheme = toggleTheme;
    if (themeBtn) themeBtn.addEventListener('click', toggleTheme);

    // ── Notification dropdown ───────────────────────────────────────────
    function toggleNotifDropdown() {
      if (!notifDropdown) return;
      const wasOpen = notifDropdown.classList.contains('open');
      notifDropdown.classList.toggle('open');
      if (!wasOpen) loadNotifications();
    }
    window.toggleNotifDropdown = toggleNotifDropdown;
    if (notifBtn) {
      notifBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        toggleNotifDropdown();
      });
    }
    document.addEventListener('click', function (e) {
      if (!notifDropdown || !notifBtn) return;
      if (!notifBtn.contains(e.target) && !notifDropdown.contains(e.target)) {
        notifDropdown.classList.remove('open');
      }
    });

    async function loadNotifications() {
      try {
        const r = await fetch('/api/notifications/unread');
        const d = await r.json();
        const dot           = document.getElementById('notif-dot');
        const sidebarBadge  = document.getElementById('sidebar-notif-count');
        const bottomBadge   = document.getElementById('bottom-notif-badge');

        const hasUnread = d.count > 0;
        const countLabel = d.count > 99 ? '99+' : String(d.count);
        // Notification bell badge (header)
        if (dot) {
          dot.textContent = hasUnread ? countLabel : '';
          dot.style.display = hasUnread ? 'inline-flex' : 'none';
        }
        [sidebarBadge, bottomBadge].forEach(function (el) {
          if (!el) return;
          el.textContent = countLabel;
          el.style.display = hasUnread ? 'inline-block' : 'none';
        });

        const list = document.getElementById('notif-list');
        if (list) {
          if (!d.recent || !d.recent.length) {
            list.innerHTML = '<div class="notif-item text-muted text-center" style="padding:24px 18px">No new notifications</div>';
          } else {
            list.innerHTML = d.recent.map(function (n) { return renderNotifItem(n); }).join('');
          }
        }
      } catch (_err) { /* silent */ }
    }

    // ── Global SSE stream (replaces all badge polling) ────────────────
    if (notifBtn) {
      // Do an immediate fetch for first paint, then SSE takes over
      loadNotifications();

      const _globalSrc = new EventSource('/api/stream');

      _globalSrc.addEventListener('notifications', function(e) {
        const d = JSON.parse(e.data);
        const dot          = document.getElementById('notif-dot');
        const sidebarBadge = document.getElementById('sidebar-notif-count');
        const bottomBadge  = document.getElementById('bottom-notif-badge');
        const hasUnread = d.count > 0;
        const countLabel = d.count > 99 ? '99+' : String(d.count);
        if (dot) {
          dot.textContent = hasUnread ? countLabel : '';
          dot.style.display = hasUnread ? 'inline-flex' : 'none';
        }
        [sidebarBadge, bottomBadge].forEach(function(el) {
          if (!el) return;
          el.textContent = countLabel;
          el.style.display = hasUnread ? 'inline-block' : 'none';
        });
        const list = document.getElementById('notif-list');
        if (list && d.recent) {
          if (!d.recent.length) {
            list.innerHTML = '<div class="notif-item text-muted text-center" style="padding:24px 18px">No new notifications</div>';
          } else {
            list.innerHTML = d.recent.map(function (n) { return renderNotifItem(n); }).join('');
          }
        }
      });

      _globalSrc.addEventListener('dm_unread', function(e) {
        const d = JSON.parse(e.data);
        const cnt = d.count || 0;
        const dmLabel = cnt > 99 ? '99+' : String(cnt);
        ['bottom-dm-badge','sidebar-dm-badge'].forEach(function(id) {
          const el = document.getElementById(id);
          if (!el) return;
          el.textContent = dmLabel;
          el.style.display = cnt > 0 ? 'inline-flex' : 'none';
        });
      });

      _globalSrc.addEventListener('group_unread', function(e) {
        const d = JSON.parse(e.data);
        const cnt = d.count || 0;
        const badge = document.getElementById('sidebar-grp-badge');
        if (badge) {
          badge.textContent = cnt;
          badge.style.display = cnt > 0 ? 'inline-flex' : 'none';
        }
      });

      _globalSrc.addEventListener('activity', function(e) {
        const d = JSON.parse(e.data);
        // Update dashboard activity feed if it exists on this page
        const feed = document.getElementById('activity-feed');
        if (feed && d.items && d.items.length) {
          d.items.forEach(function(item) {
            const row = document.createElement('div');
            row.className = 'activity-row';
            row.innerHTML = '<span class="act-worker">@' + escapeHtml(item.worker) +
              '</span> completed <span class="act-ad">' + escapeHtml(item.ad) +
              '</span> <span class="act-reward">+$' + item.reward.toFixed(2) +
              '</span> <span class="act-time">' + escapeHtml(item.time) + '</span>';
            feed.insertBefore(row, feed.firstChild);
            // Keep max 10 rows
            while (feed.children.length > 10) feed.removeChild(feed.lastChild);
          });
        }
      });

      _globalSrc.onerror = function() {
        // Browser auto-reconnects — no manual retry needed
      };

      window.addEventListener('beforeunload', function() { _globalSrc.close(); });
    }

    // ── Modal helpers ───────────────────────────────────────────────────
    function openModal(id) {
      const m = document.getElementById(id);
      if (!m) return;
      m.classList.add('active');
      document.body.classList.add('no-scroll');
      const first = m.querySelector('input, select, textarea, button');
      if (first) setTimeout(function () { first.focus(); }, 100);
    }
    function closeModal(id) {
      const m = document.getElementById(id);
      if (!m) return;
      m.classList.remove('active');
      if (!document.querySelector('.modal-overlay.active') &&
          !(sidebar && sidebar.classList.contains('open'))) {
        document.body.classList.remove('no-scroll');
      }
    }
    window.openModal = openModal;
    window.closeModal = closeModal;

    document.querySelectorAll('.modal-overlay').forEach(function (overlay) {
      overlay.addEventListener('click', function (e) {
        if (e.target === overlay) closeModal(overlay.id);
      });
    });

    // ── Global ESC handler ──────────────────────────────────────────────
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      if (sidebar && sidebar.classList.contains('open')) closeSidebar();
      document.querySelectorAll('.modal-overlay.active').forEach(function (m) {
        closeModal(m.id);
      });
      if (notifDropdown) notifDropdown.classList.remove('open');
    });

    // ── Password strength meter ─────────────────────────────────────────
    document.querySelectorAll('[data-strength-for]').forEach(function (meter) {
      const targetId = meter.getAttribute('data-strength-for');
      const input = document.getElementById(targetId);
      if (!input) return;
      const labelEl = meter.querySelector('.pw-strength-label');
      input.addEventListener('input', function () {
        const scored = scorePassword(input.value);
        meter.setAttribute('data-level', scored.level);
        if (labelEl) labelEl.textContent = input.value ? scored.label : '';
      });
    });

    function scorePassword(pw) {
      if (!pw) return { level: 0, label: '' };
      let score = 0;
      if (pw.length >= 8) score++;
      if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) score++;
      if (/\d/.test(pw)) score++;
      if (/[^A-Za-z0-9]/.test(pw) && pw.length >= 10) score++;
      const labels = ['', 'Weak', 'Fair', 'Good', 'Strong'];
      return { level: score, label: labels[score] };
    }
  });
})();

// ── Phase 4: Autocomplete keyboard navigation ─────────────────────────────
(function() {
  document.addEventListener('keydown', function(e) {
    const dropdown = document.getElementById('ac-dropdown');
    if (!dropdown || !dropdown.classList.contains('open')) return;
    const items = Array.from(dropdown.querySelectorAll('.ac-item'));
    if (!items.length) return;
    const current = dropdown.querySelector('.ac-item.selected');
    let idx = items.indexOf(current);

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      if (current) current.classList.remove('selected');
      idx = (idx + 1) % items.length;
      items[idx].classList.add('selected');
      items[idx].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      if (current) current.classList.remove('selected');
      idx = (idx - 1 + items.length) % items.length;
      items[idx].classList.add('selected');
      items[idx].scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && current) {
      e.preventDefault();
      current.click();
    }
  });
})();

// ── Online presence — handled by SSE keepalive, no polling needed ────────
// The global SSE stream (/api/stream) updates online_at on every cycle.
// A lightweight fallback heartbeat fires only when SSE is unavailable.
(function() {
  const notifBtn = document.getElementById('notif-btn');
  if (!notifBtn) return;
  // Only send REST heartbeat if EventSource is not supported
  if (typeof EventSource === 'undefined') {
    setInterval(function() {
      fetch('/api/online/heartbeat', { method: 'POST' }).catch(() => {});
    }, 30000);
  }
})();

// ── Post image lightbox (for feed media images) ───────────────────────────
(function() {
  function openPostLightbox(postId) {
    const card = document.querySelector(`[data-post-id="${postId}"]`);
    if (!card) return;
    const img = card.querySelector('.post-media-img');
    if (!img) return;
    const lb = document.createElement('div');
    lb.className = 'post-lightbox';
    lb.innerHTML = `<img src="${img.src}" alt="">`;
    lb.addEventListener('click', () => lb.remove());
    document.addEventListener('keydown', function esc(e) {
      if (e.key === 'Escape') { lb.remove(); document.removeEventListener('keydown', esc); }
    });
    document.body.appendChild(lb);
  }
  window.openPostLightbox = openPostLightbox;
})();

// ── Poll voting ─────────────────────────────────────────────────────────────
async function castVote(postId, optionId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const fd = new FormData();
    fd.append('option_id', optionId);
    const r = await fetch('/post/' + postId + '/poll/vote', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.success) {
      const block = document.getElementById('poll-block-' + postId);
      if (block) {
        block.innerHTML = d.options.map(function(o) {
          return '<div class="poll-option-row" style="cursor:default">' +
            '<div class="poll-option-label' + (o.id === d.user_vote ? ' poll-voted' : '') + '">' +
            escapeHtml(o.label) + (o.id === d.user_vote ? ' ✓' : '') + '</div>' +
            '<div class="poll-bar-wrap"><div class="poll-bar" style="width:' + o.pct + '%"></div></div>' +
            '<div class="poll-pct">' + o.pct + '%</div>' +
            '</div>';
        }).join('') +
        '<div class="text-xs text-muted" style="margin-top:6px">' + d.total + ' vote' + (d.total !== 1 ? 's' : '') + '</div>';
      }
      if (typeof showToast === 'function') showToast('Vote recorded!');
    } else {
      if (typeof showToast === 'function') showToast(d.error || 'Could not vote.', 'error');
      if (btn) { btn.disabled = false; btn.textContent = btn.dataset.label || btn.textContent; }
    }
  } catch (_) {
    if (typeof showToast === 'function') showToast('Network error.', 'error');
    if (btn) btn.disabled = false;
  }
}
window.castVote = castVote;

// ── Group unread badge — now pushed via SSE global stream ───────────────────
// Handled by the 'group_unread' event in the EventSource above.
// This block intentionally left empty (polling removed).

// ── Share post (works on all pages) ─────────────────────────────────────────
async function sharePost(postId, body) {
  const url  = window.location.origin + '/post/' + postId;
  const text = (body || '').slice(0, 100) + (body && body.length > 100 ? '…' : '');

  // Try native share sheet first (mobile)
  if (navigator.share) {
    try {
      await navigator.share({ title: 'DUYS Boost', text: text, url: url });
      return;
    } catch (e) {
      if (e.name === 'AbortError') return; // user cancelled — do nothing
      // Fall through to clipboard copy on other errors (NotAllowedError etc.)
    }
  }

  // Clipboard copy fallback
  let copied = false;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(url);
      copied = true;
    } catch (_) {}
  }
  if (!copied) {
    // Final fallback for HTTP or older browsers
    try {
      const ta = document.createElement('textarea');
      ta.value = url;
      ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      copied = true;
    } catch (_) {}
  }

  if (typeof showToast === 'function') {
    showToast(copied ? 'Link copied to clipboard!' : 'Could not copy link', copied ? 'success' : 'error');
  }
}
window.sharePost = sharePost;
