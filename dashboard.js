// Immediately apply theme before DOM is fully loaded to prevent flashing
const savedTheme = localStorage.getItem('mailmate-theme');
if (savedTheme === 'dark' || (!savedTheme && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
  document.documentElement.setAttribute('data-theme', 'dark');
}

document.addEventListener('DOMContentLoaded', () => {
  const API_BASE = window.location.origin;
  const $ = id => document.getElementById(id);
  const els = {
    tabs: [...document.querySelectorAll('nav.nav-tabs .nav-tab')],
    panels: [...document.querySelectorAll('.tab-panel')],
    filters: [...document.querySelectorAll('.filter-tab')],
    pageTitle: $('pageTitle'),
    pageSubtitle: $('pageSubtitle'),
    profileName: $('profileName'),
    profilePic: $('profilePic'),
    overviewGreeting: $('overviewGreeting'),
    refreshBtn: $('refreshBtn'),
    processState: $('processState'),
    processList: $('processList'),
    summaryText: $('summaryText'),
    emailCount: $('emailCount'),
    importantCount: $('importantCount'),
    actionCount: $('actionCount'),
    attentionBadge: $('attentionBadge'),
    attentionList: $('attentionList'),
    waitingBadge: $('waitingBadge'),
    waitingList: $('waitingList'),
    kyleSuggestions: $('kyleSuggestions'),
    actionList: $('actionList'),
    upcomingList: $('upcomingList'),
    emailList: $('emailList'),
    emailDetail: $('emailDetail'),
    inboxCount: $('inboxCount'),
    workCount: $('workCount'),
    workList: $('workList'),
    runTitle: $('runTitle'),
    agentTimeline: $('agentTimeline'),
    statusList: $('statusList'),
    integrationList: $('integrationList'),
    errorBox: $('errorBox')
  };

  const state = {
    userId: localStorage.getItem('userId'),
    userName: localStorage.getItem('userName') || 'User',
    userPicture: localStorage.getItem('userPicture') || '',
    data: null,
    health: null,
    selectedEmailId: null,
    fullMessages: new Map(),
    loadingMessageIds: new Set(),
    mailboxLoadingCount: 0,
    inboxFilter: 'all',
    currentPage: 'overview',
    calendarEvents: [],
    calendarConflictPairs: [],
    calendarWeekStart: startOfWeek(new Date()),
    calendarSelectedEventId: null,
    calendarSyncTimer: null,
    inboxSyncTimer: null,
    calendarSyncing: false,
    calendarLastSyncAt: null,
    calendarSyncSeq: 0,
    calendarDismissedMarkers: new Set(),
    automations: []
  };

  const pageCopy = {
    overview: ['Overview', 'Your inbox, distilled into what matters now.'],
    inbox: ['Inbox', 'Recent Gmail, sorted for urgency and action.'],
    work: ['Work', 'Useful agent activity and approved next steps.'],
    calendar: ['Calendar', 'Schedule and extracted deadlines.'],
    automations: ['Automations', 'Scheduled workflows that watch your inbox for you.'],
    status: ['Status', 'Auth, cache, AI, browser voice, and runtime details.'],
    integrations: ['Integrations', 'Services powering your inbox workspace.'],
    settings: ['Settings', 'Session preferences and data handling.']
  };

  boot();

  async function boot() {
    hydrateReturnParams();
    mountDeveloperSettings();
    bindEvents();
    registerStaticObjects();
    initCalendarControls();
    initAutomationControls();
    bindAutopilotSettings();
    loadWorkSettings();

    const liveProfile = await hydrateAuthenticatedProfile();
    if (!liveProfile && !state.userId) return;
    setProfile(liveProfile || {});

    // Paint the last same-session snapshot immediately, then refresh network data.
    hydrateSessionSnapshot();
    const inboxPromise = loadInbox(false);
    const healthPromise = loadHealth();
    const automationsPromise = loadAutomations();
    await Promise.allSettled([inboxPromise, healthPromise, automationsPromise]);
    await loadCloudRecovery();
    startCalendarAutoSync();
    startInboxAutoSync();
  }

  async function hydrateAuthenticatedProfile() {
    try {
      const response = await fetch(`${API_BASE}/api/user/profile?ts=${Date.now()}`, {
        cache: 'no-store',
        headers: { Accept: 'application/json' }
      });
      if (response.status === 401) {
        ['userId', 'userName', 'userPicture', 'profileAccountId'].forEach(key => localStorage.removeItem(key));
        window.location.replace('/');
        return null;
      }
      if (!response.ok) throw new Error(`Profile returned ${response.status}`);
      const profile = normalizeTextTree(await response.json());
      state.userId = profile.email || profile.id || profile.sub || state.userId;
      state.userName = profile.name || profile.email || state.userName;
      state.userPicture = profile.picture || '';
      return profile;
    } catch (error) {
      console.warn('[Mailmate] profile hydration failed:', error.message || error);
      return null;
    }
  }

  function sessionSnapshotKey() {
    return `mailmate.dashboard.${state.userId || 'anonymous'}`;
  }

  function hydrateSessionSnapshot() {
    if (!state.userId) return false;
    try {
      const raw = sessionStorage.getItem(sessionSnapshotKey());
      if (!raw) return false;
      const snapshot = JSON.parse(raw);
      if (!snapshot?.data || Date.now() - Number(snapshot.savedAt || 0) > 15 * 60 * 1000) return false;
      state.data = normalizeTextTree(snapshot.data);
      state.calendarDismissedMarkers = new Set(state.data.calendar_dismissed_markers || []);
      renderDashboard(state.data);
      if (state.data.user) setProfile(state.data.user);
      els.processState.textContent = 'Showing recent session · refreshing';
      return true;
    } catch (_) {
      return false;
    }
  }

  function saveSessionSnapshot(data) {
    if (!state.userId || !data) return;
    try {
      sessionStorage.setItem(sessionSnapshotKey(), JSON.stringify({ savedAt: Date.now(), data }));
    } catch (_) {
      // Session storage is only a speed optimization.
    }
  }

  function hydrateReturnParams() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('connected') === 'true' && params.get('userId')) {
      const nextUserId = params.get('userId');
      const previousUserId = localStorage.getItem('userId') || '';

      // Never carry a previous Google account's avatar into a new account.
      if (previousUserId && previousUserId !== nextUserId) {
        state.userPicture = '';
        state.userName = 'Google User';
        localStorage.removeItem('userPicture');
        localStorage.removeItem('userName');
      }

      state.userId = nextUserId;
      state.userName = params.get('name') || state.userName;
      state.userPicture = params.get('picture') || '';

      localStorage.setItem('userId', state.userId);
      localStorage.setItem('userName', state.userName);
      localStorage.setItem('profileAccountId', state.userId);
      if (state.userPicture) localStorage.setItem('userPicture', state.userPicture);
      else localStorage.removeItem('userPicture');

      window.history.replaceState({}, '', './dashboard.html');
    }
  }

  function bindEvents() {
    document.addEventListener('click', event => {
      const tab = event.target?.closest?.('nav.nav-tabs .nav-tab');
      if (tab) showTab(tab.dataset.tab);
    });
    els.filters.forEach(filter => filter.addEventListener('click', () => setInboxFilter(filter.dataset.filter)));
    els.refreshBtn?.addEventListener('click', async () => {
      const btn = els.refreshBtn;
      const icon = $('refreshIcon');
      const label = $('refreshLabel');
      if (btn.classList.contains('is-refreshing')) return;
      btn.classList.add('is-refreshing');
      if (label) label.textContent = 'Syncing…';
      try {
        await loadInbox(true);
      } finally {
        btn.classList.remove('is-refreshing');
        if (label) label.textContent = 'Refresh';
      }
    });
    document.querySelectorAll('[data-overview-target]').forEach(button => button.addEventListener('click', () => {
      const target = button.dataset.overviewTarget;
      if (target === 'work') return showTab('work');
      showTab('inbox');
      if (target === 'attention') setInboxFilter('action');
    }));
    $('viewAllAttentionBtn')?.addEventListener('click', () => {
      showTab('inbox');
      setInboxFilter('action');
    });
    $('logoutBtn')?.addEventListener('click', logout);
    $('sidebarProfileBtn')?.addEventListener('click', () => showTab('settings'));
    $('cloudRecoveryBtn')?.addEventListener('click', toggleCloudRecovery);
    $('cloudRecoveryEnable')?.addEventListener('click', enableCloudRecovery);
    $('cloudRecoveryLater')?.addEventListener('click', dismissCloudRecovery);

    const themeToggleBtn = $('themeToggleBtn');
    const darkModeToggle = $('settingDarkMode');
    function updateThemeIcon() {
      const dark = document.documentElement.getAttribute('data-theme') === 'dark';
      if (darkModeToggle) darkModeToggle.checked = dark;
      if (!themeToggleBtn) return;
      if (dark) {
        themeToggleBtn.innerHTML = '<i class="fas fa-sun"></i><span>Light Mode</span>';
      } else {
        themeToggleBtn.innerHTML = '<i class="fas fa-moon"></i><span>Dark Mode</span>';
      }
    }
    function setTheme(theme) {
      const dark = theme === 'dark';
      if (dark) document.documentElement.setAttribute('data-theme', 'dark');
      else document.documentElement.removeAttribute('data-theme');
      localStorage.setItem('mailmate-theme', dark ? 'dark' : 'light');
      updateThemeIcon();
    }
    updateThemeIcon();
    if (themeToggleBtn) {
      themeToggleBtn.addEventListener('click', () => {
        setTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
      });
    }
    darkModeToggle?.addEventListener('change', () => setTheme(darkModeToggle.checked ? 'dark' : 'light'));

    window.addEventListener('mailmate:set-preference', event => {
      const { key, value } = event.detail || {};
      if (key === 'theme') setTheme(value === 'dark' ? 'dark' : 'light');
      if (key === 'voice') {
        const voiceToggle = $('settingKyleVoice');
        if (voiceToggle) {
          voiceToggle.checked = value !== 'muted';
          voiceToggle.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }
      if (key === 'developer_mode') setDeveloperMode(value === 'enabled');
    });

    window.addEventListener('harness:open-email', event => {
      const email = event.detail;
      showTab('inbox');
      openEmail(email);
    });

    window.addEventListener('harness:error', event => {
      addError(event.detail?.message || 'Unknown Kyle error.');
      renderStatus();
    });
  }

  function setInboxFilter(filterName) {
    state.inboxFilter = filterName || 'all';
    els.filters.forEach(item => item.classList.toggle('active', item.dataset.filter === state.inboxFilter));
    renderEmails(state.data?.emails || []);
  }

  function registerStaticObjects() {
    els.tabs.forEach(tab => window.MailmateObjects?.register({
      type: 'page',
      id: tab.dataset.tab,
      label: tab.textContent.trim(),
      page: tab.dataset.tab
    }, tab));
    els.filters.forEach(filter => window.MailmateObjects?.register({
      type: 'inbox-filter',
      id: filter.dataset.filter,
      label: filter.textContent.trim(),
      page: 'inbox'
    }, filter));
  }

  function showTab(name) {
    if (name === 'status' || name === 'integrations') {
      name = 'settings';
      setDeveloperMode(true);
    }
    state.currentPage = name;
    document.querySelector('.main')?.classList.toggle('is-overview-page', name === 'overview');
    document.querySelector('.main')?.classList.toggle('is-settings-page', name === 'settings');
    const copy = pageCopy[name] || pageCopy.overview;
    els.pageTitle.textContent = copy[0];
    els.pageSubtitle.textContent = copy[1];
    document.title = `MailMate - ${copy[0]}`;
    els.tabs.forEach(tab => tab.classList.toggle('active', tab.dataset.tab === name));
    els.panels.forEach(panel => panel.classList.toggle('active', panel.id === `tab-${name}`));
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
    document.querySelector('.main')?.scrollTo?.({ top: 0, left: 0, behavior: 'auto' });
    window.MailmateContext?.setPage(name);
    window.KyleCanvas?.onPageChange?.(name);
    window.KyleUi?.active?.setPresentationMode?.(name === 'overview' ? 'overview' : 'floating');
    window.Kyle?.setContext({
      ...(state.data || {}),
      calendarEvents: state.calendarEvents,
      workJobs: workJobs,
      health: state.health,
      currentPage: name
    });
    if (name === 'calendar') refreshCalendar(false);
    if (name === 'automations') loadAutomations();
    if (name === 'work') renderWork(state.data);
    if (name === 'status') renderStatus();
    updateWorkLivePolling();
    console.log('[Mailmate] navigation', name);
  }

  function setProfile(profile = {}) {
    const profileId = profile.email || profile.id || profile.sub || state.userId || '';
    const storedProfileId = localStorage.getItem('profileAccountId') || localStorage.getItem('userId') || '';

    if (profileId && storedProfileId && profileId !== storedProfileId) {
      state.userPicture = '';
      localStorage.removeItem('userPicture');
    }

    const name = profile.name || profile.email || state.userName || 'Google User';
    const fallback = `https://ui-avatars.com/api/?name=${encodeURIComponent(name)}&background=111111&color=ffffff`;

    state.userId = profile.id || profile.sub || profile.email || state.userId;
    state.userName = name;

    // If a live profile object was supplied, trust that account's picture only.
    // Do not silently reuse the previous account's localStorage picture.
    if (Object.keys(profile).length) {
      state.userPicture = profile.picture || '';
    }

    const picture = state.userPicture ? `${API_BASE}/api/user/avatar` : fallback;

    localStorage.setItem('userName', name);
    if (state.userId) {
      localStorage.setItem('userId', state.userId);
      localStorage.setItem('profileAccountId', profileId || state.userId);
    }
    if (state.userPicture) localStorage.setItem('userPicture', state.userPicture);
    else localStorage.removeItem('userPicture');

    els.profileName.textContent = name;
    els.profilePic.alt = name;
    els.profilePic.referrerPolicy = 'no-referrer';
    els.profilePic.onerror = () => {
      els.profilePic.onerror = null;
      els.profilePic.src = fallback;
    };
    els.profilePic.src = picture;
    if ($('profileEmail')) $('profileEmail').textContent = profile.email || state.userId || 'Google account';

    const settingsPicture = $('settingsProfilePic');
    if (settingsPicture) {
      settingsPicture.alt = name;
      settingsPicture.referrerPolicy = 'no-referrer';
      settingsPicture.onerror = () => {
        settingsPicture.onerror = null;
        settingsPicture.src = fallback;
      };
      settingsPicture.src = picture;
    }
    if ($('settingsProfileName')) $('settingsProfileName').textContent = name;
    if ($('settingsProfileEmail')) $('settingsProfileEmail').textContent = profile.email || state.userId || 'Google account';

    const hour = new Date().getHours();
    const greeting = hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
    els.overviewGreeting.textContent = `${greeting}, ${name.split(' ')[0]}`;
  }

  async function loadHealth() {
    try {
      const response = await fetch(`${API_BASE}/api/health`);
      state.health = await response.json();
      renderStatus();
      renderIntegrations();
      renderCacheSettings();
      renderWorkPermissions();
      window.Kyle?.setContext({
        ...(state.data || {}),
        calendarEvents: state.calendarEvents,
        workJobs: workJobs,
        health: state.health,
        currentPage: state.currentPage
      });
    } catch (error) {
      addError('Health check failed: ' + error.message);
    }
  }

  function renderWorkPermissions() {
    const banner = $('workPermissionBanner');
    if (!banner) return;
    if (state.health && state.health.gmailWrite === false) {
      banner.style.display = 'flex';
    } else {
      banner.style.display = 'none';
    }
  }


  function renderCacheSettings() {
    const status = $('cacheSettingsStatus');
    const policyText = $('cachePolicyText');

    if (status) {
      status.textContent = 'Active · Zero retention (RAM-only)';
    }

    if (policyText) {
      policyText.textContent = 'Raw mailbox content is kept in transient browser RAM only. Privacy gate filters all sensitive correspondence locally before AI summarization.';
    }
  }

  let cloudRecoveryState = null;

  async function loadCloudRecovery() {
    const button = $('cloudRecoveryBtn');
    try {
      const response = await fetch(`${API_BASE}/api/security/cloud-recovery`, { cache: 'no-store' });
      cloudRecoveryState = await response.json();
      if (!response.ok) throw new Error(cloudRecoveryState.error || 'Recovery status unavailable');
      if (button) {
        button.disabled = !cloudRecoveryState.available;
        button.textContent = cloudRecoveryState.enrolled ? 'Enabled' : (cloudRecoveryState.available ? 'Enable' : 'Administrator setup required');
      }
      const dismissed = localStorage.getItem(`mailmate.recovery.dismissed.${state.userId}`) === '1';
      if (cloudRecoveryState.recommended && !dismissed) openCloudRecovery();
    } catch (_) {
      if (button) { button.disabled = true; button.textContent = 'Administrator setup required'; }
    }
  }

  function openCloudRecovery() {
    const modal = $('cloudRecoveryModal');
    if (!modal) return;
    modal.hidden = false;
    document.body.classList.add('modal-open');
    requestAnimationFrame(() => modal.classList.add('is-visible'));
  }

  function closeCloudRecovery() {
    const modal = $('cloudRecoveryModal');
    modal?.classList.remove('is-visible');
    document.body.classList.remove('modal-open');
    setTimeout(() => { if (modal && !modal.classList.contains('is-visible')) modal.hidden = true; }, 140);
  }

  function dismissCloudRecovery() {
    localStorage.setItem(`mailmate.recovery.dismissed.${state.userId}`, '1');
    closeCloudRecovery();
  }

  async function enableCloudRecovery() {
    const button = $('cloudRecoveryEnable');
    if (button) { button.disabled = true; button.textContent = 'Protecting key...'; }
    try {
      const response = await fetch(`${API_BASE}/api/security/cloud-recovery`, { method: 'POST' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || 'Could not enable recovery');
      cloudRecoveryState = result;
      closeCloudRecovery();
      await loadCloudRecovery();
    } catch (error) {
      addError(`Recovery: ${error.message}`);
    } finally {
      if (button) { button.disabled = false; button.textContent = 'Enable recovery'; }
    }
  }

  async function toggleCloudRecovery() {
    if (!cloudRecoveryState?.available) return;
    if (!cloudRecoveryState.enrolled) return openCloudRecovery();
    if (!window.confirm('Disable Google account recovery for this MailMate key?')) return;
    await fetch(`${API_BASE}/api/security/cloud-recovery`, { method: 'DELETE' });
    await loadCloudRecovery();
  }

  async function logout() {
    const button = $('logoutBtn');
    if (button) {
      button.disabled = true;
      button.textContent = 'Signing out…';
    }
    try {
      const response = await fetch(`${API_BASE}/api/auth/logout`, { method: 'POST' });
      if (!response.ok) throw new Error(`Logout returned ${response.status}`);
      try { sessionStorage.removeItem(sessionSnapshotKey()); } catch (_) {}
      ['userId', 'userName', 'userPicture'].forEach(key => localStorage.removeItem(key));
      window.location.replace('/');
    } catch (error) {
      addError('Logout: ' + error.message);
      if (button) {
        button.disabled = false;
        button.innerHTML = '<i class="fas fa-arrow-right-from-bracket"></i> Sign out';
      }
    }
  }

  async function loadInbox(forceRefresh) {
    console.log('[Mailmate] dashboard load', forceRefresh ? 'refresh' : 'cache-first');
    if (!state.userId) {
      setStep('auth', 'active', 'Connect Google from the landing page');
      els.summaryText.textContent = 'Connect Google from the landing page to see your Gmail priorities here.';
      window.Kyle?.setContext({ health: state.health, emails: [], metrics: {}, currentPage: state.currentPage });
      return;
    }
    state.mailboxLoadingCount += 1;
    $('tab-overview')?.classList.toggle('is-loading', state.mailboxLoadingCount > 0);
    window.KyleCanvas?.setDataLoading?.(true);

    clearSteps();
    setStep('auth', 'done', 'Gmail session found');
    setStep('cache', 'active', 'Checking current mailbox context');

    try {
      setStep('cache', 'done', forceRefresh ? 'Refresh requested' : 'Cache checked first');
      setStep('fetch', 'active', forceRefresh ? 'Refreshing Gmail now' : 'Loading current context');
      const response = await fetch(`${API_BASE}/api/dashboard/overview?userId=${encodeURIComponent(state.userId)}${forceRefresh ? '&refresh=true' : ''}`);
      if (response.status === 401) {
        ['userId', 'userName', 'userPicture', 'profileAccountId'].forEach(key => localStorage.removeItem(key));
        window.location.replace('/');
        return;
      }
      if (!response.ok) throw new Error(`Dashboard returned ${response.status}`);
      setStep('fetch', 'done', 'Context loaded');
      setStep('extract', 'active', forceRefresh ? 'Reconciling mailbox changes' : 'Extracting work, blockers, and deadlines');
      const data = normalizeTextTree(await response.json());
      const changedMessages = Number(data.context_sync?.changed_messages || 0);
      setStep('extract', 'done', changedMessages ? `${changedMessages} changed message${changedMessages === 1 ? '' : 's'} updated` : 'No mailbox changes');
      setStep('store', 'active', forceRefresh ? 'Updating active context' : 'Saving processed context');
      const storageMode = data.context_sync?.storage?.mode || 'memory-only';
      setStep('store', 'done', storageMode === 'user-scoped-jwt' ? 'Saved private derived context' : 'Transient context ready');

      state.data = data;
      state.calendarDismissedMarkers = new Set(data.calendar_dismissed_markers || []);
      renderDashboard(data);

      try {
        const workResponse = await fetch(`${API_BASE}/api/work/jobs${forceRefresh ? '' : '?ensure=1'}`, { cache: 'no-store' });
        if (workResponse.ok) {
          workJobs = normalizeTextTree(await workResponse.json());
          renderOverviewWorkingNow();
        }
      } catch (workSyncError) {
        console.debug('Work sync notice:', workSyncError);
      }
      saveSessionSnapshot(data);
      if (data.user) setProfile(data.user);

      await refreshCalendar(false);
      els.processState.textContent = data.cached ? (data.background_refresh_started ? 'Cache ready · checking updates' : 'Cache reused') : 'Complete';
      window.Kyle?.setContext({
        ...data,
        calendarEvents: state.calendarEvents,
        workJobs: workJobs,
        health: state.health,
        currentPage: state.currentPage
      });
      console.log('[Mailmate] dashboard ready');
    } catch (error) {
      addError(error.message);
      els.processState.textContent = 'Needs attention';
      if (!state.data) {
        els.summaryText.textContent = 'Your inbox summary is temporarily unavailable. Try refresh.';
        window.Kyle?.setContext({ health: state.health, emails: [], metrics: {}, currentPage: state.currentPage });
      }
    } finally {
      state.mailboxLoadingCount = Math.max(0, state.mailboxLoadingCount - 1);
      $('tab-overview')?.classList.toggle('is-loading', state.mailboxLoadingCount > 0);
      window.KyleCanvas?.setDataLoading?.(state.mailboxLoadingCount > 0);
    }
  }

  function renderOverviewWorkingNow() {
    if (!els.actionList) return;
    const activeStatuses = new Set([
      'queued', 'reading_context', 'planning', 'researching',
      'generating', 'drafting_reply', 'creating_files',
      'verifying', 'preparing', 'working'
    ]);
    const activeJobs = (workJobs || []).filter(j => activeStatuses.has(j.status));
    const reviewJobs = (workJobs || []).filter(j => ['waiting_approval', 'auto_send_countdown', 'needs_input'].includes(j.status));
    const unresolvedCount = activeJobs.length + reviewJobs.length;
    els.actionCount.textContent = unresolvedCount;
    const stateLabel = $('workingStateLabel');

    if (activeJobs.length > 0) {
      if (stateLabel) stateLabel.textContent = 'Kyle is working';
      els.actionList.innerHTML = activeJobs.map(job => {
        const displayTitle = cleanJobTitle(job.clean_title || job.title, job.source?.subject);
        const latestStep = (job.steps && job.steps.length > 0) ? job.steps[job.steps.length - 1] : null;
        const stepText = latestStep ? (latestStep.label || latestStep.thought || latestStep.action || 'Executing...') : (decodeHtml(job.source?.snippet) || 'Agent working...');
        const badgeText = job.status === 'working' ? 'Working' : job.status.replace(/_/g, ' ');
        return `
          <article data-kyle-type="work-item" data-kyle-id="${escapeHtml(job.id)}" data-kyle-label="${escapeHtml(displayTitle)}">
            <span class="status-dot"></span>
            <div>
              <strong>${escapeHtml(displayTitle)}</strong>
              <p>${escapeHtml(stepText)}</p>
            </div>
            <small style="text-transform:capitalize;">${escapeHtml(badgeText)}</small>
          </article>
        `;
      }).join('');
    } else if (reviewJobs.length > 0) {
      if (stateLabel) stateLabel.textContent = `${reviewJobs.length} ready`;
      els.actionList.innerHTML = reviewJobs.slice(0, 2).map(job => {
        const displayTitle = cleanJobTitle(job.clean_title || job.title, job.source?.subject);
        return `<article class="work-ready-overview" role="button" tabindex="0" data-kyle-id="${escapeHtml(job.id)}" data-kyle-type="work-item" data-kyle-label="${escapeHtml(displayTitle)}"><span class="status-dot"></span><div><strong>${escapeHtml(displayTitle)}</strong><p>${job.status === 'needs_input' ? 'Kyle needs your input to continue.' : 'Prepared and waiting for review.'}</p></div><small>Review <i class="fas fa-arrow-right"></i></small></article>`;
      }).join('');
    } else {
      if (stateLabel) stateLabel.textContent = 'Standing by';
      els.actionList.innerHTML = `
        <article>
          <span class="status-dot" style="background:#94a3b8;"></span>
          <div>
            <strong>Standing by</strong>
            <p>Nothing is being prepared right now.</p>
          </div>
        </article>
      `;
    }

    els.actionList.querySelectorAll('[data-kyle-id]').forEach(element => {
      window.MailmateObjects?.register({ type: element.dataset.kyleType, id: element.dataset.kyleId, label: element.dataset.kyleLabel, page: 'overview' }, element);
      const openWork = () => { selectedJobId = element.dataset.kyleId; showTab('work'); };
      element.addEventListener('click', openWork);
      element.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openWork(); } });
    });
  }

  function renderDashboard(data) {
    const metrics = data.metrics || {};
    els.emailCount.textContent = metrics.emails ?? 0;
    const attention = data.needs_attention || [];
    const waiting = data.waiting_on_others || [];
    els.importantCount.textContent = attention.length;
    els.attentionBadge.textContent = `${attention.length} item${attention.length === 1 ? '' : 's'}`;
    els.summaryText.textContent = overviewSummary(attention.length, waiting.length);
    renderKyleSuggestions(attention);
    els.attentionList.innerHTML = attention.length
      ? attention.slice(0, 5).map((item, index) => {
          const title = decodeHtml(item.subject || item.title || conciseActionTitle(item.description || item.reason) || 'Your task');
          const description = decodeHtml(item.description || item.reason || 'Needs follow-up');
          const sourceId = attentionSourceId(item);
          const email = (data.emails || []).find(candidate => String(emailKey(candidate)) === sourceId) || {};
          const context = email.context_scores || {};
          const meta = [context.requires_reply ? 'Reply requested' : item.deadline ? 'Deadline' : 'Review requested', senderName(email.sender || item.sender || ''), item.deadline ? humanWhen(item.deadline) : formatDate(email.date || email.timestamp)].filter(Boolean).join(' · ');
          const secondary = context.work_allowed ? '<button type="button" data-attention-action="work">Open Work</button>' : context.requires_reply ? '<button type="button" data-attention-action="draft">Draft reply</button>' : item.deadline ? '<button type="button" data-attention-action="calendar">View calendar</button>' : '';
          return `<li class="attention-link" data-source-id="${escapeHtml(sourceId)}" data-kyle-type="email" data-kyle-id="${escapeHtml(sourceId || `attention-${index}`)}" data-kyle-label="${escapeHtml(title)}"><button class="attention-main" type="button"><span class="priority-dot" aria-hidden="true"></span><span><strong>${escapeHtml(title)}</strong><span class="attention-description">${escapeHtml(description)}</span><small>${escapeHtml(meta)}</small></span><i class="fas fa-arrow-right" aria-hidden="true"></i></button><div class="attention-actions"><button type="button" data-attention-action="email">Open email</button>${secondary}</div></li>`;
        }).join('')
      : '<li class="overview-empty"><strong>Inbox clear</strong><span>Nothing needs your attention right now.</span></li>';

    const viewAll = $('viewAllAttentionBtn');
    if (viewAll) viewAll.hidden = attention.length <= 5;
    if ($('viewAllAttentionCount')) $('viewAllAttentionCount').textContent = String(attention.length);

    els.waitingBadge.textContent = waiting.length ? `${waiting.length} item${waiting.length === 1 ? '' : 's'}` : 'Clear';
    els.waitingList.innerHTML = waiting.length
      ? waiting.slice(0, 4).map(item => `<li class="waiting-link" data-source-id="${escapeHtml(attentionSourceId(item))}"><button class="attention-main" type="button"><span><strong>${escapeHtml(decodeHtml(item.subject || item.title || 'Waiting for reply'))}</strong><small>${escapeHtml(decodeHtml(item.description || item.reason || 'Waiting on someone else'))}</small></span><i class="fas fa-arrow-right"></i></button></li>`).join('')
      : '<li class="overview-empty"><strong>Nothing you\'re waiting on.</strong></li>';

    renderOverviewWorkingNow();

    els.attentionList.querySelectorAll('[data-kyle-id]')
      .forEach(element => {
        window.MailmateObjects?.register({
        type: element.dataset.kyleType,
        id: element.dataset.kyleId,
        label: element.dataset.kyleLabel,
        page: 'overview'
        }, element);
        element.querySelector('.attention-main')?.addEventListener('click', () => openSourceEmail(element.dataset.sourceId));
        element.querySelectorAll('[data-attention-action]').forEach(button => button.addEventListener('click', event => {
          event.stopPropagation();
          const action = button.dataset.attentionAction;
          if (action === 'email') return openSourceEmail(element.dataset.sourceId);
          if (action === 'calendar') return showTab('calendar');
          if (action === 'work') { const job = workJobs.find(candidate => String(candidate.source?.message_id || '') === element.dataset.sourceId); if (job) selectedJobId = job.id; return showTab('work'); }
          if (action === 'draft') return window.Kyle?.handlePrompt?.(`Draft a reply to ${element.dataset.kyleLabel}`);
        }));
      });
    els.waitingList.querySelectorAll('[data-source-id]').forEach(element => element.querySelector('button')?.addEventListener('click', () => openSourceEmail(element.dataset.sourceId)));

    renderUpcomingFromContext();

    renderEmails(data.emails || []);
    renderWork(data);
    window.KyleCanvas?.animateDashboardArrival?.();
  }

  function overviewSummary(attentionCount, waitingCount) {
    if (!attentionCount) return waitingCount ? `Your inbox is clear. You're waiting on ${waitingCount} response${waitingCount === 1 ? '' : 's'}.` : 'Your inbox is mostly clear. Nothing needs attention right now.';
    return `You have ${attentionCount} thing${attentionCount === 1 ? '' : 's'} that need${attentionCount === 1 ? 's' : ''} attention today.`;
  }

  function renderKyleSuggestions(attention) {
    if (!els.kyleSuggestions) return;
    const prompts = attention.length
      ? [`Summarize these ${Math.min(attention.length, 5)}`, 'What should I handle first?', 'Draft the top reply', "What's urgent today?"]
      : ['Summarize my inbox', "What's coming up?", 'Show unread mail', 'Check my calendar'];
    els.kyleSuggestions.innerHTML = prompts.map(prompt => `<button type="button" data-kyle-prompt="${escapeHtml(prompt)}">${escapeHtml(prompt)}</button>`).join('');
    els.kyleSuggestions.querySelectorAll('[data-kyle-prompt]').forEach(button => button.addEventListener('click', () => window.Kyle?.handlePrompt?.(button.dataset.kylePrompt)));
  }

  function privacyPillHtml(gate, email = null) {
    if (!gate) return '';
    if (gate.ai_allowed === false) {
      return `<span class="privacy-pill private" title="Private: Excluded from AI &amp; Work Agent (${escapeHtml(gate.reason || gate.label || 'Sensitive')})"><i class="fas fa-shield-halved"></i> Private</span>`;
    }
    if (gate.work_agent_allowed) {
      const work = emailWorkPresentation(email || { privacy_gate: gate });
      if (work.state === 'done') {
        return `<span class="privacy-pill work-done" title="Work completed for this source email"><i class="fas fa-check"></i> Work done</span>`;
      }
      if (work.state === 'needs-input') {
        return `<span class="privacy-pill work-needs-input" title="Kyle needs input before continuing"><i class="fas fa-circle-exclamation"></i> Needs input</span>`;
      }
      if (work.state === 'active') {
        return `<span class="privacy-pill work-active" title="Kyle Work is active for this source email"><i class="fas fa-robot"></i> Work Active</span>`;
      }
      return '';
    }
    return '';
  }

  function emailOverviewText(email) {
    return email.pdf_summary || email.summary || email.ai_summary || email.snippet || 'No preview available.';
  }

  function shouldRenderRichEmailHtml(email) {
    const html = String(email?.body_html || '');
    if (!html) return false;
    const tableCount = (html.match(/<table\b/gi) || []).length;
    const linkCount = (html.match(/<a\b/gi) || []).length;
    const designed = /<(?:h1|h2|h3)\b|background-color\s*:|\bbgcolor=|role=\"presentation\"/i.test(html);
    return tableCount > 0 || linkCount >= 3 || designed;
  }

  function workJobForEmail(email) {
    const messageId = String(emailKey(email) || '');
    if (!messageId) return null;
    return (workJobs || []).find(job => String(job?.source?.message_id || '') === messageId) || null;
  }

  function emailWorkPresentation(email) {
    const gate = email?.privacy_gate || {};
    if (!gate.work_agent_allowed) return { state: 'none', job: null };
    const job = workJobForEmail(email);
    if (!job) return { state: 'eligible', job: null };
    const status = String(job.status || '');
    if (WORK_TERMINAL_STATUSES.has(status) || status === 'failed') return { state: 'done', job };
    if (status === 'needs_input') return { state: 'needs-input', job };
    return { state: 'active', job };
  }

  async function openSourceEmail(sourceId, fallback = {}) {
    sourceId = String(sourceId || '');
    if (!sourceId) return;
    const emails = state.data?.emails || [];
    const source = emails.find(email => String(emailKey(email)) === sourceId) || {
      id: sourceId,
      gmail_id: sourceId,
      sender: fallback.sender || 'Unknown sender',
      subject: fallback.subject || 'No subject',
      snippet: fallback.snippet || '',
      date: fallback.date || ''
    };
    if (state.data && !emails.some(email => String(emailKey(email)) === sourceId)) {
      state.data.emails = [source, ...emails];
    }
    showTab('inbox');
    await new Promise(resolve => requestAnimationFrame(resolve));
    await openEmail(source);
  }

  function privacyDetailBox(gate, email = null) {
    if (!gate) return '';
    if (gate.ai_allowed === false) {
      return `
        <div class="email-privacy-card private">
          <div style="display:flex;align-items:center;gap:8px;">
            <i class="fas fa-shield-halved" style="font-size:1.1rem;color:#f43f5e;"></i>
            <div>
              <strong>Display Plane Only</strong> · Excluded from AI &amp; Work Agent
              <p style="margin:2px 0 0;font-size:0.75rem;">${escapeHtml(gate.reason || 'Contains financial, security, or sensitive information.')}</p>
            </div>
          </div>
          <span class="privacy-state-label private">Private</span>
        </div>`;
    }
    if (gate.work_agent_allowed) {
      const work = emailWorkPresentation(email || { privacy_gate: gate });
      if (work.state === 'eligible') return '';
      const done = work.state === 'done';
      const needs = work.state === 'needs-input';
      const cardClass = done ? 'work-done' : needs ? 'work-needs-input' : 'work';
      const title = done ? 'Work completed' : needs ? 'Work needs input' : 'Actionable Task Plane';
      const subtitle = done
        ? 'Kyle has already resolved or completed the Work item attached to this source email.'
        : needs
          ? 'Kyle is waiting for required input before it can continue.'
          : 'Kyle Work is currently active for this source email.';
      const label = done ? 'Work done' : needs ? 'Needs input' : 'Work Active';
      const icon = done ? 'fa-check' : needs ? 'fa-circle-exclamation' : 'fa-robot';
      return `
        <div class="email-privacy-card ${cardClass}">
          <div style="display:flex;align-items:center;gap:8px;">
            <i class="fas ${icon}" style="font-size:1.05rem;"></i>
            <div>
              <strong>${title}</strong>
              <p style="margin:2px 0 0;font-size:0.75rem;">${escapeHtml(subtitle)}</p>
            </div>
          </div>
          <span class="privacy-state-label">${label}</span>
        </div>`;
    }
    return '';
  }

  const prefetchTimers = new WeakMap();

  function prefetchEmail(email) {
    const id = String(emailKey(email) || '');
    if (!id || state.fullMessages.has(id) || state.loadingMessageIds.has(id)) return;
    state.loadingMessageIds.add(id);
    fetch(`${API_BASE}/api/gmail/messages/${encodeURIComponent(id)}`, { cache: 'no-store' })
      .then(async response => {
        if (!response.ok) return null;
        return response.json();
      })
      .then(detail => {
        if (!detail || detail.error) return;
        detail = normalizeTextTree(detail);
        state.fullMessages.set(id, detail);
        const source = state.data?.emails?.find(item => emailKey(item) === id);
        if (source) Object.assign(source, detail, { is_read: source.is_read });
        if (state.selectedEmailId === id) {
          const fullSelected = { ...email, ...detail };
          renderEmailDetail(fullSelected, false);
        }
      })
      .catch(() => {})
      .finally(() => {
        state.loadingMessageIds.delete(id);
      });
  }

  function renderEmails(emails) {
    window.MailmateObjects?.unregisterType('email');
    const filtered = emails.filter(email => {
      if (state.inboxFilter === 'important') return isImportant(email);
      if (state.inboxFilter === 'action') {
        const context = email.context_scores || {};
        return Boolean(context.attention_allowed || context.requires_reply || context.work_allowed || context.calendar_allowed);
      }
      if (state.inboxFilter === 'unread') return email.is_read === false;
      return true;
    });

    els.inboxCount.textContent = `${filtered.length} message${filtered.length === 1 ? '' : 's'}`;
    els.emailList.innerHTML = filtered.length
      ? filtered.slice(0, 40).map((email, index) => {
        const selected = state.selectedEmailId && state.selectedEmailId === emailKey(email);
        return `
          <article class="email-item ${selected ? 'is-selected' : ''} ${email.is_read === false ? 'is-unread' : ''} ${isImportant(email) ? 'is-important' : ''}" data-index="${index}" data-kyle-type="email" data-kyle-id="${escapeHtml(emailKey(email))}" data-kyle-label="${escapeHtml(email.subject || 'No subject')}">
            <span class="email-marker"></span>
            <div class="email-copy">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:2px;">
                <p class="email-sender">${escapeHtml(senderName(email.sender))}</p>
                ${privacyPillHtml(email.privacy_gate, email)}
              </div>
              <p class="email-subject">${escapeHtml(email.subject || 'No subject')}</p>
              <p class="email-preview">${safeSnippet(emailOverviewText(email))}</p>
            </div>
            <time class="email-time">${escapeHtml(formatDate(email.date || email.timestamp))}</time>
          </article>`;
      }).join('')
      : '<div class="empty-detail"><i class="far fa-envelope"></i><p>No messages match this filter.</p></div>';

    [...els.emailList.querySelectorAll('.email-item[data-index]')].forEach(item => {
      const email = filtered[Number(item.dataset.index)];
      window.MailmateObjects?.register({
        ...emailReference(email),
        page: 'inbox',
        metadata: {
          sender: email.sender,
          subject: email.subject,
          date: email.date || email.timestamp,
          unread: email.is_read === false,
          important: isImportant(email)
        }
      }, item);
      item.addEventListener('mouseenter', () => {
        const timer = setTimeout(() => {
          prefetchEmail(email);
        }, 200);
        prefetchTimers.set(item, timer);
      });
      item.addEventListener('mouseleave', () => {
        const timer = prefetchTimers.get(item);
        if (timer) {
          clearTimeout(timer);
          prefetchTimers.delete(item);
        }
      });
      item.addEventListener('click', () => {
        const timer = prefetchTimers.get(item);
        if (timer) {
          clearTimeout(timer);
          prefetchTimers.delete(item);
        }
        openEmail(email);
      });
    });

    const selected = emails.find(email => emailKey(email) === state.selectedEmailId);
    const hydrated = selected ? { ...selected, ...(state.fullMessages.get(emailKey(selected)) || {}) } : null;
    renderEmailDetail(hydrated, selected ? state.loadingMessageIds.has(emailKey(selected)) : false);
  }

  async function openEmail(email) {
    const id = String(emailKey(email) || '');
    if (!id) return;
    state.selectedEmailId = id;
    const reference = emailReference(email);
    window.MailmateContext?.select(reference);
    window.MailmateContext?.open(reference);

    if (email.is_read === false) {
      email.is_read = true;
      const source = state.data?.emails?.find(item => emailKey(item) === id);
      if (source) source.is_read = true;
      saveSessionSnapshot(state.data);
      fetch(`${API_BASE}/api/gmail/messages/${encodeURIComponent(id)}/read`, { method: 'POST' })
        .then(async response => {
          if (response.ok) return;
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.error || `Gmail mark-read returned ${response.status}`);
        })
        .catch(error => {
          email.is_read = false;
          if (source) source.is_read = false;
          addError('Gmail: ' + error.message);
          renderEmails(state.data?.emails || []);
        });
    }

    renderEmails(state.data?.emails || []);
    if (state.fullMessages.has(id)) return;
    if (state.loadingMessageIds.has(id)) {
      renderEmailDetail(email, true);
      return;
    }

    state.loadingMessageIds.add(id);
    renderEmailDetail(email, true);
    try {
      const response = await fetch(`${API_BASE}/api/gmail/messages/${encodeURIComponent(id)}`, { cache: 'no-store' });
      const detail = normalizeTextTree(await response.json().catch(() => ({})));
      if (!response.ok) throw new Error(detail.error || `Gmail message returned ${response.status}`);
      state.fullMessages.set(id, detail);
      const source = state.data?.emails?.find(item => emailKey(item) === id);
      if (source) Object.assign(source, detail, { is_read: source.is_read ?? detail.is_read });
    } catch (error) {
      addError('Gmail: ' + error.message);
    } finally {
      state.loadingMessageIds.delete(id);
      if (state.selectedEmailId === id) renderEmails(state.data?.emails || []);
    }
  }

  function renderEmailDetail(email, loading = false) {
    if (!email) {
      els.emailDetail.innerHTML = '<div class="empty-detail"><i class="far fa-envelope-open"></i><p>Select an email to read it here.</p></div>';
      return;
    }

    const hasFullBody = Boolean(email.body_html || email.body);
    const useRichHtml = shouldRenderRichEmailHtml(email);
    let bodyContent = '';
    if (useRichHtml) {
      bodyContent = `<div class="email-rich-surface"><div class="email-html-content">${email.body_html}</div></div>`;
    } else if (hasFullBody) {
      bodyContent = `<div class="email-plain-content">${linkifyText(email.body || email.snippet || '')}</div>`;
    } else if (loading) {
      bodyContent = `
        <div class="email-prefetch-indicator"><i class="fas fa-circle-notch fa-spin"></i> Loading full message...</div>
        <div class="email-plain-content email-snippet-content">${safeSnippet(email.snippet || 'Loading preview...')}</div>`;
    } else {
      bodyContent = `<div class="email-plain-content">${linkifyText(email.body || email.snippet || 'This message has no readable text body.')}</div>`;
    }

    els.emailDetail.innerHTML = `
      <header class="email-detail-header">
        <div class="email-detail-title-row">
          <p class="section-label">${isImportant(email) ? 'Needs attention' : 'Message'}</p>
          <div style="display:flex;align-items:center;gap:8px;">
            ${privacyPillHtml(email.privacy_gate, email)}
            <button class="email-trash-btn" id="emailTrashBtn" type="button" title="Move this message to Gmail Trash"><i class="far fa-trash-can"></i> Trash</button>
          </div>
        </div>
        <h2>${escapeHtml(email.subject || 'No subject')}</h2>
        <div class="email-detail-meta"><span>${escapeHtml(email.sender || 'Unknown sender')}</span><time>${escapeHtml(formatDate(email.date || email.timestamp, true))}</time></div>
        ${privacyDetailBox(email.privacy_gate, email)}
      </header>
      <div class="email-body" ${loading ? 'aria-busy="true"' : ''}>${bodyContent}</div>`;
    $('emailTrashBtn')?.addEventListener('click', () => trashEmail(email));
    const reference = emailReference(email);
    window.MailmateObjects?.register({
      ...reference,
      page: 'inbox',
      metadata: { sender: email.sender, subject: email.subject, date: email.date || email.timestamp }
    }, els.emailDetail);
    window.MailmateContext?.open(reference);
  }


  async function trashEmail(email) {
    const id = String(emailKey(email) || '');
    if (!id) return;
    if (!window.confirm('Move this message to Gmail Trash?')) return;

    const button = $('emailTrashBtn');
    if (button) {
      button.disabled = true;
      button.textContent = 'Moving…';
    }

    try {
      const response = await fetch(`${API_BASE}/api/gmail/messages/${encodeURIComponent(id)}`, { method: 'DELETE' });
      const detail = await response.json().catch(() => ({}));
      if (!response.ok) {
        const hint = detail.hint ? ` ${detail.hint}` : '';
        throw new Error((detail.error || `Gmail delete returned ${response.status}`) + hint);
      }

      if (state.data) {
        state.data.emails = (state.data.emails || []).filter(item => String(emailKey(item) || '') !== id);
        for (const key of ['needs_attention', 'waiting_on_others']) {
          state.data[key] = (state.data[key] || []).filter(item => attentionSourceId(item) !== id);
        }
        state.data.metrics = {
          ...(state.data.metrics || {}),
          emails: (state.data.emails || []).length,
          important: (state.data.needs_attention || []).length,
          actions: (state.data.needs_attention || []).length
        };
      }

      state.selectedEmailId = null;
      renderDashboard(state.data || { emails: [], needs_attention: [], waiting_on_others: [], metrics: {} });
      saveSessionSnapshot(state.data);
      window.KyleTools?.execute?.([{ tool: 'ui.toast', args: { message: 'Moved to Gmail Trash' } }]);
    } catch (error) {
      addError('Gmail: ' + error.message);
      if (button) {
        button.disabled = false;
        button.innerHTML = '<i class="far fa-trash-can"></i> Trash';
      }
    }
  }


  let workJobs = [];
  let selectedJobId = null;
  let countdownTimerInterval = null;

  function cleanJobTitle(title, subject = '') {
    const raw = title || subject || 'Work Task';
    let cleaned = raw.replace(/^(?:assignment|task|follow-?up|re|fwd|todo)[\s:]+/i, '').trim();
    if (!cleaned || cleaned.toLowerCase() === 'no subject') {
      cleaned = (subject && subject.toLowerCase() !== 'no subject') ? subject : 'DA Submission';
    }
    cleaned = cleaned.replace(/^(?:assignment|task|follow-?up|re|fwd|todo)[\s:]+/i, '').trim();
    if (!cleaned) cleaned = 'DA Submission';

    const acronyms = new Set(['da', 'os', 'ai', 'stt', 'tts', 'api', 'db', 'ui', 'ux', 'ml', 'llm', 'cse', 'ece', 'vit']);
    return cleaned.split(/\s+/).map(w => {
      const low = w.toLowerCase().replace(/[^\w]/g, '');
      if (acronyms.has(low)) return w.replace(/[a-zA-Z]+/g, low.toUpperCase());
      if (w === w.toUpperCase() && w.length > 1) return w;
      return w.charAt(0).toUpperCase() + w.slice(1);
    }).join(' ');
  }

  function formatRelativeDeadline(isoString) {
    if (!isoString) return 'No deadline set';
    try {
      const date = new Date(isoString);
      if (isNaN(date.getTime())) return isoString;
      const now = new Date();
      const diffMs = date.getTime() - now.getTime();
      const diffMins = Math.round(diffMs / 60000);

      const timeStr = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      const isToday = date.toDateString() === now.toDateString();
      const tomorrow = new Date(now);
      tomorrow.setDate(tomorrow.getDate() + 1);
      const isTomorrow = date.toDateString() === tomorrow.toDateString();

      if (diffMins > 0 && diffMins <= 60) {
        return `Due in ${diffMins} min`;
      }
      if (isToday) {
        return `Today · ${timeStr}`;
      }
      if (isTomorrow) {
        return `Tomorrow · ${timeStr}`;
      }
      const dayStr = date.toLocaleDateString([], { month: 'short', day: 'numeric' });
      return `${dayStr} · ${timeStr}`;
    } catch (_) {
      return isoString;
    }
  }

  function getJobStatusInfo(status) {
    switch (status) {
      case 'waiting_approval':
        return { label: 'Review', cls: 'badge-waiting_approval' };
      case 'auto_send_countdown':
        return { label: 'Auto-send', cls: 'badge-auto_send_countdown' };
      case 'needs_input':
        return { label: 'Needs input', cls: 'badge-needs_input' };
      case 'resolved_external':
        return { label: 'Replied', cls: 'badge-resolved_external' };
      case 'ignored_outbound':
        return { label: 'Ignored', cls: 'badge-cancelled' };
      case 'sent':
      case 'approved_sent':
        return { label: 'Sent', cls: 'badge-sent' };
      case 'completed':
        return { label: 'Complete', cls: 'badge-completed' };
      case 'cancelled':
        return { label: 'Cancelled', cls: 'badge-cancelled' };
      case 'failed':
        return { label: 'Failed', cls: 'badge-failed' };
      case 'preparing':
      case 'analyzing':
      case 'working':
      case 'generating':
      case 'researching':
      case 'planning':
      case 'reading_context':
      case 'drafting_reply':
      case 'creating_files':
      case 'verifying':
        return { label: 'Working', cls: 'badge-working' };
      default:
        return { label: 'Queued', cls: 'badge-queued' };
    }
  }


  const WORK_TERMINAL_STATUSES = new Set([
    'sent','approved_sent','resolved_external','ignored_outbound','completed','cancelled'
  ]);
  const WORK_ACTIVE_STATUSES = new Set([
    'queued','reading_context','planning','researching','generating',
    'drafting_reply','creating_files','verifying','preparing','working','analyzing'
  ]);

  function humanizeWorkStep(value) {
    const raw=String(value||'').trim();
    const key=raw.toLowerCase().replace(/\s+/g,'_');
    const exact={
      read_thread:'Reading email',reading_context:'Reading email',
      extract_requirements:'Understanding request',planning:'Planning next steps',
      research:'Researching',researching:'Researching',
      prepare_reply:'Preparing response',drafting_reply:'Preparing response',
      generating:'Preparing response',create_files:'Creating file',
      creating_files:'Creating file',verify:'Checking result',verifying:'Checking result',
      human_approval:'Ready for review',waiting_approval:'Ready for review'
    };
    if(exact[key]) return exact[key];
    if(/read|context/.test(key)) return 'Reading email';
    if(/require|understand|extract/.test(key)) return 'Understanding request';
    if(/research|search/.test(key)) return 'Researching';
    if(/draft|reply|generat|response/.test(key)) return 'Preparing response';
    if(/file|artifact|document|pdf/.test(key)) return 'Creating file';
    if(/verify|check/.test(key)) return 'Checking result';
    const clean=raw.replace(/\[[^\]]+\]\s*/g,'').replace(/[._:/-]+/g,' ').replace(/\s+/g,' ').trim();
    return clean ? clean[0].toUpperCase()+clean.slice(1) : 'Working';
  }

  function applyWorkInspectorState(job) {
    const status=String(job?.status||'');
    const automation=job?.type==='automation_run';
    const terminal=WORK_TERMINAL_STATUSES.has(status);
    const active=WORK_ACTIVE_STATUSES.has(status);
    const ready=status==='waiting_approval'||status==='auto_send_countdown';
    const needs=status==='needs_input', failed=status==='failed';
    const stateName=terminal?'terminal':active?'working':ready?'review':needs?'needs-input':failed?'failed':'neutral';

    const inspector=$('workRunInspector'), card=$('workPreparedCard'), tag=$('workPreparedTag');
    const summary=$('workPreparedSummary'), policy=$('workPolicyBanner'), reply=$('workReplyBox');
    const review=$('workReviewEmailBtn'), stepsBadge=$('workStepsStatusBadge');
    if(inspector) inspector.dataset.workState=stateName;
    if(card) card.dataset.workState=stateName;
    if(review&&!automation){review.hidden=false;review.innerHTML='<i class="fas fa-envelope-open-text"></i> Open source email';}
    if(stepsBadge) stepsBadge.textContent=terminal?'Complete':failed?'Failed':needs?'Needs input':active?'Working':ready?'Ready':'Current';

    let retry=$('workRetryJobBtn');
    if(!retry&&review?.parentElement){
      retry=document.createElement('button'); retry.id='workRetryJobBtn'; retry.type='button';
      retry.className='secondary-btn compact-btn work-retry-btn';
      retry.innerHTML='<i class="fas fa-rotate-right"></i> Retry';
      review.insertAdjacentElement('afterend',retry);
    }
    if(retry){
      retry.hidden=!failed||automation;
      retry.onclick=async()=>{retry.disabled=true;retry.innerHTML='<i class="fas fa-spinner fa-spin"></i> Retrying';
        try{await fetch(`${API_BASE}/api/work/jobs/${encodeURIComponent(job.id)}/run`,{method:'POST'});await renderWork(state.data);}
        catch(e){retry.disabled=false;retry.innerHTML='<i class="fas fa-rotate-right"></i> Retry';console.warn(e);}
      };
    }
    if(automation) return;

    if(terminal){
      if(tag) tag.hidden=true;
      if(reply) reply.style.display='none';
      if(policy) policy.style.display='none';
      if(summary){
        if(status==='resolved_external') summary.textContent='✓ Replied manually in Gmail. Mailmate verified a newer outbound message in this thread.';
        else if(status==='sent'||status==='approved_sent') summary.textContent='✓ Sent via Gmail. This Work item is resolved.';
        else if(status==='ignored_outbound') summary.textContent='This sent message was ignored because Kyle Work only acts on inbound requests.';
        else if(status==='cancelled') summary.textContent='This Work run was cancelled.';
        else summary.textContent='✓ Work completed.';
      }
      return;
    }
    if(active){
      const steps=Array.isArray(job.steps)?job.steps:[];
      const cur=[...steps].reverse().find(x=>!['done','completed'].includes(String(x?.status||'').toLowerCase()))||steps[steps.length-1];
      if(tag){tag.hidden=false;tag.innerHTML='<i class="fas fa-circle-notch fa-spin"></i> KYLE IS WORKING';}
      if(summary) summary.textContent=humanizeWorkStep(cur?.label||cur?.type||cur?.action||job.current_step||'Working');
      if(reply) reply.style.display='none';
      if(policy) policy.style.display='none';
      return;
    }
    if(ready){
      if(tag){tag.hidden=false;tag.innerHTML='<i class="fas fa-sparkles"></i> READY FOR REVIEW';}
      if(reply) reply.style.display='flex';
      if(policy) policy.style.display=status==='auto_send_countdown'?'':'none';
      return;
    }
    if(needs){
      if(tag){tag.hidden=false;tag.innerHTML='<i class="fas fa-circle-exclamation"></i> NEEDS INPUT';}
      if(reply) reply.style.display='none';
      if(policy) policy.style.display='';
      return;
    }
    if(failed){
      if(tag){tag.hidden=false;tag.innerHTML='<i class="fas fa-triangle-exclamation"></i> FAILED';}
      if(reply) reply.style.display='none';
      if(summary) summary.textContent='Kyle could not finish this Work item. Existing progress is preserved.';
      if(policy) policy.style.display='';
      const pb=$('workPolicyBadge'),pc=$('workPolicyCategory'),pe=$('workPolicyExplanation');
      if(pb) pb.textContent='ERROR'; if(pc) pc.textContent='Work could not finish';
      if(pe) pe.textContent=job.output?.error||job.error||job.current_step||'Retry or open the source email.';
    }
  }

  let workLivePollTimer = null;
  function updateWorkLivePolling() {
    const activeStatuses = new Set([
      'queued', 'reading_context', 'planning', 'researching',
      'generating', 'drafting_reply', 'creating_files',
      'verifying', 'preparing', 'working'
    ]);
    const hasActive = (workJobs || []).some(j => activeStatuses.has(j.status));
    const shouldPoll = hasActive || state.currentPage === 'work';

    if (shouldPoll && !workLivePollTimer) {
      workLivePollTimer = setInterval(async () => {
        if (document.hidden) return;
        try {
          const res = await fetch(`${API_BASE}/api/work/jobs`, { cache: 'no-store' });
          if (res.ok) {
            workJobs = normalizeTextTree(await res.json());
            renderOverviewWorkingNow();
            if (state.currentPage === 'work') {
              renderWorkRail(workJobs);
              const inspected = workJobs.find(j => j.id === selectedJobId) || workJobs[0];
              if (inspected && (!countdownTimerInterval || inspected.status !== 'auto_send_countdown')) {
                renderJobInspector(inspected);
              }
            }
          }
        } catch (e) {
          console.debug('Work live poll notice:', e);
        }
      }, 8000);
    } else if (!shouldPoll && workLivePollTimer) {
      clearInterval(workLivePollTimer);
      workLivePollTimer = null;
    }
  }

  function renderWorkRail(list) {
    const activeStatuses = new Set([
      'queued', 'reading_context', 'planning', 'researching',
      'generating', 'drafting_reply', 'creating_files',
      'verifying', 'preparing', 'working'
    ]);
    const readyStatuses = new Set(['waiting_approval', 'auto_send_countdown']);
    const needsInputStatuses = new Set(['needs_input']);
    const historyStatuses = new Set(['sent', 'approved_sent', 'resolved_external', 'ignored_outbound', 'completed', 'cancelled', 'failed']);

    const activeList = list.filter(j => activeStatuses.has(j.status));
    const readyList = list.filter(j => readyStatuses.has(j.status));
    const needsInputList = list.filter(j => needsInputStatuses.has(j.status));
    const historyList = list.filter(j => historyStatuses.has(j.status));

    function renderJobGroup(groupTitle, groupItems) {
      if (!groupItems || groupItems.length === 0) return '';
      const header = `<div class="work-rail-group-header">${escapeHtml(groupTitle)} <span>(${groupItems.length})</span></div>`;
      const itemsHtml = groupItems.map(job => {
        const isSel = job.id === selectedJobId;
        const displayTitle = cleanJobTitle(job.clean_title || job.title, job.source?.subject);
        const { label: statusLabel, cls: statusClass } = getJobStatusInfo(job.status);
        const senderDisplay = (job.source?.sender || '').split('<')[0].trim();
        const latestStep = (job.steps && job.steps.length > 0) ? job.steps[job.steps.length - 1] : null;
        const isHistory = historyStatuses.has(job.status);
        const stepProgress = activeStatuses.has(job.status) && latestStep ? humanizeWorkStep(latestStep.label || latestStep.type || latestStep.action || '') : '';
        const subtitle = isHistory ? [senderDisplay,'Gmail'].filter(Boolean).join(' · ') : (stepProgress || ((senderDisplay ? senderDisplay + ' · ' : '') + (decodeHtml(job.source?.subject) || 'Preparation work')));
        const successHistory=['sent','approved_sent','resolved_external','completed'].includes(job.status);
        const railIcon=successHistory?'<i class="fas fa-check"></i>':job.status==='failed'?'<i class="fas fa-triangle-exclamation"></i>':activeStatuses.has(job.status)?'<i class="fas fa-circle"></i>':'';
        return `
          <article class="work-item work-state-${escapeHtml(statusClass.replace('badge-', ''))} ${activeStatuses.has(job.status) ? 'is-buffering' : ''} ${isHistory ? 'is-history' : ''} ${isSel ? 'active' : ''}" role="button" tabindex="0" data-job-id="${escapeHtml(job.id)}" data-kyle-type="work-item" data-kyle-id="${escapeHtml(job.id)}" data-kyle-label="${escapeHtml(displayTitle)}">
            <div class="work-item-topline">
              <strong>${escapeHtml(displayTitle)}</strong>
              <span class="work-rail-status ${statusClass}">${railIcon}<span>${escapeHtml(statusLabel)}</span></span>
            </div>
            <p class="work-item-meta">${escapeHtml(subtitle)}</p>
          </article>
        `;
      }).join('');
      return header + itemsHtml;
    }

    let railHtml = '';
    if (activeList.length > 0) railHtml += renderJobGroup('Active', activeList);
    if (readyList.length > 0) railHtml += renderJobGroup('Ready for Review', readyList);
    if (needsInputList.length > 0) railHtml += renderJobGroup('Needs Input', needsInputList);
    if (historyList.length > 0) railHtml += renderJobGroup('History', historyList);

    els.workList.innerHTML = railHtml || '<article class="work-item active"><strong>No work tasks yet</strong><p>Actionable Gmail threads will appear here.</p></article>';

    [...els.workList.querySelectorAll('.work-item[data-job-id]')].forEach(item => {
      const jId = item.dataset.jobId;
      const job = list.find(j => j.id === jId);
      if (job) {
        const displayTitle = cleanJobTitle(job.clean_title || job.title, job.source?.subject);
        window.MailmateObjects?.register({
          type: 'work-item',
          id: job.id,
          label: displayTitle,
          page: 'work',
          metadata: {
            status: job.status,
            sender: job.source?.sender,
            subject: job.source?.subject
          }
        }, item);
      }
      item.addEventListener('click', () => {
        selectedJobId = item.dataset.jobId;
        renderWork(state.data);
      });
      item.addEventListener('keydown', event => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          item.click();
        }
      });
    });
  }

  async function renderWork(data) {
    try {
      // Entering Work is an explicit request to reconcile actionable Gmail into
      // WorkJobs. This prevents an empty Work pane when Overview already shows
      // Work-ready attention items.
      const res = await fetch(`${API_BASE}/api/work/jobs?ensure=1&reconcile=1`, { cache: 'no-store' });
      if (res.ok) {
        workJobs = normalizeTextTree(await res.json());
        if (state.currentPage === 'inbox' && state.data?.emails) {
          renderEmails(state.data.emails);
        }
      }
    } catch (e) {
      console.warn('Failed to load work jobs:', e);
    }

    renderOverviewWorkingNow();
    updateWorkLivePolling();

    if (!workJobs || workJobs.length === 0) {
      const attention = data?.needs_attention || [];
      els.workCount.textContent = `${attention.length} runs`;
      if (attention.length === 0) {
        els.workList.innerHTML = '<article class="work-item active"><strong>No work tasks yet</strong><p>Actionable Gmail threads will appear here.</p></article>';
        const emptyPl = $('workEmptyPlaceholder');
        if (emptyPl) emptyPl.style.display = 'block';
        const prepCard = $('workPreparedCard');
        if (prepCard) prepCard.style.display = 'none';
        const colls = $('workCollapsiblesGroup');
        if (colls) colls.style.display = 'none';
        return;
      }
    }

    els.workCount.textContent = `${workJobs.length} run${workJobs.length === 1 ? '' : 's'}`;

    if (!selectedJobId || !workJobs.some(j => j.id === selectedJobId)) {
      const preferredOrder = ['waiting_approval', 'auto_send_countdown', 'needs_input', 'working', 'preparing'];
      const found = workJobs.find(j => preferredOrder.includes(j.status)) || workJobs[0];
      if (found) selectedJobId = found.id;
    }

    renderWorkRail(workJobs);

    window.Kyle?.setContext({
      ...(state.data || {}),
      calendarEvents: state.calendarEvents,
      workJobs: workJobs,
      health: state.health,
      currentPage: state.currentPage
    });

    const activeJob = workJobs.find(j => j.id === selectedJobId) || workJobs[0];
    if (activeJob) {
      renderJobInspector(activeJob);
    }
  }

  function renderJobInspector(job) {
    if (countdownTimerInterval) {
      clearInterval(countdownTimerInterval);
      countdownTimerInterval = null;
    }

    const titleEl = $('runTitle');
    const subMetaEl = $('workSubMeta');
    const sourceSenderEl = $('workSourceSender');
    const deadlineBadgeEl = $('workDeadlineBadge');
    const statusEl = $('workRunStatus');
    const emptyPlaceholder = $('workEmptyPlaceholder');
    const preparedCard = $('workPreparedCard');
    const preparedSummary = $('workPreparedSummary');
    const collapsiblesGroup = $('workCollapsiblesGroup');
    const runInspector = $('workRunInspector');
    const activeStatuses = new Set(['queued', 'reading_context', 'planning', 'researching', 'generating', 'drafting_reply', 'creating_files', 'verifying', 'preparing', 'working']);
    if (runInspector) runInspector.classList.toggle('is-buffering', activeStatuses.has(job.status));

    const displayTitle = cleanJobTitle(job.clean_title || job.title, job.source?.subject);
    if (titleEl) titleEl.textContent = displayTitle;

    const senderDisplay = (job.source?.sender || 'Unknown').split('<')[0].trim();
    if (sourceSenderEl) sourceSenderEl.textContent = senderDisplay;
    if (deadlineBadgeEl) deadlineBadgeEl.textContent = formatRelativeDeadline(job.source?.deadline);
    if (subMetaEl) subMetaEl.style.display = 'block';

    const statusInfo = getJobStatusInfo(job.status);
    if (statusEl) {
      statusEl.className = `run-status ${statusInfo.cls}`;
      statusEl.textContent = statusInfo.label;
    }

    if (emptyPlaceholder) emptyPlaceholder.style.display = 'none';
    if (preparedCard) preparedCard.style.display = 'flex';
    if (collapsiblesGroup) collapsiblesGroup.style.display = 'flex';

    const isResolvedExternal = job.status === 'resolved_external';
    const isIgnoredOutbound = job.status === 'ignored_outbound';
    const isNeedsInput = job.status === 'needs_input';
    const isSent = job.status === 'sent' || job.status === 'approved_sent' || isResolvedExternal;
    const isCountdown = job.status === 'auto_send_countdown';
    const isAutomation = job.type === 'automation_run';
    const verdict = job.policy_verdict || {};
    const preparedArtifacts = (job.artifacts || []).filter(artifact => artifact.type === 'file');
    if (preparedSummary) {
      if (job.type === 'automation_run') {
        preparedSummary.textContent = job.status === 'working'
          ? 'Kyle is checking Gmail and Calendar now. Progress appears below.'
          : (job.output?.summary || 'Kyle completed this scheduled workspace check.');
      } else {
        const fileCopy = preparedArtifacts.length ? ` and ${preparedArtifacts.length} supporting file${preparedArtifacts.length === 1 ? '' : 's'}` : '';
        preparedSummary.textContent = `Kyle prepared a reply${fileCopy}.`;
      }
    }

    const reviewEmailBtn = $('workReviewEmailBtn');
    if (reviewEmailBtn) {
      const sourceId = String(job.source?.message_id || job.source?.id || '');
      reviewEmailBtn.hidden = isAutomation;
      reviewEmailBtn.disabled = !sourceId;
      reviewEmailBtn.onclick = () => openSourceEmail(sourceId, job.source || {});
    }

    // 1. Policy Banner
    const policyBanner = $('workPolicyBanner');
    const policyBadge = $('workPolicyBadge');
    const policyCat = $('workPolicyCategory');
    const policyExpl = $('workPolicyExplanation');
    const preparedTag = $('workPreparedTag');
    const replyBox = $('workReplyBox');
    const sourceDetails = $('workSourceDetails');
    if (preparedTag) preparedTag.innerHTML = isAutomation
      ? '<i class="fas fa-check"></i> SCHEDULED RUN'
      : '<i class="fas fa-sparkles"></i> READY FOR REVIEW';
    if (replyBox) replyBox.style.display = isAutomation ? 'none' : 'flex';
    if (sourceDetails) sourceDetails.hidden = isAutomation;

    if (policyBanner) {
      policyBanner.style.display = isAutomation ? 'none' : '';
      if (isAutomation) {
        // Automation summaries are already presented above; no mail-send policy applies.
      } else if (isResolvedExternal) {
        policyBanner.className = 'policy-banner auto-sent';
        if (policyBadge) policyBadge.textContent = 'VERIFIED IN GMAIL';
        if (policyCat) policyCat.textContent = 'Replied manually';
        if (policyExpl) policyExpl.textContent = 'Mailmate found a newer outbound Gmail message after the exact source email. This task is resolved.';
      } else if (isIgnoredOutbound) {
        policyBanner.className = 'policy-banner requires-approval';
        if (policyBadge) policyBadge.textContent = 'SENT MAIL';
        if (policyCat) policyCat.textContent = 'Not an inbound task';
        if (policyExpl) policyExpl.textContent = 'This message was sent by you. Kyle Work only starts from verified inbound requests.';
      } else if (isNeedsInput) {
        policyBanner.className = 'policy-banner requires-approval';
        if (policyBadge) policyBadge.textContent = 'NEEDS INPUT';
        if (policyCat) policyCat.textContent = 'Missing deliverable';
        if (policyExpl) policyExpl.textContent = job.missing_deliverable || job.output?.error || 'Required attachment or input is needed before preparing this reply.';
      } else if (isSent) {
        policyBanner.className = 'policy-banner auto-sent';
        if (policyBadge) policyBadge.textContent = 'DISPATCHED';
        if (policyCat) policyCat.textContent = 'Sent via Gmail';
        const wasAuto = (job.steps || []).some(s => s.auto_sent || s.label?.includes('automatically'));
        if (policyExpl) policyExpl.textContent = wasAuto ? 'Sent automatically via Safe Acknowledgement Rule.' : 'Approved and sent via Gmail.';
      } else if (isCountdown || verdict.auto_send_allowed) {
        policyBanner.className = 'policy-banner low-risk';
        if (policyBadge) policyBadge.textContent = 'LOW RISK';
        if (policyCat) policyCat.textContent = 'Auto-send eligible';
        if (policyExpl) policyExpl.textContent = verdict.explanation || 'Low risk · Simple acknowledgement · No attachment · No commitment · Known sender';
      } else {
        policyBanner.className = 'policy-banner requires-approval';
        if (policyBadge) policyBadge.textContent = 'READY FOR REVIEW';
        if (policyCat) policyCat.textContent = 'Prepared by Kyle';
        if (policyExpl) policyExpl.textContent = preparedArtifacts.length
          ? `Review the reply and ${preparedArtifacts.length} supporting file${preparedArtifacts.length === 1 ? '' : 's'} before sending.`
          : 'Review the prepared reply before sending.';
      }
    }

    // 2. Countdown Card
    const countdownCard = $('workCountdownCard');
    const countdownTimerText = $('countdownTimerText');
    const countdownProgressBar = $('countdownProgressBar');
    const cancelCountdownBtn = $('workCancelCountdownBtn');

    if (job.status === 'auto_send_countdown' && countdownCard) {
      countdownCard.style.display = 'block';
      const sendAt = job.countdown?.auto_send_at ? new Date(job.countdown.auto_send_at).getTime() : Date.now() + 20000;
      const duration = job.countdown?.duration || 20;

      const updateTicker = () => {
        const remainingMs = sendAt - Date.now();
        const remainingSec = Math.max(0, Math.ceil(remainingMs / 1000));
        if (countdownTimerText) countdownTimerText.textContent = `Sending in ${remainingSec}s…`;
        if (countdownProgressBar) countdownProgressBar.style.width = `${Math.min(100, Math.max(0, (remainingSec / duration) * 100))}%`;
        if (remainingSec <= 0) {
          clearInterval(countdownTimerInterval);
          countdownTimerInterval = null;
          if (countdownTimerText) countdownTimerText.textContent = 'Dispatching via Gmail…';
          setTimeout(() => renderWork(state.data), 1800);
        }
      };
      updateTicker();
      countdownTimerInterval = setInterval(updateTicker, 1000);

      if (cancelCountdownBtn) {
        cancelCountdownBtn.disabled = false;
        cancelCountdownBtn.onclick = async () => {
          clearInterval(countdownTimerInterval);
          countdownTimerInterval = null;
          cancelCountdownBtn.disabled = true;
          try {
            await fetch(`${API_BASE}/api/work/jobs/${encodeURIComponent(job.id)}/cancel-countdown`, { method: 'POST' });
            await renderWork(state.data);
          } catch (err) {
            console.warn('Cancel countdown failed:', err);
          }
        };
      }
    } else if (countdownCard) {
      countdownCard.style.display = 'none';
    }

    // 3. Reply Editor & Draft Badge
    const replyText = $('workReplyText');
    const draftBadge = $('workDraftBadge');
    if (replyText) {
      replyText.value = job.output?.suggested_reply || (job.reply_draft || {}).body || '';
      replyText.disabled = (job.status === 'sent' || job.status === 'approved_sent' || isResolvedExternal || isIgnoredOutbound);
    }
    if (draftBadge) {
      if (isResolvedExternal) {
        draftBadge.innerHTML = `<span style="color:#0d9488;"><i class="fas fa-envelope-open-text"></i> Manual Gmail reply verified</span>`;
      } else if (isIgnoredOutbound) {
        draftBadge.innerHTML = `<span style="color:#64748b;"><i class="fas fa-ban"></i> Sent mail ignored</span>`;
      } else if (job.status === 'sent' || job.status === 'approved_sent') {
        const verified = job.output?.gmail_send_verified === true;
        const sentId = job.output?.gmail_sent_message_id || '';
        draftBadge.innerHTML = `<span style="color:#1e8e48;"><i class="fas fa-check-circle"></i> ${verified ? 'Sent & verified in Gmail' : 'Sent via Gmail API'}${sentId ? ` · ${escapeHtml(sentId.slice(-8))}` : ''}</span>`;
      } else if (isNeedsInput) {
        draftBadge.innerHTML = `<span style="color:#dc2626;"><i class="fas fa-circle-exclamation"></i> Deliverable missing</span>`;
      } else if (job.output?.gmail_draft_id) {
        draftBadge.innerHTML = `<span style="color:#1e8e48;"><i class="fas fa-check-circle"></i> Gmail Draft Staged</span>`;
      } else {
        draftBadge.innerHTML = '<span>Local draft ready</span>';
      }
    }

    // 4. Action Buttons
    const saveDraftBtn = $('workSaveDraftBtn');
    if (saveDraftBtn) {
      saveDraftBtn.disabled = (job.status === 'sent' || job.status === 'approved_sent' || isResolvedExternal || isIgnoredOutbound || isNeedsInput);
      saveDraftBtn.onclick = async () => {
        saveDraftBtn.disabled = true;
        saveDraftBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving...';
        try {
          const res = await fetch(`${API_BASE}/api/work/jobs/${encodeURIComponent(job.id)}/save-draft`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reply: replyText.value })
          });
          if (res.ok) {
            saveDraftBtn.innerHTML = '<i class="fas fa-check"></i> Saved';
            setTimeout(() => {
              saveDraftBtn.disabled = false;
              saveDraftBtn.innerHTML = '<i class="fas fa-floppy-disk"></i> Save edit';
            }, 1400);
            await renderWork(state.data);
          } else {
            const json = await res.json().catch(() => ({}));
            const errMsg = json.error || res.statusText || 'Save failed';
            if (res.status === 403 || json.code === 'insufficient_scopes' || errMsg.includes('insufficient') || errMsg.includes('Permission')) {
              if (confirm('Google draft & send permissions (gmail.modify) are required to sync drafts with Gmail.\n\nYour current session only has read permissions. Would you like to reconnect Google now to grant permissions?')) {
                window.location.href = '/auth/google';
                return;
              }
            } else {
              alert('Save error: ' + errMsg);
            }
            saveDraftBtn.disabled = false;
            saveDraftBtn.innerHTML = '<i class="fas fa-floppy-disk"></i> Save edit';
          }
        } catch (err) {
          alert('Save error: ' + err.message);
          saveDraftBtn.disabled = false;
          saveDraftBtn.innerHTML = '<i class="fas fa-floppy-disk"></i> Save edit';
        }
      };
    }

    const rerunBtn = $('workRerunBtn');
    if (rerunBtn) {
      rerunBtn.disabled = (job.status === 'sent' || job.status === 'approved_sent' || isResolvedExternal || isIgnoredOutbound);
      rerunBtn.onclick = async () => {
        rerunBtn.disabled = true;
        rerunBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Rerunning...';
        try {
          await fetch(`${API_BASE}/api/work/jobs/${encodeURIComponent(job.id)}/run`, { method: 'POST' });
          await renderWork(state.data);
        } catch (_) {
          rerunBtn.disabled = false;
          rerunBtn.innerHTML = '<i class="fas fa-rotate-right"></i> Rerun';
        }
      };
    }

    const approveBtn = $('workApproveBtn');
    if (approveBtn) {
      if (isResolvedExternal) {
        approveBtn.disabled = true;
        approveBtn.innerHTML = '<i class="fas fa-check-double"></i> Manual reply verified';
      } else if (isIgnoredOutbound) {
        approveBtn.disabled = true;
        approveBtn.innerHTML = '<i class="fas fa-ban"></i> Sent mail ignored';
      } else if (isNeedsInput) {
        approveBtn.disabled = true;
        approveBtn.innerHTML = '<i class="fas fa-circle-question"></i> Needs Input';
      } else if (job.status === 'sent' || job.status === 'approved_sent') {
        approveBtn.disabled = true;
        approveBtn.innerHTML = '<i class="fas fa-check-double"></i> Sent via Gmail';
      } else {
        approveBtn.disabled = false;
        approveBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Approve &amp; Send';
        approveBtn.onclick = async () => {
          approveBtn.disabled = true;
          approveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Sending via Gmail...';
          try {
            const res = await fetch(`${API_BASE}/api/work/jobs/${encodeURIComponent(job.id)}/approve`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ reply: replyText ? replyText.value : undefined })
            });
            const json = await res.json();
            if (res.ok) {
              await renderWork(state.data);
            } else {
              const errMsg = json.error || res.statusText || 'Approval failed';
              if (res.status === 403 || json.code === 'insufficient_scopes' || errMsg.includes('insufficient') || errMsg.includes('Permission')) {
                if (confirm('Google draft & send permissions (gmail.modify) are required to approve and send replies via Gmail.\n\nYour current Google login only has read permissions. Would you like to reconnect Google now to grant draft & send permissions?')) {
                  window.location.href = '/auth/google';
                  return;
                }
              } else {
                alert('Approval failed: ' + errMsg);
              }
              approveBtn.disabled = false;
              approveBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Approve &amp; Send';
              await renderWork(state.data);
            }
          } catch (err) {
            alert('Network error: ' + err.message);
            approveBtn.disabled = false;
            approveBtn.innerHTML = '<i class="fas fa-paper-plane"></i> Approve &amp; Send';
          }
        };
      }
    }


    // 5. Generated Workspace Files
    const artifactsSection = $('workArtifactsSection');
    const artifactsList = $('workArtifactsList');
    if (artifactsSection && artifactsList) {
      const artifacts = (job.artifacts || []).filter(art => {
        if (art.type === 'email_draft' && !job.output?.gmail_draft_id) return false;
        return true;
      });
      if (artifacts.length > 0) {
        artifactsSection.style.display = 'flex';
        artifactsList.innerHTML = artifacts.map(art => {
          const isDocx = art.name?.endsWith('.docx');
          const isMd = art.name?.endsWith('.md');
          const isPdf = art.name?.endsWith('.pdf');
          const isFile = art.type === 'file';
          const icon = isDocx ? '<i class="fas fa-file-word" style="color:#2563eb;"></i>' :
                       isPdf ? '<i class="fas fa-file-pdf" style="color:#ef4444;"></i>' :
                       isMd ? '<i class="fas fa-file-lines" style="color:#0ea5e9;"></i>' :
                       isFile ? '<i class="fas fa-file"></i>' : '<i class="fas fa-envelope"></i>';
          const action = isFile
            ? `<a href="/api/work/jobs/${encodeURIComponent(job.id)}/artifacts/${encodeURIComponent(art.name)}" target="_blank" class="secondary-btn work-artifact-open"><i class="fas fa-arrow-up-right-from-square"></i> Open</a>`
            : `<span style="color:var(--muted);font-size:0.75rem;">Synced with Gmail</span>`;
          return `
            <div class="work-artifact-item">
              <div class="artifact-title">${icon} <span>${escapeHtml(art.name)}</span></div>
              ${action}
            </div>
          `;
        }).join('');
      } else {
        artifactsSection.style.display = 'none';
      }
    }

    // 6. Collapsible: What Kyle did (Timeline)
    const stepsSummaryTitle = $('workStepsSummaryTitle');
    const stepsStatusBadge = $('workStepsStatusBadge');
    const timeline = $('agentTimeline');
    const steps = job.steps || [];

    if (stepsSummaryTitle) stepsSummaryTitle.textContent = `What Kyle did · ${steps.length} action${steps.length === 1 ? '' : 's'}`;
    if (stepsStatusBadge) {
      stepsStatusBadge.textContent = (job.status === 'sent' || job.status === 'approved_sent') ? 'Complete' :
                                     job.status === 'auto_send_countdown' ? 'Countdown active' : 'Ready for review';
    }
    if (timeline) {
      timeline.innerHTML = steps.map(s => {
        const isDone = s.status === 'done';
        const isCountdown = s.status === 'active';
        const isWaiting = s.status === 'pending' || s.status === 'waiting_approval';
        const isErr = s.status === 'error';
        const icon = isDone ? '<i class="fas fa-check" style="color:#1e8e48;"></i>' :
                     isCountdown ? '<i class="fas fa-clock fa-spin" style="color:#f59e0b;"></i>' :
                     isWaiting ? '<i class="far fa-clock" style="color:#b87810;"></i>' :
                     isErr ? '<i class="fas fa-triangle-exclamation" style="color:#ef4444;"></i>' :
                     '<i class="fas fa-circle-notch fa-spin"></i>';

        const stepLabel = humanizeWorkStep(s.label || s.type || s.action || 'Working');
        let stepDetail = isDone ? 'Completed successfully' : isCountdown ? 'Auto-send countdown running' : isWaiting ? 'Awaiting human review' : 'In progress';
        if (s.observation) {
          if (s.observation.ok) {
            stepDetail = s.observation.summary || s.observation.name || (s.observation.results ? `Found ${s.observation.results_count} sources` : 'Completed successfully');
          } else {
            stepDetail = s.observation.error || s.observation.reason || 'Action blocked or failed';
          }
        }
        return `
          <article>
            <span>${icon}</span>
            <div>
              <strong>${escapeHtml(stepLabel)}</strong>
              <p>${escapeHtml(stepDetail)}</p>
            </div>
          </article>
        `;
      }).join('');
    }

    // 7. Collapsible: Source Email Card
    const sourceSenderFull = $('workSourceSenderFull');
    const sourceSubjectFull = $('workSourceSubjectFull');
    const sourceSnippet = $('workSourceSnippet');
    const sourceMeta = $('workSourceMeta');
    const sourceSubjectBadge = $('workSourceSubjectBadge');

    if (sourceSenderFull) sourceSenderFull.textContent = job.source?.sender || 'Unknown sender';
    if (sourceSubjectFull) sourceSubjectFull.textContent = job.source?.subject || 'No subject';
    if (sourceSubjectBadge) sourceSubjectBadge.textContent = (job.source?.subject || 'Email').slice(0, 24);
    if (sourceSnippet) sourceSnippet.textContent = decodeHtml(job.source?.snippet || job.source?.body) || 'No snippet available.';
    if (sourceMeta) {
      sourceMeta.textContent = job.source?.deadline ? `Deadline: ${job.source.deadline}` : 'Inbound Gmail thread';
    }
    applyWorkInspectorState(job);
  }

  // Autopilot Settings Management
  async function loadWorkSettings() {
    try {
      const res = await fetch(`${API_BASE}/api/work/settings`);
      if (res.ok) {
        const settings = await res.json();
        const autoPrepEl = $('settingAutoPrep');
        const createDraftsEl = $('settingCreateDrafts');
        const statusTag = $('autopilotStatusTag');

        if (autoPrepEl) autoPrepEl.checked = settings.auto_prep !== false;
        if (createDraftsEl) createDraftsEl.checked = settings.create_gmail_drafts !== false;

        const modeRadios = document.querySelectorAll('input[name="autoSendMode"]');
        modeRadios.forEach(r => {
          r.checked = r.value === (settings.auto_send_mode || 'safe_replies');
        });

        if (statusTag) {
          const modeMap = {
            off: 'Off',
            prepare: 'Prepare only',
            safe_replies: 'Safe replies only',
            full_prepare: 'Full preparation',
            never: 'Prepare only',
            safe_only: 'Safe replies only',
            trusted_only: 'Trusted contacts only',
            custom: 'Custom rules'
          };
          statusTag.textContent = modeMap[settings.auto_send_mode] || 'Safe replies only';
        }
      }
    } catch (e) {
      console.warn('Failed to load work settings:', e);
    }
  }

  function bindAutopilotSettings() {
    const autoPrepEl = $('settingAutoPrep');
    const createDraftsEl = $('settingCreateDrafts');
    const modeRadios = document.querySelectorAll('input[name="autoSendMode"]');

    const saveSettings = async () => {
      let selectedMode = 'safe_replies';
      modeRadios.forEach(r => { if (r.checked) selectedMode = r.value; });

      const payload = {
        auto_prep: autoPrepEl ? autoPrepEl.checked : true,
        create_gmail_drafts: createDraftsEl ? createDraftsEl.checked : true,
        auto_send_mode: selectedMode
      };

      try {
        await fetch(`${API_BASE}/api/work/settings`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const statusTag = $('autopilotStatusTag');
        if (statusTag) {
          const modeMap = {
            off: 'Off',
            prepare: 'Prepare only',
            safe_replies: 'Safe replies only',
            full_prepare: 'Full preparation',
            never: 'Prepare only',
            safe_only: 'Safe replies only',
            trusted_only: 'Trusted contacts only',
            custom: 'Custom rules'
          };
          statusTag.textContent = modeMap[selectedMode] || 'Safe replies only';
        }
      } catch (e) {
        console.warn('Failed to save autopilot settings:', e);
      }
    };

    autoPrepEl?.addEventListener('change', saveSettings);
    createDraftsEl?.addEventListener('change', saveSettings);
    modeRadios.forEach(r => r.addEventListener('change', saveSettings));

    const kyleVoiceEl = $('settingKyleVoice');
    if (kyleVoiceEl) {
      const isMuted = localStorage.getItem('kyle_muted') === 'true';
      kyleVoiceEl.checked = !isMuted;
      kyleVoiceEl.addEventListener('change', () => {
        const shouldMute = !kyleVoiceEl.checked;
        localStorage.setItem('kyle_muted', shouldMute ? 'true' : 'false');
        if (window.Kyle?.store) {
          window.Kyle.store.muted = shouldMute;
        }
        window.dispatchEvent(new CustomEvent('kyle:toggle-mute'));
      });
      window.addEventListener('kyle:mute-change', event => {
        kyleVoiceEl.checked = !event.detail?.muted;
      });
    }
  }


  async function renderStatus() {
    const h = state.health || {};
    const rows = [
      ['Google OAuth', h.googleClientConfigured],
      ['Gmail read session', h.gmailAuthenticated || Boolean(state.userId)],
      ['Gmail draft & send permission', h.gmailWrite],
      ['Google Calendar read/write', h.calendarReadWrite],
      ['Gemini cloud fallback', h.geminiConfigured],
      ['Local Privacy Gate', true],
      ['Browser speech recognition', Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)],
      ['Browser text-to-speech', 'speechSynthesis' in window],
      ['Kyle action registry', Boolean(window.KyleActions)]
    ];
    els.statusList.innerHTML = rows.map(([label, ok]) => {
      const statusText = ok ? 'Ready' : (label.startsWith('Gemini') ? 'Optional · off' : 'Unavailable');
      return `<li data-status="${statusText}"><strong>${escapeHtml(label)}</strong></li>`;
    }).join('');

    try {
      const res = await fetch(`${API_BASE}/api/system/context?reconcile=false`);
      if (res.ok) {
        const sys = await res.json();
        const vEl = $('sysCtxVersion');
        const wEl = $('sysCtxWorkState');
        const cEl = $('sysCtxConflicts');
        const fEl = $('sysCtxFreshness');
        const headerBadge = $('systemContextVersion');
        if (headerBadge) headerBadge.textContent = `v${sys.context_version || 1}`;
        if (vEl) vEl.textContent = `v${sys.context_version || 1}`;
        if (wEl) wEl.textContent = `${sys.work?.counts?.active || 0} active · ${sys.work?.counts?.waiting_approval || 0} ready · ${sys.work?.counts?.needs_input || 0} needs input`;
        if (cEl) cEl.textContent = `${sys.calendar?.conflict_count || 0} clashes`;
        if (fEl) fEl.textContent = `Work: ${sys.freshness?.work ? new Date(sys.freshness.work).toLocaleTimeString() : 'N/A'}`;
      }
    } catch (_) {}
  }

  function renderIntegrations() {
    const h = state.health || {};
    const rows = [
      ['Google Gmail (Draft & Send)', h.gmailWrite],
      ['Google Calendar', h.calendarReadWrite],
      ['Gemini cloud fallback', h.geminiConfigured],
      ['Local Privacy Gate', true],
      ['Browser speech recognition', Boolean(window.SpeechRecognition || window.webkitSpeechRecognition)],
      ['Browser TTS', 'speechSynthesis' in window]
    ];
    els.integrationList.innerHTML = rows.map(([label, ok]) => {
      const statusText = ok ? 'Connected' : (label.startsWith('Gemini') ? 'Optional · off' : 'Unavailable');
      return `<li data-status="${statusText}"><strong>${escapeHtml(label)}</strong></li>`;
    }).join('');
  }


  function clearSteps() {
    [...els.processList.querySelectorAll('li')].forEach(li => li.classList.remove('active', 'done'));
    els.processState.textContent = 'Working';
  }

  function setStep(step, status, label) {
    const item = els.processList.querySelector(`[data-step="${step}"]`);
    if (!item) return;
    item.classList.toggle('active', status === 'active');
    item.classList.toggle('done', status === 'done');
    if (label) item.lastChild.textContent = label;
  }

  function addError(message) {
    const existing = els.errorBox.textContent.includes('No errors') ? '' : els.errorBox.textContent + '\n';
    els.errorBox.textContent = existing + `[${new Date().toLocaleTimeString()}] ${message}`;
  }

  function emailKey(email) {
    return email?.id || email?.gmail_id || email?.subject || null;
  }

  function emailReference(email) {
    return {
      type: 'email',
      id: String(emailKey(email) || ''),
      label: email?.subject || 'No subject'
    };
  }


  function attentionSourceId(item) {
    return String(item?.source_message_id || item?.message_id || item?.email_id || '').trim();
  }

  function isImportant(email) {
    const context = email?.context_scores;
    if (context && Object.prototype.hasOwnProperty.call(context, 'attention_allowed')) {
      return Boolean(context.attention_allowed);
    }
    return Boolean(email?.is_starred || (email?.labels || []).includes('IMPORTANT'));
  }

  function senderName(sender) {
    return String(sender || 'Unknown sender').replace(/\s*<[^>]+>\s*$/, '').replace(/^"|"$/g, '').trim() || 'Unknown sender';
  }

  function formatDate(value, long = false) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, 12);
    if (long) return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const today = new Date();
    if (date.toDateString() === today.toDateString()) return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function escapeHtml(value) {
    const div = document.createElement('div');
    div.textContent = repairTextEncoding(value);
    return div.innerHTML;
  }

  function mountDeveloperSettings() {
    const host = $('developerSettingsContent');
    const statusPanel = $('tab-status');
    const integrationsPanel = $('tab-integrations');
    if (!host || !statusPanel || !integrationsPanel) return;

    const heading = document.createElement('div');
    heading.className = 'settings-developer-heading';
    heading.innerHTML = '<div><p class="section-label">Developer diagnostics</p><h2>Runtime and connections</h2></div><span>Local session</span>';
    host.appendChild(heading);
    [...statusPanel.children, ...integrationsPanel.children].forEach(child => host.appendChild(child));

    const toggle = $('settingDeveloperMode');
    const enabled = localStorage.getItem('mailmate-developer-mode') === 'true';
    if (toggle) {
      toggle.checked = enabled;
      toggle.addEventListener('change', () => setDeveloperMode(toggle.checked));
    }
    setDeveloperMode(enabled, false);
  }

  function setDeveloperMode(enabled, animate = true) {
    const host = $('developerSettingsContent');
    const toggle = $('settingDeveloperMode');
    localStorage.setItem('mailmate-developer-mode', enabled ? 'true' : 'false');
    if (toggle) toggle.checked = enabled;
    if (!host) return;
    if (enabled) {
      host.hidden = false;
      if (animate) requestAnimationFrame(() => host.classList.add('is-visible'));
      else host.classList.add('is-visible');
      renderStatus();
      renderIntegrations();
      return;
    }
    host.classList.remove('is-visible');
    if (!animate) host.hidden = true;
    else window.setTimeout(() => { if (!host.classList.contains('is-visible')) host.hidden = true; }, 220);
  }

  function repairTextEncoding(value) {
    let text = String(value ?? '');
    const replacements = new Map([
      ['\u00c3\u00a2\u00e2\u201a\u00ac\u00c2\u00a2', '\u2022'],
      ['\u00e2\u20ac\u00a2', '\u2022'], ['\u00c2\u00b7', '\u00b7'],
      ['\u00e2\u20ac\u201c', '\u2013'], ['\u00e2\u20ac\u201d', '\u2014'],
      ['\u00e2\u20ac\u00a6', '\u2026'], ['\u00e2\u20ac\u2122', '\u2019'],
      ['\u00e2\u20ac\u02dc', '\u2018'], ['\u00e2\u20ac\u0153', '\u201c'],
      ['\u00e2\u20ac\u009d', '\u201d'], ['\u00e2\u20ac\u0152', ''],
      ['\u00e2\u20ac\u008d', ''], ['\u00c2 ', ' ']
    ]);
    replacements.forEach((replacement, broken) => { text = text.split(broken).join(replacement); });
    return text.replace(/[\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, '');
  }

  function normalizeTextTree(value) {
    if (typeof value === 'string') return repairTextEncoding(value);
    if (Array.isArray(value)) return value.map(normalizeTextTree);
    if (value && typeof value === 'object') {
      Object.keys(value).forEach(key => { value[key] = normalizeTextTree(value[key]); });
    }
    return value;
  }

  /**
   * Decode HTML entities (e.g. &#39; → ') that Gmail puts in snippets,
   * then safely re-escape so the result is safe to insert into innerHTML.
   */
  function decodeHtml(value) {
    const ta = document.createElement('textarea');
    ta.innerHTML = String(value ?? '');
    return repairTextEncoding(ta.value)
      .replace(/[\u034f\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]/g, '')
      .replace(/[ \t]{2,}/g, ' ')
      .trim();
  }

  function safeSnippet(value) {
    return escapeHtml(decodeHtml(value));
  }

  /**
   * Safely render plain-text email body: escape HTML, linkify URLs, and
   * convert newlines to <br> so the message displays with proper formatting.
   * Also strips MSO/Outlook conditional comment artifacts (<!--[if !mso]><!-->
   * etc.) that the server-side HTML parser lets through into extracted text.
   */
  function linkifyText(value) {
    // 0. Strip MSO/Outlook conditional comment markers BEFORE HTML-escaping.
    //    These leak into the plain-text body as literal strings like:
    //      <!--[if !mso]><!-->   <!--[if false]><!-->   <!--<![endif]-->
    //    Fixing here means already-cached bodies in state.fullMessages are
    //    cleaned immediately, with no server restart required.
    let text = String(value ?? '');
    // Full conditional blocks: <!--[if ...]>...<![endif]-->
    text = text.replace(/<!--\[if[^\]]*\]>[\s\S]*?<!\[endif\]-->/gi, '');
    // Opening markers: <!--[if ...]><!-->  or  <!--[if ...]>
    text = text.replace(/<!--\[if[^\]]*\]><!-->/gi, '');
    text = text.replace(/<!--\[if[^\]]*\]>/gi, '');
    // Closing markers: <!--<![endif]-->  and  <!--[endif]-->
    text = text.replace(/<!--<!\[endif\]-->/gi, '');
    text = text.replace(/<!--\[endif\]-->/gi, '');
    // Bare empty comment shorthand: <!-->
    text = text.replace(/<!-{2,}>/g, '');

    // 1. Escape all HTML entities
    const escaped = escapeHtml(text);
    // 2. Linkify angle-bracket wrapped URLs: &lt;https://...&gt;
    const withAngle = escaped.replace(
      /&lt;(https?:\/\/[^\s&>]+?)&gt;/gi,
      (_, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer" class="email-link">${url}</a>`
    );
    // 3. Linkify remaining bare URLs not already inside an <a> tag
    const withLinks = withAngle.replace(
      /(?<![">/])(https?:\/\/[^\s<>"')\]]+)/gi,
      url => `<a href="${url}" target="_blank" rel="noopener noreferrer" class="email-link">${url}</a>`
    );
    // 4. Convert newlines to <br> for readable paragraph layout
    return withLinks.replace(/\n/g, '<br>');
  }

  function conciseActionTitle(value) {
    const text = String(value || '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    const cleaned = text
      .replace(/^(urgent|important|action required)\s*[:\-]\s*/i, '')
      .replace(/\b(due|submit|submission)\b.*$/i, match => match);
    const first = cleaned.split(/[.;]/)[0].trim();
    return first.length > 58 ? first.slice(0, 55).trimEnd() + '…' : first;
  }

  function startOfWeek(date) {
    const d = new Date(date);
    d.setHours(0, 0, 0, 0);
    const mondayOffset = (d.getDay() + 6) % 7;
    d.setDate(d.getDate() - mondayOffset);
    return d;
  }

  function addDays(date, days) {
    const d = new Date(date);
    d.setDate(d.getDate() + days);
    return d;
  }

  function toLocalDateInput(date) {
    const d = new Date(date);
    const offset = d.getTimezoneOffset();
    return new Date(d.getTime() - offset * 60000).toISOString().slice(0, 10);
  }

  function calendarRange() {
    const start = new Date(state.calendarWeekStart);
    const end = addDays(start, 7);
    return { start, end };
  }

  function renderUpcomingFromContext() {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const items = dedupeOverviewUpcoming(combinedCalendarItems()
      .map(item => ({ ...item, parsedStart: parseEventStart(item) }))
      .filter(item => item.parsedStart && item.parsedStart >= today && !/^no subject$/i.test(String(item.title || '').trim()))
      .sort((a, b) => a.parsedStart - b.parsedStart))
      .slice(0, 6);
    if (!items.length) {
      els.upcomingList.innerHTML = '<p class="overview-empty-copy">Nothing upcoming.</p>';
      return;
    }
    const groups = new Map();
    items.forEach(item => {
      const key = item.parsedStart.toDateString();
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    els.upcomingList.innerHTML = [...groups.entries()].map(([day, dayItems]) => {
      const date = dayItems[0].parsedStart;
      const tomorrow = new Date(today); tomorrow.setDate(tomorrow.getDate() + 1);
      const label = date.toDateString() === today.toDateString() ? 'Today' : date.toDateString() === tomorrow.toDateString() ? 'Tomorrow' : date.toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
      return `<div class="upcoming-day"><p>${escapeHtml(label)}</p>${dayItems.map(item => `<button type="button" data-upcoming-id="${escapeHtml(item.id)}"><time>${item.all_day ? 'All day' : escapeHtml(item.parsedStart.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))}</time><span><strong>${escapeHtml(item.title || 'Calendar item')}</strong><small>${item.source === 'deadline' || item.source === 'ai' ? 'Email deadline' : 'Calendar'}</small></span><i class="fas fa-arrow-right"></i></button>`).join('')}</div>`;
    }).join('');
    els.upcomingList.querySelectorAll('[data-upcoming-id]').forEach(button => button.addEventListener('click', () => {
      const event = items.find(item => String(item.id) === button.dataset.upcomingId);
      if (event && event.source !== 'deadline') state.calendarSelectedEventId = event.id;
      showTab('calendar');
      requestAnimationFrame(() => document.querySelector(`[data-calendar-event="${CSS.escape(button.dataset.upcomingId)}"]`)?.focus());
    }));
  }

  function dedupeOverviewUpcoming(items) {
    const stop = new Set(['a','an','the','at','by','due','for','on','in','of','to','and','subject','am','pm','submission','submit']);
    const words = value => new Set(String(value || '').toLowerCase()
      .replace(/\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\b/g, ' ')
      .replace(/\b\d+(?:st|nd|rd|th)?\b/g, ' ')
      .replace(/[^a-z0-9]+/g, ' ').split(/\s+/).filter(word => word.length > 1 && !stop.has(word)));
    const result = [];
    for (const item of items || []) {
      const marker = item.agent_harness?.agent_harness_marker || item.marker || '';
      const start = item.parsedStart?.getTime?.() || 0;
      const tokens = words(item.title);
      const duplicateIndex = result.findIndex(existing => {
        const existingMarker = existing.agent_harness?.agent_harness_marker || existing.marker || '';
        if (marker && existingMarker && marker === existingMarker) return true;
        if (Math.abs((existing.parsedStart?.getTime?.() || 0) - start) > 15 * 60 * 1000) return false;
        const other = words(existing.title);
        const shared = [...tokens].filter(token => other.has(token)).length;
        return shared >= 2 && shared / Math.max(1, Math.min(tokens.size, other.size)) >= 0.66;
      });
      if (duplicateIndex < 0) {
        result.push(item);
        continue;
      }
      const current = result[duplicateIndex];
      const itemScore = (item.source === 'google' ? 4 : item.source === 'ai' ? 3 : 1) - String(item.title || '').length / 500;
      const currentScore = (current.source === 'google' ? 4 : current.source === 'ai' ? 3 : 1) - String(current.title || '').length / 500;
      if (itemScore > currentScore) result[duplicateIndex] = item;
    }
    return result;
  }

  function humanWhen(value) {
    if (!value) return '';
    const raw = String(value);
    if (/within|tomorrow|today|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday/i.test(raw)) return raw;
    const date = new Date(raw);
    return Number.isNaN(date.getTime()) ? raw : formatDate(raw, true);
  }

  function parseEventStart(event) {
    if (!event?.start) return null;
    if (event.all_day || String(event.start).length <= 10) {
      const d = new Date(`${String(event.start).slice(0, 10)}T00:00:00`);
      return Number.isNaN(d.getTime()) ? null : d;
    }
    const d = new Date(event.start);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function parseEventEnd(event) {
    if (!event?.end) return null;
    if (event.all_day || String(event.end).length <= 10) {
      const d = new Date(`${String(event.end).slice(0, 10)}T00:00:00`);
      return Number.isNaN(d.getTime()) ? null : d;
    }
    const d = new Date(event.end);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function deadlineToCalendarItem(item, index) {
    const raw = String(item.deadline || '').trim();
    const now = new Date();
    let start = null;
    let allDay = true;

    const within = raw.match(/within\s+(\d+)\s*hours?/i);
    if (within) {
      start = new Date(now.getTime() + Number(within[1]) * 3600000);
      allDay = false;
    } else if (/tomorrow/i.test(raw)) {
      start = addDays(new Date(now.getFullYear(), now.getMonth(), now.getDate()), 1);
    } else if (/today|tonight/i.test(raw)) {
      start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    } else {
      const parsed = new Date(raw);
      if (!Number.isNaN(parsed.getTime())) {
        start = parsed;
        allDay = !/[T:]\d/.test(raw);
      }
    }

    if (!start) return null;
    const end = allDay ? addDays(start, 1) : new Date(start.getTime() + 20 * 60000);

    const sourceId = attentionSourceId(item);
    return {
      id: `deadline-${item.source_message_id || item.id || index}`,
      title: item.subject || item.title || conciseActionTitle(item.description) || 'Email deadline',
      description: item.description || item.reason || '',
      start: allDay ? toLocalDateInput(start) : start.toISOString(),
      end: allDay ? toLocalDateInput(end) : end.toISOString(),
      all_day: allDay,
      source: 'deadline',
      blocking: false,
      conflict: false,
      conflict_with: [],
      marker: sourceId ? `gmail-${sourceId}` : '',
      deadline_label: raw
    };
  }

  function combinedCalendarItems() {
    const dismissed = new Set([
      ...(state.data?.calendar_dismissed_markers || []),
      ...state.calendarDismissedMarkers
    ]);
    const managedMarkers = new Set(
      state.calendarEvents
        .filter(event => event.source === 'ai')
        .map(event => event.agent_harness?.agent_harness_marker)
        .filter(Boolean)
    );

    const deadlines = (state.data?.needs_attention || [])
      .filter(item => item.deadline)
      .filter(item => {
        const sourceId = attentionSourceId(item);
        const marker = sourceId ? `gmail-${sourceId}` : '';
        if (!marker) return true;
        // Do not render a second virtual deadline when the real AI event exists,
        // and do not resurrect a deadline the user explicitly deleted.
        return !managedMarkers.has(marker) && !dismissed.has(marker);
      })
      .map(deadlineToCalendarItem)
      .filter(Boolean);

    return window.CalendarConflicts.deduplicate([...state.calendarEvents, ...deadlines]);
  }

  function localConflictPass(events) {
    const result = window.CalendarConflicts.annotate(events || []);
    state.calendarConflictPairs = result.pairs;
    return result.events;
  }

  function initCalendarControls() {
    $('calendarTodayBtn')?.addEventListener('click', () => {
      state.calendarWeekStart = startOfWeek(new Date());
      refreshCalendar(true);
    });
    $('calendarPrevBtn')?.addEventListener('click', () => {
      state.calendarWeekStart = addDays(state.calendarWeekStart, -7);
      refreshCalendar(true);
    });
    $('calendarNextBtn')?.addEventListener('click', () => {
      state.calendarWeekStart = addDays(state.calendarWeekStart, 7);
      refreshCalendar(true);
    });
    $('newCalendarEventBtn')?.addEventListener('click', () => openCalendarModal());
    $('calendarModalClose')?.addEventListener('click', closeCalendarModal);
    $('calendarCancelBtn')?.addEventListener('click', closeCalendarModal);
    $('calendarModal')?.addEventListener('click', event => {
      if (event.target === $('calendarModal')) closeCalendarModal();
    });
    $('calendarEventForm')?.addEventListener('submit', saveCalendarEvent);
    $('calendarDeleteEventBtn')?.addEventListener('click', deleteSelectedCalendarEvent);

    window.addEventListener('harness:calendar-refresh', () => refreshCalendar(true));
  }

  function setCalendarSyncState(kind, text) {
    const el = $('calendarSyncState');
    if (!el) return;
    el.dataset.state = kind;
    el.lastChild.textContent = ` ${text}`;
  }

  async function refreshCalendar(force = false) {
    if (state.calendarSyncing && !force) return state.calendarEvents;
    const requestSeq = ++state.calendarSyncSeq;
    state.calendarSyncing = true;
    setCalendarSyncState('syncing', 'Syncing');

    const { start, end } = calendarRange();
    try {
      const url = new URL(`${API_BASE}/api/calendar/events`);
      url.searchParams.set('start', start.toISOString());
      url.searchParams.set('end', end.toISOString());
      url.searchParams.set('limit', '250');
      if (force) url.searchParams.set('_', Date.now());

      const response = await fetch(url);
      if (!response.ok) throw new Error(`Calendar returned ${response.status}`);
      const events = await response.json();
      if (requestSeq !== state.calendarSyncSeq) return state.calendarEvents;
      state.calendarEvents = localConflictPass(Array.isArray(events) ? events : []);
      state.calendarLastSyncAt = new Date();

      renderCalendar();
      renderUpcomingFromContext();
      setCalendarSyncState('ready', `Synced ${state.calendarLastSyncAt.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`);
      window.Kyle?.setContext({
        ...(state.data || {}),
        calendarEvents: state.calendarEvents,
        workJobs: workJobs,
        health: state.health,
        currentPage: state.currentPage
      });
      return state.calendarEvents;
    } catch (error) {
      console.warn('Calendar sync failed:', error);
      setCalendarSyncState('error', 'Sync failed');
      addError('Calendar: ' + error.message);
      return state.calendarEvents;
    } finally {
      if (requestSeq === state.calendarSyncSeq) state.calendarSyncing = false;
    }
  }

  function startInboxAutoSync() {
    if (state.inboxSyncTimer) clearInterval(state.inboxSyncTimer);
    state.inboxSyncTimer = setInterval(() => {
      if (document.hidden) return;
      if (!['overview', 'inbox'].includes(state.currentPage)) return;
      loadInbox(false);
    }, 60000);
  }

  function startCalendarAutoSync() {
    if (state.calendarSyncTimer) clearInterval(state.calendarSyncTimer);
    // Google Calendar is authoritative. Focus/visibility refreshes handle active returns.
    state.calendarSyncTimer = setInterval(() => {
      if (!document.hidden) refreshCalendar(false);
    }, 60000);

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) refreshCalendar(false);
    });
    window.addEventListener('focus', () => refreshCalendar(false));
  }

  function renderCalendar() {
    const grid = $('calendarGrid');
    const allDay = $('calendarAllDay');
    if (!grid || !allDay) return;

    const previousScrollTop = grid.scrollTop;
    const hadTimeline = grid.dataset.rendered === 'true';
    const { start: weekStart, end: weekEnd } = calendarRange();
    const visibleItems = combinedCalendarItems().filter(event => {
      const start = parseEventStart(event);
      const end = parseEventEnd(event) || start;
      return start && end && start < weekEnd && end >= weekStart;
    });
    const weekItems = localConflictPass(visibleItems);

    const rangeLabel = $('calendarRangeLabel');
    if (rangeLabel) {
      const endDate = addDays(weekStart, 6);
      rangeLabel.textContent = `${weekStart.toLocaleDateString([], { month: 'short', day: 'numeric' })} – ${endDate.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' })}`;
    }

    renderAllDayRow(allDay, weekStart, weekItems);
    renderTimedGrid(grid, weekStart, weekItems);
    renderConflictAlert();
    grid.dataset.rendered = 'true';
    requestAnimationFrame(() => {
      grid.scrollTop = hadTimeline && previousScrollTop > 0 ? previousScrollTop : 5 * 56;
    });
  }

  function renderAllDayRow(container, weekStart, items) {
    const today = new Date();
    let html = '<div class="calendar-all-day-label">all-day / deadlines</div>';

    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const day = addDays(weekStart, dayIndex);
      const dayKey = toLocalDateInput(day);
      const isToday = day.toDateString() === today.toDateString();
      const dayItems = items.filter(event => {
        if (!event.all_day) return false;
        return String(event.start).slice(0, 10) === dayKey;
      });

      html += `
        <div class="calendar-all-day-cell">
          <div class="calendar-day-head ${isToday ? 'is-today' : ''}">
            <small>${day.toLocaleDateString([], { weekday: 'short' })}</small>
            <strong>${day.getDate()}</strong>
          </div>
          <div class="calendar-all-day-events">
            ${dayItems.map(event => `
              <button class="calendar-allday-chip ${eventTypeClass(event)} urgency-${eventUrgency(event)} ${event.conflict ? 'conflict' : ''}" type="button" data-calendar-event="${escapeHtml(event.id)}" data-kyle-type="calendar-event" data-kyle-id="${escapeHtml(event.id)}" data-kyle-label="${escapeHtml(event.title)}" title="${escapeHtml(event.deadline_label || event.title)}">
                ${escapeHtml(event.title)}
              </button>`).join('')}
          </div>
        </div>`;
    }

    container.innerHTML = html;
    bindCalendarEventClicks(container, items);
  }

  function eventUrgency(event) {
    if (event.conflict) return 'clash';
    if (event.urgency) return event.urgency;
    const start = parseEventStart(event);
    if (!start) return 'normal';
    const hours = (start.getTime() - Date.now()) / 3600000;
    if (hours <= 6) return 'critical';
    if (hours <= 24) return 'urgent';
    return 'normal';
  }

  function eventTypeClass(event) {
    if (event.source === 'ai') return 'source-ai';
    if (event.source === 'deadline') return 'source-deadline';
    return 'source-google';
  }

  function renderTimedGrid(container, weekStart, items) {
    const START_HOUR = 0;
    const END_HOUR = 24;
    const HOUR_HEIGHT = 56;
    const today = new Date();
    container.dataset.startHour = String(START_HOUR);

    let timeHtml = '<div class="calendar-time-column">';
    for (let hour = START_HOUR; hour <= END_HOUR; hour += 1) {
      const top = (hour - START_HOUR) * HOUR_HEIGHT;
      const labelDate = new Date(2000, 0, 1, hour % 24);
      timeHtml += `<span class="calendar-time-label" style="top:${top}px">${labelDate.toLocaleTimeString([], { hour: 'numeric' })}</span>`;
    }
    timeHtml += '</div>';

    let columns = '';
    for (let dayIndex = 0; dayIndex < 7; dayIndex += 1) {
      const day = addDays(weekStart, dayIndex);
      const isToday = day.toDateString() === today.toDateString();
      const dayKey = toLocalDateInput(day);

      const dayItems = items
        .filter(event => !event.all_day && parseEventStart(event)?.toDateString() === day.toDateString())
        .sort((a, b) => parseEventStart(a) - parseEventStart(b));

      let blocks = '';
      dayItems.forEach(event => {
        const start = parseEventStart(event);
        const end = parseEventEnd(event) || new Date(start.getTime() + 30 * 60000);
        const startMinutes = start.getHours() * 60 + start.getMinutes();
        const crossesDay = end.toDateString() !== start.toDateString() || end <= start;
        const endMinutes = crossesDay ? END_HOUR * 60 : end.getHours() * 60 + end.getMinutes();
        const visibleStart = Math.max(START_HOUR * 60, startMinutes);
        const visibleEnd = Math.min(END_HOUR * 60, Math.max(endMinutes, visibleStart + 15));
        if (visibleEnd <= START_HOUR * 60 || visibleStart >= END_HOUR * 60) return;

        const top = ((visibleStart - START_HOUR * 60) / 60) * HOUR_HEIGHT;
        const height = Math.max(22, ((visibleEnd - visibleStart) / 60) * HOUR_HEIGHT - 2);
        const classes = [
          'calendar-event-block',
          height < 42 ? 'is-compact' : '',
          eventTypeClass(event),
          `urgency-${eventUrgency(event)}`,
          event.conflict ? 'conflict' : ''
        ].filter(Boolean).join(' ');

        blocks += `
          <button class="${classes}" type="button" data-calendar-event="${escapeHtml(event.id)}" data-kyle-type="calendar-event" data-kyle-id="${escapeHtml(event.id)}" data-kyle-label="${escapeHtml(event.title)}" style="top:${top}px;height:${height}px" title="${escapeHtml(event.title)}">
            <strong>${event.source === 'ai' ? '<span class="calendar-ai-mark">AI</span> ' : ''}${escapeHtml(event.title)}</strong>
            <small>${start.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}${event.conflict ? ' · CLASH' : event.source === 'ai' ? ' · AUTO' : ''}</small>
          </button>`;
      });

      let nowLine = '';
      if (isToday) {
        const nowMinutes = today.getHours() * 60 + today.getMinutes();
        if (nowMinutes >= START_HOUR * 60 && nowMinutes <= END_HOUR * 60) {
          const top = ((nowMinutes - START_HOUR * 60) / 60) * HOUR_HEIGHT;
          nowLine = `<div class="calendar-now-line" style="top:${top}px"></div>`;
        }
      }

      columns += `<div class="calendar-day-column ${isToday ? 'is-today' : ''}" data-calendar-day="${dayKey}">${blocks}${nowLine}</div>`;
    }

    container.innerHTML = timeHtml + columns;
    bindCalendarEventClicks(container, items);
  }

  function bindCalendarEventClicks(root, items) {
    root.querySelectorAll('[data-calendar-event]').forEach(button => {
      const event = items.find(item => String(item.id) === button.dataset.calendarEvent);
      if (event) window.MailmateObjects?.register({
        type: 'calendar-event',
        id: String(event.id),
        label: event.title || 'Calendar event',
        page: 'calendar',
        metadata: {
          start: event.start,
          end: event.end,
          source: event.source,
          conflict: event.conflict
        }
      }, button);
      button.addEventListener('click', () => {
        if (!event) return;
        const reference = { type: 'calendar-event', id: String(event.id), label: event.title || 'Calendar event' };
        window.MailmateContext?.select(reference);
        window.MailmateContext?.open(reference);
        state.calendarSelectedEventId = event.source === 'deadline' ? null : event.id;
        if (event.source === 'deadline') {
          showDeadlineInKyle(event);
          return;
        }
        openCalendarModal(event);
      });
    });
  }

  function renderConflictAlert() {
    const alert = $('calendarConflictAlert');
    const text = $('calendarConflictText');
    if (!alert || !text) return;
    const pairCount = state.calendarConflictPairs.length;
    alert.hidden = pairCount === 0;
    alert.style.display = pairCount === 0 ? 'none' : '';
    if (pairCount) {
      text.textContent = `${pairCount} schedule clash${pairCount === 1 ? '' : 'es'} this week. Conflicting events are highlighted in red.`;
    }
  }

  function showDeadlineInKyle(event) {
    window.dispatchEvent(new CustomEvent('harness:kyle-brief', {
      detail: {
        title: 'Email deadline',
        items: [{ title: event.title, meta: event.deadline_label || event.description }]
      }
    }));
  }

  function openCalendarModal(event = null) {
    const modal = $('calendarModal');
    if (!modal) return;

    const now = new Date();
    const start = event ? parseEventStart(event) : new Date(now.getTime() + 30 * 60000);
    if (!event) start.setMinutes(start.getMinutes() < 30 ? 30 : 0, 0, 0);
    const end = event ? (parseEventEnd(event) || new Date(start.getTime() + 3600000)) : new Date(start.getTime() + 3600000);

    $('calendarModalTitle').textContent = event ? 'Edit event' : 'New event';
    $('calendarEventId').value = event?.id || '';
    $('calendarEventTitle').value = event?.title || '';
    $('calendarEventDate').value = toLocalDateInput(start);
    $('calendarEventStart').value = `${String(start.getHours()).padStart(2, '0')}:${String(start.getMinutes()).padStart(2, '0')}`;
    $('calendarEventEnd').value = `${String(end.getHours()).padStart(2, '0')}:${String(end.getMinutes()).padStart(2, '0')}`;
    $('calendarEventLocation').value = event?.location || '';
    $('calendarEventDescription').value = event?.description || '';
    $('calendarDeleteEventBtn').hidden = !event;
    state.calendarSelectedEventId = event?.id || null;
    if (event) window.MailmateContext?.open({ type: 'calendar-event', id: String(event.id), label: event.title || 'Calendar event' });
    modal.hidden = false;
    setTimeout(() => $('calendarEventTitle')?.focus(), 20);
  }

  function closeCalendarModal() {
    const modal = $('calendarModal');
    if (modal) modal.hidden = true;
  }

  function formEventPayload() {
    const date = $('calendarEventDate').value;
    const startTime = $('calendarEventStart').value;
    const endTime = $('calendarEventEnd').value;
    const start = new Date(`${date}T${startTime}:00`);
    let end = new Date(`${date}T${endTime}:00`);
    if (end <= start) end = addDays(end, 1);

    return {
      title: $('calendarEventTitle').value.trim(),
      description: $('calendarEventDescription').value.trim(),
      location: $('calendarEventLocation').value.trim(),
      start: start.toISOString(),
      end: end.toISOString()
    };
  }

  async function saveCalendarEvent(event) {
    event.preventDefault();
    const id = $('calendarEventId').value;
    const payload = formEventPayload();
    if (!payload.title) return;

    const response = await fetch(id ? `${API_BASE}/api/calendar/events/${encodeURIComponent(id)}` : `${API_BASE}/api/calendar/events`, {
      method: id ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      addError(detail.error || `Calendar save returned ${response.status}`);
      return;
    }

    closeCalendarModal();
    await refreshCalendar(true);
    window.dispatchEvent(new CustomEvent('harness:calendar-changed'));
  }

  async function deleteSelectedCalendarEvent() {
    const id = $('calendarEventId').value;
    if (!id) return;
    const selected = state.calendarEvents.find(item => String(item.id) === String(id));
    const reference = { type: 'calendar-event', id: String(id), label: selected?.title || 'Calendar event' };
    closeCalendarModal();
    if (window.KyleExecutor) {
      await window.KyleExecutor.execute({
        id: `calendar_delete_${Date.now().toString(36)}`,
        goal: `Delete ${reference.label}`,
        steps: [{ tool: 'calendar.delete_prepare', args: { references: [reference] } }]
      });
      return;
    }
    addError('Calendar deletion confirmation is unavailable. Reload Mailmate and try again.');
  }

  async function createCalendarEvent(payload) {
    const response = await fetch(`${API_BASE}/api/calendar/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error(`Calendar create returned ${response.status}`);
    const event = await response.json();
    await refreshCalendar(true);
    return event;
  }

  async function updateCalendarEvent(id, payload) {
    const response = await fetch(`${API_BASE}/api/calendar/events/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error(`Calendar update returned ${response.status}`);
    const event = await response.json();
    await refreshCalendar(true);
    return event;
  }

  async function removeCalendarEvent(id) {
    const response = await fetch(`${API_BASE}/api/calendar/events/${encodeURIComponent(id)}`, { method: 'DELETE' });
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `Calendar delete returned ${response.status}`);
    const marker = detail.dismissed_marker || detail.marker;
    if (marker) {
      state.calendarDismissedMarkers.add(marker);
      if (state.data) state.data.calendar_dismissed_markers = [...state.calendarDismissedMarkers];
      saveSessionSnapshot(state.data);
    }
    state.calendarEvents = state.calendarEvents.filter(item => String(item.id) !== String(id));
    renderCalendar();
    await refreshCalendar(true);
    return true;
  }

  async function dismissCalendarDeadline(marker) {
    if (!marker) throw new Error('Deadline marker is missing');
    const response = await fetch(`${API_BASE}/api/calendar/deadlines/dismiss`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ marker })
    });
    const detail = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(detail.error || `Deadline dismissal returned ${response.status}`);
    state.calendarDismissedMarkers.add(marker);
    if (state.data) state.data.calendar_dismissed_markers = [...state.calendarDismissedMarkers];
    saveSessionSnapshot(state.data);
    renderCalendar();
    return detail;
  }

  function automationScheduleLabel(schedule = {}) {
    const type = schedule.type || 'daily';
    if (type === 'interval') return `Every ${schedule.minutes || 60} minutes`;
    if (type === 'once') return `Once · ${formatAutomationDate(schedule.at)}`;
    const [hour = '08', minute = '00'] = String(schedule.time || '08:00').split(':');
    const time = new Date(2000, 0, 1, Number(hour), Number(minute)).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    if (type === 'weekly') {
      const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
      return `Every ${days[Number(schedule.weekday || 0)]} · ${time}`;
    }
    return `Every day · ${time}`;
  }

  function formatAutomationDate(value) {
    if (!value) return 'Not scheduled';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return 'Not scheduled';
    return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  async function loadAutomations() {
    try {
      const response = await fetch(`${API_BASE}/api/automations`);
      if (!response.ok) throw new Error(`Automations returned ${response.status}`);
      state.automations = await response.json();
      renderAutomations();
      return state.automations;
    } catch (error) {
      const list = $('automationList');
      if (list) list.innerHTML = `<p class="automation-empty">${escapeHtml(error.message)}</p>`;
      return [];
    }
  }

  function renderAutomations() {
    const list = $('automationList');
    if (!list) return;
    if (!state.automations.length) {
      list.innerHTML = '<p class="automation-empty">No scheduled runs yet. Create one to have Kyle check your workspace automatically.</p>';
      return;
    }
    list.innerHTML = state.automations.map(automation => `
      <article class="automation-row" data-automation-id="${escapeHtml(automation.id)}" data-kyle-type="automation" data-kyle-id="${escapeHtml(automation.id)}" data-kyle-label="${escapeHtml(automation.name)}">
        <div>
          <small>${escapeHtml(automationScheduleLabel(automation.schedule))}</small>
          <h2>${escapeHtml(automation.name)}</h2>
          <p>${escapeHtml(automation.action?.goal || '')}</p>
          <div class="automation-meta"><span>Last: ${escapeHtml(formatAutomationDate(automation.last_run))}</span><span>Next: ${escapeHtml(formatAutomationDate(automation.next_run))}</span></div>
        </div>
        <dl class="automation-rule">
          <div><dt>When</dt><dd>${escapeHtml(automationScheduleLabel(automation.schedule))}</dd></div>
          <div><dt>Kyle does</dt><dd>${escapeHtml(automation.action?.goal || '')}</dd></div>
          <div><dt>Output</dt><dd>Create a Work summary</dd></div>
        </dl>
        <div class="automation-controls">
          <button class="icon-btn plain-icon automation-run" type="button" title="Run now" aria-label="Run now"><i class="fas fa-play"></i></button>
          <button class="icon-btn plain-icon automation-edit" type="button" title="Edit" aria-label="Edit automation"><i class="fas fa-pen"></i></button>
          <label class="switch" title="${automation.enabled ? 'Disable' : 'Enable'} ${escapeHtml(automation.name)}"><input class="automation-toggle" type="checkbox" ${automation.enabled ? 'checked' : ''}><span></span></label>
        </div>
      </article>
    `).join('');

    list.querySelectorAll('.automation-row').forEach(row => {
      const automation = state.automations.find(item => item.id === row.dataset.automationId);
      if (!automation) return;
      window.MailmateObjects?.register({ type: 'automation', id: automation.id, label: automation.name, page: 'automations' }, row);
      row.querySelector('.automation-edit')?.addEventListener('click', () => openAutomationModal(automation));
      row.querySelector('.automation-run')?.addEventListener('click', async event => {
        const button = event.currentTarget;
        button.disabled = true;
        button.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        await fetch(`${API_BASE}/api/automations/${encodeURIComponent(automation.id)}/run`, { method: 'POST' });
        await loadAutomations();
        setTimeout(() => renderWork(state.data), 350);
      });
      row.querySelector('.automation-toggle')?.addEventListener('change', async event => {
        await updateAutomation(automation.id, { enabled: event.target.checked });
      });
    });
  }

  function updateAutomationScheduleFields() {
    const type = $('automationScheduleType')?.value || 'daily';
    $('automationTimeField').hidden = !['daily', 'weekly'].includes(type);
    $('automationWeekdayField').hidden = type !== 'weekly';
    $('automationOnceField').hidden = type !== 'once';
    $('automationIntervalField').hidden = type !== 'interval';
  }

  function openAutomationModal(automation = null) {
    const modal = $('automationModal');
    if (!modal) return;
    const schedule = automation?.schedule || { type: 'daily', time: '08:00', timezone: 'Asia/Kolkata' };
    $('automationModalTitle').textContent = automation ? 'Edit automation' : 'New automation';
    $('automationId').value = automation?.id || '';
    $('automationName').value = automation?.name || '';
    $('automationGoal').value = automation?.action?.goal || '';
    $('automationEnabled').checked = automation?.enabled !== false;
    $('automationScheduleType').value = schedule.type || 'daily';
    $('automationTime').value = schedule.time || '08:00';
    $('automationWeekday').value = String(schedule.weekday ?? 0);
    $('automationIntervalMinutes').value = String(schedule.minutes || 240);
    if (schedule.at) {
      const at = new Date(schedule.at);
      const local = new Date(at.getTime() - at.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      $('automationOnceAt').value = local;
    } else {
      $('automationOnceAt').value = '';
    }
    $('automationDeleteBtn').hidden = !automation;
    updateAutomationScheduleFields();
    modal.hidden = false;
    document.body.classList.add('modal-open');
    requestAnimationFrame(() => modal.classList.add('is-visible'));
    setTimeout(() => $('automationName')?.focus(), 40);
  }

  function closeAutomationModal() {
    const modal = $('automationModal');
    if (!modal) return;
    modal.classList.remove('is-visible');
    document.body.classList.remove('modal-open');
    setTimeout(() => {
      if (!modal.classList.contains('is-visible')) modal.hidden = true;
    }, 140);
  }

  function automationPayload() {
    const type = $('automationScheduleType').value;
    const schedule = { type, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Kolkata' };
    if (type === 'daily') schedule.time = $('automationTime').value || '08:00';
    if (type === 'weekly') {
      schedule.time = $('automationTime').value || '08:00';
      schedule.weekday = Number($('automationWeekday').value || 0);
    }
    if (type === 'once') schedule.at = new Date($('automationOnceAt').value).toISOString();
    if (type === 'interval') schedule.minutes = Number($('automationIntervalMinutes').value || 240);
    return {
      name: $('automationName').value.trim(),
      enabled: $('automationEnabled').checked,
      schedule,
      action: { type: 'kyle_goal', goal: $('automationGoal').value.trim() },
      output: { type: 'work_summary' }
    };
  }

  async function updateAutomation(id, payload) {
    const response = await fetch(`${API_BASE}/api/automations/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (!response.ok) throw new Error(`Automation update returned ${response.status}`);
    await loadAutomations();
    return response.json().catch(() => ({}));
  }

  function initAutomationControls() {
    $('newAutomationBtn')?.addEventListener('click', () => openAutomationModal());
    $('automationScheduleType')?.addEventListener('change', updateAutomationScheduleFields);
    $('automationModalClose')?.addEventListener('click', closeAutomationModal);
    $('automationCancelBtn')?.addEventListener('click', closeAutomationModal);
    $('automationModal')?.addEventListener('click', event => {
      if (event.target === $('automationModal')) closeAutomationModal();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !$('automationModal')?.hidden) closeAutomationModal();
    });
    $('automationForm')?.addEventListener('submit', async event => {
      event.preventDefault();
      const submitBtn = event.currentTarget.querySelector('button[type="submit"]');
      const name = $('automationName')?.value?.trim() || '';
      const goal = $('automationGoal')?.value?.trim() || '';
      if (!name || !goal) {
        addError('Automation: name and Kyle task are required.');
        return;
      }
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Saving...';
      }
      try {
        const payload = automationPayload();
        const id = $('automationId').value;
        const response = await fetch(id ? `${API_BASE}/api/automations/${encodeURIComponent(id)}` : `${API_BASE}/api/automations`, {
          method: id ? 'PATCH' : 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!response.ok) {
          const detail = await response.json().catch(() => ({}));
          throw new Error(detail.error || response.statusText || `HTTP ${response.status}`);
        }
        closeAutomationModal();
        await loadAutomations();
      } catch (error) {
        addError(`Automation: ${error.message || 'Could not save automation.'}`);
      } finally {
        if (submitBtn) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'Save automation';
        }
      }
    });
    $('automationDeleteBtn')?.addEventListener('click', async () => {
      const id = $('automationId').value;
      if (!id || !window.confirm('Delete this scheduled Kyle run?')) return;
      await fetch(`${API_BASE}/api/automations/${encodeURIComponent(id)}`, { method: 'DELETE' });
      closeAutomationModal();
      await loadAutomations();
    });
  }

  window.AgentAutomations = {
    list: () => [...state.automations],
    refresh: loadAutomations,
    open: () => showTab('automations'),
    runNow: async id => {
      const response = await fetch(`${API_BASE}/api/automations/${encodeURIComponent(id)}/run`, { method: 'POST' });
      if (!response.ok) throw new Error(`Automation run returned ${response.status}`);
      await loadAutomations();
      return response.json();
    },
    setEnabled: (id, enabled) => updateAutomation(id, { enabled })
  };

  window.AgentCalendar = {
    refresh: () => refreshCalendar(true),
    createEvent: createCalendarEvent,
    updateEvent: updateCalendarEvent,
    deleteEvent: removeCalendarEvent,
    getSelectedEvent: () => state.calendarEvents.find(event => event.id === state.calendarSelectedEventId) || null,
    getSelectedEventId: () => state.calendarSelectedEventId,
    getEvents: () => [...state.calendarEvents],
    getVisibleEvents: () => combinedCalendarItems(),
    dismissDeadline: dismissCalendarDeadline,
    open: () => showTab('calendar')
  };

  window.AgentMail = {
    getSelectedEmail: () => {
      if (!state.selectedEmailId) return null;
      const selected = state.data?.emails?.find(email => emailKey(email) === state.selectedEmailId);
      if (!selected) return null;
      return { ...selected, ...(state.fullMessages.get(emailKey(selected)) || {}) };
    },
    getEmails: () => [...(state.data?.emails || [])],
    findContact: query => {
      const q = String(query || '').toLowerCase().trim();
      if (!q) return [];
      const contacts = new Map();
      (state.data?.emails || []).forEach(e => {
        const raw = e.sender || '';
        const match = raw.match(/^(.*?)\s*<(.+?)>$/);
        const name = match ? match[1].replace(/["']/g, '').trim() : raw;
        const email = match ? match[2].trim() : raw;
        if (email && email.includes('@')) {
          const key = email.toLowerCase();
          if (!contacts.has(key)) {
            contacts.set(key, { name: name || key.split('@')[0], email, full: raw });
          }
        }
      });
      return [...contacts.values()].filter(c => c.name.toLowerCase().includes(q) || c.email.toLowerCase().includes(q));
    },
    openEmail: openEmail,
    refresh: () => loadInbox(true)
  };
});
