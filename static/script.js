const $ = (id) => document.getElementById(id);

document.documentElement.classList.add('js');

const TOKEN_TYPE_HINTS = {
  bot: 'Phone or PC: Developer Portal > your application > Bot > Reset Token. Invite the bot to both servers with Manage Roles/Channels/Server/Emojis.',
  user: 'Mobile Discord does not officially reveal user tokens. Paste a user token you already obtained on your PC; it must NOT have a "Bot " prefix.'
};

function tokenTypeHintText(type) {
  return TOKEN_TYPE_HINTS[type] || TOKEN_TYPE_HINTS.bot;
}

const OPTION_FIELDS = {
  server_name: 'serverName',
  server_icon: 'serverIcon',
  server_banner: 'serverBanner',
  roles: 'roles',
  categories: 'categories',
  channels: 'channels',
  settings: 'settings',
  permissions: 'permissions',
  emojis: 'emojis',
  stickers: 'stickers',
  clean_target: 'cleanTarget'
};

const DEFAULTS = {
  server_name: true,
  server_icon: true,
  server_banner: false,
  roles: true,
  categories: true,
  channels: true,
  settings: true,
  permissions: true,
  emojis: false,
  stickers: false,
  clean_target: true
};

const STEPS = [1, 2, 3];

const el = {
  token: $('token'),
  tokenType: $('tokenType'),
  tokenTypeHint: $('tokenTypeHint'),
  source: $('sourceGuildId'),
  target: $('targetGuildId'),
  dryRun: $('dryRun'),
  tokenHint: $('tokenHint'),
  status: $('statusPill'),
  statusText: $('statusText'),
  log: $('logOutput'),
  jobInfo: $('jobInfo'),
  jobInfoText: $('jobInfoText'),
  start: $('startCloneBtn'),
  stop: $('stopCloneBtn'),
  reset: $('resetBtn'),
  load: $('loadConfigBtn'),
  clear: $('clearLogBtn'),
  theme: $('themeToggleBtn'),
  autoScroll: $('autoScrollToggle'),
  optionCount: $('optionCount'),
  progress: $('progressTrack'),
  statStatus: $('statStatus'),
  statElapsed: $('statElapsed'),
  statLines: $('statLines'),
  statExit: $('statExit'),
  wizard: $('wizard'),
  back: $('backBtn'),
  next: $('nextBtn')
};

const ICON_SUN =
  '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6"/>';
const ICON_MOON = '<path d="M20.5 14.3A8.5 8.5 0 1 1 9.7 3.5a6.8 6.8 0 0 0 10.8 10.8z"/>';

const dots = Array.from(document.querySelectorAll('.step-dot'));
const panels = Array.from(document.querySelectorAll('.step-panel'));

let activeJobId = null;
let pollTimer = null;
let tickTimer = null;
let startedAt = null;
let currentStep = 1;
let maxUnlocked = 1;

/* ---------------- theme ---------------- */

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  if (el.theme) {
    el.theme.innerHTML =
      '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" ' +
      'stroke-width="1.6" stroke-linecap="round">' +
      (theme === 'dark' ? ICON_SUN : ICON_MOON) +
      '</svg>';
  }
  try {
    localStorage.setItem('qd-theme', theme);
  } catch (err) {
    /* storage unavailable */
  }
}

function initTheme() {
  let stored = null;
  try {
    stored = localStorage.getItem('qd-theme');
  } catch (err) {
    /* storage unavailable */
  }
  applyTheme(stored === 'light' ? 'light' : 'dark');
}

/* ---------------- scroll reveal ---------------- */

function initReveal() {
  const targets = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    targets.forEach((node) => node.classList.add('is-visible'));
    return;
  }
  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: '0px 0px -8% 0px' }
  );
  targets.forEach((node, index) => {
    node.style.transitionDelay = `${Math.min(index, 4) * 70}ms`;
    observer.observe(node);
  });
}

/* ---------------- wizard ---------------- */

function isRunning() {
  return activeJobId !== null;
}

