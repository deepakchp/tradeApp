/* ═══════════════════════════════════════════════════════════════
   VRP Trading System — UI JavaScript
   ═══════════════════════════════════════════════════════════════ */

const VRP = {

  // ── AJAX Helper ──────────────────────────────────────────────
  async fetchJSON(url, options = {}) {
    try {
      const resp = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options,
      });
      const data = await resp.json();
      if (!resp.ok) {
        data._status = resp.status;
        return { ok: false, status: resp.status, data };
      }
      return { ok: true, status: resp.status, data };
    } catch (err) {
      console.error('Fetch error:', err);
      return { ok: false, status: 0, data: { error: err.message } };
    }
  },

  // ── Auto-Refresh ─────────────────────────────────────────────
  autoRefresh(callback, intervalMs = 15000) {
    callback(); // immediate first call
    const id = setInterval(callback, intervalMs);
    return {
      stop: () => clearInterval(id),
      id,
    };
  },

  // ── Toast Notifications ──────────────────────────────────────
  showToast(message, type = 'info') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const colorMap = {
      success: 'bg-success',
      error: 'bg-danger',
      warning: 'bg-warning text-dark',
      info: 'bg-info text-dark',
    };

    const iconMap = {
      success: 'bi-check-circle-fill',
      error: 'bi-x-circle-fill',
      warning: 'bi-exclamation-triangle-fill',
      info: 'bi-info-circle-fill',
    };

    const toastEl = document.createElement('div');
    toastEl.className = `toast align-items-center ${colorMap[type] || colorMap.info} border-0`;
    toastEl.setAttribute('role', 'alert');
    toastEl.innerHTML = `
      <div class="d-flex">
        <div class="toast-body">
          <i class="bi ${iconMap[type] || iconMap.info} me-2"></i>${message}
        </div>
        <button type="button" class="btn-close btn-close-white me-2 m-auto"
                data-bs-dismiss="toast"></button>
      </div>`;

    container.appendChild(toastEl);
    const toast = new bootstrap.Toast(toastEl, { delay: 4000 });
    toast.show();
    toastEl.addEventListener('hidden.bs.toast', () => toastEl.remove());
  },

  // ── Confirmation Modal ───────────────────────────────────────
  confirmAction(title, message, onConfirm) {
    const modal = document.getElementById('confirmModal');
    if (!modal) return;

    modal.querySelector('.modal-title').textContent = title;
    modal.querySelector('.modal-body').innerHTML = message;
    const confirmBtn = modal.querySelector('#confirmBtn');

    const handler = () => {
      onConfirm();
      confirmBtn.removeEventListener('click', handler);
      bootstrap.Modal.getInstance(modal).hide();
    };

    confirmBtn.replaceWith(confirmBtn.cloneNode(true));
    modal.querySelector('#confirmBtn').addEventListener('click', handler);
    new bootstrap.Modal(modal).show();
  },

  // ── Formatters ───────────────────────────────────────────────
  formatCurrency(value) {
    if (value == null || isNaN(value)) return '--';
    const abs = Math.abs(value);
    let formatted;
    if (abs >= 1e7) {
      formatted = (value / 1e7).toFixed(2) + ' Cr';
    } else if (abs >= 1e5) {
      formatted = (value / 1e5).toFixed(2) + ' L';
    } else {
      formatted = value.toLocaleString('en-IN', { maximumFractionDigits: 0 });
    }
    return '\u20B9' + formatted;
  },

  formatPnl(value, el) {
    if (value == null || isNaN(value)) {
      if (el) el.className = 'pnl-zero';
      return '--';
    }
    const cls = value > 0 ? 'pnl-positive' : value < 0 ? 'pnl-negative' : 'pnl-zero';
    if (el) el.className = cls;
    const sign = value > 0 ? '+' : '';
    return sign + VRP.formatCurrency(value);
  },

  formatPercent(value) {
    if (value == null || isNaN(value)) return '--';
    const sign = value > 0 ? '+' : '';
    return sign + value.toFixed(1) + '%';
  },

  pnlClass(value) {
    return value > 0 ? 'pnl-positive' : value < 0 ? 'pnl-negative' : 'pnl-zero';
  },

  stateBadgeClass(state) {
    const map = {
      ACTIVE: 'badge-active',
      ADJUSTING: 'badge-adjusting',
      CLOSING: 'badge-closing',
      CLOSED: 'badge-closed',
      PENDING: 'badge-pending',
    };
    return map[state] || 'bg-secondary';
  },

  limitClass(ok) {
    return ok ? 'limit-ok' : 'limit-breach';
  },

  // ── Sidebar Toggle (Mobile) ──────────────────────────────────
  initSidebar() {
    const toggle = document.querySelector('.sidebar-toggle');
    const sidebar = document.querySelector('.sidebar');
    if (toggle && sidebar) {
      toggle.addEventListener('click', () => sidebar.classList.toggle('show'));
      document.addEventListener('click', (e) => {
        if (!sidebar.contains(e.target) && !toggle.contains(e.target)) {
          sidebar.classList.remove('show');
        }
      });
    }
  },

  // ── Update System Status Badges ──────────────────────────────
  async updateStatusBadges() {
    const res = await VRP.fetchJSON('/api/v1/health');
    if (!res.ok) return;
    const d = res.data;

    const ksBadge = document.getElementById('ks-badge');
    const cbBadge = document.getElementById('cb-badge');
    const brokerBadge = document.getElementById('broker-badge');

    if (ksBadge) {
      ksBadge.textContent = d.kill_switch_active ? 'KILL SWITCH ACTIVE' : 'ARMED';
      ksBadge.className = 'badge ' + (d.kill_switch_active ? 'bg-danger' : 'bg-success');
    }
    if (cbBadge) {
      cbBadge.textContent = d.circuit_breaker ? 'CIRCUIT BREAKER ON' : 'NORMAL';
      cbBadge.className = 'badge ' + (d.circuit_breaker ? 'bg-warning text-dark' : 'bg-secondary');
    }
    if (brokerBadge) {
      if (d.broker_connected) {
        brokerBadge.textContent = 'KITE CONNECTED';
        brokerBadge.className = 'badge broker-badge-connected';
        brokerBadge.style.cursor = 'pointer';
        brokerBadge.title = 'Click to disconnect from Kite';
        brokerBadge.onclick = function () {
          VRP.confirmAction('Disconnect Kite', 'Are you sure you want to disconnect from Kite? Live data will stop.', async function () {
            const res = await VRP.fetchJSON('/api/v1/broker/disconnect', { method: 'POST' });
            if (res.ok) {
              VRP.showToast('Disconnected from Kite', 'warning');
              VRP.updateStatusBadges();
            } else {
              VRP.showToast('Failed to disconnect', 'error');
            }
          });
        };
      } else {
        brokerBadge.textContent = 'KITE DISCONNECTED';
        brokerBadge.className = 'badge broker-badge-disconnected';
        brokerBadge.style.cursor = 'pointer';
        brokerBadge.title = 'Click to log in to Kite';
        brokerBadge.onclick = async function () {
          const loginRes = await VRP.fetchJSON('/api/v1/broker/login');
          if (loginRes.ok && loginRes.data.login_url) {
            window.open(loginRes.data.login_url, '_blank');
          } else {
            VRP.showToast('Could not get Kite login URL', 'error');
          }
        };
      }
    }
  },

  // ── Init ─────────────────────────────────────────────────────
  init() {
    VRP.initSidebar();
    VRP.updateStatusBadges();
    setInterval(VRP.updateStatusBadges, 30000);
  },
};

document.addEventListener('DOMContentLoaded', VRP.init);
