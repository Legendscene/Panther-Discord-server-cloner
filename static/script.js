const $ = (id) => document.getElementById(id);

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
  stickers: 'stickers'
};

const el = {
  token: $('token'),
  source: $('sourceGuildId'),
  target: $('targetGuildId'),
  dryRun: $('dryRun'),
  tokenHint: $('tokenHint'),
  status: $('statusPill'),
  log: $('logOutput'),
  jobInfoText: $('jobInfoText'),
  start: $('startCloneBtn'),
  stop: $('stopCloneBtn'),
  reset: $('resetBtn'),
  load: $('loadConfigBtn'),
  clear: $('clearLogBtn')
};

let activeJobId = null;
let pollTimer = null;
let tokenMasked = false;

function setStatus(text, tone = 'ready') {
  const tones = {
    ready: ['rgba(52, 211, 153, 0.18)', 'rgba(52, 211, 153, 0.34)', '#b8f4dc'],
    run: ['rgba(124, 156, 255, 0.18)', 'rgba(124, 156, 255, 0.34)', '#dfe7ff'],
    error: ['rgba(248, 113, 113, 0.15)', 'rgba(248, 113, 113, 0.34)', '#ffd3d3']
  };
  const [bg, border, color] = tones[tone] || tones.ready;
  el.status.textContent = text;
  el.status.style.background = bg;
  el.status.style.borderColor = border;
  el.status.style.color = color;
}

function setJobInfo(text, tone = 'ready') {
  el.jobInfoText.textContent = text;
  el.jobInfoText.parentElement.dataset.tone = tone;
}

function setLog(text) {
  el.log.textContent = text;
  el.log.scrollTop = el.log.scrollHeight;
}

function setRunning(running) {
  el.start.disabled = running;
  el.stop.disabled = !running;
  el.load.disabled = running;
  el.reset.disabled = running;
}

function buildPayload() {
  const options = {};
  Object.entries(OPTION_FIELDS).forEach(([key, id]) => {
    options[key] = $(id).checked;
  });
  return {
    token: el.token.value.trim(),
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
}

async function loadConfig() {
  try {
    const response = await fetch('/api/config');
    const config = await response.json();
    tokenMasked = Boolean(config.has_token);
    el.token.value = config.token || '';
    el.source.value = config.source_guild_id || '';
    el.target.value = config.target_guild_id || '';
    applyOptions(config.options);
    el.tokenHint.textContent = tokenMasked
      ? 'Saved token loaded (masked). Type a new one to replace it.'
      : 'No saved token yet - paste one from the Developer Portal.';
    setStatus(tokenMasked ? 'Config loaded' : 'No token saved', tokenMasked ? 'ready' : 'error');
  } catch (err) {
    setStatus('Config load failed', 'error');
    setLog(`[X] config load failed: ${err}`);
  }
}

async function startClone() {
  if (activeJobId) {
    setStatus('Already running', 'error');
    return;
  }

  const payload = buildPayload();
  const tokenMissing = !payload.token && !tokenMasked;
  if (tokenMissing || !payload.source_guild_id || !payload.target_guild_id) {
    setStatus('Missing data', 'error');
    setLog(
      tokenMissing
        ? 'Bot token daalo (Developer Portal -> Bot -> Reset Token).'
        : 'Source aur Target guild ID dono daalo.'
    );
    return;
  }

  setStatus('Starting', 'run');
  setRunning(true);
  setJobInfo('Starting clone job...', 'run');
  setLog('Launching Discord clone job...\n');

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
      setJobInfo('Job start nahi hua.', 'error');
      setLog(`[X] ${data.error || 'Unknown error'}\n`);
      return;
    }
    activeJobId = data.job_id;
    pollJob();
  } catch (err) {
    setStatus('Error', 'error');
    setRunning(false);
    setLog(`[X] request failed: ${err}\n`);
  }
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function pollJob() {
  if (!activeJobId) {
    return;
  }
  try {
    const response = await fetch(`/api/jobs/${activeJobId}`);
    if (response.status === 404) {
      setStatus('Job lost', 'error');
      setRunning(false);
      setJobInfo('Dashboard restart ho gaya, job logs logs/ me hain.', 'error');
      stopPolling();
      return;
    }
    const data = await response.json();
    setLog(data.log || 'No logs yet.');

    if (data.status === 'completed') {
      setStatus('Completed', 'ready');
      setJobInfo('Clone finished successfully.', 'ready');
      stopPolling();
      setRunning(false);
      activeJobId = null;
      return;
    }
    if (data.status === 'failed') {
      setStatus('Failed', 'error');
      setJobInfo(`Clone failed (exit ${data.exit_code}). Log me error dekho.`, 'error');
      stopPolling();
      setRunning(false);
      activeJobId = null;
      return;
    }
    if (data.status === 'cancelled') {
      setStatus('Cancelled', 'error');
      setJobInfo('Cancelled by user.', 'error');
      stopPolling();
      setRunning(false);
      activeJobId = null;
      return;
    }

    setStatus('Running', 'run');
    setJobInfo('Clone in progress...', 'run');
  } catch (err) {
    setStatus('Connection lost', 'error');
    setJobInfo(`Polling failed: ${err}`, 'error');
    stopPolling();
    setRunning(false);
    return;
  }

  pollTimer = setTimeout(pollJob, 1200);
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
    setLog(`\n[!] stop request failed: ${err}\n`);
  }
}

function clearLog() {
  setLog('No logs yet.');
  setJobInfo('Waiting for start...', 'ready');
  setStatus('Ready', 'ready');
}

function resetForm() {
  stopPolling();
  activeJobId = null;
  tokenMasked = false;
  el.token.value = '';
  el.source.value = '';
  el.target.value = '';
  el.dryRun.checked = false;
  applyOptions({
    server_name: true,
    server_icon: true,
    server_banner: false,
    roles: true,
    categories: true,
    channels: true,
    settings: true,
    permissions: true,
    emojis: false,
    stickers: false
  });
  el.tokenHint.textContent = 'Token sirf localhost dashboard me rehta hai, config.json me save hota hai.';
  setRunning(false);
  clearLog();
}

el.token.addEventListener('input', () => {
  if (el.token.value.trim() && el.token.value.trim() !== '********') {
    tokenMasked = false;
  }
});

el.start.addEventListener('click', startClone);
el.stop.addEventListener('click', stopClone);
el.load.addEventListener('click', loadConfig);
el.clear.addEventListener('click', clearLog);
el.reset.addEventListener('click', resetForm);

resetForm();
loadConfig();