function stepOneReady() {
  const token = el.token.value.trim();
  const source = el.source.value.trim();
  const target = el.target.value.trim();
  return Boolean(token) && /^\d+$/.test(source) && /^\d+$/.test(target) && source !== target;
}

function validateStepOne(showFeedback = true) {
  const token = el.token.value.trim();
  const source = el.source.value.trim();
  const target = el.target.value.trim();

  if (!token) {
    if (showFeedback) {
      setStatus('Missing token', 'error');
      el.token.focus();
    }
    return false;
  }
  if (!source || !target || !/^\d+$/.test(source) || !/^\d+$/.test(target)) {
    if (showFeedback) {
      setStatus('Missing guild IDs', 'error');
      (!source || !/^\d+$/.test(source) ? el.source : el.target).focus();
    }
    return false;
  }
  if (source === target) {
    if (showFeedback) {
      setStatus('Source and target must differ', 'error');
      el.target.focus();
    }
    return false;
  }
  return true;
}

function goToStep(step, direction = 'forward', shouldScroll = true) {
  if (!STEPS.includes(step)) {
    return;
  }
  if (step > maxUnlocked) {
    return;
  }
  if (isRunning() && step !== 3) {
    return;
  }

  currentStep = step;
  panels.forEach((panel) => {
    const active = Number(panel.dataset.panel) === step;
    panel.classList.toggle('is-active', active);
    panel.classList.remove('enter-forward', 'enter-back');
    if (active) {
      void panel.offsetWidth;
      panel.classList.add(direction === 'back' ? 'enter-back' : 'enter-forward');
    }
  });
  updateWizard();
  updateWizardNav();

  if (document.activeElement instanceof HTMLElement) {
    document.activeElement.blur();
  }
  const rect = el.wizard.getBoundingClientRect();
  const topBarOffset = 90;
  if (shouldScroll && (rect.top < topBarOffset || rect.top > window.innerHeight * 0.4)) {
    el.wizard.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function updateWizard() {
  el.wizard.dataset.step = String(currentStep);
  dots.forEach((dot) => {
    const number = Number(dot.dataset.step);
    dot.classList.toggle('is-active', number === currentStep);
    dot.classList.toggle('is-complete', number < currentStep);
    dot.disabled = isRunning() ? number !== currentStep : number > maxUnlocked;
  });
}

function updateWizardNav() {
  el.back.hidden = currentStep === 1;
  el.next.hidden = currentStep !== 1;
  el.reset.hidden = currentStep !== 3;

  el.back.disabled = currentStep === 1 || isRunning();
  el.next.disabled = currentStep !== 1 || isRunning() || !stepOneReady();
  if (!isRunning()) {
    el.reset.disabled = false;
  }
}

function handleNext() {
  if (currentStep !== 1 || isRunning()) {
    return;
  }
  if (!validateStepOne(true)) {
    updateWizardNav();
    return;
  }
  maxUnlocked = Math.max(maxUnlocked, 2);
  goToStep(2, 'forward');
  setStatus('Step 2 of 3', 'ready');
  setJobInfo('Choose what to clone, then start the run.', 'ready');
}

function handleBack() {
  if (currentStep <= 1 || isRunning()) {
    return;
  }
  goToStep(currentStep - 1, 'back');
}

function handleStepDot(event) {
  const target = Number(event.currentTarget.dataset.step);
  if (isRunning()) {
    return;
  }
  if (target <= maxUnlocked) {
    goToStep(target, target > currentStep ? 'forward' : 'back');
  }
}

/* ---------------- status / stats ---------------- */

function setStatus(text, tone = 'ready') {
  el.status.dataset.tone = tone;
  el.statusText.textContent = text;
  el.statStatus.textContent = text;
}

function setJobInfo(text, tone = 'ready') {
  el.jobInfoText.textContent = text;
  el.jobInfo.dataset.tone = tone;
}

function setRunning(running) {
  el.start.disabled = running;
  el.stop.disabled = !running;
  el.load.disabled = running;
  el.reset.disabled = running;
  el.progress.dataset.active = String(running);
  el.token.readOnly = running;
  el.source.readOnly = running;
  el.target.readOnly = running;
  document
    .querySelectorAll('#wizard [data-panel="2"] input[type="checkbox"]')
    .forEach((checkbox) => {
      checkbox.disabled = running;
    });
  if (running) {
    startTicker();
  } else {
    stopTicker();
  }
  updateWizard();
  updateWizardNav();
}

function setExit(code) {
  el.statExit.textContent = code === null || code === undefined ? '—' : String(code);
}

function startTicker() {
  stopTicker();
  tickTimer = setInterval(() => {
    if (startedAt === null) {
      return;
    }
    const total = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
    const mm = String(Math.floor(total / 60)).padStart(2, '0');
    const ss = String(total % 60).padStart(2, '0');
    el.statElapsed.textContent = `${mm}:${ss}`;
  }, 500);
}

function stopTicker() {
  if (tickTimer) {
    clearInterval(tickTimer);
    tickTimer = null;
  }
}

function resetTicker() {
  startedAt = null;
  el.statElapsed.textContent = '00:00';
  stopTicker();
}

/* ---------------- log rendering ---------------- */

// Incremental log state: we only fetch and render NEW bytes since the last
// poll, so the output panel updates smoothly instead of re-rendering the
// whole log every 1.2s.
let logOffset = 0;
let logBuffer = '';
let logRenderQueued = false;

function appendLogText(chunk) {
  if (!chunk) {
    return;
  }
  logBuffer += chunk;
  if (!logRenderQueued) {
    logRenderQueued = true;
    requestAnimationFrame(() => {
      logRenderQueued = false;
      const text = logBuffer;
      logBuffer = '';
      const frag = document.createDocumentFragment();
      const lines = text.split('\n');
      const existingText = el.log.textContent || '';
      const endsWithNewline = existingText === '' || existingText.endsWith('\n');
      lines.forEach((line, index) => {
        const isFirst = index === 0;
        if (!endsWithNewline && isFirst) {
          // previous chunk ended mid-line; append to the last text node
          const last = el.log.lastChild;
          const cls = classify((last?.textContent || '') + line);
          if (last && last.nodeType === Node.TEXT_NODE) {
            last.textContent += line;
          } else if (last && cls) {
            last.textContent = (last.textContent || '') + line;
          } else if (last) {
            last.textContent = (last.textContent || '') + line;
          } else if (line) {
            frag.appendChild(document.createTextNode(line));
          }
        } else {
          const cls = classify(line);
          if (cls) {
            const span = document.createElement('span');
            span.className = cls;
            span.textContent = line;
            frag.appendChild(span);
          } else {
            frag.appendChild(document.createTextNode(line));
          }
        }
        if (index < lines.length - 1) {
          frag.appendChild(document.createTextNode('\n'));
        }
      });
      el.log.appendChild(frag);
      // trim the DOM if it grows huge
      while (el.log.childNodes.length > 8000) {
        el.log.removeChild(el.log.firstChild);
      }
      const lineCount = (el.log.textContent || '').split('\n').filter((l) => l.trim() !== '').length;
      el.statLines.textContent = String(lineCount);
      if (!el.autoScroll || el.autoScroll.checked) {
        el.log.scrollTop = el.log.scrollHeight;
      }
    });
  }
}

function classify(line) {
  if (line.startsWith('[+]')) {
    return 'lv-ok';
  }
  if (line.startsWith('[!]')) {
    return 'lv-warn';
  }
  if (line.startsWith('[X]')) {
    return 'lv-err';
  }
  if (line.startsWith('[i]')) {
    return 'lv-run';
  }
  return null;
}

function renderLog(text) {
  const lines = (text || '').split('\n');
  el.statLines.textContent = String(lines.filter((line) => line.trim() !== '').length);

  const frag = document.createDocumentFragment();
  lines.forEach((line, index) => {
    const cls = classify(line);
    if (cls) {
      const span = document.createElement('span');
      span.className = cls;
      span.textContent = line;
      frag.appendChild(span);
    } else {
      frag.appendChild(document.createTextNode(line));
    }
    if (index < lines.length - 1) {
      frag.appendChild(document.createTextNode('\n'));
    }
  });

  el.log.replaceChildren(frag);

  if (!el.autoScroll || el.autoScroll.checked) {
    el.log.scrollTop = el.log.scrollHeight;
  }
}

/* ---------------- options ---------------- */

function updateOptionCount() {
  const count = Object.values(OPTION_FIELDS).filter((id) => $(id).checked).length;
  el.optionCount.textContent = `${count} selected`;
}

function buildPayload() {
  const options = {};
  Object.entries(OPTION_FIELDS).forEach(([key, id]) => {
    options[key] = $(id).checked;
  });
  return {
    token: el.token.value.trim(),
    token_type: el.tokenType ? el.tokenType.value : 'bot',
    source_guild_id: el.source.value.trim(),
    target_guild_id: el.target.value.trim(),
    dry_run: el.dryRun.checked,
    options
  };
}

function applyOptions(options = {}) {
  Object.entries(OPTION_FIELDS).forEach(([key, id]) => {
    if (key in options) {
      $(id).checked = Boolean(options[key]);
    }
  });
  updateOptionCount();
}

/* ---------------- config ---------------- */

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    const config = await response.json();
    el.source.value = config.source_guild_id || '';
    el.target.value = config.target_guild_id || '';
    if (el.tokenType && config.token_type) {
      el.tokenType.value = config.token_type;
    }
    updateTokenTypeHint();
    applyOptions(config.options);
    el.tokenHint.textContent = config.has_token
      ? 'A token is cached server-side for this session only.'
      : 'Kept in memory for this tab only — never written to config.json.';
    if (el.source.value.trim() && el.target.value.trim()) {
      maxUnlocked = Math.max(maxUnlocked, 2);
    }
    setStatus('Ready', 'ready');
    updateWizard();
    updateWizardNav();
  } catch (err) {
    setStatus('Config load failed', 'error');
    renderLog(`[X] config load failed: ${err}\n`);
  }
}

