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

      var _sseAttempt = 0;
      var _globalSrc;

      function _openSSE() {
        _globalSrc = new EventSource('/api/stream');

        _globalSrc.addEventListener('open', function() {
          _sseAttempt = 0;  // reset backoff on successful connection
        });

        _globalSrc.onerror = function() {
          _globalSrc.close();
          _sseAttempt++;
          // Jittered exponential backoff: 1s base, capped at 30s, +up-to-4s jitter
          var delay = Math.min(30000, 1000 * Math.pow(1.8, _sseAttempt - 1)) + Math.random() * 4000;
          setTimeout(_openSSE, delay);
        };

        _bindSSEHandlers(_globalSrc);
      }

      function _bindSSEHandlers(src) {
        src.addEventListener('notifications', function(e) {
          var d = JSON.parse(e.data);
          var dot          = document.getElementById('notif-dot');
          var sidebarBadge = document.getElementById('sidebar-notif-count');
          var bottomBadge  = document.getElementById('bottom-notif-badge');
          var hasUnread    = d.count > 0;
          var countLabel   = d.count > 99 ? '99+' : String(d.count);
          if (dot) { dot.textContent = hasUnread ? countLabel : ''; dot.style.display = hasUnread ? 'inline-flex' : 'none'; }
          [sidebarBadge, bottomBadge].forEach(function(el) {
            if (!el) return;
            el.textContent = countLabel;
            el.style.display = hasUnread ? 'inline-block' : 'none';
          });
          var list = document.getElementById('notif-list');
          if (list && d.recent) {
            list.innerHTML = d.recent.length
              ? d.recent.map(function(n) { return renderNotifItem(n); }).join('')
              : '<div class="notif-item text-muted text-center" style="padding:24px 18px">No new notifications</div>';
          }
        });

        src.addEventListener('dm_unread', function(e) {
          var d = JSON.parse(e.data);
          var cnt = d.count || 0;
          var dmLabel = cnt > 99 ? '99+' : String(cnt);
          ['bottom-dm-badge','sidebar-dm-badge'].forEach(function(id) {
            var el = document.getElementById(id);
            if (!el) return;
            el.textContent = dmLabel;
            el.style.display = cnt > 0 ? 'inline-flex' : 'none';
          });
        });

        src.addEventListener('group_unread', function(e) {
          var d = JSON.parse(e.data);
          var cnt = d.count || 0;
          var badge = document.getElementById('sidebar-grp-badge');
          if (badge) { badge.textContent = cnt; badge.style.display = cnt > 0 ? 'inline-flex' : 'none'; }
        });

        src.addEventListener('activity', function(e) {
          var d = JSON.parse(e.data);
          var feed = document.getElementById('activity-feed');
          if (feed && d.items && d.items.length) {
            d.items.forEach(function(item) {
              var row = document.createElement('div');
              row.className = 'activity-row';
              row.innerHTML = '<span class="act-worker">@' + escapeHtml(item.worker) +
                '</span> completed <span class="act-ad">' + escapeHtml(item.ad) +
                '</span> <span class="act-reward">+$' + item.reward.toFixed(2) +
                '</span> <span class="act-time">' + escapeHtml(item.time) + '</span>';
              feed.insertBefore(row, feed.firstChild);
              while (feed.children.length > 10) feed.removeChild(feed.lastChild);
            });
          }
        });
      }  // end _bindSSEHandlers

      _openSSE();
      window.addEventListener('beforeunload', function() { if (_globalSrc) _globalSrc.close(); });
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

// ── Poll countdown timers ─────────────────────────────────────────────────
(function() {
  function fmtCountdown(sec) {
    if (sec <= 0) return 'Poll ended';
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h left';
    if (h > 0) return h + 'h ' + m + 'm left';
    return m + 'm left';
  }
  function initCountdowns() {
    document.querySelectorAll('.poll-countdown[data-expires-in]').forEach(function(el) {
      if (el._timer) return;
      var secs = parseInt(el.getAttribute('data-expires-in'), 10) || 0;
      el.textContent = fmtCountdown(secs);
      el._timer = setInterval(function() {
        secs--;
        if (secs <= 0) {
          el.textContent = 'Poll ended';
          clearInterval(el._timer);
        } else {
          el.textContent = fmtCountdown(secs);
        }
      }, 1000);
    });
  }
  initCountdowns();
  // Re-run when new posts are appended (infinite scroll)
  document.addEventListener('postsAppended', initCountdowns);
})();

