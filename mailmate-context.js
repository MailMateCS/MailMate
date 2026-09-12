(function () {
  const state = {
    page: 'overview',
    selected: null,
    open: null,
    hovered: null,
    focused: null,
    lastClicked: null,
    selectedText: '',
    selectedTextSource: null,
    references: {
      lastMentioned: null,
      lastOpened: null,
      lastCreated: null,
      lastModified: null,
      lastManipulated: null
    }
  };

  let hoverTimer = null;

  function sameReference(left, right) {
    return Boolean(left && right && left.type === right.type && left.id === right.id);
  }

  function setPage(page) {
    state.page = String(page || 'overview');
    state.hovered = null;
    state.focused = null;
    emit();
  }

  function select(reference) {
    state.selected = reference || null;
    if (reference) state.references.lastMentioned = reference;
    emit();
  }

  function open(reference) {
    state.open = reference || null;
    if (reference) {
      state.references.lastOpened = reference;
      state.references.lastMentioned = reference;
    }
    emit();
  }

  function clear(reference) {
    if (!reference || sameReference(state.selected, reference)) state.selected = null;
    if (!reference || sameReference(state.open, reference)) state.open = null;
    emit();
  }

  function remember(kind, reference) {
    const names = {
      mentioned: 'lastMentioned',
      opened: 'lastOpened',
      created: 'lastCreated',
      modified: 'lastModified',
      manipulated: 'lastManipulated'
    };
    const name = names[kind] || kind;
    if (name in state.references) state.references[name] = reference || null;
    emit();
  }

  function snapshot() {
    const visibleObjects = window.MailmateObjects?.listVisible({ page: state.page, limit: 12 }) || [];
    return {
      page: state.page,
      selected: state.selected,
      open: state.open,
      hovered: state.hovered,
      focused: state.focused,
      lastClicked: state.lastClicked,
      selectedText: String(state.selectedText || '').slice(0, 280),
      selectedTextSource: state.selectedTextSource,
      references: { ...state.references },
      visibleObjects
    };
  }

  function emit() {
    window.dispatchEvent(new CustomEvent('mailmate:context', { detail: snapshot() }));
  }

  document.addEventListener('pointerover', event => {
    const reference = window.MailmateObjects?.getFromElement(event.target);
    if (!reference || sameReference(reference, state.hovered)) return;
    state.hovered = reference;
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => {
      if (sameReference(state.hovered, reference)) state.hovered = null;
    }, 8000);
  }, true);

  document.addEventListener('click', event => {
    const reference = window.MailmateObjects?.getFromElement(event.target);
    if (!reference) return;
    state.lastClicked = reference;
    state.references.lastMentioned = reference;
    emit();
  }, true);

  document.addEventListener('focusin', event => {
    state.focused = window.MailmateObjects?.getFromElement(event.target) || null;
  }, true);

  document.addEventListener('selectionchange', () => {
    const selection = window.getSelection?.();
    state.selectedText = String(selection || '').trim().slice(0, 280);
    const sourceElement = selection?.anchorNode?.nodeType === Node.ELEMENT_NODE
      ? selection.anchorNode
      : selection?.anchorNode?.parentElement;
    state.selectedTextSource = state.selectedText
      ? (window.MailmateObjects?.getFromElement(sourceElement) || null)
      : null;
  });

  window.MailmateContext = { state, setPage, select, open, clear, remember, snapshot };

  // Kyle's original voice bootstrap considered `downloading` to mean Whisper was
  // ready, which could make the first mic request block against a model that was
  // still loading. Normalize that status before kyle.js is evaluated so the
  // browser speech-recognition fallback is used until Whisper is actually ready.
  if (!window.__mailmateVoiceFetchGuardV1 && typeof window.fetch === 'function') {
    window.__mailmateVoiceFetchGuardV1 = true;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async function mailmateFetch(input, init) {
      const url = typeof input === 'string' ? input : String(input?.url || '');
      const response = await nativeFetch(input, init);
      if (!/\/api\/stt\/status(?:\?|$)/.test(url) || !response.ok) return response;

      try {
        const status = await response.clone().json();
        if (status?.downloading && !status?.loaded && !status?.available) {
          const headers = new Headers(response.headers);
          headers.set('content-type', 'application/json');
          return new Response(JSON.stringify({
            ...status,
            downloading: false,
            available: false,
            loaded: false,
            fallback: 'browser-speech-recognition'
          }), {
            status: response.status,
            statusText: response.statusText,
            headers
          });
        }
      } catch (_) {}
      return response;
    };
  }

  // Fork-only stabilization layer. Keep the experimental fixes isolated until
  // they are verified and folded back into the core dashboard modules.
  if (!document.querySelector('script[data-mailmate-fork-fixes]')) {
    const patch = document.createElement('script');
    patch.src = './mailmate-fork-fixes.js?v=1';
    patch.async = false;
    patch.dataset.mailmateForkFixes = 'true';
    patch.onerror = () => console.warn('[MailMate] fork fixes failed to load');
    document.head.appendChild(patch);
  }

  if (!document.querySelector('script[data-mailmate-email-fixes]')) {
    const emailPatch = document.createElement('script');
    emailPatch.src = './mailmate-email-fixes.js?v=1';
    emailPatch.async = false;
    emailPatch.dataset.mailmateEmailFixes = 'true';
    emailPatch.onerror = () => console.warn('[MailMate] email safety/theme fixes failed to load');
    document.head.appendChild(emailPatch);
  }
})();
