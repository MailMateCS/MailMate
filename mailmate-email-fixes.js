(function () {
  'use strict';

  const PATCH_ID = '__mailmateEmailSafetyThemePatchV1';
  if (window[PATCH_ID]) return;
  window[PATCH_ID] = true;

  let protectedMode = false;
  let listRefreshQueued = false;
  let detailRefreshQueued = false;
  const originalStyles = new WeakMap();

  function mailContext() {
    const context = window.Kyle?.store?.context || {};
    const emails = context.emails || context.mail?.recent || [];
    return Array.isArray(emails) ? emails : [];
  }

  function emailIds(email) {
    return [email?.id, email?.gmail_id, email?.message_id, email?.source_message_id, email?.threadId, email?.thread_id]
      .filter(Boolean)
      .map(String);
  }

  function emailIndex() {
    const index = new Map();
    mailContext().forEach(email => emailIds(email).forEach(id => index.set(id, email)));
    return index;
  }

  function privacyText(email) {
    const gate = email?.privacy_gate || {};
    return [gate.reason, gate.explanation, gate.category, gate.label, email?.privacy_reason]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();
  }

  function isProtectedEmail(email) {
    if (!email) return false;
    const gate = email.privacy_gate || {};
    const route = String(gate.routing || '').toUpperCase();
    const text = privacyText(email);
    return route === 'BLOCK'
      || route === 'LOCAL_ONLY'
      || /\b(private|sensitive|bank|banking|financial|otp|one[- ]time password|account security)\b/i.test(text);
  }

  function isExplicitlySuspicious(email) {
    if (!email) return false;
    const labels = Array.isArray(email.labels) ? email.labels.map(label => String(label).toUpperCase()) : [];
    if (labels.includes('SPAM')) return true;

    const security = [
      email.security_verdict,
      email.security_classification,
      email.threat_type,
      email.security_analysis?.verdict,
      email.security_analysis?.classification,
      email.security_analysis?.threat,
      email.risk?.verdict
    ].filter(Boolean).join(' ').toLowerCase();
    if (/\b(phish(?:ing)?|scam|malware|credential theft|fraudulent)\b/.test(security)) return true;
    if (email.is_phishing === true || email.is_suspicious === true) return true;

    // Privacy routing is deliberately NOT a threat signal. Banking, OTP and
    // account-security mail is often kept off AI while still being legitimate.
    const trustedSensitive = /recognized (?:banking|financial)|financial institution|known sender|verified sender/.test(privacyText(email));
    if (trustedSensitive) return false;

    const text = `${email.subject || ''} ${email.snippet || ''} ${email.sender || ''}`.toLowerCase();
    const suspiciousUrl = /https?:\/\/(?:\d{1,3}\.){3}\d{1,3}(?:[:/]|$)|\b(?:bit\.ly|tinyurl\.com|is\.gd|cutt\.ly)\/|https?:\/\/[^\s/]*xn--/i.test(text);
    const credentialLure = /\b(?:verify|confirm|restore|unlock)\s+(?:your\s+)?(?:account|identity|password|login)|account\s+(?:suspended|locked|compromised)|credentials?|seed phrase|recovery phrase\b/i.test(text);
    const coerciveMoney = /\b(?:wire|transfer|send)\s+(?:money|funds|payment)|gift cards?|crypto(?:currency)?\s+(?:wallet|payment)|western union|inheritance claim\b/i.test(text);
    const classicScam = /\b(?:lottery winner|you(?:'|’)ve won|claim (?:your )?(?:prize|reward)|crypto giveaway|jackpot winner)\b/i.test(text);
    const dangerousAttachment = /\b(?:open|run|install)\s+(?:the\s+)?attachment\b[^.]{0,80}\.(?:exe|scr|js|vbs|bat|cmd)\b/i.test(text);

    return Boolean(
      (suspiciousUrl && credentialLure)
      || coerciveMoney
      || classicScam
      || dangerousAttachment
    );
  }

  function emailForArticle(article, index) {
    const id = String(article?.dataset?.kyleId || article?.getAttribute('data-kyle-id') || '');
    return index.get(id) || null;
  }

  function riskPill(article) {
    return [...article.querySelectorAll('.privacy-pill')]
      .find(pill => /suspicious/i.test(pill.textContent || '')) || null;
  }

  function markArticle(article, email) {
    if (!article || !email) return;
    const suspicious = isExplicitlySuspicious(email);
    const protectedEmail = isProtectedEmail(email) && !suspicious;
    const pill = riskPill(article);

    article.classList.toggle('is-suspicious', suspicious);
    article.classList.toggle('mailmate-is-protected', protectedEmail);

    if (!suspicious && pill) {
      if (protectedEmail) {
        pill.className = 'privacy-pill mailmate-protected-pill';
        pill.removeAttribute('style');
        pill.innerHTML = '<i class="fas fa-shield-halved"></i> Protected';
      } else {
        pill.remove();
      }
    }
  }

  function ensureProtectedFilter() {
    const filters = document.querySelector('.inbox-filters');
    if (!filters || filters.querySelector('[data-mailmate-filter="protected"]')) return;
    const phishing = filters.querySelector('.filter-tab[data-filter="phishing"]');
    const button = document.createElement('button');
    button.className = 'filter-tab mailmate-protected-filter';
    button.type = 'button';
    button.dataset.mailmateFilter = 'protected';
    button.innerHTML = '<i class="fas fa-shield-halved"></i> Protected <span class="filter-count-badge mailmate-protected-count"></span>';
    filters.insertBefore(button, phishing || null);

    button.addEventListener('click', event => {
      event.preventDefault();
      protectedMode = true;
      const all = filters.querySelector('.filter-tab[data-filter="all"]');
      all?.click();
      requestAnimationFrame(() => {
        protectedMode = true;
        filters.querySelectorAll('.filter-tab').forEach(item => item.classList.remove('active'));
        button.classList.add('active');
        refreshInboxRiskView();
      });
    });

    filters.addEventListener('click', event => {
      const normal = event.target.closest('.filter-tab[data-filter]');
      if (!normal) return;
      protectedMode = false;
      button.classList.remove('active');
      requestAnimationFrame(refreshInboxRiskView);
    }, true);
  }

  function refreshInboxRiskView() {
    if (listRefreshQueued) return;
    listRefreshQueued = true;
    requestAnimationFrame(() => {
      listRefreshQueued = false;
      ensureProtectedFilter();
      const list = document.getElementById('emailList');
      if (!list) return;
      const index = emailIndex();
      let visible = 0;
      let protectedCount = 0;

      mailContext().forEach(email => {
        if (isProtectedEmail(email) && !isExplicitlySuspicious(email)) protectedCount += 1;
      });

      document.querySelectorAll('.mailmate-protected-count').forEach(node => {
        node.textContent = protectedCount ? String(protectedCount) : '';
        node.style.display = protectedCount ? '' : 'none';
      });

      const existingEmpty = list.querySelector('.mailmate-risk-empty');
      const phishingMode = document.querySelector('.filter-tab[data-filter="phishing"]')?.classList.contains('active') && !protectedMode;

      list.querySelectorAll('article.email-item').forEach(article => {
        const email = emailForArticle(article, index);
        if (email) markArticle(article, email);

        let shouldShow = true;
        if (protectedMode) shouldShow = Boolean(email && isProtectedEmail(email) && !isExplicitlySuspicious(email));
        else if (phishingMode) shouldShow = Boolean(email && isExplicitlySuspicious(email));

        article.hidden = !shouldShow;
        article.dataset.mailmateRiskHidden = shouldShow ? 'false' : 'true';
        if (shouldShow) visible += 1;
      });

      if ((protectedMode || phishingMode) && visible === 0) {
        const empty = existingEmpty || document.createElement('div');
        empty.className = 'mailmate-risk-empty';
        empty.textContent = protectedMode
          ? 'No protected messages in the current mailbox view.'
          : 'No suspicious messages detected.';
        if (!existingEmpty) list.appendChild(empty);
      } else if (existingEmpty) {
        existingEmpty.remove();
      }

      if (protectedMode || phishingMode) {
        const count = document.getElementById('inboxCount');
        if (count) count.textContent = `${visible} message${visible === 1 ? '' : 's'}`;
      }
    });
  }

  function parseRgb(value) {
    const match = String(value || '').match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+))?/i);
    if (!match) return null;
    return {
      r: Number(match[1]),
      g: Number(match[2]),
      b: Number(match[3]),
      a: match[4] == null ? 1 : Number(match[4])
    };
  }

  function channelLuminance(value) {
    const c = Math.max(0, Math.min(255, value)) / 255;
    return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  function luminance(rgb) {
    if (!rgb) return 0;
    return 0.2126 * channelLuminance(rgb.r)
      + 0.7152 * channelLuminance(rgb.g)
      + 0.0722 * channelLuminance(rgb.b);
  }

  function rgbToHsl(rgb) {
    let r = rgb.r / 255;
    let g = rgb.g / 255;
    let b = rgb.b / 255;
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    let h = 0;
    let s = 0;
    const l = (max + min) / 2;
    const d = max - min;
    if (d) {
      s = d / (1 - Math.abs(2 * l - 1));
      if (max === r) h = 60 * (((g - b) / d) % 6);
      else if (max === g) h = 60 * (((b - r) / d) + 2);
      else h = 60 * (((r - g) / d) + 4);
    }
    if (h < 0) h += 360;
    return { h, s, l };
  }

  function darkBackground(rgb) {
    const hsl = rgbToHsl(rgb);
    if (hsl.s < 0.08) return '#1b1b1b';
    const saturation = Math.round(Math.min(72, Math.max(22, hsl.s * 100)));
    const lightness = Math.round(Math.min(24, 14 + hsl.s * 8));
    return `hsl(${Math.round(hsl.h)} ${saturation}% ${lightness}%)`;
  }

  function rememberStyle(element) {
    if (!originalStyles.has(element)) originalStyles.set(element, element.getAttribute('style'));
  }

  function setImportantStyle(element, property, value) {
    rememberStyle(element);
    element.style.setProperty(property, value, 'important');
    element.classList.add('mailmate-dark-adapted');
  }

  function adaptElementForDark(element) {
    if (!(element instanceof HTMLElement)) return;
    if (/^(IMG|PICTURE|VIDEO|CANVAS|SVG|SOURCE)$/.test(element.tagName)) return;
    const style = getComputedStyle(element);
    const background = parseRgb(style.backgroundColor);
    const color = parseRgb(style.color);

    if (background && background.a > 0.04 && luminance(background) > 0.55) {
      setImportantStyle(element, 'background-color', darkBackground(background));
    }
    if (color && color.a > 0.15 && luminance(color) < 0.42) {
      setImportantStyle(element, 'color', '#e8e8e8');
    }
    if (element.tagName === 'A') {
      setImportantStyle(element, 'color', '#8ab4f8');
    }

    ['border-top-color', 'border-right-color', 'border-bottom-color', 'border-left-color'].forEach(property => {
      const border = parseRgb(style.getPropertyValue(property));
      if (border && border.a > 0.1 && luminance(border) > 0.72) {
        setImportantStyle(element, property, 'rgba(255,255,255,.18)');
      }
    });
  }

  function restoreLightEmail(surface) {
    surface.classList.remove('mailmate-email-dark');
    const root = surface.shadowRoot || surface;
    root.querySelectorAll('.mailmate-dark-adapted').forEach(element => {
      const original = originalStyles.get(element);
      if (original == null) element.removeAttribute('style');
      else element.setAttribute('style', original);
      element.classList.remove('mailmate-dark-adapted');
      originalStyles.delete(element);
    });
  }

  function isolateRichEmail(surface) {
    if (surface.shadowRoot) return surface.shadowRoot;
    const content = surface.querySelector('.email-html-content');
    if (!content || !surface.attachShadow) return surface;

    const shadow = surface.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = `
      :host { display: block; width: 100%; color: #171717; background: #fff; }
      .email-html-content { width: 100%; max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
      .email-html-content img, .email-html-content video, .email-html-content canvas, .email-html-content svg { max-width: 100%; height: auto; }
      .email-html-content table { max-width: 100%; }
      .email-html-content a { word-break: break-word; }
    `;
    shadow.append(style, content);
    return shadow;
  }

  function adaptRichEmail(surface) {
    const root = isolateRichEmail(surface);
    const dark = document.documentElement.getAttribute('data-theme') === 'dark';
    if (!dark) {
      restoreLightEmail(surface);
      return;
    }

    surface.classList.add('mailmate-email-dark');
    const content = root.querySelector('.email-html-content');
    if (!content) return;
    setImportantStyle(content, 'background-color', '#171717');
    setImportantStyle(content, 'color', '#e8e8e8');

    const nodes = [...content.querySelectorAll('*')].slice(0, 1800);
    nodes.forEach(adaptElementForDark);
  }

  function refreshEmailDetailTheme() {
    if (detailRefreshQueued) return;
    detailRefreshQueued = true;
    requestAnimationFrame(() => {
      detailRefreshQueued = false;
      document.querySelectorAll('.email-rich-surface').forEach(adaptRichEmail);
    });
  }

  function injectStyles() {
    if (document.getElementById('mailmateEmailSafetyThemeStyles')) return;
    const style = document.createElement('style');
    style.id = 'mailmateEmailSafetyThemeStyles';
    style.textContent = `
      .email-item.mailmate-is-protected {
        border-left-color: rgba(139, 92, 246, .65) !important;
        background: rgba(139, 92, 246, .045) !important;
      }
      .mailmate-protected-pill {
        color: #8b5cf6 !important;
        border-color: rgba(139,92,246,.28) !important;
        background: rgba(139,92,246,.08) !important;
      }
      [data-theme="dark"] .mailmate-protected-pill { color: #c4b5fd !important; }
      .mailmate-risk-empty {
        padding: 30px 20px;
        color: var(--muted);
        text-align: center;
        font-size: .82rem;
      }
      [data-theme="dark"] .email-rich-surface.mailmate-email-dark {
        background: #171717 !important;
        color: #e8e8e8 !important;
        color-scheme: dark !important;
        border-color: rgba(255,255,255,.12) !important;
      }
    `;
    document.head.appendChild(style);
  }

  function boot() {
    injectStyles();
    ensureProtectedFilter();
    refreshInboxRiskView();
    refreshEmailDetailTheme();

    const list = document.getElementById('emailList');
    if (list) new MutationObserver(refreshInboxRiskView).observe(list, { childList: true, subtree: true });
    const detail = document.getElementById('emailDetail');
    if (detail) new MutationObserver(refreshEmailDetailTheme).observe(detail, { childList: true, subtree: true });
    new MutationObserver(() => {
      refreshEmailDetailTheme();
      refreshInboxRiskView();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    window.addEventListener('mailmate:context', refreshInboxRiskView);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => setTimeout(boot, 0), { once: true });
  } else {
    setTimeout(boot, 0);
  }
})();