// ── Media lightbox — image & video with pinch-to-zoom ────────────────────
(function() {
  var _lb = null;

  function openMediaLightbox(src, isVideo) {
    if (_lb) closeLb();
    isVideo = !!isVideo;

    var lb = document.createElement('div');
    lb.className = 'post-lightbox';
    lb.setAttribute('role', 'dialog');
    lb.setAttribute('aria-modal', 'true');
    lb.style.cssText = 'cursor:default';

    var closeBtn = document.createElement('button');
    closeBtn.className = 'lb-close-btn';
    closeBtn.innerHTML = '&times;';
    closeBtn.setAttribute('aria-label', 'Close');
    closeBtn.addEventListener('click', closeLb);

    var wrapper = document.createElement('div');
    wrapper.className = 'lb-wrapper';

    var media;
    if (isVideo) {
      media = document.createElement('video');
      media.src = src;
      media.controls = true;
      media.autoplay = true;
      media.playsInline = true;
      media.style.cssText = 'max-width:94vw;max-height:88vh;border-radius:10px;display:block;touch-action:none';
    } else {
      media = document.createElement('img');
      media.src = src;
      media.alt = '';
      media.draggable = false;
      media.style.cssText = 'max-width:94vw;max-height:88vh;object-fit:contain;border-radius:10px;display:block;touch-action:none;user-select:none;-webkit-user-select:none';
    }

    wrapper.appendChild(media);
    lb.appendChild(closeBtn);
    lb.appendChild(wrapper);
    document.body.appendChild(lb);
    _lb = lb;

    // Close on backdrop click (not on media)
    lb.addEventListener('click', function(e) {
      if (e.target === lb) closeLb();
    });

    // Keyboard close
    function kClose(e) {
      if (e.key === 'Escape') { closeLb(); document.removeEventListener('keydown', kClose); }
    }
    document.addEventListener('keydown', kClose);

    // ── Pinch-to-zoom + pan + swipe-down (images only) ────────────────
    if (!isVideo) {
      var scale = 1, tx = 0, ty = 0;
      var ptrs = {};
      var pinchBase = 0, scaleBase = 1;
      var panBase = null;
      var lastTap = 0;
      var swipeStartY = 0, swipePossible = false;

      function applyXform() {
        media.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(' + scale + ')';
      }

      function ptrDist() {
        var pts = Object.values(ptrs);
        return Math.hypot(pts[1].clientX - pts[0].clientX, pts[1].clientY - pts[0].clientY);
      }

      media.addEventListener('pointerdown', function(e) {
        media.setPointerCapture(e.pointerId);
        ptrs[e.pointerId] = e;
        var n = Object.keys(ptrs).length;
        if (n === 2) {
          pinchBase = ptrDist();
          scaleBase = scale;
          panBase = null;
        } else if (n === 1) {
          var now = Date.now();
          if (now - lastTap < 300) {
            // double-tap: toggle zoom
            if (scale > 1) { scale = 1; tx = 0; ty = 0; } else { scale = 3; }
            applyXform();
          }
          lastTap = now;
          panBase = { x: e.clientX, y: e.clientY, tx: tx, ty: ty };
          swipeStartY = e.clientY;
          swipePossible = scale <= 1;
        }
      });

      media.addEventListener('pointermove', function(e) {
        ptrs[e.pointerId] = e;
        var n = Object.keys(ptrs).length;
        if (n >= 2) {
          scale = Math.max(1, Math.min(6, scaleBase * ptrDist() / pinchBase));
          swipePossible = false;
          applyXform();
        } else if (n === 1 && panBase) {
          if (scale > 1) {
            tx = panBase.tx + (e.clientX - panBase.x);
            ty = panBase.ty + (e.clientY - panBase.y);
            applyXform();
          } else if (swipePossible) {
            var dy = e.clientY - swipeStartY;
            if (dy > 0) {
              lb.style.opacity = Math.max(0, 1 - dy / 220).toFixed(2);
              media.style.transform = 'translateY(' + dy + 'px)';
            }
          }
        }
      });

      media.addEventListener('pointerup', function(e) {
        var dy = e.clientY - swipeStartY;
        if (swipePossible && dy > 90) {
          closeLb(); return;
        }
        if (!swipePossible || dy <= 0) {
          lb.style.opacity = '1';
          applyXform();
        } else {
          lb.style.opacity = '1';
          applyXform();
        }
        delete ptrs[e.pointerId];
        panBase = null;
        swipePossible = false;
      });

      media.addEventListener('pointercancel', function(e) {
        delete ptrs[e.pointerId];
      });

      // Prevent context menu on long-press
      media.addEventListener('contextmenu', function(e) { e.preventDefault(); });
    }
  }

  function closeLb() {
    if (_lb) { _lb.remove(); _lb = null; }
  }

  function openPostLightbox(postId) {
    var card = document.querySelector('[data-post-id="' + postId + '"]');
    if (!card) return;
    var img  = card.querySelector('.post-media-img');
    var vid  = card.querySelector('.post-media-video');
    if (img) { openMediaLightbox(img.src, false); return; }
    if (vid) { openMediaLightbox(vid.src, true);  return; }
  }

  window.openPostLightbox  = openPostLightbox;
  window.openMediaLightbox = openMediaLightbox;
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

// ── Post interaction functions (global — work on all pages) ──────────────────

// Reaction emoji map
const REACTION_EMOJI = { fire:'🔥', heart:'❤️', laugh:'😂', target:'🎯', money:'💰' };

function toggleReactionPicker(postId, event) {
  event && event.stopPropagation();
  const picker = document.getElementById('reaction-picker-' + postId);
  if (!picker) return;
  const isOpen = picker.style.display === 'flex';
  // Close all open pickers first
  document.querySelectorAll('.reaction-picker').forEach(function(p) { p.style.display = 'none'; });
  if (!isOpen) {
    picker.style.display = 'flex';
    // Close on outside click
    setTimeout(function() {
      document.addEventListener('click', function _close(e) {
        if (!picker.contains(e.target)) {
          picker.style.display = 'none';
          document.removeEventListener('click', _close);
        }
      });
    }, 0);
  }
}
window.toggleReactionPicker = toggleReactionPicker;

async function sendReaction(postId, reaction, event) {
  event && event.stopPropagation();
  const picker = document.getElementById('reaction-picker-' + postId);
  if (picker) picker.style.display = 'none';

  var r, d;
  try {
    r = await fetch('/post/' + postId + '/react', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reaction })
    });
    d = await r.json();
  } catch (_) { showToast('Network error.', 'error'); return; }
  if (!d || !d.success) { if (d && d.error) showToast(d.error, 'error'); return; }

  const btn  = document.querySelector('#reaction-wrap-' + postId + ' .like-btn');
  const icon = document.querySelector('.reaction-icon-' + postId);
  const cnt  = document.getElementById('like-count-' + postId);

  if (d.reaction) {
    if (icon) icon.textContent = REACTION_EMOJI[d.reaction] || '❤️';
    if (btn) { btn.classList.add('liked'); btn.setAttribute('aria-pressed', 'true'); }
  } else {
    if (icon) {
      icon.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"><path d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/></svg>';
    }
    if (btn) { btn.classList.remove('liked'); btn.setAttribute('aria-pressed', 'false'); }
  }
  if (cnt) cnt.textContent = d.total || '';
}
window.sendReaction = sendReaction;

