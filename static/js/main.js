/* =========================================================================
   DUYS Boost — Client JS (shared utilities)
   All wiring is done via addEventListener on DOMContentLoaded so it works
   regardless of script-tag position.
   ========================================================================= */
(function () {
  'use strict';

  // ── Utility: escape HTML ───────────────────────────────────────────────
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
          el.textContent = d.theme === 'dark' ? '☀️' : '🌙';
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
        if (dot) dot.style.display = hasUnread ? 'block' : 'none';
        [sidebarBadge, bottomBadge].forEach(function (el) {
          if (!el) return;
          el.textContent = d.count;
          el.style.display = hasUnread ? 'inline-block' : 'none';
        });

        const list = document.getElementById('notif-list');
        if (list) {
          if (!d.recent || !d.recent.length) {
            list.innerHTML = '<div class="notif-item text-muted text-center" style="padding:24px 18px">No new notifications</div>';
          } else {
            list.innerHTML = d.recent.map(function (n) {
              return '<div class="notif-item"><div>' + escapeHtml(n.msg) +
                     '</div><div class="notif-time">' + escapeHtml(n.time) + '</div></div>';
            }).join('');
          }
        }
      } catch (_err) { /* silent */ }
    }

    // Only poll / load if user is logged in (notif btn only exists when logged in)
    if (notifBtn) {
      loadNotifications();
      setInterval(loadNotifications, 30000);
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
