(function () {
  'use strict';

  const IMPORTANT_EMAIL_INTENT = /\b(?:most|top|highest)\s+(?:important|priority)\s+(?:e-?mail|mail|message)\b/i;
  const HOLD_KEY = '__mailmateOverviewImportantEmailHold';
  const PATCH_MARK = '__mailmateForkUxPatchV1';

  function currentPage() {
    return window.MailmateContext?.snapshot?.().page
      || document.querySelector('.nav-tab.active')?.dataset.tab
      || 'overview';
  }

  function isOverviewImportantEmailIntent(prompt) {
    return currentPage() === 'overview' && IMPORTANT_EMAIL_INTENT.test(String(prompt || ''));
  }

  function armOverviewEmailHold(prompt) {
    if (!isOverviewImportantEmailIntent(prompt)) return false;
    window[HOLD_KEY] = { expiresAt: Date.now() + 20000 };
    return true;
  }

  function overviewEmailHoldActive() {
    const hold = window[HOLD_KEY];
    if (!hold || currentPage() !== 'overview') return false;
    if (Date.now() > Number(hold.expiresAt || 0)) {
      delete window[HOLD_KEY];
      return false;
    }
    return true;
  }

  function clearOverviewEmailHold() {
    delete window[HOLD_KEY];
  }

  function highestPriorityEmailId() {
    const context = window.Kyle?.store?.context || {};
    const attention = context.needs_attention || context.mail?.needs_attention || [];
    const emails = context.emails || context.mail?.recent || [];
    const first = Array.isArray(attention) ? attention[0] : null;
    if (first) {
      const candidates = [first.id, first.gmail_id, first.message_id, first.source_message_id, first.threadId]
        .filter(Boolean).map(String);
      const exact = (Array.isArray(emails) ? emails : []).find(email => {
        const ids = [email.id, email.gmail_id, email.message_id, email.source_message_id, email.threadId]
          .filter(Boolean).map(String);
        return candidates.some(id => ids.includes(id));
      });
      const exactId = exact && (exact.id || exact.gmail_id || exact.message_id || exact.threadId);
      if (exactId) return String(exactId);
      if (candidates.length) return candidates[0];
    }

    const important = (Array.isArray(emails) ? emails : []).find(email =>
      email.important === true || email.is_important === true || email.context_scores?.important === true
    );
    const fallbackId = important && (important.id || important.gmail_id || important.message_id || important.threadId);
    return fallbackId ? String(fallbackId) : '';
  }

  function openHighestPriorityEmail() {
    if (currentPage() === 'overview') return false;
    const id = highestPriorityEmailId();
    if (!id || !window.KyleActions?.openEmail) return false;
    return window.KyleActions.openEmail(id) !== false;
  }

  function injectStyles() {
    if (document.getElementById('mailmateForkUxStyles')) return;
    const style = document.createElement('style');
    style.id = 'mailmateForkUxStyles';
    style.textContent = `
      .mailmate-overview-priority-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.25fr) minmax(320px, .75fr);
        gap: 32px;
        align-items: start;
        border-bottom: 1px solid var(--line);
      }
      .mailmate-overview-priority-grid > .workspace-section {
        min-width: 0;
        border-bottom: 0;
      }
      #tab-overview .overview-grid.mailmate-working-only {
        grid-template-columns: minmax(0, 1fr);
      }
      #tab-overview .overview-grid.mailmate-working-only .working-section {
        width: 100%;
      }
      .calendar-now-line {
        z-index: 30 !important;
        height: 2px !important;
        background: #ef4444 !important;
        box-shadow: 0 0 0 1px rgba(239, 68, 68, .12), 0 0 7px rgba(239, 68, 68, .32);
        pointer-events: none !important;
      }
      .calendar-now-line::before {
        width: 8px !important;
        height: 8px !important;
        left: -4px !important;
        top: -3px !important;
        background: #ef4444 !important;
        border-radius: 50%;
      }
      .calendar-event-block.is-in-progress {
        opacity: 1 !important;
        filter: none !important;
        box-shadow: inset 3px 0 0 rgba(239, 68, 68, .78), 0 2px 8px rgba(0,0,0,.05) !important;
      }
      .calendar-day-column.is-today {
        opacity: 1 !important;
      }
      .calendar-day-column.is-today .calendar-event-block:not(.is-past) {
        opacity: 1 !important;
        filter: none !important;
      }
      @media (max-width: 980px) {
        .mailmate-overview-priority-grid {
          grid-template-columns: minmax(0, 1fr);
          gap: 0;
        }
        .mailmate-overview-priority-grid > .workspace-section {
          border-bottom: 1px solid var(--line);
        }
      }
    `;
    document.head.appendChild(style);
  }

  function arrangeOverview() {
    const surface = document.getElementById('overviewDataSurface');
    if (!surface) return;

    const attention = surface.querySelector('.attention-section');
    const upcoming = surface.querySelector('.upcoming-section');
    if (!attention || !upcoming) return;

    let priorityGrid = surface.querySelector('.mailmate-overview-priority-grid');
    if (!priorityGrid) {
      priorityGrid = document.createElement('div');
      priorityGrid.className = 'mailmate-overview-priority-grid';
      attention.parentNode.insertBefore(priorityGrid, attention);
    }

    if (attention.parentElement !== priorityGrid) priorityGrid.appendChild(attention);
    if (upcoming.parentElement !== priorityGrid) priorityGrid.appendChild(upcoming);

    const oldGrid = surface.querySelector('.overview-grid');
    if (oldGrid) oldGrid.classList.add('mailmate-working-only');
  }

  function parseEventDate(value, allDay) {
    if (!value) return null;
    const raw = String(value);
    const date = allDay || raw.length <= 10
      ? new Date(`${raw.slice(0, 10)}T00:00:00`)
      : new Date(raw);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function classifyCalendarEvents() {
    const events = window.AgentCalendar?.getVisibleEvents?.() || window.AgentCalendar?.getEvents?.() || [];
    if (!Array.isArray(events) || !events.length) return;
    const byId = new Map(events.map(event => [String(event.id), event]));
    const now = new Date();

    document.querySelectorAll('[data-calendar-event]').forEach(element => {
      const event = byId.get(String(element.dataset.calendarEvent || ''));
      if (!event) return;

      const start = parseEventDate(event.start, event.all_day);
      let end = parseEventDate(event.end, event.all_day);
      if (!start) return;
      if (!end) end = new Date(start.getTime() + (event.all_day ? 86400000 : 30 * 60000));

      const isPast = end.getTime() <= now.getTime();
      const isInProgress = !event.all_day && start.getTime() <= now.getTime() && end.getTime() > now.getTime();

      element.classList.toggle('is-past', isPast);
      element.classList.toggle('is-in-progress', isInProgress);

      // The core renderer currently derives "past" from start time. That makes an
      // event look finished as soon as it begins. Time state belongs to end time.
      if (!isPast) element.classList.remove('urgency-past');
      if (isPast) element.classList.add('urgency-past');
    });

    document.querySelectorAll('.calendar-day-column.is-today.is-past').forEach(column => {
      column.classList.remove('is-past');
    });
  }

  function updateCurrentTimeNeedle() {
    const grid = document.getElementById('calendarGrid');
    if (!grid) return;

    const todayColumn = grid.querySelector('.calendar-day-column.is-today');
    grid.querySelectorAll('.calendar-day-column:not(.is-today) .calendar-now-line').forEach(line => line.remove());
    if (!todayColumn) return;

    const startHour = Number(grid.dataset.startHour || 0);
    const hourHeight = parseFloat(getComputedStyle(grid).getPropertyValue('--calendar-hour-height')) || 56;
    const now = new Date();
    const minuteOfDay = now.getHours() * 60 + now.getMinutes() + now.getSeconds() / 60;
    const visibleMinute = minuteOfDay - startHour * 60;
    if (visibleMinute < 0 || visibleMinute > 24 * 60) return;

    let line = todayColumn.querySelector('.calendar-now-line');
    if (!line) {
      line = document.createElement('div');
      line.className = 'calendar-now-line';
      line.setAttribute('aria-hidden', 'true');
      todayColumn.appendChild(line);
    }
    line.style.top = `${(visibleMinute / 60) * hourHeight}px`;
  }

  let calendarRefreshQueued = false;
  function refreshCalendarVisualState() {
    if (calendarRefreshQueued) return;
    calendarRefreshQueued = true;
    requestAnimationFrame(() => {
      calendarRefreshQueued = false;
      updateCurrentTimeNeedle();
      classifyCalendarEvents();
    });
  }

  function observeCalendar() {
    const roots = [document.getElementById('calendarGrid'), document.getElementById('calendarAllDay')].filter(Boolean);
    if (!roots.length) return false;

    const observer = new MutationObserver(refreshCalendarVisualState);
    roots.forEach(root => observer.observe(root, { childList: true, subtree: true }));
    refreshCalendarVisualState();
    window.setInterval(refreshCalendarVisualState, 30000);
    window.addEventListener('focus', refreshCalendarVisualState);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshCalendarVisualState();
    });
    return true;
  }

  function patchKyleRouting() {
    if (!window.KyleCanvas || !window.KyleActions) return false;
    if (window[PATCH_MARK]) return true;

    const originalShouldPresent = window.KyleCanvas.shouldPresent?.bind(window.KyleCanvas);
    if (originalShouldPresent) {
      window.KyleCanvas.shouldPresent = function (prompt, payload) {
        if (armOverviewEmailHold(prompt)) return true;
        const importantIntent = IMPORTANT_EMAIL_INTENT.test(String(prompt || ''));
        if (importantIntent && currentPage() !== 'overview') {
          window.setTimeout(openHighestPriorityEmail, 0);
        }
        return originalShouldPresent(prompt, payload);
      };
    }

    const originalOpenEmail = window.KyleActions.openEmail?.bind(window.KyleActions);
    if (originalOpenEmail) {
      window.KyleActions.openEmail = function (indexOrId) {
        if (overviewEmailHoldActive()) {
          const results = window.Kyle?.store?.lastResults || [];
          const index = Number(indexOrId);
          const exact = Number.isFinite(index)
            ? results[index - 1]
            : results.find(item => String(item.id) === String(indexOrId));
          if (exact && window.Kyle?.store) window.Kyle.store.selectedEmail = exact;
          return true;
        }
        return originalOpenEmail(indexOrId);
      };
    }

    const originalShowEmailResults = window.KyleActions.showEmailResults?.bind(window.KyleActions);
    if (originalShowEmailResults) {
      window.KyleActions.showEmailResults = function (results) {
        if (overviewEmailHoldActive()) {
          if (window.Kyle?.store) window.Kyle.store.lastResults = results || [];
          return true;
        }
        return originalShowEmailResults(results);
      };
    }

    const originalOpenPage = window.KyleActions.openPage?.bind(window.KyleActions);
    if (originalOpenPage) {
      window.KyleActions.openPage = function (page) {
        if (page === 'inbox' && overviewEmailHoldActive()) return true;
        return originalOpenPage(page);
      };
    }

    const originalReveal = window.KyleCanvas.reveal?.bind(window.KyleCanvas);
    if (originalReveal) {
      window.KyleCanvas.reveal = async function (...args) {
        try {
          return await originalReveal(...args);
        } finally {
          window.setTimeout(clearOverviewEmailHold, 250);
        }
      };
    }

    window[PATCH_MARK] = true;
    return true;
  }

  function bootPatch() {
    injectStyles();
    arrangeOverview();

    const overviewSurface = document.getElementById('overviewDataSurface');
    if (overviewSurface) {
      const overviewObserver = new MutationObserver(arrangeOverview);
      overviewObserver.observe(overviewSurface, { childList: true, subtree: true });
    }

    if (!observeCalendar()) {
      const waitCalendar = window.setInterval(() => {
        if (observeCalendar()) window.clearInterval(waitCalendar);
      }, 250);
      window.setTimeout(() => window.clearInterval(waitCalendar), 10000);
    }

    if (!patchKyleRouting()) {
      const waitKyle = window.setInterval(() => {
        if (patchKyleRouting()) window.clearInterval(waitKyle);
      }, 100);
      window.setTimeout(() => window.clearInterval(waitKyle), 10000);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => window.setTimeout(bootPatch, 0), { once: true });
  } else {
    window.setTimeout(bootPatch, 0);
  }
})();