async function toggleLike(postId, btn) {
  const hasReaction = btn && btn.classList.contains('liked');
  await sendReaction(postId, hasReaction ? '' : 'heart', null);
}
window.toggleLike = toggleLike;

async function toggleBookmark(postId, btn) {
  var r, d;
  try {
    r = await fetch('/post/' + postId + '/bookmark', { method: 'POST' });
    d = await r.json();
  } catch (_) { showToast('Network error.', 'error'); return; }
  if (!d || !d.success) { showToast((d && d.error) || 'Could not bookmark.', 'error'); return; }
  const icon = btn && btn.querySelector('.action-icon svg');
  if (icon) { icon.setAttribute('fill', d.saved ? 'currentColor' : 'none'); }
  if (btn) {
    btn.classList.toggle('bookmarked', d.saved);
    btn.setAttribute('aria-pressed', String(d.saved));
  }
  showToast(d.saved ? 'Bookmarked!' : 'Removed from bookmarks');
}
window.toggleBookmark = toggleBookmark;

async function deletePost(postId) {
  if (!confirm('Delete this post?')) return;
  var r, d;
  try {
    r = await fetch('/post/' + postId + '/delete', { method: 'POST' });
    d = await r.json();
  } catch (_) { showToast('Network error.', 'error'); return; }
  if (d.success) {
    const card = document.querySelector(`[data-post-id="${postId}"]`);
    if (card) { card.style.opacity = '0'; setTimeout(() => card.remove(), 300); }
    showToast('Post deleted.');
  } else {
    showToast(d.error || 'Could not delete.', 'error');
  }
}
window.deletePost = deletePost;

/* Close every open post-menu */
function _closeAllPostMenus() {
  document.querySelectorAll('.post-menu-dropdown.open').forEach(function(m) {
    m.classList.remove('open');
    m.classList.remove('flip-up');
  });
}

/* Auto-close menu when any menu item is clicked (capture phase fires before the item's onclick) */
document.addEventListener('click', function(ev) {
  if (ev.target && ev.target.closest && ev.target.closest('.post-menu-item')) {
    _closeAllPostMenus();
  }
}, true);