/* ---------------- job control ---------------- */

async function startClone() {
  if (activeJobId) {
    maxUnlocked = Math.max(maxUnlocked, 3);
    goToStep(3, 'forward');
    setStatus('Already running', 'error');
    return;
  }

  if (!validateStepOne(true)) {
    goToStep(1, 'back');
    return;
  }

  const payload = buildPayload();
  setStatus('Starting', 'run');
  setRunning(true);
  setExit(null);
  setJobInfo('Starting clone job...', 'run');
  logOffset = 0;
  logBuffer = '';
  renderLog('[i] Launching Discord clone job...\n');

  try {
    const response = await fetch('/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
      setStatus('Rejected', 'error');
      setRunning(false);
      setJobInfo('The job could not be started.', 'error');
      renderLog(`[X] ${data.error || 'Unknown error'}\n`);
      return;
    }
    activeJobId = data.job_id;
    startedAt = Date.now() / 1000;
    maxUnlocked = Math.max(maxUnlocked, 3);
    goToStep(3, 'forward');
    pollJob();
  } catch (err) {
    setStatus('Error', 'error');
    setRunning(false);
    renderLog(`[X] request failed: ${err}\n`);
  }
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function finishJob() {
  activeJobId = null;
  setRunning(false);
}

async function pollJob() {
  if (!activeJobId) {
    return;
  }
  try {
    // Incremental feed: ask only for the bytes after what we already rendered.
    const response = await fetch(`/api/jobs/${activeJobId}/stream?offset=${logOffset}`);
    if (response.status === 404) {
      setStatus('Job lost', 'error');
      setJobInfo('The dashboard was restarted. Job logs are still in the logs/ folder.', 'error');
      stopPolling();
      finishJob();
      return;
    }
    const data = await response.json();
    if (data.log_slice) {
      appendLogText(data.log_slice);
      logOffset = data.next_offset;
    }
    setExit(data.exit_code);

    if (data.status === 'completed') {
      setStatus('Completed', 'ready');
      setJobInfo('Clone finished successfully.', 'ready');
      stopPolling();
      finishJob();
      return;
    }
    if (data.status === 'failed') {
      setStatus('Failed', 'error');
      setJobInfo(`Clone failed (exit ${data.exit_code}). Check the log for details.`, 'error');
      stopPolling();
      finishJob();
      return;
    }
    if (data.status === 'cancelled') {
      setStatus('Cancelled', 'error');
      setJobInfo('Cancelled by user.', 'error');
      stopPolling();
      finishJob();
      return;
    }

    setStatus('Running', 'run');
    setJobInfo('Clone in progress...', 'run');
  } catch (err) {
    setStatus('Connection lost', 'error');
    setJobInfo(`Polling failed: ${err}`, 'error');
    stopPolling();
    finishJob();
    return;
  }

  // Fast poll keeps the output feeling live; the incremental endpoint makes
  // each poll tiny so this is cheap.
  pollTimer = setTimeout(pollJob, 600);
}

async function stopClone() {
  if (!activeJobId) {
    return;
  }
  el.stop.disabled = true;
  setJobInfo('Stopping...', 'run');
  try {
    await fetch(`/api/jobs/${activeJobId}/stop`, { method: 'POST' });
  } catch (err) {
    renderLog(`\n[!] stop request failed: ${err}\n`);
  }
}

function clearLog() {
  renderLog('No logs yet.');
  setJobInfo('Waiting for start...', 'ready');
  setStatus('Ready', 'ready');
  setExit(null);
}

function updateTokenTypeHint() {
  if (el.tokenTypeHint) {
    const type = el.tokenType ? el.tokenType.value : 'bot';
    el.tokenTypeHint.textContent = tokenTypeHintText(type);
    if (el.token) {
      el.token.placeholder = type === 'bot' ? 'Bot token from Developer Portal' : 'User token from your PC';
    }
  }
}

function resetForm() {
  stopPolling();
  activeJobId = null;
  resetTicker();
  el.token.value = '';
  el.source.value = '';
  el.target.value = '';
  if (el.tokenType) el.tokenType.value = 'bot';
  updateTokenTypeHint();
  el.dryRun.checked = false;
  applyOptions(DEFAULTS);
  el.tokenHint.textContent = 'Kept in memory for this tab only — never written to config.json.';
  currentStep = 1;
  maxUnlocked = 1;
  goToStep(1, 'back', false);
  setRunning(false);
  clearLog();
}

/* ---------------- wiring ---------------- */

el.start.addEventListener('click', startClone);
el.stop.addEventListener('click', stopClone);
el.load.addEventListener('click', loadConfig);
el.clear.addEventListener('click', clearLog);
el.reset.addEventListener('click', resetForm);
el.back.addEventListener('click', handleBack);
el.next.addEventListener('click', handleNext);
dots.forEach((dot) => dot.addEventListener('click', handleStepDot));

[el.token, el.source, el.target].forEach((field) => {
  field.addEventListener('input', () => {
    if (currentStep === 1) {
      updateWizardNav();
    }
  });
});

if (el.tokenType) {
  el.tokenType.addEventListener('change', () => {
    updateTokenTypeHint();
    if (el.token && el.token.value.trim()) {
      el.token.value = '';
      renderLog(
        "[!] Token type changed — the previous token was cleared. Paste a token that matches the selected type.\n"
      );
    }
  });
}

if (el.theme) {
  el.theme.addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    applyTheme(current === 'dark' ? 'light' : 'dark');
  });
}

if (el.autoScroll) {
  el.autoScroll.addEventListener('change', () => {
    if (el.autoScroll.checked) {
      el.log.scrollTop = el.log.scrollHeight;
    }
  });
}

Object.values(OPTION_FIELDS).forEach((id) => {
  $(id).addEventListener('change', updateOptionCount);
});

initTheme();
initReveal();
updateTokenTypeHint();
resetForm();
loadConfig();