function togglePostMenu(postId, e) {
  if (e) e.stopPropagation();

  var menu   = document.getElementById('post-menu-' + postId);
  if (!menu) return;

  /* Save opener button BEFORE closing — used to exclude it from _outerClick below */
  var opener  = e ? (e.currentTarget || e.target) : null;
  var wasOpen = menu.classList.contains('open');

  _closeAllPostMenus();
  if (wasOpen) return;  // was open → just closed it, done

  /* Flip menu above the button if there isn't enough room below */
  var wrap = menu.parentElement;
  if (wrap) {
    var wRect     = wrap.getBoundingClientRect();
    var itemCount = menu.querySelectorAll('.post-menu-item').length;
    if (wRect.bottom + itemCount * 44 + 16 > window.innerHeight - 8) {
      menu.classList.add('flip-up');
    }
  }

  menu.classList.add('open');

  /* Close on any click outside the menu.
     Exclude the opener button so that clicking ⋯ again doesn't immediately re-open.
     (capture phase fires before onclick, so without this exclusion wasOpen would
     already be false by the time togglePostMenu runs.) */
  function _outerClick(ev) {
    var onOpener = opener && (ev.target === opener || opener.contains(ev.target));
    if (!menu.contains(ev.target) && !onOpener) {
      _closeAllPostMenus();
      document.removeEventListener('click', _outerClick, true);
    }
  }
  setTimeout(function() {
    document.addEventListener('click', _outerClick, true);
  }, 0);
}
window.togglePostMenu = togglePostMenu;

/* Close on page scroll */
document.addEventListener('scroll', _closeAllPostMenus, { passive: true, capture: true });

function copyPostLink(postId) {
  navigator.clipboard.writeText(window.location.origin + '/post/' + postId);
  showToast('Link copied!');
}
window.copyPostLink = copyPostLink;

function reportPost(postId) {
  document.getElementById('report-target-type').value = 'post';
  document.getElementById('report-target-id').value   = postId;
  document.getElementById('report-reason').value      = '';
  document.getElementById('report-details').value     = '';
  openModal('report-modal');
}
window.reportPost = reportPost;

async function submitReport() {
  const target_type = document.getElementById('report-target-type').value;
  const target_id   = parseInt(document.getElementById('report-target-id').value);
  const reason      = document.getElementById('report-reason').value;
  const details     = document.getElementById('report-details').value.trim();
  if (!reason) return showToast('Please select a reason.', 'error');
  try {
    const r = await fetch('/api/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_type, target_id, reason, details }),
    });
    const d = await r.json();
    if (d.success) {
      closeModal('report-modal');
      showToast('🚩 Report submitted. Thank you.');
    } else {
      showToast(d.error || 'Could not submit report.', 'error');
    }
  } catch (_) { showToast('Network error.', 'error'); }
}
window.submitReport = submitReport;

function openEditPost(postId, body) {
  // Use base.html's canonical edit modal (ep-* IDs)
  var postIdEl = document.getElementById('ep-post-id');
  var bodyEl   = document.getElementById('ep-body');
  if (!postIdEl || !bodyEl) return;
  postIdEl.value = postId;
  bodyEl.value   = body || '';
  var charsEl = document.getElementById('ep-chars');
  if (charsEl) charsEl.textContent = 500 - (body || '').length;
  openModal('edit-post-modal');
}
window.openEditPost = openEditPost;

// submitEditPost is an alias for saveEditPost which lives in base.html
function submitEditPost() {
  if (typeof saveEditPost === 'function') saveEditPost();
}
window.submitEditPost = submitEditPost;

async function pinPost(postId, btn) {
  var r, d;
  try {
    r = await fetch('/post/' + postId + '/pin', { method: 'POST' });
    d = await r.json();
  } catch (_) { showToast('Network error.', 'error'); return; }
  if (d && d.success) {
    showToast(d.pinned ? '📌 Pinned to profile!' : 'Unpinned.');
    var item = btn && btn.closest('.post-menu-item');
    if (item) item.innerHTML = item.innerHTML.replace(
      d.pinned ? 'Pin to profile' : 'Unpin post',
      d.pinned ? 'Unpin post'    : 'Pin to profile'
    );
  } else {
    showToast((d && d.error) || 'Could not pin post.', 'error');
  }
}
window.pinPost = pinPost;

// ── Repost / Quote ────────────────────────────────────────────────────────────
var _repostTargetId  = null;
var _repostWasActive = false;

function openRepostModal(postId, isReposted) {
  var targetIdEl = document.getElementById('repost-target-id');
  if (!targetIdEl) return;
  targetIdEl.value   = postId;
  _repostTargetId    = postId;
  window._repostTargetId = postId;
  _repostWasActive   = !!isReposted;
  var titleEl   = document.getElementById('repost-modal-title');
  var actionsEl = document.getElementById('repost-modal-actions');
  var REPOST_SVG = '<svg class="i-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 014-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 01-4 4H3"/></svg> ';
  var QUOTE_SVG  = '<svg class="i-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg> ';
  var UNDO_SVG   = '<svg class="i-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 14 4 9 9 4"/><path d="M20 20v-7a4 4 0 00-4-4H4"/></svg> ';
  if (isReposted) {
    if (titleEl) titleEl.textContent = 'You reposted this';
    if (actionsEl) actionsEl.innerHTML =
      '<button class="btn btn-outline btn-block" onclick="undoRepost()" style="color:var(--red,#e53e3e)">' + UNDO_SVG + 'Undo repost</button>' +
      '<button class="btn btn-ghost btn-block" onclick="openQuoteModal()">' + QUOTE_SVG + 'Quote &amp; comment instead</button>';
  } else {
    if (titleEl) titleEl.textContent = 'Repost';
    if (actionsEl) actionsEl.innerHTML =
      '<button class="btn btn-outline btn-block" onclick="quickRepost()">' + REPOST_SVG + 'Repost</button>' +
      '<button class="btn btn-ghost btn-block" onclick="openQuoteModal()">' + QUOTE_SVG + 'Quote &amp; comment</button>';
  }
  openModal('repost-modal');
}
window.openRepostModal = openRepostModal;

async function quickRepost() {
  var fd = new FormData();
  fd.append('repost_of_id', _repostTargetId);
  fd.append('body', '');
  var d = null;
  try {
    var r = await fetch('/post', { method: 'POST', body: fd });
    d = await r.json();
  } catch (_) {
    closeModal('repost-modal');
    showToast('Network error', 'error');
    return;
  }
  closeModal('repost-modal');
  setTimeout(function() {
    if (d && d.success) {
      _updateRepostButton(_repostTargetId, true, 1);
      showToast('Reposted! ♻️');
    } else {
      showToast((d && d.error) ? d.error : 'Could not repost.', 'error');
    }
  }, 180);
}
window.quickRepost = quickRepost;

async function undoRepost() {
  var d = null;
  try {
    var r = await fetch('/post/' + _repostTargetId + '/unrepost', { method: 'POST' });
    d = await r.json();
  } catch (_) {
    closeModal('repost-modal');
    showToast('Network error', 'error');
    return;
  }
  closeModal('repost-modal');
  setTimeout(function() {
    if (d && d.success) {
      _updateRepostButton(_repostTargetId, false, -(d.removed || 1));
      showToast('Repost removed.');
    } else {
      showToast((d && d.error) ? d.error : 'Could not remove repost.', 'error');
    }
  }, 180);
}
window.undoRepost = undoRepost;

function _updateRepostButton(postId, isReposted, delta) {
  var cnt = document.getElementById('repost-count-' + postId);
  if (cnt) {
    var next = Math.max(0, parseInt(cnt.textContent || '0') + delta);
    cnt.textContent = next || '';
  }
  document.querySelectorAll('[data-post-id="' + postId + '"] .repost-btn').forEach(function(btn) {
    btn.classList.toggle('reposted', isReposted);
    btn.setAttribute('aria-pressed', isReposted ? 'true' : 'false');
    btn.setAttribute('onclick', 'openRepostModal(' + postId + ',' + isReposted + ')');
  });
}

function openQuoteModal() {
  closeModal('repost-modal');
  openModal('quote-modal');
}
window.openQuoteModal = openQuoteModal;

async function submitQuote() {
  var postId = document.getElementById('repost-target-id').value;
  var body   = document.getElementById('quote-input').value.trim();
  var fd     = new FormData();
  fd.append('repost_of_id', postId);
  fd.append('body', body);
  var r = await fetch('/post', { method: 'POST', body: fd });
  var d = await r.json();
  closeModal('quote-modal');
  if (d.success) {
    if (typeof prependPost === 'function') prependPost(d.post);
    showToast('Quoted!');
  } else { showToast(d.error || 'Error.', 'error'); }
}
window.submitQuote = submitQuote;

// ── Reply ─────────────────────────────────────────────────────────────────────
var _replyPhotoData = null;

function updateReplyChars(el) {
  var c = 200 - el.value.length;
  var charsEl = document.getElementById('reply-chars');
  var submitBtn = document.getElementById('reply-submit-btn');
  if (charsEl) charsEl.textContent = c;
  if (submitBtn) submitBtn.disabled = (el.value.trim().length === 0 && !_replyPhotoData);
  el.style.height = 'auto';
  el.style.height = Math.min(120, el.scrollHeight) + 'px';
}
window.updateReplyChars = updateReplyChars;

function handleReplyPhoto(input) {
  var f = input.files && input.files[0];
  if (!f) return;
  if (f.size > 5 * 1024 * 1024) { showToast('Image too large (max 5MB)', 'error'); return; }
  var reader = new FileReader();
  reader.onload = function(e) {
    _replyPhotoData = { dataUrl: e.target.result, mime: f.type };
    var img = document.getElementById('reply-photo-img');
    var preview = document.getElementById('reply-photo-preview');
    var btn = document.getElementById('reply-submit-btn');
    if (img) img.src = e.target.result;
    if (preview) preview.style.display = 'block';
    if (btn) btn.disabled = false;
  };
  reader.readAsDataURL(f);
}
window.handleReplyPhoto = handleReplyPhoto;

function clearReplyPhoto() {
  _replyPhotoData = null;
  var inp = document.getElementById('reply-photo-input');
  var preview = document.getElementById('reply-photo-preview');
  var ta = document.getElementById('reply-input');
  var btn = document.getElementById('reply-submit-btn');
  if (inp) inp.value = '';
  if (preview) preview.style.display = 'none';
  if (btn) btn.disabled = !ta || ta.value.trim().length === 0;
}
window.clearReplyPhoto = clearReplyPhoto;

async function openReplyModal(postId, authorUsername) {
  var postIdEl = document.getElementById('reply-post-id');
  if (!postIdEl) return;
  postIdEl.value = postId;
  var ctx = document.getElementById('reply-context-card');
  if (ctx) ctx.innerHTML = '<span style="color:var(--muted)">Replying to</span> <strong>@' + authorUsername + '</strong>';
  var ta = document.getElementById('reply-input');
  if (ta) ta.value = '';
  var charsEl = document.getElementById('reply-chars');
  if (charsEl) charsEl.textContent = '200';
  var submitBtn = document.getElementById('reply-submit-btn');
  if (submitBtn) submitBtn.disabled = true;
  if (typeof clearReplyPhoto === 'function') clearReplyPhoto();
  openModal('reply-modal');

  var listEl = document.getElementById('reply-existing-list');
  if (listEl) {
    listEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">Loading replies…</div>';
    try {
      var r = await fetch('/api/post/' + postId + '/replies');
      var d = await r.json();
      if (d.success && d.replies && d.replies.length) {
        listEl.innerHTML = d.replies.map(function(rp) {
          var av = rp.author.avatar_url
            ? '<img src="' + rp.author.avatar_url + '" style="width:32px;height:32px;border-radius:50%;object-fit:cover;flex-shrink:0">'
            : '<div class="avatar-placeholder" style="width:32px;height:32px;border-radius:50%;flex-shrink:0"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 12a5 5 0 100-10 5 5 0 000 10zm0 2c-4.418 0-8 2.686-8 6v2h16v-2c0-3.314-3.582-6-8-6z"/></svg></div>';
          var mediaHtml = rp.media_url
            ? '<img src="' + rp.media_url + '" style="max-width:100%;border-radius:10px;margin-top:6px">'
            : '';
          return '<div style="display:flex;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)">' +
            av + '<div style="flex:1;min-width:0">' +
            '<div style="font-weight:700;font-size:13.5px">' + (rp.author.display_name || rp.author.username) +
            ' <span style="color:var(--muted);font-weight:400">@' + rp.author.username + '</span></div>' +
            '<div style="font-size:14px;margin-top:2px;word-wrap:break-word">' + (rp.body || '') + '</div>' +
            mediaHtml + '</div></div>';
        }).join('');
      } else {
        listEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">No replies yet. Be the first!</div>';
      }
    } catch (_) {
      listEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">Could not load replies</div>';
    }
  }
  setTimeout(function() { if (ta) ta.focus(); }, 180);
}
window.openReplyModal = openReplyModal;

async function submitReply() {
  var postId = document.getElementById('reply-post-id').value;
  var ta = document.getElementById('reply-input');
  var body = ta ? ta.value.trim() : '';
  if (!postId || (!body && !_replyPhotoData)) return;
  var btn = document.getElementById('reply-submit-btn');
  if (btn) btn.disabled = true;
  var fd = new FormData();
  fd.append('body', body);
  fd.append('reply_to_id', postId);
  if (_replyPhotoData) {
    fd.append('media_data', _replyPhotoData.dataUrl);
    fd.append('media_mime', _replyPhotoData.mime);
  }
  try {
    var r = await fetch('/post', { method: 'POST', body: fd });
    var d = await r.json();
    if (d.success) {
      closeModal('reply-modal');
      setTimeout(function() { showToast('Reply posted!'); }, 150);
      var card = document.querySelector('[data-post-id="' + postId + '"]');
      if (card) {
        var cnt = card.querySelector('.reply-btn .action-count');
        if (cnt) cnt.textContent = (parseInt(cnt.textContent) || 0) + 1;
      }
    } else {
      showToast(d.error || 'Could not post reply.', 'error');
      if (btn) btn.disabled = false;
    }
  } catch (_) {
    showToast('Network error', 'error');
    if (btn) btn.disabled = false;
  }
}
window.submitReply = submitReply;

// ── Follow user ───────────────────────────────────────────────────────────────
async function followUser(username, btn) {
  if (btn) btn.disabled = true;
  var r, d;
  try {
    r = await fetch('/user/' + username + '/follow', { method: 'POST' });
    d = await r.json();
  } catch (_) {
    showToast('Network error.', 'error');
    if (btn) btn.disabled = false;
    return;
  }
  if (d && d.success) {
    if (btn) {
      btn.textContent = d.following ? 'Following' : 'Follow';
      btn.classList.toggle('btn-outline', !d.following);
      btn.classList.toggle('btn-ghost',   d.following);
    }
  } else {
    showToast((d && d.error) || 'Could not follow.', 'error');
  }
  if (btn) btn.disabled = false;
}
window.followUser = followUser;

// ── Subscribe to creator (from locked post cards) ────────────────────────────
async function subscribeToCreator(username, btn) {
  const orig = btn.textContent;
  btn.textContent = 'Subscribing…';
  btn.disabled = true;
  try {
    const r = await fetch('/user/' + username + '/subscribe', { method: 'POST' });
    const d = await r.json();
    if (d.success) {
      showToast(d.message || 'Subscribed! 🎉');
      setTimeout(function() { location.reload(); }, 800);
    } else {
      showToast(d.error || 'Could not subscribe.', 'error');
      btn.textContent = orig;
      btn.disabled = false;
    }
  } catch (_) {
    showToast('Network error.', 'error');
    btn.textContent = orig;
    btn.disabled = false;
  }
}
window.subscribeToCreator = subscribeToCreator;

// ── Post view tracking (Intersection Observer) — runs on every page ───────────
(function() {
  if (!('IntersectionObserver' in window)) return;
  const viewed = new Set();
  const observer = new IntersectionObserver(function(entries) {
    entries.forEach(function(entry) {
      if (!entry.isIntersecting) return;
      const card = entry.target;
      const id   = card.dataset.postId;
      if (!id || viewed.has(id)) return;
      viewed.add(id);
      observer.unobserve(card);
      setTimeout(function() {
        fetch('/api/post/' + id + '/view', { method: 'POST' })
          .then(function(r) { return r.ok ? r.json() : null; })
          .then(function(d) {
            if (d && d.ok) {
              const vcEl = document.getElementById('view-count-' + id)
                        || (card.querySelector('.views-btn .action-count'));
              if (vcEl) {
                const cur = parseInt(vcEl.textContent, 10) || 0;
                vcEl.textContent = cur + 1 || 1;
              }
            }
          })
          .catch(function() {});
      }, 1000);
    });
  }, { threshold: 0.6 });

  function _observePostCards() {
    document.querySelectorAll('.post-card[data-post-id]').forEach(function(card) {
      observer.observe(card);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _observePostCards);
  } else {
    _observePostCards();
  }
  // Expose so dynamically added cards can be observed
  window.observeNewPostCards = _observePostCards;
})();

// ── Compose autocomplete — # hashtags and @ mentions ─────────────────────────
(function() {
  var _acTimer  = null;
  var _acTarget = null;
  var _acPrefix = null;

  // Create the floating dropdown once and attach to body
  var _dd = document.createElement('div');
  _dd.id = 'ac-dropdown';
  _dd.style.cssText = [
    'position:fixed;z-index:99999',
    'background:var(--surface)',
    'border:1px solid var(--border)',
    'border-radius:10px',
    'box-shadow:0 8px 32px rgba(0,0,0,.22)',
    'min-width:220px;max-width:320px',
    'overflow:hidden;display:none',
  ].join(';');
  document.body.appendChild(_dd);

  function _closeAC() {
    _dd.style.display = 'none';
    _dd.classList.remove('open');
    _acTarget = null;
    _acPrefix = null;
    clearTimeout(_acTimer);
  }
  window._closeAC = _closeAC;

  function _attachToTextarea(ta) {
    if (!ta || ta.dataset.acBound) return;
    ta.dataset.acBound = '1';

    ta.addEventListener('input', function() {
      var val = ta.value;
      var pos = ta.selectionStart;

      // Walk backward to find the nearest whitespace or line break
      var i = pos - 1;
      while (i > 0 && val[i] !== '@' && val[i] !== '#' && !/[\s\n]/.test(val[i - 1])) i--;

      var ch = val[i];
      if (ch !== '@' && ch !== '#') { _closeAC(); return; }
      var word = val.slice(i + 1, pos);
      if (word.length < 1) { _closeAC(); return; }

      _acTarget = ta;
      _acPrefix = ch;

      clearTimeout(_acTimer);
      _acTimer = setTimeout(function() { _fetchAC(word, ch, ta); }, 200);
    });

    ta.addEventListener('blur', function() {
      // Slight delay so mousedown on an item fires before blur hides the dropdown
      setTimeout(_closeAC, 220);
    });
  }

  async function _fetchAC(q, prefix, ta) {
    try {
      var r = await fetch('/api/search/autocomplete?q=' + encodeURIComponent(q));
      var d = await r.json();
      var items = prefix === '@' ? (d.users || []) : (d.tags || []);
      if (!items.length) { _closeAC(); return; }
      _showDD(items, prefix, ta);
    } catch (_) { _closeAC(); }
  }

  function _showDD(items, prefix, ta) {
    var rect = ta.getBoundingClientRect();

    _dd.innerHTML = items.slice(0, 6).map(function(item) {
      if (prefix === '@') {
        var av = item.avatar_url
          ? '<img src="' + escapeHtml(item.avatar_url) + '" style="width:28px;height:28px;border-radius:50%;object-fit:cover;flex-shrink:0">'
          : '<div style="width:28px;height:28px;border-radius:50%;background:var(--surface-2);flex-shrink:0"></div>';
        return '<div class="ac-item" data-val="' + escapeHtml(item.username) + '" data-prefix="@"'
          + ' style="display:flex;align-items:center;gap:10px;padding:9px 14px;cursor:pointer;font-size:13.5px">'
          + av
          + '<div><div style="font-weight:700">' + escapeHtml(item.display_name || item.username) + '</div>'
          + '<div style="font-size:11.5px;color:var(--muted)">@' + escapeHtml(item.username) + '</div></div></div>';
      } else {
        return '<div class="ac-item" data-val="' + escapeHtml(item.name) + '" data-prefix="#"'
          + ' style="display:flex;align-items:center;gap:10px;padding:9px 14px;cursor:pointer;font-size:13.5px">'
          + '<div style="width:28px;height:28px;border-radius:50%;background:rgba(29,155,240,.12);'
          + 'display:flex;align-items:center;justify-content:center;color:#1d9bf0;flex-shrink:0;font-weight:700">#</div>'
          + '<div><div style="font-weight:700">#' + escapeHtml(item.name) + '</div>'
          + '<div style="font-size:11.5px;color:var(--muted)">' + (item.cnt || 0) + ' posts</div></div></div>';
      }
    }).join('');

    _dd.querySelectorAll('.ac-item').forEach(function(el) {
      el.addEventListener('mousedown', function(e) {
        e.preventDefault();
        _insertItem(el.dataset.val, el.dataset.prefix);
      });
    });

    _dd.style.display = 'block';
    _dd.classList.add('open');
    var ddLeft = Math.min(rect.left, window.innerWidth - 340);
    var ddTop  = rect.bottom + 4;
    // If below viewport, show above the textarea instead
    if (ddTop + 200 > window.innerHeight) ddTop = rect.top - 210;
    _dd.style.left = Math.max(8, ddLeft) + 'px';
    _dd.style.top  = ddTop + 'px';
  }

  function _insertItem(value, prefix) {
    if (!_acTarget) return;
    var ta  = _acTarget;
    var val = ta.value;
    var pos = ta.selectionStart;

    // Find the start of the current @/# token
    var i = pos - 1;
    while (i >= 0 && !/[\s\n]/.test(val[i])) i--;
    i++; // first char of token

    var before = val.slice(0, i);
    var after  = val.slice(pos);
    var insert = prefix + value + ' ';
    ta.value   = before + insert + after;
    var newPos = before.length + insert.length;
    ta.setSelectionRange(newPos, newPos);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    _closeAC();
    ta.focus();
  }

  // Attach to any compose-style textarea found now or after modals open
  function _attachAll() {
    document.querySelectorAll('#compose-input, #gc-body, #reply-input').forEach(_attachToTextarea);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _attachAll);
  } else {
    _attachAll();
  }
  // Re-attach when a modal opens (gc-body may not exist at page load)
  document.addEventListener('focusin', function(e) {
    var ta = e.target;
    if (ta && ta.tagName === 'TEXTAREA') _attachToTextarea(ta);
  });
})();

/* ── Post edit history ───────────────────────────────────────────────────── */
async function openEditHistory(postId) {
  const r = await fetch('/api/post/' + postId + '/edits');
  const d = await r.json();
  if (!d.success || !d.edits.length) { showToast('No edit history.'); return; }
  const text = d.edits.map(function(e) {
    return e.edited_at.slice(0, 16) + ':\n' + (e.body || '(empty)');
  }).join('\n\n---\n\n');
  alert('Edit history:\n\n' + text);
}
window.openEditHistory = openEditHistory;

/* ── Sensitive content reveal ────────────────────────────────────────────── */
function revealSensitive(postId) {
  var wrap = document.getElementById('sb-' + postId);
  var overlay = wrap && wrap.nextElementSibling;
  if (wrap) { wrap.style.filter = 'none'; wrap.style.pointerEvents = ''; }
  if (overlay) overlay.style.display = 'none';
}
window.revealSensitive = revealSensitive;

/* ── Compose: sensitive toggle ───────────────────────────────────────────── */
function toggleSensitive() {
  var inp = document.getElementById('compose-sensitive');
  var btn = document.getElementById('sensitive-btn');
  if (!inp) return;
  var on = inp.value === '0';
  inp.value = on ? '1' : '0';
  if (btn) {
    btn.style.color = on ? 'var(--red)' : '';
    btn.title = on ? 'Marked as sensitive (click to remove)' : 'Mark as sensitive';
  }
}
window.toggleSensitive = toggleSensitive;
